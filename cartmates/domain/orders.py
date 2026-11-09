"""Order proposals, shares and history: the plain-code side of ordering (DESIGN 7.1).

A proposal points at queue lines and never moves them. Writes open their own
`db.transaction`, so callers must not wrap these in another one.
"""
import json
import logging
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone

from cartmates.agent import memory
from cartmates.domain import pending
from cartmates.swiggy.names import short_name
from cartmates.domain.queue import QueueError, item_label, log_event, name_of, now_iso, owner_names, rupees
from cartmates.swiggy.names import short_name
from cartmates.domain.queue import _bump_version as bump_version
from cartmates.store import db
from cartmates.swiggy import cart
from cartmates.swiggy.errors import SwiggyToolError
from cartmates.swiggy.client import SwiggyClient
from cartmates.telegram.outbox import Buttons, Outbox

log = logging.getLogger(__name__)

IN_FLIGHT = ("claimed", "quoting", "checking_out")
HISTORY_LIMIT = 10
UNAVAILABLE = "Swiggy isn't reachable right now, try again in a bit."


class OrderError(QueueError):
    """An ordering rule was broken. Written for the model and fine to show people."""


# ---- rules ----

def unknown_order(conn: sqlite3.Connection) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM orders WHERE status='unknown' ORDER BY id LIMIT 1").fetchone()


def open_proposal(conn: sqlite3.Connection) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM orders WHERE status='proposed' ORDER BY id DESC LIMIT 1").fetchone()


def check_can_propose(conn: sqlite3.Connection) -> None:
    unknown = unknown_order(conn)
    if unknown:
        raise OrderError(f"Order #{unknown['id']} may or may not have gone through. Check the Swiggy app, "
                         f"then run /resolve {unknown['id']} placed|failed first.")
    marks = ",".join("?" * len(IN_FLIGHT))
    busy = conn.execute(f"SELECT id FROM orders WHERE status IN ({marks})", IN_FLIGHT).fetchone()
    if busy:
        raise OrderError(f"Order #{busy['id']} is being placed right now; wait for it to finish.")


def scope_lines(conn: sqlite3.Connection, scope: str, user_id: int) -> list[sqlite3.Row]:
    """Queued lines for the scope: everything, or the lines the user owns (shared ones included)."""
    if scope == "mine":
        rows = conn.execute(
            "SELECT l.* FROM queue_lines l JOIN queue_line_owners o ON o.line_id = l.id "
            "WHERE l.status='queued' AND o.user_id=? ORDER BY l.id", (user_id,)).fetchall()
    else:
        rows = conn.execute("SELECT * FROM queue_lines WHERE status='queued' ORDER BY slot").fetchall()
    if not rows:
        raise OrderError("You have nothing in the queue to order." if scope == "mine" else "The queue is empty.")
    return rows


def _estimate(lines: list[sqlite3.Row]) -> int:
    return sum(l["qty"] * (l["est_unit_price_paise"] or 0) for l in lines)


def _cart_lines(lines) -> list[tuple[str, str, int]]:
    return [(l["spin_id"], l["sku_id"], l["qty"]) for l in lines]


def _owners(conn: sqlite3.Connection, line_id: int) -> list[int]:
    return [r[0] for r in conn.execute(
        "SELECT user_id FROM queue_line_owners WHERE line_id=? ORDER BY user_id", (line_id,))]


def _label(l: sqlite3.Row, qty: int | None = None) -> str:
    return item_label(l["name"], l["variant"], l["qty"] if qty is None else qty)


def queue_has_many_owners(conn: sqlite3.Connection) -> bool:
    """True when the queued lines belong to more than one person."""
    return conn.execute("SELECT count(DISTINCT o.user_id) FROM queue_line_owners o "
                        "JOIN queue_lines l ON l.id = o.line_id WHERE l.status='queued'").fetchone()[0] > 1


# ---- scope question ----

async def ask_scope(conn: sqlite3.Connection, outbox: Outbox, chat_id: int, reply_to: int | None,
                    user_id: int) -> str:
    """Two buttons with estimates from stored prices. Returns the question text that was sent."""
    check_can_propose(conn)
    everything, mine = scope_lines(conn, "all", user_id), scope_lines(conn, "mine", user_id)
    with db.transaction(conn):
        pending_id = pending.create_scope(conn, user_id, {})
    question = "Order just your items, or the whole queue?"
    buttons: Buttons = [[(f"Just yours ({rupees(_estimate(mine))})", f"s:{pending_id}:m"),
                         (f"Whole queue ({rupees(_estimate(everything))})", f"s:{pending_id}:a")]]
    try:
        await outbox.send(chat_id, question, reply_to=reply_to, buttons=buttons)
    except Exception:  # the buttons never posted, so the row could never be tapped
        pending.delete(conn, pending_id)
        raise
    return question


# ---- the proposal ----

def _card_text(conn: sqlite3.Connection, order_id: int, scope: str, proposer: int, lines: list[sqlite3.Row],
               q: cart.Quote, dropped: list[sqlite3.Row], note: str) -> str:
    who = "the whole queue" if scope == "all" else f"{name_of(conn, proposer)}'s items"
    out = ([note] if note else []) + [f"Order #{order_id}: {who}"]
    for l in lines:
        unit = q.spins[l["spin_id"]].unit_paise
        out.append(f"- {_label(l)}, {owner_names(conn, l['id'])}: {rupees(unit * l['qty'])}")
    if dropped:
        out.append("Not available now (still queued): " + ", ".join(_label(l) for l in dropped))
    out += [f"{label}: {text}" for label, text in q.bill_lines]
    out.append(f"To Pay: {rupees(q.to_pay_paise)}")
    out.append("Payment: pay by UPI now, or cash on delivery")
    return "\n".join(out)


async def _quote(swiggy: SwiggyClient, address_id: str, lines) -> cart.Quote:
    try:
        return await cart.quote(swiggy, address_id, _cart_lines(lines))
    except cart.CartError as e:
        raise OrderError(str(e)) from None


@dataclass
class Quoted:
    """A live quote for a scope, before any orders row exists."""
    scope: str
    address_id: str
    lines: list[sqlite3.Row]
    dropped: list[sqlite3.Row]
    quote: cart.Quote


def delivery_fee_text(q: cart.Quote) -> str | None:
    """The value of the bill line whose label contains 'Delivery' (None if there is none)."""
    return next((text for label, text in q.bill_lines if "delivery" in label.casefold()), None)


def delivery_is_free(q: cart.Quote) -> bool:
    fee = delivery_fee_text(q)
    return fee is not None and fee.strip().upper() == "FREE"


async def quote_scope(conn: sqlite3.Connection, swiggy: SwiggyClient, address_id: str | None, user_id: int,
                      scope: str) -> Quoted:
    """Check the rules and get a live quote. Writes nothing. Raises OrderError for refusals."""
    if not (swiggy and address_id):
        raise OrderError("Swiggy isn't set up (no pinned address), so I can't order.")
    check_can_propose(conn)
    lines = scope_lines(conn, scope, user_id)
    q = await _quote(swiggy, address_id, lines)
    dropped: list[sqlite3.Row] = []
    if q.unavailable:  # leave those lines queued and quote again so the bill is exact
        dropped = [l for l in lines if l["spin_id"] in q.unavailable]
        lines = [l for l in lines if l["spin_id"] not in q.unavailable]
        if not lines:
            raise OrderError("None of those items are available right now: " + ", ".join(_label(l) for l in dropped))
        q = await _quote(swiggy, address_id, lines)
        if q.unavailable:
            raise OrderError("Stock changed while I was quoting; try the order again.")
    return Quoted(scope, address_id, lines, dropped, q)


async def propose(conn: sqlite3.Connection, outbox: Outbox, swiggy: SwiggyClient, address_id: str | None,
                  chat_id: int, user_id: int, scope: str, reply_to: int | None = None, note: str = "") -> int:
    """Quote the scope's lines, post the bill card and return the new order id.
    Raises OrderError for refusals; Swiggy errors propagate."""
    quoted = await quote_scope(conn, swiggy, address_id, user_id, scope)
    return await create_proposal(conn, outbox, swiggy, chat_id, user_id, quoted, reply_to, note)


async def create_proposal(conn: sqlite3.Connection, outbox: Outbox, swiggy: SwiggyClient, chat_id: int,
                          user_id: int, quoted: Quoted, reply_to: int | None = None, note: str = "") -> int:
    """Store the quote as a proposed order (superseding older ones) and post its card."""
    scope, address_id, lines, dropped, q = (quoted.scope, quoted.address_id, quoted.lines, quoted.dropped,
                                            quoted.quote)
    cod = await cart.cod_available(swiggy)
    bill = {"to_pay": q.to_pay_paise, "item_total": q.item_total_paise, "bill": q.bill_lines,
            "unit_prices": {s: sq.unit_paise for s, sq in q.spins.items()}, "price_basis": q.price_basis,
            "cod": cod, "dropped": [l["id"] for l in dropped]}
    old_cards: list[int] = []
    with db.transaction(conn):
        for old in conn.execute("SELECT id, card_message_id FROM orders WHERE status='proposed'"):
            old_cards.append(old["card_message_id"])
        conn.execute("UPDATE orders SET status='superseded' WHERE status='proposed'")
        version = int(conn.execute("SELECT value FROM kv WHERE key='queue_version'").fetchone()[0])
        order_id = conn.execute(
            "INSERT INTO orders (status, scope, proposed_by, proposed_at, queue_version, bill_json, address_id) "
            "VALUES ('proposed', ?, ?, ?, ?, ?, ?)",
            (scope, user_id, now_iso(), version, json.dumps(bill), address_id)).lastrowid
        conn.executemany(
            "INSERT INTO order_proposal_lines (order_id, line_id, qty_at_proposal, price_at_proposal_paise) "
            "VALUES (?, ?, ?, ?)",
            [(order_id, l["id"], l["qty"], q.spins[l["spin_id"]].unit_paise) for l in lines])
        log_event(conn, user_id, "order_proposed", {"order_id": order_id, "scope": scope,
                                                    "to_pay": q.to_pay_paise, "cod": cod})
    for card in old_cards:
        if card:
            await _try_edit(outbox, chat_id, card, "This proposal was replaced by a newer one.")
    text = _card_text(conn, order_id, scope, user_id, lines, q, dropped, note)
    # Swiggy's cod.available flag proved unreliable, so Cash is always offered; a refusal comes back at checkout.
    buttons: Buttons = [[(f"Pay by UPI {rupees(q.to_pay_paise)}", f"o:{order_id}:u"),
                         (f"Cash {rupees(q.to_pay_paise)}", f"o:{order_id}:c")],
                        [("Cancel", f"o:{order_id}:x")]]
    try:
        message_id = await outbox.send(chat_id, text, reply_to=reply_to, buttons=buttons)
    except Exception:  # no card, so nobody can tap it: cancel the proposal so it can't be claimed or block a new one
        conn.execute("UPDATE orders SET status='cancelled' WHERE id=? AND status='proposed'", (order_id,))
        log_event(conn, user_id, "order_cancelled", {"order_id": order_id, "reason": "card not sent"})
        raise
    conn.execute("UPDATE orders SET card_message_id=? WHERE id=?", (message_id, order_id))
    return order_id


async def _try_edit(outbox: Outbox, chat_id: int, message_id: int, text: str) -> None:
    try:
        await outbox.edit(chat_id, message_id, text)
    except Exception:
        log.warning("could not edit message %s", message_id, exc_info=True)


def remember_outcome(conn: sqlite3.Connection, text: str) -> None:
    """Every order outcome (placed, failed, unknown, cancelled, resolved) reaches chat memory through here."""
    memory.remember(conn, "action", text, datetime.now(timezone.utc))


def cancel(conn: sqlite3.Connection, order_id: int, user_id: int) -> bool:
    """Only a proposed order can be cancelled. False means someone already acted on it."""
    cur = conn.execute("UPDATE orders SET status='cancelled' WHERE id=? AND status='proposed'", (order_id,))
    if cur.rowcount:
        log_event(conn, user_id, "order_cancelled", {"order_id": order_id})
        remember_outcome(conn, f"order #{order_id} cancelled by {name_of(conn, user_id)}")
    return bool(cur.rowcount)


# ---- shares and recording ----

def split_equally(price_paise: int, owner_ids: list[int]) -> dict[int, int]:
    """Equal split; the leftover paise go one each to the owners with the lowest user ids."""
    owners = sorted(owner_ids)
    base, extra = divmod(price_paise, len(owners))
    return {uid: base + (1 if i < extra else 0) for i, uid in enumerate(owners)}


def proposal_lines(conn: sqlite3.Connection, order_id: int) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT p.line_id, p.qty_at_proposal, p.price_at_proposal_paise, l.* FROM order_proposal_lines p "
        "JOIN queue_lines l ON l.id = p.line_id WHERE p.order_id=? ORDER BY l.id", (order_id,)).fetchall()


def record_placed(conn: sqlite3.Connection, order_id: int, orderer: int, swiggy_order_id: str | None,
                  unit_prices: dict[str, int] | None = None, final_to_pay: int | None = None) -> dict[int, int]:
    """One transaction: order placed, an item per queue line, shares, lines ordered, version bumped.
    unit_prices (by spin_id) are the latest quote; the proposal's prices fill any gap.
    Returns {user_id: amount} for everyone including the orderer."""
    unit_prices = unit_prices or {}
    with db.transaction(conn):
        order = conn.execute("SELECT * FROM orders WHERE id=?", (order_id,)).fetchone()
        if not order or order["status"] not in ("checking_out", "unknown", "failed"):
            raise OrderError(f"Order #{order_id} can't be recorded as placed from its current state.")
        bill = json.loads(order["bill_json"] or "{}")
        if final_to_pay is not None:
            bill["to_pay"] = final_to_pay
        conn.execute("UPDATE orders SET status='placed', orderer=?, swiggy_order_id=COALESCE(?, swiggy_order_id), placed_at=?, bill_json=? WHERE id=?",
                     (orderer, swiggy_order_id, now_iso(), json.dumps(bill), order_id))
        shares: dict[int, int] = {}
        for l in proposal_lines(conn, order_id):
            unit = unit_prices.get(l["spin_id"], l["price_at_proposal_paise"] or 0)
            price = unit * l["qty_at_proposal"]
            item_id = conn.execute(
                "INSERT INTO order_items (order_id, name, variant, qty, line_price_paise, spin_id, sku_id) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (order_id, l["name"], l["variant"], l["qty_at_proposal"], price, l["spin_id"], l["sku_id"])).lastrowid
            owners = _owners(conn, l["line_id"]) or [l["added_by"]]
            conn.executemany("INSERT INTO order_item_owners (order_item_id, user_id) VALUES (?, ?)",
                             [(item_id, uid) for uid in owners])
            for uid, amount in split_equally(price, owners).items():
                shares[uid] = shares.get(uid, 0) + amount
            conn.execute("UPDATE queue_lines SET status='ordered', slot=NULL, order_id=? WHERE id=?", (order_id, l["line_id"]))
        conn.executemany("INSERT INTO order_shares (order_id, user_id, amount_paise, owed_to) VALUES (?, ?, ?, ?)",
                         [(order_id, uid, amount, orderer) for uid, amount in sorted(shares.items())])
        bump_version(conn)
        log_event(conn, orderer, "order_placed", {"order_id": order_id, "swiggy_order_id": swiggy_order_id})
        placed = conn.execute("SELECT swiggy_order_id, payment_method FROM orders WHERE id=?", (order_id,)).fetchone()
        method = {"upi": "UPI", "cod": "COD"}.get(placed["payment_method"] or "", "method unknown")
        remember_outcome(conn, f"order #{order_id} placed by {name_of(conn, orderer)}: Swiggy "
                               f"#{placed['swiggy_order_id'] or '?'}, {rupees(bill.get('to_pay') or 0)} {method}")
    return shares


def owes_text(conn: sqlite3.Connection, order_id: int) -> str:
    """'Owes Name: Priya ₹120, Rohan ₹80' (the orderer's own share is left out)."""
    rows = conn.execute("SELECT user_id, amount_paise, owed_to FROM order_shares "
                        "WHERE order_id=? AND user_id != owed_to ORDER BY user_id", (order_id,)).fetchall()
    if not rows:
        return ""
    return f"Owes {name_of(conn, rows[0]['owed_to'])}: " + ", ".join(
        f"{name_of(conn, r['user_id'])} {rupees(r['amount_paise'])}" for r in rows)


def trace_ids(conn: sqlite3.Connection, order_id: int, limit: int = 3) -> list[str]:
    """Trace ids of the latest events that mention this order (newest first)."""
    rows = conn.execute(
        "SELECT trace_id FROM events WHERE trace_id IS NOT NULL AND json_extract(payload_json, '$.order_id') = ? "
        "GROUP BY trace_id ORDER BY max(id) DESC LIMIT ?", (order_id, limit)).fetchall()
    return [r[0] for r in rows]


def resolve(conn: sqlite3.Connection, order_id: int, outcome: str, user_id: int) -> str:
    """After a human checked the Swiggy app: an unknown order becomes placed or failed."""
    order = conn.execute("SELECT * FROM orders WHERE id=?", (order_id,)).fetchone()
    late_upi = bool(order and order["status"] == "failed" and order["payment_method"] == "upi")
    if not order or (order["status"] != "unknown" and not late_upi):
        raise OrderError(f"Order #{order_id} isn't waiting to be resolved.")
    if late_upi and outcome == "failed":
        raise OrderError(f"Order #{order_id} is already marked failed.")
    if outcome == "failed":
        conn.execute("UPDATE orders SET status='failed', error=? WHERE id=?",
                     (f"resolved as failed by {name_of(conn, user_id)}", order_id))
        log_event(conn, user_id, "order_resolved", {"order_id": order_id, "outcome": "failed"})
        remember_outcome(conn, f"order #{order_id} resolved as failed by {name_of(conn, user_id)}")
        return f"Order #{order_id} marked failed. Its items are still in the queue."
    if late_upi and any(l["status"] != "queued" for l in proposal_lines(conn, order_id)):
        raise OrderError(f"Order #{order_id}'s items have changed since, so it can't be recorded as placed.")
    orderer = order["orderer"] or user_id
    # The order sits in 'unknown' (or 'failed' for a late UPI payment); record_placed accepts both.
    record_placed(conn, order_id, orderer, None)
    log_event(conn, user_id, "order_resolved", {"order_id": order_id, "outcome": "placed"})
    remember_outcome(conn, f"order #{order_id} resolved as placed by {name_of(conn, user_id)}")
    owes = owes_text(conn, order_id)
    return f"Order #{order_id} recorded as placed (ordered by {name_of(conn, orderer)})." + (f"\n{owes}" if owes else "")


# ---- history and status ----

def history_list(conn: sqlite3.Connection, limit: int = HISTORY_LIMIT) -> str:
    rows = conn.execute("SELECT * FROM orders WHERE status != 'superseded' ORDER BY id DESC LIMIT ?",
                        (limit,)).fetchall()
    if not rows:
        return "No orders yet."
    out = ["Recent orders:"]
    for o in rows:
        total = json.loads(o["bill_json"] or "{}").get("to_pay")
        who = name_of(conn, o["orderer"] or o["proposed_by"])
        out.append(f"#{o['id']} {(o['placed_at'] or o['proposed_at'])[:10]} {who} "
                   f"{rupees(total) if total is not None else '-'} {o['status']}")
    return "\n".join(out)


def history_detail(conn: sqlite3.Connection, order_id: int) -> str:
    o = conn.execute("SELECT * FROM orders WHERE id=?", (order_id,)).fetchone()
    if not o:
        raise OrderError(f"There is no order #{order_id}.")
    total = json.loads(o["bill_json"] or "{}").get("to_pay")
    head = f"Order #{o['id']}: {o['status']}"
    if o["swiggy_order_id"]:
        head += f", Swiggy #{o['swiggy_order_id']}"
    if o["orderer"]:
        head += f", ordered by {name_of(conn, o['orderer'])}"
    if total is not None:
        head += f", To Pay {rupees(total)}"
    out = [head]
    items = conn.execute("SELECT * FROM order_items WHERE order_id=? ORDER BY id", (order_id,)).fetchall()
    for it in items:
        owners = ", ".join(name_of(conn, r[0]) for r in conn.execute(
            "SELECT user_id FROM order_item_owners WHERE order_item_id=? ORDER BY user_id", (it["id"],)))
        variant = f" {it['variant']}" if it["variant"] else ""
        out.append(f"- {short_name(it['name'])}{variant} x{it['qty']}, {owners}: {rupees(it['line_price_paise'])}")
    if not items:  # never placed: show what the proposal covered
        for l in proposal_lines(conn, order_id):
            out.append(f"- {_label(l, l['qty_at_proposal'])}, {owner_names(conn, l['line_id'])}")
    owes = owes_text(conn, order_id)
    if owes:
        out.append(owes)
    if o["error"]:
        out.append(f"Note: {o['error']}")
    return "\n".join(out)


# ---- tracking (the /track command and the track_order tool share this) ----

def delivery_text(swiggy_id: str, data: dict) -> str:
    """One line for get_delivery_status: ETA text plus clock time in IST, Delivered, or Cancelled."""
    head = f"Swiggy #{swiggy_id}: "
    if data.get("cancelled"):
        return head + "Cancelled"
    if data.get("delivered"):
        return head + "Delivered ✅"
    by, now = data.get("deliveryBy"), data.get("serverNow")
    if not isinstance(by, (int, float)) or isinstance(by, bool):
        return head + "on its way (no ETA yet)"
    clock = datetime.fromtimestamp(by / 1000, memory.IST).strftime("%H:%M")
    eta = data.get("etaText")
    if not eta and isinstance(now, (int, float)):
        eta = f"{max(1, round((by - now) / 60000))} mins"
    return head + (f"arriving in ~{eta} " if eta else "arriving ") + f"(by {clock} IST)"


async def track(conn: sqlite3.Connection, swiggy: SwiggyClient | None, fallback_address: str | None,
                order_id: int | None) -> str:
    """Live status of an order (default: the latest placed one). Read-only; answers in plain text."""
    if order_id is not None:
        order = conn.execute("SELECT * FROM orders WHERE id=?", (order_id,)).fetchone()
        if not order:
            return f"There is no order #{order_id}."
    else:
        order = conn.execute("SELECT * FROM orders WHERE status='placed' ORDER BY id DESC LIMIT 1").fetchone()
        if not order:
            if conn.execute("SELECT 1 FROM orders WHERE status != 'superseded' LIMIT 1").fetchone():
                return "No order has been placed yet, so there's nothing to track."
            return "No orders yet."
    if order["status"] != "placed":
        return f"Order #{order['id']} wasn't placed (status: {order['status']}), so there's nothing to track."
    if not order["swiggy_order_id"]:
        return f"Order #{order['id']} has no Swiggy order id on record, so I can't track it."
    if not swiggy:
        return "Swiggy isn't set up."
    where = order["address_id"] or fallback_address
    if not where:
        return "No delivery address is known for that order."
    try:
        data = await cart.delivery_status(swiggy, order["swiggy_order_id"], where)
    except SwiggyToolError:
        return UNAVAILABLE
    return delivery_text(order["swiggy_order_id"], data)
