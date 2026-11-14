"""Confirm an order: claim, stale check, re-quote, checkout once, verify on doubt, record (DESIGN 7, 7.1).
Everything runs inside the household lane, so no queue edit can land between steps.
Whatever happens, an order is never left in claimed / quoting / checking_out when confirm returns."""
import json
import logging
import asyncio
import sqlite3
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from cartmates import tracing
from cartmates.domain import address, orders
from cartmates.domain.orders import OrderError
from cartmates.domain.queue import log_event, name_of, now_iso, rupees
from cartmates.swiggy import cart
from cartmates.swiggy.client import SwiggyClient
from cartmates.swiggy.errors import LoggedOut, SwiggyToolError
from cartmates.telegram.events import CallbackTap
from cartmates.telegram.outbox import Outbox, UrlButton

log = logging.getLogger(__name__)

VERIFY_SLACK = timedelta(minutes=2)
TOTAL_SLACK_PAISE = 100  # Swiggy's order total may round differently from the quote
LOGGED_OUT_TEXT = "Swiggy logged out; log in and run /order again"
Answer = Callable[[str | None], Awaitable[None]]
MIN_POLL_S = 2.0       # never poll Swiggy faster than this, whatever it asks for
POLL_GRACE_S = 15.0    # polling goes on this long past Swiggy's own payment window
REFUSAL_MARKERS = ("failed to create order", "not eligible")
WINDOW_CLOSED_TEXT = ("Payment window closed, nothing was ordered. If you paid anyway, Swiggy refunds it within "
                      "~4 days. If the order shows up in the Swiggy app, run /resolve {id} placed.")
UPI_NAMES = {"gpay": "GPay", "phonepe": "PhonePe", "paytm": "Paytm", "bhim": "BHIM"}


@dataclass
class Outcome:
    kind: str                  # placed | failed | unknown
    swiggy_id: str | None = None
    text: str = ""             # why, for failed / unknown
    announce: bool = True      # also post it as a group message (not needed when the pay message said it)


class _Unconfirmed(Exception):
    """Checkout gave no CONFIRMED answer, so the outcome has to be verified."""


class OrderExecutor:
    def __init__(self, conn: sqlite3.Connection, outbox: Outbox, swiggy: SwiggyClient,
                 price_tolerance: float = 0.05,
                 upi_app: str = "gpay://upi/", sleep=asyncio.sleep, clock=time.monotonic,
                 checkout_timeout_s: float = 45.0):
        self.upi_app, self.sleep, self.clock = upi_app, sleep, clock
        self.checkout_timeout_s = checkout_timeout_s
        self.conn, self.outbox, self.swiggy = conn, outbox, swiggy
        self.tolerance = price_tolerance

    @property
    def address_id(self) -> str | None:
        return address.current(self.conn)

    # ---- state ----

    def _move(self, order_id: int, status: str, error: str | None = None) -> None:
        self.conn.execute("UPDATE orders SET status=?, error=COALESCE(?, error) WHERE id=?",
                          (status, error, order_id))
        log.info("order #%s -> %s", order_id, status)
        log_event(self.conn, "system", "order_status", {"order_id": order_id, "status": status})
        tracing.event(status, order_id=order_id)
        if status in ("failed", "unknown"):  # placed is remembered by orders.record_placed
            why = (error or "").replace("\n", " ")[:120]
            orders.remember_outcome(self.conn, f"order #{order_id} {status}" + (f": {why}" if why else ""))

    def _status(self, order_id: int) -> str:
        return self.conn.execute("SELECT status FROM orders WHERE id=?", (order_id,)).fetchone()[0]

    async def _card(self, order: sqlite3.Row, text: str, tap: CallbackTap) -> None:
        try:
            await self.outbox.edit(tap.chat_id, order["card_message_id"] or tap.message_id, text)
        except Exception:
            log.warning("could not edit the card of order #%s", order["id"], exc_info=True)

    # ---- confirm ----

    @tracing.traced("order.confirm", attrs=lambda self, order_id, tapper, answer=None, method="cod": {
        "cartmates.order_id": order_id, "cartmates.payment_method": method, "user.id": str(tapper.user_id),
        "session.id": str(tapper.chat_id)})
    async def confirm(self, order_id: int, tapper: CallbackTap, answer: Answer | None = None,
                      method: str = "cod") -> None:
        """`answer` is called once, right after the claim, with a toast text (None = plain).
        `method` is "cod" or "upi"."""
        cur = self.conn.execute(
            "UPDATE orders SET status='claimed', orderer=?, claimed_at=?, payment_method=? "
            "WHERE id=? AND status='proposed'", (tapper.user_id, now_iso(), method, order_id))
        if answer:
            await answer(None if cur.rowcount else "Already handled")
        if not cur.rowcount:
            return
        log.info("order #%s -> claimed (by %s, %s)", order_id, tapper.user_id, method)
        log_event(self.conn, tapper.user_id, "order_claimed", {"order_id": order_id})
        tracing.event("claimed", order_id=order_id, method=method)
        order = self.conn.execute("SELECT * FROM orders WHERE id=?", (order_id,)).fetchone()
        await self._card(order, f"Placing order #{order_id}... ({tapper.display_name})", tapper)
        try:
            await self._run(order, tapper, method)
        except Exception as e:  # last resort: no order may stay in flight
            log.exception("order #%s: unexpected failure", order_id)
            await self._settle_after_crash(order, tapper, e)

    async def _run(self, order: sqlite3.Row, tapper: CallbackTap, method: str = "cod") -> None:
        order_id = order["id"]
        now_addr = self.address_id
        if not now_addr or (order["address_id"] and not address.same(order["address_id"], now_addr)):
            self._move(order_id, "superseded", "address changed")
            await self._card(order, f"Order #{order_id} not placed: the delivery address changed "
                                    "since this bill. Run /order again.", tapper)
            return
        proposal = orders.proposal_lines(self.conn, order_id)
        stale = self._stale_reason(proposal)
        if stale:
            await self._reproposal(order, tapper, stale)
            return
        self._move(order_id, "quoting")
        try:
            q = await cart.quote(self.swiggy, now_addr,
                                 [(l["spin_id"], l["sku_id"], l["qty_at_proposal"]) for l in proposal])
        except LoggedOut:
            self._move(order_id, "superseded", LOGGED_OUT_TEXT)
            await self._card(order, f"Order #{order_id} not placed: {LOGGED_OUT_TEXT}.", tapper)
            await self.outbox.send(tapper.chat_id, f"{LOGGED_OUT_TEXT}.")
            return
        bill = json.loads(order["bill_json"] or "{}")
        if q.unavailable:
            names = ", ".join(l["name"] for l in proposal if l["spin_id"] in q.unavailable)
            await self._reproposal(order, tapper, f"{names} not available now, so the bill changed.")
            return
        if q.to_pay_paise > bill.get("to_pay", 0) * (1 + self.tolerance):
            await self._reproposal(order, tapper, f"The total rose from {rupees(bill.get('to_pay', 0))} "
                                                  f"to {rupees(q.to_pay_paise)}.")
            return
        self._move(order_id, "checking_out")
        # The exact total sent to checkout, so verify can match on it even after a restart.
        self.conn.execute("UPDATE orders SET bill_json=json_set(COALESCE(bill_json, '{}'), '$.checkout_to_pay', ?) "
                          "WHERE id=?", (q.to_pay_paise, order_id))
        self.checkout_error = None
        out = await self._checkout_or_verify(order, proposal, tapper, q, method)
        if out.kind == "failed":
            return await self._failed(order, tapper, out.text, out.announce)
        if out.kind == "unknown":
            why = out.text or (f"Swiggy said: {self.checkout_error}" if self.checkout_error
                               else "checkout did not confirm")
            return await self._unknown(order, tapper, why)
        swiggy_id = out.swiggy_id
        unit_prices = {s: sq.unit_paise for s, sq in q.spins.items()}
        orders.record_placed(self.conn, order_id, tapper.user_id, swiggy_id, unit_prices, q.to_pay_paise)
        tracing.event("placed", order_id=order_id, swiggy_order_id=swiggy_id)
        log.info("order #%s -> placed (swiggy %s)", order_id, swiggy_id)
        await self._announce_placed(order_id, tapper, swiggy_id, q.to_pay_paise, method)

    # ---- steps ----

    def _stale_reason(self, proposal: list[sqlite3.Row]) -> str | None:
        for l in proposal:
            if l["status"] != "queued":
                return f"{l['name']} is no longer in the queue."
            if l["qty"] != l["qty_at_proposal"]:
                return f"The quantity of {l['name']} changed."
        return None

    async def _checkout_or_verify(self, order: sqlite3.Row, proposal: list[sqlite3.Row], tapper: CallbackTap,
                                  q: cart.Quote, method: str) -> Outcome:
        """Checkout runs at most once. Anything but a clean answer is verified against get_orders."""
        pending, swiggy_id = None, None
        try:
            try:
                async with asyncio.timeout(self.checkout_timeout_s):
                    if method == "upi":
                        result = await cart.checkout_upi(self.swiggy, self.address_id, self.upi_app)
                    else:
                        result = await cart.checkout_cod(self.swiggy, self.address_id)
            except TimeoutError:
                # The call may still be running on Swiggy's side. Drop the session (the next call
                # opens a fresh one), then verify like any other doubtful answer. Never retried.
                await self._drop_session(order["id"])
                raise TimeoutError(f"checkout took longer than {self.checkout_timeout_s:g} s") from None
            if method == "upi":
                pending = _pending_payment(result)
            else:
                swiggy_id = cart.confirmed_order_id(result)
            if not (pending or swiggy_id):
                # Only the status is quoted: a PENDING_PAYMENT answer carries payment links.
                raise _Unconfirmed(f"unexpected checkout answer: status {cart.status_of(result) or '?'}")
        except Exception as e:  # tool error, network, timeout, logged out, odd answer: never retry
            # Swiggy's own error text says why (e.g. not permitted for this account); keep it.
            self.checkout_error = f"{type(e).__name__}: {str(e)[:300]}" if str(e) else type(e).__name__
            refused = isinstance(e, SwiggyToolError) and any(m in str(e).casefold() for m in REFUSAL_MARKERS)
            reason = str(e)[:300]
            log.warning("order #%s: checkout did not confirm (%s), verifying", order["id"], self.checkout_error)
        else:  # outside the handler above: nothing in the payment flow may be mistaken for a checkout error
            if pending:
                return await self._pay_upi(order, proposal, tapper, q, pending)
            return Outcome("placed", swiggy_id)
        try:
            swiggy_id = await self._verify(order, proposal)
        except Exception as e:  # including LoggedOut: we can't tell, so a human must
            log.warning("order #%s: verify failed (%s)", order["id"], type(e).__name__)
            return Outcome("unknown")
        if swiggy_id:
            return Outcome("placed", swiggy_id)
        if refused:
            return Outcome("failed", text=f"Not ordered. Swiggy said: {reason}")
        return Outcome("unknown")

    async def _drop_session(self, order_id: int) -> None:
        try:
            await self.swiggy.reset_session()
        except Exception:
            log.warning("order #%s: could not close the Swiggy session", order_id, exc_info=True)

    # ---- UPI ----

    async def _edit_pay(self, chat_id: int, message_id: int, text: str) -> None:
        try:
            await self.outbox.edit(chat_id, message_id, text)  # no buttons: the link is gone
        except Exception:
            log.warning("could not edit the pay message %s", message_id, exc_info=True)

    async def _pay_upi(self, order: sqlite3.Row, proposal: list[sqlite3.Row], tapper: CallbackTap,
                       q: cart.Quote, pending: dict) -> Outcome:
        order_id, swiggy_id = order["id"], pending["orderId"]
        paas_id = pending["paasId"]
        window_ms = _int(pending.get("maxTimeToPollForInMs"), 60000)
        interval_s = max(_int(pending.get("pollingIntervalInMs"), 1000) / 1000, MIN_POLL_S)
        self.conn.execute("UPDATE orders SET swiggy_order_id=?, payment_json=? WHERE id=?", (
            swiggy_id, json.dumps({"orderId": swiggy_id, "paasId": paas_id,
                                   "transactionId": pending.get("transactionId"),
                                   "bridgeUrl": pending["bridgeUrl"], "windowMs": window_ms}), order_id))
        total = rupees(q.to_pay_paise)
        app = _app_name(self.upi_app)
        pay_id = await self.outbox.send(
            tapper.chat_id,
            f"{tapper.display_name}: pay {total} in the next {window_ms // 1000} s. "
            "Don't pay after the button disappears.",
            reply_to=order["card_message_id"] or tapper.message_id,
            buttons=[[UrlButton(f"Pay {total} in {app}", pending["bridgeUrl"])]])
        log.info("order #%s: pay link sent (window %s s)", order_id, window_ms // 1000)
        tracing.event("pay_link_sent", window_s=window_ms // 1000)
        # Remembered so a restart can take the button away (DESIGN 7.4).
        payment = json.loads(self.conn.execute("SELECT payment_json FROM orders WHERE id=?", (order_id,)).fetchone()[0])
        self.conn.execute("UPDATE orders SET payment_json=? WHERE id=?",
                          (json.dumps({**payment, "payMessageId": pay_id}), order_id))
        deadline = self.clock() + window_ms / 1000 + POLL_GRACE_S
        last = "pending"
        while True:
            try:
                st = await cart.payment_status(self.swiggy, paas_id, swiggy_id)
            except Exception as e:  # keep polling: the window decides
                log.warning("order #%s: payment status check failed (%s)", order_id, type(e).__name__)
                st = {}
            status = cart.status_of(st) or last
            if status != last:
                log.info("order #%s: payment %s -> %s", order_id, last, status)
                tracing.event("payment_status", status=status)
                last = status
            if st.get("isTerminalSuccess") or status in ("success", "paid"):
                return await self._paid(order, proposal, tapper, pay_id, swiggy_id, paas_id)
            if st.get("isTerminalFailure") or status in ("failed", "failure"):
                text = "Payment failed; nothing was ordered."
                await self._edit_pay(tapper.chat_id, pay_id, text)
                return Outcome("failed", text=text, announce=False)
            if self.clock() >= deadline:
                break
            await self.sleep(interval_s)
        return await self._window_closed(order, proposal, tapper, pay_id, swiggy_id, paas_id)

    async def _paid(self, order, proposal, tapper, pay_id: int, swiggy_id: str, paas_id: str) -> Outcome:
        tracing.event("paid", order_id=order["id"])
        try:
            await cart.confirm_order(self.swiggy, swiggy_id, paas_id)
        except Exception as e:
            log.warning("order #%s: confirm_order failed (%s), verifying", order["id"], type(e).__name__)
            try:
                found = await self._verify(order, proposal)
            except Exception:
                found = None
            if not found:
                await self._edit_pay(tapper.chat_id, pay_id, "Paid, but I couldn't confirm the order. See below.")
                return Outcome("unknown", text="payment succeeded but confirm_order failed")
            swiggy_id = found
        await self._edit_pay(tapper.chat_id, pay_id, "Paid ✅")
        return Outcome("placed", swiggy_id)

    async def _window_closed(self, order, proposal, tapper, pay_id: int, swiggy_id: str, paas_id: str) -> Outcome:
        order_id = order["id"]
        log.info("order #%s: payment window closed", order_id)
        tracing.event("window_closed", order_id=order_id)
        await self._edit_pay(tapper.chat_id, pay_id, "Payment window closed. Checking with Swiggy...")  # button first
        try:
            await cart.confirm_order(self.swiggy, swiggy_id, paas_id)  # finalises the order as failed if unpaid
        except Exception as e:
            log.warning("order #%s: confirm_order after the window failed (%s)", order_id, type(e).__name__)
        try:
            found = await self._verify(order, proposal)
        except Exception as e:
            log.warning("order #%s: verify failed (%s)", order_id, type(e).__name__)
            await self._edit_pay(tapper.chat_id, pay_id, "Payment window closed. I couldn't check the result.")
            return Outcome("unknown", text="payment window closed and the result could not be checked")
        if found:
            await self._edit_pay(tapper.chat_id, pay_id, "Paid ✅")
            return Outcome("placed", found)
        text = WINDOW_CLOSED_TEXT.format(id=order_id)
        await self._edit_pay(tapper.chat_id, pay_id, text)
        return Outcome("failed", text=text, announce=False)

    def _pending_swiggy_id(self, order_id: int) -> str | None:
        row = self.conn.execute("SELECT swiggy_order_id FROM orders WHERE id=?", (order_id,)).fetchone()
        return row[0] if row else None

    async def _verify(self, order: sqlite3.Row, proposal: list[sqlite3.Row]) -> str | None:
        """Find our order in Swiggy's recent orders after an unclear checkout. A known Swiggy id (UPI) matches
        exactly. Otherwise an order must match on everything we know: live, created after the claim, our
        address, our total (within a rupee), our payment method and most of our item names. The list covers
        the whole Swiggy account, including orders made in the Swiggy app, so two matches are never guessed
        between: that returns None and a human decides with /resolve."""
        claimed = datetime.fromisoformat(order["claimed_at"])
        ours = {l["name"].casefold() for l in proposal}
        known = order["swiggy_order_id"] or self._pending_swiggy_id(order["id"])
        bill = json.loads(order["bill_json"] or "{}")
        expected = bill.get("checkout_to_pay", bill.get("to_pay"))
        upi = order["payment_method"] == "upi"
        matches = []
        for recent in await cart.recent_orders(self.swiggy, 5):
            if not recent.live:  # a failed, cancelled or unpaid order was not placed
                continue
            if known and recent.order_id == known:  # UPI: we know the exact id
                return recent.order_id
            if recent.created_at < claimed - VERIFY_SLACK:
                continue
            if not cart.same_address(recent.address_id, self.address_id):
                continue
            if expected is None or recent.total_paise is None or abs(recent.total_paise - expected) > TOTAL_SLACK_PAISE:
                continue
            if recent.is_upi != upi:
                continue
            if _overlap(ours, recent.item_names):
                matches.append(recent.order_id)
        if len(matches) > 1:
            log.warning("order #%s: %d Swiggy orders match, not guessing", order["id"], len(matches))
            tracing.event("verify_ambiguous", order_id=order["id"], matches=len(matches))
            return None
        return matches[0] if matches else None

    # ---- restart recovery (DESIGN 7.4) ----

    async def _say(self, chat_id: int, message_id: int | None, text: str) -> None:
        if message_id:
            try:
                await self.outbox.edit(chat_id, message_id, text)
            except Exception:
                log.warning("could not edit message %s", message_id, exc_info=True)

    @tracing.traced("order.recover_unknown", attrs=lambda self, order_id, chat_id, logged_in: {
        "cartmates.order_id": order_id, "session.id": str(chat_id), "cartmates.logged_in": logged_in})
    async def recover_unknown(self, order_id: int, chat_id: int, logged_in: bool) -> None:
        """An order that was checking out when the bot stopped: take the Pay button away, then ask Swiggy
        (same rules as verify) unless logged out. A match is recorded as placed; otherwise a human decides."""
        order = self.conn.execute("SELECT * FROM orders WHERE id=? AND status='unknown'", (order_id,)).fetchone()
        if not order:
            return
        try:
            pay_id = json.loads(order["payment_json"] or "{}").get("payMessageId")
        except ValueError:
            pay_id = None
        await self._say(chat_id, pay_id, "Payment interrupted by a restart; don't pay now.")
        found = None
        if logged_in:
            try:
                found = await self._verify(order, orders.proposal_lines(self.conn, order_id))
            except Exception as e:  # logged out, unreachable, odd answer: a human must
                log.warning("order #%s: recovery check failed (%s)", order_id, type(e).__name__)
        if not found:
            await self._say(chat_id, order["card_message_id"], f"Order #{order_id}: unknown, see below.")
            await self.outbox.send(
                chat_id, f"Order #{order_id} was interrupted by a restart during checkout. Check the Swiggy app, "
                         f"then /resolve {order_id} placed|failed.")
            return
        unit_prices = json.loads(order["bill_json"] or "{}").get("unit_prices") or {}
        orders.record_placed(self.conn, order_id, order["orderer"], found, unit_prices)
        log.info("order #%s -> placed after restart (swiggy %s)", order_id, found)
        tracing.event("placed_after_restart", order_id=order_id, swiggy_order_id=found)
        placed = self.conn.execute("SELECT * FROM orders WHERE id=?", (order_id,)).fetchone()
        to_pay = json.loads(placed["bill_json"] or "{}").get("to_pay") or 0
        how = "paid by UPI" if placed["payment_method"] == "upi" else "cash on delivery"
        text = f"✅ Ordered by {name_of(self.conn, order['orderer'])}: Swiggy #{found}, {rupees(to_pay)} {how}"
        owes = orders.owes_text(self.conn, order_id)
        text += f"\n{owes}" if owes else ""
        await self._say(chat_id, order["card_message_id"], text)
        await self.outbox.send(chat_id, f"Order #{order_id} did go through before the restart.\n{text}")

    @tracing.traced("order.recover_failed", attrs=lambda self, order_id, chat_id: {
        "cartmates.order_id": order_id, "session.id": str(chat_id)})
    async def recover_failed(self, order_id: int, chat_id: int) -> None:
        """An order that never reached checkout: nothing was ordered and its lines are still queued."""
        order = self.conn.execute("SELECT card_message_id FROM orders WHERE id=?", (order_id,)).fetchone()
        text = f"Order #{order_id} was interrupted before checkout; nothing was ordered. Run /order again."
        await self._say(chat_id, order["card_message_id"] if order else None, text)
        await self.outbox.send(chat_id, text)

    # ---- outcomes ----

    async def _reproposal(self, order: sqlite3.Row, tapper: CallbackTap, reason: str) -> None:
        order_id = order["id"]
        self._move(order_id, "superseded", reason)
        await self._card(order, f"Order #{order_id} not placed: {reason} Fresh bill below.", tapper)
        try:
            await orders.propose(self.conn, self.outbox, self.swiggy, self.address_id, tapper.chat_id,
                                 order["proposed_by"], order["scope"], note=f"Updated: {reason}")
        except OrderError as e:
            await self.outbox.send(tapper.chat_id, f"Nothing was ordered. {e}")
        except Exception as e:  # Swiggy trouble while re-quoting; the order itself is already closed
            log.warning("order #%s: could not re-propose (%s)", order_id, type(e).__name__)
            await self.outbox.send(tapper.chat_id, f"Nothing was ordered, and I couldn't get a fresh bill "
                                                   f"({type(e).__name__}). Run /order again.")

    async def _failed(self, order: sqlite3.Row, tapper: CallbackTap, why: str, announce: bool = True) -> None:
        """Nothing was ordered and we know it: lines stay queued and new orders are not blocked."""
        order_id = order["id"]
        self._move(order_id, "failed", why)
        await self._card(order, f"Order #{order_id}: {why}", tapper)
        if announce:
            await self.outbox.send(tapper.chat_id, f"Order #{order_id}: {why} Its items are still in the queue.")

    async def _unknown(self, order: sqlite3.Row, tapper: CallbackTap, why: str = "checkout did not confirm") -> None:
        order_id = order["id"]
        self._move(order_id, "unknown", why)
        text = (f"Order #{order_id} may or may not have gone through ({why}). Check the Swiggy app, then run "
                f"/resolve {order_id} placed or /resolve {order_id} failed. New orders are blocked until then.")
        await self._card(order, f"Order #{order_id}: unknown, see below.", tapper)
        await self.outbox.send(tapper.chat_id, text)

    async def _announce_placed(self, order_id: int, tapper: CallbackTap, swiggy_id: str, to_pay: int,
                                method: str = "cod") -> None:
        order = self.conn.execute("SELECT * FROM orders WHERE id=?", (order_id,)).fetchone()
        text = f"✅ Ordered by {tapper.display_name}: Swiggy #{swiggy_id}, {rupees(to_pay)} " + ("paid by UPI" if method == "upi" else "cash on delivery")
        owes = orders.owes_text(self.conn, order_id)
        await self._card(order, text + (f"\n{owes}" if owes else ""), tapper)

    async def _settle_after_crash(self, order: sqlite3.Row, tapper: CallbackTap, error: Exception) -> None:
        """Called when _run blew up. Before checkout nothing was ordered; at/after checkout we can't tell."""
        order_id, status = order["id"], self._status(order["id"])
        if status == "checking_out":
            await self._unknown(order, tapper, f"internal error: {type(error).__name__}")
        elif status in orders.IN_FLIGHT:
            self._move(order_id, "failed", f"{type(error).__name__} before checkout")
            msg = f"Order #{order_id} was not placed ({type(error).__name__}); nothing was ordered. Try /order again."
            await self._card(order, msg, tapper)
            await self.outbox.send(tapper.chat_id, msg)


def _overlap(ours: set[str], theirs: list[str]) -> bool:
    """At least half of our item names appear (either way round, ignoring case) in their order."""
    theirs = [t.casefold() for t in theirs]
    hits = sum(1 for n in ours if any(n in t or t in n for t in theirs if t))
    return bool(ours) and hits * 2 >= len(ours)


def _int(value, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _pending_payment(result) -> dict | None:
    """The checkout answer if it is a usable PENDING_PAYMENT one."""
    if (isinstance(result, dict) and cart.status_of(result) == "pending_payment"
            and result.get("orderId") and result.get("paasId") and result.get("bridgeUrl")):
        return {**result, "orderId": str(result["orderId"]), "paasId": str(result["paasId"])}
    return None


def _app_name(upi_app: str) -> str:
    scheme = upi_app.split(":")[0].casefold()
    return UPI_NAMES.get(scheme, scheme.capitalize() or "UPI")
