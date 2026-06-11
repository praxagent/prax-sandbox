"""Tests for the control plane — persistent mode, all HTTP interactions mocked.

The control plane drives an always-on (persistent) sandbox container via the
OpenCode HTTP API; there is no per-session container creation to mock. It is
configured with a :class:`SandboxConfig` (no harness dependency), and the
OpenCode HTTP helpers are monkeypatched.
"""
import importlib
import os

import pytest

from prax_sandbox_client import SandboxConfig


@pytest.fixture()
def sandbox_mod(monkeypatch, tmp_path):
    """Reload the control plane and configure it for persistent mode."""
    module = importlib.reload(importlib.import_module("prax_sandbox.control_plane"))

    # Reset module state.
    module._sessions.clear()
    module._user_sessions.clear()
    module._config = None

    def _test_resolve(user_id: str) -> str:
        root = os.path.join(str(tmp_path), user_id.lstrip("+"))
        os.makedirs(root, exist_ok=True)
        return root

    def _test_commit(root: str, message: str) -> None:
        pass  # no git in tests

    module.configure(SandboxConfig(
        persistent=True,
        host="sandbox",
        workspace_dir=str(tmp_path),
        default_model="anthropic/claude-test",
        max_concurrent=3,
        max_rounds=10,
        timeout=60,
        on_output=None,
        resolve_workspace=_test_resolve,
        commit=_test_commit,
    ))

    return module


@pytest.fixture()
def mock_opencode(sandbox_mod, monkeypatch):
    """Mock all OpenCode HTTP API calls."""
    monkeypatch.setattr(sandbox_mod, "_wait_for_ready", lambda s, timeout=30: (True, ""))
    monkeypatch.setattr(
        sandbox_mod, "_create_opencode_session",
        lambda s, task: ("oc-session-001", ""),
    )
    monkeypatch.setattr(
        sandbox_mod, "_send_opencode_message",
        lambda s, msg, model=None: {"content": "Done!", "model": model},
    )
    monkeypatch.setattr(
        sandbox_mod, "_get_opencode_session",
        lambda s: {"status": "active", "messages": []},
    )
    monkeypatch.setattr(
        sandbox_mod, "_export_opencode_session",
        lambda s: {"messages": [{"role": "assistant", "content": "built it"}]},
    )
    return sandbox_mod


class TestStartSession:
    def test_creates_session(self, mock_opencode):
        mod = mock_opencode
        result = mod.start_session("+10000000000", "Build a calculator")

        assert result["status"] == "running"
        assert "session_id" in result
        assert result["model"] == "anthropic/claude-test"
        # Persistent mode: no container creation, just an in-memory session.
        session = mod._sessions[result["session_id"]]
        assert session.status == "running"
        assert session.opencode_session_id == "oc-session-001"

    def test_allows_multiple_sessions_per_user(self, mock_opencode):
        mod = mock_opencode
        r1 = mod.start_session("+10000000000", "Task 1")
        r2 = mod.start_session("+10000000000", "Task 2")
        assert r1["status"] == "running"
        assert r2["status"] == "running"
        assert r1["session_id"] != r2["session_id"]
        assert len(mod._user_sessions["+10000000000"]) == 2

    def test_different_users_can_have_sessions(self, mock_opencode):
        mod = mock_opencode
        r1 = mod.start_session("+10000000000", "Task 1")
        r2 = mod.start_session("+10000000001", "Task 2")
        assert r1["status"] == "running"
        assert r2["status"] == "running"

    def test_custom_model(self, mock_opencode):
        mod = mock_opencode
        result = mod.start_session("+10000000000", "Task", model="openai/gpt-5")
        assert result["model"] == "openai/gpt-5"

    def test_max_concurrent_enforcement(self, mock_opencode):
        mod = mock_opencode
        for i in range(3):
            mod.start_session(f"+1000000000{i}", f"Task {i}")
        result = mod.start_session("+10000000003", "Task 3")
        assert "error" in result
        assert "Maximum" in result["error"]

    def test_not_ready_returns_error(self, sandbox_mod, monkeypatch):
        """If the persistent sandbox isn't responding, start_session errors."""
        mod = sandbox_mod
        monkeypatch.setattr(mod, "_wait_for_ready", lambda s, timeout=30: (False, "connection refused"))
        result = mod.start_session("+10000000000", "Task")
        assert "error" in result
        assert "not responding" in result["error"]


class TestSendMessage:
    def test_sends_message(self, mock_opencode):
        mod = mock_opencode
        mod.start_session("+10000000000", "Task")
        result = mod.send_message("+10000000000", "Add error handling")
        assert "response" in result
        assert result["response"]["content"] == "Done!"

    def test_model_override(self, mock_opencode):
        mod = mock_opencode
        mod.start_session("+10000000000", "Task")
        result = mod.send_message("+10000000000", "Try again", model="openai/gpt-5")
        assert result["model"] == "openai/gpt-5"

    def test_no_active_session(self, mock_opencode):
        mod = mock_opencode
        result = mod.send_message("+10000000000", "Hello")
        assert "error" in result

    def test_round_limit_enforced(self, mock_opencode):
        mod = mock_opencode
        # Set max_rounds low for testing via the active config.
        mod._cfg().max_rounds = 3

        mod.start_session("+10000000000", "Task")
        for i in range(3):
            result = mod.send_message("+10000000000", f"Message {i}")
            assert "response" in result
            assert result["rounds_used"] == i + 1

        # 4th message should be blocked
        result = mod.send_message("+10000000000", "One more")
        assert "error" in result
        assert "maximum" in result["error"].lower()

    def test_rounds_remaining_in_response(self, mock_opencode):
        mod = mock_opencode
        mod.start_session("+10000000000", "Task")
        result = mod.send_message("+10000000000", "First message")
        assert "rounds_remaining" in result
        assert result["rounds_remaining"] == mod._cfg().max_rounds - 1

    def test_timeout_does_not_consume_round(self, sandbox_mod, monkeypatch):
        """A timed-out message should NOT count against the round budget."""
        mod = sandbox_mod
        monkeypatch.setattr(mod, "_wait_for_ready", lambda s, timeout=30: (True, ""))
        monkeypatch.setattr(mod, "_create_opencode_session", lambda s, task: ("oc-001", ""))
        monkeypatch.setattr(
            mod, "_send_opencode_message",
            lambda s, msg, model=None: {"error": "Sandbox timed out waiting for response (5 min)"},
        )
        monkeypatch.setattr(mod, "_get_opencode_session", lambda s: {})
        monkeypatch.setattr(mod, "_export_opencode_session", lambda s: None)

        mod.start_session("+10000000000", "Task")
        result = mod.send_message("+10000000000", "msg 1")
        assert result["rounds_used"] == 0  # Not consumed on failure

    def test_consecutive_failures_auto_abort(self, sandbox_mod, monkeypatch):
        """After 3 consecutive timeouts, send_message returns auto_aborted."""
        mod = sandbox_mod
        monkeypatch.setattr(mod, "_wait_for_ready", lambda s, timeout=30: (True, ""))
        monkeypatch.setattr(mod, "_create_opencode_session", lambda s, task: ("oc-001", ""))
        monkeypatch.setattr(
            mod, "_send_opencode_message",
            lambda s, msg, model=None: {"error": "Sandbox timed out"},
        )
        monkeypatch.setattr(mod, "_get_opencode_session", lambda s: {})
        monkeypatch.setattr(mod, "_export_opencode_session", lambda s: None)

        mod.start_session("+10000000000", "Task")

        # First two failures — still allowed to continue
        for i in range(2):
            result = mod.send_message("+10000000000", f"msg {i}")
            assert "auto_aborted" not in result

        # Third failure — auto-abort triggered
        result = mod.send_message("+10000000000", "msg 2")
        assert result.get("auto_aborted") is True
        assert "auto-aborted" in result["error"].lower()

    def test_success_resets_consecutive_failures(self, sandbox_mod, monkeypatch):
        """A successful message resets the failure counter."""
        mod = sandbox_mod
        monkeypatch.setattr(mod, "_wait_for_ready", lambda s, timeout=30: (True, ""))
        monkeypatch.setattr(mod, "_create_opencode_session", lambda s, task: ("oc-001", ""))
        monkeypatch.setattr(mod, "_get_opencode_session", lambda s: {})
        monkeypatch.setattr(mod, "_export_opencode_session", lambda s: None)

        call_count = [0]

        def _alternating(s, msg, model=None):
            call_count[0] += 1
            if call_count[0] <= 2:
                return {"error": "Sandbox timed out"}
            return {"response": "Success!", "raw": {}}

        monkeypatch.setattr(mod, "_send_opencode_message", _alternating)

        mod.start_session("+10000000000", "Task")

        # Two failures
        mod.send_message("+10000000000", "fail 1")
        mod.send_message("+10000000000", "fail 2")

        # Success — resets counter
        result = mod.send_message("+10000000000", "success")
        assert "error" not in result
        assert result["rounds_used"] == 1  # Only successful round counted

        # Session should have consecutive_failures reset to 0
        session_ids = mod._user_sessions["+10000000000"]
        session = mod._sessions[session_ids[-1]]
        assert session.consecutive_failures == 0


class TestReviewSession:
    def test_review_returns_status(self, mock_opencode, tmp_path):
        mod = mock_opencode
        r = mod.start_session("+10000000000", "Task")

        # Create a file in the session workspace
        session_dir = os.path.join(str(tmp_path), "10000000000", "active", "sessions", r["session_id"])
        os.makedirs(session_dir, exist_ok=True)
        with open(os.path.join(session_dir, "main.py"), "w") as f:
            f.write("print('hello')")

        result = mod.review_session("+10000000000")
        assert result["status"] == "running"
        assert "main.py" in result["files"]
        assert result["elapsed_seconds"] >= 0

    def test_no_session(self, mock_opencode):
        result = mock_opencode.review_session("+10000000000")
        assert "error" in result


class TestFinishSession:
    def test_archives_and_cleans_up(self, mock_opencode, tmp_path):
        mod = mock_opencode
        r = mod.start_session("+10000000000", "Task")
        session_id = r["session_id"]

        # Create a file in the session workspace
        session_dir = os.path.join(str(tmp_path), "10000000000", "active", "sessions", session_id)
        os.makedirs(session_dir, exist_ok=True)
        with open(os.path.join(session_dir, "main.py"), "w") as f:
            f.write("print('hello')")

        result = mod.finish_session("+10000000000", summary="Built a calculator")
        assert result["status"] == "finished"
        assert session_id not in mod._sessions
        assert not mod._user_sessions.get("+10000000000")

    def test_no_session(self, mock_opencode):
        result = mock_opencode.finish_session("+10000000000")
        assert "error" in result


class TestAbortSession:
    def test_aborts_and_cleans_up(self, mock_opencode):
        mod = mock_opencode
        r = mod.start_session("+10000000000", "Task")
        session_id = r["session_id"]

        result = mod.abort_session("+10000000000")
        assert result["status"] == "aborted"
        assert session_id not in mod._sessions
        assert not mod._user_sessions.get("+10000000000")

    def test_no_session(self, mock_opencode):
        result = mock_opencode.abort_session("+10000000000")
        assert "error" in result


class TestSearchSolutions:
    def test_finds_matching_solutions(self, mock_opencode, tmp_path):
        mod = mock_opencode
        # Create a fake archived solution
        solution_dir = os.path.join(str(tmp_path), "10000000000", "archive", "code", "abc123")
        os.makedirs(solution_dir, exist_ok=True)
        with open(os.path.join(solution_dir, "SOLUTION.md"), "w") as f:
            f.write("## Solution: abc123\nBuilt a beamer presentation from PDF\n")

        results = mod.search_solutions("+10000000000", "beamer")
        assert len(results) >= 1
        assert "beamer" in results[0]["snippet"].lower()

    def test_no_results(self, mock_opencode):
        results = mock_opencode.search_solutions("+10000000000", "nonexistent")
        assert results == []


class TestExecuteSolution:
    def test_starts_new_session_from_archive(self, mock_opencode, tmp_path):
        mod = mock_opencode
        solution_dir = os.path.join(str(tmp_path), "10000000000", "archive", "code", "abc123")
        os.makedirs(solution_dir, exist_ok=True)
        with open(os.path.join(solution_dir, "SOLUTION.md"), "w") as f:
            f.write("## Solution\nRun: python main.py\n")

        result = mod.execute_solution("+10000000000", "abc123")
        assert result["status"] == "running"

    def test_not_found(self, mock_opencode):
        result = mock_opencode.execute_solution("+10000000000", "nonexistent")
        assert "error" in result


class TestCleanupStale:
    def test_clears_in_memory_sessions(self, mock_opencode):
        mod = mock_opencode
        mod.start_session("+10000000000", "Task")
        # Persistent mode: cleanup clears in-memory bookkeeping only.
        count = mod.cleanup_stale_sessions()
        assert count >= 1
        assert not mod._sessions


class TestRuntimeMode:
    def test_runtime_mode_is_persistent(self, sandbox_mod):
        assert "persistent" in sandbox_mod.get_runtime_mode()
