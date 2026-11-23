"""Host commands, run next to the live bot (the registry and household files are in WAL mode).

    python -m cartmates.admin add-household <chat_id>

M11 adds `invite` and `usage` as further subcommands."""
import argparse
import os
import sys
from datetime import datetime, timezone

from cartmates import registry
from cartmates.config import Config, load_config
from cartmates.store import db
from cartmates.switch import DONE_KEY


def add_household(config: Config, chat_id: int) -> int:
    """DESIGN 17.8.1: a new household with its own migrated file, reached from chat_id. Returns its id.
    Raises ValueError if the chat already reaches a household."""
    config.households_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(config.households_dir, 0o700)
    reg = registry.open(config.registry_path)
    try:
        existing = registry.household_for(reg, chat_id)
        if existing is not None:
            raise ValueError(f"chat {chat_id} already reaches household {existing}")
        hid = registry.create_household(reg)
        path = config.household_db(hid)
        try:
            if path.exists():
                raise RuntimeError(f"{path} already exists")
            conn = db.connect(path)
            try:
                db.migrate(conn)
                # So the switch from v1 (DESIGN 17.3) never treats this file as the v1 one.
                conn.execute("INSERT INTO kv (key, value) VALUES (?, ?)",
                             (DONE_KEY, datetime.now(timezone.utc).isoformat()))
            finally:
                conn.close()
        except Exception:
            registry.set_status(reg, hid, "deleted")  # the id is spent; never reused
            raise
        # Mapped last, so the running bot never routes to a household whose file isn't ready.
        registry.map_chat(reg, hid, chat_id)
        return hid
    finally:
        reg.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m cartmates.admin", description="CartMates host commands")
    commands = parser.add_subparsers(dest="command", required=True)
    add = commands.add_parser("add-household", help="create a household for a Telegram group")
    add.add_argument("chat_id", type=int, help="the group's chat id (negative), from the bot's not-set-up reply")
    args = parser.parse_args(argv)

    if args.command == "add-household":
        try:
            hid = add_household(load_config(), args.chat_id)
        except ValueError as e:
            print(f"refused: {e}", file=sys.stderr)
            return 1
        print(hid)
        return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
