# prax-sandbox docs

How the sandbox works, and how to deploy it.

- [**Sandbox Code Execution**](sandbox.md) — the container, OpenCode, the image, sessions, mounts
- [**Desktop Environment**](desktop.md) — Xvfb + Fluxbox + x11vnc + noVNC, the computer-use backing
- [**Browser Automation**](browser.md) — the headless Chrome + CDP the sandbox serves
- [**Remote daemon**](remote.md) — run the sandbox on a remote box, driven over TLS + bearer

A harness drives all of this through the `prax_sandbox_client` package
(`SandboxClient` / `SandboxConfig`) — see the top-level [README](../README.md).
The agent-facing tools that *use* the sandbox (delegation, `run_python`, the
browser/desktop tools) live in the consuming harness, not here.
