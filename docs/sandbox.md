# Sandbox Code Execution

[← prax-sandbox docs](README.md)

> Part of **prax-sandbox**. This documents the sandbox's internals (the container,
> the image, the exec control plane). The agent-facing tools that *drive* it
> (`run_python`, `sandbox_*`, `data_query`, `lean_check`) live in the harness that
> consumes the sandbox — for the reference integration see the Prax repo
> (`docs/infrastructure/sandbox.md`).

> **Regenerated 2026-09-06** from `sandbox/Dockerfile`, `sandbox/supervisord.conf`,
> `sandbox/entrypoint.sh`, `docker-compose.yml` and `docker-compose.remote.yml`.
> Until 2026-07 the sandbox also ran AI coding-agent CLIs (OpenCode / Claude Code /
> Codex) and an OpenCode session server on `:4096`; those were removed in #4 / #5
> — the sandbox is now a **pure execution environment** with **no model API key**
> in the image or in this repo's compose files (rationale: prax
> `docs/security/sandbox-execution-boundary.md`). **Known gap (2026-09):** the Prax
> harness's own `docker-compose.yml` and `docker-compose.lite.yml` still forward
> `ANTHROPIC_API_KEY` / `OPENAI_API_KEY` into their `sandbox` service; that doc
> tracks it.
> The design narrative that assumed them is kept only under *History* at the end.

### The Problem

Instead of adding infinite specialized tools (one for LaTeX, one for ffmpeg, one for data transforms...), give the agent a sandbox where it can write and execute its own code. The hardest or most common operations stay as dedicated tools; everything else the agent codes up itself.

### What the sandbox is

One **always-on** container. `SandboxConfig.persistent` is `True` and is the only
mode (`prax_sandbox_client/config.py`); the per-session ephemeral containers of
the original design were dropped. The harness reaches it with `docker exec`
through `prax_sandbox.control_plane`:

| Call | What it does |
|---|---|
| `run_shell(command, timeout=60)` | `sh -c <command>` in the container; returns `{"stdout", "stderr", "exit_code"}` (stdout capped at 10 000 chars, stderr at 5 000). |
| `run_command(cmd, cwd=None, env=None, timeout=300)` | argv exec (paths already translated by the harness); returns a `subprocess.CompletedProcess`. |
| `install_package(name)` | `apt-get install -y --no-install-recommends <name>` in the container (name regex-validated) and appends it to `/root/.installed_packages`. |
| `rebuild_sandbox(dockerfile_content=None)` | optionally overwrites `sandbox/Dockerfile`, runs `docker build -t prax-sandbox:latest sandbox/`, restarts the container (`container.restart`), and waits up to 60 s for `docker exec true` to succeed. |
| `health()` | `True` once `docker exec … true` succeeds (there is no HTTP health endpoint in the container any more). |

The container is located by docker label (`com.docker.compose.service=sandbox` by
default — `SandboxConfig.container_label`), not by hostname. The `timeout`
arguments are accepted for signature parity but are **not enforced** by the
`docker exec` layer (`prax_sandbox/exec.py`).

### Sandbox Docker Image (`sandbox/Dockerfile`)

Base image `debian:trixie-slim`. Installed at build time:

- **Languages / runtimes:** `python3` + `pip` + `venv`, `nodejs` + `npm`, `uv` (copied to `/usr/local/bin`).
- **Scratch Python venv** at `/opt/prax-venv` with `faster-whisper`, `duckdb`, `pandas`, `sympy` — the entrypoint prepends it to `PATH` for supervisord's children (a `docker exec` shell does not inherit that), so harness tools address `/opt/prax-venv/bin/python` explicitly.
- **Lean 4** via elan at `/opt/elan` (`ELAN_HOME` baked; toolchain `leanprover/lean4:v4.31.0`; `lean` / `lake` symlinked into `/usr/local/bin`). mathlib is not fetched.
- **Documents / media:** TeX Live (`texlive-latex-base` / `-extra` / `-recommended`, `-fonts-recommended` / `-extra`, `-science`, `-bibtex-extra`, `-publishers`, `lmodern`, `cm-super`, `latexmk`, `biber`), `ffmpeg`, `poppler-utils`, `pandoc`, `hugo`, `imagemagick`, and `@mermaid-js/mermaid-cli` (npm; puppeteer is pointed at the image's Chromium via `PUPPETEER_SKIP_DOWNLOAD` / `PUPPETEER_EXECUTABLE_PATH`, and `/etc/puppeteer-config.json` supplies `--no-sandbox`).
- **Desktop / browser stack:** `xvfb`, `x11vnc`, `xfce4`, `xterm`, `dbus-x11`, `novnc`, `websockify`, `xdotool`, `scrot`, `xsel`, `python3-websockets`, `socat`, `supervisor`, and Debian's `chromium` package (symlinked as `/usr/bin/chromium-browser`; `/etc/chromium.d/extensions` is rewritten so the empty `--load-extension=` Debian injects cannot block the cast extension).
- **code-server** (web VS Code) via `curl -fsSL https://code-server.dev/install.sh | sh`. It is *installed only*: no supervisord program starts it, and neither compose file publishes its port. `8443` is `EXPOSE`d for it.
- **Misc:** `git`, `curl`, `wget`, `jq`, `tmux`, `psmisc`, `ca-certificates`.

Baked environment: `SHELL=/bin/bash`, `DISPLAY=:99`, `ELAN_HOME=/opt/elan`,
`WORKDIR /workspace`. `EXPOSE 6080 6090 8443`. Image `HEALTHCHECK` =
`pgrep -x supervisord`. `CMD` = `/usr/local/bin/entrypoint.sh`.

**Rule baked into the Dockerfile:** never install user-facing tooling under
`/root`. The Prax harness's compose bind-mounts a per-user directory over `/root`
at runtime, which hides anything baked there (that is why `uv`, the venv and elan
live in `/usr/local/bin` and `/opt`). The entrypoint logs any dangling `PATH`
symlink it finds at boot.

### Process tree (`sandbox/supervisord.conf`)

`entrypoint.sh` prepares on-disk state and then `exec`s supervisord as PID 1
(`user=root`). Every program has `autostart=true`, `autorestart=true`,
`startretries=10` and logs to the container's stdout/stderr:

| Priority | Program | Command / role |
|---|---|---|
| 10 | `xvfb` | `Xvfb :99 -screen 0 1920x1080x24` |
| 15 | `dbus` | session bus at `unix:path=/run/dbus-session.sock` |
| 20 | `x11vnc` | `-display :99 -forever -shared -nopw -rfbport 5900` (no VNC password) |
| 25 / 30 / 35 | `xfwm4` / `xfce4-panel` / `xfdesktop` | the XFCE session |
| 40 | `websockify` | `--web=/usr/share/novnc 6080 localhost:5900` (noVNC) |
| 45 | `clipboard-bridge` | `clipboard-bridge.py` — WebSocket server on `6090` syncing the X11 clipboard via `xsel` |
| 50 | `chromium` | `chromium-launch.sh` — non-headless, `--no-sandbox`, `--remote-debugging-port=9222`, profile `/root/.browser_profiles/default`, cast extension from `/opt/prax-cast-ext` |
| 55 | `cdp-proxy` | `socat TCP-LISTEN:9223,fork,reuseaddr,bind=0.0.0.0 TCP:127.0.0.1:9222` |

There is no `[program:opencode]` and no `[program:code-server]`.

### Entrypoint (`sandbox/entrypoint.sh`)

One-shot, in order: create the browser profile dir and clear its Chromium
singleton locks; remove stale `:99` X locks; **print** any package manifests found
under `/root` (see below); create `/opt/prax-venv` if missing and prepend it to
`PATH`; seed XFCE config, `.Xresources` and default-application entries under
`/root` on first run only; rewrite the cast extension's signaling host
(`PRAX_CAST_SIGNALING_HOST`, default `prax:8000`) and wipe its cached service
worker; pin the extension in Chromium's `Preferences`; scan the `PATH` dirs for
dangling symlinks and warn; `exec supervisord`.

### Ports and exposure

| Port | In the container | `docker-compose.yml` (local) | `docker-compose.remote.yml` |
|---|---|---|---|
| 9222 | Chromium CDP, loopback inside the container (Chromium's default binding; `--remote-allow-origins=http://127.0.0.1:9222`) | not published | not published |
| 9223 | socat forward of 9222, bound `0.0.0.0` | `127.0.0.1:9223` | not published (reach it through the daemon's `/v1/cdp/*`) |
| 5900 | x11vnc, **no password** | not published | not published |
| 6080 | websockify / noVNC | `127.0.0.1:6080` | not published |
| 6090 | clipboard bridge WebSocket | `127.0.0.1:6090` | not published |
| 8443 | `EXPOSE`d for code-server; nothing listens unless you start it | not published | not published |

**None of these endpoints authenticate.** CDP is arbitrary code execution plus
local file read; noVNC is the whole desktop. The only protections are the
loopback-only publish in the local compose and non-publication (plus the
bearer-authenticated daemon) in the remote compose. Never publish them on a
network.

### Mounts and persistence

This repo's `docker-compose.yml` mounts exactly one volume:

```yaml
volumes:
  - ${WORKSPACE_DIR:-./workspace}:/workspace
```

Nothing else is bind-mounted — in particular **`/root` is not** — so the browser
profile (`/root/.browser_profiles/default`), the XFCE config, shell history and
the package manifests live in the container's writable layer: they survive
`docker compose restart` but are lost when the container is recreated
(`docker compose up --force-recreate`, `docker compose down`).

The Prax harness's own compose (`prax/docker-compose.yml`, `sandbox` service) adds
the user-scoped mounts the Dockerfile comment refers to:

```yaml
- ${WORKSPACE_DIR:-../workspaces}/${PRAX_USER_ID}:/workspace
- .:/source                                                            # the harness repo, rw
- ${WORKSPACE_DIR:-../workspaces}/${PRAX_USER_ID}/.sandbox/home:/root  # persistent home
- ${WORKSPACE_DIR:-../workspaces}/${PRAX_USER_ID}/.sandbox/claude:/root/.claude
- ${WORKSPACE_DIR:-../workspaces}/${PRAX_USER_ID}/.sandbox/codex:/root/.codex
- ${WORKSPACE_DIR:-../workspaces}/${PRAX_USER_ID}/.sandbox/opencode:/root/.config/opencode
```

With that compose, `/workspace` is one user's workspace root (`PRAX_USER_ID`
selects whose) and `.sandbox/home` inside it is the persistent `/root`. The last
three sub-mounts were for the coding-agent CLIs and are leftovers — the CLIs are
no longer in the image. That same `sandbox` service also sets
`ANTHROPIC_API_KEY=${ANTHROPIC_KEY}` and `OPENAI_API_KEY=${OPENAI_KEY}` in its
`environment:` block — the Known gap noted at the top of this page.

### Package manifests

`install_package` appends to `/root/.installed_packages`; `run_shell` additionally
detects `apt(-get) install`, `pip(3) install` and `npm install -g` commands that
exit 0 and appends their package names to `/root/.installed_packages`,
`/root/.installed_pip_packages` and `/root/.installed_npm_packages` (best effort,
`control_plane._track_installed_packages`).

These manifests are **not auto-reinstalled** on rebuild — deliberately (a bad
package could break the desktop in a loop). On boot the entrypoint only prints
them ("Package manifests found in /root/ — review and add to Dockerfile for
persistence"). To make a package permanent, add it to `sandbox/Dockerfile`
(`rebuild_sandbox(dockerfile_content=…)` does that from the harness side).

### Terminal sessions

`SHELL=/bin/bash`; there is **no tmux wrapper** (the Dockerfile comment about
"tmux-shell" directly above `ENV SHELL=/bin/bash` is stale). Terminal persistence
across WebSocket reconnects is owned by the consuming UI (TeamWork's terminal
router), helped by `sandbox/bash-respawn.sh`, which re-spawns `bash -l` in the
same PTY when the user types `exit`. `tmux` is installed for anyone who wants to
run it by hand.

### GPU access (NVIDIA, optional)

The sandbox is CPU-only by default. The Prax harness ships a compose override
(`prax/docker-compose.gpu.yml`) and a `make sandbox-gpu` target (preflight check +
`nvidia-smi` smoke test) — both run from the Prax checkout:

```bash
make sandbox-gpu                                                       # one-shot
echo 'COMPOSE_FILE=docker-compose.yml:docker-compose.gpu.yml' >> .env  # persist for future compose commands
```

Requires `nvidia-container-toolkit` on the host (verify with `docker info | grep -i nvidia`).
The override reserves all GPUs for the sandbox and sets `NVIDIA_VISIBLE_DEVICES=all`
+ `NVIDIA_DRIVER_CAPABILITIES=compute,utility` so the toolkit injects the matching
CUDA libraries at runtime — no CUDA install in the image. Pin to specific cards by
changing `count: all` to `device_ids: ["0", "2"]` in the override. This repo's own
compose has no GPU override yet (see the top-level README roadmap).

### Container security posture

**Known gap (2026-09):** the container is not hardened beyond network
non-exposure.

- Everything runs as **root**: no `USER` in the Dockerfile, `supervisord.conf`
  sets `user=root`, and `docker exec` from the control plane runs as that user.
- Chromium runs with `--no-sandbox` (`sandbox/chromium-launch.sh`), as does
  mermaid's puppeteer (`/etc/puppeteer-config.json`).
- Neither `docker-compose.yml` nor `docker-compose.remote.yml` sets `mem_limit`,
  `pids_limit`, `cap_drop`, `security_opt`, `read_only` or a sized `tmpfs` for
  `/tmp` — a runaway process can fill the host disk (the container overlay *is*
  the host disk).
- `x11vnc` runs `-nopw`; noVNC, the clipboard bridge and the CDP forwarder have
  no auth of their own.
- One container is shared by every user of the harness (no per-tenant isolation).
- What *does* hold: the docker socket is **not** mounted into the sandbox
  container by either compose file; the local compose publishes only on
  `127.0.0.1`; the remote compose publishes nothing from the sandbox and fronts
  it with the bearer-authenticated daemon; the image carries no model API key.

### Harness-side tools (Prax reference integration)

The tools below live in the Prax repo, not here; they are listed because they are
the usual way the sandbox above gets driven.

**Desktop interaction** — Prax has 6 tools for computer-use, programmatic control of the sandbox's graphical desktop via `xdotool` and `scrot`:

| Tool | What It Does |
|------|-------------|
| `desktop_screenshot` | Capture the current desktop as a PNG. Returns the file path. |
| `desktop_click` | Click at (x, y) coordinates. Supports left/right/middle button and double-click. |
| `desktop_type` | Type text via simulated keystrokes with configurable delay. |
| `desktop_key` | Press key combinations (e.g., `ctrl+s`, `alt+F4`, `Return`, `Tab`). |
| `desktop_list_windows` | List all open windows with their titles and positions. |
| `desktop_open` | Launch a GUI application in the background on DISPLAY :99. |

These tools let Prax interact with any GUI application — Chromium, xterm, or anything installed via `sandbox_install`. The typical pattern is a **screenshot-analyze-act loop**: take a screenshot, analyze what's on screen, click or type to interact, then screenshot again to verify the result. See [Desktop](desktop.md) for the VNC desktop architecture and computer-use patterns.

**File sharing:** When the sandbox produces large files (videos, PDFs), Prax can publish them with `workspace_share_file()` to generate a public ngrok URL — but only on explicit user request, and typically only for SMS or Discord recipients (TeamWork users should be pointed at the file in their workspace browser instead). Each share is registered in `workspaces/{user}/.shares.json` with a randomized token and survives restarts. Use `workspace_list_shares()` to enumerate active shares and `workspace_unshare_file(token)` to revoke.

> **Security note:** Ngrok URLs are publicly reachable — anyone with the link can download the file. Shared file URLs are protected by two layers of randomization: a 32-character hex token in the path and a UUID-randomized filename (only the file extension is preserved). This makes URLs unguessable and reveals nothing about the original file name or contents. Still, treat shared links as semi-public: share them only with intended recipients, and revoke them with `workspace_unshare_file()` when no longer needed.

### History

**Original design (2026-06): Docker + OpenCode.** The sandbox was first built
around [OpenCode](https://opencode.ai/), an open-source coding agent with a
headless HTTP server mode (`opencode serve`), and the alternatives were weighed as
follows:

| Option | Verdict |
|--------|---------|
| **NVIDIA OpenShell** | Wraps the agent (security sandbox), doesn't provide code execution as a tool. Wrong direction of control. |
| **E2B** | Cloud-only, pay-per-second, no self-hosting. Good API but sends user data to third party. |
| **Daytona** | Self-hostable, 90ms sandbox creation, built-in Git/LSP/MCP. Strong runner-up — upgrade path if Docker management gets unwieldy. |
| **Docker SDK + custom sub-agent** | Full control but requires building everything OpenCode already has. |
| **Docker SDK + OpenCode** | **Selected at the time.** Best balance of capability, simplicity, and self-hosting. |

**Removed 2026-07 (#4, #5):** the OpenCode server and the coding-session
lifecycle (interactive feedback loop, mid-session model switching, round budgets
via `SANDBOX_MAX_ROUNDS`, auto-abort after 3 consecutive timeouts), solution
archiving to the workspace git with a `SOLUTION.md` and re-execution from the
archive, the `sandbox/opencode.json` seed, the Claude Code / Codex / OpenCode
CLIs, and model API keys in the container env. The harness now codes with its own
tools and the sandbox only executes. The `SandboxSession` / `RemoteSession` types
are still exported by `prax_sandbox_client` for compatibility, but nothing
produces them.
