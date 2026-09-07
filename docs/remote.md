# Running the sandbox remotely (the control daemon)

[← prax-sandbox docs](README.md)

By default a harness drives the sandbox **in-process**: it imports
`prax_sandbox_client`, the control plane holds the local docker socket, and there
is no network, no token, no TLS. To run the sandbox on a **remote box** and drive
it from a harness elsewhere, run the **control daemon** (`prax-sandbox-daemon`)
beside the sandbox and point the harness at it.

The same `SandboxClient` facade is used either way — only the config changes:

```python
from prax_sandbox_client import SandboxClient, SandboxConfig

# local / in-process (default)
SandboxClient(SandboxConfig(workspace_dir="./workspace"))

# remote — drive a sandbox on another box
SandboxClient(SandboxConfig(
    daemon_url="https://sandbox-host:8843",
    daemon_token="…",            # bearer, sent on every request
    tls_verify="/path/ca.crt",   # True (system trust) | CA-bundle path. (False is accepted
                                 #  but disables cert AND hostname checks — see Security model)
))
```

## Security model

The daemon runs arbitrary shell **and `docker exec`/`build` as root** for any
caller it authenticates — treat the bearer token as **root-equivalent** on that
box. Therefore:

- **Auth is mandatory and enforced by the daemon itself** — a constant-time bearer
  check on every route (including the CDP WebSocket upgrade), independent of any
  network ACL. The daemon **refuses to start without a token**.
- **TLS is mandatory too** — the daemon refuses to bind a non-loopback interface in
  plaintext (the token would leak). Provide it one of three ways (below).
- **The sandbox's own ports are never published** by the remote compose: CDP
  (`9223`), the desktop (`6080`) and the clipboard bridge (`6090`) stay on the
  internal network. CDP is reachable **only** through the daemon's authenticated
  proxy (`/v1/cdp/*`); the desktop is not proxied at all. (Until 2026-07 this list
  also included an OpenCode server on `4096`, fronted by a separate internal
  password; that server no longer exists, and the daemon's `PRAX_SANDBOX_OPENCODE_*`
  settings for it were dropped on 2026-09-07.)
- **`/healthz` is unauthenticated** (an empty `200`, nothing else) and is reachable
  wherever `8843` is published; every `/v1` route requires the bearer.
- **`tls_verify=False` is accepted but dangerous.** It disables certificate *and*
  hostname verification on both the HTTP path (`requests` `Session.verify`) and
  the CDP WebSocket path (`cdp_service._ssl_context` sets `CERT_NONE`), so a
  man-in-the-middle can capture the root-equivalent bearer. Use system trust or a
  CA-bundle path; never `False` off-loopback.
- **`/source` is not mounted** in the remote compose — so remote shell commands
  can't read/modify the harness's own source on a shared box. Mount it only on a
  trusted single-tenant deployment if you want remote self-improvement.
- **`docker build` (image rebuild) is disabled by default** — set
  `PRAX_SANDBOX_ALLOW_REBUILD=true` only on a trusted box.

## Quick start (docker compose)

> **Note (2026-09-07).** Until this date the remote compose could not come up: its
> `sandbox` healthcheck curled the removed OpenCode server on `:4096` (so the
> `daemon` service's `condition: service_healthy` never cleared) and it demanded an
> `OPENCODE_SERVER_PASSWORD` that nothing consumed. Both are fixed: the healthcheck
> is `pgrep -x supervisord` (the same liveness check as the image's `HEALTHCHECK`
> and the local `docker-compose.yml`) and the only required secret is the daemon
> token. The repaired stack has not yet been brought up live end-to-end (a live
> integration test is on the README's *Planned* list); the steps below are what
> the compose file does.

```bash
cd prax-sandbox

# 1. Build the sandbox image
make build                                   # -> prax-sandbox:latest

# 2. Generate a self-signed TLS cert (or use your own / a reverse proxy / Tailscale)
mkdir -p certs
openssl req -x509 -newkey rsa:2048 -nodes -days 825 \
  -keyout certs/daemon.key -out certs/daemon.crt \
  -subj "/CN=sandbox-host" -addext "subjectAltName=DNS:sandbox-host,IP:127.0.0.1"

# 3. Secrets in .env
cat >> .env <<'EOF'
PRAX_SANDBOX_DAEMON_TOKEN=<a long random token>     # clients present this
EOF

# 4. Up — only the daemon's TLS port (8843) is published
docker compose -f docker-compose.remote.yml up --build -d
```

Then from your harness set `daemon_url=https://sandbox-host:8843` and the token,
with `tls_verify` pointing at `certs/daemon.crt` (or your CA). Verify that `9223`,
`6080` and `6090` are **not** reachable from the network.

## Exposing it (pick one — none require Tailscale)

- **Tailscale** (easy): bind the daemon to loopback and `tailscale serve --bg
  --https=443 https+insecure://localhost:8843`. You get HTTPS + device ACLs for
  free. (ACLs are a bonus — the bearer is still required.)
- **The daemon's own cert**: set `PRAX_SANDBOX_DAEMON_TLS_CERT` / `…_TLS_KEY`
  (the compose file does this from `./certs`). Reachable wherever you publish 8843.
- **Your reverse proxy** (Caddy/nginx/Traefik): terminate TLS there and proxy to
  the daemon on loopback. The daemon still enforces the bearer.
- **mTLS** (opt-in hardening): set `PRAX_SANDBOX_DAEMON_MTLS_CA`; clients pass
  `client_cert`/`client_key`. Layered on top of the bearer, never instead of it.

## Driving it from Prax specifically

Prax reads these from its `.env` and the same `SandboxClient` switches transport:

```bash
SANDBOX_DAEMON_URL=https://sandbox-host:8843
SANDBOX_DAEMON_TOKEN=<token>
SANDBOX_TLS_VERIFY=/etc/prax/certs/daemon.crt   # true | CA path  (false is accepted but disables cert + hostname checks — avoid)
# SANDBOX_CLIENT_CERT=… SANDBOX_CLIENT_KEY=…     # opt-in mTLS
```

What crosses the wire (`prax_sandbox/daemon/app.py`): one `/v1` route per
control-plane method (`/v1/shell`, `/v1/exec`, `/v1/packages`, `/v1/rebuild`,
`/v1/health`, `/v1/capabilities`, `/v1/runtime-mode`), the confined file API
(`/v1/files/*`, including `pull_tar` / `push_tar` for workspace sync) and the CDP
proxy (`/v1/cdp/*`). There is no SSE / live-output endpoint — the coding-session
SSE stream was removed with the session API in 2026-07 (`transport._iter_sse`
remains as dead code). Empty `SANDBOX_DAEMON_URL` → Prax uses the in-process sandbox (or
none, with `SANDBOX_ENABLED=false`).

## Daemon configuration reference

| Env var | Default | Purpose |
|---|---|---|
| `PRAX_SANDBOX_DAEMON_TOKEN` / `…_TOKEN_FILE` | — (**required**) | Bearer clients present |
| `PRAX_SANDBOX_DAEMON_HOST` / `…_PORT` | `127.0.0.1` / `8843` | Bind address |
| `PRAX_SANDBOX_DAEMON_TLS_CERT` / `…_TLS_KEY` | — | Direct HTTPS (else use a proxy / tailscale serve on loopback) |
| `PRAX_SANDBOX_DAEMON_MTLS_CA` | — | Require client certs (opt-in) |
| `PRAX_SANDBOX_CDP_HOST` / `…_CDP_PORT` | `127.0.0.1` / `9222` | Where the daemon reaches Chrome's CDP (`sandbox` / `9223` when containerized: the socat forwarder inside the sandbox) |
| `ANTHROPIC_API_KEY` / `OPENAI_API_KEY` / `SANDBOX_DEFAULT_MODEL` | — | **Not read.** The daemon reads no model keys or model name from its environment and forwards none into the container (`daemon/config.py`) — the sandbox is a keyless execution environment. Don't set them here. (`PRAX_SANDBOX_OPENCODE_HOST` / `…_PASSWORD` / `OPENCODE_SERVER_PASSWORD` were dropped on 2026-09-07 with the OpenCode server they configured.) |
| `PRAX_SANDBOX_CONTAINER_LABEL` | `com.docker.compose.service=sandbox` | How the daemon finds the container to exec into |
| `PRAX_SANDBOX_WORKSPACE_DIR` | `/workspace` | Per-user workspace root for the file API |
| `PRAX_SANDBOX_ALLOW_REBUILD` | `false` | Allow `docker build` image rebuild |
| `PRAX_SANDBOX_MAX_CONCURRENT_EXEC` / `…_MAX_PAYLOAD_BYTES` | `8` / `100 MiB` | Abuse caps |

## Limitations (v1)

- Single process (`workers=1`) — the control plane keeps its configuration in
  module-global state.
- The remote workspace is the daemon's (`PRAX_SANDBOX_WORKSPACE_DIR`); the harness
  moves files with the file API (`pull_tar` / `push_tar`). The canonical workspace
  stays on the harness side.
- The daemon container holds the docker socket (`/var/run/docker.sock`) — a
  compromise of the daemon is a compromise of the docker host.
- The tightened Chrome origin (`--remote-allow-origins=http://127.0.0.1:9222`) is
  untested against some Chrome builds; if CDP rejects connections, widen it in
  `sandbox/chromium-launch.sh`.
