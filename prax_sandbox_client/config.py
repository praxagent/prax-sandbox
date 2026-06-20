"""Configuration contract for the Prax sandbox control plane.

``SandboxConfig`` is the harness-agnostic configuration a host passes to the
sandbox control plane. It carries no dependency on any particular harness — any
harness can build one. The callback slots let the host inject side effects
(live-output streaming, workspace persistence) so the control plane never has to
import the harness.

Keep this type free of harness-specific imports.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field


@dataclass
class SandboxConfig:
    """Settings + injected callbacks for driving a sandbox.

    The control plane reads these instead of importing the harness's settings
    object. Defaults mirror the persistent docker-compose deployment.
    """

    # --- Connection / lifecycle ---
    host: str = "sandbox"                 # network host of the persistent sandbox
    image: str = "prax-sandbox:latest"    # image name (used by rebuild)
    persistent: bool = True               # always-on container (the only mode)
    workspace_dir: str = "./workspaces"
    # Docker filter used to locate the running container (exec / install /
    # rebuild). Defaults to the docker-compose service label; a docker-run or
    # remote deployment overrides this with e.g. ``name=<container>``.
    container_label: str = "com.docker.compose.service=sandbox"

    # --- Session policy ---
    default_model: str = "openai/gpt-5.4"
    max_concurrent: int = 5
    max_rounds: int = 10
    timeout: int = 1800

    # --- Container layout (consumed by the control daemon in the carve phase) ---
    source_mount: str = "/source"         # full-repo rw mount for self-improvement
    scratch_venv: str = "/opt/prax-venv"  # run_python venv inside the sandbox

    # --- Provider credentials (forwarded into the container env) ---
    anthropic_key: str | None = field(default=None, repr=False)
    openai_key: str | None = field(default=None, repr=False)

    # --- Auth (None = no auth today; the remote daemon sets a token here) ---
    opencode_password: str | None = field(default=None, repr=False)

    # --- Remote transport (None/empty daemon_url = in-process, the default) ---
    # When daemon_url is set, the client talks to a remote control daemon over
    # HTTP(S) instead of driving the control plane in-process. This is the SOLE
    # transport switch — empty means local-first, no network, no Tailscale.
    daemon_url: str | None = None
    daemon_token: str | None = field(default=None, repr=False)  # mandatory bearer when remote
    daemon_timeout: int = 30           # per-request connect+read timeout (long methods override)
    tls_verify: bool | str = True      # True = system trust; or a path to a CA bundle
    client_cert: str | None = None     # opt-in mTLS, layered ON TOP of the bearer
    client_key: str | None = None

    # --- Injected host callbacks (None falls back to a no-op) ---
    # (label, text) -> None : stream incremental coding-agent output to the UI.
    on_output: Callable[[str, str], None] | None = None
    # (user_id) -> workspace_root : ensure + return the user's workspace root.
    resolve_workspace: Callable[[str], str] | None = None
    # (root, message) -> None : persist/commit the workspace after archiving.
    commit: Callable[[str, str], None] | None = None
