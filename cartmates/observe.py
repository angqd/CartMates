"""Ids for the current lane event: a short turn id and the trace id. Both are stored on events rows."""
import logging
import secrets
from contextvars import ContextVar

from opentelemetry import trace

turn_id: ContextVar[str | None] = ContextVar("turn_id", default=None)
household_id: ContextVar[int | None] = ContextVar("household_id", default=None)


def start_turn() -> str:
    tid = secrets.token_hex(6)  # 12 hex chars
    turn_id.set(tid)
    return tid


class TurnIdFilter(logging.Filter):
    """Adds %(household)s, %(turn_id)s and %(trace_id)s to every record; "-" outside a lane event."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.turn_id = turn_id.get() or "-"
        hh = household_id.get()
        record.household = "-" if hh is None else f"hh-{hh}"
        record.trace_id = current_trace_id() or "-"
        return True


def current_trace_id() -> str | None:
    """The active span's trace id as 32 hex chars, or None when there is no valid span."""
    ctx = trace.get_current_span().get_span_context()
    return format(ctx.trace_id, "032x") if ctx.is_valid else None


def looks_like_callback_paste(text: str) -> bool:
    """A pasted Swiggy login redirect URL: it holds the auth code."""
    return "/callback?" in text and "code=" in text


def looks_like_wrong_paste(text: str) -> bool:
    """The Swiggy sign-in page pasted instead of the final redirect URL."""
    return "mcp.swiggy.com/auth" in text and not looks_like_callback_paste(text)


def looks_like_paste(text: str) -> bool:
    """Any pasted login URL. It must never be stored or traced."""
    return looks_like_callback_paste(text) or looks_like_wrong_paste(text)
