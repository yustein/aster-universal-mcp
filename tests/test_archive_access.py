"""Fail-closed, exact-identity archive grant boundaries."""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from aster_archive.access import ArchiveAccessError, ArchiveScope, load_grants, scope_for_client


def write_acl(path, clients):
    path.write_text(json.dumps({"version": 1, "clients": clients}), encoding="utf-8")
    path.chmod(0o600)
    return path


def grant(owner="owner", sources="*", projects="*"):
    return {"owner_id": owner, "source_ids": sources, "project_ids": projects}


def test_exact_identity_no_implicit_wildcard_and_runtime_revocation(tmp_path):
    path = write_acl(tmp_path / "grants.json", {
        "Owner": grant(), "worker": grant(sources=["personal-account"], projects=["coordinator"]),
    })
    assert scope_for_client(path, "Owner") == ArchiveScope("Owner", "owner", None, None)
    worker = scope_for_client(path, "worker")
    assert worker.source_ids == ("personal-account",) and worker.project_ids == ("coordinator",)
    with pytest.raises(ArchiveAccessError, match="ARCHIVE_ACCESS_DENIED"):
        scope_for_client(path, "owner")
    write_acl(path, {"Owner": grant()})
    with pytest.raises(ArchiveAccessError, match="ARCHIVE_ACCESS_DENIED"):
        scope_for_client(path, "worker")


@pytest.mark.parametrize("raw", [
    b"not json", b"[]", b"null", b'{"version":true,"clients":{}}',
    b'{"version":2,"clients":{}}', b'{"version":1,"clients":{},"default":"*"}',
    b'{"version":1,"clients":{"client":{"owner_id":"owner","source_ids":"*"}}}',
    b'{"version":1,"clients":{"client":{"owner_id":"*","source_ids":"*","project_ids":"*"}}}',
    b'{"version":1,"clients":{"client":{"owner_id":"owner","source_ids":null,"project_ids":"*"}}}',
    b'{"version":1,"clients":{"client":{"owner_id":"owner","source_ids":["*"],"project_ids":"*"}}}',
    b'{"version":1,"clients":{"client":{"owner_id":"owner","source_ids":["x","x"],"project_ids":"*"}}}',
    b'{"version":1,"clients":{},"clients":{"unrelated-private-value":{}}}',
    b'{"version":1,"clients":{"client":{"owner_id":"owner","source_ids":"*","project_ids":"*","admin":true}}}',
    b'\xff',
])
def test_malformed_grants_fail_closed_without_contents(tmp_path, raw):
    path = tmp_path / "private-grants.json"
    path.write_bytes(raw)
    path.chmod(0o600)
    with pytest.raises(ArchiveAccessError, match="ARCHIVE_AUTHORIZATION_STATE_UNAVAILABLE") as error:
        load_grants(path)
    assert str(path) not in str(error.value)
    assert "unrelated-private-value" not in str(error.value)


def test_empty_filters_are_explicit_deny_all(tmp_path):
    path = write_acl(tmp_path / "grants.json", {"client": grant(sources=[], projects=[])})
    assert scope_for_client(path, "client").source_ids == ()
    assert scope_for_client(path, "client").project_ids == ()


def test_missing_and_oversized_config_rejected(tmp_path):
    from aster_archive.access import MAX_ACL_BYTES

    path = tmp_path / "missing.json"
    with pytest.raises(ArchiveAccessError):
        load_grants(path)
    path.write_bytes(b" " * (MAX_ACL_BYTES + 1))
    with pytest.raises(ArchiveAccessError):
        load_grants(path)


@pytest.mark.skipif(os.name != "posix", reason="POSIX protected-file qualification")
def test_unsafe_permissions_and_symlinks_rejected(tmp_path):
    path = write_acl(tmp_path / "grants.json", {"client": grant()})
    path.chmod(0o666)
    with pytest.raises(ArchiveAccessError):
        load_grants(path)
    path.chmod(0o600)
    link = tmp_path / "link.json"
    link.symlink_to(path)
    with pytest.raises(ArchiveAccessError):
        load_grants(link)


def test_unreadable_config_does_not_expose_error(tmp_path, monkeypatch):
    def unavailable(*args, **kwargs):
        raise PermissionError("sensitive-host-error")
    monkeypatch.setattr(os, "open", unavailable)
    with pytest.raises(ArchiveAccessError) as error:
        load_grants(tmp_path / "private-file")
    assert "sensitive-host-error" not in str(error.value)
