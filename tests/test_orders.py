"""Ordering (M4): money parsing, quotes, proposals, the executor state machine, shares and history.
No network and no real Swiggy: FakeSwiggy plays update_cart / checkout / get_orders with switches,
and the real handlers, executor and SQL run against it."""
import asyncio
import json
import logging
import re
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from cartmates import observe
from cartmates.agent.llm import LLMTurn, ScriptedLLM, ToolCall
from cartmates.domain import orders, queue
from cartmates.handlers import Handlers
from cartmates.lane import Lane
from cartmates.store import db
from cartmates.swiggy import cart
from cartmates.swiggy.errors import LoggedOut, SwiggyToolError, SwiggyUnavailable
from cartmates.telegram.events import CallbackTap, Message
from cartmates.telegram.outbox import FakeOutbox

GROUP = -100123
ADDRESS = "addr1__XYZ"
PRIYA, ROHAN, MEERA = 1, 2, 3
NAMES = {PRIYA: "Priya", ROHAN: "Rohan", MEERA: "Meera"}
FEE = Decimal("12.60")


# ---- fake Swiggy ----

class FakeSwiggy:
    """Plays the cart, payment, checkout and orders tools. Switches are plain attributes."""

    def __init__(self):
        self.calls: list[tuple[str, dict]] = []
        self.prices = {"E1": 60, "M1": 100, "B1": 45}     # rupees per unit
        self.out_of_stock: set[str] = set()
        self.caps: dict[str, int] = {}                      # spin -> max quantity Swiggy will keep
        self.missing: set[str] = set()                      # spins Swiggy silently drops
        self.surcharge = Decimal(0)                         # rupees added to To Pay (a price rise)
        self.line_prices = False                            # discountedFinalPrice is a line total
        self.cod = True
        self.selected = ADDRESS.split("__")[0]
        self.checkout_mode = "confirmed"    # confirmed | unavailable | odd | tool_error | logged_out
        self.cart_error: Exception | None = None            # raised by update_cart
        self.orders: list[dict] = []                        # what get_orders returns
        self.orders_error: Exception | None = None
        self.resets = 0                                     # times the MCP session was dropped

    async def reset_session(self):
        self.resets += 1

    @asynccontextmanager
    async def turn(self):
        yield self

    def n(self, tool):
        return sum(1 for t, _ in self.calls if t == tool)

    async def call(self, tool, args, retry_rate_limit=True):
        self.calls.append((tool, args))
        if tool == "checkout":
            assert retry_rate_limit is False  # checkout must never be retried on a 429
        return await getattr(self, tool)(args)

    async def update_cart(self, args):
        if self.cart_error:
            raise self.cart_error
        items = [self._flyer("0MGKR9R1OI", "Flyer - Test Voucher")]
        total = Decimal(0)
        for it in args["items"]:
            spin, qty = it["spinId"], it["quantity"]
            if spin in self.missing:
                continue
            kept = min(qty, self.caps.get(spin, qty))
            unit = Decimal(self.prices[spin])
            total += unit * kept
            items.append({"spinId": spin, "skuId": it["skuId"], "itemName": spin, "quantity": kept,
                          "isInStockAndAvailable": spin not in self.out_of_stock,
                          "mrp": 0, "discountedFinalPrice": float(unit * kept if self.line_prices else unit)})
        to_pay = total + FEE + self.surcharge
        return {"selectedAddress": self.selected, "cartTotalAmount": f"₹{to_pay.normalize():f}", "items": items,
                "billBreakdown": {"lineItems": [
                    {"label": "Item Total", "value": f"₹{total:.2f}"},
                    {"label": "Handling Fee", "value": f"₹{FEE + self.surcharge:.2f}"},
                    {"label": "Delivery Partner Fee", "value": "FREE"}],
                    "toPay": {"label": "To Pay", "value": f"₹{to_pay:.2f}"}}}

    @staticmethod
    def _flyer(spin, name):
        return {"spinId": spin, "skuId": "FREEBIE_X", "itemName": name, "quantity": 1,
                "isInStockAndAvailable": True, "mrp": 0, "discountedFinalPrice": 0}

    async def get_payment_options(self, args):
        return {"cod": {"available": self.cod, "id": "COD"}, "swiggyMoney": {"available": True}}

    async def checkout(self, args):
        assert args == {"addressId": ADDRESS, "paymentMethod": "Cash"}
        match self.checkout_mode:
            case "unavailable":
                raise SwiggyUnavailable("ReadTimeout")
            case "tool_error":
                raise SwiggyToolError("payment failed")
            case "logged_out":
                raise LoggedOut("rejected")
            case "odd":
                return {"status": "PENDING"}
        return {"orderId": "ORD123", "status": "CONFIRMED", "paymentMethod": "Cash", "cartTotal": 277,
                "addressId": ADDRESS.split("__")[0]}

    async def get_orders(self, args):
        assert args == {"count": 5}
        if self.orders_error:
            raise self.orders_error
        return {"orders": self.orders, "hasMore": False}

    def add_recent(self, order_id="ORD999", names=("E1", "M1", "B1"), address="addr1", age=timedelta(0),
                   status="CONFIRMED", payment_status="SUCCESS"):
        created = datetime.now(timezone.utc) - age
        self.orders.append({"orderId": order_id, "createdAt": created.isoformat().replace("+00:00", "Z"),
                            "deliveryAddress": {"id": address}, "items": [{"name": n, "quantity": 1} for n in names],
                            "status": status, "paymentStatus": payment_status})


# ---- setup ----

@pytest.fixture
def conn(tmp_path):
    c = db.connect(tmp_path / "t.db")
    db.migrate(c)
    for uid, name in NAMES.items():
        c.execute("INSERT INTO members (user_id, display_name, joined_at) VALUES (?, ?, 'x')", (uid, name))
    return c


def add(conn, spin, name, qty, owners, added_by=None):
    price = FakeSwiggy().prices[spin] * 100
    with db.transaction(conn):
        return queue.add_line(conn, {"spin": spin, "sku": f"K-{spin}", "name": name, "size": "1 pc",
                                     "price": price, "max_qty": 10}, qty, owners, "x", added_by or owners[0])[0]


def seed_queue(conn):
    """Eggs x2 (Priya+Rohan) 120, Milk x1 (all three) 100, Bread x1 (Meera) 45."""
    return add(conn, "E1", "E1", 2, [PRIYA, ROHAN]), add(conn, "M1", "M1", 1, [PRIYA, ROHAN, MEERA]), \
        add(conn, "B1", "B1", 1, [MEERA])


class Env:
    def __init__(self, conn, llm=None):
        self.conn = conn
        self.swiggy = FakeSwiggy()
        self.outbox = FakeOutbox()
        self.h = Handlers(conn, self.outbox, GROUP, None, self.swiggy, ADDRESS, llm, 199, price_tolerance=0.05)
        self.mid = 100

    async def say(self, text, user=PRIYA):
        self.mid += 1
        command, _, args = text.partition(" ") if text.startswith("/") else (None, "", "")
        await self.h.handle(Message(GROUP, self.mid, user, NAMES[user], text, bool(command),
                                    command[1:] if command else None, args.strip()))
        return self.outbox.sent[-1].text if self.outbox.sent else None

    async def tap(self, data, user=PRIYA, message_id=77):
        await self.h.handle(CallbackTap(GROUP, message_id, user, NAMES[user], data, tap_id="tap"))

    def order(self, order_id):
        return self.conn.execute("SELECT * FROM orders WHERE id=?", (order_id,)).fetchone()

    def statuses(self):
        return [r[0] for r in self.conn.execute("SELECT status FROM orders ORDER BY id")]

    def line_statuses(self):
        return [r[0] for r in self.conn.execute("SELECT status FROM queue_lines ORDER BY id")]

    def shares(self, order_id):
        return {r[0]: (r[1], r[2]) for r in self.conn.execute(
            "SELECT user_id, amount_paise, owed_to FROM order_shares WHERE order_id=?", (order_id,))}


@pytest.fixture
def env(conn):
    seed_queue(conn)
    return Env(conn)


def run(coro):
    return asyncio.run(coro)


# ---- money and quotes ----

@pytest.mark.parametrize("value, paise", [
    ("₹327", 32700), ("₹12.60", 1260), ("FREE", 0), ("free", 0), ("", 0), (None, 0), (314, 31400),
    (12.6, 1260), (116.1, 11610), ("₹1,299.50", 129950), (Decimal("0.07"), 7)])
def test_parse_paise(value, paise):
    assert cart.parse_paise(value) == paise


def test_parse_paise_rejects_garbage():
    with pytest.raises(cart.CartError):
        cart.parse_paise("soon")


def test_quote_merges_lines_per_spin_and_ignores_flyers():
    sw = FakeSwiggy()
    q = run(cart.quote(sw, ADDRESS, [("E1", "K1", 2), ("M1", "K2", 1), ("E1", "K1", 1)]))
    (tool, args), = sw.calls
    assert tool == "update_cart"
    assert args == {"selectedAddressId": ADDRESS, "items": [
        {"spinId": "E1", "skuId": "K1", "quantity": 3}, {"spinId": "M1", "skuId": "K2", "quantity": 1}]}
    assert set(q.spins) == {"E1", "M1"}  # the Rs 0 flyer Swiggy added is not ours
    assert q.spins["E1"].unit_paise == 6000 and q.spins["E1"].in_cart == 3
    assert q.item_total_paise == 28000 and q.to_pay_paise == 29260 and q.unavailable == []
    assert q.bill_lines == [("Item Total", "₹280.00"), ("Handling Fee", "₹12.60"), ("Delivery Partner Fee", "FREE")]


def test_quote_detects_line_prices_versus_unit_prices():
    for line_mode in (False, True):
        sw = FakeSwiggy()
        sw.line_prices = line_mode
        q = run(cart.quote(sw, ADDRESS, [("E1", "K1", 3), ("B1", "K3", 2)]))
        assert q.spins["E1"].unit_paise == 6000 and q.spins["E1"].line_paise == 18000
        assert q.spins["B1"].unit_paise == 4500
        assert q.price_basis == ("line" if line_mode else "unit")


def test_quote_flags_out_of_stock_capped_and_missing_spins():
    sw = FakeSwiggy()
    sw.out_of_stock, sw.caps, sw.missing = {"E1"}, {"M1": 1}, {"B1"}
    q = run(cart.quote(sw, ADDRESS, [("E1", "K1", 1), ("M1", "K2", 3), ("B1", "K3", 1)]))
    assert sorted(q.unavailable) == ["B1", "E1", "M1"]
    assert q.spins["M1"].in_cart == 1 and q.spins["M1"].requested == 3


def test_quote_aborts_on_a_different_address():
    sw = FakeSwiggy()
    sw.selected = "someoneelse"
    with pytest.raises(cart.CartError, match="different address"):
        run(cart.quote(sw, ADDRESS, [("E1", "K1", 1)]))


def test_cod_and_recent_orders_parsing():
    sw = FakeSwiggy()
    sw.cod = False
    assert run(cart.cod_available(sw)) is False
    sw.add_recent("O1")
    sw.orders.append({"orderId": "bad", "items": []})  # unreadable: skipped
    (o,) = run(cart.recent_orders(sw))
    assert (o.order_id, o.address_id, o.item_names) == ("O1", "addr1", ["E1", "M1", "B1"])
    assert o.created_at.tzinfo is not None


def test_split_equally_gives_leftover_paise_to_lowest_ids():
    assert orders.split_equally(10000, [3, 1, 2]) == {1: 3334, 2: 3333, 3: 3333}
    assert orders.split_equally(10001, [3, 1, 2]) == {1: 3334, 2: 3334, 3: 3333}
    assert orders.split_equally(500, [7]) == {7: 500}


# ---- proposals ----

def test_order_all_posts_a_card_and_stores_the_proposal(env):
    run(env.say("/order all"))
    (card,) = env.outbox.sent
    assert env.swiggy.n("update_cart") == 1 and env.swiggy.n("get_payment_options") == 1
    assert card.buttons == [[("Pay by UPI ₹277.60", "o:1:u"), ("Cash ₹277.60", "o:1:c")], [("Cancel", "o:1:x")]]
    for part in ("Order #1: the whole queue", "E1 (1 pc) ×2, Priya, Rohan: ₹120", "Item Total: ₹265.00",
                 "Delivery Partner Fee: FREE", "To Pay: ₹277.60", "cash on delivery"):
        assert part in card.text
    o = env.order(1)
    assert (o["status"], o["scope"], o["proposed_by"], o["card_message_id"]) == ("proposed", "all", PRIYA, 1000)
    bill = json.loads(o["bill_json"])
    assert bill["to_pay"] == 27760 and bill["unit_prices"]["E1"] == 6000
    rows = env.conn.execute("SELECT line_id, qty_at_proposal, price_at_proposal_paise "
                            "FROM order_proposal_lines ORDER BY line_id").fetchall()
    assert [tuple(r) for r in rows] == [(1, 2, 6000), (2, 1, 10000), (3, 1, 4500)]
    assert env.line_statuses() == ["queued"] * 3  # a proposal never moves lines


def test_order_mine_only_covers_the_askers_lines(env):
    run(env.say("/order mine", user=MEERA))
    assert "Meera's items" in env.outbox.sent[0].text
    ids = [r[0] for r in env.conn.execute("SELECT line_id FROM order_proposal_lines ORDER BY line_id")]
    assert ids == [2, 3]  # milk (shared) and bread; not the eggs
    assert env.order(1)["scope"] == "mine"


def test_order_refusals(env):
    assert run(env.say("/order sideways")).startswith("Usage")
    env.conn.execute("UPDATE queue_lines SET status='removed' WHERE id IN (1, 2)")
    assert run(env.say("/order mine")) == "You have nothing in the queue to order."
    env.conn.execute("UPDATE queue_lines SET status='removed'")
    assert run(env.say("/order all")) == "The queue is empty."
    env.conn.execute("UPDATE queue_lines SET status='queued'")
    env.conn.execute("INSERT INTO orders (status, scope, proposed_by, proposed_at, queue_version, claimed_at) "
                     "VALUES ('checking_out', 'all', 1, 'x', 0, 'x')")
    assert "being placed right now" in run(env.say("/order all"))
    assert env.swiggy.calls == []


def test_unknown_order_blocks_new_proposals_and_shows_in_status(env):
    env.conn.execute("INSERT INTO orders (status, scope, proposed_by, proposed_at, queue_version) "
                     "VALUES ('unknown', 'all', 1, 'x', 0)")
    reply = run(env.say("/order all"))
    assert "/resolve 1 placed|failed" in reply and env.swiggy.calls == []
    assert "WARNING: order #1 is unknown" in run(env.say("/status"))


def test_new_proposal_supersedes_the_old_card(env):
    run(env.say("/order all"))
    run(env.say("/order mine"))
    assert env.statuses() == ["superseded", "proposed"]
    assert env.outbox.edited == [(GROUP, 1000, "This proposal was replaced by a newer one.")]
    assert env.outbox.edited_buttons == [None]  # buttons removed
    assert env.order(2)["card_message_id"] == 1001
    assert "Order #2: Priya's items" in env.outbox.sent[1].text
    assert "open order: #2" in run(env.say("/status")).lower()


def test_unavailable_items_are_dropped_and_requoted_once(env):
    env.swiggy.out_of_stock = {"B1"}
    run(env.say("/order all"))
    assert env.swiggy.n("update_cart") == 2
    assert [i["spinId"] for i in env.swiggy.calls[1][1]["items"]] == ["E1", "M1"]
    card = env.outbox.sent[0].text
    assert "Not available now (still queued): B1 (1 pc) ×1" in card and "To Pay: ₹232.60" in card
    ids = [r[0] for r in env.conn.execute("SELECT line_id FROM order_proposal_lines")]
    assert ids == [1, 2] and env.line_statuses() == ["queued"] * 3


def test_nothing_available_and_address_mismatch_are_refusals(env):
    env.swiggy.out_of_stock = {"E1", "M1", "B1"}
    assert "None of those items are available" in run(env.say("/order all"))
    env.swiggy.out_of_stock = set()
    env.swiggy.selected = "other"
    assert "different address" in run(env.say("/order all"))
    assert env.statuses() == [] and env.outbox.sent[-1].buttons is None


def test_cod_flag_does_not_hide_the_cash_button(env):
    env.swiggy.cod = False
    run(env.say("/order all"))
    card = env.outbox.sent[0]
    assert [b[1] for row in card.buttons for b in row] == ["o:1:u", "o:1:c", "o:1:x"]
    assert json.loads(env.order(1)["bill_json"])["cod"] is False


# ---- scope question (through the agent) ----

def call(name, **args):
    return ToolCall(f"c-{name}", name, json.dumps(args))


def test_agent_propose_order_all_ends_the_turn(conn, caplog):
    seed_queue(conn)
    llm = ScriptedLLM([LLMTurn(tool_calls=[call("propose_order", scope="all")])])  # no second round scripted
    env = Env(conn, llm)
    caplog.set_level(logging.INFO, logger="cartmates.agent.loop")
    run(env.say("@CartMatesBot order now"))
    assert len(llm.sent) == 1
    (card,) = env.outbox.sent  # the card only; no model reply after it
    assert card.buttons and "Order #1" in card.text
    rows = [(r["role"], r["text"]) for r in conn.execute("SELECT role, text FROM chat_memory ORDER BY id")]
    assert rows[-1] == ("assistant", "(posted the bill card for order #1)")
    line = next(r.getMessage() for r in caplog.records if r.getMessage().startswith("turn:"))
    assert "rounds=1" in line and "tools=propose_order" in line and "tokens_in=0" in line
    assert "outcome=proposed" in line


def test_agent_refusal_goes_back_to_the_model(conn):
    llm = ScriptedLLM([LLMTurn(tool_calls=[call("propose_order", scope="mine")]), LLMTurn(text="Nothing queued.")])
    env = Env(conn, llm)
    run(env.say("@CartMatesBot order mine"))
    output = next(i["output"] for i in llm.sent[1][1] if i.get("type") == "function_call_output")
    assert output == "error: You have nothing in the queue to order."
    assert [s.text for s in env.outbox.sent] == ["Nothing queued."]


def test_agent_scope_ask_then_only_the_asker_can_tap(conn):
    seed_queue(conn)
    llm = ScriptedLLM([LLMTurn(tool_calls=[call("propose_order", scope="ask")])])
    env = Env(conn, llm)
    run(env.say("@CartMatesBot I want eggs now"))
    (ask,) = env.outbox.sent
    assert ask.buttons == [[("Just yours (₹220)", "s:1:m"), ("Whole queue (₹265)", "s:1:a")]]
    assert conn.execute("SELECT kind FROM pending").fetchone()[0] == "scope"
    run(env.tap("s:1:m", user=ROHAN))
    assert env.outbox.answers == [("tap", "Only Priya can pick this", True)]
    assert env.statuses() == []
    run(env.tap("s:1:m", user=PRIYA))
    assert env.outbox.answers[-1] == ("tap", None, False)
    assert env.order(1)["scope"] == "mine" and conn.execute("SELECT count(*) FROM pending").fetchone()[0] == 0
    assert env.outbox.sent[1].buttons[-1] == [("Cancel", "o:1:x")]
    run(env.tap("s:1:m", user=PRIYA))  # a second tap finds the question gone
    assert env.outbox.answers[-1][1] == "This choice expired" and env.statuses() == ["proposed"]


# ---- confirm ----

def test_confirm_happy_path_records_everything(env):
    run(env.say("/order all"))
    version = env.conn.execute("SELECT value FROM kv WHERE key='queue_version'").fetchone()[0]
    run(env.tap("o:1:c", user=ROHAN, message_id=1000))
    o = env.order(1)
    assert (o["status"], o["swiggy_order_id"], o["orderer"]) == ("placed", "ORD123", ROHAN)
    assert o["claimed_at"] and o["placed_at"]
    assert env.swiggy.n("checkout") == 1
    assert [t for t, _ in env.swiggy.calls].count("update_cart") == 2  # proposal + the re-quote at confirm
    # Eggs 120 -> 60/60. Milk 100 -> 3334/3333/3333 (leftover paise to the lowest id). Bread 45 -> Meera.
    assert env.shares(1) == {PRIYA: (9334, ROHAN), ROHAN: (9333, ROHAN), MEERA: (7833, ROHAN)}  # incl. the orderer's own
    assert sum(a for a, _ in env.shares(1).values()) == 26500  # item total; fees stay with the orderer
    assert env.line_statuses() == ["ordered"] * 3
    assert [r[0] for r in env.conn.execute("SELECT order_id FROM queue_lines")] == [1, 1, 1]
    items = env.conn.execute("SELECT name, qty, line_price_paise FROM order_items ORDER BY id").fetchall()
    assert [tuple(i) for i in items] == [("E1", 2, 12000), ("M1", 1, 10000), ("B1", 1, 4500)]
    owners = env.conn.execute("SELECT count(*) FROM order_item_owners").fetchone()[0]
    assert owners == 2 + 3 + 1
    assert int(env.conn.execute("SELECT value FROM kv WHERE key='queue_version'").fetchone()[0]) == int(version) + 1
    first, last = env.outbox.edited[0], env.outbox.edited[-1]
    assert first[1] == 1000 and first[2].startswith("Placing order #1... (Rohan)")
    assert last[2] == ("✅ Ordered by Rohan: Swiggy #ORD123, ₹277.60 cash on delivery\n"
                       "Owes Rohan: Priya ₹93.34, Meera ₹78.33")
    assert env.outbox.answers == [("tap", None, False)]


def test_state_transitions_are_logged_with_the_order_id(env, caplog):
    caplog.set_level(logging.INFO, logger="cartmates.executor.order_executor")
    run(env.say("/order all"))
    run(env.tap("o:1:c"))
    msgs = [r.getMessage() for r in caplog.records]
    assert [m.split(" (")[0] for m in msgs] == [
        "order #1 -> claimed", "order #1 -> quoting", "order #1 -> checking_out", "order #1 -> placed"]


def test_double_confirm_tap_is_a_noop(env):
    run(env.say("/order all"))
    run(env.tap("o:1:c"))
    run(env.tap("o:1:c", user=ROHAN))
    assert env.swiggy.n("checkout") == 1
    assert env.outbox.answers == [("tap", None, False), ("tap", "Already handled", False)]
    assert env.order(1)["orderer"] == PRIYA
    assert env.conn.execute("SELECT count(*) FROM order_shares").fetchone()[0] == 3


def test_stale_queue_supersedes_and_reproposes(env):
    run(env.say("/order all"))
    with db.transaction(env.conn):
        queue.update_line(env.conn, 1, PRIYA, qty=3)
    run(env.tap("o:1:c"))
    assert env.swiggy.n("checkout") == 0
    assert env.statuses() == ["superseded", "proposed"]
    assert "quantity of E1 changed" in env.order(1)["error"]
    assert "Updated: The quantity of E1 changed." in env.outbox.sent[-1].text
    assert "E1 (1 pc) ×3" in env.outbox.sent[-1].text
    run(env.tap("o:2:c"))
    assert env.order(2)["status"] == "placed"


def test_removed_line_also_makes_the_bill_stale(env):
    run(env.say("/order all"))
    with db.transaction(env.conn):
        queue.remove_line(env.conn, 3, MEERA)
    run(env.tap("o:1:c"))
    assert env.statuses() == ["superseded", "proposed"] and env.swiggy.n("checkout") == 0
    assert "no longer in the queue" in env.order(1)["error"]


def test_price_drift_beyond_tolerance_reproposes(env):
    run(env.say("/order all"))
    env.swiggy.surcharge = Decimal(20)  # 277.60 -> 297.60, +7.2%
    run(env.tap("o:1:c"))
    assert env.swiggy.n("checkout") == 0
    assert env.statuses() == ["superseded", "proposed"]
    assert "total rose from ₹277.60 to ₹297.60" in env.outbox.sent[-1].text
    assert env.swiggy.n("update_cart") == 3  # proposal, confirm re-quote, fresh proposal


def test_price_drift_within_tolerance_goes_through(env):
    run(env.say("/order all"))
    env.swiggy.surcharge = Decimal(10)  # +3.6%
    run(env.tap("o:1:c"))
    assert env.order(1)["status"] == "placed" and env.swiggy.n("checkout") == 1
    assert json.loads(env.order(1)["bill_json"])["to_pay"] == 28760  # the final bill is what we record


def test_out_of_stock_at_confirm_drops_the_item_and_reproposes(env):
    run(env.say("/order all"))
    env.swiggy.out_of_stock = {"B1"}
    run(env.tap("o:1:c"))
    assert env.swiggy.n("checkout") == 0
    assert env.statuses() == ["superseded", "proposed"]
    new = env.outbox.sent[-1].text
    assert "B1 not available now" in new and "Not available now (still queued): B1 (1 pc) ×1" in new
    assert env.line_statuses() == ["queued"] * 3


def test_capped_quantity_at_confirm_counts_as_unavailable(env):
    run(env.say("/order all"))
    env.swiggy.caps = {"E1": 1}
    run(env.tap("o:1:c"))
    assert env.swiggy.n("checkout") == 0 and env.statuses() == ["superseded", "proposed"]


def test_logged_out_before_checkout_supersedes_with_a_message(env):
    run(env.say("/order all"))
    env.swiggy.cart_error = LoggedOut("x")
    run(env.tap("o:1:c"))
    assert env.swiggy.n("checkout") == 0
    assert env.statuses() == ["superseded"]
    assert env.order(1)["error"] == "Swiggy logged out; log in and run /order again"
    assert env.outbox.sent[-1].text == "Swiggy logged out; log in and run /order again."
    assert env.line_statuses() == ["queued"] * 3


def test_unreachable_before_checkout_fails_the_order_cleanly(env):
    run(env.say("/order all"))
    env.swiggy.cart_error = SwiggyUnavailable("down")
    run(env.tap("o:1:c"))
    assert env.order(1)["status"] == "failed" and env.swiggy.n("checkout") == 0
    assert "nothing was ordered" in env.outbox.sent[-1].text


# ---- checkout errors and verify ----

@pytest.mark.parametrize("mode", ["unavailable", "odd", "tool_error"])
def test_checkout_error_then_verify_finds_the_order(env, mode):
    run(env.say("/order all"))
    env.swiggy.checkout_mode = mode
    env.swiggy.add_recent("ORD777", names=("E1", "M1"))  # overlap is enough
    run(env.tap("o:1:c", user=ROHAN))
    assert env.swiggy.n("checkout") == 1 and env.swiggy.n("get_orders") == 1  # never retried
    o = env.order(1)
    assert (o["status"], o["swiggy_order_id"]) == ("placed", "ORD777")
    assert env.line_statuses() == ["ordered"] * 3 and len(env.shares(1)) == 3


@pytest.mark.parametrize("recent", [
    {},                                                    # no orders at all
    {"age": timedelta(hours=3)},                          # an old order with the same items
    {"address": "elsewhere"},                             # another address
    {"names": ("Something Else",)},                       # no item overlap
])
def test_checkout_error_and_no_match_is_unknown_and_blocks_orders(env, recent):
    run(env.say("/order all"))
    env.swiggy.checkout_mode = "unavailable"
    if recent != {}:
        env.swiggy.add_recent(**recent)
    run(env.tap("o:1:c"))
    assert env.order(1)["status"] == "unknown" and env.swiggy.n("checkout") == 1
    assert env.line_statuses() == ["queued"] * 3 and env.conn.execute("SELECT count(*) FROM order_shares").fetchone()[0] == 0
    assert "/resolve 1 placed" in env.outbox.sent[-1].text
    assert "/resolve 1 placed|failed" in run(env.say("/order all"))


def test_logged_out_during_checkout_that_cannot_verify_is_unknown(env):
    run(env.say("/order all"))
    env.swiggy.checkout_mode = "logged_out"
    env.swiggy.orders_error = LoggedOut("still out")
    run(env.tap("o:1:c"))
    assert env.order(1)["status"] == "unknown"


def test_resolve_placed_records_the_order_without_a_swiggy_id(env):
    run(env.say("/order all"))
    env.swiggy.checkout_mode = "unavailable"
    run(env.tap("o:1:c", user=ROHAN))
    reply = run(env.say("/resolve 1 placed", user=MEERA))
    o = env.order(1)
    assert (o["status"], o["swiggy_order_id"], o["orderer"]) == ("placed", None, ROHAN)
    assert env.line_statuses() == ["ordered"] * 3
    assert reply == "Order #1 recorded as placed (ordered by Rohan).\nOwes Rohan: Priya ₹93.34, Meera ₹78.33"
    assert run(env.say("/resolve 1 placed")) == "Order #1 isn't waiting to be resolved."
    run(env.say("/order all"))  # no longer blocked (queue is empty now)
    assert env.outbox.sent[-1].text == "The queue is empty."


def test_resolve_failed_leaves_lines_queued_and_unblocks(env):
    run(env.say("/order all"))
    env.swiggy.checkout_mode = "odd"
    run(env.tap("o:1:c"))
    assert "marked failed" in run(env.say("/resolve #1 failed"))
    assert env.order(1)["status"] == "failed" and env.line_statuses() == ["queued"] * 3
    env.swiggy.checkout_mode = "confirmed"
    run(env.say("/order all"))
    assert env.order(2)["status"] == "proposed"


def test_resolve_usage(env):
    assert run(env.say("/resolve")).startswith("Usage")
    assert run(env.say("/resolve 1 maybe")).startswith("Usage")
    assert run(env.say("/resolve 9 placed")) == "Order #9 isn't waiting to be resolved."


# ---- cancel ----

def test_cancel_by_another_member_leaves_lines_alone(env):
    run(env.say("/order all"))
    run(env.tap("o:1:x", user=ROHAN, message_id=1000))
    assert env.statuses() == ["cancelled"] and env.line_statuses() == ["queued"] * 3
    assert env.outbox.edited == [(GROUP, 1000, "Order #1 cancelled by Rohan.")]
    run(env.tap("o:1:c"))  # confirm after cancel does nothing
    assert env.swiggy.n("checkout") == 0
    assert env.outbox.answers == [("tap", None, False), ("tap", "Already handled", False)]
    run(env.tap("o:1:x"))
    assert env.outbox.answers[-1] == ("tap", "Already handled", False)


def test_every_tap_is_answered_even_when_garbage(env):
    run(env.tap("o:zzz:c"))
    run(env.tap("o:1"))
    run(env.tap("s:99:m"))
    run(env.tap("o:99:c"))
    assert [a[1] for a in env.outbox.answers] == ["This button expired", "This button expired",
                                                  "This choice expired", "Already handled"]


# ---- history ----

def test_history_list_and_detail(env):
    assert run(env.say("/history")) == "No orders yet."
    run(env.say("/order all"))
    run(env.tap("o:1:c", user=ROHAN))
    listing = run(env.say("/history"))
    assert re.fullmatch(r"Recent orders:\n#1 \d{4}-\d\d-\d\d Rohan ₹277.60 placed", listing)
    detail = run(env.say("/history 1"))
    assert detail.splitlines() == [
        "Order #1: placed, Swiggy #ORD123, ordered by Rohan, To Pay ₹277.60",
        "- E1 1 pc x2, Priya, Rohan: ₹120", "- M1 1 pc x1, Priya, Rohan, Meera: ₹100",
        "- B1 1 pc x1, Meera: ₹45", "Owes Rohan: Priya ₹93.34, Meera ₹78.33"]
    assert run(env.say("/history 42")) == "There is no order #42."
    assert run(env.say("/history abc")).startswith("Usage")


def test_history_hides_superseded_proposals(env):
    run(env.say("/order all"))
    run(env.say("/order mine"))
    lines = run(env.say("/history")).splitlines()
    assert len(lines) == 2 and lines[1].startswith("#2") and lines[1].endswith("proposed")


# ---- observability ----

def test_turn_id_lands_on_events_rows(conn):
    seed_queue(conn)
    env = Env(conn)

    async def go():
        lane = Lane(env.h.handle, env.outbox)
        lane.start()
        await lane.submit(Message(GROUP, 5, PRIYA, "Priya", "/order all", True, "order", "all"))
        await asyncio.sleep(0.3)
        await lane.submit(CallbackTap(GROUP, 1000, ROHAN, "Rohan", "o:1:x", "t"))
        await asyncio.sleep(0.2)
        await lane.stop()
    asyncio.run(go())
    rows = conn.execute("SELECT kind, turn_id FROM events WHERE kind != 'queue_add' ORDER BY id").fetchall()
    assert [r["kind"] for r in rows] == ["message", "order_proposed", "tap", "order_cancelled"]
    ids = [r["turn_id"] for r in rows]
    assert all(re.fullmatch(r"[0-9a-f]{12}", i) for i in ids)
    assert ids[0] == ids[1] and ids[2] == ids[3] and ids[0] != ids[2]


def test_turn_id_filter_defaults_to_a_dash():
    record = logging.LogRecord("x", logging.INFO, "f", 1, "m", None, None)
    observe.turn_id.set(None)
    observe.TurnIdFilter().filter(record)
    assert record.turn_id == "-"
    observe.turn_id.set("abc123abc123")
    observe.TurnIdFilter().filter(record)
    assert record.turn_id == "abc123abc123"
    observe.turn_id.set(None)


def test_outbox_edit_can_replace_buttons_and_send_returns_ids():
    async def go():
        o = FakeOutbox()
        assert await o.send(1, "a") == 1000 and await o.send(1, "b") == 1001
        await o.edit(1, 1000, "x", [[("Go", "g")]])
        await o.edit(1, 1001, "y")
        assert o.edited_buttons == [[[("Go", "g")]], None]
    asyncio.run(go())


def test_checkout_tool_error_text_reaches_the_group_and_the_order(env):
    run(env.say("/order all"))
    env.swiggy.checkout_mode = "tool_error"
    run(env.tap("o:1:c"))
    assert env.order(1)["status"] == "unknown"
    assert "payment failed" in env.order(1)["error"]
    assert "Swiggy said: SwiggyToolError: payment failed" in env.outbox.sent[-1].text


@pytest.mark.parametrize("status, payment", [("FAILED", ""), ("CANCELLED", "REFUNDED"), ("PLACED", "PENDING"),
                                             ("CONFIRMED", "FAILED")])
def test_verify_ignores_failed_cancelled_or_unpaid_orders(env, status, payment):
    run(env.say("/order all"))
    env.swiggy.checkout_mode = "unavailable"
    env.swiggy.add_recent(status=status, payment_status=payment)
    run(env.tap("o:1:c"))
    assert env.order(1)["status"] == "unknown"
