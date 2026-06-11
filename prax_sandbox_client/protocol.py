"""Public types for the sandbox client — the shape callers code against.

Kept transport-agnostic and free of ephemeral/docker internals so any harness
(or a future remote control daemon) can satisfy the contract. Notably there are
no ``container_id`` / ``host_port`` fields — those were ephemeral-mode docker
internals and are not part of the public session shape.
"""
from __future__ import annotations

from typing import Protocol, TypedDict, runtime_checkable


@runtime_checkable
class SandboxSession(Protocol):
    """The public shape of a sandbox coding session.

    Only these fields are part of the contract; internal bookkeeping (timers,
    OpenCode handles) stays private to the control-plane implementation.
    """

    session_id: str
    user_id: str
    model: str
    created_at: float
    status: str          # starting | running | finished | aborted | timed_out
    rounds_used: int
    max_rounds: int


class ExecResult(TypedDict, total=False):
    """Result of ``run_shell``."""

    stdout: str
    stderr: str
    exit_code: int
    error: str
