import asyncio
import logging

import httpx
import pytest
from cryptography.fernet import Fernet

from cartmates.store import db
from cartmates.store.secrets import SecretStore
from cartmates.swiggy.auth import REDIRECT_URI, SwiggyAuth
from test_swiggy import FakeAuthServer


@pytest.fixture
def conn(tmp_path):
    c = db.connect(tmp_path / "t.db")
    db.migrate(c)
    return c


def key() -> str:
    return Fernet.generate_key().decode()


def auth_for(conn, master_key, server=None):
    return SwiggyAuth(SecretStore(conn, master_key), conn,
                      httpx.MockTransport(server or FakeAuthServer()), lambda: 1_000_000.0)


def login(auth):
    async def go():
        _, state, verifier = await auth.start_login()
        await auth.finish_login(f"{REDIRECT_URI}?code=c&state={state}", state, verifier)
    asyncio.run(go())


def test_put_then_get_round_trips(conn):
    store = SecretStore(conn, key())
    store.put("swiggy", {"access_token": "a", "n": 1})
    assert store.get("swiggy") == {"access_token": "a", "n": 1}
    store.put("swiggy", {"access_token": "b"})
    assert store.get("swiggy") == {"access_token": "b"}
    store.delete("swiggy")
    assert store.get("swiggy") is None and store.unreadable is False


def test_new_auth_on_same_connection_sees_earlier_login(conn):
    k = key()
    login(auth_for(conn, k))
    later = auth_for(conn, k)
    assert later.logged_in()
    assert asyncio.run(later.access_token()) == "access-1"


def test_wrong_key_reads_as_logged_out_without_raising(conn, caplog):
    login(auth_for(conn, key()))
    auth = auth_for(conn, key())
    with caplog.at_level(logging.WARNING):
        assert auth.store.get("swiggy") is None
    assert auth.store.unreadable is True
    assert "swiggy" in caplog.text and "access-1" not in caplog.text and "refresh-1" not in caplog.text
    assert auth.logged_in() is False
    assert auth.sync_state() == "logged_out"
    # A fresh login overwrites the row under the current key.
    login(auth)
    assert auth.logged_in() and auth.store.unreadable is False


def test_put_without_key_raises(conn):
    store = SecretStore(conn, None)
    with pytest.raises(RuntimeError, match="CARTMATES_MASTER_KEY is not set"):
        store.put("swiggy", {"access_token": "a"})
