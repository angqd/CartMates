import asyncio
import logging
import os
import signal
import sqlite3
from dataclasses import dataclass, field

from telegram import BotCommand
from telegram.error import InvalidToken
from telegram.ext import Application

from queuify import observe, tracing
from queuify.agent.llm import OpenAILLM
from queuify.config import Config, load_config
from queuify.domain import group, orders, pending
from queuify.handlers import Handlers, menu_commands
from queuify.lane import Lane
from queuify.store import db
from queuify.swiggy.auth import SwiggyAuth
from queuify.swiggy.client import SwiggyClient
from queuify.telegram.adapter import ALLOWED_UPDATES, TelegramAdapter
from queuify.telegram.events import CleanupPending, Recover
from queuify.telegram.outbox import TelegramOutbox
from queuify.telegram.polling import PollingErrorLogger

log = logging.getLogger("queuify")

# An order in one of these states was mid-flight when the process died.
IN_FLIGHT = ("claimed", "quoting", "checking_out")


CLEANUP_EVERY_S = 3600
BEFORE_CHECKOUT = "interrupted before checkout, nothing was ordered"
DURING_CHECKOUT = "interrupted by restart"


@dataclass
class Recovered:
    """Orders settled at boot. The Swiggy check for `unknown` ones happens later, on the lane."""
    unknown: list[int] = field(default_factory=list)  # were checking out
    failed: list[int] = field(default_factory=list)   # never reached checkout

    def __len__(self) -> int:
        return len(self.unknown) + len(self.failed)


def recover_stuck_orders(conn: sqlite3.Connection) -> Recovered:
    """claimed / quoting never reached checkout, so nothing was ordered: failed. checking_out may have
    ordered: unknown, and a human (or the recovery check against Swiggy) must settle it."""
    out = Recovered()
    for row in conn.execute("SELECT id, status FROM orders WHERE status IN (?, ?, ?) ORDER BY id", IN_FLIGHT).fetchall():
        if row["status"] == "checking_out":
            conn.execute("UPDATE orders SET status='unknown', error=? WHERE id=?", (DURING_CHECKOUT, row["id"]))
            out.unknown.append(row["id"])
            orders.remember_outcome(conn, f"order #{row['id']} unknown: {DURING_CHECKOUT}")
        else:
            conn.execute("UPDATE orders SET status='failed', error=? WHERE id=?", (BEFORE_CHECKOUT, row["id"]))
            out.failed.append(row["id"])
            orders.remember_outcome(conn, f"order #{row['id']} failed: {BEFORE_CHECKOUT}")
    return out


def prepare_data_dirs(config: Config) -> None:
    config.data_dir.mkdir(parents=True, exist_ok=True)
    config.tokens_dir.mkdir(mode=0o700, exist_ok=True)
    # mkdir's mode is masked by umask and ignored if the dir already exists.
    os.chmod(config.tokens_dir, 0o700)


async def _ignore(event) -> None:
    pass


async def cleanup_ticker(lane: Lane, conn: sqlite3.Connection, default_group: int | None,
                         every: float = CLEANUP_EVERY_S, sleep=asyncio.sleep) -> None:
    """Hourly: queue a cleanup event on the lane like any other. Cancelled at shutdown."""
    while True:
        await sleep(every)
        chat_id = group.current(conn, default_group)
        if chat_id is not None:
            await lane.submit(CleanupPending(chat_id))


async def run(config: Config, conn: sqlite3.Connection, recovered: Recovered | None = None) -> None:
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    # `docker stop` sends SIGTERM and kills the process after 10 s,
    # so we must react to it and exit promptly.
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)

    app = lane = ticker = None
    if config.telegram_bot_token is None:
        log.warning("TELEGRAM_BOT_TOKEN is not set: Telegram is disabled")
    else:
        # Manual lifecycle, not run_polling(), which would install its own signal handlers.
        app = Application.builder().token(config.telegram_bot_token).build()
        await app.initialize()
        me = await app.bot.get_me()
        # The Telegram "/" menu comes from the same list as /help, so the two never drift.
        await app.bot.set_my_commands([BotCommand(name, desc) for name, desc in menu_commands()])
        outbox = TelegramOutbox(app.bot, lambda new_id: group.record_migration(conn, new_id))
        group_id = group.current(conn, config.telegram_group_id)  # kv wins after a supergroup upgrade
        if group_id is None:
            log.warning("TELEGRAM_GROUP_ID is not set: setup mode, send /status in the group")
            lane = Lane(_ignore, outbox)  # the adapter submits nothing in setup mode
        else:
            # Built even when logged out, so /login can start the first login.
            auth = SwiggyAuth(config.tokens_dir, conn)
            log.info("swiggy auth: %s", auth.sync_state())  # local only, no Swiggy call
            llm, missing = None, "OPENAI_API_KEY"
            if config.openai_api_key and config.agent_model:
                llm = OpenAILLM(config.openai_api_key, config.agent_model, config.agent_reasoning_effort)
            else:
                missing = "AGENT_MODEL" if config.openai_api_key else missing
                log.warning("%s is not set: free-text messages get a 'not configured' reply", missing)
            handlers = Handlers(conn, outbox, group_id, auth,
                                SwiggyClient(auth, config.swiggy_mcp_url), config.swiggy_address_id,
                                llm, config.free_delivery_min, missing, config.price_tolerance,
                                config.upi_app, config.checkout_timeout_s)
            lane = Lane(handlers.handle, outbox)
            handlers.resubmit = lane.submit
        adapter = TelegramAdapter(lane, outbox, config.telegram_group_id, me.id, me.username, conn)
        adapter.register(app)
        lane.start()
        await app.start()
        await app.updater.start_polling(allowed_updates=ALLOWED_UPDATES, drop_pending_updates=False,
                                        error_callback=PollingErrorLogger(config.telegram_bot_token))
        if group_id is not None:
            if recovered:  # Telegram and the lane are up: settle what the restart interrupted
                await lane.submit(Recover(group_id, tuple(recovered.unknown), tuple(recovered.failed)))
            ticker = asyncio.create_task(cleanup_ticker(lane, conn, config.telegram_group_id))

    # Logged only after polling and the signal handlers exist, so "ready" means a signal is safe.
    log.info("queuify ready (db: %s)", config.db_path)
    await stop.wait()

    if app:
        # Order matters: stop taking new updates first, then let the lane finish the
        # event in progress (it still needs the bot to reply), then close the bot.
        if ticker:
            ticker.cancel()
            await asyncio.gather(ticker, return_exceptions=True)
        await app.updater.stop()
        await lane.stop()
        await app.stop()
        await app.shutdown()


def main() -> int:
    config = load_config()
    logging.basicConfig(
        level=config.log_level.upper(),
        format="%(asctime)s %(levelname)s %(name)s [%(turn_id)s]: %(message)s",
    )
    # The filter goes on the handler so records from every logger get a turn_id.
    for handler in logging.getLogger().handlers:
        handler.addFilter(observe.TurnIdFilter())
    # httpx logs every request URL at INFO, and Telegram URLs contain the bot token,
    # so without this the token would leak into the docker logs.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    # The Swiggy MCP transport logs every POST at INFO: noise, about 4 lines per call.
    logging.getLogger("httpx2").setLevel(logging.WARNING)
    prepare_data_dirs(config)
    tracing.setup(config)

    conn = db.connect(config.db_path)
    log.info("migrations applied: %s", db.migrate(conn) or "none")
    with tracing.span("boot.recover_stuck_orders", tracing.CHAIN) as sp:  # runs before the lane exists
        recovered = recover_stuck_orders(conn)
        sp.set_attributes({"queuify.unknown": len(recovered.unknown), "queuify.failed": len(recovered.failed)})
    log.info("recovered %d stuck order(s)", len(recovered))
    log.info("deleted %d expired question(s)", pending.delete_expired(conn))

    code = 0
    try:
        asyncio.run(run(config, conn, recovered))
    except InvalidToken:
        # A bad token is a config error, not a crash: say so plainly and exit non-zero.
        log.error("Telegram rejected TELEGRAM_BOT_TOKEN; check it in .env (BotFather /token)")
        code = 1
    finally:
        conn.close()
        tracing.shutdown()
    log.info("queuify stopped")
    return code


if __name__ == "__main__":
    raise SystemExit(main())
