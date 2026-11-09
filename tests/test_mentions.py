"""B11: when someone changes a line they don't own, its owners are mentioned on the reply."""
from test_agent import AgentFlow, add_member, call, rig, run, say, seed_line, use  # noqa: F401

from queuify.telegram.outbox import Mention, TelegramOutbox

# The fake sender in test_agent is user 1, "Priya".


def setup(rig):
    add_member(rig.conn, 1, "Priya")
    add_member(rig.conn, 2, "Rohan")
    add_member(rig.conn, 3, "Meera")


def ask(rig, turns, text="do it"):
    async def go():
        async with AgentFlow(rig, turns) as f:
            await f.ask(text)
            return f
    return run(go())


def test_removing_someone_elses_line_mentions_the_owner(rig):
    setup(rig)
    seed_line(rig.conn, "Eggs", added_by=2)
    f = ask(rig, [use(call("remove_from_queue", number=1)), say("Dropped.")])
    sent = f.outbox.sent[-1]
    assert sent.text == "Dropped."
    assert sent.mentions == [Mention(2, "Rohan", "Priya removed your Eggs (#1)")]


def test_editing_your_own_line_mentions_nobody(rig):
    setup(rig)
    seed_line(rig.conn, "Eggs", added_by=1)
    f = ask(rig, [use(call("update_queue_item", number=1, qty=3)), say("Done.")])
    assert f.outbox.sent[-1].mentions is None


def test_shared_line_mentions_only_the_other_owner(rig):
    setup(rig)
    seed_line(rig.conn, "Eggs", added_by=1, owners=[1, 2])
    f = ask(rig, [use(call("update_queue_item", number=1, qty=3)), say("Done.")])
    assert f.outbox.sent[-1].mentions == [Mention(2, "Rohan", "Priya changed your Eggs (#1)")]


def test_two_changes_to_one_owner_make_one_mention_and_each_owner_gets_one(rig):
    setup(rig)
    seed_line(rig.conn, "Eggs", added_by=2)
    seed_line(rig.conn, "Milk", added_by=2)
    seed_line(rig.conn, "Rice", added_by=3)
    f = ask(rig, [use(call("remove_from_queue", number=1), call("update_queue_item", number=2, qty=2),
                      call("remove_from_queue", number=3)), say("Done.")])
    assert f.outbox.sent[-1].mentions == [
        Mention(2, "Rohan", "Priya removed your Eggs (#1); Priya changed your Milk (#2)"),
        Mention(3, "Meera", "Priya removed your Rice (#3)")]


def test_no_mention_when_nothing_changed(rig):
    setup(rig)
    seed_line(rig.conn, "Eggs", added_by=2)
    f = ask(rig, [use(call("remove_from_queue", number=9)), say("No such line.")])  # rejected
    assert f.outbox.sent[-1].mentions is None


def test_mentions_still_go_out_when_the_reply_fails(rig):
    setup(rig)
    seed_line(rig.conn, "Eggs", added_by=2)
    # The model's turn blows up after the removal.
    f = ask(rig, [use(call("remove_from_queue", number=1)), RuntimeError("boom")])
    assert any(s.mentions and s.mentions[0].user_id == 2 for s in f.outbox.sent)


def test_telegram_message_is_html_escaped_and_links_the_user_id():
    class Bot:
        def __init__(self):
            self.calls = []

        async def send_message(self, chat_id, text, **kw):
            self.calls.append((text, kw))
            return type("M", (), {"message_id": 5})()

    bot = Bot()
    out = TelegramOutbox(bot)
    run(out.send(-1, "<b>hi</b> & bye", mentions=[Mention(42, "Ro<han>", "P removed your A&B (#1)")]))
    text, kw = bot.calls[0]
    assert kw["parse_mode"] == "HTML"
    assert text == ('&lt;b&gt;hi&lt;/b&gt; &amp; bye\n\n'
                    '<a href="tg://user?id=42">Ro&lt;han&gt;</a>: P removed your A&amp;B (#1)')


def test_plain_messages_are_unchanged():
    class Bot:
        async def send_message(self, chat_id, text, **kw):
            self.kw = (text, kw)
            return type("M", (), {"message_id": 5})()

    bot = Bot()
    run(TelegramOutbox(bot).send(-1, "<b>hi</b> & bye"))
    assert bot.kw[0] == "<b>hi</b> & bye" and bot.kw[1]["parse_mode"] is None
