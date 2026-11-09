"""A network drop mid-turn (B1): outbox retries, an honest reply after partial changes, no dangling
question, and a transcript that shows the attempt. Telegram is faked; the LLM is scripted."""
import asyncio

import httpx
import pytest
from telegram.error import NetworkError, TimedOut

from queuify.agent import memory
from queuify.agent.llm import ScriptedLLM
from queuify.handlers import Handlers
from queuify.lane import Lane
from queuify.telegram.adapter import TelegramAdapter
from queuify.telegram.outbox import FakeOutbox, TelegramOutbox, is_connect_error
from test_agent import AgentFlow, call, lines, rig, run, say, use  # noqa: F401 (rig is a fixture)
from test_swiggy import Rig
from test_telegram import BOT_ID, BOT_NAME, GROUP


def connect_error() -> NetworkError:
    """What python-telegram-bot raises when the connection to Telegram cannot be opened."""
    try:
        try:
            raise httpx.ConnectError("all connection attempts failed")
        except httpx.ConnectError as e:
            raise NetworkError("httpx.ConnectError: all connection attempts failed") from e
    except NetworkError as e:
        return e


def two_adds_then_question():
    return [use(call("search_catalog", query="milk")),
            use(call("add_to_queue", "c1", ref="p2v1", qty=1, original_text="diet coke"),
                call("add_to_queue", "c2", ref="p2v1", qty=1, original_text="salt"),
                call("ask_variant", question="Which milk?", refs=["p1v1", "p2v1"], qty=1,
                     owners=["me"], original_text="milk")),
            say("Added two; pick milk above.")]


def is_question(sent) -> bool:
    return sent.buttons is not None


def memory_rows(conn):
    return [(r["role"], r["text"]) for r in conn.execute("SELECT role, text FROM chat_memory ORDER BY id")]


def pending_count(conn):
    return conn.execute("SELECT count(*) FROM pending").fetchone()[0]


def test_dropped_question_send_reports_what_was_done_and_leaves_nothing_dangling(rig):
    async def go():
        async with AgentFlow(rig, two_adds_then_question()) as f:
            f.outbox.fail_sends(connect_error(), when=is_question)
            await f.ask("diet coke, salt and some milk")
            return f
    f = run(go())
    assert len(lines(rig.conn)) == 2
    (reply,) = f.texts()
    assert reply.startswith("Done so far: added #1 ") and "; added #2 " in reply
    assert "connection problem" in reply and "Something went wrong" not in reply
    assert pending_count(rig.conn) == 0
    rows = memory_rows(rig.conn)
    assert [role for role, _ in rows] == ["user", "action", "action"]
    assert rows[0][1].endswith("diet coke, salt and some milk")


def test_a_question_send_that_fails_once_goes_out_on_retry(rig):
    class Bot:
        def __init__(self):
            self.calls, self.failures = [], [connect_error()]

        async def send_message(self, chat_id, text, **kw):
            self.calls.append(text)
            if kw.get("reply_markup") and self.failures:
                raise self.failures.pop()
            return type("Sent", (), {"message_id": 500 + len(self.calls)})()

    bot, naps = Bot(), []

    async def nap(seconds):
        naps.append(seconds)

    async def go():
        outbox = TelegramOutbox(bot, sleep=nap)
        llm = ScriptedLLM(two_adds_then_question())
        h = Handlers(rig.conn, outbox, GROUP, rig.auth, rig.client, "aaaa1111bbbb2222cccc__FAKEsuffix123", llm, 199)
        lane = Lane(h.handle, outbox)
        lane.start()
        from test_agent import MENTION
        from test_telegram import make_update
        await TelegramAdapter(lane, outbox, GROUP, BOT_ID, BOT_NAME).on_update(
            make_update("@QueuifyBot diet coke, salt and some milk", entities=MENTION))
        await asyncio.sleep(0.3)
        await lane.stop()
    run(go())
    assert naps == [1.0]
    assert bot.calls == ["Which milk?", "Which milk?", "Added two; pick milk above."]  # question failed once, then delivered
    assert len(lines(rig.conn)) == 2 and pending_count(rig.conn) == 1
    assert rig.conn.execute("SELECT message_id FROM pending").fetchone()[0] == 502
    assert [role for role, _ in memory_rows(rig.conn)] == ["user", "action", "action", "action", "assistant"]


def test_send_gives_up_after_three_attempts_with_growing_backoff():
    class Bot:
        calls = 0

        async def send_message(self, *a, **kw):
            Bot.calls += 1
            raise connect_error()
    naps = []

    async def nap(seconds):
        naps.append(seconds)
    with pytest.raises(NetworkError):
        run(TelegramOutbox(Bot(), sleep=nap).send(GROUP, "hi"))
    assert Bot.calls == 3 and naps == [1.0, 2.0]


def test_timed_out_send_is_not_retried_but_timed_out_edit_and_tap_are():
    class Bot:
        def __init__(self):
            self.n = {"send": 0, "edit": 0, "tap": 0}

        async def send_message(self, *a, **kw):
            self.n["send"] += 1
            raise TimedOut()

        async def edit_message_text(self, *a, **kw):
            self.n["edit"] += 1
            if self.n["edit"] == 1:
                raise TimedOut()

        async def answer_callback_query(self, *a, **kw):
            self.n["tap"] += 1
            if self.n["tap"] == 1:
                raise TimedOut()

    async def nap(seconds):
        pass
    bot = Bot()
    outbox = TelegramOutbox(bot, sleep=nap)
    with pytest.raises(TimedOut):
        run(outbox.send(GROUP, "hi"))
    run(outbox.edit(GROUP, 5, "x"))
    run(outbox.answer_tap("t"))
    assert bot.n == {"send": 1, "edit": 2, "tap": 2}


def test_failure_with_no_changes_says_so_and_the_transcript_shows_the_attempt(rig):
    async def go():
        async with AgentFlow(rig, [use(call("list_queue")), say("Queue is empty.")]) as f:
            f.outbox.fail_sends(connect_error(), when=lambda s: s.text == "Queue is empty.")
            await f.ask("what's queued")
            return f
    f = run(go())
    assert f.texts() == ["Something went wrong (connection problem?), nothing was changed. Try again."]
    assert [r for r, _ in memory_rows(rig.conn)] == ["user", "assistant"]
    assert memory_rows(rig.conn)[1][1] == "(failed: connection problem)"


def test_if_the_failure_reply_cannot_be_sent_either_nothing_else_is_raised(rig, caplog):
    async def go():
        async with AgentFlow(rig, two_adds_then_question()) as f:
            f.outbox.fail_sends(connect_error())  # every send fails
            await f.ask("diet coke, salt and some milk")
            return f
    f = run(go())
    assert f.texts() == [] and len(lines(rig.conn)) == 2
    assert "handler failed" not in caplog.text  # the lane's last-resort path was not reached
    assert caplog.text.count("could not send the failure reply") == 1


@pytest.mark.parametrize("exc, expected", [
    (connect_error(), True),
    (TimedOut(), False),
    (NetworkError("httpx.ReadError: peer closed"), False),
    (NetworkError("httpx.ConnectError: no chain"), True),   # message fallback
    (NetworkError("ConnectError: no chain"), True),
    (Exception("ConnectError"), False),
    (httpx.ConnectError("raw httpx is not a telegram error"), False),
])
def test_is_connect_error(exc, expected):
    assert is_connect_error(exc) is expected


@pytest.mark.parametrize("scope", ["ask", "all"])
def test_dropped_scope_question_or_bill_card_leaves_nothing_unanswerable(tmp_path, scope):
    from queuify.store import db
    from test_orders import Env, PRIYA, NAMES, seed_queue
    c = db.connect(tmp_path / "o.db")
    db.migrate(c)
    for uid, name in NAMES.items():
        c.execute("INSERT INTO members (user_id, display_name, joined_at) VALUES (?, ?, 'x')", (uid, name))
    seed_queue(c)
    env = Env(c, ScriptedLLM([use(call("propose_order", scope=scope))]))
    env.outbox.fail_sends(connect_error(), when=is_question)
    run(env.say("@QueuifyBot order now"))
    assert pending_count(c) == 0
    assert env.statuses() == {"ask": [], "all": ["cancelled"]}[scope]  # a card that never posted is cancelled
    assert "connection problem" in env.outbox.sent[-1].text
