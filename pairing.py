"""Local one-time enrollment code store. Not a complete OAuth server."""
from __future__ import annotations
import hashlib
import hmac
import secrets
import sqlite3
import time
from pathlib import Path

# 32 unambiguous symbols: 12 characters provide 60 bits of entropy.
PAIRING_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
PAIRING_LENGTH = 12


class PairingStore:
    def __init__(self, db_path: str):
        self.db_path = str(db_path)
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS pairing (
                id INTEGER PRIMARY KEY CHECK(id=1),
                digest BLOB NOT NULL,
                expires_at INTEGER NOT NULL,
                attempts INTEGER NOT NULL DEFAULT 0
            )""")

    def _connect(self):
        db = sqlite3.connect(self.db_path, timeout=10, isolation_level="IMMEDIATE")
        db.execute("PRAGMA busy_timeout=10000")
        return db

    def issue(self, ttl_seconds: int = 300) -> str:
        if not 30 <= ttl_seconds <= 900:
            raise ValueError("pairing lifetime must be 30..900 seconds")
        code = "".join(secrets.choice(PAIRING_ALPHABET) for _ in range(PAIRING_LENGTH))
        digest = hashlib.sha256(code.encode("ascii")).digest()
        with self._connect() as db:
            db.execute("DELETE FROM pairing")
            db.execute("INSERT INTO pairing (id,digest,expires_at,attempts) VALUES (1,?,?,0)",
                       (digest, int(time.time()) + ttl_seconds))
        return code

    def consume(self, code: str) -> bool:
        if not isinstance(code, str) or len(code) > 256:
            return False
        # Human entry accepts spaces/hyphens and lowercase, but is otherwise strict.
        code = code.replace("-", "").replace(" ", "").upper()
        if len(code) != PAIRING_LENGTH or any(ch not in PAIRING_ALPHABET for ch in code):
            return False
        digest = hashlib.sha256(code.encode("ascii")).digest()
        with self._connect() as db:
            row = db.execute("SELECT digest,expires_at,attempts FROM pairing WHERE id=1").fetchone()
            if row is None:
                return False
            saved, expiry, attempts = row
            if attempts >= 5 or int(time.time()) >= expiry:
                db.execute("DELETE FROM pairing")
                return False
            db.execute("UPDATE pairing SET attempts=attempts+1 WHERE id=1")
            if not hmac.compare_digest(saved, digest):
                return False
            db.execute("DELETE FROM pairing")
            return True

    def revoke(self):
        with self._connect() as db:
            db.execute("DELETE FROM pairing")
