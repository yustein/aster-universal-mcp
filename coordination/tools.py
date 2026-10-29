"""Optional coordination tools for existing owner-approved OAuth clients.

The authenticated client ID is supplied by the gateway, never by a tool
argument. Enrollment, route grants and native receiver verification are local
operator actions and deliberately have no MCP tool surface here.
"""
from __future__ import annotations

from pathlib import Path
from typing import Literal

from mcp.server.mcpserver.exceptions import ToolError
from pydantic import StrictBool, StrictInt

from tool_access import current_client_access
from .store import CoordinationError, Store


def register_coordination_tools(server, *, db_path: str, allowlist_path: str | Path,
                                event_notifier=None) -> Store:
    """Register the opt-in surface and return its store for transport adapters.

    The existing administrative trust model is unchanged: these clients are
    owner-approved administrators, not sandboxed or lower-privilege workers.
    """
    if not isinstance(db_path, str) or not db_path or not Path(db_path).is_absolute():
        raise ValueError("ASTER_COORDINATION_DB must be an absolute database path")
    store = Store(db_path, event_notifier=event_notifier)

    def invoke(method: str, **arguments) -> dict:
        # Read the current grant on every invocation, including read tools and
        # lease renewal. A previously approved token is not a cached grant.
        access = current_client_access(allowlist_path)
        access.require_admin()
        try:
            return getattr(store, method)(access.client_id, **arguments)
        except CoordinationError as error:
            # Only the store's deliberate, sanitized public errors are exposed.
            # Database/OS failures keep the SDK's generic unexpected-error path.
            raise ToolError(str(error)) from None

    @server.tool()
    def aster_profile() -> dict:
        """Read your operator-enrolled agent profile and receiver readiness; this does not enroll or wake an agent."""
        return invoke("profile")

    @server.tool()
    def aster_list_agents() -> dict:
        """List your own profile and the agents the local operator permits you to delegate to."""
        return invoke("list_agents")

    @server.tool()
    def aster_delegate_task(
        to_agent: str,
        request_id: str,
        instruction: str,
        expected_result: str,
        inputs: dict | None = None,
        authorized_scope: dict | None = None,
        ttl_seconds: StrictInt = 86400,
        parent_task_id: str | None = None,
    ) -> dict:
        """Create durable assigned work. Retry the same request ID and unchanged payload to recover its existing task. Scope records do not grant host privileges; queued/delivered is not completed."""
        return invoke("delegate_task", to_agent=to_agent, request_id=request_id,
                      instruction=instruction, expected_result=expected_result,
                      inputs=inputs, authorized_scope=authorized_scope,
                      ttl_seconds=ttl_seconds, parent_task_id=parent_task_id)

    @server.tool()
    def aster_inbox(limit: StrictInt = 20) -> dict:
        """Read pending tasks assigned to your enrolled agent. Reading does not claim ownership or establish native wake."""
        return invoke("inbox", limit=limit)

    @server.tool()
    def aster_outcomes(cursor: StrictInt = 0, limit: StrictInt = 20) -> dict:
        """Read your persisted sender outcomes, including accepted results. Resume with next_cursor; pages are bounded by count and bytes. Later verification may repeat a task with the same outcome_id. This read neither advances expiry nor verifies, acknowledges, or publishes a result; native readiness is not required."""
        return invoke("outcomes", cursor=cursor, limit=limit)

    @server.tool()
    def aster_claim_task(
        request_id: str, task_id: str | None = None, lease_seconds: StrictInt = 300,
    ) -> dict:
        """Exclusively claim assigned work with a bounded lease. Reuse the claim request ID after a lost response; preserve the returned lease token and fence for subsequent updates."""
        return invoke("claim_task", request_id=request_id, task_id=task_id,
                      lease_seconds=lease_seconds)

    @server.tool()
    def aster_renew_lease(
        task_id: str, lease_token: str, fence: StrictInt, lease_seconds: StrictInt = 300,
    ) -> dict:
        """Renew your current unexpired fenced claim. Expired or replaced ownership cannot be revived by renewal."""
        return invoke("renew_lease", task_id=task_id, lease_token=lease_token,
                      fence=fence, lease_seconds=lease_seconds)

    @server.tool()
    def aster_checkpoint_task(
        task_id: str, lease_token: str, fence: StrictInt,
        expected_revision: StrictInt, checkpoint: dict,
    ) -> dict:
        """Persist progress under your live claim and expected task revision. A checkpoint is not completion or independent evidence of an external effect."""
        return invoke("checkpoint_task", task_id=task_id, lease_token=lease_token,
                      fence=fence, expected_revision=expected_revision,
                      checkpoint=checkpoint)

    @server.tool()
    def aster_complete_task(
        task_id: str, lease_token: str, fence: StrictInt, result: dict,
        outcome: Literal["completed", "failed", "uncertain", "blocked"] = "completed",
        evidence: dict | None = None,
    ) -> dict:
        """Record your task outcome under its current claim. Use uncertain when effects may have occurred but are unconfirmed. Evidence is worker-reported, not independent verification."""
        return invoke("complete_task", task_id=task_id, lease_token=lease_token,
                      fence=fence, result=result, outcome=outcome, evidence=evidence)

    @server.tool()
    def aster_task_status(task_id: str) -> dict:
        """Read a task you sent or received, including its persisted outcome and uncertainty. Does not execute or retry work."""
        return invoke("task_status", task_id=task_id)

    @server.tool()
    def aster_cancel_task(task_id: str, reason: str) -> dict:
        """Cancel a task you sent. Running work becomes uncertain and its lease is fenced; this does not prove an external process or effect stopped."""
        return invoke("cancel_task", task_id=task_id, reason=reason)

    @server.tool()
    def aster_reconcile_task(
        task_id: str, decision: Literal["retry", "complete", "fail", "cancel"],
        evidence: dict, expected_revision: StrictInt,
    ) -> dict:
        """Reconcile blocked/uncertain work you sent using evidence and the expected revision. Retry requires effects_absent=true after checking for prior effects; never guess after a timeout."""
        return invoke("reconcile_task", task_id=task_id, decision=decision,
                      evidence=evidence, expected_revision=expected_revision)

    @server.tool()
    def aster_verify_result(
        task_id: str, expected_revision: StrictInt, accepted: StrictBool, evidence: dict,
    ) -> dict:
        """As the sender, review a completed result against the expected revision. Evidence must include nonempty checks and a reference. Records sender acceptance/rejection separately from worker completion; does not assert independent provider verification."""
        return invoke("verify_result", task_id=task_id, expected_revision=expected_revision,
                      accepted=accepted, evidence=evidence)

    return store
