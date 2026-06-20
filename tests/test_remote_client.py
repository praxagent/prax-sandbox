"""Remote client file-API mapping + remote-compose exposure (M3 Step 3)."""
import pathlib

from prax_sandbox_client import SandboxConfig
from prax_sandbox_client.transport import HttpTransport


class FakeResp:
    def __init__(self, status=200, json_data=None, content=b""):
        self.status_code = status
        self.ok = status < 400
        self._json = json_data
        self.content = content
        self.text = ""
        self.reason = "OK"

    def json(self):
        return self._json


class FakeSession:
    def __init__(self, handler):
        self.headers = {}
        self.verify = True
        self.cert = None
        self.handler = handler
        self.calls = []

    def request(self, method, url, json=None, params=None, data=None, timeout=None):
        self.calls.append({"method": method, "url": url, "json": json, "params": params, "data": data})
        return self.handler(self.calls[-1])


def _http(handler):
    t = HttpTransport(SandboxConfig(daemon_url="https://d:8843", daemon_token="tok"))
    t._s = FakeSession(handler)
    return t


class TestFileApiMapping:
    def test_write_sends_raw_body(self):
        t = _http(lambda c: FakeResp(200, {"bytes": 5}))
        assert t.file_write("+1", "active/a.py", b"hello") == 5
        call = t._s.calls[0]
        assert call["method"] == "PUT" and call["url"].endswith("/v1/files/write")
        assert call["data"] == b"hello" and call["params"]["path"] == "active/a.py"

    def test_read_returns_content(self):
        t = _http(lambda c: FakeResp(200, content=b"data"))
        assert t.file_read("+1", "x") == b"data"
        assert t._s.calls[0]["url"].endswith("/v1/files/read")

    def test_grep_returns_results(self):
        t = _http(lambda c: FakeResp(200, {"results": [{"path": "p", "session_id": "s", "snippet": "x"}]}))
        out = t.file_grep("+1", "needle", path="archive")
        assert out[0]["session_id"] == "s"

    def test_list_returns_entries(self):
        t = _http(lambda c: FakeResp(200, {"entries": [{"path": "a", "type": "file"}]}))
        assert t.file_list("+1", recursive=True)[0]["path"] == "a"

    def test_pull_and_push_tar(self):
        t = _http(lambda c: FakeResp(200, content=b"TARDATA"))
        assert t.pull_tar("+1", "active") == b"TARDATA"
        t2 = _http(lambda c: FakeResp(200, {"extracted": 3}))
        assert t2.push_tar("+1", b"TARDATA", "active") == 3
        assert t2._s.calls[0]["data"] == b"TARDATA"


class TestRemoteComposeExposure:
    def test_only_daemon_port_published(self):
        text = (pathlib.Path(__file__).parent.parent / "docker-compose.remote.yml").read_text()
        # The daemon's TLS port is the only thing published...
        assert '"8843:8843"' in text
        # ...and none of the sandbox's unauthenticated ports are published.
        for bad in ('"4096:4096"', '"9223:9223"', '"6080:6080"', '"6090:6090"',
                    '"127.0.0.1:4096:4096"', '"127.0.0.1:9223:9223"'):
            assert bad not in text, f"remote compose must not publish {bad}"
