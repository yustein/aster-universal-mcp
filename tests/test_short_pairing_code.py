"""Human-friendly owner pairing code security and compatibility tests."""
import hashlib
import sqlite3
import time

from pairing import PairingStore, PAIRING_ALPHABET, PAIRING_LENGTH


def test_short_code_has_60_bit_alphabet_and_is_single_use(tmp_path):
    db = tmp_path / "oauth.sqlite"
    store = PairingStore(str(db))
    code = store.issue(ttl_seconds=900)
    assert len(PAIRING_ALPHABET) == 32
    assert PAIRING_LENGTH == 12
    assert len(code) == PAIRING_LENGTH
    assert all(ch in PAIRING_ALPHABET for ch in code)
    pretty = "-".join(code[i:i + 4] for i in range(0, len(code), 4))
    assert store.consume(pretty.lower())
    assert not store.consume(pretty)


def test_short_code_stored_as_hash_and_attempts_are_limited(tmp_path):
    db = tmp_path / "oauth.sqlite"
    store = PairingStore(str(db))
    code = store.issue(ttl_seconds=900)
    with sqlite3.connect(db) as conn:
        digest, attempts = conn.execute("SELECT digest, attempts FROM pairing").fetchone()
    assert digest == hashlib.sha256(code.encode("ascii")).digest()
    assert attempts == 0
    for _ in range(5):
        assert not store.consume("AAAA-AAAA-AAAA")
    assert not store.consume(code)


def test_new_issue_invalidates_previous_code(tmp_path):
    store = PairingStore(str(tmp_path / "oauth.sqlite"))
    first = store.issue()
    second = store.issue()
    assert first != second
    assert not store.consume(first)
    assert store.consume(second)


def test_rejected_input_and_expired_code(tmp_path):
    db = tmp_path / "oauth.sqlite"
    store = PairingStore(str(db))
    code = store.issue()
    assert not store.consume("0" * 12)
    assert not store.consume("A" * 300)
    with sqlite3.connect(db) as conn:
        conn.execute("UPDATE pairing SET expires_at=?", (int(time.time()) - 1,))
    assert not store.consume(code)
