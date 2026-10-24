"""Per-call administrative access and safe onboarding results for MCP tools.

OAuth authenticates the current client; a separate protected allowlist grants
administrative tools. This module only reads that state and never grants access.
"""
from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path

from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.mcpserver.exceptions import ToolError


MAX_ALLOWLIST_BYTES = 1024 * 1024
AUTHORIZATION_STATE_ERROR = (
    "AUTHORIZATION_STATE_UNAVAILABLE: The server's protected administrative "
    "allowlist is missing, unreadable or invalid. The local server owner must "
    "inspect and repair that configuration. Reconnecting or repeating pairing "
    "does not repair it."
)


def read_admin_allowlist(path: str | Path) -> list[str]:
    """Read a bounded list of exact client IDs, or fail closed without its contents."""
    try:
        with Path(path).open("rb") as stream:
            raw = stream.read(MAX_ALLOWLIST_BYTES + 1)
        if len(raw) > MAX_ALLOWLIST_BYTES:
            raise ToolError(AUTHORIZATION_STATE_ERROR)
        allowed = json.loads(raw.decode("utf-8-sig"))
    except (OSError, UnicodeError, ValueError, RecursionError):
        raise ToolError(AUTHORIZATION_STATE_ERROR) from None
    if not isinstance(allowed, list) or not all(
        isinstance(item, str) and item and "\x00" not in item for item in allowed
    ):
        raise ToolError(AUTHORIZATION_STATE_ERROR)
    return allowed


@dataclass(frozen=True)
class ClientAccess:
    """Only the current client's non-secret authorization state."""

    client_id: str
    admin_approved: bool

    def approval_required(self) -> ToolError:
        # JSON quoting preserves the exact ID while escaping control characters;
        # no caller-provided value is interpolated into a shell command.
        identity = json.dumps(self.client_id, ensure_ascii=True)
        return ToolError(
            "ADMIN_APPROVAL_REQUIRED: administrator client not authorized. "
            "OAuth authentication succeeded, but the local server owner must "
            f"approve this exact client ID: {identity}. Call connection_status "
            "for the local approval procedure. Do not repeat pairing to fix "
            "this missing administrative grant."
        )

    def require_admin(self) -> None:
        if not self.admin_approved:
            raise self.approval_required()


def current_client_access(allowlist_path: str | Path) -> ClientAccess:
    """Recheck authentication, scope and the exact-client grant on every call."""
    token = get_access_token()
    if token is None or not isinstance(token.client_id, str) or not token.client_id:
        raise ToolError(
            "AUTHENTICATION_REQUIRED: Connect using this server's OAuth flow "
            "before calling its tools."
        )
    if "system:admin" not in token.scopes:
        raise ToolError(
            "OAUTH_SCOPE_REQUIRED: This OAuth credential lacks the required "
            "system:admin scope. Ask the local server owner to review the "
            "connection; an administrative allowlist grant cannot add OAuth scope."
        )
    allowed = read_admin_allowlist(allowlist_path)
    return ClientAccess(token.client_id, token.client_id in allowed)


def connection_status(
    access: ClientAccess, *, platform: str, default_workspace: str, listing_base: str
) -> dict:
    """Describe this client and the platform's actual path/permission policy."""
    if platform not in ("linux", "windows"):
        raise ValueError("unsupported gateway platform")
    approved = access.admin_approved
    listing_allowed = platform == "linux" or approved
    if platform == "linux":
        listing_scope = "host_absolute_paths" if approved else "configured_root"
        operator_step = (
            "Ask the local server owner to run the installed protected operator's "
            "clients command, verify the client_id shown here, then approve that "
            "exact ID with its approve command."
        )
    else:
        listing_scope = "host_accessible" if approved else "approval_required"
        operator_step = (
            "Ask the local server owner to use scripts/manage-client-windows.ps1 "
            "with -Action clients, verify the client_id shown here, then use "
            "-Action approve -ClientId with that exact ID."
        )
    next_step = (
        "Administrative permission is enabled for this client. Before treating "
        "setup as complete, run harmless native hostname and identity commands "
        "and check status completed, exit_code 0 and the expected output. This "
        "status does not verify executor health. Each call rechecks the grant. "
        "Commands and jobs use the default workspace when cwd is omitted; "
        "changing directory in one call does not change later calls."
        if approved else
        operator_step + " Then call connection_status again. OAuth pairing and "
        "administrative approval are separate; no new pairing is required."
    )
    return {
        "status": "approved" if approved else "approval_required",
        "authenticated": True,
        "client_id": access.client_id,
        "platform": platform,
        "admin_approved": approved,
        "permissions": {
            "commands": approved,
            "jobs": approved,
            "file_transfers": approved,
            "directory_listing": listing_allowed,
            "directory_listing_outside_root": approved,
        },
        "default_workspace": default_workspace,
        "listing_base": listing_base,
        "directory_listing": {
            "scope": listing_scope,
            "relative_paths_base": listing_base,
            "configured_root": listing_base if platform == "linux" else None,
            "absolute_paths_outside_root_allowed": approved,
            "relative_paths_confined_to_root": platform == "linux",
        },
        "next_step": next_step,
    }
