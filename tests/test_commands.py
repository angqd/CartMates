"""B8: chat is the main interface. The menu, /help, the removed commands, bare /order and the read-only tools."""
import json

from queuify.agent.llm import LLMTurn, ScriptedLLM, ToolCall
from queuify.handlers import menu_commands
from tests.test_nudge import NudgeSwiggy, placed
from tests.test_orders import ADDRESS, PRIYA, ROHAN, Env, add, conn, run, seed_queue  # noqa: F401


def tool_turn(name, **args):
    return [LLMTurn(tool_calls=[ToolCall("c1", name, json.dumps(args))]), LLMTurn(text="ok")]


def snapshot(conn):
    return [tuple(r) for t in ("queue_lines", "orders", "pending") for r in conn.execute(f"SELECT * FROM {t}")] + \
        [conn.execute("SELECT value FROM kv WHERE key='queue_version'").fetchone()[0]]


# ---- menu and removed commands ----

def test_menu_has_exactly_the_five_commands_in_order():
    assert [name for name, _ in menu_commands()] == ["help", "list", "order", "address", "status"]
    assert all(desc and not desc.startswith(("[", "<")) for _, desc in menu_commands())


def test_removed_commands_nudge_and_change_nothing(conn):
    seed_queue(conn)
    env = Env(conn, ScriptedLLM([]))
    before = snapshot(conn)
    for text in ("/add 2 milk", "/remove 1", "/search coffee"):
        reply = run(env.say(text))
        assert reply.startswith("Just @mention me") and "plain words" in reply
    assert snapshot(conn) == before and env.swiggy.calls == []


# ---- /order ----

def test_bare_order_with_two_owners_asks_the_scope(conn):
    seed_queue(conn)
    env = Env(conn)
    run(env.say("/order"))
    assert env.outbox.sent[0].text == "Order just your items, or the whole queue?"
    assert env.statuses() == []  # nothing proposed yet


def test_bare_order_with_one_owner_proposes_everything(conn):
    add(conn, "E1", "E1", 1, [PRIYA])
    add(conn, "M1", "M1", 1, [PRIYA])
    env = Env(conn)
    run(env.say("/order"))
    assert env.statuses() == ["proposed"] and env.order(1)["scope"] == "all"
    assert env.outbox.sent[0].buttons[-1] == [("Cancel", "o:1:x")]  # the bill card, not the scope question


def test_order_mine_and_all_are_explicit(conn):
    seed_queue(conn)
    env = Env(conn)
    run(env.say("/order mine", user=ROHAN))
    run(env.say("/order all"))
    assert [env.order(i)["scope"] for i in (1, 2)] == ["mine", "all"]
    assert run(env.say("/order sideways")).startswith("Usage")


# ---- read-only tools ----

def test_track_order_tool_returns_the_eta_and_changes_nothing(conn):
    env = Env(conn)
    env.swiggy = NudgeSwiggy()
    env.h.swiggy = env.swiggy
    env.h.executor.swiggy = env.swiggy
    add(conn, "E1", "E1", 1, [PRIYA])
    add(conn, "M1", "M1", 1, [PRIYA])
    add(conn, "B1", "B1", 1, [PRIYA])
    placed(env)
    env.swiggy.status = {"orderId": "ORD123", "deliveryBy": 1793889113299, "serverNow": 1793888393299,
                         "etaText": "12 mins", "cancelled": False, "delivered": False}
    env.h.llm = ScriptedLLM(tool_turn("track_order"))
    before = snapshot(conn)
    run(env.say("where is my order?"))
    assert env.outbox.sent[-1].text == "ok"
    out = [i["output"] for i in env.h.llm.sent[1][1] if i.get("type") == "function_call_output"]
    assert out == ["Swiggy #ORD123: arriving in ~12 mins (by 20:01 IST)"]
    assert snapshot(conn) == before


def test_track_order_with_nothing_placed(conn):
    env = Env(conn, ScriptedLLM(tool_turn("track_order", order_id=7)))
    run(env.say("where is order 7?"))
    out = [i["output"] for i in env.h.llm.sent[1][1] if i.get("type") == "function_call_output"]
    assert out == ["There is no order #7."] and env.swiggy.n("get_delivery_status") == 0


def test_order_history_tool_lists_recent_orders_capped_at_ten(conn):
    seed_queue(conn)
    env = Env(conn, ScriptedLLM(tool_turn("order_history", count=1)))
    run(env.say("/order all"))
    run(env.say("/order all"))  # a second proposal supersedes the first
    before = snapshot(conn)
    run(env.say("what did we order?"))
    out = [i["output"] for i in env.h.llm.sent[1][1] if i.get("type") == "function_call_output"]
    assert out[0].startswith("Recent orders:") and len(out[0].splitlines()) == 2 and "#2" in out[0]
    assert snapshot(conn) == before
