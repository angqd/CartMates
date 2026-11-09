"""The model behind the agent loop, behind a tiny interface so tests can script it."""
import copy
import logging
from dataclasses import dataclass, field
from typing import Protocol

import openai

from cartmates import tracing

log = logging.getLogger(__name__)

TIMEOUT_SECONDS = 45


class LLMUnavailable(Exception):
    """The model could not be reached or refused the request."""


@dataclass(frozen=True)
class ToolCall:
    id: str
    name: str
    arguments: str  # a JSON string, exactly as the model wrote it; never trusted


@dataclass(frozen=True)
class LLMTurn:
    """Either final text, or tool calls to run."""
    text: str | None = None
    tool_calls: list[ToolCall] = field(default_factory=list)
    input_tokens: int = 0
    output_tokens: int = 0


class LLM(Protocol):
    async def respond(self, instructions: str, input_items: list[dict], tools: list[dict]) -> LLMTurn: ...


def _messages(instructions: str, items: list[dict]) -> list[dict]:
    """The Responses API input as chat-style messages, for the trace."""
    out = [{"role": "system", "content": instructions}]
    for item in items:
        match item.get("type"):
            case "function_call":
                out.append({"role": "assistant", "tool": f"{item['name']}({item['arguments']})"})
            case "function_call_output":
                out.append({"role": "tool", "content": item["output"]})
            case _:
                out.append({"role": item.get("role", "user"), "content": item.get("content", "")})
    return out


async def traced_respond(llm: LLM, instructions: str, items: list[dict], tools: list[dict]) -> LLMTurn:
    """One model round inside an "llm.chat" span: model, messages, answer, token counts."""
    model = getattr(llm, "model", type(llm).__name__)
    with tracing.span("llm.chat", tracing.LLM, {"llm.model_name": model}) as sp:
        if tracing.content_on():
            for i, m in enumerate(_messages(instructions, items)):
                sp.set_attribute(f"llm.input_messages.{i}.message.role", m["role"])
                tracing.set_text(sp, f"llm.input_messages.{i}.message.content", m.get("content") or m.get("tool"))
        turn = await llm.respond(instructions, items, tools)
        sp.set_attributes({"llm.token_count.prompt": turn.input_tokens,
                           "llm.token_count.completion": turn.output_tokens,
                           "llm.token_count.total": turn.input_tokens + turn.output_tokens})
        if tracing.content_on():
            sp.set_attribute("llm.output_messages.0.message.role", "assistant")
            tracing.set_text(sp, "llm.output_messages.0.message.content", turn.text)
            for i, c in enumerate(turn.tool_calls):
                prefix = f"llm.output_messages.0.message.tool_calls.{i}.tool_call.function"
                sp.set_attribute(f"{prefix}.name", c.name)
                tracing.set_text(sp, f"{prefix}.arguments", c.arguments)
            tracing.set_text(sp, "output.value", turn.text or tracing.dump([c.name for c in turn.tool_calls]))
        return turn


class OpenAILLM:
    """Responses API, stateless: we resend the whole context every round (store=False)."""

    def __init__(self, api_key: str, model: str, reasoning_effort: str | None = None):
        # One retry only: with the 45 s timeout the SDK's default of two would hold the lane for minutes.
        self._client = openai.AsyncOpenAI(api_key=api_key, timeout=TIMEOUT_SECONDS, max_retries=1)
        self._model = model
        self.model = model
        self._effort = reasoning_effort

    async def respond(self, instructions: str, input_items: list[dict], tools: list[dict]) -> LLMTurn:
        kwargs = {}
        if self._effort:  # non-reasoning models reject the parameter, so only send it when asked
            kwargs["reasoning"] = {"effort": self._effort}
        try:
            response = await self._client.responses.create(
                model=self._model, instructions=instructions, input=input_items,
                tools=tools, store=False, **kwargs)
        except openai.OpenAIError as e:
            log.warning("openai call failed: %s", type(e).__name__)
            raise LLMUnavailable(type(e).__name__) from e
        calls = [ToolCall(item.call_id, item.name, item.arguments)
                 for item in response.output if item.type == "function_call"]
        usage = getattr(response, "usage", None)
        return LLMTurn(text=response.output_text or None, tool_calls=calls,
                       input_tokens=getattr(usage, "input_tokens", 0) or 0,
                       output_tokens=getattr(usage, "output_tokens", 0) or 0)


class ScriptedLLM:
    """Returns the given turns in order (an Exception in the list is raised), and records
    what each round was sent, as (instructions, input_items, tools)."""

    def __init__(self, turns: list[LLMTurn | Exception]):
        self._turns = list(turns)
        self.sent: list[tuple[str, list[dict], list[dict]]] = []

    async def respond(self, instructions, input_items, tools) -> LLMTurn:
        self.sent.append((instructions, copy.deepcopy(input_items), tools))
        turn = self._turns.pop(0)  # IndexError here means the loop asked for more rounds than scripted
        if isinstance(turn, Exception):
            raise turn
        return turn
