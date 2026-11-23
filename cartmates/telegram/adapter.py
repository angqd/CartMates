import logging
import re
from typing import Protocol

from telegram import Update
from telegram.ext import Application, CallbackQueryHandler, ContextTypes, MessageHandler, filters

from cartmates.telegram.events import CallbackTap, Event, Message
from cartmates.telegram.gate import is_for_bot, parse_command

log = logging.getLogger(__name__)

ALLOWED_UPDATES = ["message", "callback_query"]


class Router(Protocol):
    """What the adapter hands events to: the LaneRouter (cartmates/router.py), or a stand-in in tests."""
    async def submit(self, event: Event, *, private: bool = False) -> None: ...
    def migrate(self, old_chat_id: int, new_chat_id: int) -> None: ...


class TelegramAdapter:
    """Turns PTB updates into events and submits them to the router, which picks the household."""

    def __init__(self, router: Router, bot_id: int, bot_username: str):
        self.router = router
        self.bot_id = bot_id
        self.bot_username = bot_username

    def register(self, app: Application) -> None:
        app.add_handler(MessageHandler(filters.StatusUpdate.MIGRATE, self.on_migrate))
        app.add_handler(MessageHandler(filters.TEXT, self.on_update))
        app.add_handler(CallbackQueryHandler(self.on_update))

    async def on_migrate(self, update: Update, context: ContextTypes.DEFAULT_TYPE | None = None) -> None:
        """The group became a supergroup. Telegram says so in the old chat (migrate_to_chat_id) and
        again in the new one (migrate_from_chat_id); either is enough. The router ignores chats it doesn't
        know and a notice it has already applied."""
        msg = update.effective_message
        if not msg:
            return
        if msg.migrate_to_chat_id:
            self.router.migrate(msg.chat.id, msg.migrate_to_chat_id)
        elif msg.migrate_from_chat_id:
            self.router.migrate(msg.migrate_from_chat_id, msg.chat.id)

    async def on_update(self, update: Update, context: ContextTypes.DEFAULT_TYPE | None = None) -> None:
        if update.callback_query:
            await self._on_callback(update)
        elif update.effective_message:
            await self._on_message(update)

    async def _on_message(self, update: Update) -> None:
        msg = update.effective_message
        chat = update.effective_chat
        user = update.effective_user
        # Every message Telegram delivers, before the gate. Use LOG_LEVEL=DEBUG to see
        # whether a message never arrived (Telegram privacy mode) or was gated out by us.
        log.debug("update chat=%s user=%s reply_to=%s text=%.40r",
                  chat.id if chat else None, user.id if user else None,
                  msg.reply_to_message.from_user.id
                  if msg.reply_to_message and msg.reply_to_message.from_user else None,
                  msg.text)
        private = bool(chat and chat.type == "private")
        # In a private chat every message is addressed to the bot; in a group the gate decides.
        if not msg.text or not user or not (private or is_for_bot(msg, self.bot_id, self.bot_username)):
            return
        command = parse_command(msg)
        reply = msg.reply_to_message
        await self.router.submit(Message(
            chat_id=chat.id, message_id=msg.message_id, user_id=user.id,
            display_name=user.full_name, text=self._strip_mention(msg.text),
            is_command=command is not None,
            command=command[0] if command else None,
            args=command[1] if command else "",
            reply_to_message_id=reply.message_id if reply else None,
            reply_to_bot=bool(reply and reply.from_user and reply.from_user.id == self.bot_id),
        ), private=private)

    def _strip_mention(self, text: str) -> str:
        # "@cartmates_home_bot add milk" -> "add milk": the mention is addressing, not content,
        # and keeping it would clutter the agent's transcript.
        text = re.sub(rf"@{re.escape(self.bot_username)}\b", "", text, flags=re.IGNORECASE)
        return re.sub(r"\s{2,}", " ", text).strip()

    async def _on_callback(self, update: Update) -> None:
        query = update.callback_query
        # Button taps always pass the gate; the router decides which household (if any) gets them.
        if not query.message:
            return
        await self.router.submit(CallbackTap(
            chat_id=query.message.chat.id, message_id=query.message.message_id,
            user_id=query.from_user.id, display_name=query.from_user.full_name,
            data=query.data or "", tap_id=query.id,
        ), private=query.message.chat.type == "private")
