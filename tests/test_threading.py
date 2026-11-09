"""B6: which reply answered whom. Scripted LLM, fake Swiggy, FakeOutbox; the real lane, handlers and SQL run."""
import json
from datetime import datetime, timedelta, timezone

from cartmates.agent import memory
from cartmates.domain import pending
from cartmates.store import db
from test_agent import (AgentFlow, add_member, ambiguous_turns, call, lines, rig, run, say,  # noqa: F401
                        two_questions, use)

ROHAN = dict(user_id=2, name="Rohan")


def new_message(f, round_index=-1):
    return f.llm.sent[round_index][1][1]["content"]


def context(f, round_index=-1):
    return f.llm.sent[round_index][1][0]["content"]


def test_transcript_tags_each_assistant_line_with_who_it_answered(rig):
    async def go():
        async with AgentFlow(rig, [say("Added milk for you."), say("Bread is in."), say("ok")]) as f:
            await f.ask("milk")
            await f.ask("bread", message_id=11, **ROHAN)
            await f.ask("hello", message_id=12)
            return f
    f = run(go())
    chat = context(f).split("Recent chat:\n")[1]
    assert "[assistant → Priya] Added milk for you." in chat
    assert "[assistant → Rohan] Bread is in." in chat


def test_rows_without_an_addressee_render_as_before(rig):
    memory.remember(rig.conn, "assistant", "old", datetime.now(timezone.utc))
    memory.remember(rig.conn, "user", "hi", datetime.now(timezone.utc), 1)
    assert memory.transcript(rig.conn, datetime.now(timezone.utc)).splitlines() == ["[user] hi"]  # window starts at the user row
    memory.remember(rig.conn, "assistant", "later", datetime.now(timezone.utc))
    assert "[assistant] later" in memory.transcript(rig.conn, datetime.now(timezone.utc))


def test_reply_to_someone_elses_message_names_both_and_caps_the_text(rig):
    long_reply = "Diwali gift options " + "x" * 300

    async def go():
        async with AgentFlow(rig, [say(long_reply), say("sukoon added"), say("ok")]) as f:
            await f.ask("diwali gifts")
            sent_id = 1000  # FakeOutbox's first message id: the reply to Priya
            await f.ask("what about sukoon", message_id=11, reply_to_bot=True, reply_to_id=sent_id, **ROHAN)
            await f.ask("hello", message_id=12)
            return f
    f = run(go())
    line = new_message(f, 1)
    head = "New message from Rohan (id 2), replying to the bot's message to Priya (\"Diwali gift options "
    assert line.startswith(head) and line.endswith("…\"): what about sukoon")
    quoted = line.split('("')[1].split('")')[0]
    assert len(quoted) <= 121
    chat = context(f).split("Recent chat:\n")[1]  # the stored user row carries the same tags
    assert "[user] Rohan (id 2), replying to the bot's message to Priya (\"Diwali gift options" in chat


def test_reply_to_a_variant_question_names_the_open_question(rig):
    async def go():
        async with AgentFlow(rig, ambiguous_turns(["me"]) + [say("ok")]) as f:
            await f.ask("milk")
            await f.ask("the first one", message_id=11, reply_to_bot=True, reply_to_id=1000, **ROHAN)
            return f
    f = run(go())
    assert new_message(f).startswith(
        "New message from Rohan (id 2), replying to the bot's message to Priya (\"Which one?\"; open question #1): ")
    row = rig.conn.execute("SELECT message_id FROM pending").fetchone()
    assert row["message_id"] == 1000
    assert rig.conn.execute("SELECT to_user_id, message_id FROM chat_memory WHERE role='assistant'").fetchone()[:] == (1, 1001)  # the summary; the question (1000) is the pending row


def test_reply_to_an_unknown_bot_message_says_earlier(rig):
    async def go():
        async with AgentFlow(rig, [say("ok")]) as f:
            await f.ask("hi", reply_to_bot=True, reply_to_id=5)
            return f
    assert new_message(run(go())) == "New message from Priya (id 1), replying to an earlier bot message: hi"


def test_login_links_and_pastes_are_not_quoted(rig):
    async def go():
        async with AgentFlow(rig, [say("Log in at https://swiggy.example/auth?x=1"), say("ok")]) as f:
            await f.ask("hello")
            await f.ask("done", message_id=11, reply_to_bot=True, reply_to_id=1000, **ROHAN)
            return f
    line = new_message(run(go()))
    assert line == "New message from Rohan (id 2), replying to the bot's message to Priya: done"


def test_snapshot_lists_everyones_open_questions_and_marks_the_senders(rig):
    async def go():
        async with AgentFlow(rig, two_questions() + [say("ok")]) as f:
            await f.ask("chicken")
            await f.ask("instant coffee", message_id=11, **ROHAN)
            await f.ask("hello", message_id=12)
            return f
    f = run(go())
    block = context(f).split("Open questions:\n")[1].splitlines()[:2]
    assert block[0].startswith("  #1 Priya (sender's), 0 min ago: Which chicken? Options: ")
    assert block[1].startswith("  #2 Rohan, 0 min ago: Which instant coffee? Options: ")
    assert "(sender's)" not in block[1]


def test_open_questions_show_only_the_picks_cap_at_ten_and_old_rows_show_four(rig):
    add_member(rig.conn, 1, "Priya")
    options = [{"name": f"Brand {i} Coffee", "size": "50 g"} for i in range(7)]
    pending.create_variant(rig.conn, 1, {"question": "Old?", "options": options})  # no "picked"
    pending.create_variant(rig.conn, 1, {"question": "New?", "options": options, "picked": 2})
    for i in range(10):
        pending.create_variant(rig.conn, 1, {"question": f"Q{i}", "options": options[:2], "picked": 2})
    snap = memory.build_snapshot(rig.conn, 1, datetime.now(timezone.utc), 199)
    asked = snap.split("Open questions:\n")[1].splitlines()[:10]
    assert len([l for l in snap.splitlines() if l.startswith("  #")]) == 10
    assert "Old?" not in snap and "New?" not in snap  # the two oldest fell off the cap
    assert asked[0].startswith("  #3 ") and asked[-1].startswith("  #12 ")
    rig.conn.execute("DELETE FROM pending")
    pending.create_variant(rig.conn, 1, {"question": "Old?", "options": options})
    pending.create_variant(rig.conn, 1, {"question": "New?", "options": options, "picked": 2})
    old, new = memory.build_snapshot(rig.conn, 1, datetime.now(timezone.utc), 199).split("Open questions:\n")[1].splitlines()[:2]
    assert old.endswith("Brand 0 Coffee 50 g; Brand 1 Coffee 50 g; Brand 2 Coffee 50 g; Brand 3 Coffee 50 g; (+3 more)")
    assert new.endswith("Options: Brand 0 Coffee 50 g; Brand 1 Coffee 50 g; (+5 more)")


# ---- close_question ----

def test_answering_in_text_then_close_question_removes_the_buttons(rig):
    async def go():
        async with AgentFlow(rig, ambiguous_turns(["me"]) + [
                use(call("search_catalog", query="oat")),
                use(call("add_to_queue", ref="p2v1", qty=1, owners=["me"], original_text="oat one"),
                    call("close_question", question_id=1)),
                say("Added the oat drink.")]) as f:
            await f.ask("milk")
            await f.ask("the oat one", message_id=11)
            return f
    f = run(go())
    assert [l["name"] for l in lines(rig.conn)] == ["Meadow Oat Drink"]
    assert rig.conn.execute("SELECT count(*) FROM pending").fetchone()[0] == 0
    assert f.outbox.edited == [(-100123, 1000, "Answered in chat")] and f.outbox.edited_buttons == [None]
    actions = [r["text"] for r in rig.conn.execute("SELECT text FROM chat_memory WHERE role='action'")]
    assert any("question #1 closed" in a for a in actions)
    assert f.texts()[-1] == "Added the oat drink."  # close_question didn't end the turn


def test_only_the_asker_can_close_a_question(rig):
    async def go():
        async with AgentFlow(rig, ambiguous_turns(["me"]) + [
                use(call("close_question", "c1", question_id=1)), say("Priya has to pick.")]) as f:
            await f.ask("milk")
            await f.ask("close it", message_id=11, **ROHAN)
            return f
    f = run(go())
    assert f.tool_output(4, "c1") == "error: only Priya can close that"
    assert rig.conn.execute("SELECT count(*) FROM pending").fetchone()[0] == 1
    assert f.outbox.edited == []


# ---- how questions end ----

def actions(conn):
    return [r["text"] for r in conn.execute("SELECT text FROM chat_memory WHERE role='action' ORDER BY id")]


def test_none_of_these_leaves_one_action_line(rig):
    async def go():
        async with AgentFlow(rig, ambiguous_turns(["me"])) as f:
            await f.ask("milk")
            await f.tap("v:1:x")
    run(go())
    assert actions(rig.conn) == ['asked Priya "Which one?" (question #1)',
                                 'Priya: none of the options for "Which one?"']


def test_expiry_leaves_one_action_line_whichever_path_sees_it_first(rig):
    async def go():
        async with AgentFlow(rig, ambiguous_turns(["me"])) as f:
            await f.ask("milk")
            rig.conn.execute("UPDATE pending SET expires_at=?",
                             ((datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat(),))
            await f.tap("v:1:0")  # lazy expiry in _get
            assert pending.delete_expired(rig.conn) == 0
            assert pending.get_variant(rig.conn, 1) is None
    run(go())
    assert actions(rig.conn) == ['asked Priya "Which one?" (question #1)', 'Priya\'s question "Which one?" expired']


def test_the_hourly_cleanup_writes_the_expiry_line_once(rig):
    async def go():
        async with AgentFlow(rig, ambiguous_turns(["me"])) as f:
            await f.ask("milk")
            rig.conn.execute("UPDATE pending SET expires_at=?",
                             ((datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat(),))
            assert pending.delete_expired(rig.conn) == 1
            assert pending.delete_expired(rig.conn) == 0
    run(go())
    assert actions(rig.conn) == ['asked Priya "Which one?" (question #1)', 'Priya\'s question "Which one?" expired']


def test_reply_to_a_bill_card_names_the_order(rig):
    add_member(rig.conn, 1, "Priya")
    rig.conn.execute("INSERT INTO orders (id, status, scope, proposed_by, proposed_at, queue_version, card_message_id) "
                     "VALUES (7, 'proposed', 'all', 1, 'x', 0, 555)")
    assert memory.speaker_label(rig.conn, "Rohan", 2, 555) == "Rohan (id 2), replying to the bot's message (order #7)"
