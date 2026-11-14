import sqlite3
from pathlib import Path

import pytest

from cartmates.config import Config
from cartmates.store.db import MIGRATIONS_DIR, REGISTRY_MIGRATIONS_DIR, connect, migrate


def _tables(conn):
    return {r["name"] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def test_registry_migration_creates_tables_and_is_idempotent():
    conn = connect(":memory:")
    assert migrate(conn, REGISTRY_MIGRATIONS_DIR) == ["001_registry"]
    assert {"households", "chats", "invites", "login_links"} <= _tables(conn)
    assert migrate(conn, REGISTRY_MIGRATIONS_DIR) == []


def test_bad_household_status_rejected():
    conn = connect(":memory:")
    migrate(conn, REGISTRY_MIGRATIONS_DIR)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO households (status, created_at) VALUES ('bogus', 'now')")


def test_household_migrations_include_secrets():
    conn = connect(":memory:")
    assert "008_secrets" in migrate(conn, MIGRATIONS_DIR)
    assert "secrets" in _tables(conn)


def test_household_db_path():
    cfg = Config(
        data_dir=Path("/data"), log_level="INFO", free_delivery_min=199,
        price_tolerance=0.05, swiggy_mcp_url="x", telegram_bot_token=None,
        telegram_group_id=None, openai_api_key=None, agent_model=None,
        agent_reasoning_effort=None, swiggy_address_id=None,
    )
    assert cfg.household_db(3).parts[-2:] == ("households", "3.db")
