import asyncio
import html
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Protocol, TypeVar

import httpx
from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import ChatMigrated, NetworkError, TimedOut

from cartmates import tracing

log = logging.getLogger(__name__)
T = TypeVar("T")

ATTEMPTS = 3  # tries in total per call
BACKOFF = (1.0, 2.0, 4.0)  # seconds slept before try 2, 3, ...; a longer ATTEMPTS would use the 4 s


def is_connect_error(exc: BaseException) -> bool:
    """True when a request never reached Telegram, so repeating it cannot post twice. python-telegram-bot
    raises NetworkError("httpx.ConnectError: ...") from the httpx error; TimedOut is a NetworkError too
    but says nothing about whether the request arrived, so it is not a connect error."""
    if not isinstance(exc, NetworkError) or isinstance(exc, TimedOut):
        return False
    seen: set[int] = set()
    err: BaseException | None = exc
    while err is not None and id(err) not in seen:  # __cause__/__context__ chain, cycle-safe
        if isinstance(err, httpx.ConnectError):
            return True
        seen.add(id(err))
        err = err.__cause__ or err.__context__
    message = str(exc)  # no chain available: fall back to the class name PTB puts in the message
    return message.startswith("ConnectError") or message.startswith("httpx.ConnectError")

@dataclass(frozen=True)
class UrlButton:
    """A button that opens a link instead of sending a tap."""
    label: str
    url: str


@dataclass(frozen=True)
class Mention:
    """A line that names a person so they get notified: 'Name: text'. Plain data; only the Telegram
    outbox turns it into markup."""
    user_id: int
    name: str
    text: str


# Rows of buttons: (label, callback_data) pairs, or UrlButton for a link.
Button = tuple[str, str] | UrlButton
Buttons = list[list[Button]]


class Outbox(Protocol):
    async def send(self, chat_id: int, text: str, reply_to: int | None = None,
                   buttons: Buttons | None = None, mentions: list[Mention] | None = None) -> int: ...
    async def edit(self, chat_id: int, message_id: int, text: str,
                   buttons: Buttons | None = None) -> None: ...
    async def answer_tap(self, tap_id: str, text: str | None = None, alert: bool = False) -> None: ...
    async def delete(self, chat_id: int, message_id: int) -> None: ...


def _markup(buttons: Buttons | None) -> InlineKeyboardMarkup | None:
    if not buttons:
        return None
    return InlineKeyboardMarkup([[_button(b) for b in row] for row in buttons])


def _button(b: Button) -> InlineKeyboardButton:
    if isinstance(b, UrlButton):
        return InlineKeyboardButton(b.label, url=b.url)
    label, data = b
    return InlineKeyboardButton(label, callback_data=data)


def _with_mentions(text: str, mentions: list[Mention]) -> str:
    """HTML for `text` followed by one line per mention. Everything but the link markup is escaped.
    tg://user?id= links notify people who have no username."""
    lines = [f'<a href="tg://user?id={int(m.user_id)}">{html.escape(m.name)}</a>: {html.escape(m.text)}'
             for m in mentions]
    return "\n\n".join(part for part in [html.escape(text), "\n".join(lines)] if part)


def _message_span(name: str, chat_id: int, text: str):
    """Chat id, length and (content capture on) the text. Never the bot token. Text that holds a link
    is left out: it can be the Swiggy login link."""
    span = tracing.span(name, tracing.CHAIN, {"session.id": str(chat_id), "telegram.text_length": len(text)})
    return span, ("http" not in text)


class TelegramOutbox:
    def __init__(self, bot: Bot, on_migrated: Callable[[int], None] | None = None,
                 sleep: Callable[[float], Awaitable[None]] = asyncio.sleep):
        self._bot = bot
        self._sleep = sleep  # injectable so tests don't wait out the backoff
        self._on_migrated = on_migrated  # told the new chat id when Telegram says the group moved

    def _moved(self, e: ChatMigrated) -> int:
        if self._on_migrated:
            self._on_migrated(e.new_chat_id)
        return e.new_chat_id

    async def _retry(self, call: Callable[[], Awaitable[T]], retry_timeouts: bool) -> T:
        """Run `call`, repeating it on a connect failure (and on TimedOut when `retry_timeouts`)."""
        for n in range(1, ATTEMPTS + 1):
            try:
                return await call()
            except NetworkError as e:
                if n == ATTEMPTS or not (is_connect_error(e) or (retry_timeouts and isinstance(e, TimedOut))):
                    raise
                delay = BACKOFF[n - 1]
                log.warning("telegram %s, retrying in %gs (attempt %d of %d)", type(e).__name__, delay, n + 1, ATTEMPTS)
                tracing.event("retry", attempt=n + 1, error=type(e).__name__)
                await self._sleep(delay)
        raise AssertionError("unreachable")

    async def send(self, chat_id: int, text: str, reply_to: int | None = None,
                   buttons: Buttons | None = None, mentions: list[Mention] | None = None) -> int:
        body, mode = (_with_mentions(text, mentions), "HTML") if mentions else (text, None)

        async def attempt(chat: int) -> int:
            async def once() -> int:
                sent = await self._bot.send_message(
                    chat, body, reply_to_message_id=reply_to, allow_sending_without_reply=True,
                    reply_markup=_markup(buttons), parse_mode=mode)
                return sent.message_id
            # No retry on TimedOut: the message may have arrived, and a retry could post it twice.
            return await self._retry(once, retry_timeouts=False)
        cm, plain = _message_span("telegram.send", chat_id, text)
        with cm as sp:
            if plain:
                tracing.set_text(sp, "output.value", text)
            try:
                return await attempt(chat_id)
            except ChatMigrated as e:  # the group became a supergroup: retry once to the new id
                return await attempt(self._moved(e))

    async def edit(self, chat_id: int, message_id: int, text: str,
                   buttons: Buttons | None = None) -> None:
        # Editing without a reply_markup also removes the buttons.
        async def attempt(chat: int) -> None:
            # Same text and buttons again is harmless (Telegram's "not modified" is not a NetworkError),
            # so a TimedOut edit is retried too.
            await self._retry(lambda: self._bot.edit_message_text(
                text, chat_id=chat, message_id=message_id, reply_markup=_markup(buttons)), retry_timeouts=True)
        cm, plain = _message_span("telegram.edit", chat_id, text)
        with cm as sp:
            if plain:
                tracing.set_text(sp, "output.value", text)
            try:
                await attempt(chat_id)
            except ChatMigrated as e:
                await attempt(self._moved(e))

    async def answer_tap(self, tap_id: str, text: str | None = None, alert: bool = False) -> None:
        # Answering twice is harmless, so a TimedOut answer is retried as well.
        await self._retry(lambda: self._bot.answer_callback_query(tap_id, text=text, show_alert=alert),
                          retry_timeouts=True)

    async def delete(self, chat_id: int, message_id: int) -> None:
        await self._bot.delete_message(chat_id, message_id)


@dataclass(frozen=True)
class Sent:
    chat_id: int
    text: str
    reply_to: int | None
    buttons: Buttons | None = None
    mentions: list[Mention] | None = None


class FakeOutbox:
    """Records calls instead of calling Telegram, for tests."""

    def __init__(self):
        self.sent: list[Sent] = []
        self.edited: list[tuple[int, int, str]] = []
        self.edited_buttons: list[Buttons | None] = []  # parallel to `edited`
        self._next_id = 1000
        self.answers: list[tuple[str, str | None, bool]] = []
        self.deleted: list[tuple[int, int]] = []
        self._failures: list[list] = []  # [matches(Sent) -> bool, exception, times left or None]

    def fail_sends(self, exc: Exception, when: Callable[["Sent"], bool] = lambda sent: True,
                   times: int | None = None) -> None:
        """Make matching sends raise `exc` (the next `times` of them, or every one when None).
        A failed send is not recorded in `sent`."""
        self._failures.append([when, exc, times])

    async def send(self, chat_id: int, text: str, reply_to: int | None = None,
                   buttons: Buttons | None = None, mentions: list[Mention] | None = None) -> int:
        attempt = Sent(chat_id, text, reply_to, buttons, mentions)
        for failure in self._failures:
            when, exc, left = failure
            if left != 0 and when(attempt):
                if left is not None:
                    failure[2] = left - 1
                raise exc
        self.sent.append(attempt)
        self._next_id += 1
        return self._next_id - 1

    async def edit(self, chat_id: int, message_id: int, text: str,
                   buttons: Buttons | None = None) -> None:
        self.edited.append((chat_id, message_id, text))
        self.edited_buttons.append(buttons)

    async def answer_tap(self, tap_id: str, text: str | None = None, alert: bool = False) -> None:
        self.answers.append((tap_id, text, alert))

    async def delete(self, chat_id: int, message_id: int) -> None:
        self.deleted.append((chat_id, message_id))
