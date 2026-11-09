import asyncio
import re
from datetime import datetime

import pytest
from telegram import Bot, Update

from queuify.handlers import Handlers
from queuify.lane import Lane
from queuify.store import db
from queuify.telegram.adapter import TelegramAdapter
from queuify.telegram.events import Message
from queuify.telegram.gate import is_for_bot
from queuify.telegram.outbox import FakeOutbox

BOT_ID, BOT_NAME, GROUP = 999, "QueuifyBot", -100123
BOT = Bot("123:fake-token")  # only used to build objects; never touches the network


def make_update(text, chat_id=GROUP, user_id=1, name="Priya", entities=None,
                reply_to_bot=False, chat_type="supergroup", message_id=10, reply_to_id=5):
    msg = {
        "message_id": message_id, "date": int(datetime.now().timestamp()),
        "chat": {"id": chat_id, "type": chat_type},
        "from": {"id": user_id, "is_bot": False, "first_name": name},
        "text": text,
    }
    if entities:
        msg["entities"] = entities
    if reply_to_bot:
        msg["reply_to_message"] = {
            "message_id": reply_to_id, "date": 0, "chat": msg["chat"],
            "from": {"id": BOT_ID, "is_bot": True, "first_name": "Queuify"}, "text": "hi",
        }
    return Update.de_json({"update_id": 1, "message": msg}, BOT)


def cmd(name):
    return [{"type": "bot_command", "offset": 0, "length": len(name)}]


@pytest.mark.parametrize("text, entities, reply, expected", [
    ("/list", cmd("/list"), False, True),
    ("/LIST", cmd("/LIST"), False, True),
    ("/list@queuifybot", cmd("/list@queuifybot"), False, True),
    ("/list@otherbot", cmd("/list@otherbot"), False, False),
    ("/list@otherbot", cmd("/list@otherbot"), True, False),
    ("hey @QueuifyBot add milk", [{"type": "mention", "offset": 4, "length": 11}], False, True),
    ("hey @rohan add milk", [{"type": "mention", "offset": 4, "length": 6}], False, False),
    ("hey @QueuifyBot_fan", [{"type": "mention", "offset": 4, "length": 15}], False, False),
    ("add milk", None, True, True),
    ("just chatting", None, False, False),
    ("mail queuifybot", None, False, False),
    ("http://localhost:8765/callback?code=abc&state=xyz", None, False, True),
    ("see http://example.com/callback?page=2", None, False, False),
])
def test_gate(text, entities, reply, expected):
    msg = make_update(text, entities=entities, reply_to_bot=reply).message
    assert is_for_bot(msg, BOT_ID, BOT_NAME) is expected


# ---- lane ----

def test_lane_is_sequential_and_in_order():
    log = []

    async def handler(event):
        log.append(("start", event.message_id))
        await asyncio.sleep(0.02)
        log.append(("end", event.message_id))

    async def go():
        lane = Lane(handler, FakeOutbox())
        lane.start()
        for i in (1, 2, 3):
            await lane.submit(Message(GROUP, i, 1, "P", "x"))
        await asyncio.sleep(0.2)
        await lane.stop()

    asyncio.run(go())
    assert log == [("start", 1), ("end", 1), ("start", 2), ("end", 2), ("start", 3), ("end", 3)]


def test_lane_survives_a_failing_handler():
    seen, outbox = [], FakeOutbox()

    async def handler(event):
        if event.message_id == 1:
            raise RuntimeError("boom")
        seen.append(event.message_id)

    async def go():
        lane = Lane(handler, outbox)
        lane.start()
        await lane.submit(Message(GROUP, 1, 1, "P", "x"))
        await lane.submit(Message(GROUP, 2, 1, "P", "x"))
        await asyncio.sleep(0.05)
        await lane.stop()

    asyncio.run(go())
    assert seen == [2]
    assert [s.text for s in outbox.sent] == ["Something went wrong, try again."]
    assert outbox.sent[0].chat_id == GROUP


def test_lane_stop_finishes_current_and_drops_queued():
    done = []

    async def handler(event):
        await asyncio.sleep(0.05)
        done.append(event.message_id)

    async def go():
        lane = Lane(handler, FakeOutbox())
        lane.start()
        for i in (1, 2, 3):
            await lane.submit(Message(GROUP, i, 1, "P", "x"))
        await asyncio.sleep(0.01)  # event 1 is now in progress
        await lane.stop()

    asyncio.run(go())
    assert done == [1]


# ---- full flow: update -> adapter -> lane -> handlers -> outbox ----

@pytest.fixture
def conn(tmp_path):
    c = db.connect(tmp_path / "t.db")
    db.migrate(c)
    return c


def run_updates(conn, updates, group_id=GROUP):
    """Feed updates through a real adapter, lane and handlers; return the outbox."""
    outbox = FakeOutbox()

    async def go():
        lane = Lane(Handlers(conn, outbox, group_id).handle, outbox)
        adapter = TelegramAdapter(lane, outbox, group_id, BOT_ID, BOT_NAME)
        lane.start()
        for u in updates:
            await adapter.on_update(u)
        await asyncio.sleep(0.05)
        await lane.stop()

    asyncio.run(go())
    return outbox


def test_status_with_no_members(conn):
    out = run_updates(conn, [make_update("/status", entities=cmd("/status"))])
    text = out.sent[0].text
    assert "Swiggy login: logged_out" in text
    assert "Queue version: 0" in text
    assert "Open order: no" in text
    # The sender is upserted before the reply is built.
    assert "Members: 1" in text
    assert f"Group id: {GROUP}" in text
    assert out.sent[0].reply_to == 10


def test_help_lists_exactly_the_five_menu_commands(conn):
    out = run_updates(conn, [make_update("/help", entities=cmd("/help"))])
    lines = out.sent[0].text.splitlines()
    listed = [l.split(" - ")[0] for l in lines if l.startswith("/")]
    assert listed == ["/help", "/list", "/order", "/address", "/status"]
    assert "Also: /track, /history, /login, /resolve" in lines
    assert not re.search(r"/(add|remove|search)\b", out.sent[0].text)


def test_list_empty_and_with_a_line(conn):
    out = run_updates(conn, [make_update("/list", entities=cmd("/list"))])
    assert out.sent[0].text == "Queue is empty."

    conn.execute("INSERT INTO members (user_id, display_name, joined_at) VALUES (2, 'Rohan', 'x')")
    conn.execute(
        "INSERT INTO queue_lines (spin_id, name, qty, added_by, added_at) VALUES ('s', 'Maggi', 2, 1, 'x')")
    conn.execute("INSERT INTO queue_line_owners VALUES (1, 1), (1, 2)")
    out = run_updates(conn, [make_update("/list", entities=cmd("/list"))])
    text = out.sent[0].text
    assert "Maggi ×2" in text and "Priya" in text and "Rohan" in text


def test_member_upserted_and_name_refreshed(conn):
    run_updates(conn, [make_update("/help", entities=cmd("/help"), name="Priya")])
    run_updates(conn, [make_update("/help", entities=cmd("/help"), name="Priya S")])
    assert [tuple(r) for r in conn.execute("SELECT user_id, display_name FROM members")] == [(1, "Priya S")]
    assert conn.execute("SELECT count(*) FROM events WHERE kind='message' AND actor='1'").fetchone()[0] == 2


def test_free_text_and_unknown_command(conn):
    mention = [{"type": "mention", "offset": 0, "length": 11}]
    out = run_updates(conn, [
        make_update("@QueuifyBot add milk", entities=mention),
        make_update("/bogus", entities=cmd("/bogus")),
    ])
    assert out.sent[0].text == "The assistant isn't configured (OPENAI_API_KEY)."
    assert "/bogus" in out.sent[1].text


def test_ungated_and_other_chat_are_ignored(conn):
    out = run_updates(conn, [
        make_update("just chatting"),
        make_update("/list", entities=cmd("/list"), chat_id=555),
        make_update("/list", entities=cmd("/list"), chat_id=42, chat_type="private"),
    ])
    assert out.sent == []
    assert conn.execute("SELECT count(*) FROM members").fetchone()[0] == 0


def test_setup_mode_replies_with_chat_id_only_to_status(conn):
    out = run_updates(conn, [
        make_update("/status", entities=cmd("/status")),
        make_update("/list", entities=cmd("/list")),
        make_update("/status", entities=cmd("/status"), chat_id=7, chat_type="private"),
    ], group_id=None)
    assert [s.text for s in out.sent] == [
        f"Setup: this chat's id is {GROUP}. Put it in .env as TELEGRAM_GROUP_ID and restart."]
