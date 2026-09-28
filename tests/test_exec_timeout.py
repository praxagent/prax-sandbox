"""exec_in_sandbox enforces its timeout only when the config asks it to."""
from __future__ import annotations

import pytest

from prax_sandbox import exec as sandbox_exec
from prax_sandbox_client.config import SandboxConfig


class _FakeContainer:
    def __init__(self, exit_code=0, out=b"", err=b""):
        self.exit_code, self.out, self.err = exit_code, out, err
        self.argv = None

    def exec_run(self, argv, demux, environment):
        self.argv = argv
        return self.exit_code, (self.out, self.err)


@pytest.fixture
def container(monkeypatch):
    fake = _FakeContainer()
    monkeypatch.setattr(sandbox_exec, "find_sandbox_container", lambda config=None: fake)
    return fake


def test_default_config_runs_without_a_deadline(container):
    sandbox_exec.exec_in_sandbox(["echo", "hi"], timeout=7, config=SandboxConfig())
    assert container.argv == ["sh", "-c", "echo hi"]


def test_no_config_runs_without_a_deadline(container):
    sandbox_exec.exec_in_sandbox(["echo", "hi"], timeout=7)
    assert container.argv == ["sh", "-c", "echo hi"]


def test_enforced_wraps_the_whole_shell_in_timeout(container):
    cfg = SandboxConfig(enforce_exec_timeout=True)
    sandbox_exec.exec_in_sandbox(["echo", "a b"], cwd="/workspace/x", timeout=7, config=cfg)
    assert container.argv == [
        "timeout", "-k", "5", "7", "sh", "-c", "cd /workspace/x && echo 'a b'",
    ]


@pytest.mark.parametrize("timeout", [0, None])
def test_enforced_with_no_positive_timeout_is_unbounded(container, timeout):
    cfg = SandboxConfig(enforce_exec_timeout=True)
    sandbox_exec.exec_in_sandbox(["true"], timeout=timeout, config=cfg)
    assert container.argv == ["sh", "-c", "true"]


def test_timed_out_result_says_so(container):
    container.exit_code, container.err = 124, b"partial"
    cfg = SandboxConfig(enforce_exec_timeout=True)
    r = sandbox_exec.exec_in_sandbox(["sleep", "99"], timeout=3, config=cfg)
    assert r.returncode == 124
    assert r.stderr.startswith("partial")
    assert "timed out after 3s" in r.stderr


def test_exit_124_is_not_relabelled_when_not_enforced(container):
    # Without enforcement 124 is just the command's own exit status.
    container.exit_code = 124
    r = sandbox_exec.exec_in_sandbox(["false"], timeout=3, config=SandboxConfig())
    assert "timed out" not in r.stderr


def test_daemon_config_forwards_the_flag():
    from prax_sandbox.daemon.config import DaemonConfig

    on = DaemonConfig.from_env({"PRAX_SANDBOX_ENFORCE_EXEC_TIMEOUT": "true"})
    off = DaemonConfig.from_env({})
    assert on.to_sandbox_config().enforce_exec_timeout is True
    assert off.to_sandbox_config().enforce_exec_timeout is False


def test_a_command_killed_after_ignoring_term_is_still_reported_as_timed_out(container, monkeypatch):
    t = iter([0.0, 12.0])  # started, finished: past the 7 s deadline
    monkeypatch.setattr(sandbox_exec.time, "monotonic", lambda: next(t))
    container.exit_code = 137
    r = sandbox_exec.exec_in_sandbox(["x"], timeout=7, config=SandboxConfig(enforce_exec_timeout=True))
    assert "timed out after 7s" in r.stderr


def test_an_early_137_is_not_called_a_timeout(container, monkeypatch):
    t = iter([0.0, 1.0])  # killed well before the deadline: e.g. the memory limit
    monkeypatch.setattr(sandbox_exec.time, "monotonic", lambda: next(t))
    container.exit_code = 137
    r = sandbox_exec.exec_in_sandbox(["x"], timeout=7, config=SandboxConfig(enforce_exec_timeout=True))
    assert "timed out" not in r.stderr
