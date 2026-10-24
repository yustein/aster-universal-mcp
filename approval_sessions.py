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
            db.execute("BEGIN IMMEDIATE")
            db.execute("""CREATE TABLE IF NOT EXISTS approval_sessions(
                request_id TEXT PRIMARY KEY, browser_digest TEXT NOT NULL,
                nonce_digest TEXT, expires_at INTEGER NOT NULL,
                nonce_generation INTEGER NOT NULL DEFAULT 0)""")
            columns = {row[1] for row in db.execute("PRAGMA table_info(approval_sessions)")}
            if "nonce_generation" not in columns:
                db.execute("ALTER TABLE approval_sessions ADD COLUMN "
                           "nonce_generation INTEGER NOT NULL DEFAULT 0")
                # A legacy NULL may represent a consumed form. Never revive its
                # generation-zero nonce when the application is upgraded.
                db.execute("UPDATE approval_sessions SET nonce_generation=1 "
                           "WHERE nonce_digest IS NULL")

    def _db(self):
        db = sqlite3.connect(self.db_path, timeout=10, isolation_level="IMMEDIATE")
        db.execute("PRAGMA busy_timeout=10000")
        return db

    def bind_browser(self, request_id: str, *, now: int | None = None) -> str | None:
        now = int(time.time()) if now is None else now
        browser = secrets.token_urlsafe(32)
        with self._db() as db:
            db.execute("INSERT OR IGNORE INTO approval_sessions "
                       "(request_id,browser_digest,nonce_digest,expires_at) VALUES (?,?,NULL,?)",
                       (request_id, _hash(browser), now + 300))
            if db.execute("SELECT changes()").fetchone()[0] != 1:
                return None
        return browser

    def issue_completion(self, request_id: str, browser: str, *, now: int | None = None) -> str | None:
        now = int(time.time()) if now is None else now
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT nonce_generation FROM approval_sessions "
                "WHERE request_id=? AND browser_digest=? AND expires_at>?",
                (request_id, _hash(browser), now)).fetchone()
            if row is None:
                return None
            # Refreshes keep an unsubmitted form usable. Consumption advances the
            # generation, so refreshing can never revive a submitted nonce.
            # Generation zero retains the pre-migration derivation for live forms.
            message = "aster-complete:" + request_id
            if row[0]:
                message += ":" + str(row[0])
            nonce = hmac.new(browser.encode("utf-8"), message.encode("utf-8"),
                             hashlib.sha256).hexdigest()
            db.execute("UPDATE approval_sessions SET nonce_digest=? WHERE request_id=?",
                       (_hash(nonce), request_id))
        return nonce

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
                "UPDATE approval_sessions SET nonce_digest=NULL, "
                "nonce_generation=nonce_generation+1 "
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
