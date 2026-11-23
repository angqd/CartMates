"""registry.db (DESIGN 17.2): which households exist and which chat reaches each one. Plain functions that
take the registry connection first."""
import logging
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from cartmates.store import db

log = logging.getLogger(__name__)


def open(path: Path | str) -> sqlite3.Connection:  # noqa: A001 - the name callers use
    reg = db.connect(path)
    db.migrate(reg, db.REGISTRY_MIGRATIONS_DIR)
    return reg


def create_household(reg: sqlite3.Connection, setup_by: int | None = None) -> int:
    return reg.execute("INSERT INTO households (created_at, setup_by) VALUES (?, ?)",
                       (datetime.now(timezone.utc).isoformat(), setup_by)).lastrowid


def household_ids(reg: sqlite3.Connection, status: str = "active") -> list[int]:
    return [r[0] for r in reg.execute("SELECT id FROM households WHERE status=? ORDER BY id", (status,))]


def status(reg: sqlite3.Connection, household_id: int) -> str | None:
    row = reg.execute("SELECT status FROM households WHERE id=?", (household_id,)).fetchone()
    return row[0] if row else None


def set_status(reg: sqlite3.Connection, household_id: int, status: str) -> None:
    reg.execute("UPDATE households SET status=? WHERE id=?", (status, household_id))


def map_chat(reg: sqlite3.Connection, household_id: int, chat_id: int, platform: str = "telegram") -> None:
    reg.execute("INSERT INTO chats (platform, chat_id, household_id) VALUES (?, ?, ?) "
                "ON CONFLICT(platform, chat_id) DO NOTHING", (platform, chat_id, household_id))


def chat_for(reg: sqlite3.Connection, household_id: int, platform: str = "telegram") -> int | None:
    row = reg.execute("SELECT chat_id FROM chats WHERE household_id=? AND platform=? ORDER BY rowid LIMIT 1",
                      (household_id, platform)).fetchone()
    return row[0] if row else None


def household_for(reg: sqlite3.Connection, chat_id: int, platform: str = "telegram") -> int | None:
    row = reg.execute("SELECT household_id FROM chats WHERE platform=? AND chat_id=?",
                      (platform, chat_id)).fetchone()
    return row[0] if row else None


def remap_chat(reg: sqlite3.Connection, household_id: int, new_chat_id: int, platform: str = "telegram") -> None:
    """A supergroup upgrade: the household's chat now has a new id. A repeat notice changes nothing."""
    old = chat_for(reg, household_id, platform)
    if old == new_chat_id:
        return
    with db.transaction(reg):
        reg.execute("DELETE FROM chats WHERE household_id=? AND platform=?", (household_id, platform))
        reg.execute("INSERT INTO chats (platform, chat_id, household_id) VALUES (?, ?, ?)",
                    (platform, new_chat_id, household_id))
    log.warning("household %s's chat became a supergroup: chat id %s -> %s", household_id, old, new_chat_id)
