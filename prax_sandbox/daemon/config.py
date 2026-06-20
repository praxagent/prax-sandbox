"""Daemon configuration — separate from the client's SandboxConfig.

Built from environment variables (``PRAX_SANDBOX_DAEMON_*``) at startup. Holds
the server-side secrets the client never sees: the bearer token clients present,
and a SEPARATE internal OpenCode password (so OpenCode auth is pure
defense-in-depth and port 4096 is never the client-facing surface).
"""
from __future__ import annotations

import os
from dataclasses import dataclass

from prax_sandbox_client.config import SandboxConfig


def _bool(v: str | None, default: bool = False) -> bool:
    if v is None:
        return default
    return v.strip().lower() in {"1", "true", "yes", "on"}


@dataclass
class DaemonConfig:
    # --- bind / TLS ---
    bind_host: str = "127.0.0.1"
    port: int = 8843
    tls_cert: str | None = None
    tls_key: str | None = None
    mtls_ca: str | None = None              # opt-in client-cert verification

    # --- auth ---
    bearer_token: str | None = None         # MANDATORY; daemon refuses to start without it

    # --- sandbox wiring ---
    opencode_host: str = "localhost"        # where the daemon reaches OpenCode :4096
    opencode_password: str | None = None    # internal, != bearer_token; never client-facing
    cdp_host: str = "127.0.0.1"             # where the daemon reaches Chrome's CDP
    cdp_port: int = 9222                     # 9222 if co-located; "sandbox":9223 if containerized
    container_label: str = "com.docker.compose.service=sandbox"
    workspace_dir: str = "/workspace"
    image: str = "prax-sandbox:latest"
    default_model: str = "openai/gpt-5.4"
    anthropic_key: str | None = None
    openai_key: str | None = None

    # --- limits / policy ---
    max_concurrent_exec: int = 8
    max_payload_bytes: int = 100 * 1024 * 1024
    request_timeout: int = 600
    # Rebuilding the sandbox image runs `docker build` as root — dangerous for a
    # multi-tenant remote daemon. Off by default; enable only for a trusted box.
    allow_rebuild: bool = False

    @classmethod
    def from_env(cls, env: dict | None = None) -> DaemonConfig:
        e = env if env is not None else os.environ
        token = e.get("PRAX_SANDBOX_DAEMON_TOKEN")
        token_file = e.get("PRAX_SANDBOX_DAEMON_TOKEN_FILE")
        if not token and token_file and os.path.isfile(token_file):
            with open(token_file) as f:
                token = f.read().strip()
        return cls(
            bind_host=e.get("PRAX_SANDBOX_DAEMON_HOST", "127.0.0.1"),
            port=int(e.get("PRAX_SANDBOX_DAEMON_PORT", "8843")),
            tls_cert=e.get("PRAX_SANDBOX_DAEMON_TLS_CERT") or None,
            tls_key=e.get("PRAX_SANDBOX_DAEMON_TLS_KEY") or None,
            mtls_ca=e.get("PRAX_SANDBOX_DAEMON_MTLS_CA") or None,
            bearer_token=token or None,
            opencode_host=e.get("PRAX_SANDBOX_OPENCODE_HOST", "localhost"),
            opencode_password=e.get("PRAX_SANDBOX_OPENCODE_PASSWORD") or None,
            cdp_host=e.get("PRAX_SANDBOX_CDP_HOST", "127.0.0.1"),
            cdp_port=int(e.get("PRAX_SANDBOX_CDP_PORT", "9222")),
            container_label=e.get("PRAX_SANDBOX_CONTAINER_LABEL", "com.docker.compose.service=sandbox"),
            workspace_dir=e.get("PRAX_SANDBOX_WORKSPACE_DIR", "/workspace"),
            image=e.get("SANDBOX_IMAGE", "prax-sandbox:latest"),
            default_model=e.get("SANDBOX_DEFAULT_MODEL", "openai/gpt-5.4"),
            anthropic_key=e.get("ANTHROPIC_API_KEY") or None,
            openai_key=e.get("OPENAI_API_KEY") or None,
            max_concurrent_exec=int(e.get("PRAX_SANDBOX_MAX_CONCURRENT_EXEC", "8")),
            max_payload_bytes=int(e.get("PRAX_SANDBOX_MAX_PAYLOAD_BYTES", str(100 * 1024 * 1024))),
            request_timeout=int(e.get("PRAX_SANDBOX_REQUEST_TIMEOUT", "600")),
            allow_rebuild=_bool(e.get("PRAX_SANDBOX_ALLOW_REBUILD"), False),
        )

    def to_sandbox_config(self) -> SandboxConfig:
        """Build the control-plane config the daemon drives in-process."""
        return SandboxConfig(
            host=self.opencode_host,
            image=self.image,
            persistent=True,
            workspace_dir=self.workspace_dir,
            container_label=self.container_label,
            default_model=self.default_model,
            anthropic_key=self.anthropic_key,
            openai_key=self.openai_key,
            opencode_password=self.opencode_password,
            # on_output/resolve_workspace/commit stay None -> control plane's
            # built-in inert defaults (the harness owns those callbacks).
        )

    def validate_or_die(self) -> None:
        """Fail-closed checks — call before binding."""
        if not self.bearer_token:
            raise SystemExit(
                "prax-sandbox-daemon: refusing to start without a bearer token. "
                "Set PRAX_SANDBOX_DAEMON_TOKEN or PRAX_SANDBOX_DAEMON_TOKEN_FILE."
            )
        non_loopback = self.bind_host not in {"127.0.0.1", "::1", "localhost"}
        has_tls = bool(self.tls_cert and self.tls_key)
        if non_loopback and not has_tls:
            raise SystemExit(
                f"prax-sandbox-daemon: refusing to bind {self.bind_host} in plaintext "
                "(the bearer token would be exposed). Provide TLS_CERT/TLS_KEY, or bind "
                "127.0.0.1 behind a TLS-terminating reverse proxy / tailscale serve."
            )
