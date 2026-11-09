"""/address (DESIGN 7.2): the helper, the command, the tap, and the guards. Fake Swiggy and fake addresses only."""
import json

import pytest

from queuify.agent.llm import LLMTurn, ScriptedLLM, ToolCall
from queuify.domain import address
from queuify.store import db
from tests.test_orders import ADDRESS, GROUP, MEERA, PRIYA, ROHAN, Env, FakeSwiggy, conn, run, seed_queue  # noqa: F401

NEW = "addr2__NEWsuffix"


def fake_addresses(extra=()):
    base = [
        {"id": ADDRESS, "addressTag": "Flat 12", "addressCategory": "Home", "area": "Faketown",
         "addressLine": "SECRET LINE 1", "phoneNumber": "9999999999", "latitude": 1.5, "longitude": 2.5,
         "name": "SECRET NAME"},
        {"id": NEW, "addressTag": "", "addressCategory": "Other", "area": "Mockville",
         "addressLine": "SECRET LINE 2", "phoneNumber": "9999999999", "latitude": 3.5, "longitude": 4.5,
         "name": "SECRET NAME"},
    ]
    return base + list(extra)


class AddrSwiggy(FakeSwiggy):
    def __init__(self):
        super().__init__()
        self.saved = fake_addresses()
        self.page_size = 1

    async def search_products(self, args):
        return {"products": []}

    async def get_addresses(self, args):
        p = args["page"]
        chunk = self.saved[(p - 1) * self.page_size: p * self.page_size]
        return {"addresses": chunk, "pagination": {"page": p, "hasMore": p * self.page_size < len(self.saved)}}


@pytest.fixture
def env(conn):
    seed_queue(conn)
    e = Env(conn)
    e.swiggy = AddrSwiggy()
    e.h.swiggy = e.swiggy
    e.h.executor.swiggy = e.swiggy
    return e


def buttons(env):
    return env.outbox.sent[-1].buttons


# ---- helper ----

def test_helper_falls_back_to_env_and_kv_overrides(conn):
    assert address.current(conn, None) is None
    assert address.current(conn, "env1") == "env1"
    address.set_current(conn, "kv1")
    assert address.current(conn, "env1") == "kv1"
    address.set_current(conn, "kv2")
    assert address.current(conn, "env1") == "kv2"


# ---- the command ----

def test_address_lists_every_page_with_labels_and_tick(env):
    run(env.say("/address"))
    sent = env.outbox.sent[-1]
    assert "Flat 12" in sent.text
    assert [b[0][0] for b in sent.buttons] == ["✓ Flat 12", "Other"]
    assert [b[0][1] for b in sent.buttons] == [f"a:{ADDRESS}", f"a:{NEW}"]
    assert env.swiggy.n("get_addresses") == 2  # two pages
    blob = json.dumps([sent.text, sent.buttons])
    for secret in ("SECRET", "9999999999", "1.5", "2.5", "3.5"):
        assert secret not in blob


def test_equal_labels_get_the_area(env):
    env.swiggy.saved = fake_addresses([{"id": "addr3", "addressTag": "Flat 12", "area": "Elsewhere"}])
    run(env.say("/address"))
    assert [b[0][0] for b in buttons(env)] == ["✓ Flat 12 · Faketown", "Other", "Flat 12 · Elsewhere"]


def test_overlong_callback_data_is_skipped(env):
    env.swiggy.saved = fake_addresses([{"id": "x" * 70, "addressTag": "Huge"}])
    run(env.say("/address"))
    assert [b[0][0] for b in buttons(env)] == ["✓ Flat 12", "Other"]


def test_address_is_in_help(env):
    assert "/address - switch the delivery address" in run(env.say("/help"))


# ---- the tap ----

def propose(env):
    run(env.say("/order all"))
    return env.order(1)


def test_any_member_switches_announces_and_supersedes_an_open_proposal(env):
    run(env.say("/address"))
    propose(env)
    assert env.order(1)["address_id"] == ADDRESS
    env.conn.execute("INSERT INTO kv (key, value) VALUES ('address_label', ?)",
                     (json.dumps({"id": ADDRESS, "label": "Flat 12"}),))
    run(env.tap(f"a:{NEW}", user=ROHAN, message_id=555))
    assert address.current(env.conn, ADDRESS) == NEW
    assert env.conn.execute("SELECT value FROM kv WHERE key='address_label'").fetchone() is None
    o = env.order(1)
    assert (o["status"], o["error"]) == ("superseded", "address changed")
    assert (GROUP, 1001, "Address changed — run /order again") in env.outbox.edited
    edit = [e for e in env.outbox.edited if e[1] == 555][-1]
    assert "Other" in edit[2]
    assert env.outbox.sent[-1].text == "Delivery address is now Other (changed by Rohan)"
    assert env.outbox.answers[-1] == ("tap", None, False)
    kinds = [r[0] for r in env.conn.execute("SELECT kind FROM events")]
    assert "address_changed" in kinds
    memory = " ".join(str(tuple(r)) for r in env.conn.execute("SELECT * FROM chat_memory"))
    assert "changed the delivery address to Other" in memory


def test_search_and_propose_use_the_switched_address(env):
    run(env.say("/order all"))
    assert env.swiggy.calls[0][1]["selectedAddressId"] == ADDRESS
    run(env.tap(f"a:{NEW}"))
    env.swiggy.calls.clear()
    env.swiggy.selected = "addr2"
    run(env.say("/order all"))
    assert env.swiggy.calls[0][1]["selectedAddressId"] == NEW
    assert env.order(2)["address_id"] == NEW


def test_chat_search_reads_the_new_address(env):
    call = ToolCall("c1", "search_catalog", json.dumps({"query": "milk"}))
    env.h.llm = ScriptedLLM([LLMTurn(tool_calls=[call]), LLMTurn(text="Nothing."),
                             LLMTurn(tool_calls=[call]), LLMTurn(text="Nothing.")])
    run(env.say("find milk"))
    run(env.tap(f"a:{NEW}"))
    run(env.say("find milk"))
    seen = [args["addressId"] for tool, args in env.swiggy.calls if tool == "search_products"]
    assert seen == [ADDRESS, NEW]


def test_status_shows_the_new_label(env):
    class Auth:
        def logged_in(self):
            return True
    env.h.auth = Auth()
    assert "Swiggy address: Flat 12" in run(env.say("/status"))
    run(env.tap(f"a:{NEW}"))
    assert "Swiggy address: Other" in run(env.say("/status"))


def test_tampered_or_stale_id_is_rejected(env):
    run(env.tap("a:addr9__nope"))
    assert env.outbox.answers[-1] == ("tap", "That address is no longer saved", False)
    run(env.tap("a:"))
    assert env.outbox.answers[-1][1] == "That address is no longer saved"
    assert address.current(env.conn, ADDRESS) == ADDRESS
    assert env.outbox.sent == []


def test_already_current_toast(env):
    run(env.tap(f"a:{ADDRESS}"))
    assert env.outbox.answers[-1] == ("tap", "Already the delivery address", False)
    assert env.outbox.sent == []
    run(env.tap(f"a:{ADDRESS.split('__')[0]}"))  # bare id form is the same address
    assert env.outbox.answers[-1][1] == "Already the delivery address"


# ---- the executor guard ----

def test_confirm_on_an_order_for_another_address_is_superseded_without_swiggy_writes(env):
    propose(env)
    address.set_current(env.conn, NEW)  # a switch that slipped past the tap path
    env.swiggy.calls.clear()
    run(env.tap("o:1:c"))
    assert env.order(1)["status"] == "superseded" and env.order(1)["error"] == "address changed"
    assert env.swiggy.calls == []
    assert "address changed" in env.outbox.edited[-1][2] and "/order again" in env.outbox.edited[-1][2]


def test_confirm_still_works_after_a_switch_back_and_when_unchanged(env):
    propose(env)
    run(env.tap("o:1:c"))
    assert env.order(1)["status"] == "placed"
