"""Entry point: ``prax-sandbox-daemon`` (and ``python -m prax_sandbox.daemon``).

Reads config from the environment, fails closed (no bearer token / non-loopback
plaintext bind), and serves the app over TLS via uvicorn (single process — the
control plane holds module-global state).
"""
from __future__ import annotations

import logging


def main() -> None:
    import uvicorn

    from prax_sandbox.daemon.app import build_app
    from prax_sandbox.daemon.config import DaemonConfig

    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    cfg = DaemonConfig.from_env()
    cfg.validate_or_die()

    ssl_kw: dict = {}
    if cfg.tls_cert and cfg.tls_key:
        ssl_kw["ssl_certfile"] = cfg.tls_cert
        ssl_kw["ssl_keyfile"] = cfg.tls_key
        if cfg.mtls_ca:
            import ssl
            ssl_kw["ssl_ca_certs"] = cfg.mtls_ca
            ssl_kw["ssl_cert_reqs"] = ssl.CERT_REQUIRED  # opt-in mTLS

    logging.getLogger("prax_sandbox.daemon").info(
        "prax-sandbox-daemon listening on %s:%s (tls=%s, mtls=%s)",
        cfg.bind_host, cfg.port, bool(ssl_kw), bool(cfg.mtls_ca),
    )
    uvicorn.run(
        build_app(cfg), host=cfg.bind_host, port=cfg.port, workers=1,
        log_level="info", timeout_keep_alive=cfg.request_timeout, **ssl_kw,
    )


if __name__ == "__main__":
    main()
