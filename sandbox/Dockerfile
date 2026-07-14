FROM debian:trixie-slim

ENV DEBIAN_FRONTEND=noninteractive

# ──────────────────────────────────────────────────────────────────────
# IMPORTANT: do NOT install user-facing tooling under /root/...
#
# At runtime, docker-compose.yml bind-mounts the user's empty
# `${WORKSPACE_DIR}/${PRAX_USER_ID}/.sandbox/home` over /root, which
# hides everything baked into /root at build time.  uv was previously
# installed to /root/.local/bin/uv with a symlink in /usr/local/bin —
# the symlink ended up dangling on every fresh container start.
#
# Install long-lived binaries to /usr/local/bin/ or /opt/ instead.
# Caches (apt, pip, npm, uv) under /root are fine to lose — they'll
# rebuild on demand.  The entrypoint scans for dangling PATH symlinks
# at startup and logs a warning if anything regresses.
# ──────────────────────────────────────────────────────────────────────

# ── System packages ──
RUN apt-get update -qq && apt-get install -y --no-install-recommends \
    python3 python3-pip python3-venv \
    nodejs npm \
    texlive-latex-base texlive-latex-extra texlive-latex-recommended \
    texlive-fonts-recommended texlive-fonts-extra \
    texlive-science texlive-bibtex-extra texlive-publishers \
    lmodern cm-super latexmk biber \
    ffmpeg poppler-utils pandoc hugo \
    git curl wget jq tmux \
    socat \
    supervisor \
    xvfb x11vnc xfce4 xterm dbus-x11 \
    novnc websockify \
    xdotool scrot imagemagick psmisc \
    xsel python3-websockets \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# ── Chromium — native Debian package, arm64 + amd64, no snap ──
RUN apt-get update -qq \
    && apt-get install -y --no-install-recommends chromium \
    && rm -rf /var/lib/apt/lists/* \
    && ln -sf /usr/bin/chromium /usr/bin/chromium-browser

# Debian's /etc/chromium.d/extensions injects --load-extension= with an
# empty value (the `ls -dm /usr/share/chromium/extensions/*` glob expands
# to nothing on a fresh install).  In Chromium 147 that empty flag runs
# before our own --load-extension=/opt/prax-cast-ext and prevents the
# MV3 service worker from registering — the offscreen document loads
# but background.js never runs, so chrome.runtime.sendMessage fails with
# "Receiving end does not exist".  Keep --enable-remote-extensions,
# drop the broken load-extension injection.
RUN echo 'export CHROMIUM_FLAGS="$CHROMIUM_FLAGS --enable-remote-extensions"' \
      > /etc/chromium.d/extensions

# ── code-server (web-based VS Code, works on amd64 + arm64) ──
RUN curl -fsSL https://code-server.dev/install.sh | sh

# ── Coding agents ──
RUN npm install -g opencode-ai @anthropic-ai/claude-code @openai/codex \
    @mermaid-js/mermaid-cli

# Persistence + `exit` resilience now live in TeamWork's terminal
# router (server-side session that outlives the WS) plus
# bash-respawn.sh.  No tmux involvement in the auto-shell — tmux owns
# the screen which broke native xterm.js scrolling and complicated
# resize.  tmux is still apt-installed for users who want to invoke
# it manually.

# ── uv (copied direct to /usr/local/bin so the /root volume mount can't
#       wipe it — see prior incident: installer wrote to /root/.local/bin
#       which the workspace volume hides on container start).
COPY --from=ghcr.io/astral-sh/uv@sha256:e49fde5daf002023f0a2e2643861ce9ca8a8da5b73d0e6db83ef82ff99969baf /uv /uvx /usr/local/bin/

# ── Persistent scratch Python venv ──
# Keep ad-hoc Python deps out of Debian's externally-managed system Python.
# The entrypoint puts this venv on PATH; Prax can use it for temporary audio
# transcription without writing artifacts into the user's workspace.
RUN uv venv /opt/prax-venv --python python3 \
    && uv pip install --python /opt/prax-venv/bin/python faster-whisper

# ── Lean 4 toolchain (elan → lean/lake) for the lean_check proof tool ──
# Installed to /opt/elan, NOT the elan default ~/.elan — /root is volume-mounted
# at runtime and would hide it (same reason as uv above). ELAN_HOME is baked as
# an ENV so `docker exec` shells inherit it and the lean/lake proxies (symlinked
# into /usr/local/bin) resolve the toolchain. Pinned to the Lean version cdc-lean
# uses, so that repo stays rebuildable. mathlib is deliberately NOT fetched here
# (multi-GB, needs a lake project) — this is the toolchain-only scope: lean_check
# handles Lean-core proofs (arithmetic, logic, rfl). See prax's
# docs/research/cdc-lean-teach-prax-lean.md.
ENV ELAN_HOME=/opt/elan
RUN curl -fsSL https://elan.lean-lang.org/elan-init.sh -o /tmp/elan-init.sh \
    && sh /tmp/elan-init.sh -y --default-toolchain leanprover/lean4:v4.31.0 \
    && rm -f /tmp/elan-init.sh \
    && ln -sf /opt/elan/bin/* /usr/local/bin/ \
    && lean --version

WORKDIR /workspace

# Seed config — copied to /opt/ so it survives volume mounts.
# The entrypoint seeds /root/.config/opencode/ on first run.
COPY opencode.json /opt/opencode.json

COPY entrypoint.sh /usr/local/bin/entrypoint.sh
COPY bash-respawn.sh /usr/local/bin/bash-respawn.sh
COPY chromium-launch.sh /usr/local/bin/chromium-launch.sh
COPY clipboard-bridge.py /usr/local/bin/clipboard-bridge.py
COPY supervisord.conf /etc/supervisor/supervisord.conf
COPY cast-ext/ /opt/prax-cast-ext/
RUN chmod +x /usr/local/bin/entrypoint.sh /usr/local/bin/bash-respawn.sh /usr/local/bin/chromium-launch.sh
# tmux is still apt-installed above for opt-in user use (`tmux` from a
# bash prompt just works).  We just don't auto-wrap the shell in it
# anymore — that broke native xterm.js scrolling, resize behavior, and
# was a constant source of "why is the terminal weird" tickets.

# Set tmux-shell as the default shell so terminal WebSocket connections
# attach to the persistent tmux session instead of spawning fresh shells.
ENV SHELL=/bin/bash
# DISPLAY for the virtual framebuffer — baked into the image so docker exec
# commands (sandbox_shell, terminal sessions) can interact with the desktop.
ENV DISPLAY=:99

# 4096 = OpenCode serve, 6080 = noVNC web desktop, 6090 = clipboard bridge, 8443 = code-server.
# 9223 (raw CDP) is intentionally NOT exposed — reach Chrome only via the
# authenticated daemon proxy (remote) or the internal docker network (local).
EXPOSE 4096 6080 6090 8443

HEALTHCHECK --interval=3s --timeout=2s --start-period=15s --retries=5 \
    CMD curl -sf http://localhost:4096/global/health > /dev/null || exit 1

CMD ["/usr/local/bin/entrypoint.sh"]
