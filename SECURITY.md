# Security policy — privileged MCP beta

**High-risk beta:** This release intentionally exposes powerful host-execution
tools over an OAuth-protected MCP connection. Only install on systems you own
and control, after reading this entire document. Do not use production customer
data or financial/health records for beta testing.

- The MCP service binds to `127.0.0.1` and must sit behind a trusted HTTPS
  reverse proxy or secure tunnel with the correct public Host and HTTPS scheme.
  Never expose port 8767/18767 directly to the public internet.
- Each client receives a short-lived `system:admin` token only after local
  owner pairing and PKCE authorization. Privileged tools additionally require
  its OAuth client ID on a **separate local operator allowlist**.
- Never use a default, shared admin password, broad wildcard allowlist,
  anonymous login, or hidden credentials in the repository.
- Run Linux's administrator executor through a root-only Unix socket;
  the OAuth web process must be deployed following the platform isolation
  guide. Never make the Unix socket world-writable.
- Windows privileged operations inherit the gateway process account; use a
  dedicated account and review the consequences before elevated execution.
- Every tool call is potentially sensitive: commands, stdout, and file contents
  can be submitted to Claude. Use a disposable test machine first.
- A free Claude account supports one remote custom MCP connector, but it
  connects from Anthropic's cloud to a publicly reachable HTTPS URL.
- Protect the OAuth SQLite database, allowlist and pairing codes; file
  permissions 0600 for operator-owned files on Linux, restricted ACLs on Windows.
- The current beta does **not** include a third-party security audit, independent
  signed binary installer, or guarantee against filesystem race conditions.
  OAuth pairing, rate limiting and native host capabilities require continued
  adversarial testing prior to a general public rollout.

Report vulnerabilities privately to the project maintainer via GitHub's
private vulnerability reporting feature, if enabled, or another private
channel rather than publishing exploit details.
