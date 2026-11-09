"""The shared queue: every rule about lines and owners lives here, not in the prompt.

Functions that write do not open a transaction; the caller wraps them in
`db.transaction` so a line, its owners, the version bump and the event land together.
"""
import json
import sqlite3
from datetime import datetime, timezone

from queuify import observe
from queuify.swiggy.names import short_name

MAX_QTY = 20


class QueueError(Exception):
    """A rule was broken. The message is written for the model (and fine to show people)."""


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def rupees(paise: int) -> str:
    return f"₹{paise // 100}" if paise % 100 == 0 else f"₹{paise / 100:.2f}"


def log_event(conn: sqlite3.Connection, actor: int | str, kind: str, payload: dict) -> None:
    conn.execute("INSERT INTO events (ts, actor, kind, payload_json, turn_id, trace_id) VALUES (?, ?, ?, ?, ?, ?)",
                 (now_iso(), str(actor), kind, json.dumps(payload), observe.turn_id.get(),
                  observe.current_trace_id()))


def _bump_version(conn: sqlite3.Connection) -> None:
    conn.execute("UPDATE kv SET value = CAST(value AS INTEGER) + 1 WHERE key='queue_version'")


# ---- people ----

def members(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute("SELECT user_id, display_name FROM members WHERE active=1 ORDER BY user_id").fetchall()


def resolve_owners(conn: sqlite3.Connection, names: list[str], sender_id: int) -> list[int]:
    """Member names (or "me") to user ids. Empty means the sender."""
    if not names:
        return [sender_id]
    everyone = members(conn)
    ids: list[int] = []
    for raw in names:
        name = str(raw).strip().casefold()
        if name in ("me", "myself", "i"):
            found = [sender_id]
        else:
            exact = [m["user_id"] for m in everyone if m["display_name"].casefold() == name]
            # "Rohan" should match "Rohan Sharma", but only if that is unambiguous.
            first = [m["user_id"] for m in everyone if name in m["display_name"].casefold().split()]
            found = exact or first
        if not found:
            raise QueueError(
                f'unknown name "{raw}". Owners can only be people who have talked to the bot: '
                f'tell the user to ask {raw} to say hi to me once. Known members: '
                + ", ".join(m["display_name"] for m in everyone))
        if len(found) > 1:
            raise QueueError(f'"{raw}" matches more than one member; ask which one')
        if found[0] not in ids:
            ids.append(found[0])
    return ids


def name_of(conn: sqlite3.Connection, user_id: int) -> str:
    row = conn.execute("SELECT display_name FROM members WHERE user_id=?", (user_id,)).fetchone()
    return row["display_name"] if row else f"user {user_id}"


def owner_names(conn: sqlite3.Connection, line_id: int) -> str:
    rows = conn.execute(
        "SELECT m.display_name FROM queue_line_owners o JOIN members m ON m.user_id = o.user_id "
        "WHERE o.line_id=? ORDER BY m.user_id", (line_id,)).fetchall()
    return ", ".join(r[0] for r in rows) or "nobody"


# ---- lines ----

def queued_lines(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM queue_lines WHERE status='queued' ORDER BY slot").fetchall()


def queued_total_paise(conn: sqlite3.Connection) -> int:
    return conn.execute("SELECT COALESCE(SUM(qty * est_unit_price_paise), 0) FROM queue_lines "
                        "WHERE status='queued'").fetchone()[0]


def _check_qty(qty: int, max_qty: int | None) -> None:
    limit = min(MAX_QTY, max_qty) if max_qty else MAX_QTY
    if not 1 <= qty <= limit:
        raise QueueError(f"qty must be between 1 and {limit}")


def _set_owners(conn: sqlite3.Connection, line_id: int, owner_ids: list[int]) -> None:
    conn.execute("DELETE FROM queue_line_owners WHERE line_id=?", (line_id,))
    conn.executemany("INSERT INTO queue_line_owners (line_id, user_id) VALUES (?, ?)",
                     [(line_id, uid) for uid in owner_ids])


def item_label(name: str, variant: str | None, qty: int) -> str:
    """The one way an item is written for people: 'Amul Taaza (1 L) ×2' (no parentheses without a variant)."""
    name = short_name(name)
    return f"{name} ({variant}) ×{qty}" if variant else f"{name} ×{qty}"


def describe(option: dict, qty: int, owners: str) -> str:
    """'Amul Taaza (1 L) ×2 for Priya, Rohan — ₹132'."""
    return f"{item_label(option['name'], option['size'], qty)} for {owners} — {rupees(option['price'] * qty)}"


def _free_slot(conn: sqlite3.Connection) -> int:
    """The lowest positive number not used by a queued line. Call inside the insert's transaction."""
    used = {r[0] for r in conn.execute("SELECT slot FROM queue_lines WHERE status='queued'")}
    slot = 1
    while slot in used:
        slot += 1
    return slot


def add_line(conn: sqlite3.Connection, option: dict, qty: int, owner_ids: list[int],
             original_text: str, actor: int) -> tuple[int, str]:
    """Insert one new line (lines are never merged). `option` has spin, sku, name, size,
    price (paise) and max_qty. Returns (slot, description)."""
    _check_qty(qty, option["max_qty"])
    slot = _free_slot(conn)
    cur = conn.execute(
        "INSERT INTO queue_lines (spin_id, sku_id, name, variant, qty, est_unit_price_paise, "
        "max_qty, original_text, added_by, added_at, slot) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (option["spin"], option["sku"], option["name"], option["size"], qty, option["price"],
         option["max_qty"], original_text, actor, now_iso(), slot))
    line_id = cur.lastrowid
    _set_owners(conn, line_id, owner_ids)
    _bump_version(conn)
    log_event(conn, actor, "queue_add", {"line_id": line_id, "slot": slot, "qty": qty, "owners": owner_ids})
    return slot, describe(option, qty, owner_names(conn, line_id))


def _queued_line(conn: sqlite3.Connection, slot: int) -> sqlite3.Row:
    """Finds a line by its queue number, among queued lines only."""
    line = conn.execute("SELECT * FROM queue_lines WHERE status='queued' AND slot=?", (slot,)).fetchone()
    if not line:
        raise QueueError(f"there is no queued line #{slot}; check the snapshot or call list_queue")
    return line


def _label(conn: sqlite3.Connection, line: sqlite3.Row, actor: int) -> str:
    """Names the original adder when someone else edits their line: "Rohan's Eggs"."""
    if line["added_by"] == actor:
        return short_name(line["name"])
    return f"{name_of(conn, line['added_by'])}'s {short_name(line['name'])}"



def other_owners(conn: sqlite3.Connection, slot: int, actor: int) -> tuple[list[int], str]:
    """Owners of a queued line who are not the actor, and the item's name. Call before changing the
    line: they are who must be told. Empty when the actor is the only owner (or the line is missing)."""
    line = conn.execute("SELECT id, name FROM queue_lines WHERE status='queued' AND slot=?", (slot,)).fetchone()
    if not line:
        return [], ""
    owners = [r[0] for r in conn.execute(
        "SELECT user_id FROM queue_line_owners WHERE line_id=? ORDER BY user_id", (line["id"],))]
    return [o for o in owners if o != actor], short_name(line["name"])


def update_line(conn: sqlite3.Connection, slot: int, actor: int,
                qty: int | None = None, owner_ids: list[int] | None = None) -> str:
    line = _queued_line(conn, slot)
    line_id = line["id"]
    if qty is None and owner_ids is None:
        raise QueueError("nothing to change: give qty and/or owners")
    changes = []
    if qty is not None:
        _check_qty(qty, line["max_qty"])
        conn.execute("UPDATE queue_lines SET qty=? WHERE id=?", (qty, line_id))
        changes.append(f"qty {line['qty']} -> {qty}")
    if owner_ids is not None:
        _set_owners(conn, line_id, owner_ids)
        changes.append(f"owners now {owner_names(conn, line_id)}")
    _bump_version(conn)
    log_event(conn, actor, "queue_update", {"line_id": line_id, "slot": slot, "qty": qty, "owners": owner_ids})
    return f"Updated #{slot} {_label(conn, line, actor)}: {', '.join(changes)}"


def remove_line(conn: sqlite3.Connection, slot: int, actor: int) -> str:
    line = _queued_line(conn, slot)
    line_id = line["id"]
    conn.execute("UPDATE queue_lines SET status='removed', slot=NULL WHERE id=?", (line_id,))
    _bump_version(conn)
    log_event(conn, actor, "queue_remove", {"line_id": line_id, "slot": slot})
    return f"Removed #{slot} {item_label(_label(conn, line, actor), line['variant'], line['qty'])}"
