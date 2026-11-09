class LoggedOut(Exception):
    """No usable Swiggy login; a human must log in again."""


class LoggedOutAfterChanges(LoggedOut):
    """Swiggy logged out partway through an agent turn that had already changed the queue.
    Replaying the message would repeat those changes, so it must not be parked."""

    def __init__(self, actions: list[str]):
        super().__init__("logged out after changes")
        self.actions = actions


class SwiggyUnavailable(Exception):
    """Network failure or a 5xx; worth retrying later."""


class LoginError(Exception):
    """A paste-back login failed. The message is safe to show in the group."""


class SwiggyToolError(Exception):
    """The tool ran but reported an error."""
