"""B9: queue lines show a short, stable slot number, not the ever-growing row id."""
import json
from datetime import datetime, timezone

from cartmates.agent import memory
from cartmates.agent.llm import LLMTurn, ScriptedLLM, ToolCall
from cartmates.domain import queue
from cartmates.store import db
from tests.test_nudge import placed
from tests.test_orders import PRIYA, ROHAN, Env, add, conn, run  # noqa: F401


def slots(conn):
    return {r["name"]: r["slot"] for r in conn.execute("SELECT name, slot FROM queue_lines WHERE status='queued'")}


def remove(conn, slot):
    with db.transaction(conn):
        queue.remove_line(conn, slot, PRIYA)


def tool_env(conn, name, **args):
    return Env(conn, ScriptedLLM([LLMTurn(tool_calls=[ToolCall("c1", name, json.dumps(args))]), LLMTurn(text="ok")]))


def test_slots_are_reused_after_a_removal(conn):
    for spin in ("E1", "M1", "B1"):
        add(conn, spin, spin, 1, [PRIYA])
    assert slots(conn) == {"E1": 1, "M1": 2, "B1": 3}
    remove(conn, 2)
    add(conn, "E1", "N1", 1, [PRIYA])
    assert slots(conn) == {"E1": 1, "N1": 2, "B1": 3}
    assert conn.execute("SELECT id FROM queue_lines WHERE name='N1'").fetchone()[0] == 4  # id keeps growing


def test_an_ordered_line_frees_its_slot(conn):
    env = Env(conn)
    add(conn, "E1", "E1", 1, [PRIYA])
    add(conn, "B1", "B1", 1, [PRIYA])
    placed(env)
    assert conn.execute("SELECT slot FROM queue_lines WHERE status='ordered'").fetchall()[0][0] is None
    assert slots(conn) == {}
    add(conn, "M1", "M1", 1, [PRIYA])
    assert slots(conn) == {"M1": 1}


def test_a_queued_slot_never_changes(conn):
    for spin in ("E1", "M1", "B1"):
        add(conn, spin, spin, 1, [PRIYA])
    remove(conn, 1)
    add(conn, "E1", "N1", 1, [PRIYA])
    remove(conn, 3)
    add(conn, "B1", "N2", 1, [PRIYA])
    add(conn, "B1", "N3", 1, [PRIYA])
    assert slots(conn) == {"M1": 2, "N1": 1, "N2": 3, "N3": 4}


def test_agent_tools_resolve_the_slot_not_the_row_id(conn):
    add(conn, "E1", "E1", 1, [PRIYA])
    add(conn, "M1", "M1", 1, [PRIYA])
    remove(conn, 2)
    add(conn, "B1", "B1", 1, [PRIYA])  # id 3, slot 2
    run(tool_env(conn, "update_queue_item", number=2, qty=4).say("make #2 four"))
    assert {r["name"]: r["qty"] for r in conn.execute("SELECT * FROM queue_lines WHERE status='queued'")} == \
        {"E1": 1, "B1": 4}
    env = tool_env(conn, "remove_from_queue", number=2)
    run(env.say("drop #2"))
    assert slots(conn) == {"E1": 1}
    # a removed line's id (or slot) is no longer addressable
    run(tool_env(conn, "remove_from_queue", number=3).say("drop #3"))
    assert slots(conn) == {"E1": 1}


def test_snapshot_and_list_show_slots(conn):
    env = Env(conn)
    add(conn, "E1", "E1", 1, [PRIYA])
    add(conn, "M1", "M1", 1, [PRIYA])
    remove(conn, 1)
    add(conn, "B1", "B1", 1, [ROHAN])  # id 3, slot 1
    assert "#1 B1 (1 pc) ×1 - Rohan" in run(env.say("/list")) and "#3" not in run(env.say("/list"))
    snap = memory.build_snapshot(conn, PRIYA, datetime.now(timezone.utc))
    assert "  #1 B1 (1 pc) ×1 for Rohan" in snap and "  #2 M1" in snap and "#3" not in snap


def test_migration_007_backfills_one_to_n(tmp_path):
    c = db.connect(tmp_path / "t.db")
    c.execute("CREATE TABLE schema_migrations (version TEXT PRIMARY KEY, applied_at TEXT)")
    for file in sorted(db.MIGRATIONS_DIR.glob("00[1-6]_*.sql")):
        c.executescript(file.read_text())
        c.execute("INSERT INTO schema_migrations VALUES (?, 'x')", (file.stem,))
    c.execute("INSERT INTO members (user_id, display_name, joined_at) VALUES (1, 'Priya', 'x')")
    for status in ("queued", "removed", "queued", "ordered", "queued"):
        c.execute("INSERT INTO queue_lines (spin_id, name, qty, added_by, added_at, status) "
                  "VALUES ('s', 'n', 1, 1, 'x', ?)", (status,))
    assert db.migrate(c) == ["007_queue_slot", "008_secrets"]
    assert [tuple(r) for r in c.execute("SELECT id, slot FROM queue_lines ORDER BY id")] == \
        [(1, 1), (2, None), (3, 2), (4, None), (5, 3)]
