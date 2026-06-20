"""Bearer-token auth for the daemon.

Enforced by the daemon ITSELF on every route (including the CDP WebSocket
upgrade), independent of any network ACL — so it holds with or without
Tailscale. mTLS (when a CA is configured) is verified at the TLS layer by
uvicorn and is layered ON TOP of the bearer, never a replacement.
"""
from __future__ import annotations

import hmac

from fastapi import Header, HTTPException, status


def check_bearer(authorization: str | None, expected_token: str) -> bool:
    """Constant-time bearer check. Returns True iff the header presents the token."""
    if not authorization:
        return False
    scheme, _, presented = authorization.partition(" ")
    if scheme.lower() != "bearer" or not presented:
        return False
    # compare_digest on equal-typed bytes; length differences don't short-circuit.
    return hmac.compare_digest(presented.encode("utf-8"), expected_token.encode("utf-8"))


def make_bearer_dependency(expected_token: str):
    """FastAPI dependency that 401s unless a valid bearer token is presented."""

    def require_bearer(authorization: str | None = Header(default=None)) -> None:
        if not check_bearer(authorization, expected_token):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="missing or invalid bearer token",
                headers={"WWW-Authenticate": "Bearer"},
            )

    return require_bearer
