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

    def test_capabilities_uses_remote(self):
        t = _http(lambda m, u, j: FakeResp(200, {"persistent": True, "remote": True, "file_api": True}))
        caps = t.capabilities()
        assert caps["remote"] is True and caps["file_api"] is True


# --------------------------------------------------------------------------- #
# Failure handling — never silently fall back to in-process
# --------------------------------------------------------------------------- #

def _boom(*a, **k):
    raise requests.exceptions.ConnectionError("connection refused")


class TestFailures:
    def test_dict_methods_return_error_dict(self):
        t = _http(_boom)
        assert "error" in t.run_shell("ls")
        assert "error" in t.install_package("jq")

    def test_run_command_raises_not_falls_back(self):
        t = _http(_boom)
        with pytest.raises(SandboxTransportError):
            t.run_command(["echo", "hi"])

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
        assert "SEKRIT-TOKEN" not in str(t.run_shell("echo hi"))


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
