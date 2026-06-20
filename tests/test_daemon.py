"""Tests for the FastAPI control daemon (M3 Step 2).

Requires the [daemon] extra; skipped otherwise. The control plane is
monkeypatched, so no docker/OpenCode is touched.
"""
import subprocess

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402
from starlette.websockets import WebSocketDisconnect  # noqa: E402

from prax_sandbox import control_plane  # noqa: E402
from prax_sandbox.daemon import cdp_proxy  # noqa: E402
from prax_sandbox.daemon.app import build_app  # noqa: E402
from prax_sandbox.daemon.config import DaemonConfig  # noqa: E402

TOKEN = "test-bearer-token"
AUTH = {"Authorization": f"Bearer {TOKEN}"}


@pytest.fixture()
def client(tmp_path, monkeypatch):
    cfg = DaemonConfig(bearer_token=TOKEN, workspace_dir=str(tmp_path))
    # Stub the control plane so no docker/OpenCode is needed.
    monkeypatch.setattr(control_plane, "configure", lambda c: None)
    monkeypatch.setattr(control_plane, "start_session",
                        lambda u, t, model=None: {"session_id": "s1", "status": "running", "model": model or "m"})
    monkeypatch.setattr(control_plane, "run_command",
                        lambda cmd, cwd=None, env=None, timeout=300: subprocess.CompletedProcess(cmd, 0, "out\n", ""))
    monkeypatch.setattr(control_plane, "get_active_sessions", lambda u: [])
    monkeypatch.setattr(control_plane, "health", lambda: True)
    monkeypatch.setattr(control_plane, "get_runtime_mode", lambda: "docker (persistent)")
    with TestClient(build_app(cfg)) as c:
        yield c


class TestAuth:
    def test_no_bearer_401(self, client):
        assert client.post("/v1/sessions/start", json={"user_id": "+1", "task": "x"}).status_code == 401

    def test_wrong_bearer_401(self, client):
        r = client.post("/v1/sessions/start", headers={"Authorization": "Bearer nope"},
                        json={"user_id": "+1", "task": "x"})
        assert r.status_code == 401

    def test_correct_bearer_200(self, client):
        r = client.post("/v1/sessions/start", headers=AUTH, json={"user_id": "+1", "task": "x"})
        assert r.status_code == 200 and r.json()["session_id"] == "s1"

    def test_healthz_unauth_200(self, client):
        assert client.get("/healthz").status_code == 200

    def test_v1_health_needs_auth(self, client):
        assert client.get("/v1/health").status_code == 401
        assert client.get("/v1/health", headers=AUTH).json()["sandbox"] is True


class TestRoutes:
    def test_exec_returns_completedprocess_fields(self, client):
        r = client.post("/v1/exec", headers=AUTH, json={"cmd": ["echo", "hi"]})
        assert r.status_code == 200
        body = r.json()
        assert body["returncode"] == 0 and body["stdout"] == "out\n" and body["args"] == ["echo", "hi"]

    def test_capabilities(self, client):
        caps = client.get("/v1/capabilities", headers=AUTH).json()
        assert caps["remote"] is True and caps["file_api"] is True

    def test_active_sessions_empty(self, client):
        assert client.post("/v1/sessions/active", headers=AUTH, json={"user_id": "+1"}).json() == []


class TestFileRoutes:
    def test_write_then_read(self, client):
        w = client.put("/v1/files/write", headers=AUTH, params={"user_id": "+1", "path": "active/a.py"}, content=b"hi")
        assert w.status_code == 200 and w.json()["bytes"] == 2
        r = client.get("/v1/files/read", headers=AUTH, params={"user_id": "+1", "path": "active/a.py"})
        assert r.status_code == 200 and r.content == b"hi"

    def test_traversal_rejected(self, client):
        r = client.get("/v1/files/read", headers=AUTH, params={"user_id": "+1", "path": "../../etc/passwd"})
        assert r.status_code == 400

    def test_read_missing_404(self, client):
        r = client.get("/v1/files/read", headers=AUTH, params={"user_id": "+1", "path": "nope"})
        assert r.status_code == 404


class TestSseStreaming:
    def test_send_message_streams_output_then_result(self, client, monkeypatch):
        def fake_send(user_id, message, model=None, session_id=None):
            # emits via the per-call sink the route installed (output_to)
            control_plane._push_sandbox_live(None, "partial...")
            return {"response": "done", "rounds_used": 1}

        monkeypatch.setattr(control_plane, "send_message", fake_send)
        with client.stream("POST", "/v1/sessions/message", headers=AUTH,
                           json={"user_id": "+1", "message": "hi"}) as r:
            assert r.status_code == 200
            body = "".join(r.iter_text())
        assert "event: output" in body and "partial..." in body
        assert "event: result" in body and '"response": "done"' in body


class TestCdpAuthBeforeDial:
    def test_ws_bad_bearer_never_dials_upstream(self, client, monkeypatch):
        dialed = []

        async def _spy(cfg, path):
            dialed.append(path)
            raise AssertionError("upstream must not be dialed on auth failure")

        monkeypatch.setattr(cdp_proxy, "_connect_upstream", _spy)
        # No / wrong Authorization on the WS upgrade -> closed before any dial.
        with pytest.raises(WebSocketDisconnect):
            with client.websocket_connect("/v1/cdp/ws/devtools/page/ABC"):
                pass
        with pytest.raises(WebSocketDisconnect):
            with client.websocket_connect("/v1/cdp/ws/devtools/page/ABC",
                                          headers={"Authorization": "Bearer wrong"}):
                pass
        assert dialed == []


class TestRebuildGate:
    def test_rebuild_disabled_by_default_403(self, client):
        assert client.post("/v1/rebuild", headers=AUTH, json={}).status_code == 403

    def test_capabilities_reflects_rebuild_off(self, client):
        assert client.get("/v1/capabilities", headers=AUTH).json()["rebuild"] is False

    def test_rebuild_allowed_when_enabled(self, tmp_path, monkeypatch):
        cfg = DaemonConfig(bearer_token=TOKEN, workspace_dir=str(tmp_path), allow_rebuild=True)
        monkeypatch.setattr(control_plane, "configure", lambda c: None)
        monkeypatch.setattr(control_plane, "rebuild_sandbox", lambda dc=None: {"status": "rebuilt"})
        with TestClient(build_app(cfg)) as c:
            r = c.post("/v1/rebuild", headers=AUTH, json={})
            assert r.status_code == 200 and r.json()["status"] == "rebuilt"
            assert c.get("/v1/capabilities", headers=AUTH).json()["rebuild"] is True


class TestSerializationParity:
    """The daemon must serialize control-plane returns identically to in-process."""

    def test_review_session_json_matches_in_process(self, client, monkeypatch):
        payload = {"session_id": "s1", "status": "running", "files": ["a.py"],
                   "opencode_state": {"role": "assistant"}, "rounds_remaining": 9}
        monkeypatch.setattr(control_plane, "review_session", lambda u, session_id=None: payload)
        local = control_plane.review_session("+1")
        remote = client.post("/v1/sessions/review", headers=AUTH, json={"user_id": "+1"}).json()
        assert remote == local == payload

    def test_run_command_fields_match_in_process(self, client):
        # client fixture stubs control_plane.run_command -> CompletedProcess(cmd, 0, "out\n", "")
        local = control_plane.run_command(["echo", "hi"])
        remote = client.post("/v1/exec", headers=AUTH, json={"cmd": ["echo", "hi"]}).json()
        assert remote == {"args": local.args, "returncode": local.returncode,
                          "stdout": local.stdout, "stderr": local.stderr}


class TestPayloadCap:
    def test_oversize_413(self, tmp_path, monkeypatch):
        cfg = DaemonConfig(bearer_token=TOKEN, workspace_dir=str(tmp_path), max_payload_bytes=10)
        monkeypatch.setattr(control_plane, "configure", lambda c: None)
        with TestClient(build_app(cfg)) as c:
            r = c.put("/v1/files/write", headers=AUTH, params={"user_id": "+1", "path": "a"}, content=b"x" * 100)
            assert r.status_code == 413


class TestFailClosed:
    def test_no_token_refuses_to_start(self):
        with pytest.raises(SystemExit):
            DaemonConfig(bearer_token=None).validate_or_die()

    def test_non_loopback_plaintext_refused(self):
        with pytest.raises(SystemExit):
            DaemonConfig(bearer_token="t", bind_host="0.0.0.0").validate_or_die()

    def test_non_loopback_with_tls_ok(self):
        DaemonConfig(bearer_token="t", bind_host="0.0.0.0", tls_cert="c", tls_key="k").validate_or_die()
