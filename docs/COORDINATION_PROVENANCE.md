# Coordination design and reuse provenance

This development feature joins existing Aster work into a provider-neutral
contract, without importing deployment state or private business data.

| Existing owned component | Reused in the new implementation |
| --- | --- |
| Native MCP Events bridge, reuse package v1.2, 15 October 2026 | Event discovery/subscription semantics, fresh signed callback challenge, public-address pinning, Standard Webhooks signatures, durable outbox, stable event IDs and bounded retry |
| PA intersession work contract | Stable assignment identity, explicit authority records, exclusive ownership, generation fencing, progress checkpoints, uncertain-effect reconciliation and distinction between transport/work/outcome |
| Reviewed session-binding development | Matching a route alias is insufficient; actual destination evidence must have a trusted source. Binding replacement invalidates the previous generation. Reserve intent before execution and reconcile ambiguous delivery |
| Current public Universal MCP | Existing OAuth registration/pairing, token/resource validation, exact-client administrative allowlist and Windows/Linux command, job and transfer tools remain the surrounding access and execution layers |

The owner-provided v1.2 reference archive was recovered and inspected statically.
Its SHA-256 is `bc2e908c89b0beafc3d8406add856d164767a379dc34ca8838fcaec8e6fec3be`.
Its deployment configuration, service names, historical conversation IDs, user
records, OAuth database, credentials and PA business database are not bundled.
Native transport behavior is adapted to the current Python repository and pinned
MCP SDK 2.3.0 extension seam. A fixed HMAC test vector was cross-checked against the
reference bridge's Standard Webhooks library; no JavaScript dependency is vendored.

The core's new transactional claims and worker journal implement the combined
contract in a separate store. They do not copy or migrate a production PA database,
assert that legacy lease code is deployed here, or require the AgentSocket service.

## Protocol sources checked during implementation

- [OpenAI MCP Events](https://developers.openai.com/plugins/build/mcp-events): native webhook integration, null-cursor compatibility, deterministic principal/callback/event/arguments identity, signed callback challenge, and replacement-key rotation with a bounded dual-signature window.
- [OpenAI custom MCP servers](https://developers.openai.com/api/docs/guides/custom-mcp-server): host connection and authentication workflow.
- [Claude Code headless](https://code.claude.com/docs/en/headless) and [CLI reference](https://code.claude.com/docs/en/cli-reference): configured supervised executor and terminal output.
- [Claude Code Channels](https://code.claude.com/docs/en/channels): separate preview transport into a running opted-in Code session; not an implementation in this change.
- [Claude remote connectors](https://support.claude.com/en/articles/11175166-get-started-with-custom-connectors-using-remote-mcp): tool integration; no assumed arbitrary-chat background wake.

Routine signing-key rotation verifies a fresh challenge using only the new key,
then preserves the live subscription and operator-verified destination. Event
deliveries include the old and new signatures for at most 300 seconds. A changed
callback or expected session requires a new destination verification. Expired or
revoked receiver enrollment starts a new delivery epoch: notification IDs change
for task-level catchup while work IDs and historical receipts remain intact.
Prior running claims are fenced; uncertain work requires reconciliation before a
fresh claim. This is separate from protocol replay, which is not advertised; the event
returns `cursor: null` and rejects non-null replay cursors.

The new Aster implementation uses the repository's Apache-2.0 license.
Third-party dependencies retain their own licenses.
Provider branding and external protocol documentation do not imply endorsement
or successful live provider qualification.
