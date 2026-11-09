import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

MIGRATIONS_DIR = Path(__file__).parent / "migrations"


def connect(path: Path | str) -> sqlite3.Connection:
    # isolation_level=None turns off Python's implicit transactions, so
    # migrate() controls BEGIN/COMMIT itself.
    conn = sqlite3.connect(path, isolation_level=None)
    conn.row_factory = sqlite3.Row
    # SQLite ignores foreign keys unless this is switched on for every connection.
    conn.execute("PRAGMA foreign_keys=ON")
    # WAL lets readers and a writer work at the same time and survives crashes well.
    conn.execute("PRAGMA journal_mode=WAL")
    # Wait up to 5 s for a lock instead of failing immediately with "database is locked".
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def migrate(conn: sqlite3.Connection) -> list[str]:
    """Apply migrations that have not run yet. Returns the versions applied."""
    conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_migrations "
        "(version TEXT PRIMARY KEY, applied_at TEXT)"
    )
    done = {row["version"] for row in conn.execute("SELECT version FROM schema_migrations")}
    applied = []
    for file in sorted(MIGRATIONS_DIR.glob("*.sql")):
        if file.stem in done:
            continue
        # One transaction per migration: a failure leaves no half-applied schema.
        # BEGIN goes in the same executescript call: executescript commits any
        # transaction already open when it starts.
        try:
            conn.executescript("BEGIN;\n" + file.read_text())
            conn.execute(
                "INSERT INTO schema_migrations (version, applied_at) VALUES (?, ?)",
                (file.stem, datetime.now(timezone.utc).isoformat()),
            )
            conn.execute("COMMIT")
        except Exception:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
        applied.append(file.stem)
    return applied


@contextmanager
def transaction(conn: sqlite3.Connection):
    """All-or-nothing group of writes. The connection autocommits, so we BEGIN ourselves."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    conn.execute("COMMIT")
