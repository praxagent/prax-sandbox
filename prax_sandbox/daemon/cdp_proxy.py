"""Authenticated CDP proxy — the ONLY network path to Chrome DevTools.

Raw CDP (:9223) is removed from the image; Chrome's :9222 is loopback-only. The
daemon fronts it with bearer auth so unauthenticated callers can't reach CDP
(which is arbitrary code execution + local file read). The load-bearing security
invariant: the bearer is validated BEFORE any upstream socket is opened.
"""
from __future__ import annotations

import asyncio

from fastapi import APIRouter, Depends, HTTPException, Request, WebSocket
from fastapi.responses import JSONResponse

from prax_sandbox.daemon.auth import check_bearer

# Chrome identifies itself as 127.0.0.1:9222 regardless of how the daemon reaches
# it (direct or via socat), and is launched with --remote-allow-origins set to
# exactly this. We always present it — never a client-controlled Origin.
_CHROME_ORIGIN = "http://127.0.0.1:9222"


async def _connect_upstream(cfg, path: str):
    """Open the upstream CDP WebSocket. Separated so tests can assert it is
    NEVER called when auth fails."""
    import websockets
    chrome = f"{cfg.cdp_host}:{cfg.cdp_port}"
    url = f"ws://{chrome}/{path.lstrip('/')}"
    return await websockets.connect(url, origin=_CHROME_ORIGIN, max_size=None)


def build_cdp_router(cfg, require_auth) -> APIRouter:
    router = APIRouter(prefix="/v1/cdp", tags=["cdp"])
    chrome = f"{cfg.cdp_host}:{cfg.cdp_port}"

    def _rewrite(body: str, public_base: str) -> str:
        # Point devtools websocket URLs at the authenticated daemon proxy.
        return (
            body.replace(f"ws://{chrome}/", f"{public_base}/v1/cdp/ws/")
                .replace("ws://localhost:9222/", f"{public_base}/v1/cdp/ws/")
                .replace("ws://127.0.0.1:9222/", f"{public_base}/v1/cdp/ws/")
        )

    @router.get("/json", dependencies=[Depends(require_auth)])
    @router.get("/json/version", dependencies=[Depends(require_auth)])
    async def cdp_json(request: Request):
        import httpx
        upstream = f"http://{chrome}" + request.url.path.replace("/v1/cdp", "")
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                r = await client.get(upstream, headers={"Host": chrome})
        except Exception as e:
            raise HTTPException(status_code=502, detail=f"chrome unreachable: {type(e).__name__}") from None
        scheme = "wss" if request.url.scheme in ("https", "wss") else "ws"
        public_base = f"{scheme}://{request.url.netloc}"
        return JSONResponse(content=_safe_json(_rewrite(r.text, public_base)))

    @router.websocket("/ws/{path:path}")
    async def cdp_ws(websocket: WebSocket, path: str):
        # AUTH BEFORE DIAL: validate the bearer from the upgrade headers and
        # reject WITHOUT opening the upstream socket if it fails.
        if not check_bearer(websocket.headers.get("authorization"), cfg.bearer_token):
            await websocket.close(code=1008)  # policy violation
            return
        await websocket.accept()
        try:
            upstream = await _connect_upstream(cfg, path)
        except Exception:
            await websocket.close(code=1011)
            return
        await _bridge(websocket, upstream)

    return router


async def _bridge(ws: WebSocket, upstream) -> None:
    """Pump frames both directions until either side closes."""
    async def client_to_upstream():
        try:
            while True:
                msg = await ws.receive_text()
                await upstream.send(msg)
        except Exception:
            pass

    async def upstream_to_client():
        try:
            async for msg in upstream:
                await ws.send_text(msg if isinstance(msg, str) else msg.decode("utf-8", "ignore"))
        except Exception:
            pass

    t1 = asyncio.create_task(client_to_upstream())
    t2 = asyncio.create_task(upstream_to_client())
    try:
        await asyncio.wait({t1, t2}, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for t in (t1, t2):
            t.cancel()
        try:
            await upstream.close()
        except Exception:
            pass


def _safe_json(text: str):
    import json
    try:
        return json.loads(text)
    except Exception:
        return {"raw": text[:2000]}
