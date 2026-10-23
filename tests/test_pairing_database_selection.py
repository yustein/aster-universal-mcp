"""Prevent accidental pairing issuance in a different Windows database."""
from pathlib import Path
import pytest
from pairing_admin import configured_db

def test_windows_rejects_implicit_database():
    with pytest.raises(ValueError, match="Specify --db"):
        configured_db(environ={}, platform="nt")

def test_windows_accepts_exact_gateway_database():
    explicit = r"C:\Users\astertest\AsterClaudeBeta\state\windows-oauth.sqlite"
    assert configured_db(explicit, environ={}, platform="nt") == explicit

def test_windows_uses_gateway_environment_path():
    win = r"C:\Users\astertest\AsterClaudeBeta\state\windows-oauth.sqlite"
    assert configured_db(environ={"ASTER_WINDOWS_OAUTH_DB": win}, platform="nt") == win

def test_linux_retains_default_when_not_set():
    assert configured_db(environ={}, platform="posix") == "/var/lib/aster-universal-mcp/oauth.sqlite"
