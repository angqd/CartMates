import logging
import re
import sqlite3

from telegram import Update
from telegram.ext import Application, CallbackQueryHandler, ContextTypes, MessageHandler, filters

from queuify.domain import group
from queuify.lane import Lane
from queuify.telegram.events import CallbackTap, Message
from queuify.telegram.gate import is_for_bot, parse_command
from queuify.telegram.outbox import Outbox

log = logging.getLogger(__name__)

ALLOWED_UPDATES = ["message", "callback_query"]


class TelegramAdapter:
    """Turns PTB updates into events and submits them to the lane."""

    def __init__(self, lane: Lane, outbox: Outbox, group_id: int | None,
                 bot_id: int, bot_username: str, conn: sqlite3.Connection | None = None):
        self.lane = lane
        self.outbox = outbox
        self._group_default = group_id  # the .env value; None with nothing in kv means setup mode
        self.conn = conn  # for the group id that kv may override (DESIGN 7.4)
        self.bot_id = bot_id
        self.bot_username = bot_username

    @property
    def group_id(self) -> int | None:
        return group.current(self.conn, self._group_default)

    def register(self, app: Application) -> None:
        app.add_handler(MessageHandler(filters.StatusUpdate.MIGRATE, self.on_migrate))
        app.add_handler(MessageHandler(filters.TEXT, self.on_update))
        app.add_handler(CallbackQueryHandler(self.on_update))

    async def on_migrate(self, update: Update, context: ContextTypes.DEFAULT_TYPE | None = None) -> None:
        """The group became a supergroup. Telegram says so in the old chat (migrate_to_chat_id) and
        again in the new one (migrate_from_chat_id); either is enough, and only for our group."""
        msg = update.effective_message
        current = self.group_id
        if not msg or not self.conn or current is None:
            return
        if msg.migrate_to_chat_id and msg.chat.id == current:
            group.record_migration(self.conn, msg.migrate_to_chat_id)
        elif msg.migrate_from_chat_id and msg.migrate_from_chat_id == current:
            group.record_migration(self.conn, msg.chat.id)

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
        if not msg.text or not user or not is_for_bot(msg, self.bot_id, self.bot_username):
            return
        command = parse_command(msg)

        if self.group_id is None:
            # Setup mode: the only thing we do is tell the owner the chat id.
            if command and command[0] == "status" and chat.type in ("group", "supergroup"):
                await self.outbox.send(
                    chat.id,
                    f"Setup: this chat's id is {chat.id}. "
                    "Put it in .env as TELEGRAM_GROUP_ID and restart.",
                    reply_to=msg.message_id,
                )
            return
        if chat.id != self.group_id:
            return

        reply = msg.reply_to_message
        await self.lane.submit(Message(
            chat_id=chat.id, message_id=msg.message_id, user_id=user.id,
            display_name=user.full_name, text=self._strip_mention(msg.text),
            is_command=command is not None,
            command=command[0] if command else None,
            args=command[1] if command else "",
            reply_to_message_id=reply.message_id if reply else None,
            reply_to_bot=bool(reply and reply.from_user and reply.from_user.id == self.bot_id),
        ))

    def _strip_mention(self, text: str) -> str:
        # "@queuify_home_bot add milk" -> "add milk": the mention is addressing, not content,
        # and keeping it would clutter the agent's transcript.
        text = re.sub(rf"@{re.escape(self.bot_username)}\b", "", text, flags=re.IGNORECASE)
        return re.sub(r"\s{2,}", " ", text).strip()

    async def _on_callback(self, update: Update) -> None:
        query = update.callback_query
        # Button taps always pass the gate, but still only from our group.
        if self.group_id is None or not query.message or query.message.chat.id != self.group_id:
            return
        await self.lane.submit(CallbackTap(
            chat_id=query.message.chat.id, message_id=query.message.message_id,
            user_id=query.from_user.id, display_name=query.from_user.full_name,
            data=query.data or "", tap_id=query.id,
        ))
