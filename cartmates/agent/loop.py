"""One agent turn: model -> tools -> model, until it answers or the round limit is hit."""
import logging
import sqlite3
import time
from datetime import datetime, timezone

from telegram.error import NetworkError

from cartmates.agent import memory
from cartmates.agent.errors import TurnFailedAfterChanges, done_so_far
from cartmates import tracing
from cartmates.agent.llm import LLM, LLMUnavailable, traced_respond
from cartmates.agent.prompt import SYSTEM_PROMPT
from cartmates.agent.tools import SCHEMAS, Toolbox
from cartmates.domain import address
from cartmates.swiggy.errors import LoggedOut, LoggedOutAfterChanges
from cartmates.telegram.events import Message
from cartmates.telegram.outbox import Outbox

log = logging.getLogger(__name__)

MAX_ROUNDS = 10
MAX_TOOL_CALLS = 40  # per turn, across all rounds
APOLOGY_TOO_LONG = "Sorry, I got tangled up on that one. Anything I already did is in the queue; try rephrasing the rest."
TOO_LONG_AFTER_CHANGES = "I ran out of steps before finishing, so ask me again for the rest."
APOLOGY_DOWN = "Sorry, my brain is unreachable right now. Nothing was changed; try again in a bit."

FAILED_CONNECTION = "(failed: connection problem)"
FAILED = "(failed: error)"


async def run_turn(**kwargs) -> None:
    """The agent turn inside an "agent.turn" span; see _run_turn for the arguments."""
    event: Message = kwargs["event"]
    with tracing.span("agent.turn", tracing.AGENT,
                      {"session.id": tracing.session_id(event.chat_id), "user.id": str(event.user_id)}) as sp:
        tracing.set_text(sp, "input.value", kwargs["text"])
        await _run_turn(sp, **kwargs)


async def _run_turn(sp, *, conn: sqlite3.Connection, llm: LLM, swiggy, outbox: Outbox,
                   event: Message, text: str, free_delivery_min: int, now: datetime | None = None) -> None:
    """Run the agent for one message and post the reply. Changes made by tools are already
    committed and each is also recorded as an action line; they stay even if a later round fails.
    A Swiggy LoggedOut propagates so the caller can park the message; nothing is remembered then,
    so the replay starts clean. If the turn had already changed the queue, LoggedOutAfterChanges is
    raised instead (the message and the actions are remembered, and it must not be replayed).
    Any other exception that escapes the turn is also remembered: with changes made it becomes
    TurnFailedAfterChanges (actions kept), without it the user row and a "(failed: ...)" assistant row
    are stored and the original exception propagates."""
    now = now or datetime.now(timezone.utc)
    tools = Toolbox(conn, swiggy, address.current(conn), outbox, event.chat_id, event.message_id,
                    event.user_id, event.display_name, now)
    replied_to = event.reply_to_message_id if event.reply_to_bot else None
    speaker = memory.speaker_label(conn, event.display_name, event.user_id, replied_to)
    context = (memory.build_snapshot(conn, event.user_id, now, free_delivery_min)
               + "\n\nRecent chat:\n" + (memory.transcript(conn, now) or "(none)"))
    items: list[dict] = [{"role": "user", "content": context},
                         {"role": "user", "content": f"New message from {speaker}: {text}"}]

    started = time.monotonic()
    rounds = tokens_in = tokens_out = 0
    called: list[str] = []
    outcome = "replied"
    reply: str | None = None
    capped = False
    try:
        for _ in range(MAX_ROUNDS):
            try:
                turn = await traced_respond(llm, SYSTEM_PROMPT, items, SCHEMAS)
            except LLMUnavailable:
                reply, outcome = APOLOGY_DOWN, "llm_down"
                break
            rounds += 1
            tokens_in += turn.input_tokens
            tokens_out += turn.output_tokens
            if not turn.tool_calls:
                reply = turn.text or _fallback_summary(tools)
                break
            for call in turn.tool_calls:  # sequentially: each tool's DB change is its own transaction
                if len(called) >= MAX_TOOL_CALLS:
                    capped = True
                    break
                items.append({"type": "function_call", "call_id": call.id, "name": call.name,
                              "arguments": call.arguments})
                called.append(call.name)
                output = await tools.run(call.name, call.arguments)
                log.debug("tool %s(%s) -> %s", call.name, call.arguments, output)
                items.append({"type": "function_call_output", "call_id": call.id, "output": output})
                if tools.ended:
                    break
            if tools.ended:
                outcome = tools.outcome
                break
            if capped:
                break
        else:
            capped = True
        if capped:
            reply, outcome = _too_long_reply(tools.actions), "too_long"
        elif not tools.ended and outcome == "replied" and tools.asked and tools.asked == len(tools.actions):
            outcome = "asked"  # only questions, nothing added
    except LoggedOut:
        _summarize(sp, rounds, called, tokens_in, tokens_out, "logged_out")
        if not tools.actions:
            raise
        memory.remember(conn, "user", f"{speaker}: {text}", now, event.user_id)
        for action in tools.actions:
            memory.remember(conn, "action", action, now)
        await _tell_owners_alone(outbox, event, tools)
        raise LoggedOutAfterChanges(tools.actions) from None
    except Exception as e:
        _summarize(sp, rounds, called, tokens_in, tokens_out, "failed")
        _remember_failure(conn, tools.actions, speaker, text, now, event.user_id, e)
        if tools.actions:
            await _tell_owners_alone(outbox, event, tools)
            raise TurnFailedAfterChanges(tools.actions, e) from e
        raise
    _summarize(sp, rounds, called, tokens_in, tokens_out, outcome)
    tracing.set_text(sp, "output.value", tools.ended or reply)
    log.info("turn: rounds=%d tools=%s tokens_in=%d tokens_out=%d ms=%d outcome=%s", rounds,
             ",".join(called) or "-", tokens_in, tokens_out, (time.monotonic() - started) * 1000, outcome)

    # propose_order already sent its own message (an order card or the scope question).
    reply_text = tools.ended or reply
    memory.remember(conn, "user", f"{speaker}: {text}", now, event.user_id)
    for action in tools.actions:
        memory.remember(conn, "action", action, now)
    sent_id = tools.ended_message_id
    if tools.ended:  # the card was the reply, so the owners get their own message
        await _tell_owners_alone(outbox, event, tools)
    else:
        try:  # owners are tagged on the reply itself, so one message and one notification
            sent_id = await outbox.send(event.chat_id, reply, reply_to=event.message_id,
                                        mentions=tools.mentions() or None)
        except Exception as e:  # the user and action rows are already stored above
            if tools.actions:
                await _tell_owners_alone(outbox, event, tools)
                raise TurnFailedAfterChanges(tools.actions, e) from e
            memory.remember(conn, "assistant", FAILED_CONNECTION if isinstance(e, NetworkError) else FAILED,
                            now, to_user_id=event.user_id)
            raise
    memory.remember(conn, "assistant", reply_text, now, to_user_id=event.user_id, message_id=sent_id)


async def _tell_owners_alone(outbox: Outbox, event: Message, tools: Toolbox) -> None:
    """When the turn can't carry the mentions on its reply: send them alone, best effort."""
    mentions = tools.mentions()
    if not mentions:
        return
    try:
        await outbox.send(event.chat_id, "", reply_to=event.message_id, mentions=mentions)
    except Exception as e:
        log.warning("could not tell the owners (%s: %s)", type(e).__name__, e)


def _fallback_summary(tools: Toolbox) -> str:
    """The reply when the model ended the turn with no text: built from what the tools did."""
    if not tools.actions:
        return "Sorry, I have nothing to say to that."
    changes = [a for a in tools.actions if not a.startswith("asked ")]
    done = f"Done: {done_so_far(changes)}. " if changes else ""
    return (done + ("Pick an option above." if tools.asked else "")).strip()


def _too_long_reply(actions: list[str]) -> str:
    if not actions:
        return APOLOGY_TOO_LONG
    return f"Done so far: {done_so_far(actions)}. {TOO_LONG_AFTER_CHANGES}"


def _remember_failure(conn: sqlite3.Connection, actions: list[str], speaker: str, text: str, now: datetime,
                      user_id: int, error: Exception) -> None:
    """A crashed turn still shows in the transcript: the message, what it changed, and (when nothing
    changed) a failure row so the attempt isn't invisible."""
    memory.remember(conn, "user", f"{speaker}: {text}", now, user_id)
    for action in actions:
        memory.remember(conn, "action", action, now)
    if not actions:
        memory.remember(conn, "assistant", FAILED_CONNECTION if isinstance(error, NetworkError) else FAILED,
                        now, to_user_id=user_id)


def _summarize(sp, rounds: int, called: list[str], tokens_in: int, tokens_out: int, outcome: str) -> None:
    sp.set_attributes({"cartmates.rounds": rounds, "cartmates.tools": ",".join(called) or "-",
                       "cartmates.outcome": outcome, "llm.token_count.prompt": tokens_in,
                       "llm.token_count.completion": tokens_out, "llm.token_count.total": tokens_in + tokens_out})
