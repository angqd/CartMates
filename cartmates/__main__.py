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
from cartmates.domain.group import GroupChat
from cartmates.handlers import Handlers, menu_commands
from cartmates.router import LaneRouter, household_opener
from cartmates.store.secrets import SecretStore
from cartmates.switch import SwitchError, switch_from_v1
from cartmates.swiggy.auth import SwiggyAuth
from cartmates.swiggy.client import SwiggyClient
from cartmates.telegram.adapter import ALLOWED_UPDATES, TelegramAdapter
from cartmates.telegram.outbox import TelegramOutbox
from cartmates.telegram.polling import PollingErrorLogger

log = logging.getLogger("cartmates")

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


def handlers_factory(config: Config, outbox: TelegramOutbox):
    """How the router builds one household's Handlers: its own connection, Swiggy login and client, and the
    shared LLM."""
    llm, missing = None, "OPENAI_API_KEY"
    if config.openai_api_key and config.agent_model:
        llm = OpenAILLM(config.openai_api_key, config.agent_model, config.agent_reasoning_effort)
    else:
        missing = "AGENT_MODEL" if config.openai_api_key else missing
        log.warning("%s is not set: free-text messages get a 'not configured' reply", missing)

    def make(household_id: int, conn: sqlite3.Connection, group: GroupChat) -> Handlers:
        # Built even when logged out, so /login can start the first login.
        auth = SwiggyAuth(SecretStore(conn, config.master_key), conn)
        log.info("household %s: swiggy auth: %s", household_id, auth.sync_state())  # local only, no Swiggy call
        return Handlers(conn, outbox, group, auth, SwiggyClient(auth, config.swiggy_mcp_url),
                        llm, config.free_delivery_min, missing, config.price_tolerance,
                        config.upi_app, config.checkout_timeout_s)
    return make


async def run(config: Config, reg: sqlite3.Connection, recovered: dict[int, Recovered]) -> None:
    """Serve every household through the router. recovered: households whose lanes start right away."""
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    # `docker stop` sends SIGTERM and kills the process after 10 s,
    # so we must react to it and exit promptly.
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)

    app = router = None
    if config.telegram_bot_token is None:
        log.warning("TELEGRAM_BOT_TOKEN is not set: Telegram is disabled")
    else:
        # Manual lifecycle, not run_polling(), which would install its own signal handlers.
        app = Application.builder().token(config.telegram_bot_token).build()
        await app.initialize()
        me = await app.bot.get_me()
        # The Telegram "/" menu comes from the same list as /help, so the two never drift.
        await app.bot.set_my_commands([BotCommand(name, desc) for name, desc in menu_commands()])
        outbox = TelegramOutbox(app.bot, lambda old, new: router.migrate(old, new))
        router = LaneRouter(reg, outbox, handlers_factory(config, outbox), household_opener(config.household_db),
                            idle_s=config.lane_idle_s)
        TelegramAdapter(router, me.id, me.username).register(app)
        router.start()
        await app.start()
        await app.updater.start_polling(allowed_updates=ALLOWED_UPDATES, drop_pending_updates=False,
                                        error_callback=PollingErrorLogger(config.telegram_bot_token))
        for hid, rec in recovered.items():  # Telegram is up: settle what the restart interrupted
            await router.recover(hid, rec)

    # Logged only after polling and the signal handlers exist, so "ready" means a signal is safe.
    log.info("cartmates ready (registry: %s)", config.registry_path)
    await stop.wait()

    if app:
        # Order matters: stop taking new updates first, then let each lane finish the
        # event in progress (it still needs the bot to reply), then close the bot.
        await app.updater.stop()
        await router.stop()
        await app.stop()
        await app.shutdown()


def main() -> int:
    config = load_config()
    logging.basicConfig(
        level=config.log_level.upper(),
        format="%(asctime)s %(levelname)s %(name)s [%(household)s %(turn_id)s]: %(message)s",
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
    # The router opens each household again when its first event comes; only these start at once.
    recovered = {s.household_id: s.recovered for s in started if s.recovered}
    for s in started:
        s.conn.close()
    if not started:
        log.warning("no household is active: every group gets the not-set-up reply")

    code = 0
    try:
        asyncio.run(run(config, reg, recovered))
    except InvalidToken:
        # A bad token is a config error, not a crash: say so plainly and exit non-zero.
        log.error("Telegram rejected TELEGRAM_BOT_TOKEN; check it in .env (BotFather /token)")
        code = 1
    finally:
        reg.close()
        tracing.shutdown()
    log.info("cartmates stopped")
    return code


if __name__ == "__main__":
    raise SystemExit(main())
