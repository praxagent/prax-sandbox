"""prax-sandbox control plane — host-side driver for the sandbox container.

This package holds the privileged, docker-aware machinery:

- :mod:`prax_sandbox.control_plane` — session lifecycle + OpenCode HTTP client
- :mod:`prax_sandbox.exec` — ``docker exec`` into the sandbox container
- :mod:`prax_sandbox.cdp_service` — Chrome DevTools Protocol client

Harnesses drive it through :mod:`prax_sandbox_client` (the ``SandboxClient``
facade); they do not import this package's internals directly, except
``cdp_service`` for browser reads.
"""
