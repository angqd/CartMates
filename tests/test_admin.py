"""`python -m cartmates.admin add-household` (DESIGN 17.8.1)."""
import os
import sqlite3
import subprocess
import sys

from cartmates import registry
from cartmates.store import db
from cartmates.switch import DONE_KEY

CHAT = -100555


def admin(data_dir, *args):
    env = {"PATH": os.environ["PATH"], "DATA_DIR": str(data_dir)}
    return subprocess.run([sys.executable, "-m", "cartmates.admin", *args], env=env,
                          capture_output=True, text=True, timeout=30)


def test_add_household_creates_the_row_the_file_and_the_mapping_and_refuses_a_second_time(tmp_path):
    done = admin(tmp_path, "add-household", str(CHAT))
    assert done.returncode == 0, done.stderr
    hid = int(done.stdout.strip())

    reg = registry.open(tmp_path / "registry.db")
    assert registry.household_ids(reg) == [hid]
    assert registry.household_for(reg, CHAT) == hid

    path = tmp_path / "households" / f"{hid}.db"
    conn = sqlite3.connect(path)
    applied = {r[0] for r in conn.execute("SELECT version FROM schema_migrations")}
    assert applied == {f.stem for f in db.MIGRATIONS_DIR.glob("*.sql")}
    assert conn.execute("SELECT 1 FROM kv WHERE key=?", (DONE_KEY,)).fetchone()
    conn.close()

    again = admin(tmp_path, "add-household", str(CHAT))
    assert again.returncode != 0
    assert str(CHAT) in again.stderr and again.stdout == ""
    assert registry.household_ids(reg) == [hid]  # no second household
    assert sorted(p.name for p in (tmp_path / "households").iterdir() if p.suffix == ".db") == [f"{hid}.db"]
    reg.close()


def test_a_second_group_gets_a_new_household(tmp_path):
    first = int(admin(tmp_path, "add-household", str(CHAT)).stdout)
    second = admin(tmp_path, "add-household", "-100777")
    assert second.returncode == 0 and int(second.stdout) == first + 1


def test_a_positive_chat_id_is_refused_and_nothing_is_created(tmp_path):
    done = admin(tmp_path, "add-household", "5160608902")
    assert done.returncode == 1 and done.stdout == ""
    assert done.stderr.startswith("refused:") and "-5160608902" in done.stderr
    assert not (tmp_path / "registry.db").exists()
    assert not (tmp_path / "households").exists()


def test_main_accepts_a_negative_positional(tmp_path, monkeypatch, capsys):
    from cartmates import admin as admin_mod
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    assert admin_mod.main(["add-household", "-100123"]) == 0
    hid = int(capsys.readouterr().out.strip())
    reg = registry.open(tmp_path / "registry.db")
    assert registry.household_for(reg, -100123) == hid
    reg.close()
