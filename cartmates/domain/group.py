"""The household group's chat id (DESIGN 7.4, 17.2). It lives in registry.chats. If Telegram upgrades the
group to a supergroup its id changes and the registry row moves with it. Everything that needs the id
calls current() when it uses it."""
import sqlite3

from cartmates import registry


class GroupChat:
    """The chat registered for one household, read from the registry each time."""

    def __init__(self, reg: sqlite3.Connection, household_id: int):
        self.reg, self.household_id = reg, household_id

    def current(self) -> int | None:
        return registry.chat_for(self.reg, self.household_id)

    def record_migration(self, new_id: int) -> None:
        registry.remap_chat(self.reg, self.household_id, new_id)


class FixedGroup:
    """A chat id held in memory: tests."""

    def __init__(self, chat_id: int | None):
        self.chat_id = chat_id

    def current(self) -> int | None:
        return self.chat_id

    def record_migration(self, new_id: int) -> None:
        self.chat_id = new_id


Group = GroupChat | FixedGroup


def wrap(group: "int | None | Group") -> "Group":
    return group if isinstance(group, (GroupChat, FixedGroup)) else FixedGroup(group)
