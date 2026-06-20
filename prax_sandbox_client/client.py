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

    # --- Session lifecycle ---
    def start_session(self, user_id, task, model=None):
        return self._t.start_session(user_id, task, model=model)

    def send_message(self, user_id, message, model=None, session_id=None):
        return self._t.send_message(user_id, message, model=model, session_id=session_id)

    def review_session(self, user_id, session_id=None):
        return self._t.review_session(user_id, session_id=session_id)

    def finish_session(self, user_id, summary="", session_id=None):
        return self._t.finish_session(user_id, summary=summary, session_id=session_id)

    def abort_session(self, user_id, session_id=None):
        return self._t.abort_session(user_id, session_id=session_id)

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

    # --- Solutions archive ---
    def search_solutions(self, user_id, query):
        return self._t.search_solutions(user_id, query)

    def execute_solution(self, user_id, solution_id, command=None):
        return self._t.execute_solution(user_id, solution_id, command=command)

    # --- Introspection ---
    def get_active_session(self, user_id):
        return self._t.get_active_session(user_id)

    def get_active_sessions(self, user_id):
        return self._t.get_active_sessions(user_id)

    def get_runtime_mode(self):
        return self._t.get_runtime_mode()

    def cleanup_stale_sessions(self):
        return self._t.cleanup_stale_sessions()

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
