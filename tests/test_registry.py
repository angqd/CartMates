"""registry.py (DESIGN 17.2): households and the chats that reach them."""
from cartmates import registry


def test_household_ids_are_never_reused_after_a_delete(tmp_path):
    path = tmp_path / "registry.db"
    reg = registry.open(path)
    a, b = registry.create_household(reg, setup_by=42), registry.create_household(reg)
    registry.set_status(reg, b, "deleted")
    reg.execute("DELETE FROM households WHERE id=?", (b,))
    reg.close()

    reg = registry.open(path)  # reopening migrates nothing new and keeps the counter
    c = registry.create_household(reg)
    assert (a, b, c) == (1, 2, 3)
    assert registry.household_ids(reg) == [1, 3]
    row = reg.execute("SELECT setup_by, created_at FROM households WHERE id=1").fetchone()
    assert row["setup_by"] == 42 and row["created_at"].endswith("+00:00")


def test_household_ids_filter_by_status():
    reg = registry.open(":memory:")
    for _ in range(3):
        registry.create_household(reg)
    registry.set_status(reg, 2, "broken")
    assert registry.household_ids(reg) == [1, 3]
    assert registry.household_ids(reg, "broken") == [2]


def test_unknown_chat_has_no_household():
    reg = registry.open(":memory:")
    registry.map_chat(reg, registry.create_household(reg), -100)
    assert registry.household_for(reg, -999) is None
    assert registry.household_for(reg, -100, platform="other") is None
    assert registry.chat_for(reg, 2) is None


def test_map_chat_is_idempotent():
    reg = registry.open(":memory:")
    hh = registry.create_household(reg)
    registry.map_chat(reg, hh, -100)
    registry.map_chat(reg, hh, -100)
    assert reg.execute("SELECT count(*) FROM chats").fetchone()[0] == 1
    assert registry.household_for(reg, -100) == hh and registry.chat_for(reg, hh) == -100
