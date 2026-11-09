import asyncio
import logging
from collections.abc import Awaitable, Callable

from cartmates import observe, tracing
from cartmates.telegram.events import Event
from cartmates.telegram.outbox import Outbox

log = logging.getLogger(__name__)

Handler = Callable[[Event], Awaitable[None]]


class Lane:
    """One FIFO queue and one worker, so events are handled strictly one at a time."""

    def __init__(self, handler: Handler, outbox: Outbox):
        self._handler = handler
        self._outbox = outbox
        self._queue: asyncio.Queue[Event | None] = asyncio.Queue()
        self._stopping = False
        self._worker: asyncio.Task | None = None

    def start(self) -> None:
        self._worker = asyncio.create_task(self._run())

    async def submit(self, event: Event) -> None:
        if self._stopping:
            log.warning("lane is stopping, dropping %r", event)
            return
        await self._queue.put(event)

    async def _run(self) -> None:
        while not self._stopping:
            event = await self._queue.get()
            if event is None:  # wake-up sentinel from stop()
                break
            with tracing.event_span(event) as sp:
                sp.set_attribute("cartmates.turn_id", observe.start_turn())
                text = getattr(event, "text", None)
                paste = text is not None and observe.looks_like_paste(text)
                if paste:
                    sp.set_attribute("cartmates.redacted", True)
                else:
                    tracing.set_text(sp, "input.value", text)
                try:
                    await self._handler(event)
                except Exception as e:
                    log.exception("handler failed for %r", event)
                    tracing.fail(sp, e, redact=paste)
                    try:
                        await self._outbox.send(event.chat_id, "Something went wrong, try again.")
                    except Exception:
                        log.exception("could not send the error message")

    async def stop(self) -> None:
        """Let the event in progress finish, drop the rest."""
        self._stopping = True
        while not self._queue.empty():
            dropped = self._queue.get_nowait()
            if dropped is not None:
                log.warning("dropping queued event on shutdown: %r", dropped)
        self._queue.put_nowait(None)
        if self._worker:
            await self._worker
