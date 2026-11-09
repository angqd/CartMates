"""Free-delivery nudge (DESIGN 5.3): after a queue change, quote the whole queue and, only if Swiggy
shows delivery as FREE, post an order card. Silent in every other case; never raises."""
import logging
import sqlite3

from cartmates.domain import orders
from cartmates.domain.orders import OrderError
from cartmates.domain.queue import queued_total_paise, rupees
from cartmates.swiggy.client import SwiggyClient
from cartmates.swiggy.errors import LoggedOut
from cartmates.telegram.outbox import Outbox

log = logging.getLogger(__name__)


def _get_base(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT value FROM kv WHERE key='nudge_base'").fetchone()
    try:
        return int(row[0]) if row else 0
    except (TypeError, ValueError):
        return 0


def _set_base(conn: sqlite3.Connection, paise: int) -> None:
    conn.execute("INSERT INTO kv (key, value) VALUES ('nudge_base', ?) "
                 "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (str(paise),))


async def maybe_nudge(conn: sqlite3.Connection, outbox: Outbox, swiggy: SwiggyClient | None,
                      address_id: str | None, chat_id: int, user_id: int, free_delivery_min: int,
                      logged_in: bool = True) -> None:
    try:
        await _check(conn, outbox, swiggy, address_id, chat_id, user_id, free_delivery_min, logged_in)
    except Exception:  # a nudge must never break the turn that triggered it
        log.warning("free-delivery nudge failed", exc_info=True)


async def _check(conn, outbox, swiggy, address_id, chat_id, user_id, free_delivery_min, logged_in) -> None:
    total, base = queued_total_paise(conn), _get_base(conn)
    if total < base:  # a removal or an order: measure growth again from here
        _set_base(conn, total)
        base = total
    if total < free_delivery_min * 100 or total <= base:
        return
    if not (swiggy and address_id and logged_in):
        return
    try:
        orders.check_can_propose(conn)  # an unknown or in-flight order
    except OrderError:
        return
    if orders.open_proposal(conn):
        return
    try:
        quoted = await orders.quote_scope(conn, swiggy, address_id, user_id, "all")
    except LoggedOut:  # never start a login just to nudge
        return
    except Exception:
        _set_base(conn, total)  # this was a check; don't hammer Swiggy on every edit
        raise
    _set_base(conn, total)
    q = quoted.quote
    if not orders.delivery_is_free(q):
        log.info("free-delivery nudge: queue at %s but delivery is not free (%s)", rupees(total),
                 orders.delivery_fee_text(q) or "no delivery line in the bill")
        return
    await orders.create_proposal(conn, outbox, swiggy, chat_id, user_id, quoted,
                                 note=f"🎉 Free delivery reached ({rupees(q.item_total_paise)}). Order now?")
