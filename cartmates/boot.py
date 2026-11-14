"""The boot pass (DESIGN 17.2): open each active household, migrate it and settle what the last
restart interrupted. A household that fails is marked broken; the others still start."""
import logging
import sqlite3
from dataclasses import dataclass, field

from cartmates import registry, tracing
from cartmates.config import Config
from cartmates.domain import orders, pending
from cartmates.store import db

log = logging.getLogger("cartmates")

# An order in one of these states was mid-flight when the process died.
IN_FLIGHT = ("claimed", "quoting", "checking_out")

BEFORE_CHECKOUT = "interrupted before checkout, nothing was ordered"
DURING_CHECKOUT = "interrupted by restart"


@dataclass
class Recovered:
    """Orders settled at boot. The Swiggy check for `unknown` ones happens later, on the lane."""
    unknown: list[int] = field(default_factory=list)  # were checking out
    failed: list[int] = field(default_factory=list)   # never reached checkout

    def __len__(self) -> int:
        return len(self.unknown) + len(self.failed)


def recover_stuck_orders(conn: sqlite3.Connection) -> Recovered:
    """claimed / quoting never reached checkout, so nothing was ordered: failed. checking_out may have
    ordered: unknown, and a human (or the recovery check against Swiggy) must settle it."""
    out = Recovered()
    for row in conn.execute("SELECT id, status FROM orders WHERE status IN (?, ?, ?) ORDER BY id", IN_FLIGHT).fetchall():
        if row["status"] == "checking_out":
            conn.execute("UPDATE orders SET status='unknown', error=? WHERE id=?", (DURING_CHECKOUT, row["id"]))
            out.unknown.append(row["id"])
            orders.remember_outcome(conn, f"order #{row['id']} unknown: {DURING_CHECKOUT}")
        else:
            conn.execute("UPDATE orders SET status='failed', error=? WHERE id=?", (BEFORE_CHECKOUT, row["id"]))
            out.failed.append(row["id"])
            orders.remember_outcome(conn, f"order #{row['id']} failed: {BEFORE_CHECKOUT}")
    return out


@dataclass
class Started:
    household_id: int
    conn: sqlite3.Connection
    recovered: Recovered


def boot_pass(config: Config, reg: sqlite3.Connection) -> list[Started]:
    started = []
    for hid in registry.household_ids(reg):
        conn = None
        try:
            conn = db.connect(config.household_db(hid))
            applied = db.migrate(conn)
            with tracing.span("boot.recover_stuck_orders", tracing.CHAIN) as sp:  # runs before the lane exists
                recovered = recover_stuck_orders(conn)
                sp.set_attributes({"cartmates.household_id": hid, "cartmates.unknown": len(recovered.unknown),
                                   "cartmates.failed": len(recovered.failed)})
            expired = pending.delete_expired(conn)
        except Exception:
            log.exception("household %s failed to start; marking it broken", hid)
            registry.set_status(reg, hid, "broken")
            if conn is not None:
                conn.close()
            continue
        log.info("household %s: migrations applied: %s; recovered %d stuck order(s); deleted %d expired "
                 "question(s)", hid, applied or "none", len(recovered), expired)
        started.append(Started(hid, conn, recovered))
    return started
