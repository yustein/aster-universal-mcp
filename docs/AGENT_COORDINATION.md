# Agent coordination and supervised workers

The optional coordination service assigns durable tasks to operator-enrolled
agents. A task has an owner, a bounded lease, a fencing number, a persisted result
and a separate sender acceptance record. ChatGPT, Claude and machine processes
can use the same task lifecycle through different receivers.

This feature is source-level functionality, not an upgrade of an already
installed release. Installing it does not connect accounts, authorize a client,
subscribe a chat, start a worker or establish a successful live provider test.

## Trust and identity

The existing Aster authentication model remains **owner-approved administrative
OAuth clients**. Agent enrollment and route grants restrict the coordination
API's routing; they do not turn an administrator into a lower-privilege customer
worker. `authorized_scope` records task intent and is not an OS permission grant.
Use separate service identities, operating environments and credentials where
customer isolation is required.

One exact OAuth client maps to one logical agent. The server obtains that client
from authentication; senders cannot supply a different client identity in a tool
argument. Enrollment, route changes and native destination verification are local
operator operations. Sharing an OAuth client between unrelated chats also shares
its logical coordination identity; use distinct clients when those destinations
must remain distinct.

## Coordinator setup

Run the standalone companion when coordination should have its own origin and
state. It exposes coordination tools without importing the machine executor.
It retains the existing OAuth and administrative approval policy.

Configure these environment variables for `python -m coordination.service`:

| Variable | Meaning |
| --- | --- |
| `ASTER_MCP_PUBLIC_ORIGIN` | Public HTTPS origin owned by the operator |
| `ASTER_MCP_OAUTH_DB` | Absolute path to this companion's OAuth database |
| `ASTER_ADMIN_CLIENTS` | Absolute path to its owner-approved client allowlist |
| `ASTER_COORDINATION_DB` | Separate absolute SQLite path for coordination state |
| `ASTER_COORDINATION_PORT` | Loopback listener; default `18769` |
| `ASTER_COORDINATION_NATIVE_EVENTS` | Optional native Events support; default `0` |

Use distinct OAuth, coordination and allowlist files. Put a trusted HTTPS proxy
in front of the loopback listener using the existing [onboarding](ONBOARDING.md)
and [security](../SECURITY.md) requirements. Adding `ASTER_COORDINATION_DB` to a
supported existing gateway enables the optional tools there instead; its existing
machine permissions continue to apply.

Register and approve the intended clients through Aster's existing OAuth flow.
Then enroll their exact client IDs locally, for example:

```sh
python -m coordination.cli --db /var/lib/aster-coordination/tasks.sqlite init
python -m coordination.cli --db /var/lib/aster-coordination/tasks.sqlite enroll \
  --oauth-db /var/lib/aster-coordination/oauth.sqlite \
  --allowlist /var/lib/aster-coordination/approved-clients.json \
  --agent-id echo-worker --client-id EXACT_APPROVED_WORKER_CLIENT_ID \
  --name "Echo worker" --provider command --receive-mode pull
```

Enroll the sender with its own exact approved client ID. Grant only the needed
direction:

```sh
python -m coordination.cli --db /var/lib/aster-coordination/tasks.sqlite grant sender echo-worker
python -m coordination.cli --db /var/lib/aster-coordination/tasks.sqlite agents
```

Client IDs are identifiers, not bearer tokens. Enrollment does not issue a token.

## Run the no-model fixture first

Copy [the example configuration](../examples/coordination-worker.json) to an
operator-controlled file. Set its coordinator URL and absolute paths for your
installation. Create the workspace directory. The state directory must be private
to the worker account: mode `0700` on POSIX; use a restrictive ACL on Windows.
Use absolute Windows executable and directory paths in Windows JSON, escaping
backslashes normally. The configured executable must already be installed.

The default example invokes [coordination-echo.py](../examples/coordination-echo.py).
It parses the task and returns its data without executing its instructions. It
makes no model request and performs no business action.

Supply the worker's current OAuth bearer token through the environment variable
named by `token_env`. Provision it through your existing secure credential path;
do not paste it into task inputs, command arguments or the configuration file.
The worker consumes an existing token; it does not perform an interactive OAuth
flow or refresh a token automatically.

The current Aster OAuth provider issues access tokens for 900 seconds. The worker
reads its bearer once at connection startup. Expiry prevents subsequent calls and
can make in-flight work uncertain when renewal fails. For this development worker,
provision a fresh token and restart under operator control with its journal
preserved. Changing another process's environment does not refresh a running
worker. Unattended operation needs a separately implemented and tested credential
refresh mechanism; `--watch` alone does not supply one.

From an authorized sender, call `aster_delegate_task` with a fresh request ID:

```json
{
  "to_agent": "echo-worker",
  "request_id": "echo-check-001",
  "instruction": "Confirm receipt of the supplied fixture data.",
  "expected_result": "kind=echo_fixture, executed_instruction=false, and unchanged inputs",
  "inputs": {"message": "hello"},
  "authorized_scope": {"operation": "fixture_only"},
  "ttl_seconds": 3600
}
```

Reuse that request ID only when retrying the identical delegation. Then run:

```sh
python -m coordination.worker --config /absolute/path/worker.json --once
```

`--once` is the default: process at most one task or recover the saved receipt.
Use `--watch` only when you explicitly want a persistent polling worker:

```sh
python -m coordination.worker --config /absolute/path/worker.json --watch
```

Watch mode stops with exit code `2` when attention is required. Success or an empty
inbox exits `0` in single-run mode. Interrupt exits `130`. The console prints only
a small status summary and receipt path, not the bearer token, lease or model
output. Polling is a selected worker mode; native receiver failure does not
silently enable it or schedule a ChatGPT fallback task.

Read `aster_task_status` from the sender. Check the fixture output against the
expected fields. A sender can record actual checks through `aster_verify_result`
using the current revision and evidence. `completed` means that the worker's
terminal result was persisted; it is not independent proof of a business effect.

## Fixed command executor contract

Only the operator's configuration chooses the executable, arguments, environment
names, workspace and limits. There is no shell expansion, task-selected executable,
template substitution or sender-selected working directory. A sender's `inputs`
are data on stdin, even if they contain a field named `command`.

The executor receives one UTF-8 JSON document followed by a newline:

```json
{
  "version": 1,
  "task": {
    "task_id": "opaque-task-id",
    "instruction": "The authorized task",
    "expected_result": "Acceptance criteria",
    "inputs": {},
    "authorized_scope": {},
    "deadline": 1800000000
  }
}
```

The coordinator token, lease token and fencing number remain in the supervisor.
The child receives only explicitly listed environment variables. Required provider
credential variables must also appear in `env_passthrough`; missing ones stop the
worker before it claims new work. The coordinator token variable is forbidden in
that list.

The default `json_receipt` output format requires a matching task ID, a terminal
status and a genuine process exit code of zero:

```json
{
  "task_id": "opaque-task-id",
  "status": "completed",
  "exit_code": 0,
  "result": {"summary": "The observed result"}
}
```

A `job_id`, `ok=true`, `started`, progress message or running status is insufficient.
The supervisor also supports `claude_json` and `codex_jsonl`, described below.

The default workspace mode creates a fresh task-attempt directory under the
configured root. `workspace_mode: "fixed"` deliberately uses the configured
directory for every task; it does not isolate customer files or conversation
history. Neither mode is an operating-system sandbox. The command runs with the
worker account's privileges. Use a constrained account/container or an appropriately
restricted existing Aster execution environment for stronger isolation.

## Lease, output and recovery behavior

The supervisor renews its lease independently of the model. Any failed or invalid
renewal stops the local execution attempt and records uncertainty. Runtime and
output are bounded; timeout, excessive output or an invalid terminal receipt also
become `uncertain`. Nonzero process exit becomes `failed`; neither outcome triggers
automatic execution retry.

On POSIX the supervisor terminates the process group it created when stopping an
attempt. On Windows it terminates its direct subprocess; full descendant
containment requires a Job Object or another managed harness. Cancellation of a
local process cannot prove that a remote request or external side effect stopped.

The private journal follows these durable transitions:

1. Save a claim request ID before calling the coordinator. A lost response retries
   the same request.
2. Save `running` before launching the executor.
3. Save the complete result and evidence before sending completion.
4. Archive and remove the active receipt only after observing the matching
   persisted result and evidence, comparing canonical JSON types as well as
   values, or archive it explicitly as requiring reconciliation when
   the server has fenced or otherwise settled that attempt.

Restarting a `running` journal records uncertainty and **does not launch again**.
A pending completion retries its saved outcome, result, evidence, lease and fence;
it does not regenerate output. An acknowledgement without the matching terminal
task remains `receipt_pending`. Fenced receipts retain their output for operator
review with disposition `reconciliation_required`, never `acknowledged`.

Preserve the journal after an error. A claim whose reply was lost and whose lease
has since expired needs operator review; do not delete state to force execution.
The sender reconciles uncertain work using actual evidence. Retrying requires
`effects_absent=true` after checking for prior effects. Output and stderr in local
receipts may contain sensitive task data, so protect and retain them according to
your data policy. Archived receipts omit the lease secret; an active receipt needs
it for recovery.

## Provider configurations

### Claude Code headless

Set `output_format` to `claude_json` and configure a current, tested executable:

```json
{
  "argv": [
    "/absolute/path/to/claude", "--bare", "-p",
    "--output-format", "json", "--tools", "", "--disallowedTools", "mcp__*",
    "--no-session-persistence", "--max-turns", "4", "--max-budget-usd", "1.00"
  ],
  "output_format": "claude_json",
  "env_passthrough": ["PATH", "SystemRoot", "WINDIR", "TEMP", "TMP", "ANTHROPIC_API_KEY"],
  "required_env": ["ANTHROPIC_API_KEY"],
  "timeout_seconds": 240
}
```

This is a bounded text-processing example. Add tools only for the intended
workload. The parser requires `type=result`, `subtype=success`, `is_error=false`,
a nonempty result and process exit zero. Check installed CLI compatibility before
using it. Bare mode avoids implicit customization and requires explicit API
authentication; `--allowedTools` is an approval list, whereas `--tools` restricts
built-in availability. See Anthropic's [headless guide](https://code.claude.com/docs/en/headless),
[CLI reference](https://code.claude.com/docs/en/cli-reference) and
[SDK authentication guidance](https://code.claude.com/docs/en/agent-sdk/quickstart).

### Codex headless

Set `output_format` to `codex_jsonl`. A read-only starting configuration is:

```json
{
  "argv": [
    "/absolute/path/to/codex", "exec", "--json", "--ephemeral",
    "--sandbox", "read-only", "--ignore-user-config", "--ignore-rules",
    "--skip-git-repo-check", "-"
  ],
  "output_format": "codex_jsonl",
  "env_passthrough": ["PATH", "SystemRoot", "WINDIR", "TEMP", "TMP", "CODEX_API_KEY"],
  "required_env": ["CODEX_API_KEY"],
  "timeout_seconds": 240
}
```

The example uses an isolated task directory and therefore explicitly bypasses the
Git-directory prerequisite. The parser requires a final `turn.completed` event,
a completed agent-message item and exit zero; error/failed-turn events prevent
success. Pin and test the CLI version and its configuration policy. See OpenAI's
[noninteractive execution guide](https://developers.openai.com/codex/noninteractive).

### Other providers and existing machine tools

Use the same JSON receipt contract for a fixed operator-owned harness calling
another provider or existing Aster machine tools. Provider credentials belong to
that harness's explicit environment, not the delegated task.

For existing machine jobs, a harness must persist the returned stable `job_id`
before polling `job_status` (Windows: `background_job_status`). Only a terminal
`status=completed` with `exit_code=0` can produce a successful worker receipt.
`start_job`/`start_background_job` returning a job ID proves launch acceptance only.
After uncertain launch or timeout, inspect the saved job instead of starting a new
one; cancellation is a request, not proof of absent effects. A harness that cannot
prove terminal success must exit nonzero or return a non-completed receipt. This
worker includes the generic command boundary, not a separate remote-job adapter.

An agent driving the existing machine MCP tools should follow this sequence:

1. Claim the coordination task and retain its lease token/fence outside ordinary
   task text.
2. Use `aster_checkpoint_task` to record the intended machine, operation and
   execution attempt before launch, with the current task revision.
3. Call the existing machine's `start_job` or `start_background_job` once.
4. Checkpoint the returned stable `job_id` immediately. Renew the coordination
   lease while reading that job's actual status.
5. Confirm the terminal job state, exit code and requested output/effect. Then
   complete the coordination task with the observed job ID and evidence.
6. The requestor checks its acceptance criteria and records its own verification.

If the launch response is lost, the intent checkpoint is not proof of non-launch.
Mark the coordination attempt uncertain and investigate the machine's durable job
records. Do not repeat the launch to obtain a new ID. These are calls to the
existing machine gateway; coordination does not create a new remote execution
protocol or confer additional machine privileges.

## Native receiving is provider-specific

| Surface | Documented receiving behavior | This implementation |
| --- | --- | --- |
| ChatGPT eligible Work surfaces | Native MCP Events webhook subscriptions | Optional `ASTER_COORDINATION_NATIVE_EVENTS=1`; destination verification still required |
| Ordinary Claude custom connector | Tools invoked in a conversation | Ordinary tools; no assumed webhook wake |
| Claude Code headless / Agent SDK | Application starts and supervises execution | Configured command worker |
| Claude Code Channels | Events into an already running opted-in Code session | Separate preview transport; not implemented by this command worker |
| Other provider / local machine | Depends on its callable runtime | Fixed command harness with a terminal receipt |

For ChatGPT native receiving, enable the extension explicitly, enroll a native
receiver, then create the host subscription. Callback verification establishes a
transport destination; it does not identify the intended chat by itself. Inspect
the probe in the actual intended session, then record that observation locally:

The advertised event is `aster.coordination.changed`. Its subscription arguments
are `agent_id` and `expected_session_id`; the authenticated client must own that
enrolled native agent. Supply the actual expected host session identifier, not a
friendly label. The host supplies the callback URL and signing secret as webhook
delivery fields. Neither the user nor a task should invent or copy a callback from
another chat. If the actual destination cannot be observed, leave the binding
unverified; a working ordinary MCP tool does not establish native readiness.

```sh
python -m coordination.cli --db /var/lib/aster-coordination/tasks.sqlite binding native-agent
python -m coordination.cli --db /var/lib/aster-coordination/tasks.sqlite verify-receiver native-agent \
  --subscription-id ACTUAL_SUBSCRIPTION_ID \
  --observed-session-id ACTUAL_OBSERVED_SESSION_ID \
  --evidence-ref OPERATOR_INSPECTED_PROBE_REFERENCE
```

An alias or agent-reported session name is not sufficient evidence. This records
operator-observed binding, not cryptographic provider attestation. Readiness
expires or is revoked with its binding. Replacing a receiver invalidates old
ownership. Delivery can repeat; exclusive task claims prevent duplicate ownership.
An HTTP delivery receipt still does not mean that execution began or completed.

OpenAI native Events requires MCP revision `2026-07-28`; Claude Code's current
channel documentation says that revision cannot carry its channel messages.
Channels also require local stdio and an open session, with preview/account
controls. Keep the adapters separate. Primary references:

- [OpenAI custom MCP setup](https://developers.openai.com/api/docs/guides/custom-mcp-server)
- [OpenAI MCP Events contract](https://developers.openai.com/plugins/build/mcp-events)
- [Claude custom connectors](https://support.claude.com/en/articles/11175166-get-started-with-custom-connectors-using-remote-mcp)
- [Claude Code Channels](https://code.claude.com/docs/en/channels)
- [Claude channel protocol](https://code.claude.com/docs/en/channels-reference)
- [Claude MCP protocol compatibility](https://code.claude.com/docs/en/mcp)
- [Claude Messages API MCP connector](https://platform.claude.com/docs/en/agents-and-tools/mcp-connector)

## Local verification

```sh
python -m pytest -q tests/test_coordination_worker.py
```

These tests use temporary Store state, real no-model subprocesses, simulated
network failures and an authenticated in-memory SDK HTTP roundtrip. They exercise
terminal-result validation, lost-response recovery, lease renewal, token exclusion,
timeouts, output bounds, fencing and journal locking. They do not establish native
Windows qualification, live provider authentication, billing behavior or delivery
into an actual ChatGPT/Claude account.
