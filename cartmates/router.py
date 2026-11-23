"""The LaneRouter (DESIGN 17.4, 17.8.1): finds the household for each event and hands it to that household's
lane, starting the lane on first use and stopping it when idle. Chats that reach no working household get a
fixed reply here, with no LLM call and no household file opened."""
import asyncio
import logging
import sqlite3
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path

from cartmates import registry
from cartmates.boot import Recovered
from cartmates.domain.group import GroupChat
from cartmates.handlers import Handlers
from cartmates.lane import Lane
from cartmates.store import db
from cartmates.telegram.events import CallbackTap, CleanupPending, Event, Message, Recover
from cartmates.telegram.outbox import Outbox

log = logging.getLogger("cartmates.router")

LANE_IDLE_S = 1800.0
CLEANUP_EVERY_S = 3600.0
IDLE_CHECK_S = 60.0

UNKNOWN_GROUP = "This group isn't set up yet (chat id {chat_id}). Ask the host to add it."
PRIVATE_CHAT = "I only work in your household group."
BROKEN = "This group's data can't be opened right now. The host has been told."

MakeHandlers = Callable[[int, sqlite3.Connection, GroupChat], Handlers]
OpenHousehold = Callable[[int], sqlite3.Connection]


def household_opener(path_for: Callable[[int], Path]) -> OpenHousehold:
    """Opens households/<id>.db and applies any migrations it lacks (a no-op after the boot pass). db.connect
    is looked up on each call, so a test can wrap it."""
    def open_household(household_id: int) -> sqlite3.Connection:
        conn = db.connect(path_for(household_id))
        try:
            db.migrate(conn)
        except Exception:
            conn.close()
            raise
        return conn
    return open_household


@dataclass
class _Running:
    household_id: int
    conn: sqlite3.Connection
    group: GroupChat
    handlers: Handlers
    lane: Lane
    last_event: float
    in_flight: int = 0  # submitted and not yet finished, so queued events count too


class LaneRouter:
    def __init__(self, reg: sqlite3.Connection, outbox: Outbox, make_handlers: MakeHandlers,
                 open_household: OpenHousehold, *, idle_s: float = LANE_IDLE_S,
                 cleanup_every_s: float = CLEANUP_EVERY_S, idle_check_s: float = IDLE_CHECK_S,
                 clock: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], Awaitable[None]] = asyncio.sleep):
        self.reg = reg
        self.outbox = outbox
        self._make_handlers = make_handlers
        self._open = open_household
        self.idle_s = idle_s
        self.cleanup_every_s = cleanup_every_s
        self.idle_check_s = idle_check_s
        self._clock = clock
        self._sleep = sleep
        self._running: dict[int, _Running] = {}
        self._stopping: set[asyncio.Task] = set()  # lanes stopped for idleness, finishing their last event
        self._broken_logged: set[int] = set()
        self._migrated_from: set[int] = set()  # old ids of chats that became supergroups
        self._tasks: list[asyncio.Task] = []

    # ---- lifecycle ----

    def start(self) -> None:
        """Start the hourly cleanup and the idle check. Tests can call cleanup() and stop_idle() instead."""
        self._tasks = [asyncio.create_task(self._every(self.cleanup_every_s, self.cleanup)),
                       asyncio.create_task(self._every(self.idle_check_s, self.stop_idle))]

    async def stop(self) -> None:
        """Shutdown: stop the background loops, then every lane (each finishes its event in progress)."""
        for t in self._tasks:
            t.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks = []
        running, self._running = list(self._running.values()), {}
        await asyncio.gather(*(self._stop_lane(r) for r in running))
        await asyncio.gather(*self._stopping, return_exceptions=True)

    @property
    def running(self) -> set[int]:
        return set(self._running)

    def handlers_for(self, household_id: int) -> Handlers | None:
        r = self._running.get(household_id)
        return r.handlers if r else None

    # ---- routing ----

    def route(self, chat_id: int, platform: str = "telegram") -> int | None:
        return registry.household_for(self.reg, chat_id, platform)

    async def submit(self, event: Event, *, private: bool = False) -> None:
        """Every event from the adapter. private: it came from a private chat with the bot."""
        if private:
            await self._reply_outside(event, PRIVATE_CHAT)
            return
        if event.chat_id in self._migrated_from:
            log.debug("dropping %s from chat %s, which became a supergroup", type(event).__name__, event.chat_id)
            return
        hid = self.route(event.chat_id)
        status = registry.status(self.reg, hid) if hid is not None else None
        if status == "broken":
            await self._reply_broken(hid, event)
            return
        if status != "active":
            await self._reply_outside(event, UNKNOWN_GROUP.format(chat_id=event.chat_id))
            return
        r = await self._ensure(hid)
        if r is None:
            await self._reply_broken(hid, event)
            return
        await self._submit_to(r, event)

    async def recover(self, household_id: int, recovered: Recovered) -> None:
        """Boot: start the lane of a household whose orders were settled at boot, so the notice gets posted."""
        r = await self._ensure(household_id)
        if r is None:
            return
        chat_id = r.group.current()
        if chat_id is None:
            log.warning("household %s recovered %d order(s) but has no chat to tell", household_id, len(recovered))
            return
        await self._submit_to(r, Recover(chat_id, tuple(recovered.unknown), tuple(recovered.failed)))

    def migrate(self, old_chat_id: int, new_chat_id: int) -> None:
        """A supergroup upgrade, seen by the adapter or the outbox. Sync: the outbox calls it mid-send."""
        hid = self.route(old_chat_id)
        if hid is None:  # a repeat notice (already moved), or a chat that isn't ours
            return
        registry.remap_chat(self.reg, hid, new_chat_id)
        if old_chat_id != new_chat_id:
            self._migrated_from.add(old_chat_id)

    # ---- the hourly cleanup and the idle stop ----

    async def cleanup(self) -> None:
        """Queue CleanupPending on running lanes only, so the ticker never wakes a household."""
        for r in list(self._running.values()):
            chat_id = r.group.current()
            if chat_id is not None:
                await self._submit_to(r, CleanupPending(chat_id), touch=False)

    async def stop_idle(self) -> None:
        """Stop lanes with nothing queued or running and no event for idle_s. Stopping closes the connection."""
        now = self._clock()
        for hid, r in list(self._running.items()):
            if r.in_flight == 0 and now - r.last_event >= self.idle_s:
                # Out of the map first: an event arriving while this lane winds down starts a fresh one.
                del self._running[hid]
                log.info("household %s idle for %.0f s: stopping its lane", hid, now - r.last_event)
                await self._stop_lane(r)

    # ---- internals ----

    async def _ensure(self, household_id: int) -> _Running | None:
        """The household's running lane, started if needed. Nothing here yields before the lane is in the map
        and its CleanupPending is queued (Lane.submit on an unbounded queue doesn't suspend), so two events
        can't start two lanes and the cleanup always runs first."""
        r = self._running.get(household_id)
        if r is not None:
            return r
        try:
            conn = self._open(household_id)
        except Exception:
            log.exception("household %s failed to open; marking it broken", household_id)
            registry.set_status(self.reg, household_id, "broken")
            self._broken_logged.add(household_id)
            return None
        group = GroupChat(self.reg, household_id)
        try:
            handlers = self._make_handlers(household_id, conn, group)
        except Exception:
            conn.close()
            raise
        lane = Lane(self._handler(household_id, handlers), self.outbox, household_id=household_id)
        r = _Running(household_id, conn, group, handlers, lane, self._clock())
        handlers.resubmit = lambda event, r=r: self._submit_to(r, event)  # this household's own lane
        self._running[household_id] = r
        lane.start()
        log.info("household %s: lane started", household_id)
        chat_id = group.current()
        if chat_id is not None:  # a starting lane tidies up first
            await self._submit_to(r, CleanupPending(chat_id), touch=False)
        return r

    def _handler(self, household_id: int, handlers: Handlers):
        async def handle(event: Event) -> None:
            try:
                await handlers.handle(event)
            finally:
                r = self._running.get(household_id)
                if r is not None and r.handlers is handlers:
                    r.in_flight -= 1
                    r.last_event = self._clock()
        return handle

    async def _submit_to(self, r: _Running, event: Event, touch: bool = True) -> None:
        r.in_flight += 1
        if touch:
            r.last_event = self._clock()
        await r.lane.submit(event)

    async def _stop_lane(self, r: _Running) -> None:
        try:
            await r.lane.stop()
        finally:
            r.conn.close()
            log.info("household %s: lane stopped", r.household_id)

    async def _every(self, seconds: float, action: Callable[[], Awaitable[None]]) -> None:
        while True:
            await self._sleep(seconds)
            try:
                await action()
            except Exception:
                log.exception("router background job failed")

    async def _reply_broken(self, household_id: int, event: Event) -> None:
        if household_id not in self._broken_logged:
            self._broken_logged.add(household_id)
            log.error("household %s is broken; its group (chat %s) gets the offline reply",
                      household_id, event.chat_id)
        await self._reply_outside(event, BROKEN)

    async def _reply_outside(self, event: Event, text: str) -> None:
        """A fixed reply from no household: no LLM, no household file."""
        try:
            if isinstance(event, Message):
                # Never followed to a new chat id: this reply is only for the chat that asked.
                await self.outbox.send(event.chat_id, text, reply_to=event.message_id, follow_migration=False)
            elif isinstance(event, CallbackTap):
                await self.outbox.answer_tap(event.tap_id, text)
        except Exception:
            log.exception("could not send the reply to chat %s", event.chat_id)
