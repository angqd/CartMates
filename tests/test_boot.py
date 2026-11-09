import os
import signal
import sqlite3
import subprocess
import sys
import time

import pytest

from queuify.__main__ import recover_stuck_orders
from queuify.store import db

TABLES = {
    "members", "queue_lines", "queue_line_owners", "pending", "orders",
    "order_proposal_lines", "order_items", "order_item_owners", "order_shares",
    "events", "chat_memory", "kv",
}


def run_app(data_dir):
    """Boot `python -m queuify`, wait until ready, SIGTERM it, return (exit code, output)."""
    # Minimal env: no Telegram/OpenAI vars, no inherited DATA_DIR.
    env = {"PATH": os.environ["PATH"], "DATA_DIR": str(data_dir)}
    proc = subprocess.Popen(
        [sys.executable, "-m", "queuify"],
        env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )
    output = ""
    deadline = time.monotonic() + 10
    while "queuify ready" not in output:
        assert time.monotonic() < deadline, f"never became ready:\n{output}"
        line = proc.stdout.readline()
        assert line or proc.poll() is None, f"exited early:\n{output}"
        output += line
    proc.send_signal(signal.SIGTERM)
    output += proc.communicate(timeout=10)[0]
    return proc.returncode, output


def test_migrate_creates_schema_and_is_idempotent(tmp_path):
    conn = db.connect(tmp_path / "t.db")
    assert db.migrate(conn) == ["001_init", "002_orders", "003_order_address", "004_payment", "005_trace_id",
                                  "006_reply_threading", "007_queue_slot"]

    names = {r["name"] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert TABLES <= names
    kv = dict(conn.execute("SELECT key, value FROM kv").fetchall())
    assert kv == {"queue_version": "0", "auth_state": "logged_out"}

    assert db.migrate(conn) == []


def test_migration_006_applies_on_an_existing_db_and_keeps_rows(tmp_path):
    conn = db.connect(tmp_path / "t.db")
    conn.execute("CREATE TABLE schema_migrations (version TEXT PRIMARY KEY, applied_at TEXT)")
    for file in sorted(db.MIGRATIONS_DIR.glob("00[1-5]_*.sql")):  # a DB from before this change
        conn.executescript(file.read_text())
        conn.execute("INSERT INTO schema_migrations VALUES (?, 'x')", (file.stem,))
    conn.execute("INSERT INTO chat_memory (ts, role, text) VALUES ('t', 'assistant', 'old reply')")
    assert db.migrate(conn) == ["006_reply_threading", "007_queue_slot"]
    row = conn.execute("SELECT text, to_user_id, message_id FROM chat_memory").fetchone()
    assert tuple(row) == ("old reply", None, None)
    assert "message_id" in [r["name"] for r in conn.execute("PRAGMA table_info(pending)")]


def test_app_boots_and_stops_cleanly_on_sigterm(tmp_path):
    code, output = run_app(tmp_path)
    assert code == 0
    assert (tmp_path / "queuify.db").exists()
    assert (tmp_path / "tokens").is_dir()
    assert "queuify stopped" in output


def test_boot_recovery_maps_in_flight_orders(tmp_path):
    """Called directly (no subprocess), so it is deterministic."""
    conn = db.connect(tmp_path / "queuify.db")
    db.migrate(conn)
    conn.execute("INSERT INTO members VALUES (1, 'Priya', '2026-01-01T00:00:00+00:00', 1)")
    for status in ("checking_out", "proposed", "claimed", "quoting", "placed"):
        conn.execute("INSERT INTO orders (status, scope, proposed_by, proposed_at, queue_version) "
                     "VALUES (?, 'all', 1, '2026-10-29T00:00:00Z', 0)", (status,))

    recovered = recover_stuck_orders(conn)

    assert (recovered.unknown, recovered.failed) == ([1], [3, 4])
    rows = [tuple(r) for r in conn.execute("SELECT status, error FROM orders ORDER BY id")]
    assert rows == [("unknown", "interrupted by restart"), ("proposed", None),
                    ("failed", "interrupted before checkout, nothing was ordered"),
                    ("failed", "interrupted before checkout, nothing was ordered"), ("placed", None)]
    assert len(recover_stuck_orders(conn)) == 0  # nothing left in flight


@pytest.mark.parametrize("sql", [
    "INSERT INTO orders (status, scope, proposed_by, proposed_at, queue_version) VALUES ('bogus', 'all', 1, '2026-10-29T00:00:00Z', 0)",
    "INSERT INTO orders (status, scope, proposed_by, proposed_at, queue_version) VALUES ('placed', 'everyone', 1, '2026-10-29T00:00:00Z', 0)",
    "INSERT INTO pending (user_id, kind, created_at) VALUES (1, 'bogus', 'x')",
    "INSERT INTO queue_lines (spin_id, name, qty, added_by, added_at, status) "
    "VALUES ('s', 'n', 1, 1, 'x', 'bogus')",
    "INSERT INTO queue_lines (spin_id, name, qty, added_by, added_at) VALUES ('s', 'n', 0, 1, 'x')",
    "INSERT INTO orders (status, scope, proposed_by, proposed_at, queue_version) VALUES ('placed', 'all', 999, '2026-10-29T00:00:00Z', 0)",
])
def test_constraints_reject_bad_rows(tmp_path, sql):
    conn = db.connect(tmp_path / "t.db")
    db.migrate(conn)
    conn.execute("INSERT INTO members VALUES (1, 'Priya', '2026-01-01T00:00:00+00:00', 1)")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(sql)
