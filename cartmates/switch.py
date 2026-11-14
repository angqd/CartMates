"""The one-time switch from the v1 file (DESIGN 17.3).

Runs on every boot. Each step checks whether it already happened, so a crash halfway and a second run
both end in the same state. Never log a token.
"""
import json
import logging
import os
import shutil
import sqlite3
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path

from cartmates import registry
from cartmates.config import Config
from cartmates.domain import address
from cartmates.store import db
from cartmates.store.secrets import SecretStore

log = logging.getLogger("cartmates.switch")

V1_HOUSEHOLD = 1
STEPS = ("backup", "registry", "move", "address", "tokens", "log")
DONE_KEY = "v1_switch_done"


class SwitchError(RuntimeError):
    pass


def _v1_db(config: Config) -> Path:
    return config.db_path


def _token_file(config: Config) -> Path:
    return config.tokens_dir / "swiggy.json"


def _kv(path: Path, key: str) -> str | None:
    conn = sqlite3.connect(path)
    try:
        row = conn.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
    except sqlite3.OperationalError:  # no kv table yet
        row = None
    finally:
        conn.close()
    return row[0] if row else None


def _mkdir_private(path: Path) -> None:
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(path, 0o700)  # mkdir's mode is masked by umask and ignored if the dir already exists


def _backup(config: Config) -> None:
    src = _v1_db(config)
    if not src.exists():
        return
    _mkdir_private(config.backups_dir)
    if any(config.backups_dir.glob("v1-*")):
        return
    # Write into a temp dir and rename it, so a crash mid-copy never leaves a backup that looks done.
    tmp = config.backups_dir / ".v1-partial"
    shutil.rmtree(tmp, ignore_errors=True)
    tmp.mkdir(mode=0o700)
    source, dest = sqlite3.connect(src), sqlite3.connect(tmp / "cartmates.db")
    try:
        source.backup(dest)  # reads through the WAL, so its contents are included
    finally:
        dest.close()
        source.close()
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    os.replace(tmp, config.backups_dir / f"v1-{stamp}")
    log.info("backed up %s to %s", src.name, config.backups_dir / f"v1-{stamp}")


def _registry(config: Config) -> None:
    src = _v1_db(config) if _v1_db(config).exists() else config.household_db(V1_HOUSEHOLD)
    reg = registry.open(config.registry_path)
    try:
        if not src.exists():
            return
        if not reg.execute("SELECT 1 FROM households LIMIT 1").fetchone():
            hid = registry.create_household(reg)
            if hid != V1_HOUSEHOLD:
                raise SwitchError(f"the v1 household got id {hid}, expected {V1_HOUSEHOLD}")
            log.info("created household %s for the v1 group", hid)
        status = reg.execute("SELECT status FROM households WHERE id=?", (V1_HOUSEHOLD,)).fetchone()
        if status is None or status[0] == "deleted" or registry.chat_for(reg, V1_HOUSEHOLD) is not None:
            return
        stored = _kv(src, "group_id")
        chat_id = int(stored) if stored else config.telegram_group_id
        if chat_id is None:
            log.warning("household %s has no group chat yet (no kv.group_id, no TELEGRAM_GROUP_ID): setup mode",
                        V1_HOUSEHOLD)
            return
        registry.map_chat(reg, V1_HOUSEHOLD, chat_id)
        log.info("household %s reaches chat %s", V1_HOUSEHOLD, chat_id)
    finally:
        reg.close()


def _move(config: Config) -> None:
    src, dest = _v1_db(config), config.household_db(V1_HOUSEHOLD)
    if src.exists() and dest.exists():
        raise SwitchError(f"both {src} and {dest} exist; not guessing which one is current. "
                          f"Move one of them away and restart.")
    if src.exists():
        dest.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        conn = sqlite3.connect(src)
        try:
            busy, wal_pages, _ = conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        finally:
            conn.close()
        if busy:
            raise SwitchError(f"{src} is in use (WAL checkpoint was blocked); stop the other process and restart")
        if wal_pages not in (0, -1):  # -1: the file was not in WAL mode
            raise SwitchError(f"{src}'s WAL was not fully checkpointed ({wal_pages} pages left)")
        os.replace(src, dest)
        log.info("moved %s to %s", src.name, dest)
    # After a TRUNCATE checkpoint these are empty; left over only if we crashed right after the move.
    if not src.exists():
        for suffix in ("-wal", "-shm"):
            Path(f"{src}{suffix}").unlink(missing_ok=True)
    if dest.exists():
        conn = db.connect(dest)
        try:
            applied = db.migrate(conn)
        finally:
            conn.close()
        if applied:
            log.info("household %s: migrations applied: %s", V1_HOUSEHOLD, applied)


def _switch_target(config: Config) -> sqlite3.Connection | None:
    """households/1.db, if it exists and the switch hasn't finished on it."""
    path = config.household_db(V1_HOUSEHOLD)
    if not path.exists():
        return None
    conn = db.connect(path)
    if conn.execute("SELECT 1 FROM kv WHERE key=?", (DONE_KEY,)).fetchone():
        conn.close()
        return None
    return conn


def _address(config: Config) -> None:
    conn = _switch_target(config)
    if conn is None:
        return
    try:
        if address.current(conn) is None and config.swiggy_address_id:
            address.set_current(conn, config.swiggy_address_id)
            log.info("household %s: delivery address copied from SWIGGY_ADDRESS_ID", V1_HOUSEHOLD)
    finally:
        conn.close()


def _tokens(config: Config) -> bool:
    """Returns True if this run completed the switch."""
    conn = _switch_target(config)
    if conn is None:
        return False
    try:
        path = _token_file(config)
        if path.exists():
            store = SecretStore(conn, config.master_key)
            if store.get("swiggy") is None:
                store.put("swiggy", json.loads(path.read_text()))
            if store.get("swiggy") is None:
                raise SwitchError("the Swiggy tokens did not read back after encrypting; the token file was kept")
            path.unlink()
            log.info("household %s: Swiggy tokens moved into the encrypted store", V1_HOUSEHOLD)
        if config.tokens_dir.is_dir() and not any(config.tokens_dir.iterdir()):
            config.tokens_dir.rmdir()
        with db.transaction(conn):
            conn.execute("DELETE FROM kv WHERE key='group_id'")  # the registry holds it now
            conn.execute("INSERT INTO kv (key, value) VALUES (?, ?)",
                         (DONE_KEY, datetime.now(timezone.utc).isoformat()))
        return True
    finally:
        conn.close()


def switch_from_v1(config: Config, after_step: Callable[[str], None] | None = None) -> None:
    hook = after_step or (lambda name: None)
    _backup(config)
    hook("backup")
    _registry(config)
    hook("registry")
    _move(config)
    hook("move")
    _address(config)
    hook("address")
    completed = _tokens(config)
    hook("tokens")
    if completed:
        log.warning("the switch from v1 is done; these .env lines can now be removed: "
                    "TELEGRAM_GROUP_ID, SWIGGY_ADDRESS_ID")
    hook("log")
