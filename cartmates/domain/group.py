"""The household group's chat id (DESIGN 7.4). Kept in kv under `group_id`; TELEGRAM_GROUP_ID in `.env`
is only the starting value. If Telegram upgrades the group to a supergroup its id changes, and the new
one is stored here. Everything that needs the id calls current() when it uses it."""
import logging
import sqlite3

log = logging.getLogger(__name__)


def current(conn: sqlite3.Connection | None, default: int | None = None) -> int | None:
    row = conn.execute("SELECT value FROM kv WHERE key='group_id'").fetchone() if conn else None
    return int(row[0]) if row else default


def set_current(conn: sqlite3.Connection, group_id: int) -> None:
    conn.execute("INSERT INTO kv (key, value) VALUES ('group_id', ?) "
                 "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (str(group_id),))


def record_migration(conn: sqlite3.Connection, new_id: int) -> None:
    """The group became a supergroup: use the new id from now on and ask the host to update .env."""
    if current(conn) == new_id:
        return
    set_current(conn, new_id)
    log.warning("the group was upgraded to a supergroup; its new chat id is %s. "
                "Update TELEGRAM_GROUP_ID in .env to %s (the stored id is used until then)", new_id, new_id)
