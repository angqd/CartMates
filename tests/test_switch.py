"""The one-time switch from the v1 file and the boot pass (DESIGN 17.3, 17.2)."""
import json
import shutil
import sqlite3
from pathlib import Path

import pytest
from cryptography.fernet import Fernet

from cartmates import registry
from cartmates.__main__ import master_key_missing
from cartmates.boot import boot_pass
from cartmates.config import Config
from cartmates.store import db
from cartmates.store.secrets import SecretStore
from cartmates.switch import STEPS, SwitchError, switch_from_v1

KEY = Fernet.generate_key().decode()
GROUP = -100123
FAKE_TOKENS = {"access_token": "fake-access-7f3a9c", "refresh_token": "fake-refresh-2b8d1e", "client_id": "cid-1"}


def make_config(data_dir: Path, master_key: str | None = KEY, group_id: int | None = None) -> Config:
    return Config(data_dir=data_dir, log_level="INFO", free_delivery_min=199, price_tolerance=0.05,
                  swiggy_mcp_url="http://unused", telegram_bot_token=None, telegram_group_id=group_id,
                  openai_api_key=None, agent_model=None, agent_reasoning_effort=None,
                  swiggy_address_id="addr-v1", master_key=master_key)


def build_v1(data_dir: Path, v1_migrations: Path, group_id: int | None = GROUP) -> None:
    """A cartmates.db as the v1 code left it: migrations 001-007, a group id in kv, a token file."""
    data_dir.mkdir(exist_ok=True)
    conn = db.connect(data_dir / "cartmates.db")
    db.migrate(conn, v1_migrations)
    conn.execute("INSERT INTO members VALUES (1, 'Priya', '2026-11-01T00:00:00+00:00', 1)")
    conn.execute("INSERT INTO queue_lines (spin_id, name, qty, added_by, added_at) "
                 "VALUES ('s1', 'Amul milk', 2, 1, '2026-11-01T00:00:00+00:00')")
    conn.execute("INSERT INTO orders (status, scope, proposed_by, proposed_at, queue_version) "
                 "VALUES ('checking_out', 'all', 1, '2026-11-01T00:00:00+00:00', 0)")
    if group_id is not None:
        conn.execute("INSERT INTO kv (key, value) VALUES ('group_id', ?)", (str(group_id),))
    conn.close()  # leaves WAL mode set on the file, as the live bot does
    (data_dir / "tokens").mkdir(mode=0o700)
    (data_dir / "tokens" / "swiggy.json").write_text(json.dumps(FAKE_TOKENS))


@pytest.fixture(scope="session")
def v1_migrations(tmp_path_factory) -> Path:
    d = tmp_path_factory.mktemp("v1_migrations")
    for f in sorted(db.MIGRATIONS_DIR.glob("00[1-7]_*.sql")):
        shutil.copy(f, d)
    return d


@pytest.fixture
def v1(tmp_path, v1_migrations) -> Config:
    data = tmp_path / "data"
    build_v1(data, v1_migrations)
    return make_config(data)


def snapshot(config: Config) -> dict:
    """Everything the switch should leave behind, with paths relative and the backup stamp hidden."""
    reg = sqlite3.connect(config.registry_path)
    out = {
        "households": reg.execute("SELECT id, status, setup_by FROM households ORDER BY id").fetchall(),
        "chats": reg.execute("SELECT platform, chat_id, household_id FROM chats").fetchall(),
        "files": sorted("backups/v1-*/" + p.name if p.parent.name.startswith("v1-") else
                        str(p.relative_to(config.data_dir)) for p in config.data_dir.rglob("*")),
    }
    reg.close()
    path = config.household_db(1)
    if path.exists():
        conn = db.connect(path)
        out["members"] = [tuple(r) for r in conn.execute("SELECT user_id, display_name FROM members")]
        out["lines"] = [tuple(r) for r in conn.execute("SELECT name, qty FROM queue_lines")]
        out["orders"] = [tuple(r) for r in conn.execute("SELECT id, status FROM orders")]
        out["kv"] = sorted(k for (k,) in conn.execute("SELECT key FROM kv"))
        out["address"] = conn.execute("SELECT value FROM kv WHERE key='address_id'").fetchone()[0]
        out["migrations"] = [v for (v,) in conn.execute("SELECT version FROM schema_migrations ORDER BY version")]
        out["secret"] = SecretStore(conn, config.master_key).get("swiggy")
        conn.close()
    return out


def test_switch_moves_the_v1_file_into_household_1(v1):
    switch_from_v1(v1)

    reg = registry.open(v1.registry_path)
    assert registry.household_ids(reg) == [1]
    assert registry.chat_for(reg, 1) == GROUP
    conn = db.connect(v1.household_db(1))
    assert conn.execute("SELECT display_name FROM members").fetchone()[0] == "Priya"
    assert conn.execute("SELECT name FROM queue_lines").fetchone()[0] == "Amul milk"
    assert conn.execute("SELECT count(*) FROM orders").fetchone()[0] == 1
    assert conn.execute("SELECT 1 FROM schema_migrations WHERE version='008_secrets'").fetchone()
    kv = dict(conn.execute("SELECT key, value FROM kv").fetchall())
    assert kv["address_id"] == "addr-v1" and "group_id" not in kv
    assert SecretStore(conn, KEY).get("swiggy") == FAKE_TOKENS
    conn.close()

    assert not (v1.tokens_dir / "swiggy.json").exists()
    assert not v1.db_path.exists()
    assert not any(v1.data_dir.glob("cartmates.db*"))
    [backup] = v1.backups_dir.glob("v1-*/cartmates.db")
    assert list(backup.parent.iterdir()) == [backup]  # no token file in the backup
    assert (v1.backups_dir.stat().st_mode & 0o777) == 0o700
    b = sqlite3.connect(backup)
    assert b.execute("SELECT name FROM queue_lines").fetchone()[0] == "Amul milk"
    b.close()

    for f in v1.data_dir.rglob("*"):
        if f.is_file():
            data = f.read_bytes()
            assert b"fake-access-7f3a9c" not in data and b"fake-refresh-2b8d1e" not in data, f


def test_a_second_run_changes_nothing(v1):
    switch_from_v1(v1)
    first = snapshot(v1)
    switch_from_v1(v1)
    assert snapshot(v1) == first


@pytest.mark.parametrize("step", STEPS)
def test_a_crash_after_any_step_then_a_rerun_ends_like_one_clean_run(tmp_path, v1_migrations, step):
    clean = make_config(tmp_path / "clean")
    build_v1(clean.data_dir, v1_migrations)
    switch_from_v1(clean)

    crashed = make_config(tmp_path / "crashed")
    build_v1(crashed.data_dir, v1_migrations)

    def crash(name):
        if name == step:
            raise RuntimeError("power cut")
    with pytest.raises(RuntimeError, match="power cut"):
        switch_from_v1(crashed, after_step=crash)
    switch_from_v1(crashed)

    assert snapshot(crashed) == snapshot(clean)


def test_telegram_group_id_is_used_when_kv_has_none(tmp_path, v1_migrations):
    config = make_config(tmp_path / "data", group_id=-555)
    build_v1(config.data_dir, v1_migrations, group_id=None)
    switch_from_v1(config)
    assert registry.chat_for(registry.open(config.registry_path), 1) == -555


def test_no_group_anywhere_leaves_household_1_without_a_chat(tmp_path, v1_migrations):
    config = make_config(tmp_path / "data")
    build_v1(config.data_dir, v1_migrations, group_id=None)
    switch_from_v1(config)
    reg = registry.open(config.registry_path)
    assert registry.household_ids(reg) == [1] and registry.chat_for(reg, 1) is None


def test_clean_install_creates_only_an_empty_registry(tmp_path):
    config = make_config(tmp_path, master_key=None)
    config.households_dir.mkdir()
    switch_from_v1(config)
    assert registry.household_ids(registry.open(config.registry_path)) == []
    assert list(config.households_dir.iterdir()) == []
    assert not config.backups_dir.exists() or not any(config.backups_dir.iterdir())


def test_both_v1_and_household_files_stop_the_switch(v1):
    v1.households_dir.mkdir()
    shutil.copy(v1.db_path, v1.household_db(1))
    with pytest.raises(SwitchError, match="both"):
        switch_from_v1(v1)
    assert v1.db_path.exists() and (v1.tokens_dir / "swiggy.json").exists()  # nothing was moved


def test_boot_pass_marks_a_corrupt_household_broken_and_starts_the_others(v1):
    switch_from_v1(v1)
    reg = registry.open(v1.registry_path)
    assert registry.create_household(reg) == 2
    v1.household_db(2).write_bytes(b"this is not a database" * 100)

    started = boot_pass(v1, reg)

    assert [s.household_id for s in started] == [1]
    assert started[0].recovered.unknown == [1]  # the order that was checking out
    assert registry.household_ids(reg, "broken") == [2]
    assert registry.household_ids(reg) == [1]
    started[0].conn.close()


def test_master_key_is_required_once_a_household_exists(tmp_path, v1_migrations):
    clean = make_config(tmp_path / "clean", master_key=None)
    assert not master_key_missing(clean)  # nothing on disk at all

    v1 = make_config(tmp_path / "v1", master_key=None)
    build_v1(v1.data_dir, v1_migrations)
    assert master_key_missing(v1)  # the switch is about to create household 1

    switch_from_v1(make_config(v1.data_dir))  # with a key
    assert master_key_missing(v1)  # now the registry has a household
    assert not master_key_missing(make_config(v1.data_dir))
