"""Run commands inside the persistent sandbox container via ``docker exec``.

Harness-free: the caller passes a :class:`SandboxConfig` (for container
discovery) and *already-translated* paths. Path translation between a host's
workspace layout and the sandbox's ``/workspace`` mount is the harness's concern
— the harness knows its own filesystem; this module only knows docker.
"""
from __future__ import annotations

import logging
import shlex
import subprocess
import time

from prax_sandbox_client.config import SandboxConfig

logger = logging.getLogger(__name__)

# coreutils `timeout`: seconds between TERM and KILL, and the exit status it
# returns when the deadline passed (a KILL after the grace period gives 137).
_KILL_GRACE_SECONDS = 5
_TIMED_OUT = 124
_KILLED = 137


def _get_docker_client():
    import docker
    return docker.from_env()


def _parse_filter(label: str) -> dict:
    """Turn a ``key=value`` selector into a docker ``filters`` dict."""
    if "=" in label and not label.startswith("label="):
        key, _, value = label.partition("=")
        if key in {"name", "id", "status"}:
            return {key: value}
    return {"label": label}


def find_sandbox_container(config: SandboxConfig | None = None):
    """Return the running sandbox container, or raise if not found."""
    label = (config.container_label if config else None) or "com.docker.compose.service=sandbox"
    client = _get_docker_client()
    containers = client.containers.list(filters=_parse_filter(label))
    if not containers:
        raise RuntimeError(
            "Sandbox container not running. Start it with: docker compose up sandbox"
        )
    return containers[0]


def exec_in_sandbox(
    cmd: list[str],
    *,
    cwd: str | None = None,
    env: dict[str, str] | None = None,
    timeout: int = 300,
    config: SandboxConfig | None = None,
) -> subprocess.CompletedProcess:
    """Execute *cmd* (already path-translated) inside the sandbox container.

    Returns a :class:`subprocess.CompletedProcess` so callers can treat it like
    a local ``subprocess.run`` result.

    ``docker exec`` has no deadline of its own. With
    ``config.enforce_exec_timeout`` the command runs under coreutils
    ``timeout``, which signals the command's whole process group — so a
    runaway child (the unbounded ffmpeg of 2026-07-08) is stopped too — and
    the result carries exit code 124 and a note on stderr. A process that
    detaches with ``setsid`` leaves the group and outlives the deadline; the
    container's own pids/memory limits are the backstop for that. Without the
    flag, *timeout* is ignored, as it always was.
    """
    container = find_sandbox_container(config)

    parts: list[str] = []
    if cwd:
        parts.append(f"cd {shlex.quote(cwd)}")
    parts.append(" ".join(shlex.quote(str(c)) for c in cmd))
    shell_cmd = " && ".join(parts)

    argv = ["sh", "-c", shell_cmd]
    enforced = bool(config and config.enforce_exec_timeout and timeout and timeout > 0)
    if enforced:
        argv = ["timeout", "-k", str(_KILL_GRACE_SECONDS), str(int(timeout)), *argv]

    started = time.monotonic()
    exit_code, output = container.exec_run(
        argv,
        demux=True,
        environment=dict(env) if env else None,
    )
    stdout = (output[0] or b"").decode(errors="replace") if output else ""
    stderr = (output[1] or b"").decode(errors="replace") if output else ""

    if enforced and (exit_code == _TIMED_OUT or
                     (exit_code == _KILLED and time.monotonic() - started >= timeout)):
        # 124: stopped by TERM. 137 after the deadline: it ignored TERM and was
        # KILLed five seconds later (137 before the deadline is something else,
        # e.g. the memory limit, and is left alone).
        stderr += (
            f"\n[sandbox] command timed out after {int(timeout)}s and was stopped."
        )

    result = subprocess.CompletedProcess(
        args=cmd, returncode=exit_code, stdout=stdout, stderr=stderr,
    )
    if result.returncode != 0:
        logger.debug(
            "Sandbox command failed (rc=%d): %s\nstderr: %s",
            exit_code, shell_cmd, stderr[:500],
        )
    return result
