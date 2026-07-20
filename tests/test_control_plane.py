"""Tests for the control plane — the EXECUTION primitives.

The sandbox is a pure execution environment (the OpenCode coding-session code
was removed 2026-07). These tests cover the exec surface — run_shell,
run_command, install_package, health, get_runtime_mode — with the docker/exec
layer mocked so no container is needed.
"""
import importlib
import subprocess

import pytest

from prax_sandbox_client import SandboxConfig


@pytest.fixture()
def cp(monkeypatch, tmp_path):
    """Reload the control plane, configured for persistent mode."""
    module = importlib.reload(importlib.import_module("prax_sandbox.control_plane"))
    module._config = None
    module.configure(SandboxConfig(persistent=True, host="sandbox", workspace_dir=str(tmp_path)))
    return module


def test_run_shell_returns_structured_output(cp, monkeypatch):
    monkeypatch.setattr(cp, "exec_in_sandbox",
                        lambda cmd, timeout=60, config=None: subprocess.CompletedProcess(cmd, 0, "hi\n", ""))
    out = cp.run_shell("echo hi")
    assert out == {"stdout": "hi\n", "stderr": "", "exit_code": 0}


def test_run_shell_error_is_captured(cp, monkeypatch):
    def boom(cmd, timeout=60, config=None):
        raise RuntimeError("no container")
    monkeypatch.setattr(cp, "exec_in_sandbox", boom)
    out = cp.run_shell("ls")
    assert out["exit_code"] == -1 and "no container" in out["error"]


def test_run_command_returns_completedprocess(cp, monkeypatch):
    monkeypatch.setattr(cp, "exec_in_sandbox",
                        lambda cmd, cwd=None, env=None, timeout=300, config=None:
                        subprocess.CompletedProcess(cmd, 0, "out", ""))
    r = cp.run_command(["echo", "hi"])
    assert isinstance(r, subprocess.CompletedProcess) and r.returncode == 0


def test_install_package_local_mode_refuses(monkeypatch, tmp_path):
    module = importlib.reload(importlib.import_module("prax_sandbox.control_plane"))
    module._config = None
    module.configure(SandboxConfig(persistent=False, workspace_dir=str(tmp_path)))
    out = module.install_package("jq")
    assert "error" in out and "local mode" in out["error"].lower()


def test_install_package_rejects_bad_name(cp):
    assert "Invalid package name" in cp.install_package("; rm -rf /")["error"]


def test_get_runtime_mode(cp):
    assert "docker" in cp.get_runtime_mode()


def test_health_probes_exec(cp, monkeypatch):
    monkeypatch.setattr(cp, "exec_in_sandbox",
                        lambda cmd, timeout=5, config=None: subprocess.CompletedProcess(cmd, 0, "", ""))
    assert cp.health() is True


def test_health_false_when_exec_fails(cp, monkeypatch):
    def boom(cmd, timeout=5, config=None):
        raise RuntimeError("down")
    monkeypatch.setattr(cp, "exec_in_sandbox", boom)
    assert cp.health() is False
