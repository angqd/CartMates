"""Paste-back OAuth for Swiggy, written by hand over httpx.

The SDK's OAuthClientProvider is not used: its callback_handler waits inside the HTTP
call for the auth code, but our code arrives as a Telegram message through the same
serial lane that would be blocked waiting. Everything the SDK sends is mirrored here
(PKCE S256, scope, and the RFC 8707 `resource`, which the SDK sends because it found
the protected-resource metadata; its value is that metadata's `resource`).

Never log a token, code, verifier or pasted URL.
"""
import asyncio
import base64
import hashlib
import json
import logging
import os
import secrets
import sqlite3
import time
from collections.abc import Callable
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse

import httpx

from queuify.swiggy.errors import LoggedOut, LoginError, SwiggyUnavailable

log = logging.getLogger(__name__)

AUTH_BASE = "https://mcp.swiggy.com/auth"
RESOURCE = "https://mcp.swiggy.com"
REDIRECT_URI = "http://localhost:8765/callback"
SCOPE = "mcp:tools mcp:resources mcp:prompts"
REFRESH_MARGIN = 300  # refresh when less than 5 minutes are left


def _challenge(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode()).digest()
    return base64.urlsafe_b64encode(digest).decode().rstrip("=")


class SwiggyAuth:
    def __init__(self, tokens_dir: Path, conn: sqlite3.Connection | None = None,
                 transport: httpx.AsyncBaseTransport | None = None,
                 clock: Callable[[], float] = time.time):
        self.path = Path(tokens_dir) / "swiggy.json"
        self.conn = conn
        self._transport = transport
        self._clock = clock
        self._refresh_lock = asyncio.Lock()

    # ---- token store ----

    def _load(self) -> dict:
        try:
            return json.loads(self.path.read_text())
        except FileNotFoundError:
            return {}

    def _save(self, data: dict) -> None:
        # Temp file + rename, so a crash never leaves half a token file.
        tmp = self.path.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump(data, f)
        os.replace(tmp, self.path)

    def _set_state(self, state: str) -> None:
        if self.conn:
            self.conn.execute("UPDATE kv SET value=? WHERE key='auth_state'", (state,))

    def logged_in(self) -> bool:
        return bool(self._load().get("refresh_token") or self._load().get("access_token"))

    def sync_state(self) -> str:
        """Set kv auth_state from the token file. Local only; no network."""
        state = "logged_in" if self.logged_in() else "logged_out"
        self._set_state(state)
        return state

    def _log_out(self) -> None:
        data = self._load()
        self._save({"client_id": data["client_id"]} if "client_id" in data else {})
        self._set_state("logged_out")

    async def _post(self, url: str, **kwargs) -> httpx.Response:
        try:
            async with httpx.AsyncClient(transport=self._transport, timeout=20) as http:
                return await http.post(url, **kwargs)
        except httpx.HTTPError as e:
            raise SwiggyUnavailable(f"could not reach Swiggy auth ({type(e).__name__})") from None

    # ---- login ----

    async def register_client(self) -> str:
        """Dynamic client registration, once. The client_id is cached in the store."""
        data = self._load()
        if data.get("client_id"):
            return data["client_id"]
        resp = await self._post(f"{AUTH_BASE}/register", json={
            "client_name": "queuify",
            "redirect_uris": [REDIRECT_URI],
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"],
            "token_endpoint_auth_method": "none",
            "scope": SCOPE,
        })
        if resp.status_code not in (200, 201):
            raise SwiggyUnavailable(f"client registration failed (HTTP {resp.status_code})")
        data["client_id"] = resp.json()["client_id"]
        self._save(data)
        return data["client_id"]

    async def start_login(self) -> tuple[str, str, str]:
        """Returns (authorize_url, state, verifier). The caller keeps state and verifier."""
        client_id = await self.register_client()
        verifier = secrets.token_urlsafe(48)
        state = secrets.token_urlsafe(24)
        query = urlencode({
            "response_type": "code",
            "client_id": client_id,
            "redirect_uri": REDIRECT_URI,
            "state": state,
            "code_challenge": _challenge(verifier),
            "code_challenge_method": "S256",
            "resource": RESOURCE,
            "scope": SCOPE,
        })
        return f"{AUTH_BASE}/authorize?{query}", state, verifier

    async def finish_login(self, pasted: str, state: str, verifier: str) -> None:
        code = self._parse_paste(pasted, state)
        client_id = await self.register_client()
        resp = await self._post(f"{AUTH_BASE}/token", data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": REDIRECT_URI,
            "client_id": client_id,
            "code_verifier": verifier,
            "resource": RESOURCE,
        })
        if resp.status_code in (400, 401):
            raise LoginError("Swiggy rejected that code. Codes last 2 minutes and work once; "
                             "open the login link again and reply faster.")
        if resp.status_code != 200:
            raise LoginError(f"Swiggy login failed (HTTP {resp.status_code}). Try again.")
        self._store_tokens(resp.json(), {"client_id": client_id})
        log.info("swiggy login completed")

    @staticmethod
    def _parse_paste(pasted: str, state: str) -> str:
        # The user may paste extra words around the URL, so find the URL token first.
        word = next((w for w in pasted.split() if "/callback?" in w), "")
        query = parse_qs(urlparse(word).query)
        if "error" in query:
            raise LoginError(f"Swiggy refused the login ({query['error'][0]}).")
        if not secrets.compare_digest(query.get("state", [""])[0], state):
            raise LoginError("That link belongs to a different login attempt (state mismatch). "
                             "Use the latest login link.")
        if not query.get("code", [""])[0]:
            raise LoginError("That URL has no code= part. Copy the full URL of the last page.")
        return query["code"][0]

    def _store_tokens(self, body: dict, base: dict) -> None:
        old = self._load()
        data = {
            "client_id": base.get("client_id") or old.get("client_id"),
            "access_token": body["access_token"],
            # Refresh tokens rotate; keep the old one only if the server sent no new one.
            "refresh_token": body.get("refresh_token") or old.get("refresh_token"),
            "expires_at": self._clock() + int(body.get("expires_in", 3600)),
        }
        self._save(data)
        self._set_state("logged_in")

    # ---- using the tokens ----

    async def access_token(self) -> str:
        data = self._load()
        if not data.get("access_token"):
            self._set_state("logged_out")
            raise LoggedOut("no Swiggy token")
        if data["expires_at"] - self._clock() > REFRESH_MARGIN:
            return data["access_token"]
        return await self.force_refresh()

    async def force_refresh(self) -> str:
        """Refresh now. Also used after a 401, when the token is bad despite its expiry."""
        async with self._refresh_lock:
            data = self._load()
            if not data.get("refresh_token"):
                self._log_out()
                raise LoggedOut("no refresh token")
            resp = await self._post(f"{AUTH_BASE}/token", data={
                "grant_type": "refresh_token",
                "refresh_token": data["refresh_token"],
                "client_id": data["client_id"],
                "resource": RESOURCE,
            })
            if resp.status_code in (400, 401):
                log.warning("swiggy refresh rejected (HTTP %s); logged out", resp.status_code)
                self._log_out()
                raise LoggedOut("refresh token rejected")
            if resp.status_code != 200:
                raise SwiggyUnavailable(f"token refresh failed (HTTP {resp.status_code})")
            # _store_tokens saves the rotated refresh token before we return anything.
            self._store_tokens(resp.json(), {})
            return self._load()["access_token"]

    def mark_logged_out(self) -> None:
        self._log_out()
