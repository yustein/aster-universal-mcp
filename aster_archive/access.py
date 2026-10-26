"""Exact-client archive grants, independently reloaded for every read.

OAuth authenticates a client. This protected local file supplies its archive
owner/source/project scope; neither scope nor owner comes from tool arguments.
"""
from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import stat

MAX_ACL_BYTES = 1024 * 1024
MAX_CLIENTS = 256
MAX_FILTER_ITEMS = 256


class ArchiveAccessError(PermissionError):
    """A deliberate, sanitized authorization failure safe for a caller."""


@dataclass(frozen=True)
class ArchiveScope:
    client_id: str
    owner_id: str
    source_ids: tuple[str, ...] | None
    project_ids: tuple[str, ...] | None


def _identifier(value: object) -> bool:
    return (
        isinstance(value, str)
        and 1 <= len(value) <= 256
        and value != "*"
        and not any(ord(char) < 32 or ord(char) == 127 for char in value)
    )


def _filter(value: object) -> tuple[str, ...] | None:
    if value == "*":
        return None
    if not isinstance(value, list) or len(value) > MAX_FILTER_ITEMS:
        raise ValueError("invalid scope")
    if not all(_identifier(item) for item in value) or len(set(value)) != len(value):
        raise ValueError("invalid scope")
    return tuple(value)


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate field")
        result[key] = value
    return result


def load_grants(path: str | Path) -> dict[str, ArchiveScope]:
    """Read a bounded, regular, non-writable-by-others grant file.

    Mount this configuration read-only in the serving container. A malformed
    entry fails the complete file closed; no implicit wildcard or default grant
    exists. Empty source/project lists intentionally grant no matching rows.
    """
    try:
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(path, flags)
        with os.fdopen(descriptor, "rb") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_ACL_BYTES:
                raise ValueError("invalid grants file")
            if os.name == "posix":
                if info.st_mode & 0o022 or info.st_uid not in (0, os.geteuid()):
                    raise ValueError("unprotected grants file")
            raw = stream.read(MAX_ACL_BYTES + 1)
        if len(raw) > MAX_ACL_BYTES:
            raise ValueError("oversized grants file")
        value = json.loads(raw.decode("utf-8-sig"), object_pairs_hook=_unique_object)
        if not isinstance(value, dict) or set(value) != {"version", "clients"}:
            raise ValueError("invalid grants")
        if type(value["version"]) is not int or value["version"] != 1:
            raise ValueError("unsupported grants")
        clients = value["clients"]
        if not isinstance(clients, dict) or len(clients) > MAX_CLIENTS:
            raise ValueError("invalid clients")
        grants = {}
        for client_id, record in clients.items():
            if not _identifier(client_id) or not isinstance(record, dict):
                raise ValueError("invalid client")
            if set(record) != {"owner_id", "source_ids", "project_ids"}:
                raise ValueError("invalid grant")
            if not _identifier(record["owner_id"]):
                raise ValueError("invalid owner")
            grants[client_id] = ArchiveScope(
                client_id=client_id, owner_id=record["owner_id"],
                source_ids=_filter(record["source_ids"]),
                project_ids=_filter(record["project_ids"]),
            )
        return grants
    except (OSError, ValueError, TypeError, UnicodeError, RecursionError):
        raise ArchiveAccessError(
            "ARCHIVE_AUTHORIZATION_STATE_UNAVAILABLE: The protected archive "
            "client grants are missing, unreadable, unsafe or invalid."
        ) from None


def scope_for_client(path: str | Path, client_id: str) -> ArchiveScope:
    if not _identifier(client_id):
        raise ArchiveAccessError("ARCHIVE_AUTHENTICATION_REQUIRED")
    scope = load_grants(path).get(client_id)
    if scope is None:
        raise ArchiveAccessError(
            "ARCHIVE_ACCESS_DENIED: This authenticated client has no archive grant."
        )
    return scope


def current_scope(path: str | Path) -> ArchiveScope:
    """Resolve the authenticated SDK identity without accepting caller IDs."""
    from mcp.server.auth.middleware.auth_context import get_access_token

    token = get_access_token()
    if token is None or not _identifier(token.client_id):
        raise ArchiveAccessError("ARCHIVE_AUTHENTICATION_REQUIRED")
    if "system:admin" not in token.scopes:
        raise ArchiveAccessError("ARCHIVE_OAUTH_SCOPE_REQUIRED")
    return scope_for_client(path, token.client_id)
