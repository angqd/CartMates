"""The household's delivery address (DESIGN 7.2). Kept in kv; the env value is only the starting point.
Everything that needs the address calls current() when it uses it, so a switch applies at once."""
import sqlite3


def current(conn: sqlite3.Connection, default: str | None = None) -> str | None:
    row = conn.execute("SELECT value FROM kv WHERE key='address_id'").fetchone()
    return (row[0] if row else None) or default or None


def set_current(conn: sqlite3.Connection, address_id: str) -> None:
    conn.execute("INSERT INTO kv (key, value) VALUES ('address_id', ?) "
                 "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (address_id,))


def same(a: str, b: str) -> bool:
    # Responses sometimes give only the part before "__".
    return a.split("__")[0] == b.split("__")[0]
