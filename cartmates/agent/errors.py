MAX_ACTIONS_SHOWN = 5


def done_so_far(actions: list[str]) -> str:
    """The actions as one line for a reply: the first five, then "and N more"."""
    shown = actions[:MAX_ACTIONS_SHOWN]
    more = len(actions) - len(shown)
    return "; ".join(shown) + (f"; and {more} more" if more else "")


class TurnFailedAfterChanges(Exception):
    """An agent turn failed (network drop, bug, ...) after tools had already changed the queue.
    Carries the action lines so the handler can say what was done. The message and the actions are
    already in chat_memory, and the message must not be replayed."""

    def __init__(self, actions: list[str], cause: BaseException):
        super().__init__(f"turn failed after changes: {type(cause).__name__}")
        self.actions = actions
        self.cause = cause
