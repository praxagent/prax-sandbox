"""Sandbox code-execution service — Docker exec primitives.

The sandbox is an always-on (persistent) container running 24/7 alongside the
app. This module is the control plane for **execution**: run a shell command or
an argv in the container, install packages, probe liveness, and rebuild the
image. Path translation between the host's workspace layout and the sandbox
mount stays on the harness side.

History: this module also drove OpenCode *coding sessions* (start/send/review/
finish/abort + a solutions archive) over the OpenCode HTTP API on :4096. Those
were REMOVED (2026-07) — the sandbox is a pure execution environment and the
harness codes with its own tools; the image no longer ships a coding-agent
server or any model key. See prax `docs/security/sandbox-execution-boundary.md`.
"""
from __future__ import annotations

import logging
import os
import re
import subprocess
import time

from prax_sandbox.exec import exec_in_sandbox, find_sandbox_container
from prax_sandbox_client.config import SandboxConfig

logger = logging.getLogger(__name__)

_config: SandboxConfig | None = None


def configure(config: SandboxConfig) -> None:
    """Set the active sandbox configuration (called by the harness/client)."""
    global _config
    _config = config


def _cfg() -> SandboxConfig:
    """Return the active config, falling back to built-in defaults."""
    global _config
    if _config is None:
        _config = SandboxConfig()
    return _config


def _builtin_resolve_workspace(user_id: str) -> str:
    """Default workspace resolver — a local per-user dir under workspace_dir.

    The harness overrides this via ``SandboxConfig.resolve_workspace`` to point
    at its own (e.g. git-backed) workspace.
    """
    root = os.path.join(os.path.abspath(_cfg().workspace_dir), user_id.lstrip("+"))
    os.makedirs(root, exist_ok=True)
    return root


def _noop_commit(root: str, message: str) -> None:
    """Default commit hook — no-op (the harness injects git via the config)."""
    return


# ---------------------------------------------------------------------------
# Lazy Docker import — not every deployment needs Docker
# ---------------------------------------------------------------------------
_docker_client = None


def _get_docker():
    """Return the ``docker`` module, importing lazily."""
    import docker
    return docker


def _get_docker_client():
    global _docker_client
    if _docker_client is None:
        _docker_client = _get_docker().from_env()
    return _docker_client


# ---------------------------------------------------------------------------
# Package installation
# ---------------------------------------------------------------------------
def install_package(package_name: str) -> dict:
    """Install a system package in the sandbox container.

    Only works in persistent (docker-compose) mode. In local mode, returns
    an error with instructions for the user to install manually.
    """
    if not _cfg().persistent:
        return {
            "error": (
                "Cannot auto-install packages in local mode. "
                f"The user needs to install '{package_name}' on their system."
            ),
            "local_install_hints": {
                "macOS": f"brew install {package_name}",
                "Ubuntu": f"sudo apt-get install {package_name}",
            },
        }

    # Sanitize package name — alphanumeric, dots, hyphens, plus, colons only.
    if not re.match(r"^[a-zA-Z0-9][a-zA-Z0-9._+:-]*$", package_name):
        return {"error": f"Invalid package name: {package_name}"}

    try:
        container = find_sandbox_container(_cfg())
        exit_code, output = container.exec_run(
            ["sh", "-c", f"apt-get update -qq && apt-get install -y --no-install-recommends {package_name}"],
            demux=True,
        )
        stdout = (output[0] or b"").decode(errors="replace")
        stderr = (output[1] or b"").decode(errors="replace")
        if exit_code != 0:
            return {"error": f"apt-get failed (exit {exit_code}): {stderr[-500:]}"}
        # Recorded for rebuilds by the image's dpkg hook
        # (/workspace/.sandbox/installed-apt.txt), whoever ran apt.
        return {"installed": package_name, "output": stdout[-300:]}
    except Exception as e:
        return {"error": str(e)}


# Runtime install records live on the workspace mount, so they survive the
# container being recreated, which is when the packages themselves are lost.
# apt is recorded by the image's dpkg hook (sandbox/record-apt-packages.sh),
# which sees every install, not just the ones that pass through here.
MANIFEST_DIR = "/workspace/.sandbox"


def _record(container, filename: str, packages: list[str]) -> None:
    for pkg in packages:
        container.exec_run([
            "sh", "-c", 'mkdir -p "$1" && echo "$2" >> "$1/$3"',
            "sh", MANIFEST_DIR, pkg, filename,
        ])


def _track_installed_packages(command: str, exit_code: int) -> None:
    """Best-effort: record pip and npm global installs run via sandbox_shell /
    run_python in ``/workspace/.sandbox/installed-{pip,npm}.txt``."""
    if exit_code != 0:
        return
    try:
        container = find_sandbox_container(_cfg())

        # pip install / pip3 install
        m = re.search(r"pip3?\s+install\s+(?:-\S+\s+)*(.+)", command)
        if m:
            pkgs = re.findall(r"[a-zA-Z0-9][a-zA-Z0-9._-]+", m.group(1))
            pkgs = [p for p in pkgs if not p.startswith("-") and "/" not in p]
            _record(container, "installed-pip.txt", pkgs)
            return

        # npm install -g
        m = re.search(r"npm\s+(?:install|i)\s+(?:.*-g|.*--global)\s*(.+)", command)
        if not m:
            m = re.search(r"npm\s+(?:install|i)\s+(.+?)(?:\s+-g|\s+--global)", command)
        if m:
            pkgs = re.findall(r"[a-zA-Z0-9@][a-zA-Z0-9._/@-]+", m.group(1))
            pkgs = [p for p in pkgs if not p.startswith("-")]
            _record(container, "installed-npm.txt", pkgs)
    except Exception:
        pass  # best-effort


# ---------------------------------------------------------------------------
# Execution primitives
# ---------------------------------------------------------------------------
def run_shell(command: str, timeout: int = 60) -> dict:
    """Run a shell command directly in the sandbox container.

    Use for any shell command: ``ls``, ``df -h``, ``python3 …``, ``git`` …
    Returns dict with ``stdout``, ``stderr``, ``exit_code``.
    """
    try:
        result = exec_in_sandbox(["sh", "-c", command], timeout=timeout, config=_cfg())
        # Track install commands for rebuild reproducibility
        _track_installed_packages(command, result.returncode)
        return {
            "stdout": (result.stdout or "")[:10000],
            "stderr": (result.stderr or "")[:5000],
            "exit_code": result.returncode,
        }
    except Exception as e:
        return {"error": str(e), "exit_code": -1}


def run_command(
    cmd: list[str],
    *,
    cwd: str | None = None,
    env: dict[str, str] | None = None,
    timeout: int = 300,
) -> subprocess.CompletedProcess:
    """Run a command (already path-translated by the caller) in the sandbox.

    Returns a :class:`subprocess.CompletedProcess`. This is the exec primitive
    the harness's shell helper delegates to; path translation between the host's
    workspace layout and the sandbox mount stays on the harness side.
    """
    return exec_in_sandbox(cmd, cwd=cwd, env=env, timeout=timeout, config=_cfg())


# ---------------------------------------------------------------------------
# Liveness + lifecycle
# ---------------------------------------------------------------------------
def _container_ready(timeout: float = 3.0) -> bool:
    """True if the container is up and can execute a trivial command.

    Replaces the old OpenCode health probe (curling :4096) — there is no
    coding-agent server anymore; liveness = 'can I docker-exec into it'.
    """
    deadline = time.time() + max(0.0, timeout)
    while True:
        try:
            r = exec_in_sandbox(["true"], timeout=5, config=_cfg())
            if r.returncode == 0:
                return True
        except Exception:
            pass
        if time.time() >= deadline:
            return False
        time.sleep(0.5)


def get_runtime_mode() -> str:
    """Return a human-readable description of the sandbox mode."""
    return "docker (persistent sandbox — can auto-install packages)"


def health() -> bool:
    """Return True if the persistent sandbox container is reachable/executable."""
    return _container_ready(timeout=3)


def restart_sandbox(ready_timeout: float = 60.0) -> dict:
    """Restart the sandbox container — for when the desktop, a terminal or the
    browser is stuck.

    ``docker restart`` keeps the container: files, installed packages and the
    browser profile survive; running programs stop. Waits until the container
    can run a command again (``ready``).
    """
    try:
        container = find_sandbox_container(_cfg())
    except Exception as e:
        return {"error": f"No sandbox container to restart: {e}"}
    try:
        container.restart(timeout=10)
    except Exception as e:
        return {"error": f"Restart failed: {e}"}
    return {"restarted": getattr(container, "name", "sandbox"),
            "ready": _container_ready(timeout=ready_timeout)}


def rebuild_sandbox(dockerfile_content: str | None = None) -> dict:
    """Rebuild the sandbox Docker image and restart the container.

    Only works in persistent (docker-compose) mode. If *dockerfile_content*
    is provided, it overwrites ``sandbox/Dockerfile`` before building.
    """
    if not _cfg().persistent:
        return {"error": "Sandbox rebuild is only available in Docker deployment mode."}

    try:
        _get_docker_client()
    except Exception as e:
        return {"error": f"Docker not available: {e}"}

    # __file__ is prax_sandbox/control_plane.py, so two dirnames up is the repo
    # root that holds sandbox/.
    sandbox_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), "sandbox")
    if dockerfile_content:
        dockerfile_path = os.path.join(sandbox_dir, "Dockerfile")
        if not os.path.isfile(dockerfile_path):
            return {"error": f"Cannot find sandbox Dockerfile at {dockerfile_path}"}
        with open(dockerfile_path, "w") as f:
            f.write(dockerfile_content)

    # Build the image.
    try:
        result = subprocess.run(
            ["docker", "build", "-t", _cfg().image, sandbox_dir],
            capture_output=True, text=True, timeout=600,
        )
        if result.returncode != 0:
            return {"error": f"Docker build failed:\n{result.stderr[-1000:]}"}
    except subprocess.TimeoutExpired:
        return {"error": "Docker build timed out (10 min)."}

    # Find and restart the sandbox container.
    try:
        container = find_sandbox_container(_cfg())
        container.restart(timeout=10)
    except Exception as e:
        return {"error": f"Failed to restart sandbox container: {e}"}

    # Wait for the sandbox to come back up (exec probe, not OpenCode).
    if not _container_ready(timeout=60):
        return {"error": "Sandbox rebuilt but failed to become executable within 60s."}

    return {"status": "rebuilt", "image": _cfg().image}
