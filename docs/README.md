# prax-sandbox docs

How the sandbox works, and how to deploy it.

- [**Sandbox Code Execution**](sandbox.md) — the container image, the exec control plane, ports, mounts, package manifests, security posture
- [**Desktop Environment**](desktop.md) — Xvfb + XFCE (xfwm4 / xfce4-panel / xfdesktop) + x11vnc + noVNC, the computer-use backing
- [**Browser Automation**](browser.md) — the (non-headless) Chromium + CDP the sandbox serves
- [**Remote daemon**](remote.md) — run the sandbox on a remote box, driven over TLS + bearer

A harness drives all of this through the `prax_sandbox_client` package
(`SandboxClient` / `SandboxConfig`) — see the top-level [README](../README.md).
The agent-facing tools that *use* the sandbox (delegation, `run_python`, the
browser/desktop tools) live in the consuming harness, not here.

History: until 2026-07 the sandbox also ran AI coding-agent CLIs and an OpenCode
session server on `:4096`. Those were removed (#4, #5); `sandbox.md` and
`desktop.md` were regenerated from the image sources on 2026-09-06 and keep only
dated history notes about that design; `browser.md` and `remote.md` were
corrected in place.
