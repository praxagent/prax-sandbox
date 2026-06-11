"""Plug-and-play client for the Prax sandbox control plane.

A harness drives the sandbox through :class:`SandboxClient` (or the process-wide
:func:`get_client`), configured with a :class:`SandboxConfig`. The client is
harness-agnostic: it has no import-time dependency on prax, so any agentic
harness can adopt it.
"""
from prax_sandbox_client.client import SandboxClient, get_client
from prax_sandbox_client.config import SandboxConfig
from prax_sandbox_client.protocol import ExecResult, SandboxSession

__all__ = [
    "SandboxClient",
    "SandboxConfig",
    "SandboxSession",
    "ExecResult",
    "get_client",
]
