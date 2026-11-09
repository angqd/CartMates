"""UPI payment and clear refusals (DESIGN 7.3). Fake Swiggy, fake links, injected sleep and clock: nothing waits."""
import json
import logging

import pytest

from cartmates.swiggy.errors import SwiggyToolError, SwiggyUnavailable
from cartmates.telegram.outbox import UrlButton, _markup
from tests.test_orders import ADDRESS, GROUP, MEERA, PRIYA, ROHAN, Env, FakeSwiggy, conn, run, seed_queue  # noqa: F401

PAY_URL = "https://example.test/pay/abc"
INTENT_URL = "gpay://example.test/intent/xyz"
REFUSAL = "Failed to create order: API returned error: User is not eligible for COD order"
PENDING = {"paasId": "P1", "status": "pending", "terminal": False, "isTerminalSuccess": False,
           "isTerminalFailure": False, "orderId": "SW1"}
SUCCESS = {**PENDING, "status": "SUCCESS", "terminal": True, "isTerminalSuccess": True}
FAILED = {**PENDING, "status": "failed", "terminal": True, "isTerminalFailure": True}


class UpiSwiggy(FakeSwiggy):
    def __init__(self):
        super().__init__()
        self.upi_mode = "pending"       # pending | odd | tool_error
        self.cod_error: Exception | None = None
        self.statuses = [PENDING]       # answers of check_payment_status; the last one repeats
        self.confirm_error: Exception | None = None
        self.on_confirm = None
        self.window_ms, self.interval_ms = 60000, 1000

    async def checkout(self, args):
        if args["paymentMethod"] == "Cash":
            if self.cod_error:
                raise self.cod_error
            return await super().checkout(args)
        assert args == {"addressId": ADDRESS, "paymentMethod": "UPI", "intentApp": "gpay://upi/"}
        if self.upi_mode == "tool_error":
            raise SwiggyToolError("payment broke")
        if self.upi_mode == "odd":
            return {"status": "PENDING_PAYMENT", "orderId": "SW1", "paasId": "P1", "upiIntentUrl": INTENT_URL}
        return {"orderId": "SW1", "transactionId": "T1", "paasId": "P1", "upiIntentUrl": INTENT_URL,
                "bridgeUrl": PAY_URL, "pollingIntervalInMs": self.interval_ms,
                "maxTimeToPollForInMs": self.window_ms, "status": "PENDING_PAYMENT", "cartTotal": 277}

    async def check_payment_status(self, args):
        assert args == {"paasId": "P1", "orderId": "SW1"}
        n = self.n("check_payment_status")
        return self.statuses[min(n, len(self.statuses)) - 1]

    async def confirm_order(self, args):
        assert args == {"orderId": "SW1", "paasId": "P1"}
        if self.on_confirm:
            self.on_confirm()
        if self.confirm_error:
            raise self.confirm_error
        return {"status": "CONFIRMED"}


class UpiEnv(Env):
    def __init__(self, conn):
        super().__init__(conn)
        self.swiggy = UpiSwiggy()
        self.h.executor.swiggy = self.swiggy
        self.now, self.sleeps = 0.0, []
        self.h.executor.clock = lambda: self.now
        self.h.executor.sleep = self.sleep

    async def sleep(self, secs):
        self.sleeps.append(secs)
        self.now += secs

    def pay_edits(self):
        return [(t, b) for (_, mid, t), b in zip(self.outbox.edited, self.outbox.edited_buttons) if mid == 1001]


@pytest.fixture
def env(conn):
    seed_queue(conn)
    return UpiEnv(conn)


def placed_ok(env, swiggy_id="SW1"):
    o = env.order(1)
    assert (o["status"], o["swiggy_order_id"], o["payment_method"]) == ("placed", swiggy_id, "upi")
    assert env.line_statuses() == ["ordered"] * 3 and len(env.shares(1)) == 3


def unpaid_ok(env, status="failed"):
    assert env.order(1)["status"] == status and env.line_statuses() == ["queued"] * 3
    assert env.conn.execute("SELECT count(*) FROM order_shares").fetchone()[0] == 0


# ---- the card and the outbox ----

def test_card_has_upi_cash_and_cancel(env):
    run(env.say("/order all"))
    assert env.outbox.sent[0].buttons == [[("Pay by UPI ₹277.60", "o:1:u"), ("Cash ₹277.60", "o:1:c")],
                                          [("Cancel", "o:1:x")]]


def test_url_button_markup_and_plain_buttons_still_work():
    markup = _markup([[UrlButton("Pay", PAY_URL), ("Go", "g:1")], [("Cancel", "x")]])
    first, second = markup.inline_keyboard[0]
    assert first.url == PAY_URL and first.callback_data is None
    assert second.callback_data == "g:1" and second.url is None
    assert _markup(None) is None and _markup([]) is None  # edit(buttons=None) removes every button


# ---- UPI ----

def test_upi_happy_path(env):
    env.swiggy.statuses = [PENDING, PENDING, SUCCESS]
    run(env.say("/order all"))
    run(env.tap("o:1:u", user=ROHAN, message_id=1000))
    placed_ok(env)
    assert env.order(1)["orderer"] == ROHAN
    assert env.swiggy.n("checkout") == 1 and env.swiggy.n("check_payment_status") == 3
    assert env.swiggy.n("confirm_order") == 1
    assert env.sleeps == [2.0, 2.0]  # Swiggy asked for 1 s; the floor is 2 s
    assert env.shares(1)[ROHAN][1] == ROHAN and sum(a for a, _ in env.shares(1).values()) == 26500
    pay = env.outbox.sent[1]
    assert pay.reply_to == 1000 and pay.buttons == [[UrlButton("Pay ₹277.60 in GPay", PAY_URL)]]
    assert pay.text == "Rohan: pay ₹277.60 in the next 60 s. Don't pay after the button disappears."
    assert env.pay_edits() == [("Paid ✅", None)]
    pj = json.loads(env.order(1)["payment_json"])
    assert (pj["orderId"], pj["paasId"], pj["transactionId"], pj["bridgeUrl"], pj["windowMs"]) == (
        "SW1", "P1", "T1", PAY_URL, 60000)
    assert "paid by UPI" in env.outbox.edited[-1][2] and "Swiggy #SW1" in env.outbox.edited[-1][2]


def test_upi_failure_leaves_lines_queued_and_never_confirms(env):
    env.swiggy.statuses = [PENDING, FAILED]
    run(env.say("/order all"))
    run(env.tap("o:1:u"))
    unpaid_ok(env)
    assert env.swiggy.n("confirm_order") == 0 and env.swiggy.n("get_orders") == 0
    assert env.pay_edits() == [("Payment failed; nothing was ordered.", None)]
    run(env.say("/order all"))  # not blocked
    assert env.statuses() == ["failed", "proposed"]


def test_upi_window_closes_button_is_removed_before_confirm_order(env):
    seen = []
    env.swiggy.window_ms = 10000
    env.swiggy.on_confirm = lambda: seen.append(env.pay_edits())
    run(env.say("/order all"))
    run(env.tap("o:1:u"))
    assert seen == [[("Payment window closed. Checking with Swiggy...", None)]]  # edit came first, no buttons
    assert env.swiggy.n("confirm_order") == 1 and env.swiggy.n("get_orders") == 1
    unpaid_ok(env)
    assert env.now >= 25.0  # window plus the 15 s grace, on the fake clock
    final = env.pay_edits()[-1]
    assert final[1] is None and "Swiggy refunds it within ~4 days" in final[0] and "/resolve 1 placed" in final[0]


def test_upi_window_closes_but_get_orders_finds_the_order(env):
    env.swiggy.window_ms = 10000
    env.swiggy.add_recent("SW9", names=("E1", "M1"))
    run(env.say("/order all"))
    run(env.tap("o:1:u"))
    placed_ok(env, "SW9")
    assert env.pay_edits()[-1] == ("Paid ✅", None)


def test_upi_window_confirm_order_error_is_ignored(env):
    env.swiggy.window_ms = 10000
    env.swiggy.confirm_error = SwiggyToolError("already failed")
    run(env.say("/order all"))
    run(env.tap("o:1:u"))
    unpaid_ok(env)
    assert env.swiggy.n("confirm_order") == 1


def test_upi_odd_checkout_answer_goes_to_verify(env):
    env.swiggy.upi_mode = "odd"  # PENDING_PAYMENT without a bridgeUrl
    run(env.say("/order all"))
    run(env.tap("o:1:u"))
    assert env.order(1)["status"] == "unknown" and len(env.outbox.sent) == 2  # no pay message
    assert env.swiggy.n("get_orders") == 1 and env.swiggy.n("checkout") == 1
    assert INTENT_URL not in env.order(1)["error"] and INTENT_URL not in env.outbox.sent[-1].text


def test_upi_odd_checkout_answer_but_order_exists_is_placed(env):
    env.swiggy.upi_mode = "odd"
    env.swiggy.add_recent("SW5")
    run(env.say("/order all"))
    run(env.tap("o:1:u"))
    placed_ok(env, "SW5")


def test_upi_confirm_order_error_after_success_verify_found(env):
    env.swiggy.statuses = [SUCCESS]
    env.swiggy.confirm_error = SwiggyUnavailable("timeout")
    env.swiggy.add_recent("SW1", names=("E1", "M1", "B1"))
    run(env.say("/order all"))
    run(env.tap("o:1:u"))
    placed_ok(env)
    assert env.swiggy.n("confirm_order") == 1


def test_upi_confirm_order_error_after_success_not_found_is_unknown(env):
    env.swiggy.statuses = [SUCCESS]
    env.swiggy.confirm_error = SwiggyUnavailable("timeout")
    run(env.say("/order all"))
    run(env.tap("o:1:u"))
    assert env.order(1)["status"] == "unknown" and env.line_statuses() == ["queued"] * 3
    assert env.pay_edits()[-1][1] is None
    assert "/resolve 1 placed|failed" in run(env.say("/order all"))


def test_upi_checkout_tool_error_without_a_clear_refusal_is_unknown(env):
    env.swiggy.upi_mode = "tool_error"
    run(env.say("/order all"))
    run(env.tap("o:1:u"))
    assert env.order(1)["status"] == "unknown"


# ---- clear refusals ----

def test_cod_refusal_is_failed_not_unknown_and_does_not_block(env):
    env.swiggy.cod_error = SwiggyToolError(REFUSAL)
    run(env.say("/order all"))
    run(env.tap("o:1:c"))
    unpaid_ok(env)
    assert env.order(1)["payment_method"] == "cod" and env.swiggy.n("get_orders") == 1
    why = "Not ordered. Swiggy said: " + REFUSAL
    assert why in env.outbox.edited[-1][2] and why in env.outbox.sent[-1].text
    run(env.say("/order all"))
    assert env.statuses() == ["failed", "proposed"]


def test_refusal_but_verify_finds_an_order_is_placed(env):
    env.swiggy.cod_error = SwiggyToolError(REFUSAL)
    env.swiggy.add_recent("SW7")
    run(env.say("/order all"))
    run(env.tap("o:1:c"))
    assert env.order(1)["status"] == "placed" and env.order(1)["swiggy_order_id"] == "SW7"


def test_refusal_when_verify_itself_fails_is_unknown(env):
    env.swiggy.cod_error = SwiggyToolError(REFUSAL)
    env.swiggy.orders_error = SwiggyUnavailable("down")
    run(env.say("/order all"))
    run(env.tap("o:1:c"))
    assert env.order(1)["status"] == "unknown"


def test_network_error_is_still_unknown(env):
    env.swiggy.cod_error = SwiggyUnavailable("ReadTimeout")
    run(env.say("/order all"))
    run(env.tap("o:1:c"))
    assert env.order(1)["status"] == "unknown"


def test_upi_refusal_text_is_failed_too(env):
    env.swiggy.upi_mode = "tool_error"
    env.swiggy.on_confirm = None
    run(env.say("/order all"))
    # replace the generic error with a clear refusal
    async def refuse(args):
        raise SwiggyToolError("Failed to create order: nope")
    env.swiggy.checkout = refuse
    run(env.tap("o:1:u"))
    unpaid_ok(env)


# ---- /resolve for late payments ----

def test_resolve_placed_works_for_a_failed_upi_order(env):
    env.swiggy.window_ms = 10000
    run(env.say("/order all"))
    run(env.tap("o:1:u", user=ROHAN))
    unpaid_ok(env)
    reply = run(env.say("/resolve 1 placed", user=MEERA))
    assert "recorded as placed" in reply
    placed_ok(env)
    assert env.order(1)["orderer"] == ROHAN  # swiggy order id kept from the payment


def test_resolve_does_not_reopen_other_failed_orders(env):
    env.swiggy.cod_error = SwiggyToolError(REFUSAL)
    run(env.say("/order all"))
    run(env.tap("o:1:c"))
    assert run(env.say("/resolve 1 placed")) == "Order #1 isn't waiting to be resolved."


def test_resolve_late_upi_refuses_when_the_items_moved_on(env):
    env.swiggy.window_ms = 10000
    run(env.say("/order all"))
    run(env.tap("o:1:u"))
    env.conn.execute("UPDATE queue_lines SET status='removed' WHERE id=1")
    assert "changed since" in run(env.say("/resolve 1 placed"))
    assert env.order(1)["status"] == "failed"


# ---- logs ----

def test_logs_never_carry_payment_links_and_show_payment_changes(env, caplog):
    caplog.set_level(logging.DEBUG)
    env.swiggy.statuses = [PENDING, SUCCESS]
    run(env.say("/order all"))
    run(env.tap("o:1:u"))
    text = "\n".join(r.getMessage() for r in caplog.records)
    for bad in ("deeplink-redirect", "upi/pay", PAY_URL, INTENT_URL):
        assert bad not in text
    assert "order #1: pay link sent" in text and "order #1: payment pending -> success" in text
    assert "order #1 -> placed" in text


def test_logs_do_not_leak_links_on_the_odd_answer_path(env, caplog):
    caplog.set_level(logging.DEBUG)
    env.swiggy.upi_mode = "odd"
    run(env.say("/order all"))
    run(env.tap("o:1:u"))
    assert INTENT_URL not in "\n".join(r.getMessage() for r in caplog.records)
