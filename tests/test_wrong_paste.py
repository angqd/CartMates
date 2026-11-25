"""L3: the Swiggy sign-in page pasted instead of the final redirect URL."""
import asyncio

from cartmates.handlers import WRONG_PASTE
from test_agent import AgentFlow, rig, say  # noqa: F401  (rig is a fixture: logged in)
import pytest
from test_swiggy import Rig, command, pending_login
from test_telegram import make_update
from test_tracing import all_values, by_name, spans  # noqa: F401

@pytest.fixture
def out(tmp_path, rig):
    return Rig(tmp_path, rig.conn)  # same database, logged out


WRONG = ("https://mcp.swiggy.com/auth/authorize?response_type=code&client_id=swiggy-mcp"
         "&code_challenge=CHALLENGE123&state=STATE456")


def paste(text, message_id=30):
    return make_update(text, reply_to_bot=True, message_id=message_id, user_id=2, name="Rohan")


def run(rig, turns, updates):
    async def go():
        async with AgentFlow(rig, turns) as f:
            for u in updates:
                await f.adapter.on_update(u)
                await asyncio.sleep(0.3)
            return f
    return asyncio.run(go())


def dump(conn):
    tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")]
    return "\n".join(str(tuple(r)) for t in tables for r in conn.execute(f"SELECT * FROM {t}"))


def test_wrong_paste_gets_the_fixed_reply_and_goes_nowhere(rig, out, spans):
    rig = out
    f = run(rig, [], [command("/login"), paste(WRONG)])
    before = pending_login(rig.conn)
    assert f.texts()[-1] == WRONG_PASTE
    assert f.llm.sent == []
    assert before is not None  # login still pending, and /login was not run again
    assert sum(t.startswith("Swiggy login:") for t in f.texts()) == 1
    assert "CHALLENGE123" not in dump(rig.conn) and "STATE456" not in dump(rig.conn)
    assert rig.conn.execute("SELECT count(*) FROM chat_memory WHERE text LIKE '%swiggy%'").fetchone()[0] == 0
    values = " ".join(all_values(spans))
    assert "CHALLENGE123" not in values and "STATE456" not in values
    redacted = [s for s in by_name(spans, "event:") if s.attributes.get("cartmates.redacted")]
    assert len(redacted) == 1 and "input.value" not in redacted[0].attributes


def test_wrong_paste_with_no_login_pending_starts_none(rig):
    f = run(rig, [], [paste("see https://mcp.swiggy.com/auth/something")])
    assert f.texts() == [WRONG_PASTE]
    assert pending_login(rig.conn) is None and f.llm.sent == []


def test_real_callback_paste_still_works(rig, out):
    rig = out
    async def go():
        async with AgentFlow(rig, []) as f:
            await f.adapter.on_update(command("/login"))
            await asyncio.sleep(0.3)
            state = pending_login(rig.conn)["state"]
            await f.adapter.on_update(paste(f"http://localhost:8765/callback?code=abc&state={state}"))
            await asyncio.sleep(0.3)
            return f
    f = asyncio.run(go())
    assert f.texts()[-1] == "Logged in to Swiggy ✅ (by Rohan)"
    assert pending_login(rig.conn) is None


def test_message_mentioning_swiggy_dot_com_still_reaches_the_agent(rig):
    f = run(rig, [say("Noted.")], [make_update("@CartMatesBot is swiggy.com open late?", entities=[
        {"type": "mention", "offset": 0, "length": len("@CartMatesBot")}])])
    assert f.texts() == ["Noted."] and len(f.llm.sent) == 1
