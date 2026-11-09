"""The agent loop, queue tools and variant buttons. No network: ScriptedLLM plays the model and
the fake Swiggy servers from test_swiggy.py play Swiggy, so the real lane, handlers, tools and SQL run."""
import asyncio
import json
from datetime import datetime, timedelta, timezone

import pytest

from cartmates.agent import memory
from cartmates.agent.llm import LLMTurn, LLMUnavailable, ScriptedLLM, ToolCall
from cartmates.agent.loop import APOLOGY_DOWN, APOLOGY_TOO_LONG, MAX_ROUNDS
from cartmates.agent.prompt import SYSTEM_PROMPT
from cartmates.agent.tools import SCHEMAS
from cartmates.domain import queue
from cartmates.handlers import Handlers
from cartmates.lane import Lane
from cartmates.store import db
from cartmates.telegram.adapter import TelegramAdapter
from cartmates.telegram.events import CallbackTap
from cartmates.telegram.outbox import FakeOutbox
from test_swiggy import Rig, kv, pending_login, command
from test_telegram import BOT_ID, BOT_NAME, GROUP, make_update

MENTION = [{"type": "mention", "offset": 0, "length": len("@CartMatesBot")}]
# The fake catalog: p1v1 Farmfresh milk 500 ml x 4 (max 1), p1v2 1 L (out of stock), p2v1 Meadow Oat Drink (max 5).
OAT_PER_UNIT = 4000


def call(name, cid=None, **args):
    return ToolCall(cid or f"c-{name}", name, json.dumps(args))


def use(*calls):
    return LLMTurn(tool_calls=list(calls))


def say(text):
    return LLMTurn(text=text)


@pytest.fixture
def rig(tmp_path):
    c = db.connect(tmp_path / "t.db")
    db.migrate(c)
    r = Rig(tmp_path, c)
    r.seed()  # logged in
    return r


class AgentFlow:
    def __init__(self, rig, turns):
        self.rig = rig
        self.conn = rig.conn
        self.llm = ScriptedLLM(turns)
        self.outbox = FakeOutbox()
        self.handlers = Handlers(rig.conn, self.outbox, GROUP, rig.auth, rig.client,
                                 "aaaa1111bbbb2222cccc__FAKEsuffix123", self.llm, 199)
        self.lane = Lane(self.handlers.handle, self.outbox)
        self.handlers.resubmit = self.lane.submit
        self.adapter = TelegramAdapter(self.lane, self.outbox, GROUP, BOT_ID, BOT_NAME)

    async def __aenter__(self):
        self.lane.start()
        return self

    async def __aexit__(self, *exc):
        await self.lane.stop()

    async def ask(self, text, **kw):
        await self.adapter.on_update(make_update(f"@CartMatesBot {text}", entities=MENTION, **kw))
        await asyncio.sleep(0.3)

    async def tap(self, data, user_id=1, name="Priya"):
        await self.lane.submit(CallbackTap(GROUP, 77, user_id, name, data, tap_id="tap1"))
        await asyncio.sleep(0.2)

    def texts(self):
        return [s.text for s in self.outbox.sent]

    def tool_output(self, round_index, call_id):
        """What the model was shown for a tool call, as of the given round's input."""
        items = self.llm.sent[round_index][1]
        return next(i["output"] for i in items if i.get("call_id") == call_id and i["type"] == "function_call_output")


def lines(conn):
    return [dict(r) for r in conn.execute("SELECT * FROM queue_lines ORDER BY id")]


def owners_of(conn, line_id):
    return [r[0] for r in conn.execute(
        "SELECT user_id FROM queue_line_owners WHERE line_id=? ORDER BY user_id", (line_id,))]


def add_member(conn, user_id, name):
    conn.execute("INSERT OR IGNORE INTO members (user_id, display_name, joined_at) VALUES (?, ?, 'x')",
                 (user_id, name))


def seed_line(conn, name, added_by, qty=1, owners=None, max_qty=12):
    with db.transaction(conn):
        return queue.add_line(conn, {"spin": "S", "sku": "K", "name": name, "size": "12 pcs",
                                     "price": 9000, "max_qty": max_qty}, qty, owners or [added_by], "x", added_by)[0]


def run(coro):
    return asyncio.run(coro)


# ---- adding ----

def test_single_clear_match_search_then_add(rig):
    add_member(rig.conn, 2, "Rohan")

    async def go():
        async with AgentFlow(rig, [
            use(call("search_catalog", query="oat drink")),
            use(call("add_to_queue", ref="p2v1", qty=2, owners=["me"], original_text="2 oat drink")),
            say("Added 2 Meadow Oat Drink."),
        ]) as f:
            await f.ask("2 oat drink")
            return f
    f = run(go())
    (line,) = lines(rig.conn)
    assert (line["name"], line["variant"], line["qty"], line["est_unit_price_paise"], line["added_by"]) == \
        ("Meadow Oat Drink", "200 ml", 2, OAT_PER_UNIT, 1)
    assert line["original_text"] == "2 oat drink" and line["status"] == "queued"
    assert owners_of(rig.conn, line["id"]) == [1]
    assert kv(rig.conn, "queue_version") == "1"
    kinds = [r[0] for r in rig.conn.execute("SELECT kind FROM events ORDER BY id")]
    assert kinds.count("tool_call") == 2 and kinds.count("queue_add") == 1
    args = json.loads(rig.conn.execute(
        "SELECT payload_json FROM events WHERE kind='tool_call' ORDER BY id DESC").fetchone()[0])
    assert args["name"] == "add_to_queue" and args["args"]["qty"] == 2  # name + args, not the result
    rows = [(r["role"], r["text"]) for r in rig.conn.execute("SELECT * FROM chat_memory ORDER BY id")]
    assert [r for r, _ in rows] == ["user", "action", "assistant"]
    assert rows[0][1] == "Priya (id 1): 2 oat drink" and rows[1][1].startswith("added #1 Meadow Oat Drink (200 ml) ×2")
    assert f.texts() == ["Added 2 Meadow Oat Drink."] and f.outbox.sent[0].reply_to == 10
    # The model saw the system prompt unchanged, a snapshot, the tagged message, then the tool results.
    assert all(sent[0] == SYSTEM_PROMPT for sent in f.llm.sent)
    first_input = f.llm.sent[0][1]
    assert "Members: Priya (id 1), Rohan (id 2)" in first_input[0]["content"]
    assert first_input[1]["content"].startswith("New message from Priya (id 1): 2 oat drink")
    assert f.tool_output(1, "c-search_catalog") == (
        "p1 Farmfresh Toned Milk: v1 500 ml x 4 ₹116 · v2 1 L ₹66.50 OUT\np2 Meadow Oat Drink: v1 200 ml ₹40\n"
        '(search "oat drink": 2 products returned, 2 shown)')
    assert f.tool_output(2, "c-add_to_queue").startswith("Added #1 Meadow Oat Drink (200 ml) ×2 for Priya")


def test_owners_me_and_rohan_resolved_in_code(rig):
    add_member(rig.conn, 2, "Rohan Sharma")

    async def go():
        async with AgentFlow(rig, [
            use(call("search_catalog", query="oat")),
            use(call("add_to_queue", ref="p2v1", qty=1, owners=["me", "rohan"], original_text="x")),
            say("ok"),
        ]) as f:
            await f.ask("oat for me and rohan")
            return f
    f = run(go())
    assert owners_of(rig.conn, 1) == [1, 2]
    assert "for Priya, Rohan Sharma" in f.tool_output(2, "c-add_to_queue")


def test_unknown_owner_name_is_an_error_the_model_sees(rig):
    async def go():
        async with AgentFlow(rig, [
            use(call("search_catalog", query="oat")),
            use(call("add_to_queue", ref="p2v1", qty=1, owners=["me", "Sneha"], original_text="x")),
            say("Ask Sneha to say hi first."),
        ]) as f:
            await f.ask("oat for me and sneha")
            return f
    f = run(go())
    assert lines(rig.conn) == []
    out = f.tool_output(2, "c-add_to_queue")
    assert out.startswith("error:") and "ask Sneha to say hi to me once" in out
    assert f.texts() == ["Ask Sneha to say hi first."]


def test_made_up_ref_is_rejected(rig):
    async def go():
        async with AgentFlow(rig, [
            use(call("add_to_queue", ref="p9v9", qty=1, original_text="x")),
            say("Let me search first."),
        ]) as f:
            await f.ask("add something")
            return f
    f = run(go())
    assert lines(rig.conn) == [] and kv(rig.conn, "queue_version") == "0"
    assert "Call search_catalog" in f.tool_output(1, "c-add_to_queue")


def test_quantity_and_stock_limits(rig):
    async def go():
        async with AgentFlow(rig, [
            use(call("search_catalog", query="x")),
            use(call("add_to_queue", "big", ref="p2v1", qty=6, original_text="x"),   # max_qty is 5
                call("add_to_queue", "oos", ref="p1v2", qty=1, original_text="x")),  # out of stock
            say("no"),
        ]) as f:
            await f.ask("x")
            return f
    f = run(go())
    assert lines(rig.conn) == []
    assert f.tool_output(2, "big") == "error: qty must be between 1 and 5"
    assert "out of stock" in f.tool_output(2, "oos")


# ---- ambiguity and buttons ----

def ambiguous_turns(owners=("me", "Rohan")):
    return [use(call("search_catalog", query="milk")),
            use(call("ask_variant", question="Which one?", refs=["p1v1", "p2v1"], qty=2,
                     owners=list(owners), original_text="milk")),
            say("Pick one above.")]


def test_ask_variant_sends_buttons_and_the_turn_goes_on_to_a_summary(rig):
    add_member(rig.conn, 2, "Rohan")

    async def go():
        async with AgentFlow(rig, ambiguous_turns()) as f:
            await f.ask("2 milk for me and rohan")
            return f
    f = run(go())
    assert len(f.llm.sent) == 3  # the turn goes on after ask_variant, to the model's summary
    assert f.tool_output(2, "c-ask_variant") == \
        "Question #1 sent; keep going with the other items, then write one short summary reply."
    msg, summary = f.outbox.sent
    assert summary.text == "Pick one above." and summary.buttons is None
    assert msg.text == "Which one?" and msg.reply_to == 10
    assert msg.buttons == [[("Farmfresh Toned Milk · 500 ml x 4 · ₹116", "v:1:0")],
                           [("Meadow Oat Drink · 200 ml · ₹40", "v:1:1")],
                           [("None of these", "v:1:x")]]
    row = rig.conn.execute("SELECT * FROM pending").fetchone()
    payload = json.loads(row["payload_json"])
    assert (row["kind"], row["user_id"], payload["qty"], payload["owners"]) == ("variant", 1, 2, [1, 2])
    assert payload["options"][1] == {"spin": "SPIN000003", "sku": "SKU0000003", "name": "Meadow Oat Drink",
                                     "size": "200 ml", "price": 4000, "max_qty": 5}
    assert lines(rig.conn) == []


def two_questions(first="chicken", second="instant coffee"):
    return [use(call("search_catalog", query=first)),
            use(call("ask_variant", question=f"Which {first}?", refs=["p1v1", "p2v1"], qty=1,
                     owners=["me"], original_text=first)),
            say("Pick one above."),
            use(call("search_catalog", query=second)),
            use(call("ask_variant", question=f"Which {second}?", refs=["p1v1", "p2v1"], qty=2,
                     owners=["me"], original_text=second)),
            say("Pick one above.")]


def test_a_new_question_does_not_kill_the_older_one(rig):
    async def go():
        async with AgentFlow(rig, two_questions()) as f:
            await f.ask("chicken")
            await f.ask("instant coffee", message_id=11)
            return f
    f = run(go())
    assert [r["id"] for r in rig.conn.execute("SELECT id FROM pending ORDER BY id")] == [1, 2]
    assert [m.buttons[0][0][1] for m in f.outbox.sent if m.buttons] == ["v:1:0", "v:2:0"]


def test_both_open_questions_can_be_answered_by_tapping_their_own_messages(rig):
    async def go():
        async with AgentFlow(rig, two_questions()) as f:
            await f.ask("chicken")
            await f.ask("instant coffee", message_id=11)
            chicken, coffee = [m for m in f.outbox.sent if m.buttons]
            await f.tap(chicken.buttons[0][0][1])  # Farmfresh, from the first message
            await f.tap(coffee.buttons[1][0][1])  # Oat, from the second
            return f
    f = run(go())
    first, second = lines(rig.conn)
    assert (first["name"], first["qty"], first["original_text"]) == ("Farmfresh Toned Milk", 1, "chicken")
    assert (second["name"], second["qty"], second["original_text"]) == ("Meadow Oat Drink", 2, "instant coffee")
    assert owners_of(rig.conn, first["id"]) == [1] and owners_of(rig.conn, second["id"]) == [1]
    assert rig.conn.execute("SELECT count(*) FROM pending").fetchone()[0] == 0
    assert all(not e[2].startswith("This choice") for e in f.outbox.edited)


def test_next_turn_snapshot_lists_every_open_question_oldest_first(rig):
    async def go():
        async with AgentFlow(rig, two_questions() + [say("ok")]) as f:
            await f.ask("chicken")
            await f.ask("instant coffee", message_id=11)
            await f.ask("hello", message_id=12)
            return f
    f = run(go())
    snap = f.llm.sent[-1][1][0]["content"]
    block = snap.split("Open questions:\n")[1].splitlines()
    asked = [l for l in block if l.startswith("  #")]
    assert len(asked) == 2
    assert asked[0].startswith("  #1 Priya (sender's), 0 min ago: Which chicken? Options: Farmfresh Toned Milk")
    assert asked[1].startswith("  #2 Priya (sender's), 0 min ago: Which instant coffee?")


def test_scope_question_still_replaces_the_askers_older_one(rig):
    from cartmates.domain import pending
    add_member(rig.conn, 1, "Priya")
    first = pending.create_scope(rig.conn, 1, {})
    second = pending.create_scope(rig.conn, 1, {})
    assert [r["id"] for r in rig.conn.execute("SELECT id FROM pending")] == [second] and second > first


def test_asker_taps_a_variant_and_the_line_is_added(rig):
    add_member(rig.conn, 2, "Rohan")

    async def go():
        async with AgentFlow(rig, ambiguous_turns()) as f:
            await f.ask("2 milk for me and rohan")
            await f.tap("v:1:1")
            return f
    f = run(go())
    (line,) = lines(rig.conn)
    assert (line["name"], line["qty"], line["added_by"], line["original_text"]) == ("Meadow Oat Drink", 2, 1, "milk")
    assert owners_of(rig.conn, 1) == [1, 2] and kv(rig.conn, "queue_version") == "1"
    assert f.outbox.edited == [(GROUP, 77, "✅ Meadow Oat Drink (200 ml) ×2 for Priya, Rohan — ₹80")]
    assert f.outbox.answers == [("tap1", None, False)]
    assert rig.conn.execute("SELECT count(*) FROM pending").fetchone()[0] == 0
    assert rig.conn.execute("SELECT text FROM chat_memory WHERE role='action' ORDER BY id DESC").fetchone()[0] \
        .startswith("added #1 Meadow Oat Drink")


# ---- the full search view, More options and short names ----

def snack_turns(*picks):
    return [use(call("search_catalog", query="snacks")),
            use(call("ask_variant", question="Which chips?", refs=list(picks), qty=1, owners=["me"],
                     original_text="chips")),
            say("Pick one above.")]


def test_search_shows_every_product_merged_with_out_marked(rig):
    async def go():
        async with AgentFlow(rig, snack_turns("p1v1", "p4v1")) as f:
            await f.ask("chips")
            return f
    f = run(go())
    rows = f.tool_output(1, "c-search_catalog").splitlines()
    assert len(rows) == 13  # 13 entries from Swiggy, the repeated spin merged
    assert rows[0] == "p1 SuperYou Chips - Pudina: v1 40 g ₹31"  # marketing text dropped
    assert rows[1] == "p2 SuperYou Chips - Cheese: v1 40 g ₹32 · v2 80 g ₹50"  # merged, not listed twice
    assert rows[2].endswith("v1 40 g ₹33 · v2 80 g ₹60 OUT")
    assert rows[6].endswith("v1 40 g ₹37 OUT")
    assert rows[11].startswith("p12 SuperYou Chips - Chilli")


def test_ask_variant_with_two_picks_pages_the_rest_of_the_search(rig):
    async def go():
        async with AgentFlow(rig, snack_turns("p1v1", "p4v1")) as f:
            await f.ask("chips")
            return f
    f = run(go())
    (msg,) = [m for m in f.outbox.sent if m.buttons]
    assert [b[0][0] for b in msg.buttons[:4]] == [
        "Pudina · 40 g · ₹31", "Sour Cream · 40 g · ₹34", "Cheese · 40 g · ₹32", "Cheese · 80 g · ₹50"]
    assert [b[0][1] for b in msg.buttons] == ["v:1:0", "v:1:1", "v:1:2", "v:1:3", "v:1:m1", "v:1:x"]
    assert msg.buttons[4][0][0] == "More options ▸" and msg.buttons[5][0][0] == "None of these"
    payload = json.loads(rig.conn.execute("SELECT payload_json FROM pending").fetchone()[0])
    assert len(payload["options"]) == 12  # 2 picks, then 10 more in-stock variants; OUT ones left out
    assert [o["sku"] for o in payload["options"][:4]] == ["SKUB0001", "SKUB0004", "SKUB0002", "SKUB0020"]


def test_more_edits_the_same_message_and_wraps_after_the_last_page(rig):
    async def go():
        async with AgentFlow(rig, snack_turns("p1v1", "p4v1")) as f:
            await f.ask("chips")
            await f.tap("v:1:m1")
            await f.tap("v:1:m2")
            await f.tap("v:1:m0")
            return f
    f = run(go())
    assert len(f.llm.sent) == 3  # no model call for More
    assert [e[:3] for e in f.outbox.edited] == [(GROUP, 77, "Which chips?")] * 3
    page2, page3, page1 = f.outbox.edited_buttons
    assert [b[0][1] for b in page2] == ["v:1:4", "v:1:5", "v:1:6", "v:1:7", "v:1:m2", "v:1:x"]
    assert [b[0][1] for b in page3] == ["v:1:8", "v:1:9", "v:1:10", "v:1:11", "v:1:m0", "v:1:x"]  # last page: More wraps
    assert page1[0][0][0] == "Pudina · 40 g · ₹31" and page1[4][0][1] == "v:1:m1"
    assert rig.conn.execute("SELECT count(*) FROM pending").fetchone()[0] == 1  # still open
    assert lines(rig.conn) == []


def test_tapping_an_option_on_page_two_adds_that_sku(rig):
    async def go():
        async with AgentFlow(rig, snack_turns("p1v1", "p4v1")) as f:
            await f.ask("chips")
            await f.tap("v:1:m1")
            await f.tap("v:1:5")
            return f
    f = run(go())
    (line,) = lines(rig.conn)
    assert (line["sku_id"], line["variant"]) == ("SKUB0005", "40 g")  # Tomato: index 5 of the full list
    assert f.outbox.edited[-1][2].startswith("✅ SuperYou Chips - Tomato (40 g) ×1")  # short name


def test_picks_from_the_first_ref_search_supply_the_extras(rig):
    turns = [use(call("search_catalog", query="snacks")), use(call("search_catalog", query="milk")),
             use(call("ask_variant", question="Which?", refs=["p13v1", "p1v1"], qty=1, original_text="x")),
             say("Pick one above.")]

    async def go():
        async with AgentFlow(rig, turns) as f:
            await f.ask("x")
            return f
    f = run(go())
    options = json.loads(rig.conn.execute("SELECT payload_json FROM pending").fetchone()[0])["options"]
    assert [o["sku"] for o in options] == ["SKU0000001", "SKUB0001", "SKU0000003"]  # picks, then milk's other in-stock


def test_an_old_pending_row_with_only_options_still_resolves(rig):
    from cartmates.domain import pending
    add_member(rig.conn, 1, "Priya")
    options = [{"spin": "S1", "sku": "K1", "name": "Old Milk", "size": "1 L", "price": 5000, "max_qty": 5},
               {"spin": "S2", "sku": "K2", "name": "Old Oat", "size": "1 L", "price": 6000, "max_qty": 5}]
    pid = pending.create_variant(rig.conn, 1, {"question": "Which one?", "options": options, "qty": 1,
                                               "owners": [1], "original_text": "milk"})

    async def go():
        async with AgentFlow(rig, [say("ok")]) as f:
            await f.tap(f"v:{pid}:1")
            return f
    run(go())
    (line,) = lines(rig.conn)
    assert (line["sku_id"], line["name"]) == ("K2", "Old Oat")


def test_another_user_cannot_pick(rig):
    add_member(rig.conn, 2, "Rohan")

    async def go():
        async with AgentFlow(rig, ambiguous_turns()) as f:
            await f.ask("milk")
            await f.tap("v:1:1", user_id=2, name="Rohan")
            return f
    f = run(go())
    assert f.outbox.answers == [("tap1", "Only Priya can pick this", True)]
    assert lines(rig.conn) == [] and f.outbox.edited == []
    assert rig.conn.execute("SELECT count(*) FROM pending").fetchone()[0] == 1


def test_none_of_these(rig):
    async def go():
        async with AgentFlow(rig, ambiguous_turns(["me"])) as f:
            await f.ask("milk")
            await f.tap("v:1:x")
            return f
    f = run(go())
    assert f.outbox.edited == [(GROUP, 77, "No problem — tell me what you want instead")]
    assert lines(rig.conn) == [] and rig.conn.execute("SELECT count(*) FROM pending").fetchone()[0] == 0
    assert f.outbox.answers == [("tap1", None, False)]


def test_expired_or_unknown_choice(rig):
    async def go():
        async with AgentFlow(rig, ambiguous_turns(["me"])) as f:
            await f.ask("milk")
            rig.conn.execute("UPDATE pending SET expires_at=?",
                             ((datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat(),))
            await f.tap("v:1:1")
            await f.tap("v:99:0")
            await f.tap("garbage")
            return f
    f = run(go())
    assert [a[1] for a in f.outbox.answers] == ["This choice expired"] * 3
    assert lines(rig.conn) == [] and f.outbox.edited == []


def test_double_tap_adds_once(rig):
    async def go():
        async with AgentFlow(rig, ambiguous_turns(["me"])) as f:
            await f.ask("milk")
            await f.tap("v:1:1")
            await f.tap("v:1:1")
            return f
    f = run(go())
    assert len(lines(rig.conn)) == 1
    assert [a[1] for a in f.outbox.answers] == [None, "This choice expired"]


# ---- editing ----

def test_editing_someone_elses_line_names_the_adder_and_removed_lines_are_final(rig):
    add_member(rig.conn, 1, "Priya")
    add_member(rig.conn, 2, "Rohan")
    seed_line(rig.conn, "Eggs", added_by=2, qty=1)

    async def go():
        async with AgentFlow(rig, [
            use(call("update_queue_item", number=1, qty=3)), say("Made it 3."),
            use(call("remove_from_queue", number=1)), say("Dropped."),
            use(call("update_queue_item", number=1, qty=2)), say("Cannot."),
        ]) as f:
            await f.ask("make the eggs 3")
            await f.ask("drop the eggs")
            await f.ask("make the eggs 2")
            return f
    f = run(go())
    assert f.tool_output(1, "c-update_queue_item") == "Updated #1 Rohan's Eggs: qty 1 -> 3"
    assert f.tool_output(3, "c-remove_from_queue") == "Removed #1 Rohan's Eggs (12 pcs) ×3"
    assert "no queued line #1" in f.tool_output(5, "c-update_queue_item")
    assert rig.conn.execute("SELECT status, qty FROM queue_lines").fetchone()[:] == ("removed", 3)
    assert [a for (a,) in rig.conn.execute("SELECT text FROM chat_memory WHERE role='action'")] == \
        ["Priya: Updated #1 Rohan's Eggs: qty 1 -> 3", "Priya: Removed #1 Rohan's Eggs (12 pcs) ×3"]
    assert int(kv(rig.conn, "queue_version")) == 3  # the seed add, then update and remove


def test_removed_commands_only_nudge_and_change_nothing(rig):
    add_member(rig.conn, 1, "Priya")
    seed_line(rig.conn, "Eggs", added_by=1)
    before = lines(rig.conn)

    async def go():
        async with AgentFlow(rig, []) as f:
            for text in ("/remove 1", "/add 2 maggi", "/search milk"):
                await f.adapter.on_update(command(text))
            await asyncio.sleep(0.3)
            return f
    f = run(go())
    assert f.llm.sent == [] and lines(rig.conn) == before and rig.mcp.calls == []
    assert [t.split(",")[0] for t in f.texts()] == ["Just @mention me in plain words"] * 2 + ["Just @mention me and ask in plain words"]
    assert 'remove the eggs' in f.texts()[0] and 'add 2 milk' in f.texts()[1] and 'instant coffee' in f.texts()[2]


def test_list_queue_tool(rig):
    add_member(rig.conn, 1, "Priya")
    seed_line(rig.conn, "Eggs", added_by=1, qty=2)

    async def go():
        async with AgentFlow(rig, [use(call("list_queue")), say("One line.")]) as f:
            await f.ask("what's in the queue")
            return f
    f = run(go())
    assert f.tool_output(1, "c-list_queue") == "#1 Eggs (12 pcs) ×2 for Priya\nTotal ₹180"


# ---- limits and failures ----

def test_round_cap_gives_an_apology(rig):
    async def go():
        async with AgentFlow(rig, [use(call("list_queue"))] * (MAX_ROUNDS + 1)) as f:
            await f.ask("loop forever")
            return f
    f = run(go())
    assert len(f.llm.sent) == MAX_ROUNDS
    assert f.texts() == [APOLOGY_TOO_LONG]


def test_llm_unavailable_apologises_and_changes_nothing(rig):
    async def go():
        async with AgentFlow(rig, [LLMUnavailable("timeout")]) as f:
            await f.ask("add oat")
            return f
    f = run(go())
    assert f.texts() == [APOLOGY_DOWN] and lines(rig.conn) == []
    assert kv(rig.conn, "queue_version") == "0"


def test_not_configured_and_bare_mention(rig):
    out = FakeOutbox()

    async def go():
        lane = Lane(Handlers(rig.conn, out, GROUP).handle, out)
        adapter = TelegramAdapter(lane, out, GROUP, BOT_ID, BOT_NAME)
        lane.start()
        await adapter.on_update(make_update("@CartMatesBot add milk", entities=MENTION))
        await adapter.on_update(make_update("@CartMatesBot", entities=MENTION))
        await asyncio.sleep(0.2)
        await lane.stop()
    run(go())
    assert out.sent[0].text == "The assistant isn't configured (OPENAI_API_KEY)."
    assert "/help" in out.sent[1].text and "/add" not in out.sent[1].text


def test_logged_out_during_search_parks_then_replays(tmp_path):
    c = db.connect(tmp_path / "t.db")
    db.migrate(c)
    rig = Rig(tmp_path, c)  # not seeded: logged out

    async def go():
        async with AgentFlow(rig, [
            use(call("search_catalog", query="oat")),   # raises LoggedOut inside the tool
            use(call("search_catalog", query="oat")),   # the replay starts the turn again
            use(call("add_to_queue", ref="p2v1", qty=1, original_text="oat")),
            say("Added."),
        ]) as f:
            await f.ask("add oat", message_id=20)
            assert "I need a Swiggy login first" in f.texts()[-1]
            assert c.execute("SELECT count(*) FROM chat_memory").fetchone()[0] == 0  # nothing half-written
            state = pending_login(c)["state"]
            await f.adapter.on_update(make_update(f"http://localhost:8765/callback?code=abc&state={state}",
                                                  reply_to_bot=True, message_id=21))
            await asyncio.sleep(0.5)
            return f
    f = run(go())
    assert f.texts()[-1] == "Added." and f.outbox.sent[-1].reply_to == 20
    assert len(lines(c)) == 1
    assert [r[0] for r in c.execute("SELECT role FROM chat_memory ORDER BY id")] == ["user", "action", "assistant"]


# ---- memory ----

def test_transcript_window_is_20_messages_within_12_hours(tmp_path):
    c = db.connect(tmp_path / "t.db")
    db.migrate(c)
    now = datetime(2026, 11, 5, 12, 0, tzinfo=timezone.utc)

    def put(role, text, ago_hours):
        memory.remember(c, role, text, now - timedelta(hours=ago_hours), 1 if role == "user" else None)

    put("user", "too old", 13)
    put("assistant", "old reply", 12.9)
    for i in range(25):
        put("user", f"msg {i}", 11 - i * 0.1)
    put("action", "added #1", 0.5)
    text = memory.transcript(c, now)
    rows = text.splitlines()
    assert rows[0] == "[user] msg 5" and rows[-1] == "[action] added #1"
    assert "too old" not in text and "old reply" not in text and "msg 4" not in text
    assert len([r for r in rows if r.startswith("[user]")]) == 20

    # Only the messages inside 12 h count, even with fewer than 20.
    c.execute("DELETE FROM chat_memory")
    put("user", "stale", 13)
    put("assistant", "stale reply", 12.5)
    assert memory.transcript(c, now) == ""
    put("user", "fresh", 1)
    assert memory.transcript(c, now) == "[user] fresh"


def test_snapshot_has_ids_owners_total_and_minimum(tmp_path):
    c = db.connect(tmp_path / "t.db")
    db.migrate(c)
    add_member(c, 1, "Priya")
    add_member(c, 2, "Rohan")
    seed_line(c, "Eggs", added_by=1, qty=2, owners=[1, 2])  # 2 x ₹90
    c.execute("INSERT INTO kv (key, value) VALUES ('address_label', ?)",
              (json.dumps({"id": "x", "label": "Flat 12"}),))
    now = datetime(2026, 11, 5, 12, 0, tzinfo=timezone.utc)
    snap = memory.build_snapshot(c, 1, now + timedelta(hours=3), 199)
    assert "Now: Thu 05 Nov 2026 20:30 IST" in snap
    assert "Members: Priya (id 1), Rohan (id 2)" in snap
    assert "#1 Eggs (12 pcs) ×2 for Priya, Rohan, est ₹180" in snap
    assert "Estimated total ₹180 vs free-delivery minimum ₹199: ₹19 to go" in snap
    assert "Swiggy login: logged_out; address: Flat 12" in snap
    assert "Open questions" not in snap
    from cartmates.domain import pending
    pending.create_variant(c, 1, {"question": "Which milk?", "options": [{"name": "A", "size": "1 L"}]})
    pending.create_variant(c, 1, {"question": "Which tea?", "options": [{"name": "B", "size": "250 g"}]})
    two = memory.build_snapshot(c, 1, now, 199)
    assert "Open questions:\n  #1 Priya (sender's), 0 min ago: Which milk? Options: A 1 L\n" \
        "  #2 Priya (sender's), 0 min ago: Which tea? Options: B 250 g" in two
    assert "Priya, 0 min ago" in memory.build_snapshot(c, 2, now, 199)  # not marked as the sender's


def test_search_result_names_the_query_for_explaining_misses(rig):
    async def go():
        async with AgentFlow(rig, [use(call("search_catalog", query="instant coffee")), say("ok")]) as f:
            await f.ask("coffee")
            return f
    out = run(go()).tool_output(1, "c-search_catalog")
    assert '"instant coffee"' in out and "products returned" in out


def test_prompt_tells_agent_to_explain_from_facts_not_apologise():
    text = SYSTEM_PROMPT.lower()
    assert "why something wasn't shown" in text and "never apologise" in text and "don't invent" in text


def test_update_and_remove_tools_take_number_not_line_id():
    params = {s["name"]: s["parameters"] for s in SCHEMAS}
    for name in ("update_queue_item", "remove_from_queue"):
        assert "number" in params[name]["properties"] and "line_id" not in params[name]["properties"]
        assert params[name]["required"] == ["number"]
