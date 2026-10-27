# Aster inside ChatGPT

The Coordinator exposes a read-only MCP App for inspecting the work already
visible to the authenticated, enrolled agent. It uses the existing service,
OAuth resource, owner approval, client allowlist, enrollment and task store.

## Views and contract

- **Overview:** task counts, bounded assignment list, search and status filters.
- **Task detail:** instructions, saved result, evidence, verification and receipt.
  Execution completion and sender acceptance remain separate facts.
  For a sender viewing a persisted outcome, the receipt uses the same canonical
  outcome ID and latest sender event sequence as the Coordinator outcome feed.
  It is a Coordinator record, not proof of an external side effect.
- **Fleet:** enrolled agent information and receive readiness. Readiness is not
  evidence of a live connection, a heartbeat or a currently running worker.
- **Archive and costs:** source status, source timestamps and measured values.
  Missing measurements are displayed as not reported, never as zero.

`aster_panel` opens the component and accepts an empty argument object. It
returns a `CallToolResult` with a concise text summary and the bounded
`aster.panel.v1` structured payload. `aster_panel_refresh` and
`aster_panel_task` are app-only helper tools. They have no UI resource metadata,
so refresh and detail calls do not open additional components.

The opening tool declares `global` and `thread` OpenAI entrypoints. The HTML
resource is `text/html;profile=mcp-app` at a content-versioned `ui://` URI. All
JavaScript, styles and dependency notices are bundled. The component declares
no external connect or resource domains and sends no direct API requests.
Requests use the host's authenticated MCP Apps bridge.

## Access and effects

Every data call checks the current OAuth client's owner approval, admin scope,
allowlist membership and active Coordinator enrollment. A task is visible only
under the existing sender/receiver rule. The view cannot claim work, change a
lease, accept a result, retry a job, subscribe a conversation to events or alter
provider settings. Reads do not expire leases as a side effect.

The optional **Ask in chat** action is available only when the host advertises
that capability. It sends a prompt only after the user clicks. It is not an
automatic dispatch or acceptance action.

Visible timestamps use Europe/Istanbul (UTC+3). Source values remain ISO
timestamps, and a cost ledger's own accounting date/timezone remains explicit.

## Optional ancillary metrics

The Coordinator does not open another application's database. An operator may
publish a bounded `panel-sources.json` alongside the task database. The file
must be a regular, owner-only file, owned by root or the task database owner.
Symlinks, group/world access, files larger than 64 KiB and invalid JSON are
rejected. Failure leaves the authoritative task panel usable.

The document contains `allowed_agent_ids` and `sources`. Audience membership
only narrows access for already authenticated, enrolled callers. It never
changes task visibility. Only `archive` and `costs` are accepted, with an
explicit field projection; unknown fields and nested documents are discarded.

Shared fields: `status` (available, partial, snapshot, stale, unavailable or
not_reported), bounded `label`, `source`, `message`, and timezone-aware
`checked_at`/`as_of` timestamps.

Archive fields: nonnegative integer `conversations`, `exact_passages`,
`revisions`, `completed_imports`, `running_imports`, `pending_knowledge_jobs`,
`completed_knowledge_jobs`, `verified_decisions`; boolean
`complete_account_history_verified` and `embedding_index_available`.

Cost fields: `currency: USD`, an ISO `date`, `timezone` (UTC or Europe/Istanbul),
finite nonnegative `daily_budget_usd`, `reported_spend_usd`,
`reserved_spend_usd`, `remaining_budget_usd`, and boolean
`paid_dispatch_enabled`. A published source is a timestamped measurement, not
an account-wide billing guarantee. Refresh reads the published document; it
does not rerun an external archive import or provider billing query.

## Build and validation

```sh
cd ui
npm ci --ignore-scripts --no-audit --no-fund
npm run build
npm test
cd ..
python -m pytest -q tests/test_panel_data.py tests/test_panel_integration.py
python -m build --wheel --no-isolation
```

The generated asset is `coordination/ui/aster-panel.html` and is included in
the wheel by setuptools package data. The frontend source, exact dependency
lock, dependency notices, fixture host and browser checks are under `ui/`.
The fixture is a test host; its sample assignments are not production data.

HTTP integration tests exercise the production application composition with
isolated SQLite state, real OAuth validation, SDK tool/resource dispatch,
origin enforcement and authorization failures. No paid provider or production
task is needed for testing.

## Host support and deployment

MCP App embedding and OpenAI sidebar/thread extension surfaces are host
capabilities. A server advertisement does not establish that every ChatGPT
plan, browser, mobile client or current conversation supports that surface.
Test the connected app in the intended host. Do not switch the user's plan or
create a second Coordinator identity merely to display this view.

Deploy through the existing Coordinator wheel, lock, backup and operator
workflow. Preserve the OAuth database, allowlist, task database, public origin
and native bindings. Stop the service only for the guarded backup and release
cutover; verify the backup before starting the new release. If startup or
health verification fails, restore the previous venv symlink and restart the
same service. This feature changes no persistent Coordinator schema.

Run the existing installer from `/`, not `/opt/aster-coordinator`: the latter
contains `operator.py`, which can shadow Python's standard-library module
during virtual-environment creation. See [ASTER_PANEL_ACCEPTANCE.md](ASTER_PANEL_ACCEPTANCE.md)
for the verified deployment, artifact digest, host checks and rollback target.

Opening the panel does not enable background event delivery. Existing native
worker events retain their separate binding and delivery requirements.

## References

- [OpenAI plugin extensions](https://developers.openai.com/plugins/build/extensions)
- [OpenAI MCP extensions specification](https://github.com/openai/mcp-extensions/blob/main/docs/spec.md)
- [Python MCP Apps](https://py.sdk.modelcontextprotocol.io/advanced/apps/)
- [MCP Apps frontend API](https://apps.extensions.modelcontextprotocol.io/api/classes/app.App.html)
