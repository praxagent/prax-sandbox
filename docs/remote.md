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
    tls_verify="/path/ca.crt",   # True (system trust) | False | CA-bundle path
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
- **The sandbox's own ports are never exposed.** OpenCode (`4096`), CDP (`9223`),
  and the desktop (`6080`) stay on the internal network. CDP is reachable **only**
  through the daemon's authenticated proxy (`/v1/cdp/*`); OpenCode is fronted by a
  **separate** internal password the daemon holds (never the client-facing token).
- **`/source` is not mounted** in the remote compose — so the coding agents can't
  read/modify the harness's own source on a shared box. Mount it only on a trusted
  single-tenant deployment if you want remote self-improvement.
- **`docker build` (image rebuild) is disabled by default** — set
  `PRAX_SANDBOX_ALLOW_REBUILD=true` only on a trusted box.

## Quick start (docker compose)

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
OPENCODE_SERVER_PASSWORD=<a different random secret> # internal, never client-facing
EOF

# 4. Up — only the daemon's TLS port (8843) is published
docker compose -f docker-compose.remote.yml up --build -d
```

Then from your harness set `daemon_url=https://sandbox-host:8843` and the token,
with `tls_verify` pointing at `certs/daemon.crt` (or your CA). Verify that `4096`
and `9223` are **not** reachable from the network.

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
SANDBOX_TLS_VERIFY=/etc/prax/certs/daemon.crt   # true | false | CA path
# SANDBOX_CLIENT_CERT=… SANDBOX_CLIENT_KEY=…     # opt-in mTLS
```

Live coding-agent output streams back to Prax over SSE; archives are pulled into
Prax's git workspace; `review`/`search` run server-side. Empty `SANDBOX_DAEMON_URL`
→ Prax uses the in-process sandbox (or none, with `SANDBOX_ENABLED=false`).

## Daemon configuration reference

| Env var | Default | Purpose |
|---|---|---|
| `PRAX_SANDBOX_DAEMON_TOKEN` / `…_TOKEN_FILE` | — (**required**) | Bearer clients present |
| `PRAX_SANDBOX_DAEMON_HOST` / `…_PORT` | `127.0.0.1` / `8843` | Bind address |
| `PRAX_SANDBOX_DAEMON_TLS_CERT` / `…_TLS_KEY` | — | Direct HTTPS (else use a proxy / tailscale serve on loopback) |
| `PRAX_SANDBOX_DAEMON_MTLS_CA` | — | Require client certs (opt-in) |
| `OPENCODE_SERVER_PASSWORD` | — | Internal OpenCode auth (sandbox + daemon share it; never client-facing) |
| `PRAX_SANDBOX_OPENCODE_HOST` / `PRAX_SANDBOX_CDP_HOST` / `…_CDP_PORT` | `localhost` / `127.0.0.1` / `9222` | Where the daemon reaches the sandbox container (`sandbox`/`9223` when containerized) |
| `PRAX_SANDBOX_CONTAINER_LABEL` | `com.docker.compose.service=sandbox` | How the daemon finds the container to exec into |
| `PRAX_SANDBOX_WORKSPACE_DIR` | `/workspace` | Per-user workspace root for the file API |
| `PRAX_SANDBOX_ALLOW_REBUILD` | `false` | Allow `docker build` image rebuild |
| `PRAX_SANDBOX_MAX_CONCURRENT_EXEC` / `…_MAX_PAYLOAD_BYTES` | `8` / `100 MiB` | Abuse caps |

## Limitations (v1)

- Single process (`workers=1`) — the control plane keeps in-memory session state.
- The remote workspace is the daemon's; the harness pulls solutions back to its own
  git on `finish`. The canonical workspace stays on the harness side.
- The tightened Chrome origin (`--remote-allow-origins=http://127.0.0.1:9222`) is
  untested against some Chrome builds; if CDP rejects connections, widen it in
  `sandbox/chromium-launch.sh`.
