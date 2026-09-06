# Desktop Environment

[← prax-sandbox docs](README.md)

> Part of **prax-sandbox**. The `desktop_*` agent tools that drive this desktop
> live in the consuming harness (e.g. Prax); here we document the desktop the
> sandbox provides.

> **Regenerated 2026-09-06** from `sandbox/Dockerfile`, `sandbox/supervisord.conf`
> and `sandbox/entrypoint.sh` (the previous text described a Fluxbox / VS Code /
> tmux image that no longer ships).

### Overview

The sandbox container runs a full Linux desktop environment — Xvfb virtual framebuffer, an XFCE session (xfwm4 window manager, xfce4-panel, xfdesktop), x11vnc VNC server, and noVNC web client. Users access it through TeamWork's Desktop tab. Prax controls it programmatically via 6 `desktop_*` tools backed by xdotool and scrot.

This gives Prax true computer-use capability. Anything a human can do on a desktop — open an IDE, click through a GUI installer, fill out a web form, use a drawing tool — Prax can do by taking screenshots, analyzing them, and issuing mouse/keyboard commands.

### Architecture

```
Xvfb :99 (1920x1080x24)            Virtual framebuffer — no physical display needed
  ↓
dbus session bus                   unix:path=/run/dbus-session.sock — XFCE needs it
  ↓
xfwm4 + xfce4-panel + xfdesktop    XFCE window manager, panel, desktop
  ↓
x11vnc :5900                       VNC server — exposes the framebuffer as a VNC stream (-nopw -shared)
  ↓
websockify :6080                   WebSocket bridge — translates VNC protocol to WebSocket
  ↓
noVNC (web client)                 Browser-based VNC viewer — static HTML/JS served from /usr/share/novnc
  ↓
TeamWork proxy                     /api/desktop/websockify — proxies the WebSocket to the user's browser
```

All of these are supervisord programs (`sandbox/supervisord.conf`, priorities 10-40) with `autorestart=true` and `startretries=10`. `sandbox/entrypoint.sh` clears stale `:99` X locks, seeds the XFCE config on first boot, and hands off to supervisord as PID 1. There is no headless-only fallback: if Xvfb cannot start, supervisord keeps retrying it and eventually marks it FATAL — nothing switches Chromium to headless mode.

### Installed Software

The sandbox desktop comes with:

| Software | Purpose |
|----------|---------|
| **Chromium** | Web browser — same instance serves the Browser tab (CDP) and Desktop tab (VNC) |
| **code-server** (web VS Code) | Installed via code-server's install script; **not started automatically** — no supervisord program, and `8443` (its `EXPOSE`d port) is not published by either compose file |
| **xterm** | Terminal emulator (XFCE's configured `TerminalEmulator`) |
| **Python 3 + uv** | Python development; scratch venv at `/opt/prax-venv` (duckdb, pandas, sympy, faster-whisper) |
| **Node.js + npm** | JavaScript/TypeScript development (Debian trixie packages) |
| **Lean 4** | `lean` / `lake` via elan at `/opt/elan` |
| **ffmpeg** | Audio/video processing |
| **LaTeX** (TeX Live) | Document typesetting — `texlive-latex-base` / `-extra` / `-recommended`, `-fonts-recommended` / `-extra`, `-science`, `-bibtex-extra`, `-publishers`, `latexmk`, `biber` |
| **Hugo** | Static site generation (notes, courses) |
| **tmux** | Terminal multiplexer — installed for manual use; the default shell is plain bash |
| **pandoc** | Document conversion |
| **ImageMagick** | Image manipulation |
| **xdotool, scrot, xsel** | Input synthesis, screenshots, clipboard — what the desktop tools and the clipboard bridge use |
| **git, curl, wget, jq** | Standard dev tools |

Prax can install additional packages at runtime with `sandbox_install("package-name")`. Installed packages are tracked in `/root/.installed_packages` (apt), `/root/.installed_pip_packages` and `/root/.installed_npm_packages`; they are **not** auto-reinstalled on rebuild — the entrypoint prints the manifests at boot so proven packages can be promoted into `sandbox/Dockerfile` (see [Sandbox Code Execution](sandbox.md#package-manifests)).

### Prax's Desktop Tools

Six tools give Prax programmatic control of the desktop. All execute commands on DISPLAY :99 via `docker exec` into the sandbox container.

| Tool | Arguments | What It Does |
|------|-----------|-------------|
| `desktop_screenshot` | — | Captures the desktop as a PNG using `scrot`. Returns the file path. Prax reads the image to understand what's on screen. |
| `desktop_click` | `x`, `y`, `button`, `clicks` | Moves the mouse to (x, y) and clicks. Supports left/right/middle button and double-click. Uses `xdotool mousemove` + `click`. |
| `desktop_type` | `text`, `delay_ms` | Types text via simulated keystrokes. Configurable inter-key delay (default 12ms). Uses `xdotool type`. |
| `desktop_key` | `keys` | Presses key combinations. Uses xdotool syntax: `Return`, `ctrl+s`, `alt+F4`, `ctrl+shift+t`, `Tab`, `Escape`, `BackSpace`. |
| `desktop_list_windows` | — | Lists all open windows with their titles. Uses `xdotool search`. |
| `desktop_open` | `command` | Launches an application in the background. The command runs with `DISPLAY=:99` set. |

**Example — opening a terminal and running a command:**

```
1. desktop_open("xterm")                         → Launches xterm on the desktop
2. desktop_screenshot()                          → See the terminal window
3. desktop_click(500, 300)                       → Focus it
4. desktop_type("ls /workspace")                 → Type a command
5. desktop_key("Return")                         → Execute it
6. desktop_screenshot()                          → Verify the result
```

### Computer-Use Pattern

Prax interacts with GUI applications using a **screenshot → analyze → act → verify** loop:

```
┌─────────────────────────────────────────────┐
│  1. desktop_screenshot()                    │
│     → Capture current desktop state         │
│                                             │
│  2. Analyze the screenshot                  │
│     → Identify UI elements, buttons, text   │
│     → Determine coordinates for next action │
│                                             │
│  3. Act (click / type / key)                │
│     → desktop_click(x, y) or               │
│       desktop_type("text") or              │
│       desktop_key("ctrl+s")                │
│                                             │
│  4. desktop_screenshot()                    │
│     → Verify the action had the expected    │
│       effect. If not, adjust and retry.     │
└─────────────────────────────────────────────┘
```

This pattern works for any GUI application — IDEs, web browsers, file managers, terminal emulators, drawing tools. The key insight is that Prax uses vision (screenshot analysis) for understanding and xdotool for control, just like a human uses eyes and hands.

### Browser Unification

One Chromium instance serves **both** TeamWork tabs:

- **Browser tab** — live CDP screencast via `sandbox_browser_read` / `sandbox_browser_act` (Chrome DevTools Protocol on port 9222, forwarded to 9223 by socat)
- **Desktop tab** — noVNC view of the full desktop (VNC on port 5900, websockified on port 6080)

Same browser, two views. When the user logs into a site via the Desktop tab, the Browser tab reflects the same session. When Prax navigates via Playwright or CDP, the Desktop tab shows the same page. OAuth popups, CAPTCHAs, and multi-factor flows are visible in both views.

Chrome launches in **non-headless mode** (visible on the Xvfb display; `sandbox/chromium-launch.sh` passes no `--headless`) with remote debugging enabled. This is what makes both views possible — headless Chrome wouldn't appear on the desktop. A clipboard bridge (`sandbox/clipboard-bridge.py`, WebSocket on `6090`) syncs the X11 clipboard with the web client via `xsel`.

### Persistence

What persists depends on which compose file runs the container:

- **This repo's `docker-compose.yml`** mounts only `${WORKSPACE_DIR:-./workspace}:/workspace`. `/root` is *not* mounted, so everything below lives in the container's writable layer — it survives `docker compose restart` but is lost when the container is recreated.
- **The Prax harness's compose** (`prax/docker-compose.yml`) bind-mounts a per-user directory over `/root`:

  ```yaml
  - ${WORKSPACE_DIR:-../workspaces}/${PRAX_USER_ID}/.sandbox/home:/root
  ```

  With that mount, `/root` survives container rebuilds.

Under `/root` you will find:

- **Browser profile** (`/root/.browser_profiles/default`) — cookies, localStorage, login sessions (the entrypoint clears only Chromium's singleton locks and the extension's cached service worker on boot)
- **Desktop customizations** — XFCE config under `/root/.config/xfce4/` (seeded by the entrypoint on first run only), `.Xresources`
- **Package manifests** (`/root/.installed_packages`, `.installed_pip_packages`, `.installed_npm_packages`) — recorded, **not** auto-reinstalled
- **Shell history** — bash history
- **Downloads** — anything saved to `/root/`

The harness compose also creates `.sandbox/claude`, `.sandbox/codex` and `.sandbox/opencode` sub-mounts; those were for the coding-agent CLIs removed in 2026-07 and are leftovers (the CLIs are no longer in the image).

The workspace itself (`/workspace`) is persistent under both compose files — it's a directory on the host.

### Access and security

`x11vnc` runs with `-nopw` and noVNC / websockify add no authentication of their own; the clipboard bridge and the CDP forwarder are likewise unauthenticated. The only protections are where the ports are published: this repo's `docker-compose.yml` publishes `6080`, `6090` and `9223` on `127.0.0.1` only, and `docker-compose.remote.yml` publishes none of them (the remote daemon proxies CDP behind a bearer token; it does not proxy the desktop). Anyone who can reach `6080` has the whole desktop. `DISPLAY=:99` is baked into the image so `docker exec` commands can drive the display.

### User Interaction

Users interact with the desktop through the noVNC iframe in TeamWork's Desktop tab. They can:

- **Browse the web** — navigate Chromium, log into sites, handle CAPTCHAs
- **Use GUI applications** — open xterm, Chromium, or any installed GUI app
- **Watch Prax work** — see everything Prax does via the desktop tools in real-time
- **Take over** — click, type, and interact while Prax is working (shared display)
- **Install software** — use the terminal tab or ask Prax to install packages

The desktop is a shared workspace. Prax and the user operate on the same display, the same browser, and the same files.
