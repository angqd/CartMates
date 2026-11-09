"""Free-delivery nudge, /track, order outcomes in chat memory and item labels (DESIGN 5.3).
Fake Swiggy only; the real handlers, domain code and SQL run."""
import json
from decimal import Decimal

import pytest

from cartmates.agent.llm import LLMTurn, ScriptedLLM, ToolCall
from cartmates.domain import orders, pending, queue
from cartmates.store import db
from cartmates.swiggy.errors import LoggedOut, SwiggyToolError, SwiggyUnavailable
from tests.test_orders import (ADDRESS, PRIYA, ROHAN, Env, FakeSwiggy, add, conn, run)  # noqa: F401

HEADER = "🎉 Free delivery reached"


class NudgeSwiggy(FakeSwiggy):
    def __init__(self):
        super().__init__()
        self.delivery_fee = "FREE"
        self.status: dict | Exception = {}
        self.refuse = False

    async def update_cart(self, args):
        data = await super().update_cart(args)
        for item in data["billBreakdown"]["lineItems"]:
            if "Delivery" in item["label"]:
                item["value"] = self.delivery_fee
        return data

    async def get_delivery_status(self, args):
        if isinstance(self.status, Exception):
            raise self.status
        return self.status

    async def checkout(self, args):
        if self.refuse:
            raise SwiggyToolError("Failed to create order: not eligible")
        return await super().checkout(args)


def update_qty(number, qty, text="done"):
    call = ToolCall("c1", "update_queue_item", json.dumps({"number": number, "qty": qty}))
    return [LLMTurn(tool_calls=[call]), LLMTurn(text=text)]


def make(conn, turns=()):
    """160 in the queue (Eggs 60, Milk 100): below the 199 minimum."""
    env = Env(conn, ScriptedLLM(list(turns)) if turns else None)
    env.swiggy = NudgeSwiggy()
    env.h.swiggy = env.swiggy
    env.h.executor.swiggy = env.swiggy
    add(conn, "E1", "E1", 1, [PRIYA])
    add(conn, "M1", "M1", 1, [PRIYA])
    return env


def cards(env):
    return [s for s in env.outbox.sent if HEADER in s.text]


def base(conn):
    row = conn.execute("SELECT value FROM kv WHERE key='nudge_base'").fetchone()
    return int(row[0]) if row else None


# ---- nudge ----

def test_crossing_the_minimum_with_free_delivery_posts_a_card(conn):
    env = make(conn, update_qty(1, 2, "Eggs now 2."))
    run(env.say("make the eggs 2"))
    (card,) = cards(env)
    assert card.text.startswith("🎉 Free delivery reached (₹220). Order now?\nOrder #1: the whole queue")
    assert [b[1] for row in card.buttons for b in row] == ["o:1:u", "o:1:c", "o:1:x"]
    assert env.statuses() == ["proposed"] and base(conn) == 22000
    assert env.outbox.sent[0].text == "Eggs now 2."  # the reply goes out before the card
    assert env.swiggy.n("update_cart") == 1


def test_delivery_not_free_posts_nothing_and_writes_no_order(conn, caplog):
    env = make(conn, update_qty(1, 2))
    env.swiggy.delivery_fee = "₹25"
    with caplog.at_level("INFO"):
        run(env.say("make the eggs 2"))
    assert cards(env) == [] and env.statuses() == [] and env.swiggy.n("update_cart") == 1
    assert [r.getMessage() for r in caplog.records if "delivery is not free" in r.getMessage()] == [
        "free-delivery nudge: queue at ₹220 but delivery is not free (₹25)"]
    assert base(conn) == 22000


def test_no_second_nudge_without_growth_and_growth_after_cancel_nudges_again(conn):
    env = make(conn, update_qty(1, 2) + update_qty(1, 2) + update_qty(1, 3))
    run(env.say("a"))
    run(env.tap("o:1:x", message_id=1000))  # cancel
    run(env.say("b"))  # version changes, total stays 220
    assert len(cards(env)) == 1 and env.swiggy.n("update_cart") == 1
    run(env.say("c"))  # 280: grew
    assert len(cards(env)) == 2 and env.statuses() == ["cancelled", "proposed"]


def test_a_total_drop_lowers_the_base(conn):
    env = make(conn, update_qty(1, 3) + update_qty(1, 1) + update_qty(1, 2))
    run(env.say("a"))  # 280: nudge
    assert base(conn) == 28000
    run(env.tap("o:1:x", message_id=1000))
    run(env.say("b"))  # 160: below the minimum, base follows it down
    assert base(conn) == 16000 and len(cards(env)) == 1
    run(env.say("c"))  # 220 is above the lowered base: checked again, though below the old 280
    assert len(cards(env)) == 2


def test_skipped_with_an_open_proposal(conn):
    env = make(conn, update_qty(1, 2))
    run(env.say("/order all"))
    n = env.swiggy.n("update_cart")
    run(env.say("make the eggs 2"))
    assert cards(env) == [] and env.swiggy.n("update_cart") == n and env.statuses() == ["proposed"]


def test_skipped_with_an_unknown_order(conn):
    env = make(conn, update_qty(1, 2))
    conn.execute("INSERT INTO orders (status, scope, proposed_by, proposed_at, queue_version) "
                 "VALUES ('unknown', 'all', 1, 'x', 0)")
    run(env.say("make the eggs 2"))
    assert cards(env) == [] and env.swiggy.n("update_cart") == 0


def test_skipped_when_logged_out_and_no_login_is_started(conn):
    env = make(conn, update_qty(1, 2, "ok"))
    env.swiggy.cart_error = LoggedOut("rejected")
    run(env.say("make the eggs 2"))
    assert [s.text for s in env.outbox.sent] == ["ok"]  # no login link, no parking message
    assert conn.execute("SELECT count(*) FROM pending").fetchone()[0] == 0 and env.statuses() == []


def test_skipped_when_the_bot_knows_it_is_logged_out(conn):
    env = make(conn, update_qty(1, 2))

    class Auth:
        def logged_in(self):
            return False
    env.h.auth = Auth()
    run(env.say("make the eggs 2"))
    assert env.swiggy.n("update_cart") == 0 and cards(env) == []


def test_skipped_without_an_address(conn):
    env = make(conn, update_qty(1, 2))
    env.h.address_id = None
    run(env.say("make the eggs 2"))
    assert env.swiggy.n("update_cart") == 0 and cards(env) == []


@pytest.mark.parametrize("error", [SwiggyUnavailable("down"), SwiggyToolError("cart broke")])
def test_a_nudge_error_does_not_break_the_turn(conn, error):
    env = make(conn, update_qty(1, 2, "Eggs now 2."))
    env.swiggy.cart_error = error
    run(env.say("make the eggs 2"))
    assert [s.text for s in env.outbox.sent] == ["Eggs now 2."]
    assert conn.execute("SELECT qty FROM queue_lines WHERE id=1").fetchone()[0] == 2 and env.statuses() == []


def test_remove_triggers_the_check(conn):
    call = ToolCall("c1", "remove_from_queue", json.dumps({"number": 4}))
    env = make(conn, [LLMTurn(tool_calls=[call]), LLMTurn(text="Removed.")])
    add(conn, "B1", "B1", 3, [ROHAN])  # 295 in the queue, but nothing has checked yet
    add(conn, "B1", "Extra", 1, [ROHAN])
    run(env.say("drop the extra"))  # a change event: 295 is above the minimum and the base
    assert env.outbox.sent[0].text == "Removed."
    assert len(cards(env)) == 1


def test_variant_tap_triggers_the_check(conn):
    env = make(conn)
    option = {"spin": "E1", "sku": "K-E1", "name": "Eggs", "size": "6 pcs", "price": 6000, "max_qty": 10}
    with db.transaction(conn):
        pid = pending.create_variant(conn, PRIYA, {"question": "Which?", "options": [option], "qty": 1,
                                                    "owners": [PRIYA], "original_text": "eggs"})
    run(env.tap(f"v:{pid}:0"))
    assert env.outbox.edited[0][2].startswith("✅ Eggs (6 pcs) ×1")
    assert len(cards(env)) == 1


def test_below_the_minimum_does_nothing(conn):
    env = make(conn, update_qty(1, 1))
    run(env.say("x"))
    assert env.swiggy.n("update_cart") == 0 and base(conn) in (None, 0)


# ---- /track ----

def placed(env):
    run(env.say("/order all"))
    run(env.tap("o:1:c", message_id=1000))
    return env.order(1)


def test_track_in_transit_shows_eta_and_ist_clock(conn):
    env = make(conn)
    add(conn, "B1", "B1", 1, [PRIYA])
    placed(env)
    env.swiggy.status = {"orderId": "ORD123", "deliveryBy": 1793889113299, "serverNow": 1793888393299,
                         "etaText": "12 mins", "cancelled": False, "delivered": False, "pollIntervalSec": 30}
    reply = run(env.say("/track"))
    assert reply == "Swiggy #ORD123: arriving in ~12 mins (by 20:01 IST)"
    tool, args = env.swiggy.calls[-1]
    assert tool == "get_delivery_status" and args == {"orderId": "ORD123", "addressId": ADDRESS}


def test_track_delivered_cancelled_and_given_id(conn):
    env = make(conn)
    add(conn, "B1", "B1", 1, [PRIYA])
    placed(env)
    env.swiggy.status = {"orderId": "ORD123", "deliveryBy": None, "serverNow": 1, "cancelled": False,
                         "delivered": True, "pollIntervalSec": 45}
    assert run(env.say("/track 1")) == "Swiggy #ORD123: Delivered ✅"
    env.swiggy.status = {**env.swiggy.status, "delivered": False, "cancelled": True}
    assert run(env.say("/track #1")) == "Swiggy #ORD123: Cancelled"


def test_track_order_that_was_not_placed_or_does_not_exist(conn):
    env = make(conn)
    assert run(env.say("/track")) == "No orders yet."
    run(env.say("/order all"))
    assert run(env.say("/track 1")) == "Order #1 wasn't placed (status: proposed), so there's nothing to track."
    assert run(env.say("/track")) == "No order has been placed yet, so there's nothing to track."
    assert run(env.say("/track 9")) == "There is no order #9."
    assert env.swiggy.n("get_delivery_status") == 0


def test_track_swiggy_errors_give_the_usual_message(conn):
    env = make(conn)
    add(conn, "B1", "B1", 1, [PRIYA])
    placed(env)
    for err in (SwiggyUnavailable("down"), SwiggyToolError("bad")):
        env.swiggy.status = err
        assert run(env.say("/track")) == "Swiggy isn't reachable right now, try again in a bit."


# ---- order outcomes in chat memory ----

def actions(conn):
    return [r[0] for r in conn.execute("SELECT text FROM chat_memory WHERE role='action' ORDER BY id")]


def test_memory_lines_for_placed_cancelled_failed_unknown_and_resolved(conn):
    env = make(conn)
    add(conn, "B1", "B1", 1, [PRIYA])
    run(env.say("/order all"))
    run(env.tap("o:1:x", message_id=1000))
    run(env.say("/order all"))
    run(env.tap("o:2:c", message_id=1001))
    add(conn, "B1", "B1", 1, [PRIYA])
    run(env.say("/order all"))
    env.swiggy.refuse = True
    run(env.tap("o:3:c", message_id=1002))
    env.swiggy.refuse = False
    run(env.say("/order all"))
    env.swiggy.checkout_mode = "unavailable"
    run(env.tap("o:4:c", message_id=1003))
    run(env.say("/resolve 4 failed"))
    assert env.statuses() == ["cancelled", "placed", "failed", "failed"]
    lines = actions(conn)
    assert lines[0] == "order #1 cancelled by Priya"
    assert lines[1] == "order #2 placed by Priya: Swiggy #ORD123, ₹217.60 COD"
    assert lines[2].startswith("order #3 failed: Not ordered. Swiggy said:")
    assert any(l.startswith("order #4 unknown") for l in lines)
    assert lines[-1] == "order #4 resolved as failed by Priya"


def test_memory_line_for_resolved_as_placed(conn):
    env = make(conn)
    add(conn, "B1", "B1", 1, [PRIYA])
    run(env.say("/order all"))
    env.swiggy.checkout_mode = "unavailable"
    run(env.tap("o:1:c", message_id=1000))
    run(env.say("/resolve 1 placed"))
    lines = actions(conn)
    assert any(l.startswith("order #1 unknown") for l in lines)
    assert lines[-2].startswith("order #1 placed by Priya: Swiggy #?") and lines[-1] == "order #1 resolved as placed by Priya"


# ---- labels ----

def test_item_label_format():
    assert queue.item_label("Diet Coke", "330 ml x 2", 1) == "Diet Coke (330 ml x 2) ×1"
    assert queue.item_label("Eggs", "", 3) == "Eggs ×3" and queue.item_label("Eggs", None, 3) == "Eggs ×3"


def test_labels_match_across_list_snapshot_and_card(conn):
    env = make(conn)
    from datetime import datetime, timezone
    from cartmates.agent import memory
    assert "#1 E1 (1 pc) ×1 - Priya" in run(env.say("/list"))
    assert "#1 E1 (1 pc) ×1 for Priya" in memory.build_snapshot(conn, PRIYA, datetime.now(timezone.utc))
    run(env.say("/order all"))
    assert "- E1 (1 pc) ×1, Priya: ₹60" in env.outbox.sent[-1].text
