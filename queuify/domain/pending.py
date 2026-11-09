"""Open questions to a member: variant and scope questions. Login pendings are the auth flow's."""
import json
import sqlite3
from datetime import datetime, timedelta, timezone

from queuify.domain.queue import name_of, now_iso, rupees
from queuify.swiggy.names import button_labels

VARIANT_TTL = timedelta(minutes=30)
PAGE_SIZE = 4


def _create(conn: sqlite3.Connection, kind: str, user_id: int, payload: dict) -> int:
    """A person may have several open variant questions; each button names its own id."""
    if kind == "scope":
        # A scope tap re-quotes the live queue, so only the newest scope question is worth keeping.
        conn.execute("DELETE FROM pending WHERE kind='scope' AND user_id=?", (user_id,))
    # SQLite reuses the ids of deleted rows, and an old message's button must never hit a newer
    # question. So ids come from a counter in kv that only goes up.
    conn.execute("INSERT INTO kv (key, value) VALUES ('pending_seq', '1') "
                 "ON CONFLICT(key) DO UPDATE SET value = CAST(value AS INTEGER) + 1")
    pending_id = int(conn.execute("SELECT value FROM kv WHERE key='pending_seq'").fetchone()[0])
    conn.execute(
        "INSERT INTO pending (id, user_id, kind, payload_json, created_at, expires_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (pending_id, user_id, kind, json.dumps(payload), now_iso(),
         (datetime.now(timezone.utc) + VARIANT_TTL).isoformat()))
    return pending_id


def create_variant(conn: sqlite3.Connection, user_id: int, payload: dict) -> int:
    return _create(conn, "variant", user_id, payload)


def page_count(options: list[dict]) -> int:
    return max(1, -(-len(options) // PAGE_SIZE))


def variant_buttons(pending_id: int, options: list[dict], page: int = 0) -> list[list[tuple[str, str]]]:
    """One page of options, then "More options" (when there are several pages) and "None of these".
    Callbacks: v:<id>:<option index in the full list>, v:<id>:m<page to show>, v:<id>:x."""
    page = page if 0 <= page < page_count(options) else 0
    first = page * PAGE_SIZE
    shown = options[first:first + PAGE_SIZE]
    rows = [[(label, f"v:{pending_id}:{first + i}")] for i, label in enumerate(button_labels(shown, rupees))]
    if len(options) > PAGE_SIZE:
        rows.append([("More options ▸", f"v:{pending_id}:m{(page + 1) % page_count(options)}")])
    rows.append([("None of these", f"v:{pending_id}:x")])
    return rows


def create_scope(conn: sqlite3.Connection, user_id: int, payload: dict) -> int:
    return _create(conn, "scope", user_id, payload)


def _get(conn: sqlite3.Connection, kind: str, pending_id: int) -> dict | None:
    row = conn.execute("SELECT * FROM pending WHERE id=? AND kind=?", (pending_id, kind)).fetchone()
    if not row:
        return None
    if datetime.fromisoformat(row["expires_at"]) <= datetime.now(timezone.utc):
        _expire(conn, row)
        return None
    return {"id": row["id"], "user_id": row["user_id"], "payload": json.loads(row["payload_json"]),
            "created_at": row["created_at"]}


def get_variant(conn: sqlite3.Connection, pending_id: int) -> dict | None:
    """{id, user_id, payload}, or None if missing or expired (an expired row is deleted)."""
    return _get(conn, "variant", pending_id)


def get_scope(conn: sqlite3.Connection, pending_id: int) -> dict | None:
    return _get(conn, "scope", pending_id)


def for_user(conn: sqlite3.Connection, user_id: int) -> list[dict]:
    """The person's open variant questions, oldest first."""
    ids = [r["id"] for r in conn.execute(
        "SELECT id FROM pending WHERE kind='variant' AND user_id=? ORDER BY id", (user_id,))]
    return [q for q in (get_variant(conn, i) for i in ids) if q]


def set_message_id(conn: sqlite3.Connection, pending_id: int, message_id: int) -> None:
    conn.execute("UPDATE pending SET message_id=? WHERE id=?", (message_id, pending_id))


def open_variants(conn: sqlite3.Connection) -> list[dict]:
    """Every member's open variant questions, oldest first."""
    ids = [r["id"] for r in conn.execute("SELECT id FROM pending WHERE kind='variant' ORDER BY id")]
    return [q for q in (get_variant(conn, i) for i in ids) if q]


def delete(conn: sqlite3.Connection, pending_id: int) -> bool:
    """True if this call removed the row."""
    return conn.execute("DELETE FROM pending WHERE id=?", (pending_id,)).rowcount > 0


def _expire(conn: sqlite3.Connection, row: sqlite3.Row) -> bool:
    """Delete an expired row; a variant question leaves one action line (only the call that deleted it writes it)."""
    if not delete(conn, row["id"]):
        return False
    if row["kind"] == "variant":
        asker = name_of(conn, row["user_id"])
        question = (json.loads(row["payload_json"]) or {}).get("question", "?")
        conn.execute("INSERT INTO chat_memory (ts, role, user_id, text) VALUES (?, 'action', NULL, ?)",
                     (now_iso(), f'{asker}\'s question "{question}" expired'))
    return True


def delete_expired(conn: sqlite3.Connection) -> int:
    """Delete variant and scope questions past expires_at (login pendings are the auth flow's)."""
    now = datetime.now(timezone.utc)
    stale = [r for r in conn.execute("SELECT * FROM pending WHERE kind IN ('variant', 'scope')")
             if datetime.fromisoformat(r["expires_at"]) <= now]
    return sum(_expire(conn, r) for r in stale)
