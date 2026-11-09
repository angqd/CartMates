from dataclasses import dataclass


@dataclass(frozen=True)
class Message:
    chat_id: int
    message_id: int
    user_id: int
    display_name: str
    text: str
    is_command: bool = False
    command: str | None = None  # lowercase, without "/" or "@bot"
    args: str = ""
    reply_to_message_id: int | None = None  # the message this one replies to, if any
    reply_to_bot: bool = False  # true when that message is from our bot


@dataclass(frozen=True)
class CallbackTap:
    """A button press. tap_id is Telegram's callback query id, needed to stop the button's spinner."""
    chat_id: int
    message_id: int
    user_id: int
    display_name: str
    data: str
    tap_id: str = ""


@dataclass(frozen=True)
class Recover:
    """Internal: after a restart, settle the orders that were in flight (DESIGN 7.4)."""
    chat_id: int
    unknown: tuple[int, ...] = ()   # were checking out: ask Swiggy what happened
    failed: tuple[int, ...] = ()    # never reached checkout: just tell the group


@dataclass(frozen=True)
class CleanupPending:
    """Internal, hourly: delete expired variant and scope questions."""
    chat_id: int


Event = Message | CallbackTap | Recover | CleanupPending
