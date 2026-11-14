"""Per-household secrets, encrypted with Fernet under CARTMATES_MASTER_KEY.

Never log a secret's value; names only.
"""
import json
import logging
import sqlite3
from datetime import datetime, timezone

from cryptography.fernet import Fernet, InvalidToken

log = logging.getLogger(__name__)


class SecretStore:
    def __init__(self, conn: sqlite3.Connection, master_key: str | None):
        self.conn = conn
        self._fernet = Fernet(master_key) if master_key else None
        # True once a row existed but could not be decrypted (no key or wrong key).
        self.unreadable = False

    def get(self, name: str) -> dict | None:
        row = self.conn.execute("SELECT ciphertext FROM secrets WHERE name=?", (name,)).fetchone()
        if row is None:
            return None
        try:
            if self._fernet is None:
                raise InvalidToken
            return json.loads(self._fernet.decrypt(row[0]))
        except InvalidToken:
            if not self.unreadable:  # warn once, not on every read
                log.warning("secret %r cannot be read: CARTMATES_MASTER_KEY is %s", name,
                            "not set" if self._fernet is None else "not the key it was saved with")
            self.unreadable = True
            return None

    def put(self, name: str, value: dict) -> None:
        if self._fernet is None:
            raise RuntimeError("CARTMATES_MASTER_KEY is not set")
        ciphertext = self._fernet.encrypt(json.dumps(value).encode())
        self.conn.execute(
            "INSERT INTO secrets (name, ciphertext, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT(name) DO UPDATE SET ciphertext=excluded.ciphertext, "
            "updated_at=excluded.updated_at",
            (name, ciphertext, datetime.now(timezone.utc).isoformat()))
        self.unreadable = False

    def delete(self, name: str) -> None:
        self.conn.execute("DELETE FROM secrets WHERE name=?", (name,))
