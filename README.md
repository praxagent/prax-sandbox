# prax-sandbox

A standalone, **plug-and-play** code-execution sandbox for agentic harnesses —
carved out of [Prax](https://github.com/praxagent/prax) so any harness can use it.

A long-running Docker container provides a shell/exec environment with a full
toolchain (Python plus a scratch venv with DuckDB, pandas, sympy and
faster-whisper; Lean 4; TeX Live, ffmpeg, poppler, pandoc, hugo, mermaid-cli,
Node.js), one Chromium instance that serves both a noVNC desktop and the Chrome
DevTools Protocol, and an XFCE desktop on Xvfb. Your harness drives it through a
small Python client that has **no dependency on any harness**.

> **Removed 2026-07 (#4, #5).** The sandbox used to ship AI coding-agent CLIs
> (OpenCode / Claude Code / Codex), run an OpenCode server on `:4096`, and expose
> a coding-*session* API (`start_session` / `send_message` / `finish_session`,
> solution archiving, SSE live output). All of that is gone: the sandbox is a
> **pure execution environment** and needs **no model API key** (this repo's
> compose files pass none). Rationale: prax `docs/security/sandbox-execution-boundary.md`.
> Some residue remains in code and in the remote compose file — see *Known gaps*
> below.

## Layout

| Piece | What it is |
|-------|-----------|
| `sandbox/` | The container image: `Dockerfile`, `supervisord.conf` (Xvfb, dbus, x11vnc, xfwm4 / xfce4-panel / xfdesktop, websockify, clipboard bridge, Chromium, socat CDP forwarder), `entrypoint.sh`, `chromium-launch.sh`, `clipboard-bridge.py`, `bash-respawn.sh`, and the `cast-ext/` Chrome extension. |
| `prax_sandbox_client/` | The public API a harness imports: `SandboxClient`, `SandboxConfig`, `SandboxTransportError`, `ExecResult`. (`SandboxSession` / `RemoteSession` are still exported, but nothing produces them since the session API was removed.) |
| `prax_sandbox/` | Host-side control plane: `control_plane` (`run_shell` / `run_command` / `install_package` / `rebuild_sandbox` / `health`), `exec` (`docker exec`), `fileops` (confined per-user file API), `cdp_service` (Chrome DevTools client), `daemon/` (the optional remote FastAPI daemon). Privileged + docker-aware; reached only through the client. |

## Quick start

```bash
# 1. Build + run the sandbox
make build
docker compose up -d     # publishes, on 127.0.0.1 only: CDP :9223, noVNC desktop :6080, clipboard bridge :6090

# 2. Drive it from your harness (no prax required)
pip install -e .
```

```python
from prax_sandbox_client import SandboxClient, SandboxConfig

client = SandboxClient(SandboxConfig(
    workspace_dir="./workspace",   # the host dir compose bind-mounts at /workspace
    # container_label="com.docker.compose.service=sandbox",  # how the container is found (default)
))

client.health()                                    # True once `docker exec … true` succeeds
client.run_shell("ls -la /workspace")              # {"stdout": ..., "stderr": ..., "exit_code": ...}
client.run_command(["python3", "-c", "print(2+2)"], cwd="/workspace")   # subprocess.CompletedProcess
client.install_package("poppler-utils")            # apt-get install inside the container
```

The full facade (`prax_sandbox_client/client.py`): `run_shell`, `run_command`,
`install_package`, `rebuild_sandbox`, `get_runtime_mode`, `health`,
`capabilities`, and the confined per-user file API `file_list` / `file_read` /
`file_write` / `file_grep` / `pull_tar` / `push_tar`. Browser control goes
through `prax_sandbox.cdp_service` (navigate, page text, screenshot,
click / type / scroll, `evaluate_js`).

The client talks to the control plane in-process (it holds the docker socket).
To run the sandbox on a **remote server** (with or without Tailscale), run the
control daemon (`prax-sandbox-daemon`) and point the same client at its URL +
bearer token — see **[docs/remote.md](docs/remote.md)**.

## Docs

- [docs/](docs/README.md) — sandbox internals (code execution, desktop, browser)
- [docs/remote.md](docs/remote.md) — running it remotely over TLS + bearer

## Roadmap

### Shipped

**Container image & toolchain**
- [x] `debian:trixie-slim` image with full toolchain — TeX Live, ffmpeg, poppler, pandoc, hugo, git, jq, mermaid-cli (pointed at the image's own Chromium, no second browser download), Node.js/npm, `uv`, a scratch venv at `/opt/prax-venv` (faster-whisper, duckdb, pandas, sympy), Lean 4 via elan at `/opt/elan`, code-server (installed, not started)
- [x] `supervisord` process tree (Xvfb, dbus, x11vnc, xfwm4 / xfce4-panel / xfdesktop, websockify, clipboard bridge, Chromium, socat CDP forwarder) + one-shot entrypoint (profile/XFCE seeding, cast-extension setup, PATH-integrity smoke check)
- [x] `make build` / compose build of `prax-sandbox:latest`; image `HEALTHCHECK` = `pgrep -x supervisord`

**Control plane**
- [x] `docker exec` primitives — `run_shell` / `run_command` (container discovery by compose-service label)
- [x] `install_package` (apt) + best-effort install manifests under `/root/` for apt/pip/npm commands seen by `run_shell`; image rebuild from an edited Dockerfile (`rebuild_sandbox`)
- [x] Liveness = "can I `docker exec true`" (`health`)

**Browser & desktop**
- [x] Single non-headless Chromium serving both the noVNC desktop (Xvfb → x11vnc → websockify) and CDP (`:9222`, forwarded to `:9223` by socat), with the Tab Cast extension pre-loaded
- [x] stdlib CDP service (navigate, page text, screenshot, click/type/scroll, `evaluate_js`) — daemon-proxy-aware in remote mode

**File API**
- [x] Confined per-user file API (realpath containment, `O_NOFOLLOW` writes, zip-slip/symlink-hardened tar push/pull)

**Remote control daemon (optional)**
- [x] FastAPI daemon — one `/v1` route per control-plane method, confined file API, concurrency limiter, payload cap
- [x] Constant-time bearer auth on every `/v1` route (incl. the CDP WS upgrade), layered under optional mTLS
- [x] Authenticated CDP proxy — auth-before-dial
- [x] Fail-closed config (no token → refuse start; non-loopback plaintext bind refused)
- [x] Remote compose publishes only the daemon's TLS `:8843`; sandbox ports stay internal — **but that compose file cannot currently start; see Known gaps**

**Client transport seam**
- [x] Harness-agnostic `SandboxClient` / `SandboxConfig` over a Transport seam (in-process docker socket vs HTTP daemon, selected solely by `daemon_url`)
- [x] `make ci` = ruff + pytest (daemon, control-plane, file-API, transport, remote-client suites; HTTP/docker mocked). Local only — this repo has **no hosted CI workflow** (no `.github/` directory).

**Removed 2026-07 (#4, #5)** — the coding-agent CLIs (OpenCode / Claude Code / Codex) and the OpenCode server on `:4096`; the coding-session API (`start_session` / `send_message` / `review` / `finish` / `abort`) with its round budgets and stuck-session protection; solution archiving + search / re-execute; SSE live output; model API keys in the container env. The injected `on_output` / `resolve_workspace` / `commit` callbacks no longer drive anything (the fields remain on `SandboxConfig`).

### Known gaps (2026-09)

- **`docker-compose.remote.yml` cannot start as committed.** Its `sandbox` healthcheck still curls the removed OpenCode `:4096` (line 29), the `daemon` service waits on `condition: service_healthy` (lines 60-62), and line 48 still requires `OPENCODE_SERVER_PASSWORD` for the removed subsystem. The local `docker-compose.yml` was updated to `pgrep -x supervisord` and works.
- **Residue of the removed subsystem in code.** `prax_sandbox/daemon/config.py` still reads `PRAX_SANDBOX_OPENCODE_*`, `ANTHROPIC_API_KEY` and `OPENAI_API_KEY` from the daemon's env into `SandboxConfig` fields (`anthropic_key`, `openai_key`, `opencode_password`, `host`, `default_model`) that the control plane never reads; `daemon/app.py` still probes `:4096` at startup when that password is set; `SandboxConfig` keeps `anthropic_key` / `openai_key` / `opencode_password` and the session-policy fields, none of which the control plane reads; `transport._iter_sse` has no callers.
- **Container hardening.** The container runs as root (no `USER` in the Dockerfile; `supervisord.conf` `user=root`), Chromium launches with `--no-sandbox`, and neither compose file sets memory/pids limits, `cap_drop`, `security_opt`, or a bounded `/tmp`. The docker socket is not mounted into the sandbox container.
- **code-server** is installed and `8443` is `EXPOSE`d, but no supervisord program starts it and neither compose file publishes `8443`.

### Planned

- [ ] Fix `docker-compose.remote.yml` (healthcheck → `pgrep -x supervisord`, drop the OpenCode variables) and strip the OpenCode / model-key residue from `daemon/config.py`, `daemon/app.py` and `SandboxConfig`
- [ ] Live end-to-end integration test against a running sandbox container (a real `docker exec` round trip) — tests currently mock docker/HTTP
- [ ] Hosted CI for this repo (the `make ci` suite exists; nothing runs it on push)
- [ ] Container hardening: non-root user, drop `--no-sandbox`, pids/memory limits, bounded `/tmp`
- [ ] First-class GPU support in this repo's own compose (works today via the harness's `docker-compose.gpu.yml` + `make sandbox-gpu`)
- [ ] Kubernetes / Helm deployment path for the daemon + sandbox
- [ ] Multi-tenant isolation (per-user containers/namespaces — one persistent container is shared today)
- [ ] MCP server exposing sandbox tooling to other harnesses
- [ ] Reconcile the remaining stale text with the persistent-only, execution-only code: `docs/` were regenerated from `sandbox/Dockerfile` / `supervisord.conf` / `entrypoint.sh` on 2026-09-06; what is left is in code comments, `docker-compose.remote.yml` and the `Makefile` `build` comment

## Develop

```bash
make ci        # ruff + pytest (HTTP/docker interactions are mocked; [daemon] extra installed)
```

Install the daemon dependencies for local daemon work: `pip install -e ".[daemon]"`.
