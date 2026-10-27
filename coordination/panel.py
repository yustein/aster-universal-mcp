"""Authenticated, read-only MCP App for the existing Coordinator.

The view uses the caller's existing enrollment and task visibility. Opening or
refreshing a panel never claims work, changes a lease, records acceptance, or
subscribes a conversation to events. All application data crosses the host's
authenticated MCP bridge; the HTML contains no server credentials.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import stat
from datetime import date, datetime
from pathlib import Path
from typing import Callable

from mcp.server.apps import APP_MIME_TYPE, Apps
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.mcpserver.resources import TextResource
from mcp.types import CallToolResult, TextContent, ToolAnnotations
from pydantic import StrictInt

from tool_access import current_client_access
from . import panel_data
from .store import CoordinationError, Store


READ_ONLY = ToolAnnotations(readOnlyHint=True, destructiveHint=False,
                            idempotentHint=True, openWorldHint=False)
MAX_SOURCE_BYTES = 65536
SOURCE_STATUSES = frozenset({"available", "partial", "snapshot", "stale", "unavailable", "not_reported"})


def _source_projection(name: str, value: dict) -> dict | None:
    """Only the documented telemetry fields cross the authenticated UI bridge."""
    if value.get("status") not in SOURCE_STATUSES:
        return None
    result = {"status": value["status"]}
    for field, maximum in (("label", 120), ("source", 120), ("message", 500)):
        text = value.get(field)
        if isinstance(text, str) and "\x00" not in text:
            result[field] = text[:maximum]
    for field in ("checked_at", "as_of"):
        text = value.get(field)
        if isinstance(text, str) and len(text) <= 40:
            try:
                parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
                if parsed.tzinfo is not None:
                    result[field] = parsed.isoformat()
            except ValueError:
                pass
    if name == "archive":
        for field in ("conversations", "exact_passages", "revisions", "completed_imports",
                      "running_imports", "pending_knowledge_jobs", "completed_knowledge_jobs",
                      "verified_decisions"):
            number = value.get(field)
            if type(number) is int and 0 <= number <= 10**12:
                result[field] = number
        for field in ("complete_account_history_verified", "embedding_index_available"):
            if type(value.get(field)) is bool:
                result[field] = value[field]
    elif name == "costs":
        if value.get("currency") == "USD":
            result["currency"] = "USD"
        if value.get("timezone") in ("UTC", "Europe/Istanbul"):
            result["timezone"] = value["timezone"]
        text = value.get("date")
        if isinstance(text, str) and len(text) == 10:
            try:
                result["date"] = date.fromisoformat(text).isoformat()
            except ValueError:
                pass
        for field in ("daily_budget_usd", "reported_spend_usd", "reserved_spend_usd", "remaining_budget_usd"):
            number = value.get(field)
            if type(number) in (int, float) and 0 <= number <= 10**9 and math.isfinite(number):
                result[field] = number
        if type(value.get("paid_dispatch_enabled")) is bool:
            result["paid_dispatch_enabled"] = value["paid_dispatch_enabled"]
    return result


def _additional_sources(data: dict, store: Store) -> dict:
    """Merge optional operator-published metrics, never another app's database.

    This protected, bounded file is not an authorization database. It can only
    narrow the audience of ancillary metrics to already enrolled identities.
    The Coordinator's tasks, viewer, and permissions cannot be overridden.
    """
    path = store.path.with_name("panel-sources.json")
    try:
        # O_NOFOLLOW closes the symlink check/open race. The parent is already
        # the Coordinator's private state directory; telemetry is owner-only.
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as stream:
            info = os.fstat(stream.fileno())
            if (not stat.S_ISREG(info.st_mode) or info.st_size > MAX_SOURCE_BYTES
                    or info.st_uid not in (0, store.path.stat().st_uid)
                    or stat.S_IMODE(info.st_mode) & 0o077):
                raise ValueError
            raw = stream.read(MAX_SOURCE_BYTES + 1)
        if len(raw) > MAX_SOURCE_BYTES:
            raise ValueError
        published = json.loads(raw)
        if not isinstance(published, dict):
            raise ValueError
        audience = published.get("allowed_agent_ids")
        if (not isinstance(audience, list) or len(audience) > 64
                or not all(isinstance(x, str) and 0 < len(x) <= 128 for x in audience)):
            raise ValueError
        if data.get("viewer", {}).get("agent_id") not in audience:
            return data
        sources = published.get("sources", {})
        if not isinstance(sources, dict):
            raise ValueError
        for name in ("archive", "costs"):
            source = sources.get(name)
            if isinstance(source, dict):
                projected = _source_projection(name, source)
                if projected is not None:
                    data.setdefault("sources", {})[name] = projected
    except FileNotFoundError:
        return data
    except (OSError, ValueError, TypeError, RecursionError):
        # Auxiliary telemetry failure must not hide authoritative task state.
        data.setdefault("source_notices", []).append(
            "Additional operational metrics are temporarily unavailable.")
    return data


def build_panel_app(get_store: Callable[[], Store], *, allowlist_path: str | Path):
    """Return an Apps extension and a registrar for its data-only helper tools."""
    html_path = Path(__file__).with_name("ui") / "aster-panel.html"
    html = html_path.read_text(encoding="utf-8")
    digest = hashlib.sha256(html.encode()).hexdigest()[:16]
    resource_uri = f"ui://aster/panel-{digest}.html"
    apps = Apps()
    apps.add_resource(TextResource(
        uri=resource_uri, name="aster-panel", title="Aster", mime_type=APP_MIME_TYPE,
        text=html, meta={
            "ui": {"csp": {"connectDomains": [], "resourceDomains": []}},
            "openai/ui": {"availableDisplayModes": ["inline", "fullscreen"],
                          "preferredDisplayMode": "inline"},
        },
    ))

    def invoke(method: str, **arguments) -> CallToolResult:
        access = current_client_access(allowlist_path)
        access.require_admin()
        store = get_store()
        try:
            if method == "snapshot":
                data = _additional_sources(
                    panel_data.snapshot(store, access.client_id, **arguments), store)
                counts = data.get("counts", {})
                summary = (f"Aster: {counts.get('total', 0)} visible tasks; "
                           f"{counts.get('running', 0)} running, "
                           f"{counts.get('queued', 0)} queued. "
                           "Open the panel to inspect source timestamps and receipts.")
            else:
                data = panel_data.task_detail(store, access.client_id, **arguments)
                task = data.get("task", {})
                summary = (f"Aster task {task.get('task_id', '')}: "
                           f"{task.get('status', 'unknown')}. "
                           "Worker completion and sender acceptance are separate records.")
        except CoordinationError as error:
            raise ToolError(str(error)) from None
        return CallToolResult(content=[TextContent(type="text", text=summary)],
                              structuredContent=data)

    @apps.tool(resource_uri=resource_uri, visibility=["model", "app"],
               name="aster_panel", title="Aster",
               description="Open Aster's read-only task queue, worker status, evidence and completion receipts inside ChatGPT. Uses your existing access; does not run or accept work.",
               annotations=READ_ONLY,
               meta={"openai/ui": {"entrypoints": [{"type": "global"}, {"type": "thread"}]}})
    def aster_panel() -> CallToolResult:
        return invoke("snapshot")

    def register_data_tools(server):
        @server.tool(name="aster_panel_refresh", title="Refresh Aster panel",
                     description="Refresh the current caller's bounded read-only Aster panel data. No task or event changes.",
                     annotations=READ_ONLY, meta={"ui": {"visibility": ["app"]}})
        def aster_panel_refresh(limit: StrictInt = 40) -> CallToolResult:
            return invoke("snapshot", limit=limit)

        @server.tool(name="aster_panel_task", title="Inspect Aster task",
                     description="Read one Aster task visible to your enrolled identity, including saved evidence and acceptance receipts. Does not claim, verify or retry it.",
                     annotations=READ_ONLY, meta={"ui": {"visibility": ["app"]}})
        def aster_panel_task(task_id: str) -> CallToolResult:
            return invoke("task_detail", task_id=task_id)

    return apps, register_data_tools
