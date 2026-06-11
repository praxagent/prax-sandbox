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
Running the sandbox on a **remote server** over a VPC is the next milestone: the
same client points at a control daemon URL + token instead of a local socket.

## Develop

```bash
make ci        # ruff + pytest (HTTP/docker interactions are mocked)
```
