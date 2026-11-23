"""M9 (DESIGN 17.8, 17.8.1): two households in one process behind the real LaneRouter. Real registry.db and
household files made by cartmates.admin.add_household, real Handlers, lanes and agent loop; the LLM, Swiggy and
Telegram are fakes. Written from the spec, so a failure here is a spec mismatch to look at, not a test to bend."""
import asyncio
import json
import re
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from cartmates import observe, registry
from cartmates.admin import add_household
from cartmates.agent.llm import LLMTurn, ToolCall
from cartmates.config import Config
from cartmates.domain import address
from cartmates.handlers import Handlers
from cartmates.router import LaneRouter, household_opener
from cartmates.store import db
from cartmates.telegram.adapter import TelegramAdapter
from cartmates.telegram.events import Message
from cartmates.telegram.outbox import FakeOutbox
from tests.test_m6 import migrate_update
from tests.test_swiggy import Rig
from tests.test_telegram import BOT_ID, BOT_NAME, make_update

CHAT_A, CHAT_B, CHAT_C = -100111, -100222, -100333
UNKNOWN_CHAT = -100999
NEW_A = -1009111  # A's id after the supergroup upgrade
ADDRESS = "aaaa1111bbbb2222cccc__FAKEsuffix123"
MENTION = [{"type": "mention", "offset": 0, "length": len("@CartMatesBot")}]

# The spec's fixed replies (17.4, 17.8.1), copied here on purpose rather than imported from the router.
PRIVATE_REFUSAL = "I only work in your household group."
BROKEN_REPLY = "This group's data can't be opened right now. The host has been told."


class FakeLLM:
    """Plays the model for any number of turns: search, then add the item the member named, then reply
    "Added <text>". `gate`, when set, blocks every round until the event is set."""

    def __init__(self):
        self.calls = 0
        self.gate: asyncio.Event | None = None
        self.waiting = 0

    async def respond(self, instructions, input_items, tools):
        self.calls += 1
        if self.gate is not None:
            self.waiting += 1
            await self.gate.wait()
            self.waiting -= 1
        last = input_items[-1]
        said = next(i["content"] for i in reversed(input_items) if i.get("role") == "user")
        text = said.split(": ", 1)[-1]
        if last.get("type") != "function_call_output":
            return LLMTurn(tool_calls=[ToolCall("c-search", "search_catalog", json.dumps({"query": "oat drink"}))])
        if last["call_id"] == "c-search":
            return LLMTurn(tool_calls=[ToolCall("c-add", "add_to_queue", json.dumps(
                {"ref": "p2v1", "qty": 1, "owners": ["me"], "original_text": text}))])
        return LLMTurn(text=f"Added {text}")


class House:
    """The whole bot minus Telegram: registry, router, fakes. Tracks every connection db.connect opens."""

    def __init__(self, tmp_path: Path, monkeypatch):
        self.config = Config(data_dir=tmp_path, log_level="INFO", free_delivery_min=199, price_tolerance=0.05,
                             swiggy_mcp_url="https://mcp.test/im", telegram_bot_token=None,
                             telegram_group_id=None, openai_api_key=None, agent_model=None,
                             agent_reasoning_effort=None, swiggy_address_id=None)
        self.opened: list[tuple[Path, sqlite3.Connection]] = []
        self.statements: list[tuple[int | None, Path]] = []  # (household of the running lane event, file)
        real_connect = db.connect

        def connect(path):
            conn = real_connect(path)
            p = Path(path).resolve() if str(path) != ":memory:" else None
            self.opened.append((p, conn))
            if p is not None and p.parent == self.config.households_dir.resolve():
                conn.set_trace_callback(lambda sql, p=p: self.statements.append((observe.household_id.get(), p)))
            return conn
        monkeypatch.setattr(db, "connect", connect)

        self.a = self.add(CHAT_A)
        self.b = self.add(CHAT_B)
        self.reg = registry.open(self.config.registry_path)
        self.outbox = FakeOutbox()
        self.llms: dict[int, FakeLLM] = {}
        self.now = [1000.0]
        self.router = LaneRouter(self.reg, self.outbox, self.make_handlers,
                                 household_opener(self.config.household_db), clock=lambda: self.now[0])
        self.adapter = TelegramAdapter(self.router, BOT_ID, BOT_NAME)
        self._next_msg = 100
        self.opened.clear()  # from here on, only what the running bot opens

    def add(self, chat_id: int) -> int:
        """cartmates.admin add-household, then the host's /address step done by hand."""
        hid = add_household(self.config, chat_id)
        conn = sqlite3.connect(self.config.household_db(hid), isolation_level=None)
        address.set_current(conn, ADDRESS)
        conn.close()
        return hid

    def make_handlers(self, hid: int, conn: sqlite3.Connection, group) -> Handlers:
        """Like __main__.handlers_factory, with the fake Swiggy and a fake LLM per household."""
        rig = Rig(None, conn)
        rig.seed()  # logged in
        llm = self.llms.setdefault(hid, FakeLLM())
        return Handlers(conn, self.outbox, group, rig.auth, rig.client, llm, self.config.free_delivery_min)

    def file(self, hid: int) -> Path:
        return self.config.household_db(hid).resolve()

    def llm(self, hid: int) -> FakeLLM:
        return self.llms.setdefault(hid, FakeLLM())

    def msg(self, chat_id: int, text: str, user_id: int = 1, name: str = "Priya") -> Message:
        self._next_msg += 1
        return Message(chat_id=chat_id, message_id=self._next_msg, user_id=user_id, display_name=name, text=text)

    async def say(self, chat_id: int, text: str) -> None:
        await self.router.submit(self.msg(chat_id, text))

    async def via_telegram(self, chat_id: int, text: str, **kw) -> None:
        self._next_msg += 1
        await self.adapter.on_update(make_update(f"@CartMatesBot {text}", chat_id=chat_id, entities=MENTION,
                                                 message_id=self._next_msg, **kw))

    def replies(self, chat_id: int | None = None) -> list[str]:
        return [s.text for s in self.outbox.sent if chat_id is None or s.chat_id == chat_id]

    async def until(self, cond, what: str, timeout: float = 3.0) -> None:
        loop = asyncio.get_running_loop()
        end = loop.time() + timeout
        while not cond():
            if loop.time() > end:
                raise AssertionError(f"timed out waiting for {what}; sent so far: {self.replies()}")
            await asyncio.sleep(0.005)

    async def added(self, chat_id: int, *texts: str) -> None:
        await self.until(lambda: all(f"Added {t}" in self.replies(chat_id) for t in texts),
                         f"replies {texts} in chat {chat_id}")

    def lines(self, hid: int) -> list[str]:
        """Read straight from the household's file, as another process would."""
        conn = sqlite3.connect(f"file:{self.file(hid)}?mode=ro", uri=True)
        try:
            return [r[0] for r in conn.execute("SELECT original_text FROM queue_lines ORDER BY id")]
        finally:
            conn.close()

    def opened_household_files(self) -> list[Path]:
        return [p for p, _ in self.opened if p is not None and p.parent == self.config.households_dir.resolve()]


@pytest.fixture
def house(tmp_path, monkeypatch):
    return House(tmp_path, monkeypatch)


def run(house: House, scenario):
    async def go():
        try:
            return await scenario()
        finally:
            await house.router.stop()
    return asyncio.run(go())


# ---- 1, 2. no crossing ----

def test_interleaved_messages_add_lines_only_to_their_own_household_file(house):
    async def go():
        for chat, text in [(CHAT_A, "a1"), (CHAT_B, "b1"), (CHAT_A, "a2"), (CHAT_B, "b2")]:
            await house.say(chat, text)
        await house.added(CHAT_A, "a1", "a2")
        await house.added(CHAT_B, "b1", "b2")
    run(house, go)
    assert house.lines(house.a) == ["a1", "a2"], "household A's file must hold only A's lines"
    assert house.lines(house.b) == ["b1", "b2"], "household B's file must hold only B's lines"
    assert all(t.endswith(("a1", "a2")) for t in house.replies(CHAT_A) if t.startswith("Added"))
    assert all(t.endswith(("b1", "b2")) for t in house.replies(CHAT_B) if t.startswith("Added"))


def test_each_turn_touches_only_its_own_households_connection(house):
    async def go():
        for chat, text in [(CHAT_A, "a1"), (CHAT_B, "b1"), (CHAT_A, "a2"), (CHAT_B, "b2")]:
            await house.say(chat, text)
        await house.added(CHAT_A, "a1", "a2")
        await house.added(CHAT_B, "b1", "b2")
        return house.router.handlers_for(house.a).conn, house.router.handlers_for(house.b).conn
    conn_a, conn_b = run(house, go)
    path_of = {id(c): p for p, c in house.opened}
    assert path_of[id(conn_a)] == house.file(house.a)
    assert path_of[id(conn_b)] == house.file(house.b)
    in_lane = [(hid, p) for hid, p in house.statements if hid is not None]
    assert {hid for hid, _ in in_lane} == {house.a, house.b}, "both lanes should have run SQL"
    crossed = [(hid, p.name) for hid, p in in_lane if p != house.file(hid)]
    assert crossed == [], f"a lane event ran SQL on another household's file: {crossed[:5]}"


# ---- 3, 4. independence and order ----

def test_a_slow_turn_in_a_does_not_delay_b(house):
    async def go():
        gate = house.llm(house.a).gate = asyncio.Event()
        await house.say(CHAT_A, "slow")
        await house.until(lambda: house.llm(house.a).waiting == 1, "A's model call to start")
        await house.say(CHAT_B, "fast")
        await house.added(CHAT_B, "fast")
        assert house.llm(house.a).waiting == 1 and "Added slow" not in house.replies(CHAT_A), \
            "A should still be blocked when B's turn finishes"
        assert house.lines(house.b) == ["fast"] and house.lines(house.a) == []
        gate.set()
        await house.added(CHAT_A, "slow")
    run(house, go)
    assert house.lines(house.a) == ["slow"]


def test_order_inside_a_household_stays_fifo(house):
    texts = [f"a{i}" for i in range(1, 6)]

    async def go():
        gate = house.llm(house.a).gate = asyncio.Event()
        for t in texts:  # all queued behind the first, which waits on the model
            await house.say(CHAT_A, t)
        await house.until(lambda: house.llm(house.a).waiting == 1, "A's first model call")
        gate.set()
        await house.added(CHAT_A, *texts)
    run(house, go)
    assert house.lines(house.a) == texts
    assert [t for t in house.replies(CHAT_A) if t.startswith("Added")] == [f"Added {t}" for t in texts]


# ---- 5. idle stop ----

def test_an_idle_lane_stops_closes_its_connection_and_restarts_with_its_data(house):
    async def go():
        await house.say(CHAT_A, "before")
        await house.added(CHAT_A, "before")
        first = house.router.handlers_for(house.a).conn
        house.now[0] += house.router.idle_s - 1
        await house.router.stop_idle()
        assert house.a in house.router.running, "a lane must not stop before idle_s"
        house.now[0] += 2
        await house.router.stop_idle()
        assert house.a not in house.router.running, "the lane should stop after idle_s with nothing queued"
        with pytest.raises(sqlite3.ProgrammingError):
            first.execute("SELECT 1")  # closed
        await house.say(CHAT_A, "after")
        await house.added(CHAT_A, "after")
        second = house.router.handlers_for(house.a).conn
        assert second is not first
        return second
    run(house, go)
    assert house.lines(house.a) == ["before", "after"]


# ---- 6, 7. replies outside a household ----

def test_unknown_group_gets_the_reply_with_its_chat_id_every_time_and_nothing_else(house):
    async def go():
        for _ in range(3):
            await house.via_telegram(UNKNOWN_CHAT, "add milk")
        await house.until(lambda: len(house.replies(UNKNOWN_CHAT)) == 3, "three replies")
    run(house, go)
    replies = house.replies(UNKNOWN_CHAT)
    assert len(replies) == 3 and all(str(UNKNOWN_CHAT) in r for r in replies), replies
    assert all(r == replies[0] for r in replies)
    assert sum(llm.calls for llm in house.llms.values()) == 0
    assert house.opened_household_files() == [], "an unknown group must not open any household file"
    assert house.router.running == set()


def test_private_chat_gets_the_refusal(house):
    async def go():
        house._next_msg += 1
        await house.adapter.on_update(make_update("hello", chat_id=1, chat_type="private",
                                                  message_id=house._next_msg))
        await house.until(lambda: house.replies(1), "the private reply")
    run(house, go)
    assert house.replies(1) == [PRIVATE_REFUSAL]
    assert sum(llm.calls for llm in house.llms.values()) == 0
    assert house.opened_household_files() == []


# ---- 8. broken household ----

def test_broken_household_gets_its_reply_and_the_other_still_works(house):
    house.file(house.b).write_bytes(b"this is not a sqlite database" * 100)
    for suffix in ("-wal", "-shm"):
        Path(str(house.file(house.b)) + suffix).unlink(missing_ok=True)

    async def go():
        await house.say(CHAT_B, "b1")
        await house.say(CHAT_A, "a1")
        await house.say(CHAT_B, "b2")
        await house.added(CHAT_A, "a1")
        await house.until(lambda: len(house.replies(CHAT_B)) == 2, "two replies in B")
    run(house, go)
    assert house.replies(CHAT_B) == [BROKEN_REPLY, BROKEN_REPLY]
    assert registry.status(house.reg, house.b) == "broken"
    assert house.lines(house.a) == ["a1"]
    assert house.llm(house.b).calls == 0


# ---- 9. supergroup upgrade ----

def test_supergroup_upgrade_keeps_the_household_and_the_old_id_goes_quiet(house):
    async def go():
        await house.via_telegram(CHAT_A, "before")
        await house.added(CHAT_A, "before")
        await house.adapter.on_migrate(migrate_update(chat_id=CHAT_A, to=NEW_A))
        sent_before = len(house.outbox.sent)
        await house.via_telegram(CHAT_A, "late from the old id")
        await house.via_telegram(NEW_A, "after")
        await house.added(NEW_A, "after")
        return sent_before
    sent_before = run(house, go)
    assert registry.household_for(house.reg, NEW_A) == house.a
    assert registry.household_for(house.reg, CHAT_A) is None
    late = [s for s in house.outbox.sent[sent_before:] if s.chat_id == CHAT_A or "old id" in s.text]
    assert late == [], f"the old id should get no reply after the upgrade: {late}"
    assert not any("isn't set up" in t for t in house.replies()), "A's group must never read the unknown reply"
    assert house.lines(house.a) == ["before", "after"]


# ---- 10. add-household on a running router ----

def test_add_household_is_served_without_restarting_the_router(house):
    async def go():
        await house.say(CHAT_A, "a1")
        await house.added(CHAT_A, "a1")
        await house.via_telegram(CHAT_C, "too early")
        await house.until(lambda: house.replies(CHAT_C), "the not-set-up reply")
        assert str(CHAT_C) in house.replies(CHAT_C)[0]
        c = house.add(CHAT_C)  # the host runs add-household next to the live bot
        await house.via_telegram(CHAT_C, "c1")
        await house.added(CHAT_C, "c1")
        return c
    c = run(house, go)
    assert house.lines(c) == ["c1"] and house.lines(house.a) == ["a1"] and house.lines(house.b) == []


# ---- 11. the hourly cleanup ----

def expire_a_question(path: Path) -> None:
    conn = sqlite3.connect(path, isolation_level=None)
    past = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    conn.execute("INSERT OR IGNORE INTO members (user_id, display_name, joined_at) VALUES (1, 'Priya', 'x')")
    conn.execute("INSERT INTO pending (user_id, kind, payload_json, created_at, expires_at) "
                 "VALUES (1, 'variant', '{}', ?, ?)", (past, past))
    conn.close()


def pending_count(path: Path) -> int:
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        return conn.execute("SELECT COUNT(*) FROM pending").fetchone()[0]
    finally:
        conn.close()


def test_hourly_cleanup_reaches_only_running_lanes(house):
    async def go():
        await house.say(CHAT_A, "a1")
        await house.added(CHAT_A, "a1")
        # Expired questions in both files; only A is running.
        expire_a_question(house.file(house.a))
        expire_a_question(house.file(house.b))
        opened_before = len(house.opened_household_files())
        await house.router.cleanup()
        await house.until(lambda: pending_count(house.file(house.a)) == 0, "A's expired question to go")
        await asyncio.sleep(0.05)
        assert house.router.running == {house.a}, "the cleanup must not start a stopped household's lane"
        return opened_before
    opened_before = run(house, go)
    assert pending_count(house.file(house.b)) == 1, "the cleanup must not reach a stopped household"
    assert house.opened_household_files()[opened_before:] == [], "the cleanup opened a household file"
