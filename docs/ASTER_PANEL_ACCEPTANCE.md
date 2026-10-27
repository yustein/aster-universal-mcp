# Aster panel acceptance — 27 October 2026

The deployed panel is accepted for the verified **ChatGPT web global entrypoint**
and the read-only Coordinator views below. Thread metadata is registered and
tested, but the thread entrypoint was not independently opened. Android and
the regular ChatGPT conversation host remain unverified. Ancillary metric
publication has a separate status below.

## Implementation and deployed artifact

| Item | Recorded value |
| --- | --- |
| Implementation commit | `c4d703cdaf2f794413511299353e68993fd744bc` |
| Pull request | [PR #11](https://github.com/yustein/aster-universal-mcp/pull/11) |
| Package | `aster-universal-mcp`, version `0.2.0a2` |
| Wheel | `aster_universal_mcp-0.2.0a2-py3-none-any.whl` |
| Wheel size | 228,409 bytes |
| Wheel SHA-256 | `06ed9ec942d658f1873cf223cbbb2009aab2e3d3a2231dfef0553d5d4e142632` |
| Host/service | Existing Amsterdam deployment, `aster-coordinator.service` |
| Live host | [Aster panel in ChatGPT](https://chatgpt.com/mcp-app/asdk_app_6ae057c9d0808191a66201117ad488d1/aster_panel) |

The active `/opt/aster-coordinator/venv` symlink points to:

```text
/opt/aster-coordinator/releases/06ed9ec942d658f1873cf223cbbb2009aab2e3d3a2231dfef0553d5d4e142632/venv
```

Verify the recorded wheel independently of any later rebuild:

```sh
wc -c dist/aster_universal_mcp-0.2.0a2-py3-none-any.whl
sha256sum dist/aster_universal_mcp-0.2.0a2-py3-none-any.whl
```

## Test evidence and reproduction

| Suite | Result |
| --- | --- |
| Existing Store, authorization, tools, OAuth and native-event regressions | 203 passed |
| Panel data tests | 23 passed |
| Panel integration tests | 34 passed |
| Frontend browser checks | 14 passed |

The 260 Python tests include actual MCP SDK HTTP dispatch, OAuth verification,
origin enforcement, per-call allowlist/enrollment checks, task isolation,
read-only database behavior and ancillary source filtering. The integration
fixture substitutes only the HTML asset; frontend checks and live-host
acceptance cover the rendered interface separately.

From the repository root, using the prepared environment and locked frontend
dependencies:

```sh
.venv/bin/python -m pytest -q tests/test_coordination_store.py tests/test_coordination_tools.py tests/test_coordination_native.py tests/test_tool_access.py tests/test_full_oauth.py tests/test_panel_data.py tests/test_panel_integration.py
npm --prefix ui ci --ignore-scripts --no-audit --no-fund
npm --prefix ui run build
npm --prefix ui test
```

Live acceptance used no paid dispatch or test prompt. Opening and inspecting
the panel did not create a new background event binding.

## Live ChatGPT web acceptance

The actual connected ChatGPT web host successfully opened the panel, refreshed
its data, opened task detail, displayed saved evidence, displayed the canonical
Coordinator receipt and displayed the fleet view. The receipt preserves the
distinction between worker completion and sender acceptance.

Observed Coordinator state:

- Three tasks, all completed and accepted; zero queued and zero running.
- Three enrolled agent identities, of which two were receive-ready. Receive
  readiness is not a worker heartbeat or proof of an active connection.
- Three allowlisted clients and three OAuth clients.
- All three task documents remained unchanged across the deployment and checks.

Recorded task-document comparison hash:

```text
41855ba41a714b8a5f475b1fce979924f5384705e03bcb754a9afe56a9448ec4
```

To repeat host acceptance without dispatching work, open the live link using
the existing approved connection, refresh, inspect an existing completed task,
then inspect its evidence, receipt and the fleet view. Compare the current
counts and task documents with the current baseline; the figures above record
this acceptance run and are not permanent expected values. Do not create a
second identity or new event binding for this check.

| Host surface | Evidence |
| --- | --- |
| ChatGPT web global entrypoint | Independently opened and exercised |
| Thread entrypoint | Metadata registered and tested; not independently opened |
| Android | Not verified |
| Regular ChatGPT conversation host | Not verified |

## Ancillary metric observations

The backend archive snapshot was confirmed as of **2026-10-27T20:37:09Z**:

| Archive measurement | Value |
| --- | ---: |
| Conversations | 2,566 |
| Exact passages | 251,301 |
| Revisions | 2,567 |
| Completed imports | 3 |
| Running imports | 0 |
| Completed knowledge jobs | 2,495 |
| Pending knowledge jobs | 71 |

`complete_account_history_verified` is false. Embeddings are available but
incomplete. These observations do not establish complete account-history or
embedding coverage.

**Archive publication/live-panel acceptance: verified.** The operator published
the source to `/var/lib/aster-coordinator/panel-sources.json`, owned by UID 988
with mode `0600`, and restricted its audience to `aster-main-amsterdam`. An
actual ChatGPT Archive & costs refresh confirmed the archive fields and source
timestamp **23:37:09 UTC+3**. The panel refresh snapshot was **23:38:32 UTC+3** on
27 October 2026.

**Cost publication/live-panel acceptance: verified.** A read-only transaction
against `/opt/aster-pa/staging-app/pa.db` measured the shared PA API budget
ledger at **2026-10-27T20:42:33.510817+00:00**. The calculation follows the
existing `api-budget.mjs` service-liability rules, including outstanding
prior-day reservations. No application module was executed or imported, no
database was changed, and no provider call was made.

| Cost measurement | Value |
| --- | ---: |
| Accounting date/timezone | 2026-10-27, UTC |
| Daily service limit | USD 10.00 |
| Reported ledger spend | USD 0.030794 |
| Outstanding reservations | USD 0.00 |
| Remaining ledger budget | USD 9.969206 |
| Counted attempts | 21 |
| Unknown external usage records | 0 |
| Accounting holds | 0 |

These are PA ledger values, not a provider invoice or account-wide spending
total. They exclude ChatGPT subscription/Work credits and activity outside the
ledger. The protected ancillary file retains the same owner, mode and audience.
The actual ChatGPT host displayed every published cost field with source time
**23:42:33 UTC+3** and panel-refresh time **23:42:49 UTC+3**. Paid-dispatch
availability was not inferred from the budget figures or changed by this work.

Archive and cost values are explicitly operator-published snapshots. Refresh
rereads the protected file; no automatic telemetry collector was installed.
The Coordinator task and fleet data are freshly read on each refresh.

## Deployment recovery and rollback

The first deployment attempt rolled back automatically because starting the
installer from `/opt/aster-coordinator` caused a local `operator` module to
shadow Python's standard-library `operator`. Running the installer from `/`
resolved that failure, and the retry succeeded. Use `/` as the working
directory for that installer.

Protected backup for the successful retry:

```text
/var/backups/aster-coordinator/20261027-aster-panel-06ed9ec942d6-retry1
```

Prior venv target:

```text
/opt/aster-coordinator/releases/bc10d334e65910a13fb5397a6e056c07687daa291ab40b00af671e57bcf1f90a/venv
```

For an ordinary code rollback, use the existing protected operator workflow:

1. Verify the backup and prior release target, then stop
   `aster-coordinator.service` for the guarded cutover.
2. Restore `/opt/aster-coordinator/venv` to the recorded prior venv target and
   restart the same service.
3. Verify service health and an authenticated existing Coordinator read. Check
   that the preserved OAuth clients, allowlist, native bindings and task
   documents still match the intended state.

The panel changed no persistent task schema. Ordinary code rollback does not
require restoring a database. Preserve the OAuth database, allowlist, task
database, public origin and existing native bindings; a historical database
restore would be a separate operation and could discard newer state.

See [ASTER_PANEL.md](ASTER_PANEL.md) for the interface contract, ancillary
source schema and deployment requirements.
