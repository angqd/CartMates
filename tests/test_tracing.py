"""Spans, with an in-memory exporter standing in for Phoenix. No collector, no network."""
import asyncio
import json

import pytest
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from cartmates import observe, tracing
from cartmates.config import load_config
from cartmates.domain import orders, queue
from cartmates.telegram.outbox import TelegramOutbox
from test_agent import AgentFlow, call, rig, say, use  # noqa: F401  (rig is a fixture: logged in)
from test_swiggy import Flow, Rig, command, conn, pending_login  # noqa: F401
from test_telegram import GROUP, make_update
from tests.test_orders import conn as orders_conn  # noqa: F401  (members seeded)


@pytest.fixture
def spans():
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    tracing.use_provider(provider)
    tracing.configure(True, 8000)
    yield exporter
    tracing.use_provider(None)
    tracing.configure(True, 8000)


def by_name(exporter, prefix):
    return [s for s in exporter.get_finished_spans() if s.name.startswith(prefix)]


def parent_name(exporter, span):
    parents = {s.context.span_id: s.name for s in exporter.get_finished_spans()}
    return parents.get(span.parent.span_id) if span.parent else None


def all_values(exporter):
    out = []
    for s in exporter.get_finished_spans():
        out += [str(v) for v in s.attributes.values()]
        for e in s.events:
            out += [str(v) for v in e.attributes.values()]
    return out


def run_agent(rig, turns, text="2 oat drink"):
    async def go():
        async with AgentFlow(rig, turns) as f:
            await f.ask(text)
            return f
    return asyncio.run(go())


OAT = [use(call("search_catalog", query="oat drink")),
       use(call("add_to_queue", ref="p2v1", qty=2, owners=["me"], original_text="2 oat drink")),
       say("Added 2 Meadow Oat Drink.")]


def test_event_agent_llm_tool_swiggy_nest(rig, spans):
    run_agent(rig, OAT)
    (root,) = by_name(spans, "event:")
    assert root.name == "event:Message" and root.parent is None
    assert root.attributes["openinference.span.kind"] == "CHAIN"
    assert root.attributes["session.id"] == str(GROUP) and root.attributes["user.id"] == "1"
    assert root.attributes["cartmates.turn_id"]
    (turn,) = by_name(spans, "agent.turn")
    assert parent_name(spans, turn) == "event:Message"
    assert turn.attributes["openinference.span.kind"] == "AGENT"
    assert turn.attributes["cartmates.rounds"] == 3 and turn.attributes["cartmates.outcome"] == "replied"
    assert turn.attributes["cartmates.tools"] == "search_catalog,add_to_queue"
    llms = by_name(spans, "llm.chat")
    assert len(llms) == 3 and {parent_name(spans, s) for s in llms} == {"agent.turn"}
    first = llms[0].attributes
    assert first["openinference.span.kind"] == "LLM"
    assert first["llm.input_messages.0.message.role"] == "system"
    assert "2 oat drink" in first["llm.input_messages.2.message.content"]
    assert first["llm.output_messages.0.message.tool_calls.0.tool_call.function.name"] == "search_catalog"
    assert llms[2].attributes["llm.output_messages.0.message.content"] == "Added 2 Meadow Oat Drink."
    tools = by_name(spans, "tool:")
    assert [t.name for t in tools] == ["tool:search_catalog", "tool:add_to_queue"]
    assert tools[0].attributes["openinference.span.kind"] == "TOOL"
    assert json.loads(tools[0].attributes["tool.parameters"]) == {"query": "oat drink"}
    swiggy = by_name(spans, "swiggy:")
    assert swiggy and all(parent_name(spans, s) == "tool:search_catalog" for s in swiggy)
    assert swiggy[0].attributes["swiggy.status"] == "ok" and swiggy[0].attributes["swiggy.result_chars"] > 0
    ids = {s.context.trace_id for s in spans.get_finished_spans() if not s.name.startswith("telegram.")}
    assert len(ids) == 1


def test_content_off_records_no_text(rig, spans):
    tracing.configure(False, 8000)
    run_agent(rig, OAT)
    keys = {k for s in spans.get_finished_spans() for k in s.attributes}
    assert not [k for k in keys if k.startswith(("llm.input_messages", "llm.output_messages"))]
    assert not keys & {"input.value", "output.value", "tool.parameters"}
    assert "oat" not in " ".join(all_values(spans)).lower().replace("meadow", "")  # no prompt or reply text
    (turn,) = by_name(spans, "agent.turn")
    assert turn.attributes["llm.token_count.total"] == 0 and turn.attributes["cartmates.rounds"] == 3


def test_truncation_marker():
    tracing.configure(True, 10)
    try:
        assert tracing.cap("x" * 25) == "x" * 10 + "…[truncated 15 chars]"
        assert tracing.cap("short") == "short"
    finally:
        tracing.configure(True, 8000)


def test_long_text_is_capped_on_the_span(rig, spans):
    tracing.configure(True, 20)
    run_agent(rig, OAT, text="oat " * 50)
    (root,) = by_name(spans, "event:")
    assert root.attributes["input.value"].endswith("chars]") and "truncated" in root.attributes["input.value"]


def test_login_paste_never_reaches_a_span(tmp_path, conn, spans):
    rig = Rig(tmp_path, conn)  # logged out

    async def go():
        async with Flow(rig) as f:
            await f.say(command("/address", message_id=20))
            login = pending_login(rig.conn)
            paste = f"http://localhost:8765/callback?code=SECRETCODE&state={login['state']}"
            await f.say(make_update(paste, reply_to_bot=True, message_id=21, user_id=2, name="Rohan"))
            return login
    login = asyncio.run(go())
    values = " ".join(all_values(spans))
    assert "SECRETCODE" not in values and "/callback?" not in values
    assert login["verifier"] not in values
    redacted = [s for s in by_name(spans, "event:") if s.attributes.get("cartmates.redacted")]
    assert len(redacted) == 1 and "input.value" not in redacted[0].attributes


def test_paste_text_is_refused_by_set_text(spans):
    with tracing.span("x") as sp:
        tracing.set_text(sp, "k", "http://x/callback?code=abc")
    assert "k" not in spans.get_finished_spans()[0].attributes


def test_errors_are_recorded_on_the_root(spans):
    from cartmates.lane import Lane
    from cartmates.telegram.events import CleanupPending
    from cartmates.telegram.outbox import FakeOutbox

    async def boom(event):
        raise RuntimeError("bad")

    async def go():
        lane = Lane(boom, FakeOutbox())
        lane.start()
        await lane.submit(CleanupPending(GROUP))
        await asyncio.sleep(0.1)
        await lane.stop()
    asyncio.run(go())
    (root,) = spans.get_finished_spans()
    assert root.name == "event:CleanupPending" and root.status.status_code.name == "ERROR"
    assert root.events[0].name == "exception"


def test_events_rows_get_the_trace_id(rig, spans):
    run_agent(rig, OAT)
    (root,) = by_name(spans, "event:")
    expected = format(root.context.trace_id, "032x")
    ids = [r[0] for r in rig.conn.execute("SELECT trace_id FROM events")]
    assert ids and set(ids) == {expected}


def test_events_trace_id_is_null_without_a_span(conn):
    queue.log_event(conn, 1, "x", {})
    assert conn.execute("SELECT trace_id FROM events").fetchone()[0] is None
    assert observe.current_trace_id() is None


def test_resolve_shows_trace_ids(conn, spans):
    with tracing.span("t") as sp:
        queue.log_event(conn, 1, "order_status", {"order_id": 7})
        tid = format(sp.get_span_context().trace_id, "032x")
    assert orders.trace_ids(conn, 7) == [tid] and orders.trace_ids(conn, 8) == []


def test_log_filter_adds_trace_id(spans):
    import logging
    record = logging.LogRecord("x", logging.INFO, "f", 1, "m", None, None)
    observe.TurnIdFilter().filter(record)
    assert record.trace_id == "-"
    with tracing.span("t") as sp:
        observe.TurnIdFilter().filter(record)
        assert record.trace_id == format(sp.get_span_context().trace_id, "032x")


def test_telegram_send_span_hides_links_and_tokens(spans):
    class Bot:
        async def send_message(self, *a, **k):
            class R:
                message_id = 5
            return R()

    async def go():
        box = TelegramOutbox(Bot())
        await box.send(GROUP, "hello there")
        await box.send(GROUP, "open https://mcp.swiggy.com/auth/authorize?state=abc")
    asyncio.run(go())
    a, b = spans.get_finished_spans()
    assert a.name == "telegram.send" and a.attributes["output.value"] == "hello there"
    assert a.attributes["telegram.text_length"] == 11
    assert "output.value" not in b.attributes and b.attributes["telegram.text_length"] > 0


def test_setup_without_endpoint_installs_nothing(monkeypatch):
    from opentelemetry import trace
    for name in ("OTEL_EXPORTER_OTLP_ENDPOINT", "CARTMATES_TRACE_CONTENT", "CARTMATES_TRACE_MAX_CHARS"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr("cartmates.config.load_dotenv", lambda: None)
    before = trace.get_tracer_provider()
    cfg = load_config()
    assert cfg.otel_endpoint is None and cfg.trace_content is True and cfg.trace_max_chars == 8000
    assert tracing.setup(cfg) is False
    assert trace.get_tracer_provider() is before
    tracing.shutdown()


def test_config_switches(monkeypatch):
    monkeypatch.setattr("cartmates.config.load_dotenv", lambda: None)
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://phoenix:6006")
    monkeypatch.setenv("CARTMATES_TRACE_CONTENT", "0")
    monkeypatch.setenv("CARTMATES_TRACE_MAX_CHARS", "100")
    cfg = load_config()
    assert (cfg.otel_endpoint, cfg.trace_content, cfg.trace_max_chars) == ("http://phoenix:6006", False, 100)


def test_trace_url_suffix():
    assert tracing.trace_url("http://phoenix:6006") == "http://phoenix:6006/v1/traces"
    assert tracing.trace_url("http://phoenix:6006/") == "http://phoenix:6006/v1/traces"
    assert tracing.trace_url("http://h/v1/traces") == "http://h/v1/traces"


def test_order_confirm_span_has_state_events(orders_conn, spans):
    conn = orders_conn
    from tests.test_orders import ROHAN, run, seed_queue
    from tests.test_upi import PENDING, SUCCESS, UpiEnv
    seed_queue(conn)
    env = UpiEnv(conn)
    env.swiggy.statuses = [PENDING, SUCCESS]
    run(env.say("/order all"))
    run(env.tap("o:1:u", user=ROHAN, message_id=1000))
    (span,) = by_name(spans, "order.confirm")
    assert span.attributes["cartmates.order_id"] == 1 and span.attributes["cartmates.payment_method"] == "upi"
    names = [e.name for e in span.events]
    for step in ("claimed", "quoting", "checking_out", "pay_link_sent", "paid", "placed"):
        assert step in names
    assert names.index("claimed") < names.index("quoting") < names.index("checking_out") < names.index("placed")


def _run_lane(household_id):
    import logging
    from cartmates.lane import Lane
    from cartmates.telegram.events import CleanupPending
    from cartmates.telegram.outbox import FakeOutbox
    seen = []

    class Grab(logging.Handler):
        def emit(self, record):
            seen.append(record)
    h = Grab()
    h.addFilter(observe.TurnIdFilter())

    async def handler(event):
        logging.getLogger("t").warning("inside")

    async def go():
        lane = Lane(handler, FakeOutbox(), **({} if household_id is None else {"household_id": household_id}))
        lane.start()
        await lane.submit(CleanupPending(GROUP))
        await asyncio.sleep(0.1)
        await lane.stop()
    logging.getLogger("t").addHandler(h)
    try:
        asyncio.run(go())
    finally:
        logging.getLogger("t").removeHandler(h)
    return seen


def test_lane_with_household_tags_span_and_logs(spans):
    seen = _run_lane(7)
    (root,) = spans.get_finished_spans()
    assert root.attributes["session.id"] == "hh-7" and root.attributes["cartmates.household_id"] == 7
    assert root.attributes["cartmates.chat_id"] == GROUP
    assert [r.household for r in seen] == ["hh-7"]


def test_lane_without_household_uses_the_chat_id(spans):
    seen = _run_lane(None)
    (root,) = spans.get_finished_spans()
    assert root.attributes["session.id"] == str(GROUP)
    assert "cartmates.household_id" not in root.attributes
    assert [r.household for r in seen] == ["-"]


def test_outbox_migration_callback_gets_old_and_new_ids():
    from telegram.error import ChatMigrated

    class Bot:
        async def send_message(self, chat, *a, **k):
            if chat == GROUP:
                raise ChatMigrated(-1009)
            return type("R", (), {"message_id": 1})()
    got = []
    asyncio.run(TelegramOutbox(Bot(), lambda old, new: got.append((old, new))).send(GROUP, "hi"))
    assert got == [(GROUP, -1009)]
