"""Error callback for Telegram long polling: one WARNING line instead of a traceback."""
import logging
import re
import time
from collections.abc import Callable

log = logging.getLogger(__name__)

REPEAT_AFTER_S = 60.0
_TOKEN_SHAPE = re.compile(r"\d{6,}:[A-Za-z0-9_-]{20,}")


class PollingErrorLogger:
    """Pass an instance as start_polling(error_callback=...). The same message is logged at most
    once a minute. The bot token is replaced by [token] in case an error text ever carries it."""

    def __init__(self, token: str | None = None, clock: Callable[[], float] = time.monotonic):
        self._token, self._clock = token, clock
        self._last: dict[str, float] = {}

    def redact(self, text: str) -> str:
        if self._token:
            text = text.replace(self._token, "[token]")
        return _TOKEN_SHAPE.sub("[token]", text)

    def __call__(self, error: Exception) -> None:
        text = self.redact(f"{type(error).__name__}: {error}")
        now = self._clock()
        last = self._last.get(text)
        if last is not None and now - last < REPEAT_AFTER_S:
            return
        self._last = {k: t for k, t in self._last.items() if now - t < REPEAT_AFTER_S}  # forget old ones
        self._last[text] = now
        log.warning("telegram polling: %s", text)
