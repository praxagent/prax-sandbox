"""Transport seam for :class:`SandboxClient`.

The facade forwards every method to a ``Transport``. Two implementations:

- :class:`InProcessTransport` — wraps :mod:`prax_sandbox.control_plane` and runs
  the control plane in this process (holds the docker socket). This is the
  DEFAULT and is byte-for-byte the behavior the facade had before M3.
- :class:`HttpTransport` — talks to a remote control daemon over HTTP(S) with a
  bearer token. Engaged ONLY when ``SandboxConfig.daemon_url`` is set.

``make_transport(config)`` is the sole selector: empty ``daemon_url`` → in-process
(local-first; no network, no token, no TLS, no Tailscale). Only ``requests`` (an
existing base dependency) is imported here — never fastapi/uvicorn/httpx/
websockets, so importing the client never pulls in the optional ``[daemon]`` deps.
"""
from __future__ import annotations

import subprocess
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from prax_sandbox_client.config import SandboxConfig


class SandboxTransportError(RuntimeError):
    """A remote-transport call failed (daemon down, timeout, auth, 5xx).

    Carries only the method, URL path, and status — never request headers, so
    the bearer token can't leak into a harness's logs or traceback.
    """

    def __init__(self, method: str, path: str, detail: str, status: int | None = None) -> None:
        self.method = method
        self.path = path
        self.status = status
        super().__init__(f"{method} {path} failed ({status or 'no response'}): {detail}")


@runtime_checkable
class Transport(Protocol):
    """The seam the facade talks to — mirrors the SandboxClient method set."""

    def configure(self, config: SandboxConfig) -> None: ...
    def start_session(self, user_id, task, model=None) -> dict: ...
    def send_message(self, user_id, message, model=None, session_id=None) -> dict: ...
    def review_session(self, user_id, session_id=None) -> dict: ...
    def finish_session(self, user_id, summary="", session_id=None) -> dict: ...
    def abort_session(self, user_id, session_id=None) -> dict: ...
    def run_shell(self, command, timeout=60) -> dict: ...
    def run_command(self, cmd, cwd=None, env=None, timeout=300) -> subprocess.CompletedProcess: ...
    def install_package(self, package_name) -> dict: ...
    def rebuild_sandbox(self, dockerfile_content=None) -> dict: ...
    def search_solutions(self, user_id, query) -> list: ...
    def execute_solution(self, user_id, solution_id, command=None) -> dict: ...
    def get_active_session(self, user_id): ...
    def get_active_sessions(self, user_id) -> list: ...
    def get_runtime_mode(self) -> str: ...
    def cleanup_stale_sessions(self) -> int: ...
    def health(self) -> bool: ...
    def capabilities(self) -> dict: ...
    # file API — used by harness sync adapters in remote mode
    def file_list(self, user_id, path="", recursive=False) -> list: ...
    def file_read(self, user_id, path, max_bytes=10_000_000) -> bytes: ...
    def file_write(self, user_id, path, data: bytes) -> int: ...
    def file_grep(self, user_id, query, path="", include="*", max_count=200) -> list: ...
    def pull_tar(self, user_id, path="") -> bytes: ...
    def push_tar(self, user_id, tar_bytes: bytes, path="") -> int: ...


@dataclass(frozen=True)
class RemoteSession:
    """Read-only snapshot of a server-side session (satisfies SandboxSession).

    Live state (timers, OpenCode handles) stays on the daemon; this is a frozen
    view, so any accidental write fails loudly rather than silently no-op'ing.
    """

    session_id: str
    user_id: str
    model: str
    created_at: float
    status: str
    rounds_used: int
    max_rounds: int

    @classmethod
    def from_dict(cls, d: dict) -> RemoteSession:
        return cls(
            session_id=d["session_id"], user_id=d["user_id"], model=d.get("model", ""),
            created_at=float(d.get("created_at", 0.0)), status=d.get("status", ""),
            rounds_used=int(d.get("rounds_used", 0)), max_rounds=int(d.get("max_rounds", 0)),
        )


# ---------------------------------------------------------------------------
# In-process transport — the default, unchanged behavior
# ---------------------------------------------------------------------------

LOCAL_CAPABILITIES = {
    "persistent": True, "shell": True, "install": True,
    "rebuild": True, "desktop": True, "browser_cdp": True,
    "file_api": False, "remote": False,
}


class InProcessTransport:
    """Delegate to prax_sandbox.control_plane in this process."""

    def __init__(self) -> None:
        from prax_sandbox import control_plane
        self._cp = control_plane

    def configure(self, config):
        return self._cp.configure(config)

    def capabilities(self) -> dict:
        # The control plane has no capabilities() — it's a client-side concept.
        return dict(LOCAL_CAPABILITIES)

    # --- file API: local filesystem via fileops + the configured workspace_dir ---
    def _root(self, user_id):
        from prax_sandbox import fileops
        return fileops.resolve_user_root(self._cp._cfg().workspace_dir, user_id)

    def file_list(self, user_id, path="", recursive=False):
        from prax_sandbox import fileops
        return fileops.list_dir(self._root(user_id), path, recursive)

    def file_read(self, user_id, path, max_bytes=10_000_000):
        from prax_sandbox import fileops
        return fileops.read_file(self._root(user_id), path, max_bytes)

    def file_write(self, user_id, path, data):
        from prax_sandbox import fileops
        return fileops.write_file(self._root(user_id), path, data)

    def file_grep(self, user_id, query, path="", include="*", max_count=200):
        from prax_sandbox import fileops
        return fileops.grep(self._root(user_id), query, path, include, max_count)

    def pull_tar(self, user_id, path=""):
        from prax_sandbox import fileops
        return fileops.pull_tar(self._root(user_id), path)

    def push_tar(self, user_id, tar_bytes, path=""):
        from prax_sandbox import fileops
        return fileops.push_tar(self._root(user_id), tar_bytes, path)

    def __getattr__(self, name):
        # Forward every other facade method (start_session, run_command, …).
        return getattr(self._cp, name)


# ---------------------------------------------------------------------------
# HTTP transport — remote control daemon over HTTP(S) + bearer
# ---------------------------------------------------------------------------

# Methods whose timeout must exceed the daemon's own (server polls up to 300s).
_LONG_TIMEOUT = 360


class HttpTransport:
    """Talk to a remote control daemon. Engaged only when daemon_url is set."""

    def __init__(self, config: SandboxConfig) -> None:
        import requests  # base dependency — never imports the [daemon] extra
        self._base = (config.daemon_url or "").rstrip("/")
        self._timeout = config.daemon_timeout
        # Harness output callback: stays client-side and is invoked as live
        # output streams back from the daemon during send_message.
        self._on_output = config.on_output
        s = requests.Session()
        s.headers["Authorization"] = f"Bearer {config.daemon_token or ''}"
        s.verify = config.tls_verify
        if config.client_cert and config.client_key:
            s.cert = (config.client_cert, config.client_key)
        self._s = s

    # configure() is a no-op remotely: the daemon owns its own config, and the
    # harness-side callbacks (on_output/resolve_workspace/commit) can't cross a
    # process boundary, so they stay client-side and unused here.
    def configure(self, config):
        return None

    # --- request helpers ---
    def _call(self, method: str, path: str, *, json=None, params=None, data=None, timeout=None):
        import requests
        try:
            r = self._s.request(
                method, self._base + path, json=json, params=params, data=data,
                timeout=timeout or self._timeout,
            )
        except requests.RequestException as e:
            # Scrub: surface the error type, never the request (it holds the token).
            raise SandboxTransportError(method, path, type(e).__name__) from None
        if r.status_code >= 400:
            raise SandboxTransportError(method, path, _safe_reason(r), status=r.status_code)
        return r

    def _json(self, method, path, *, json=None, params=None, timeout=None) -> dict:
        """For dict-returning methods: failures become {'error': ...} (the
        existing in-process convention the agent tools render)."""
        try:
            return self._call(method, path, json=json, params=params, timeout=timeout).json()
        except SandboxTransportError as e:
            return {"error": str(e)}

    # --- session lifecycle (dict-returning → error-dict on failure) ---
    def start_session(self, user_id, task, model=None):
        return self._json("POST", "/v1/sessions/start", json={"user_id": user_id, "task": task, "model": model})

    def send_message(self, user_id, message, model=None, session_id=None):
        # Streams SSE: each `output` event is forwarded to the harness's
        # on_output callback; the final `result` event is returned.
        import requests
        payload = {"user_id": user_id, "message": message, "model": model, "session_id": session_id}
        try:
            resp = self._s.request("POST", self._base + "/v1/sessions/message",
                                   json=payload, stream=True, timeout=_LONG_TIMEOUT)
        except requests.RequestException as e:
            return {"error": str(SandboxTransportError("POST", "/v1/sessions/message", type(e).__name__))}
        if resp.status_code >= 400:
            return {"error": _safe_reason(resp)}
        result = {"error": "no result from daemon"}
        try:
            for kind, data in _iter_sse(resp):
                if kind == "output":
                    if self._on_output:
                        try:
                            self._on_output("Sandbox Agent", data if isinstance(data, str) else str(data))
                        except Exception:
                            pass
                elif kind == "result":
                    result = data
        finally:
            resp.close()
        return result

    def review_session(self, user_id, session_id=None):
        return self._json("POST", "/v1/sessions/review", json={"user_id": user_id, "session_id": session_id})

    def finish_session(self, user_id, summary="", session_id=None):
        return self._json("POST", "/v1/sessions/finish", json={"user_id": user_id, "summary": summary, "session_id": session_id})

    def abort_session(self, user_id, session_id=None):
        return self._json("POST", "/v1/sessions/abort", json={"user_id": user_id, "session_id": session_id})

    # --- shell / packages ---
    def run_shell(self, command, timeout=60):
        return self._json("POST", "/v1/shell", timeout=max(self._timeout, timeout + 30),
                          json={"command": command, "timeout": timeout})

    def run_command(self, cmd, cwd=None, env=None, timeout=300):
        # CompletedProcess has no error field → raise on transport failure.
        r = self._call("POST", "/v1/exec", timeout=max(self._timeout, timeout + 30),
                       json={"cmd": list(cmd), "cwd": cwd, "env": env, "timeout": timeout})
        d = r.json()
        return subprocess.CompletedProcess(
            args=d.get("args", cmd), returncode=d.get("returncode", -1),
            stdout=d.get("stdout", ""), stderr=d.get("stderr", ""),
        )

    def install_package(self, package_name):
        return self._json("POST", "/v1/packages", json={"package_name": package_name})

    def rebuild_sandbox(self, dockerfile_content=None):
        return self._json("POST", "/v1/rebuild", timeout=_LONG_TIMEOUT,
                          json={"dockerfile_content": dockerfile_content})

    # --- solutions ---
    def search_solutions(self, user_id, query):
        try:
            return self._call("POST", "/v1/solutions/search", json={"user_id": user_id, "query": query}).json()
        except SandboxTransportError:
            return []

    def execute_solution(self, user_id, solution_id, command=None):
        return self._json("POST", "/v1/solutions/execute",
                          json={"user_id": user_id, "solution_id": solution_id, "command": command})

    # --- introspection (sessions → raise; the rest degrade) ---
    def get_active_session(self, user_id):
        r = self._call("POST", "/v1/sessions/active-one", json={"user_id": user_id})
        d = r.json()
        return RemoteSession.from_dict(d) if d else None

    def get_active_sessions(self, user_id):
        r = self._call("POST", "/v1/sessions/active", json={"user_id": user_id})
        return [RemoteSession.from_dict(d) for d in r.json()]

    def get_runtime_mode(self):
        try:
            return self._call("GET", "/v1/runtime-mode").json().get("mode", "remote")
        except SandboxTransportError:
            return "remote (daemon unreachable)"

    def cleanup_stale_sessions(self):
        try:
            return int(self._call("POST", "/v1/admin/cleanup-stale").json().get("count", 0))
        except SandboxTransportError:
            return 0

    def health(self) -> bool:
        try:
            return self._call("GET", "/v1/health").ok
        except Exception:
            return False

    def capabilities(self) -> dict:
        try:
            return self._call("GET", "/v1/capabilities").json()
        except Exception:
            # Degrade to the static defaults — never raise.
            return {"persistent": True, "shell": True, "remote": True}

    # --- file API (raises SandboxTransportError on failure; not silently empty) ---
    def file_list(self, user_id, path="", recursive=False):
        return self._call("GET", "/v1/files/list",
                          params={"user_id": user_id, "path": path, "recursive": recursive}).json()["entries"]

    def file_read(self, user_id, path, max_bytes=10_000_000):
        return self._call("GET", "/v1/files/read",
                          params={"user_id": user_id, "path": path, "max_bytes": max_bytes}).content

    def file_write(self, user_id, path, data):
        return self._call("PUT", "/v1/files/write",
                          params={"user_id": user_id, "path": path}, data=data).json()["bytes"]

    def file_grep(self, user_id, query, path="", include="*", max_count=200):
        return self._call("POST", "/v1/files/grep",
                          json={"user_id": user_id, "query": query, "path": path,
                                "include": include, "max_count": max_count}).json()["results"]

    def pull_tar(self, user_id, path=""):
        return self._call("GET", "/v1/files/pull_tar",
                          params={"user_id": user_id, "path": path}, timeout=_LONG_TIMEOUT).content

    def push_tar(self, user_id, tar_bytes, path=""):
        return self._call("PUT", "/v1/files/push_tar", params={"user_id": user_id, "path": path},
                          data=tar_bytes, timeout=_LONG_TIMEOUT).json()["extracted"]


def _iter_sse(resp):
    """Yield (event, data) pairs from a Server-Sent-Events response.

    ``data`` is JSON-decoded when possible (the daemon sends JSON payloads).
    """
    import json
    event = None
    data_lines: list[str] = []
    for raw in resp.iter_lines(decode_unicode=True):
        line = raw if raw is not None else ""
        if line == "":  # blank line dispatches the buffered event
            if data_lines:
                blob = "\n".join(data_lines)
                try:
                    payload = json.loads(blob)
                except Exception:
                    payload = blob
                yield (event or "message", payload)
            event, data_lines = None, []
        elif line.startswith("event:"):
            event = line[6:].strip()
        elif line.startswith("data:"):
            data_lines.append(line[5:].lstrip())


def _safe_reason(resp) -> str:
    """A short, token-free reason from an error response."""
    try:
        body = resp.json()
        if isinstance(body, dict) and "detail" in body:
            return str(body["detail"])[:200]
    except Exception:
        pass
    return (resp.text or resp.reason or "")[:200]


# ---------------------------------------------------------------------------
# Selector — the sole transport switch
# ---------------------------------------------------------------------------

def make_transport(config: SandboxConfig | None) -> Transport:
    """Return the remote transport iff a non-empty daemon_url is set, else local.

    Local-first by construction: with no config (or an empty/whitespace
    daemon_url) the in-process transport is used and nothing remote is imported.
    """
    if config is not None and (config.daemon_url or "").strip():
        return HttpTransport(config)
    return InProcessTransport()
