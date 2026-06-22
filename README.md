# prax-sandbox

A standalone, **plug-and-play** code-execution sandbox for agentic harnesses —
carved out of [Prax](https://github.com/praxagent/prax) so any harness can use it.

A long-running Docker container runs the coding agents (OpenCode / Claude Code /
Codex), a headless+desktop Chromium (CDP + noVNC), and a full toolchain
(TeX, ffmpeg, pandoc, hugo, …). Your harness drives it through a small Python
client that has **no dependency on any harness**.

## Layout

| Piece | What it is |
|-------|-----------|
| `sandbox/` | The container image (supervisord, OpenCode, Chromium/CDP, desktop, cast extension). |
| `prax_sandbox_client/` | The public API a harness imports: `SandboxClient`, `SandboxConfig`, `SandboxSession`. |
| `prax_sandbox/` | Host-side control plane: `control_plane` (session lifecycle + OpenCode HTTP), `exec` (`docker exec`), `cdp_service` (Chrome DevTools). Privileged + docker-aware; reached only through the client. |

## Quick start

```bash
# 1. Build + run the sandbox
make build
docker compose up -d            # OpenCode :4096, CDP :9223, desktop :6080

# 2. Drive it from your harness (no prax required)
pip install -e .
```

```python
from prax_sandbox_client import SandboxClient, SandboxConfig

client = SandboxClient(SandboxConfig(
    host="localhost",                 # where the container is reachable
    workspace_dir="./workspace",
    anthropic_key="sk-ant-...",
    # inject your own side effects (optional):
    on_output=lambda label, text: print(text, end=""),
    resolve_workspace=my_workspace_resolver,   # else a local dir is used
    commit=my_git_commit,                       # else archiving skips commit
))

s = client.start_session("user-1", "Write a Fibonacci CLI and test it")
print(client.run_shell("ls -la /workspace/active"))
client.finish_session("user-1", summary="done")
```

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
- [x] `debian:trixie` image with full toolchain — TeX Live, ffmpeg, poppler, pandoc, hugo, git, jq, mermaid-cli, a faster-whisper venv
- [x] `supervisord` process tree (X stack, Chromium, CDP bridge, clipboard bridge, OpenCode) + one-shot config-seeding entrypoint with a PATH-integrity smoke check
- [x] `make build` / compose build of `prax-sandbox:latest`

**Coding agents**
- [x] OpenCode, Claude Code, Codex, and code-server installed in-image
- [x] OpenCode served headless on `:4096` as the driven coding-agent server

**Control plane**
- [x] Session lifecycle over the OpenCode HTTP API (create / start / message / review / finish / abort)
- [x] `docker exec` primitive (container discovery by compose-service label)
- [x] On-the-fly package install (apt / pip / npm) with manifest tracking + image rebuild from an edited Dockerfile

**Interactive sessions & budget**
- [x] Harness ↔ coding-agent feedback loop (async prompt + poll, incremental live-output, mid-session model switch)
- [x] Stuck-session protection (per-message timeout, consecutive-failure tracking, auto-abort after 3 failures)
- [x] Round-based budget per session (only successful rounds counted; wall-clock timeout timer)

**Browser & desktop**
- [x] Single Chromium serving both the noVNC desktop (Xvfb → x11vnc → websockify) and CDP, hardened flags + cast extension
- [x] stdlib CDP service (navigate, page text, screenshot, click/type/scroll, `evaluate_js`) — daemon-proxy-aware in remote mode

**File API & solution reuse**
- [x] Confined per-user file API (realpath containment, `O_NOFOLLOW` writes, zip-slip/symlink-hardened tar push/pull)
- [x] Solution archiving (`SOLUTION.md` + session log via injected git hook), grep search, and re-execute-from-archive

**Remote control daemon (optional)**
- [x] FastAPI daemon — one `/v1` route per control-plane method, confined file API, SSE live-output, concurrency limiter, payload cap
- [x] Constant-time bearer auth on every route (incl. the CDP WS upgrade), layered under optional mTLS
- [x] Authenticated CDP proxy — auth-before-dial; the only network path to Chrome
- [x] Fail-closed config (no token → refuse start; non-loopback plaintext bind refused; separate internal OpenCode password)
- [x] Remote compose publishes only the daemon's TLS `:8843`; sandbox ports stay internal

**Client transport seam**
- [x] Harness-agnostic `SandboxClient` / `SandboxConfig` over a Transport seam (in-process docker socket vs HTTP daemon, selected solely by `daemon_url`)
- [x] Injected host side-effects (`on_output` / `resolve_workspace` / `commit`) — the control plane never imports a harness
- [x] ruff + pytest CI (daemon, control-plane, file-API, transport, remote-client suites; HTTP/docker mocked)

### Planned

- [ ] Live end-to-end integration test against a running OpenCode (real `/global/health` + a real session round) — tests currently mock OpenCode/docker
- [ ] First-class GPU support in this repo's own compose (works today via the harness's `docker-compose.gpu.yml` + `make sandbox-gpu`)
- [ ] Kubernetes / Helm deployment path for the daemon + sandbox
- [ ] Multi-tenant isolation (per-user containers/namespaces — one persistent container is shared today)
- [ ] MCP server exposing sandbox tooling to other harnesses
- [ ] Reconcile stale `docs/` with the persistent-only code (ephemeral mode was dropped; manifests are not auto-reinstalled on rebuild; base image is `debian:trixie` + code-server)

## Develop

```bash
make ci        # ruff + pytest (HTTP/docker interactions are mocked; [daemon] extra installed)
```

Install the daemon dependencies for local daemon work: `pip install -e ".[daemon]"`.
