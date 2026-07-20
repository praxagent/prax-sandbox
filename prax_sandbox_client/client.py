"""SandboxClient — the plug-and-play facade a harness codes against.

The facade forwards every method to a :class:`~prax_sandbox_client.transport.Transport`.
With no ``daemon_url`` in the :class:`SandboxConfig` it uses the in-process
transport (drives :mod:`prax_sandbox.control_plane`, which holds the docker
socket) — the default, unchanged behavior. With ``daemon_url`` set it uses the
HTTP transport against a remote control daemon. The facade's method signatures
and every caller stay identical regardless of transport.
"""
from __future__ import annotations

from prax_sandbox_client.config import SandboxConfig
from prax_sandbox_client.transport import make_transport


class SandboxClient:
    """Drive a sandbox's lifecycle, shell, and solution archive.

    Pass a :class:`SandboxConfig` to select + configure the transport; omit it
    to use the in-process control plane with built-in defaults.
    """

    def __init__(self, config: SandboxConfig | None = None) -> None:
        self._config = config
        self._t = make_transport(config)
        if config is not None:
            self._t.configure(config)

    # (OpenCode coding-SESSION methods — start/send/review/finish/abort +
    #  search/execute solutions + get_active_session(s)/cleanup — were removed
    #  with the coding-agent CLIs. The sandbox is a pure execution environment.)

    # --- Shell / packages ---
    def run_shell(self, command, timeout=60):
        return self._t.run_shell(command, timeout=timeout)

    def run_command(self, cmd, cwd=None, env=None, timeout=300):
        """Run a command list (already path-translated) in the sandbox.

        Returns a ``subprocess.CompletedProcess``. The harness's shell helper
        does any host→sandbox path translation before calling this. Raises
        ``SandboxTransportError`` in remote mode if the daemon is unreachable
        (never silently falls back to in-process).
        """
        return self._t.run_command(cmd, cwd=cwd, env=env, timeout=timeout)

    def install_package(self, package_name):
        return self._t.install_package(package_name)

    def rebuild_sandbox(self, dockerfile_content=None):
        return self._t.rebuild_sandbox(dockerfile_content)

    # --- Introspection ---
    def get_runtime_mode(self):
        return self._t.get_runtime_mode()

    def health(self) -> bool:
        """Best-effort: is the sandbox reachable right now?"""
        try:
            return bool(self._t.health())
        except Exception:
            return False

    def capabilities(self) -> dict:
        """What this sandbox build supports (for handshake/degradation).

        In remote mode this reflects the daemon's advertised capabilities;
        locally it's the static in-process set.
        """
        return self._t.capabilities()

    # --- Confined workspace file access ---
    # Local mode operates on the shared workspace mount; remote mode goes through
    # the daemon's file API. Used by a harness's sync adapters in remote mode.
    def file_list(self, user_id, path="", recursive=False):
        return self._t.file_list(user_id, path=path, recursive=recursive)

    def file_read(self, user_id, path, max_bytes=10_000_000) -> bytes:
        return self._t.file_read(user_id, path, max_bytes=max_bytes)

    def file_write(self, user_id, path, data: bytes) -> int:
        return self._t.file_write(user_id, path, data)

    def file_grep(self, user_id, query, path="", include="*", max_count=200):
        return self._t.file_grep(user_id, query, path=path, include=include, max_count=max_count)

    def pull_tar(self, user_id, path="") -> bytes:
        return self._t.pull_tar(user_id, path=path)

    def push_tar(self, user_id, tar_bytes: bytes, path="") -> int:
        return self._t.push_tar(user_id, tar_bytes, path=path)


_default_client: SandboxClient | None = None


def get_client() -> SandboxClient:
    """Return the process-wide default :class:`SandboxClient`.

    Uses the harness's default configuration (no explicit ``SandboxConfig``).
    """
    global _default_client
    if _default_client is None:
        _default_client = SandboxClient()
    return _default_client
