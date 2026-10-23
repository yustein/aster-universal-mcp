"""Durable browser-bound approval completion tokens, independent of process memory."""
import hashlib
import hmac
import secrets
import sqlite3
import time

def _hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()

class ApprovalSessions:
    def __init__(self, db_path: str):
        self.db_path = db_path
        with self._db() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS approval_sessions(
                request_id TEXT PRIMARY KEY, browser_digest TEXT NOT NULL,
                nonce_digest TEXT, expires_at INTEGER NOT NULL)""")

    def _db(self):
        db = sqlite3.connect(self.db_path, timeout=10, isolation_level="IMMEDIATE")
        db.execute("PRAGMA busy_timeout=10000")
        return db

    def bind_browser(self, request_id: str, *, now: int | None = None) -> str | None:
        now = int(time.time()) if now is None else now
        browser = secrets.token_urlsafe(32)
        with self._db() as db:
            db.execute("INSERT OR IGNORE INTO approval_sessions VALUES (?,?,NULL,?)",
                       (request_id, _hash(browser), now + 300))
            if db.execute("SELECT changes()").fetchone()[0] != 1:
                return None
        return browser

    def issue_completion(self, request_id: str, browser: str, *, now: int | None = None) -> str | None:
        now = int(time.time()) if now is None else now
        # Stable for this browser/request: repeated GETs must not invalidate an already rendered form.
        nonce = hmac.new(browser.encode("utf-8"), ("aster-complete:"+request_id).encode("utf-8"), hashlib.sha256).hexdigest()
        with self._db() as db:
            row = db.execute("UPDATE approval_sessions SET nonce_digest=? WHERE request_id=? AND browser_digest=? AND expires_at>? RETURNING request_id",
                             (_hash(nonce), request_id, _hash(browser), now)).fetchone()
        return nonce if row else None

    def consume(self, request_id: str, browser: str, nonce: str, *, now: int | None = None) -> bool:
        now = int(time.time()) if now is None else now
        with self._db() as db:
            row = db.execute("DELETE FROM approval_sessions WHERE request_id=? AND browser_digest=? AND nonce_digest=? AND expires_at>? RETURNING request_id",
                             (request_id, _hash(browser), _hash(nonce), now)).fetchone()
        return row is not None

    def consume_nonce(self, request_id: str, browser: str, nonce: str, *, now: int | None = None) -> bool:
        """Consume the pairing nonce but retain browser binding for OAuth completion."""
        now = int(time.time()) if now is None else now
        with self._db() as db:
            row = db.execute(
                "UPDATE approval_sessions SET nonce_digest=NULL "
                "WHERE request_id=? AND browser_digest=? AND nonce_digest=? AND expires_at>? "
                "RETURNING request_id",
                (request_id, _hash(browser), _hash(nonce), now)).fetchone()
        return row is not None

    def completion_diagnostic(self, request_id: str, browser: str, nonce: str) -> str:
        """Return only a coarse failure reason; never log or reveal credentials."""
        with self._db() as db:
            row=db.execute("SELECT browser_digest,nonce_digest,expires_at FROM approval_sessions WHERE request_id=?",
                           (request_id,)).fetchone()
        if row is None:
            return "session_missing"
        if int(time.time()) >= row[2]:
            return "session_expired"
        if not browser:
            return "cookie_missing"
        if not hmac.compare_digest(row[0], _hash(browser)):
            return "browser_mismatch"
        if not nonce or row[1] is None or not hmac.compare_digest(row[1], _hash(nonce)):
            return "nonce_mismatch"
        return "concurrent_completion"
