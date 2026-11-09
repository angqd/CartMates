"""B2: one message with several items. The agent searches, adds the clear ones, asks one question per unclear
one, reports what it couldn't find, and ends with one summary. Scripted LLM, fake Swiggy, FakeOutbox."""
import json

from cartmates.agent.llm import LLMTurn
from cartmates.agent.loop import APOLOGY_TOO_LONG, MAX_ROUNDS, MAX_TOOL_CALLS, TOO_LONG_AFTER_CHANGES
from test_agent import AgentFlow, add_member, call, lines, rig, run, say, seed_line, use  # noqa: F401 (rig is a fixture)
from test_network import connect_error, memory_rows, pending_count

ITEMS = "1. spaghetti pasta 2. garlic 3. cheese 4. salt 5. tomato puree"
SUMMARY = "Added garlic (#1) and salt (#2). Pick pasta and cheese above. I couldn't find tomato puree."
PASTA, CHEESE = "Which pasta?", "Which cheese?"
# Refs are numbered across the turn's searches: pasta p1-p2, garlic p3-p4, cheese ("snacks", 12 products) p5-p16,
# salt p17-p18, puree none.


def searches_round():
    return use(call("search_catalog", "s-pasta", query="pasta"), call("search_catalog", "s-garlic", query="garlic"),
               call("search_catalog", "s-cheese", query="snacks"))


def ask_pasta():
    return call("ask_variant", "q-pasta", question=PASTA, refs=["p1v1", "p2v1"], qty=1, owners=["me"],
                original_text="spaghetti pasta")


def ask_cheese():
    return call("ask_variant", "q-cheese", question=CHEESE, refs=["p5v1", "p6v1"], qty=1, owners=["me"],
                original_text="cheese")


def add_garlic():
    return call("add_to_queue", "a-garlic", ref="p3v1", qty=1, original_text="garlic")


def full_script():
    return [searches_round(),
            use(call("search_catalog", "s-salt", query="salt"), call("search_catalog", "s-puree", query="tomato puree"),
                add_garlic()),
            use(call("add_to_queue", "a-salt", ref="p18v1", qty=1, original_text="salt"), ask_pasta(), ask_cheese()),
            say(SUMMARY)]


def test_five_items_add_two_ask_two_report_one_missing_and_one_summary(rig):
    async def go():
        async with AgentFlow(rig, full_script()) as f:
            await f.ask(f"add all these ingredients: {ITEMS}")
            return f
    f = run(go())
    assert [l["name"] for l in lines(rig.conn)] == ["Farmfresh Toned Milk", "Meadow Oat Drink"]
    assert f.tool_output(3, "s-puree") == 'No results for "tomato puree".'
    assert f.tool_output(3, "q-pasta").startswith("Question #1 sent")
    pasta, cheese, summary = f.outbox.sent
    assert (pasta.text, cheese.text, summary.text) == (PASTA, CHEESE, SUMMARY)
    assert summary.buttons is None and f.outbox.sent[-1] is summary
    assert [b[0][1] for b in pasta.buttons] == ["v:1:0", "v:1:1", "v:1:x"]  # its own search: milk and oat only
    assert cheese.buttons[0][0][0].startswith("Pudina") and cheese.buttons[1][0][0].startswith("Cheese")
    assert cheese.buttons[0][0][1] == "v:2:0" and cheese.buttons[-1][0][1] == "v:2:x"
    options = {r["id"]: [o["sku"] for o in json.loads(r["payload_json"])["options"]]
               for r in rig.conn.execute("SELECT id, payload_json FROM pending")}
    assert options[1] == ["SKU0000001", "SKU0000003"]
    assert options[2][:2] == ["SKUB0001", "SKUB0002"]  # the whole snacks search, no milk in it
    assert all(s.startswith("SKUB") for s in options[2])
    assert pending_count(rig.conn) == 2
    rows = memory_rows(rig.conn)
    assert [role for role, _ in rows] == ["user", "action", "action", "action", "action", "assistant"]
    assert rows[1][1].startswith("added #1 ") and rows[2][1].startswith("added #2 ")
    assert rows[3][1] == f'asked Priya "{PASTA}" (question #1)' and rows[4][1] == f'asked Priya "{CHEESE}" (question #2)'
    assert rows[5][1] == SUMMARY


def test_tapping_both_questions_in_reverse_order_adds_both_items(rig):
    async def go():
        async with AgentFlow(rig, full_script()) as f:
            await f.ask(f"add all these ingredients: {ITEMS}")
            await f.tap("v:2:1")  # cheese first: the Cheese chips
            await f.tap("v:1:1")  # then the pasta question: the oat drink stands in
            return f
    f = run(go())
    names = [(l["name"], l["original_text"]) for l in lines(rig.conn)]
    assert names[2:] == [("SuperYou Chips - Cheese (No Palm oil, Baked not fried)", "cheese"), ("Meadow Oat Drink", "spaghetti pasta")]
    assert pending_count(rig.conn) == 0


def test_a_turn_that_only_asks_gets_a_code_built_summary_not_an_empty_message(rig):
    async def go():
        async with AgentFlow(rig, [searches_round(), use(ask_pasta(), ask_cheese()), LLMTurn(text="")]) as f:
            await f.ask("pasta and cheese")
            return f
    f = run(go())
    assert f.texts() == [PASTA, CHEESE, "Pick an option above."]
    assert rows_last(rig) == "Pick an option above."


def test_fallback_summary_names_the_adds_when_the_model_writes_nothing(rig):
    async def go():
        async with AgentFlow(rig, [searches_round(), use(add_garlic(), ask_pasta()), LLMTurn(text=None)]) as f:
            await f.ask("garlic and pasta")
            return f
    f = run(go())
    assert f.texts()[-1].startswith("Done: added #1 ") and f.texts()[-1].endswith("Pick an option above.")
    assert "asked" not in f.texts()[-1]


def rows_last(rig):
    return memory_rows(rig.conn)[-1][1]


def test_a_question_that_fails_to_send_mid_list_keeps_the_earlier_ones(rig):
    async def go():
        async with AgentFlow(rig, [searches_round(), use(add_garlic(), ask_pasta(), ask_cheese())]) as f:
            f.outbox.fail_sends(connect_error(), when=lambda sent: sent.text == CHEESE)
            await f.ask(f"add all these ingredients: {ITEMS}")
            first = f.outbox.sent[0]
            await f.tap(first.buttons[0][0][1])
            return f
    f = run(go())
    pasta, reply = f.outbox.sent[0], f.outbox.sent[1]
    assert pasta.text == PASTA
    assert reply.text.startswith("Done so far: added #1 ")
    assert f'asked Priya "{PASTA}" (question #1)' in reply.text and "connection problem" in reply.text
    assert CHEESE not in reply.text
    rows = memory_rows(rig.conn)
    assert [role for role, _ in rows[:3]] == ["user", "action", "action"] and rows[-1][0] != "assistant"
    assert [l["name"] for l in lines(rig.conn)] == ["Farmfresh Toned Milk", "Farmfresh Toned Milk"]  # garlic, then the tapped pasta
    assert pending_count(rig.conn) == 0  # the failed question left no row; the tapped one is resolved


def test_a_failed_question_leaves_the_earlier_one_open_until_tapped(rig):
    async def go():
        async with AgentFlow(rig, [searches_round(), use(ask_pasta(), ask_cheese())]) as f:
            f.outbox.fail_sends(connect_error(), when=lambda sent: sent.text == CHEESE)
            await f.ask("pasta and cheese")
            return f
    f = run(go())
    assert pending_count(rig.conn) == 1
    assert rig.conn.execute("SELECT message_id FROM pending").fetchone()[0] == 1000  # the pasta question's message


def busy_script(first_round, rounds=MAX_ROUNDS + 2, per_round=8):
    return [first_round] + [use(*[call("list_queue", f"l{r}-{i}") for i in range(per_round)]) for r in range(rounds)]


def test_the_tool_call_cap_ends_the_turn_and_lists_what_was_done(rig):
    first = use(call("search_catalog", "s", query="garlic"), call("add_to_queue", "a", ref="p1v1", qty=1, original_text="g"))

    async def go():
        async with AgentFlow(rig, busy_script(first)) as f:
            await f.ask("lots")
            return f
    f = run(go())
    executed = rig.conn.execute("SELECT count(*) FROM events WHERE kind='tool_call'").fetchone()[0]
    assert executed == MAX_TOOL_CALLS and len(f.llm.sent) < MAX_ROUNDS
    (reply,) = f.texts()
    assert reply.startswith("Done so far: added #1 ") and reply.endswith(TOO_LONG_AFTER_CHANGES)
    assert [r for r, _ in memory_rows(rig.conn)] == ["user", "action", "assistant"]


def test_the_round_cap_lists_what_was_done(rig):
    first = use(call("search_catalog", "s", query="garlic"), call("add_to_queue", "a", ref="p1v1", qty=1, original_text="g"))

    async def go():
        async with AgentFlow(rig, busy_script(first, per_round=1)) as f:
            await f.ask("lots")
            return f
    f = run(go())
    assert len(f.llm.sent) == MAX_ROUNDS
    (reply,) = f.texts()
    assert reply.startswith("Done so far: added #1 ") and reply != APOLOGY_TOO_LONG


def test_propose_order_still_ends_the_turn_after_questions(rig):
    add_member(rig.conn, 1, "Priya")
    seed_line(rig.conn, "Eggs", added_by=1)  # something to order

    async def go():
        async with AgentFlow(rig, [searches_round(), use(ask_pasta(), call("propose_order", scope="ask"))]) as f:
            await f.ask("pasta, and order now")  # no model round is scripted after propose_order
            return f
    f = run(go())
    assert len(f.llm.sent) == 2
    assert f.outbox.sent[0].text == PASTA and len(f.outbox.sent) == 2  # the question and the scope question
    assert pending_count(rig.conn) == 2
