"""Tests for the client transport seam (M3 Step 1).

The remote path is exercised with a fake requests.Session — no daemon needed.
The in-process path stays the default and is covered by test_control_plane.py.
"""
import subprocess

import pytest
import requests

from prax_sandbox_client import (
    RemoteSession,
    SandboxClient,
    SandboxConfig,
    SandboxSession,
    SandboxTransportError,
    make_transport,
)
from prax_sandbox_client.transport import HttpTransport, InProcessTransport

# --------------------------------------------------------------------------- #
# Fake HTTP plumbing
# --------------------------------------------------------------------------- #

_NO_JSON = object()


class FakeResp:
    def __init__(self, status=200, json_data=_NO_JSON, text="", reason="OK", lines=None):
        self.status_code = status
        self._json = json_data
        self.text = text
        self.reason = reason
        self.ok = status < 400
        self._lines = lines or []

    def json(self):
        if self._json is _NO_JSON:
            raise ValueError("no json body")
        return self._json  # may be None (a JSON null) or any value

    def iter_lines(self, decode_unicode=False):
        yield from self._lines

    def close(self):
        pass


class FakeSession:
    """Records requests and returns whatever `handler(method, url, json)` yields.

    A handler may return a FakeResp or raise a requests exception.
    """

    def __init__(self, handler):
        self.headers = {}
        self.verify = True
        self.cert = None
        self.handler = handler
        self.calls = []

    def request(self, method, url, json=None, params=None, data=None, stream=None, timeout=None):
        self.calls.append({"method": method, "url": url, "json": json, "params": params,
                           "data": data, "stream": stream, "timeout": timeout})
        return self.handler(method, url, json)


def _http(handler, **cfg_kw):
    cfg = SandboxConfig(daemon_url="https://daemon.example:8843", daemon_token="SEKRIT-TOKEN", **cfg_kw)
    t = HttpTransport(cfg)
    t._s = FakeSession(handler)
    return t


# --------------------------------------------------------------------------- #
# Selector — the sole transport switch
# --------------------------------------------------------------------------- #

class TestSelector:
    def test_none_config_is_in_process(self):
        assert isinstance(make_transport(None), InProcessTransport)

    def test_empty_daemon_url_is_in_process(self):
        assert isinstance(make_transport(SandboxConfig()), InProcessTransport)
        assert isinstance(make_transport(SandboxConfig(daemon_url="")), InProcessTransport)
        assert isinstance(make_transport(SandboxConfig(daemon_url="   ")), InProcessTransport)

    def test_daemon_url_set_is_http(self):
        assert isinstance(make_transport(SandboxConfig(daemon_url="https://x:8843")), HttpTransport)

    def test_client_picks_transport_from_config(self):
        assert isinstance(SandboxClient()._t, InProcessTransport)
        assert isinstance(SandboxClient(SandboxConfig())._t, InProcessTransport)
        assert isinstance(SandboxClient(SandboxConfig(daemon_url="https://x:8843"))._t, HttpTransport)


# --------------------------------------------------------------------------- #
# HTTP method mapping
# --------------------------------------------------------------------------- #

class TestHttpMapping:
    def test_start_session_maps_and_returns_json(self):
        body = {"session_id": "s1", "status": "running", "model": "m"}
        t = _http(lambda m, u, j: FakeResp(200, body))
        out = t.start_session("+1", "build", model="m")
        assert out == body
        call = t._s.calls[0]
        assert call["method"] == "POST" and call["url"].endswith("/v1/sessions/start")
        assert call["json"] == {"user_id": "+1", "task": "build", "model": "m"}

    def test_run_shell_returns_dict(self):
        t = _http(lambda m, u, j: FakeResp(200, {"stdout": "hi", "stderr": "", "exit_code": 0}))
        assert t.run_shell("ls")["stdout"] == "hi"

    def test_run_command_rebuilds_completedprocess(self):
        body = {"args": ["echo", "hi"], "returncode": 0, "stdout": "hi\n", "stderr": ""}
        t = _http(lambda m, u, j: FakeResp(200, body))
        cp = t.run_command(["echo", "hi"])
        assert isinstance(cp, subprocess.CompletedProcess)
        assert cp.returncode == 0 and cp.stdout == "hi\n"
        assert t._s.calls[0]["url"].endswith("/v1/exec")

    def test_get_active_session_hydrates_protocol(self):
        d = {"session_id": "s1", "user_id": "+1", "model": "m", "created_at": 1.0,
             "status": "running", "rounds_used": 2, "max_rounds": 10}
        t = _http(lambda m, u, j: FakeResp(200, d))
        s = t.get_active_session("+1")
        assert isinstance(s, RemoteSession)
        assert isinstance(s, SandboxSession)  # runtime_checkable Protocol
        assert s.session_id == "s1" and s.rounds_used == 2

    def test_get_active_session_none(self):
        # daemon returns JSON null when there's no active session -> None
        t = _http(lambda m, u, j: FakeResp(200, json_data=None))
        assert t.get_active_session("+1") is None

    def test_capabilities_uses_remote(self):
        t = _http(lambda m, u, j: FakeResp(200, {"persistent": True, "remote": True, "file_api": True}))
        caps = t.capabilities()
        assert caps["remote"] is True and caps["file_api"] is True


class TestSseSendMessage:
    def test_streams_output_then_returns_result(self):
        captured = []
        sse = [
            "event: output", 'data: "partial 1"', "",
            "event: output", 'data: "partial 2"', "",
            "event: result", 'data: {"response": "done", "rounds_used": 1}', "",
        ]
        cfg = SandboxConfig(daemon_url="https://d:8843", daemon_token="t",
                            on_output=lambda label, text: captured.append(text))
        t = HttpTransport(cfg)
        t._s = FakeSession(lambda m, u, j: FakeResp(200, lines=sse))
        result = t.send_message("+1", "go")
        assert captured == ["partial 1", "partial 2"]
        assert result["response"] == "done" and result["rounds_used"] == 1
        assert t._s.calls[0]["stream"] is True

    def test_no_on_output_still_returns_result(self):
        sse = ["event: result", 'data: {"response": "ok"}', ""]
        t = HttpTransport(SandboxConfig(daemon_url="https://d:8843", daemon_token="t"))
        t._s = FakeSession(lambda m, u, j: FakeResp(200, lines=sse))
        assert t.send_message("+1", "go")["response"] == "ok"


# --------------------------------------------------------------------------- #
# Failure handling — never silently fall back to in-process
# --------------------------------------------------------------------------- #

def _boom(*a, **k):
    raise requests.exceptions.ConnectionError("connection refused")


class TestFailures:
    def test_dict_methods_return_error_dict(self):
        t = _http(_boom)
        assert "error" in t.start_session("+1", "x")
        assert "error" in t.run_shell("ls")
        assert t.search_solutions("+1", "q") == []  # list method degrades to []

    def test_run_command_raises_not_falls_back(self):
        t = _http(_boom)
        with pytest.raises(SandboxTransportError):
            t.run_command(["echo", "hi"])

    def test_get_active_session_raises(self):
        t = _http(_boom)
        with pytest.raises(SandboxTransportError):
            t.get_active_session("+1")

    def test_health_returns_false_never_raises(self):
        assert _http(_boom).health() is False

    def test_5xx_raises_transport_error(self):
        t = _http(lambda m, u, j: FakeResp(500, text="internal error"))
        with pytest.raises(SandboxTransportError):
            t.run_command(["x"])

    def test_token_never_in_error_message(self):
        t = _http(_boom)
        try:
            t.run_command(["echo", "hi"])
        except SandboxTransportError as e:
            assert "SEKRIT-TOKEN" not in str(e)
            assert "SEKRIT-TOKEN" not in repr(e)
        # error-dict path too
        assert "SEKRIT-TOKEN" not in str(t.start_session("+1", "x"))


# --------------------------------------------------------------------------- #
# Local default cannot regress
# --------------------------------------------------------------------------- #

class TestLocalDefault:
    def test_in_process_capabilities_are_static(self):
        caps = make_transport(None).capabilities()
        assert caps["remote"] is False and caps["persistent"] is True

    def test_importing_client_pulls_no_daemon_deps(self):
        # The [daemon] extra (fastapi/uvicorn/httpx/websockets) must never load
        # on the client path. Run in a CLEAN interpreter so a sibling test that
        # imports the daemon can't pollute sys.modules.
        import subprocess
        import sys
        import textwrap
        code = textwrap.dedent("""
            import sys
            from prax_sandbox_client import SandboxClient, SandboxConfig
            SandboxClient()                                   # in-process
            SandboxConfig(daemon_url="https://x:8843")        # build remote config (no call)
            bad = {"fastapi", "uvicorn", "websockets", "starlette"} & set(sys.modules)
            assert not bad, f"client path imported daemon deps: {bad}"
        """)
        r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
        assert r.returncode == 0, r.stderr
