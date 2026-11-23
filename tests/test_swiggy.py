"""Swiggy auth, client and the login/search flows. No network: both servers are fakes
behind httpx MockTransports, so the real OAuth, MCP and parsing code all runs."""
import asyncio
import base64
import hashlib
import json
import logging
from urllib.parse import parse_qs, urlparse

import httpx
import httpx2
import pytest
from cryptography.fernet import Fernet

from cartmates.domain import address
from cartmates.handlers import Handlers
from cartmates.lane import Lane
from cartmates.store import db
from cartmates.store.secrets import SecretStore
from cartmates.swiggy import catalog
from cartmates.swiggy.auth import REDIRECT_URI, SwiggyAuth
from cartmates.swiggy.client import REQUIRED_TOOLS, SwiggyClient
from cartmates.swiggy.errors import LoggedOut, LoginError
from cartmates.telegram.outbox import FakeOutbox
from test_telegram import BOT_ID, BOT_NAME, GROUP, adapter_for, cmd, make_update

ADDRESS = "aaaa1111bbbb2222cccc__FAKEsuffix123"


# ---- fake OAuth server ----

class FakeAuthServer:
    def __init__(self):
        self.requests: list[tuple[str, dict]] = []
        self.refresh_status = 200
        self.code_status = 200
        self.counter = 0

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/auth/register":
            body = json.loads(request.content)
            self.requests.append(("register", body))
            return httpx.Response(201, json={"client_id": "swiggy-mcp"})
        form = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
        self.requests.append((form["grant_type"], form))
        status = self.code_status if form["grant_type"] == "authorization_code" else self.refresh_status
        if status != 200:
            return httpx.Response(status, json={"error": "invalid_grant"})
        self.counter += 1
        return httpx.Response(200, json={
            "access_token": f"access-{self.counter}", "refresh_token": f"refresh-{self.counter}",
            "expires_in": 432000, "token_type": "Bearer"})

    def of(self, kind):
        return [body for k, body in self.requests if k == kind]


# ---- fake MCP server (Streamable HTTP, plain JSON responses) ----

DISPLAY_TEXT = "Found 2 product(s).\n\nDISPLAY INSTRUCTIONS:\n- show brand + name\n- ask which variant"
SEARCH = {"nextOffset": "1", "products": [
    {"displayName": "Farmfresh Toned Milk", "brand": "Farmfresh", "inStock": True, "variations": [
        {"spinId": "SPIN000001", "skuId": "SKU0000001", "quantityDescription": "500 ml x 4",
         "price": {"mrp": 120, "offerPrice": 116}, "isInStockAndAvailable": True, "maxQuantity": 1},
        {"spinId": "SPIN000002", "skuId": "SKU0000002", "quantityDescription": "1 L",
         "price": {"mrp": 66.5, "offerPrice": 66.5}, "isInStockAndAvailable": False, "maxQuantity": 0},
    ]},
    {"displayName": "Meadow Oat Drink", "brand": "Meadow", "variations": [
        {"spinId": "SPIN000003", "skuId": "SKU0000003", "quantityDescription": "200 ml",
         "price": {"mrp": 40, "offerPrice": 40}, "isInStockAndAvailable": True, "maxQuantity": 5}]},
], "similarProducts": []}
FLAVOURS = ["Pudina", "Cheese", "Masala", "Sour Cream", "Tomato", "Peri Peri", "Onion", "Salt", "Mint", "Chaat",
            "Garlic", "Chilli"]


def _chips(n: int, size: str = "40 g", in_stock: bool = True) -> dict:
    return {"spinId": f"SPINB{n:04d}", "skuId": f"SKUB{n:04d}", "quantityDescription": size,
            "price": {"mrp": 40, "offerPrice": 30 + n}, "isInStockAndAvailable": in_stock, "maxQuantity": 5}


def _snack(n: int, variations: list[dict]) -> dict:
    return {"displayName": f"SuperYou Chips - {FLAVOURS[n - 1]} (No Palm oil, Baked not fried)",
            "brand": "SuperYou", "variations": variations}


# search "snacks": 12 real products listed as 13 entries. Chips 2 appears twice (same spin, the second copy adds an
# 80 g), chips 3 has an out-of-stock 80 g, and chips 7 is out of stock.
BIG_SEARCH = {"nextOffset": "1", "products": [
    *[_snack(n, [_chips(n, in_stock=n != 7)] + ([_chips(30, "80 g", False)] if n == 3 else []))
      for n in range(1, 13)],
    _snack(2, [_chips(2), _chips(20, "80 g")]),
], "similarProducts": []}
ADDRESSES_P1 = {"addresses": [
    {"id": "zzzz9999__OTHERsuffix", "addressLine": "1 Test Lane, Faketown", "phoneNumber": "****0000",
     "addressCategory": "Other", "addressTag": "Office"}],
    "pagination": {"page": 1, "totalPages": 2, "hasMore": True}}
ADDRESSES_P2 = {"addresses": [
    {"id": ADDRESS.split("__")[0], "addressLine": "2 Test Road, Faketown", "phoneNumber": "****0000",
     "addressCategory": "Home", "addressTag": "Flat 12"}],  # id given without the "__" suffix
    "pagination": {"page": 2, "totalPages": 2, "hasMore": False}}


class FakeMcp:
    def __init__(self, valid_token="access-1", tools=REQUIRED_TOOLS):
        self.valid_token = valid_token
        self.tools = set(tools)
        self.calls: list[tuple[str, str]] = []  # (tool, bearer) for every tools/call
        self.rate_limit_next = 0
        self.initializes = 0

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        if request.method == "DELETE":
            return httpx2.Response(200)
        if request.method == "GET":
            return httpx2.Response(405)
        bearer = request.headers.get("authorization", "")
        if bearer != f"Bearer {self.valid_token}":
            return httpx2.Response(401, headers={"www-authenticate": 'Bearer realm="mcp"'}, json={})
        msg = json.loads(request.content)
        if "id" not in msg:
            return httpx2.Response(202)
        if msg["method"] == "tools/call" and self.rate_limit_next:
            self.rate_limit_next -= 1
            return httpx2.Response(429, headers={"retry-after": "7"}, json={})
        match msg["method"]:
            case "initialize":
                self.initializes += 1
                result = {"protocolVersion": msg["params"]["protocolVersion"],
                          "capabilities": {"tools": {}}, "serverInfo": {"name": "fake", "version": "1"}}
            case "tools/list":
                result = {"tools": [{"name": n, "inputSchema": {"type": "object"}} for n in sorted(self.tools)]}
            case "tools/call":
                name = msg["params"]["name"]
                self.calls.append((name, bearer))
                result = self.tool_result(name, msg["params"]["arguments"])
            case _:
                result = {}
        return httpx2.Response(200, json={"jsonrpc": "2.0", "id": msg["id"], "result": result})

    @staticmethod
    def tool_result(name, args):
        if name == "search_products":  # text-only, like the real server: instruction block, then JSON
            found = BIG_SEARCH if args.get("query") == "snacks" else SEARCH
            if args.get("query") == "tomato puree":  # nothing matches
                found = {"nextOffset": None, "products": [], "similarProducts": []}
            return {"content": [{"type": "text", "text": DISPLAY_TEXT},
                                {"type": "text", "text": json.dumps(found)}]}
        if name == "get_addresses":
            data = ADDRESSES_P1 if args["page"] == 1 else ADDRESSES_P2
            return {"content": [{"type": "text", "text": "Found addresses"}], "structuredContent": data}
        return {"content": [{"type": "text", "text": "{}"}], "isError": True}


# ---- helpers ----

@pytest.fixture
def conn(tmp_path):
    c = db.connect(tmp_path / "t.db")
    db.migrate(c)
    return c


class Rig:
    def __init__(self, tmp_path, conn, mcp_token="access-1"):
        self.conn = conn
        self.server = FakeAuthServer()
        self.mcp = FakeMcp(mcp_token)
        self.now = 1_000_000.0
        self.auth = SwiggyAuth(SecretStore(conn, Fernet.generate_key().decode()), conn, httpx.MockTransport(self.server), lambda: self.now)
        self.sleeps: list[float] = []

        async def sleep(s):
            self.sleeps.append(s)
        self.client = SwiggyClient(self.auth, "https://mcp.test/im", httpx2.MockTransport(self.mcp), sleep)

    def seed(self, access="access-1", refresh="refresh-0", expires_in=432000):
        self.auth._save({"client_id": "swiggy-mcp", "access_token": access,
                         "refresh_token": refresh, "expires_at": self.now + expires_in})


@pytest.fixture
def rig(tmp_path, conn):
    return Rig(tmp_path, conn)


def kv(conn, key):
    return conn.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()[0]


# ---- OAuth ----

def test_dcr_runs_once_and_is_cached(rig):
    async def go():
        assert await rig.auth.register_client() == "swiggy-mcp"
        assert await rig.auth.register_client() == "swiggy-mcp"
    asyncio.run(go())
    (body,) = rig.server.of("register")
    assert body["redirect_uris"] == [REDIRECT_URI]
    assert body["token_endpoint_auth_method"] == "none"
    assert rig.auth._load()["client_id"] == "swiggy-mcp"


def test_authorize_url_has_pkce_state_redirect_scope_and_resource(rig):
    url, state, verifier = asyncio.run(rig.auth.start_login())
    parsed = urlparse(url)
    q = {k: v[0] for k, v in parse_qs(parsed.query).items()}
    assert f"{parsed.scheme}://{parsed.netloc}{parsed.path}" == "https://mcp.swiggy.com/auth/authorize"
    assert q["code_challenge_method"] == "S256"
    assert q["code_challenge"] == base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    assert q["state"] == state
    assert q["redirect_uri"] == "http://localhost:8765/callback"
    assert q["scope"] == "mcp:tools mcp:resources mcp:prompts"
    assert q["client_id"] == "swiggy-mcp" and q["response_type"] == "code"
    assert q["resource"] == "https://mcp.swiggy.com"


def test_finish_login_exchanges_code_and_saves_tokens_encrypted(rig):
    async def go():
        url, state, verifier = await rig.auth.start_login()
        await rig.auth.finish_login(f"{REDIRECT_URI}?code=the-code&state={state}", state, verifier)
        return verifier
    verifier = asyncio.run(go())
    (form,) = rig.server.of("authorization_code")
    assert form["code"] == "the-code" and form["code_verifier"] == verifier
    assert form["client_id"] == "swiggy-mcp" and form["resource"] == "https://mcp.swiggy.com"
    data = rig.auth._load()
    assert data["access_token"] == "access-1" and data["refresh_token"] == "refresh-1"
    assert data["expires_at"] == rig.now + 432000
    (raw,) = rig.conn.execute("SELECT ciphertext FROM secrets WHERE name='swiggy'").fetchone()
    assert b"access-1" not in raw and b"refresh-1" not in raw
    assert kv(rig.conn, "auth_state") == "logged_in"


@pytest.mark.parametrize("pasted, message", [
    (f"{REDIRECT_URI}?code=x&state=WRONG", "state mismatch"),
    (f"{REDIRECT_URI}?state=S", "no code"),
    (f"{REDIRECT_URI}?error=access_denied&state=S", "access_denied"),
])
def test_bad_pastes_are_rejected_before_any_token_request(rig, pasted, message):
    with pytest.raises(LoginError, match=message):
        asyncio.run(rig.auth.finish_login(pasted, "S", "v"))
    assert rig.server.of("authorization_code") == []


def test_expired_code_gives_a_clear_error(rig):
    rig.server.code_status = 400
    with pytest.raises(LoginError, match="2 minutes"):
        asyncio.run(rig.auth.finish_login(f"{REDIRECT_URI}?code=old&state=S", "S", "v"))
    assert kv(rig.conn, "auth_state") == "logged_out"


def test_refresh_near_expiry_saves_rotated_refresh_token(rig):
    rig.seed(access="old-access", refresh="refresh-0", expires_in=120)  # inside the 5 minute margin
    assert asyncio.run(rig.auth.access_token()) == "access-1"
    (form,) = rig.server.of("refresh_token")
    assert form["refresh_token"] == "refresh-0" and form["client_id"] == "swiggy-mcp"
    assert rig.auth._load()["refresh_token"] == "refresh-1"


def test_fresh_token_is_returned_without_a_request(rig):
    rig.seed(access="still-good")
    assert asyncio.run(rig.auth.access_token()) == "still-good"
    assert rig.server.requests == []


def test_refresh_failure_logs_out(rig):
    rig.seed(expires_in=10)
    rig.server.refresh_status = 400
    with pytest.raises(LoggedOut):
        asyncio.run(rig.auth.access_token())
    assert kv(rig.conn, "auth_state") == "logged_out"
    assert rig.auth._load() == {"client_id": "swiggy-mcp"}
    with pytest.raises(LoggedOut):  # and it stays logged out, with no further request
        asyncio.run(rig.auth.access_token())
    assert len(rig.server.requests) == 1


# ---- MCP client ----

def test_search_parses_text_block_and_sends_bearer(rig):
    rig.seed()
    products = asyncio.run(catalog.search(rig.client, ADDRESS, "milk"))
    assert [p.name for p in products] == ["Farmfresh Toned Milk", "Meadow Oat Drink"]
    v = products[0].variants
    assert (v[0].spin_id, v[0].size, v[0].mrp_paise, v[0].offer_paise, v[0].in_stock, v[0].max_qty) == \
        ("SPIN000001", "500 ml x 4", 12000, 11600, True, 1)
    assert v[1].offer_paise == 6650 and not v[1].in_stock
    assert rig.mcp.calls == [("search_products", "Bearer access-1")]


def test_address_label_pages_and_caches(rig):
    rig.seed()

    async def go():
        async with rig.client.turn():
            first = await catalog.address_label(rig.client, ADDRESS, rig.conn)
        return first, await catalog.address_label(rig.client, ADDRESS, rig.conn)
    assert asyncio.run(go()) == ("Flat 12", "Flat 12")
    assert [t for t, _ in rig.mcp.calls] == ["get_addresses", "get_addresses"]  # two pages, then cached


def test_one_session_per_turn_and_tools_checked_once(rig):
    rig.seed()

    async def go():
        async with rig.client.turn():
            await rig.client.call("get_addresses", {"page": 1})
            await rig.client.call("get_addresses", {"page": 2})
        async with rig.client.turn():
            pass  # a turn with no call opens nothing
        async with rig.client.turn():
            await rig.client.call("get_addresses", {"page": 1})
    asyncio.run(go())
    assert rig.mcp.initializes == 2


def test_missing_required_tool_is_logged_as_error(rig, caplog):
    rig.seed()
    rig.mcp.tools -= {"checkout"}
    with caplog.at_level(logging.ERROR):
        asyncio.run(rig.client.call("get_addresses", {"page": 1}))
    assert any("checkout" in r.message and r.levelname == "ERROR" for r in caplog.records)


def test_401_refreshes_and_retries_once(rig):
    rig.seed(access="stale")
    rig.mcp.valid_token = "access-1"  # what the fake auth server hands out after a refresh
    data = asyncio.run(rig.client.call("get_addresses", {"page": 1}))
    assert data["addresses"][0]["addressTag"] == "Office"
    assert len(rig.server.of("refresh_token")) == 1
    assert rig.mcp.calls == [("get_addresses", "Bearer access-1")]


def test_second_401_means_logged_out(rig):
    rig.seed(access="stale")
    rig.mcp.valid_token = "nothing-matches"
    with pytest.raises(LoggedOut):
        asyncio.run(rig.client.call("get_addresses", {"page": 1}))
    assert len(rig.server.of("refresh_token")) == 1  # exactly one retry
    assert kv(rig.conn, "auth_state") == "logged_out"


def test_429_honors_retry_after_then_gives_up(rig):
    rig.seed()
    rig.mcp.rate_limit_next = 1
    asyncio.run(rig.client.call("get_addresses", {"page": 1}))
    assert rig.sleeps == [7]

    rig.mcp.rate_limit_next = 5
    from cartmates.swiggy.errors import SwiggyUnavailable
    with pytest.raises(SwiggyUnavailable):
        asyncio.run(rig.client.call("get_addresses", {"page": 1}))
    assert len(rig.sleeps) == 3  # two retries after the first failure


def test_no_429_retry_when_asked_not_to(rig):
    from cartmates.swiggy.errors import SwiggyUnavailable
    rig.seed()
    rig.mcp.rate_limit_next = 1
    with pytest.raises(SwiggyUnavailable):
        asyncio.run(rig.client.call("get_addresses", {"page": 1}, retry_rate_limit=False))
    assert rig.sleeps == [] and rig.mcp.calls == []  # no sleep, no second attempt


def test_network_error_is_swiggy_unavailable(rig):
    rig.seed()
    from cartmates.swiggy.errors import SwiggyUnavailable

    def boom(request):
        raise httpx2.ConnectError("down")
    rig.client._transport = httpx2.MockTransport(boom)
    with pytest.raises(SwiggyUnavailable):
        asyncio.run(rig.client.call("get_addresses", {"page": 1}))


# ---- full flows: update -> adapter -> lane -> handlers -> outbox ----

class Flow:
    def __init__(self, rig, address_id=ADDRESS):
        self.rig = rig
        self.outbox = FakeOutbox()
        if address_id:
            address.set_current(rig.conn, address_id)
        else:
            rig.conn.execute("DELETE FROM kv WHERE key='address_id'")
        self.handlers = Handlers(rig.conn, self.outbox, GROUP, rig.auth, rig.client)
        self.lane = Lane(self.handlers.handle, self.outbox)
        self.handlers.resubmit = self.lane.submit
        self.adapter = adapter_for(self.lane)

    async def __aenter__(self):
        self.lane.start()
        return self

    async def __aexit__(self, *exc):
        await self.lane.stop()

    async def say(self, update):
        await self.adapter.on_update(update)
        await asyncio.sleep(0.3)  # let the lane (and any replay) finish

    def texts(self):
        return [s.text for s in self.outbox.sent]


def command(text, **kw):
    return make_update(text, entities=cmd(text.split()[0]), **kw)


def pending_login(conn):
    row = conn.execute("SELECT payload_json FROM pending WHERE kind='login'").fetchone()
    return json.loads(row[0]) if row else None


def test_address_while_logged_out_is_parked_then_replayed_after_paste(rig):
    async def go():
        async with Flow(rig) as f:
            await f.say(command("/address", message_id=20))
            assert "I need a Swiggy login first" in f.texts()[1]
            assert f.texts()[0].startswith("Swiggy login:\n1) open https://mcp.swiggy.com/auth/authorize?")
            login = pending_login(rig.conn)
            assert [m["text"] for m in login["parked"]] == ["/address"]

            paste = f"http://localhost:8765/callback?code=SECRETCODE&state={login['state']}"
            await f.say(make_update(paste, reply_to_bot=True, message_id=21, user_id=2, name="Rohan"))
            return f
    f = asyncio.run(go())
    texts = f.texts()
    assert texts[2] == "Logged in to Swiggy ✅ (by Rohan)"
    assert texts[3].startswith("Delivery address: Flat 12")
    assert [b[0][0] for b in f.outbox.sent[3].buttons] == ["Office", "✓ Flat 12"]
    assert f.outbox.sent[3].reply_to == 20  # the replay answers the original message
    assert f.outbox.deleted == [(GROUP, 21)]
    assert pending_login(rig.conn) is None
    assert kv(rig.conn, "auth_state") == "logged_in"
    # The auth code must not be in SQLite anywhere.
    dump = "\n".join(str(tuple(r)) for t in ("events", "pending", "chat_memory", "kv")
                     for r in rig.conn.execute(f"SELECT * FROM {t}"))
    assert "SECRETCODE" not in dump
    payloads = [json.loads(r[0]) for r in rig.conn.execute(
        "SELECT payload_json FROM events WHERE actor='2'")]
    assert payloads == [{"text": "[login url redacted]"}]
    # The replayed /address was logged once, not twice.
    assert rig.conn.execute("SELECT count(*) FROM events WHERE actor='1'").fetchone()[0] == 1


def test_bad_state_paste_is_rejected_and_login_stays_pending(rig):
    async def go():
        async with Flow(rig) as f:
            await f.say(command("/login"))
            await f.say(make_update("http://localhost:8765/callback?code=abc&state=WRONG",
                                    reply_to_bot=True, message_id=11))
            return f
    f = asyncio.run(go())
    assert "state mismatch" in f.texts()[-1]
    assert pending_login(rig.conn) is not None
    assert f.outbox.deleted == []
    assert rig.server.of("authorization_code") == []


def test_expired_pending_login_is_dropped(rig):
    async def go():
        async with Flow(rig) as f:
            await f.say(command("/login"))
            state = pending_login(rig.conn)["state"]
            rig.conn.execute("UPDATE pending SET expires_at='2000-01-01T00:00:00+00:00'")
            await f.say(make_update(f"http://localhost:8765/callback?code=abc&state={state}",
                                    reply_to_bot=True, message_id=11))
            return f
    f = asyncio.run(go())
    assert "expired" in f.texts()[-1]
    assert pending_login(rig.conn) is None
    assert kv(rig.conn, "auth_state") == "logged_out"


def test_repeat_login_resends_the_same_link_and_logged_in_says_so(rig):
    async def go():
        async with Flow(rig) as f:
            await f.say(command("/login"))
            await f.say(command("/login", message_id=11))
            rig.seed()
            await f.say(command("/login", message_id=12))
            return f
    f = asyncio.run(go())
    assert f.texts()[0] == f.texts()[1]
    assert rig.conn.execute("SELECT count(*) FROM pending").fetchone()[0] == 1
    assert f.texts()[2] == "Already logged in to Swiggy."
    assert len(rig.server.of("register")) == 1


def test_status_shows_login_and_address_label(rig):
    rig.seed()

    async def go():
        async with Flow(rig) as f:
            await f.say(command("/status"))
            return f
    f = asyncio.run(go())
    assert "Swiggy address: Flat 12" in f.texts()[0]
    assert "FAKEsuffix" not in f.texts()[0]  # the id is never printed


def test_status_when_logged_out_or_unpinned(rig):
    async def go(address_id):
        async with Flow(rig, address_id) as f:
            await f.say(command("/status"))
            return f.texts()[0]
    assert "Swiggy address: unknown (log in)" in asyncio.run(go(ADDRESS))
    assert "Swiggy address: not set" in asyncio.run(go(None))
    assert rig.mcp.calls == []


def test_removed_commands_make_no_swiggy_call(rig):
    async def go():
        async with Flow(rig, None) as f:
            for i, text in enumerate(("/search milk", "/add milk", "/remove 1")):
                await f.say(command(text, message_id=10 + i))
            return f
    f = asyncio.run(go())
    assert all(t.startswith("Just @mention me") for t in f.texts()) and len(f.texts()) == 3
    assert rig.mcp.calls == []


def test_expired_access_token_recovers_via_401_on_a_command(rig):
    rig.seed(access="stale")  # looks valid by its expiry but the server rejects it

    async def go():
        async with Flow(rig) as f:
            await f.say(command("/address"))
            return f
    f = asyncio.run(go())
    assert f.texts()[0].startswith("Delivery address:")
    assert len(rig.server.of("refresh_token")) == 1
