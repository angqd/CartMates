"""M6 hardening (DESIGN 7.4): checkout timeout, restart recovery, no double add on login replay,
polling error logging, supergroup migration, expired pending cleanup. No network, fake data only."""
import asyncio
import json
import logging
from datetime import datetime, timedelta, timezone

import httpx2
import pytest
from telegram.error import ChatMigrated, NetworkError

from cartmates.__main__ import cleanup_ticker, recover_stuck_orders
from cartmates.agent.llm import ScriptedLLM
from cartmates.domain import group, pending
from cartmates.handlers import Handlers
from cartmates.lane import Lane
from cartmates.swiggy.errors import LoggedOut
from cartmates.telegram.adapter import TelegramAdapter
from cartmates.telegram.events import Recover
from cartmates.telegram.outbox import FakeOutbox, TelegramOutbox
from cartmates.telegram.polling import PollingErrorLogger
from tests.test_orders import GROUP, PRIYA, ROHAN, Env, FakeSwiggy, conn, run, seed_queue  # noqa: F401
from tests.test_telegram import BOT_ID, BOT_NAME, cmd, make_update
from tests.test_upi import UpiEnv  # noqa: F401


class HangingSwiggy(FakeSwiggy):
    """checkout never answers."""

    async def checkout(self, args):
        await asyncio.Event().wait()


@pytest.fixture
def env(conn):
    seed_queue(conn)
    e = Env(conn)
    e.swiggy = HangingSwiggy()
    e.h.executor.swiggy = e.swiggy
    e.h.executor.checkout_timeout_s = 0.05
    return e


# ---- 1. checkout timeout ----

def test_checkout_timeout_closes_session_then_verifies_and_finds_the_order(env):
    async def go():
        await env.say("/order all")
        env.swiggy.add_recent("ORD55", names=("E1", "M1"))
        await env.tap("o:1:c")
        assert asyncio.current_task().cancelling() == 0  # the timeout's cancellation did not leak
    run(go())
    assert env.swiggy.resets == 1 and env.swiggy.n("checkout") == 1 and env.swiggy.n("get_orders") == 1
    assert (env.order(1)["status"], env.order(1)["swiggy_order_id"]) == ("placed", "ORD55")


def test_checkout_timeout_with_nothing_found_is_unknown(env):
    run(env.say("/order all"))
    run(env.tap("o:1:c"))
    assert env.swiggy.resets == 1 and env.swiggy.n("checkout") == 1  # never retried
    assert env.order(1)["status"] == "unknown"
    assert "checkout took longer than 0.05 s" in env.order(1)["error"]
    assert "/resolve 1 placed" in env.outbox.sent[-1].text


def test_upi_checkout_timeout_goes_to_verify_too(env):
    run(env.say("/order all"))
    run(env.tap("o:1:u"))
    assert env.swiggy.resets == 1 and env.order(1)["status"] == "unknown"


def test_real_client_survives_a_cancelled_call_and_opens_a_fresh_session(tmp_path, conn):
    from test_swiggy import Rig
    rig = Rig(tmp_path, conn)
    rig.seed()
    hang = {"on": True}

    async def handler(request):
        body = json.loads(request.content) if request.method == "POST" and request.content else {}
        if hang["on"] and body.get("method") == "tools/call":
            await asyncio.Event().wait()
        return rig.mcp(request)
    rig.client._transport = httpx2.MockTransport(handler)

    async def go():
        async with rig.client.turn():
            with pytest.raises(TimeoutError):
                async with asyncio.timeout(0.3):
                    await rig.client.call("checkout", {}, retry_rate_limit=False)
            await rig.client.reset_session()
            hang["on"] = False
            data = await rig.client.call("get_addresses", {"page": 1})
            assert asyncio.current_task().cancelling() == 0
            return data
    assert run(go())
    assert rig.mcp.initializes == 2  # a fresh session after the reset


# ---- 2. restart recovery ----

def in_flight(env, status="checking_out", method="cod", pay_message=None):
    """Put order 1 in the state a restart would find it in."""
    run(env.say("/order all"))
    payment = json.dumps({"orderId": "SW1", "payMessageId": pay_message}) if pay_message else None
    env.conn.execute("UPDATE orders SET status=?, orderer=?, claimed_at=?, payment_method=?, payment_json=?, "
                     "swiggy_order_id=? WHERE id=1", (status, PRIYA, datetime.now(timezone.utc).isoformat(), method,
                                                      payment, "SW1" if pay_message else None))
    return recover_stuck_orders(env.conn)


def recover(env, rec, logged_in=True):
    env.h.auth = type("A", (), {"logged_in": lambda self: logged_in})()
    run(env.h.handle(Recover(GROUP, tuple(rec.unknown), tuple(rec.failed))))


def test_recovery_removes_pay_button_and_records_a_match_as_placed(conn):
    seed_queue(conn)
    env = Env(conn)
    env.swiggy.add_recent("SW1", names=("E1", "M1", "B1"))
    rec = in_flight(env, method="upi", pay_message=555)
    assert rec.unknown == [1]
    recover(env, rec)
    assert (555 in [m for _, m, _ in env.outbox.edited])
    pay = [(t, b) for (_, m, t), b in zip(env.outbox.edited, env.outbox.edited_buttons) if m == 555]
    assert pay == [("Payment interrupted by a restart; don't pay now.", None)]
    o = env.order(1)
    assert (o["status"], o["swiggy_order_id"], o["orderer"]) == ("placed", "SW1", PRIYA)
    # Shares use the proposal's unit prices: eggs 2x60 split Priya/Rohan, milk 100 in three, bread 45 Meera.
    assert env.shares(1) == {PRIYA: (6000 + 3334, PRIYA), ROHAN: (6000 + 3333, PRIYA), 3: (3333 + 4500, PRIYA)}
    assert env.line_statuses() == ["ordered"] * 3
    assert any("✅ Ordered by Priya: Swiggy #SW1" in s.text for s in env.outbox.sent)
    assert any("✅ Ordered by Priya" in t for _, m, t in env.outbox.edited if m == 1000)  # the card


def test_recovery_no_match_posts_the_resolve_message(conn):
    seed_queue(conn)
    env = Env(conn)
    rec = in_flight(env)
    recover(env, rec)
    assert env.order(1)["status"] == "unknown"
    assert env.outbox.sent[-1].text == ("Order #1 was interrupted by a restart during checkout. "
                                        "Check the Swiggy app, then /resolve 1 placed|failed.")
    assert env.swiggy.n("get_orders") == 1


def test_recovery_logged_out_does_not_verify(conn):
    seed_queue(conn)
    env = Env(conn)
    env.swiggy.add_recent("SW1")
    rec = in_flight(env, method="upi", pay_message=555)
    recover(env, rec, logged_in=False)
    assert env.swiggy.n("get_orders") == 0 and env.order(1)["status"] == "unknown"
    assert "/resolve 1 placed|failed" in env.outbox.sent[-1].text
    assert any(m == 555 for _, m, _ in env.outbox.edited)  # the button still goes


def test_recovery_unreachable_swiggy_falls_back_to_resolve_message(conn):
    seed_queue(conn)
    env = Env(conn)
    env.swiggy.orders_error = LoggedOut("rejected")
    rec = in_flight(env)
    recover(env, rec)
    assert env.order(1)["status"] == "unknown" and "/resolve 1 placed|failed" in env.outbox.sent[-1].text


def test_recovery_pre_checkout_failure_is_announced_and_lines_stay_queued(conn):
    seed_queue(conn)
    env = Env(conn)
    rec = in_flight(env, status="quoting")
    assert (rec.unknown, rec.failed) == ([], [1])
    recover(env, rec)
    assert env.order(1)["status"] == "failed"
    assert env.outbox.sent[-1].text == ("Order #1 was interrupted before checkout; nothing was ordered. "
                                        "Run /order again.")
    assert env.line_statuses() == ["queued"] * 3 and env.swiggy.n("get_orders") == 0


def test_pay_message_id_is_stored_in_payment_json(conn):
    seed_queue(conn)
    env = UpiEnv(conn)
    run(env.say("/order all"))
    run(env.tap("o:1:u"))
    payment = json.loads(env.order(1)["payment_json"])
    assert payment["payMessageId"] == 1001 and payment["orderId"] == "SW1"


# ---- 3. no double add on login replay ----

def test_logged_out_after_changes_is_not_parked_and_nothing_is_replayed(tmp_path):
    from test_agent import AgentFlow, call, lines, use
    from test_swiggy import Rig, pending_login
    from cartmates.store import db
    c = db.connect(tmp_path / "t.db")
    db.migrate(c)
    rig = Rig(tmp_path, c)
    rig.seed()

    class Flipping(ScriptedLLM):
        async def respond(self, *a, **kw):
            if len(self.sent) == 2:  # before the third model call the session stops being valid
                rig.mcp.valid_token = "revoked"
            return await super().respond(*a, **kw)

    async def go():
        async with AgentFlow(rig, []) as f:
            f.llm = Flipping([
                use(call("search_catalog", query="oat drink")),
                use(call("add_to_queue", ref="p2v1", qty=1, owners=["me"], original_text="oat")),
                use(call("search_catalog", query="milk")),   # Swiggy now rejects the token: LoggedOut
            ])
            f.handlers.llm = f.llm
            await f.ask("add oat and search milk", message_id=20)
            texts = f.texts()
            state = pending_login(c)["state"]
            assert pending_login(c)["parked"] == []
            await f.adapter.on_update(make_update(f"http://localhost:8765/callback?code=abc&state={state}",
                                                  reply_to_bot=True, message_id=21))
            await asyncio.sleep(0.4)
            return f, texts
    f, texts = run(go())
    assert any("Swiggy login:" in t for t in texts)  # the link was posted
    assert texts[-1].startswith("Swiggy logged out partway. I already did: added #1 Meadow Oat Drink")
    assert texts[-1].endswith("After logging in, ask me again for the rest.")
    assert len(lines(c)) == 1 and f.texts()[-1].startswith("Logged in")  # nothing re-added after login
    assert len(f.llm.sent) == 3
    memory = [(r["role"], r["text"]) for r in c.execute("SELECT role, text FROM chat_memory ORDER BY id")]
    assert [r for r, _ in memory] == ["user", "action"] and "added #1" in memory[1][1]


# ---- 4. polling errors ----

def test_polling_errors_log_one_line_rate_limited_and_redacted(caplog):
    now = [100.0]
    token = "123456789:AAFakeTokenFakeTokenFakeToken_abc"
    cb = PollingErrorLogger(token, clock=lambda: now[0])
    caplog.set_level(logging.WARNING)
    err = NetworkError(f"httpx.ConnectError: https://api.telegram.org/bot{token}/getUpdates failed")
    cb(err)
    cb(err)
    now[0] += 30
    cb(err)                                  # same message inside a minute: silent
    cb(NetworkError("other problem"))        # a different message is logged at once
    now[0] += 31
    cb(err)                                  # a minute since the first
    lines = [r.getMessage() for r in caplog.records]
    assert len(lines) == 3 and all(r.levelno == logging.WARNING and r.exc_info is None for r in caplog.records)
    assert lines[1] == "telegram polling: NetworkError: other problem"
    assert all(token not in l and "AAFake" not in l and l.startswith("telegram polling: NetworkError: ") for l in lines)
    assert "[token]" in lines[0]


def test_polling_logger_redacts_token_shaped_text_even_without_a_configured_token(caplog):
    caplog.set_level(logging.WARNING)
    PollingErrorLogger(None)(NetworkError("bot987654321:ZZZfaketokenfaketokenfaketoken1 down"))
    assert "ZZZfake" not in caplog.text and "[token]" in caplog.text


# ---- 5. supergroup migration ----

NEW_GROUP = -1009999


def migrate_update(chat_id=GROUP, to=NEW_GROUP):
    from telegram import Update
    from tests.test_telegram import BOT
    return Update.de_json({"update_id": 2, "message": {
        "message_id": 11, "date": 0, "chat": {"id": chat_id, "type": "group"},
        "migrate_to_chat_id": to}}, BOT)


def test_migrate_to_chat_id_switches_the_group_and_old_id_is_ignored(conn, caplog):
    outbox = FakeOutbox()
    caplog.set_level(logging.WARNING)

    async def go():
        handlers = Handlers(conn, outbox, GROUP)
        lane = Lane(handlers.handle, outbox)
        adapter = TelegramAdapter(lane, outbox, GROUP, BOT_ID, BOT_NAME, conn)
        lane.start()
        await adapter.on_migrate(migrate_update())
        await adapter.on_update(make_update("/list", entities=cmd("/list"), chat_id=GROUP))
        await adapter.on_update(make_update("/list", entities=cmd("/list"), chat_id=NEW_GROUP))
        await asyncio.sleep(0.05)
        await lane.stop()
        return handlers
    handlers = run(go())
    assert group.current(conn) == NEW_GROUP and handlers.group_id == NEW_GROUP
    assert [(s.chat_id, s.text) for s in outbox.sent] == [(NEW_GROUP, "Queue is empty.")]
    assert any("TELEGRAM_GROUP_ID" in r.getMessage() and str(NEW_GROUP) in r.getMessage()
               and r.levelno == logging.WARNING for r in caplog.records)


def test_migrate_message_from_another_chat_is_ignored(conn):
    async def go():
        adapter = TelegramAdapter(Lane(Handlers(conn, FakeOutbox(), GROUP).handle, FakeOutbox()),
                                  FakeOutbox(), GROUP, BOT_ID, BOT_NAME, conn)
        await adapter.on_migrate(migrate_update(chat_id=555))
    run(go())
    assert group.current(conn) is None


class FakeBot:
    def __init__(self, fail_for=GROUP):
        self.calls, self.fail_for = [], fail_for

    async def send_message(self, chat_id, text, **kw):
        self.calls.append(("send", chat_id))
        if chat_id == self.fail_for:
            raise ChatMigrated(NEW_GROUP)
        return type("Sent", (), {"message_id": 42})()

    async def edit_message_text(self, text, chat_id, message_id, **kw):
        self.calls.append(("edit", chat_id))
        if chat_id == self.fail_for:
            raise ChatMigrated(NEW_GROUP)


def test_chat_migrated_on_send_retries_once_to_the_new_id_and_stores_it(conn):
    bot = FakeBot()
    outbox = TelegramOutbox(bot, lambda new: group.record_migration(conn, new))
    assert run(outbox.send(GROUP, "hi")) == 42
    assert bot.calls == [("send", GROUP), ("send", NEW_GROUP)]
    assert group.current(conn) == NEW_GROUP
    run(outbox.edit(GROUP, 5, "x"))
    assert bot.calls[-2:] == [("edit", GROUP), ("edit", NEW_GROUP)]


def test_chat_migrated_is_retried_only_once(conn):
    bot = FakeBot(fail_for=None)

    async def always(chat_id, text, **kw):
        bot.calls.append(("send", chat_id))
        raise ChatMigrated(NEW_GROUP)
    bot.send_message = always
    with pytest.raises(ChatMigrated):
        run(TelegramOutbox(bot).send(GROUP, "hi"))
    assert len(bot.calls) == 2


def test_stored_group_id_overrides_env_value_and_setup_mode_is_unchanged(conn):
    group.set_current(conn, NEW_GROUP)
    assert Handlers(conn, FakeOutbox(), GROUP).group_id == NEW_GROUP
    fresh = TelegramAdapter(None, FakeOutbox(), None, BOT_ID, BOT_NAME)
    assert fresh.group_id is None  # no conn, no env id: setup mode


# ---- 6. expired pending cleanup ----

def test_expired_variant_and_scope_questions_are_deleted_but_login_stays(conn):
    past = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
    future = (datetime.now(timezone.utc) + timedelta(minutes=30)).isoformat()
    for kind, expires in (("variant", past), ("scope", past), ("scope", future), ("login", past)):
        conn.execute("INSERT INTO pending (user_id, kind, payload_json, created_at, expires_at) "
                     "VALUES (1, ?, '{}', 'x', ?)", (kind, expires))
    assert pending.delete_expired(conn) == 2
    assert [r[0] for r in conn.execute("SELECT kind FROM pending ORDER BY id")] == ["scope", "login"]


def test_cleanup_event_runs_on_the_lane_and_the_ticker_submits_and_cancels_cleanly(conn):
    conn.execute("INSERT INTO pending (user_id, kind, payload_json, created_at, expires_at) "
                 "VALUES (1, 'variant', '{}', 'x', '2000-01-01T00:00:00+00:00')")
    outbox = FakeOutbox()

    async def go():
        lane = Lane(Handlers(conn, outbox, GROUP).handle, outbox)
        lane.start()
        ticks = asyncio.Queue()

        async def sleep(_):
            await ticks.put(None)
            await asyncio.sleep(0)
        task = asyncio.create_task(cleanup_ticker(lane, conn, GROUP, every=3600, sleep=sleep))
        await ticks.get()
        await asyncio.sleep(0.05)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        assert task.cancelled() or task.done()
        await lane.stop()
    run(go())
    assert conn.execute("SELECT count(*) FROM pending").fetchone()[0] == 0
    assert outbox.sent == []
