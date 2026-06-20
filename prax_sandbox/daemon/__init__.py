"""Optional remote control daemon for prax-sandbox.

A standalone, harness-agnostic HTTPS service that fronts the control plane with a
bearer token so a harness can drive a sandbox on a remote box (the ``HttpTransport``
in :mod:`prax_sandbox_client`). Requires the ``[daemon]`` extra
(``pip install prax-sandbox[daemon]``); nothing here is imported on the client path.

Run it with ``prax-sandbox-daemon`` (see :mod:`prax_sandbox.daemon.__main__`).
"""

__all__ = ["build_app", "DaemonConfig"]


def __getattr__(name):
    # Lazy — importing the daemon package never eagerly loads FastAPI/uvicorn.
    if name == "build_app":
        from prax_sandbox.daemon.app import build_app
        return build_app
    if name == "DaemonConfig":
        from prax_sandbox.daemon.config import DaemonConfig
        return DaemonConfig
    raise AttributeError(name)
