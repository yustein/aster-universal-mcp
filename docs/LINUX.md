# Linux: experimental engineering deployment

This guide describes the Linux implementation. The Windows `0.1.0b1` technical-beta acceptance does not qualify a fresh Linux installation or a real Linux Claude-account connection. Complete the separate [Linux acceptance gate](RELEASE_STATUS.md) before advertising this deployment as accepted.

**Beta / advanced users only.** These steps create an OAuth-authenticated MCP endpoint which, after explicit owner approval, can run **privileged commands** and modify files on this computer. Use a disposable Ubuntu/Debian VM first. Never reuse production credentials.

## Prerequisites

- Ubuntu 24.04 LTS or Debian 13, Python 3.11+, sudo, Git
- A public HTTPS hostname under your control (for example `https://mcp.yourdomain.example`)
- Caddy, nginx or Cloudflare Tunnel to send *HTTPS* requests to local `127.0.0.1:8767`. Configure it to preserve the public `Host` and `X-Forwarded-Proto: https` headers.
- A Claude Free account; Free supports **one** remote custom MCP connector

## Install source and dependencies

```bash
git clone https://github.com/yustein/aster-universal-mcp.git
cd aster-universal-mcp
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
```

This beta deliberately uses manual source installation rather than a one-click installer.

For a machine you control, choose a dedicated directory for the public gateway code, such as `/opt/aster-universal-mcp`, and create **root-protected** operational directories:

```bash
sudo install -d -m 700 /var/lib/aster-universal-mcp
sudo install -d -m 700 /var/lib/aster-universal-mcp/admin-jobs
sudo install -d -m 700 /var/lib/aster-universal-mcp/transfers
sudo sh -c 'printf "[]\n" > /var/lib/aster-universal-mcp/admin-client-allowlist.json'
sudo chmod 600 /var/lib/aster-universal-mcp/admin-client-allowlist.json
```

Do not commit the SQLite databases, pairing codes, allowlist or other host-specific state.

## Configure the hostname

Set these values in the *root-owned service environment* (adjust domain and allowed root):

```bash
ASTER_MCP_PUBLIC_ORIGIN=https://mcp.yourdomain.example
ASTER_MCP_OAUTH_DB=/var/lib/aster-universal-mcp/oauth.sqlite
ASTER_ALLOWED_ROOT=/home/youruser/AsterMCPShared
ASTER_ADMIN_CLIENTS=/var/lib/aster-universal-mcp/admin-client-allowlist.json
ASTER_MCP_PORT=8767
```

`ASTER_ALLOWED_ROOT` controls the **directory-listing** tool. Privileged command tools use separately authorized administrator execution and are intentionally not confined to this folder.

Create the listing folder before launching. Make sure the process can access it.

## Start the local services

In a disposable test VM, open two separate elevated terminal sessions in the repository root.

Terminal 1 — local root-only admin socket, not exposed over TCP:

```bash
sudo .venv/bin/python admin_executor_local.py
```

Terminal 2 — root-owned OAuth service, bound to loopback:

```bash
sudo env \
  ASTER_MCP_PUBLIC_ORIGIN=https://mcp.yourdomain.example \
  ASTER_MCP_OAUTH_DB=/var/lib/aster-universal-mcp/oauth.sqlite \
  ASTER_ALLOWED_ROOT=/home/youruser/AsterMCPShared \
  ASTER_ADMIN_CLIENTS=/var/lib/aster-universal-mcp/admin-client-allowlist.json \
  ASTER_MCP_PORT=8767 \
  .venv/bin/python oauth_service.py
```

**Do not run the OAuth listener on a public address.** It binds to `127.0.0.1:8767`. Place a trusted TLS reverse proxy or persistent tunnel in front of it. The remote MCP URL entered in Claude is `https://mcp.yourdomain.example/mcp`.

For repeatable host-managed service setup, configure systemd yourself using the above two commands; keep the administrative Unix socket root-only and the OAuth listener loopback-only. Do not enable unreviewed services on a production machine.

## Connect Claude Free using the remote connector

1. In Claude, go to **Customize → Connectors → Add → Custom → Web**.
2. Set the remote server URL to `https://mcp.yourdomain.example/mcp`.
3. Choose **Sign in now** and, for OAuth client identification, **Register automatically** (dynamic client registration).
4. Aster's authorization page prompts for a short-lived local owner pairing code. Generate it in a **separate trusted local terminal**, using the exact same OAuth DB:

```bash
sudo .venv/bin/python pairing_admin.py \
  --db /var/lib/aster-universal-mcp/oauth.sqlite issue
```

5. Enter the one-time pairing code at your own Aster HTTPS origin. Finish the redirect back to Claude.

OAuth authorization alone does not unlock privileged command tools. Explicitly allow the registered Claude OAuth client's ID using **local operator commands**:

```bash
sudo .venv/bin/python admin_client_allowlist.py \
  --db /var/lib/aster-universal-mcp/oauth.sqlite \
  --allowlist /var/lib/aster-universal-mcp/admin-client-allowlist.json clients
```

Verify the client ID and redirect URI correspond to your Claude connection. Then authorize exactly that ID:

```bash
sudo .venv/bin/python admin_client_allowlist.py \
  --db /var/lib/aster-universal-mcp/oauth.sqlite \
  --allowlist /var/lib/aster-universal-mcp/admin-client-allowlist.json approve CLIENT_ID
```

You can revoke later with `revoke CLIENT_ID`. Never approve unexpected client IDs, and do not expose the approval CLI as an MCP tool.

## Test and rollback

Run `.venv/bin/python -m pip install -r requirements-dev.txt` and `.venv/bin/python -m pytest -q tests` before connecting. During beta testing ask Claude to list the allowed directory, execute a harmless command such as `id`, and inspect a bounded background job. Review Claude's tool-use approval. If you discover unexpected behavior, remove the registered client ID from the allowlist, stop the gateway and revoke the OAuth client/tokens.

**Limits:** Aster is powerful host-management software. Windows and Linux privilege behavior differs; localhost HTTP is not a substitute for correctly configured external HTTPS and OAuth. A real Claude Free end-to-end account test remains required before marking compatibility complete. Read [SECURITY.md](../SECURITY.md).


### Simplified pairing for beta testers

Run `sudo scripts/issue-pairing-linux.sh` to issue a 12-character code displayed in groups of four, valid for 15 minutes. The script uses `ASTER_MCP_OAUTH_DB` or the configured default and never calls a remote endpoint.
