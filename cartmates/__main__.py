import asyncio
import logging
import os
import signal
import sqlite3

from telegram import BotCommand
from telegram.error import InvalidToken
from telegram.ext import Application

from cartmates import observe, registry, tracing
from cartmates.agent.llm import OpenAILLM
from cartmates.boot import Recovered, boot_pass, recover_stuck_orders  # noqa: F401 - tests import these here
from cartmates.config import Config, load_config
from cartmates.domain.group import FixedGroup, Group, GroupChat
from cartmates.handlers import Handlers, menu_commands
from cartmates.lane import Lane
from cartmates.store.secrets import SecretStore
from cartmates.switch import SwitchError, switch_from_v1
from cartmates.swiggy.auth import SwiggyAuth
from cartmates.swiggy.client import SwiggyClient
from cartmates.telegram.adapter import ALLOWED_UPDATES, TelegramAdapter
from cartmates.telegram.events import CleanupPending, Recover
from cartmates.telegram.outbox import TelegramOutbox
from cartmates.telegram.polling import PollingErrorLogger

log = logging.getLogger("cartmates")

CLEANUP_EVERY_S = 3600

NO_KEY = ("CARTMATES_MASTER_KEY is not set, and it is needed once a household exists. Generate one with\n"
          "  python -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())'\n"
          "and put it in .env as CARTMATES_MASTER_KEY=<key>. Keep it safe: without it the stored Swiggy "
          "logins can't be read.")


def prepare_data_dirs(config: Config) -> None:
    config.data_dir.mkdir(parents=True, exist_ok=True)
    for d in (config.households_dir, config.backups_dir):
        d.mkdir(mode=0o700, exist_ok=True)
        # mkdir's mode is masked by umask and ignored if the dir already exists.
        os.chmod(d, 0o700)


def master_key_missing(config: Config) -> bool:
    """True when the bot must not start: households exist (or a v1 file waits to become one) and there
    is no key to read their secrets. Checked before the switch, which would need the key."""
    if config.master_key:
        return False
    if config.db_path.exists():
        return True
    if config.registry_path.exists():
        reg = sqlite3.connect(f"file:{config.registry_path}?mode=ro", uri=True)
        try:
            return reg.execute("SELECT 1 FROM households WHERE status != 'deleted' LIMIT 1").fetchone() is not None
        except sqlite3.OperationalError:  # an empty registry file, not migrated yet
            return False
        finally:
            reg.close()
    return False


async def _ignore(event) -> None:
    pass


async def cleanup_ticker(lane: Lane, group: Group, every: float = CLEANUP_EVERY_S, sleep=asyncio.sleep) -> None:
    """Hourly: queue a cleanup event on the lane like any other. Cancelled at shutdown."""
    while True:
        await sleep(every)
        chat_id = group.current()
        if chat_id is not None:
            await lane.submit(CleanupPending(chat_id))


async def run(config: Config, group: Group, conn: sqlite3.Connection | None,
              recovered: Recovered | None = None) -> None:
    """Serve one household's group (M8 has one lane). conn None or no chat: setup mode."""
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    # `docker stop` sends SIGTERM and kills the process after 10 s,
    # so we must react to it and exit promptly.
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)

    app = lane = ticker = None
    serving = False
    if config.telegram_bot_token is None:
        log.warning("TELEGRAM_BOT_TOKEN is not set: Telegram is disabled")
    else:
        # Manual lifecycle, not run_polling(), which would install its own signal handlers.
        app = Application.builder().token(config.telegram_bot_token).build()
        await app.initialize()
        me = await app.bot.get_me()
        # The Telegram "/" menu comes from the same list as /help, so the two never drift.
        await app.bot.set_my_commands([BotCommand(name, desc) for name, desc in menu_commands()])
        outbox = TelegramOutbox(app.bot, group.record_migration)
        group_id = group.current()
        if conn is None or group_id is None:
            log.warning("no household group is registered: setup mode, send /status in the group, "
                        "then set TELEGRAM_GROUP_ID in .env and restart")
            lane = Lane(_ignore, outbox)  # the adapter submits nothing in setup mode
        else:
            serving = True
            # Built even when logged out, so /login can start the first login.
            auth = SwiggyAuth(SecretStore(conn, config.master_key), conn)
            log.info("swiggy auth: %s", auth.sync_state())  # local only, no Swiggy call
            llm, missing = None, "OPENAI_API_KEY"
            if config.openai_api_key and config.agent_model:
                llm = OpenAILLM(config.openai_api_key, config.agent_model, config.agent_reasoning_effort)
            else:
                missing = "AGENT_MODEL" if config.openai_api_key else missing
                log.warning("%s is not set: free-text messages get a 'not configured' reply", missing)
            handlers = Handlers(conn, outbox, group, auth, SwiggyClient(auth, config.swiggy_mcp_url),
                                llm, config.free_delivery_min, missing, config.price_tolerance,
                                config.upi_app, config.checkout_timeout_s)
            lane = Lane(handlers.handle, outbox)
            handlers.resubmit = lane.submit
        adapter = TelegramAdapter(lane, outbox, group, me.id, me.username)
        adapter.register(app)
        lane.start()
        await app.start()
        await app.updater.start_polling(allowed_updates=ALLOWED_UPDATES, drop_pending_updates=False,
                                        error_callback=PollingErrorLogger(config.telegram_bot_token))
        if serving:
            if recovered:  # Telegram and the lane are up: settle what the restart interrupted
                await lane.submit(Recover(group.current(), tuple(recovered.unknown), tuple(recovered.failed)))
            ticker = asyncio.create_task(cleanup_ticker(lane, group))

    # Logged only after polling and the signal handlers exist, so "ready" means a signal is safe.
    log.info("cartmates ready (registry: %s)", config.registry_path)
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
    if master_key_missing(config):
        log.error(NO_KEY)
        return 1
    prepare_data_dirs(config)
    try:
        switch_from_v1(config)
    except SwitchError as e:
        log.error("the switch from v1 stopped: %s", e)
        return 1
    tracing.setup(config)

    reg = registry.open(config.registry_path)
    started = boot_pass(config, reg)
    if started:
        first = started[0]
        if len(started) > 1:
            log.warning("%d households started, but only household %s is served until M9",
                        len(started), first.household_id)
        group, conn, recovered = GroupChat(reg, first.household_id), first.conn, first.recovered
    else:
        log.warning("no household is active")
        group, conn, recovered = FixedGroup(None), None, None

    code = 0
    try:
        asyncio.run(run(config, group, conn, recovered))
    except InvalidToken:
        # A bad token is a config error, not a crash: say so plainly and exit non-zero.
        log.error("Telegram rejected TELEGRAM_BOT_TOKEN; check it in .env (BotFather /token)")
        code = 1
    finally:
        for s in started:
            s.conn.close()
        reg.close()
        tracing.shutdown()
    log.info("cartmates stopped")
    return code


if __name__ == "__main__":
    raise SystemExit(main())
