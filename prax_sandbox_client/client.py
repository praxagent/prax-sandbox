"""SandboxClient — the plug-and-play facade a harness codes against.

The facade delegates to :mod:`prax_sandbox.control_plane` (in-process; the
control plane holds the docker socket). A harness configures it with a
:class:`SandboxConfig` and never touches the control plane directly. When the
remote control daemon lands, each method's body moves to talk to it over HTTP;
callers that already go through ``SandboxClient`` need no change.
"""
from __future__ import annotations

from prax_sandbox_client.config import SandboxConfig


class SandboxClient:
    """Drive a sandbox's lifecycle, shell, and solution archive.

    Pass a :class:`SandboxConfig` to configure the control plane; omit it to use
    the control plane's built-in defaults.
    """

    def __init__(self, config: SandboxConfig | None = None) -> None:
        self._config = config
        if config is not None:
            self._svc().configure(config)

    @staticmethod
    def _svc():
        from prax_sandbox import control_plane
        return control_plane

    # --- Session lifecycle ---
    def start_session(self, user_id, task, model=None):
        return self._svc().start_session(user_id, task, model=model)

    def send_message(self, user_id, message, model=None, session_id=None):
        return self._svc().send_message(user_id, message, model=model, session_id=session_id)

    def review_session(self, user_id, session_id=None):
        return self._svc().review_session(user_id, session_id=session_id)

    def finish_session(self, user_id, summary="", session_id=None):
        return self._svc().finish_session(user_id, summary=summary, session_id=session_id)

    def abort_session(self, user_id, session_id=None):
        return self._svc().abort_session(user_id, session_id=session_id)

    # --- Shell / packages ---
    def run_shell(self, command, timeout=60):
        return self._svc().run_shell(command, timeout=timeout)

    def run_command(self, cmd, cwd=None, env=None, timeout=300):
        """Run a command list (already path-translated) in the sandbox.

        Returns a ``subprocess.CompletedProcess``. The harness's shell helper
        does any host→sandbox path translation before calling this.
        """
        return self._svc().run_command(cmd, cwd=cwd, env=env, timeout=timeout)

    def install_package(self, package_name):
        return self._svc().install_package(package_name)

    def rebuild_sandbox(self, dockerfile_content=None):
        return self._svc().rebuild_sandbox(dockerfile_content)

    # --- Solutions archive ---
    def search_solutions(self, user_id, query):
        return self._svc().search_solutions(user_id, query)

    def execute_solution(self, user_id, solution_id, command=None):
        return self._svc().execute_solution(user_id, solution_id, command=command)

    # --- Introspection ---
    def get_active_session(self, user_id):
        return self._svc().get_active_session(user_id)

    def get_active_sessions(self, user_id):
        return self._svc().get_active_sessions(user_id)

    def get_runtime_mode(self):
        return self._svc().get_runtime_mode()

    def cleanup_stale_sessions(self):
        return self._svc().cleanup_stale_sessions()

    def health(self) -> bool:
        """Best-effort: is the sandbox reachable right now?"""
        try:
            return bool(self._svc().health())
        except Exception:
            return False

    def capabilities(self) -> dict:
        """Describe what this sandbox build supports (for handshake/degradation)."""
        return {
            "persistent": True,
            "shell": True,
            "install": True,
            "rebuild": True,
            "desktop": True,
            "browser_cdp": True,
        }


_default_client: SandboxClient | None = None


def get_client() -> SandboxClient:
    """Return the process-wide default :class:`SandboxClient`.

    Uses the harness's default configuration (no explicit ``SandboxConfig``).
    """
    global _default_client
    if _default_client is None:
        _default_client = SandboxClient()
    return _default_client
