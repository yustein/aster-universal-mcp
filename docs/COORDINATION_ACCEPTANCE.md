# Agent coordination development acceptance

Date: **25 October 2026**. Source version: **0.2.0a1**.

This record covers the development source on
`feature/agent-coordination-20261025`, based on public main commit
`9bb2de2d5305c0448757e85b63139dbf5303a6dc`. It is a local implementation and
regression record, not production or live-provider qualification. The released
Windows/Linux b1, b2 and b3 tags, packages and acceptance records are unchanged.

## Implemented scope

- A separate durable SQLite task store, authenticated-client agent enrollment,
  explicit directional delegation grants and thirteen `aster_*` MCP tools.
- Atomic exclusive claims, renewable leases, monotonically increasing fencing
  numbers, optimistic checkpoints, immutable completion retries and explicit
  reconciliation of uncertain effects.
- Separate worker-reported completion and sender acceptance supported by recorded
  checks and an evidence reference.
- A standalone OAuth coordination companion with no machine executor, plus opt-in
  registration beside the existing Linux and Windows gateway tools.
- Native MCP Events discovery, subscription, signed challenge, durable callback
  outbox, bounded retry, current authorization checks and an operator-observed
  destination binding. Delivery does not establish task execution.
- An explicitly configured command worker with durable recovery journal,
  independent lease renewal, bounded execution/output, strict terminal receipts,
  and parsers for generic JSON, Claude Code JSON and Codex JSONL.

The [operator guide](AGENT_COORDINATION.md) describes setup and recovery. The
[provenance record](COORDINATION_PROVENANCE.md) identifies the existing owned
bridge, PA contract and session-binding work used in this adaptation.

## Baseline local regression result

The following full-suite and packaging results describe the baseline at
`d94a337`, before the focused recovery and sender-outcome follow-ups. Later
focused runs are reported separately; they are not added to this full-suite total.

Command, run from the repository with a dedicated Python environment:

```sh
python -m pytest -q -ra --junitxml=/tmp/aster-coordination-final-tests.xml
```

Observed result:

```text
297 passed, 23 skipped, 1 warning, 7 subtests passed in 28.10s
```

| Test area | Passed tests | Concrete coverage |
| --- | ---: | --- |
| Coordination store | 39 | Separate-client routing, concurrent claims, canonical idempotency, stale ownership, lease/deadline races, checkpoints, immutable completion, sender verification, transactional rollback, restart, catchup pagination, unrelated-state and future-schema refusal |
| Native receiving | 42 | Actual SDK/OAuth protocol requests, signed challenge/vector, `cursor: null`, public-address pinning/TLS, bounded HTTP, rotation, destination replacement, delivery epochs, retry/restart, offline catchup, dispatch fairness and live authorization checks |
| MCP tools and gateway integration | 44 | Current authenticated principal, allowlist/scope/resource checks, denial of impersonation, strict argument types, optional configuration and both gateway compositions |
| Operator and companion | 6 | Existing approved registration, revoked/nonapproved enrollment denial, separate state, local grants/binding/disable, companion surface without machine execution |
| Supervised worker | 28 | Real no-model subprocesses, SDK HTTP roundtrip, lost claim/completion responses, crash recovery, canonical result/evidence matching, timeout/output limits, token exclusion, journal lock/size/restart and launch-time lease expiry |
| Existing repository tests | 138 | Existing platform-independent gateway, OAuth, approval, execution-contract and lifecycle regression coverage |
| **Total** | **297** | The seven subtests are reported separately; do not add repeated targeted runs as additional coverage. |

The 23 skips require native Windows/ACLs, an explicitly selected disposable root
systemd VM, or a protected operator-test base directory. They were not enabled on
this development host. These skips are not passing platform acceptance results.

The visible warning was Pydantic's `TypedDictExtraConfigWarning` for the MCP SDK's
`RequestParamsMeta` extra-item configuration. It was not suppressed.

Environment: Linux development runner, Python **3.12.14**, MCP/MCP types
**2.3.0**, pytest **9.1.1**. The
[observed dependency versions](COORDINATION_TEST_ENVIRONMENT.txt) record this test
environment; they do not replace the frozen release dependency locks. Tests used
temporary databases and controlled subprocesses. The SDK worker roundtrip uses
an in-memory bearer-checking transport; actual Aster OAuth/resource handling is
covered separately by the tool and native suites. Provider labels in fixtures do
not constitute live ChatGPT or Anthropic account calls.

## Sender-outcome follow-up

The follow-up adds `aster_outcomes(cursor=0, limit=20)`: an authenticated,
sender-scoped, read-only inbox of current persisted outcomes, including already
accepted results. It provides count- and byte-bounded pagination and a versioned
semantic outcome ID that remains stable across sender verification and native
redelivery. It performs no expiry maintenance or publication acknowledgement.

New native notices also include the original event revision and recipient role.
These additive fields remain optional so retained legacy webhook bodies keep
their original signed bytes. The operator guide now requires an explicit visible
host reply for a completed task and separates that observation from transport,
execution and sender acceptance.

Focused validation on the follow-up source:

| Test selection | Observed result |
| --- | --- |
| Coordination store and MCP tools | 122 passed in 7.97s |
| Native receiving | 44 passed in 5.05s |
| Operator and companion composition | 6 passed in 1.06s |

The new coverage checks exact sender isolation and current authorization, paused
native senders, retained accepted results, all six outcome states, restart and
page recovery, canonical JSON identities, maximal legal outcome records, no
cursor skips under the response budget, and unchanged legacy retry bytes.
Independent source/test review and `git diff --check` passed. These are focused
local checks, not a repeated full-suite or new release packaging qualification.
Live task execution and visible host publication require separate observations.
The operator tests ran in the approved offline execution environment after the
restricted runner stalled on an AnyIO thread-return wakeup. A minimal
non-Aster reproducer isolated the same restriction; no product or test code was
changed to bypass the meaningful synchronous-tool check.

## Baseline packaging and review

The declared build-isolation workflow successfully built
`aster_universal_mcp-0.2.0a1-py3-none-any.whl`. It was installed without dependency
resolution into a fresh target directory. From outside the checkout, all six
coordination modules imported from that installed directory, and all seven
packaged coordination Python files matched the tested source bytes.

- Local wheel size: **92,987 bytes**.
- Local wheel SHA-256:
  `e2a15a5ba96d7b9be8ee7b3c67f389d3f67706a43d9d99d7f0673e21c4558aaa`.

This is a development packaging smoke check, not a published or qualified release
artifact. No release archive was rebuilt or replaced. A preliminary build without
build isolation failed because the test environment did not contain setuptools;
the successful build used the project's declared build dependency in isolation.

`git diff --check` passed. Focused reviews covered authorization, state isolation,
native receiving, worker recovery and documentation accuracy. The reference
archive's private deployment configuration, credentials and historical user/session
records are not included in this change. These checks are not an independent
security audit.

## Operational boundaries and remaining gates

1. **Real provider receiving is unqualified.** Validate a harmless task from the
   intended ChatGPT client to a separately enrolled Claude receiver and inspect
   the returned result. Ordinary Claude chat uses tools during a conversation;
   unattended Claude execution requires a configured runtime such as the command
   worker. Claude Code Channels are a different preview adapter and are not
   implemented here.
2. **Native identity needs actual destination evidence.** Complete a fresh host
   callback challenge, observe the probe in the intended session, record that
   observation locally, then test delivery, claim, completion and sender readback.
   The operator's observation is not cryptographic per-conversation attestation.
   A shared OAuth client remains one logical agent.
3. **Worker credential refresh is not implemented.** Aster access tokens currently
   expire after 900 seconds. The worker reads its token once and cannot sustain
   unattended operation beyond token expiry without a separately implemented and
   tested refresh mechanism. Preserve the journal through any controlled restart;
   an expired renewal may leave work uncertain. `--watch` does not solve refresh.
4. **Machine execution retains its own boundaries.** The optional gateway tools
   reuse existing execution/job APIs and permissions. This change has no new
   automatic remote-job adapter. Qualify a bounded machine job by checking its
   actual terminal state, exit code and requested effect. Native Windows worker
   process handling and full descendant containment need separate acceptance.
5. **This is a trusted-owner system.** Routing, task-scope records and fencing do
   not isolate an approved administrator or undo external side effects. Lease
   expiry and interrupted execution become uncertain; retry requires explicit
   evidence-based reconciliation. Sender verification records the sender's checks
   and does not independently prove arbitrary real-world effects.
6. **State protection and longevity remain operational responsibilities.** SQLite,
   WAL/SHM, active journals and callback keys contain sensitive plaintext data.
   Protect directories/backups and Windows ACLs. Journal records are bounded at
   16 MiB; retained tasks, events, receipts and deduplication history have no global
   storage quota or automatic deletion. Database namespace/version checks do not
   fully validate schema integrity or provenance. Sustained load, expiry/refresh,
   reboot/autostart, retention and backup recovery require their own qualification.

No production Aster/PA service, listener, tunnel, OAuth account, agent subscription
or machine job was changed by this implementation and local verification work.
