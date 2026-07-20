"""FastAPI app for the remote control daemon.

One route per control-plane method (run in a threadpool — the control plane is
synchronous), plus the confined file API and the authenticated CDP proxy. Every
``/v1`` route requires the bearer token; ``/healthz`` is unauthenticated but
loopback-only and leaks nothing.
"""
from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import Body, Depends, FastAPI, HTTPException, Query, Request, Response
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse, StreamingResponse

from prax_sandbox import control_plane
from prax_sandbox import fileops as fileapi
from prax_sandbox.daemon.auth import make_bearer_dependency
from prax_sandbox.daemon.cdp_proxy import build_cdp_router

logger = logging.getLogger("prax_sandbox.daemon")

_LOCAL_CAPS = {
    "persistent": True, "shell": True, "install": True, "rebuild": True,
    "desktop": True, "browser_cdp": True, "file_api": True, "remote": True,
}


def _session_dict(s) -> dict:
    return {
        "session_id": s.session_id, "user_id": s.user_id, "model": s.model,
        "created_at": s.created_at, "status": s.status,
        "rounds_used": s.rounds_used, "max_rounds": s.max_rounds,
    }


class _Limiter:
    """Non-blocking concurrency cap — 429 instead of unbounded queueing."""

    def __init__(self, n: int):
        self._sem = asyncio.Semaphore(n)

    def busy(self) -> bool:
        return self._sem.locked()

    async def acquire(self) -> None:
        await self._sem.acquire()

    def release(self) -> None:
        self._sem.release()

    async def __aenter__(self):
        if self._sem.locked():
            raise HTTPException(status_code=429, detail="too many concurrent operations")
        await self._sem.acquire()
        return self

    async def __aexit__(self, *exc):
        self._sem.release()


def build_app(cfg) -> FastAPI:
    require_auth = make_bearer_dependency(cfg.bearer_token)
    exec_limit = _Limiter(cfg.max_concurrent_exec)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        # Configure the control plane with the daemon's config BEFORE serving.
        # The container boots OpenCode with the same internal password (persisted
        # env), so the client+server never have a mismatched-auth window.
        control_plane.configure(cfg.to_sandbox_config())
        if cfg.opencode_password:
            await run_in_threadpool(_warn_if_opencode_unauthenticated, cfg)
        yield

    app = FastAPI(title="prax-sandbox daemon", version="1", lifespan=lifespan)

    # --- payload cap (defense against memory DoS on writes/tar) ---
    @app.middleware("http")
    async def _limit_payload(request: Request, call_next):
        cl = request.headers.get("content-length")
        if cl and cl.isdigit() and int(cl) > cfg.max_payload_bytes:
            return JSONResponse(status_code=413, content={"detail": "payload too large"})
        return await call_next(request)

    auth = [Depends(require_auth)]

    # --- health ---
    @app.get("/healthz")
    async def healthz():
        # Unauthenticated bare liveness for the container healthcheck (curl
        # localhost). Leaks nothing about the sandbox; restrict at the
        # proxy/firewall if you want it off the network entirely.
        return Response(status_code=200)

    @app.get("/v1/health", dependencies=auth)
    async def health():
        ok = await run_in_threadpool(control_plane.health)
        return {"status": "ok" if ok else "sandbox-unreachable", "sandbox": bool(ok)}

    @app.get("/v1/capabilities", dependencies=auth)
    async def capabilities():
        return {**_LOCAL_CAPS, "rebuild": cfg.allow_rebuild}

    @app.get("/v1/runtime-mode", dependencies=auth)
    async def runtime_mode():
        return {"mode": await run_in_threadpool(control_plane.get_runtime_mode)}

    # (OpenCode coding-SESSION routes /v1/sessions/* removed — pure exec env.)

    # --- shell / exec / packages ---
    @app.post("/v1/shell", dependencies=auth)
    async def run_shell(body: dict = Body(...)):
        async with exec_limit:
            return await run_in_threadpool(
                control_plane.run_shell, body["command"], body.get("timeout", 60))

    @app.post("/v1/exec", dependencies=auth)
    async def run_exec(body: dict = Body(...)):
        async with exec_limit:
            cp = await run_in_threadpool(
                control_plane.run_command, body["cmd"],
                cwd=body.get("cwd"), env=body.get("env"), timeout=body.get("timeout", 300))
        return {"args": cp.args, "returncode": cp.returncode, "stdout": cp.stdout, "stderr": cp.stderr}

    @app.post("/v1/packages", dependencies=auth)
    async def install_package(body: dict = Body(...)):
        return await run_in_threadpool(control_plane.install_package, body["package_name"])

    @app.post("/v1/rebuild", dependencies=auth)
    async def rebuild(body: dict = Body(...)):
        if not cfg.allow_rebuild:
            raise HTTPException(status_code=403, detail="image rebuild is disabled on this daemon")
        return await run_in_threadpool(control_plane.rebuild_sandbox, body.get("dockerfile_content"))

    # (OpenCode solutions-archive + stale-session cleanup routes removed.)

    # --- file API (confined per-user) ---
    import threading
    _file_locks: dict[str, threading.Lock] = {}
    _file_locks_guard = threading.Lock()

    def _with_user_lock(user_id, fn, *args):
        # Serialize tar push/pull per user so concurrent same-user sessions
        # can't clobber active/ or read a half-written tree.
        with _file_locks_guard:
            lock = _file_locks.setdefault(user_id, threading.Lock())
        with lock:
            return fn(*args)

    def _root(user_id: str) -> str:
        try:
            return fileapi.resolve_user_root(cfg.workspace_dir, user_id)
        except fileapi.PathEscape as e:
            raise HTTPException(status_code=400, detail=str(e)) from None

    @app.get("/v1/files/list", dependencies=auth)
    async def files_list(user_id: str, path: str = "", recursive: bool = False):
        root = _root(user_id)
        try:
            return {"root": root, "entries": await run_in_threadpool(fileapi.list_dir, root, path, recursive)}
        except fileapi.PathEscape as e:
            raise HTTPException(status_code=400, detail=str(e)) from None

    @app.get("/v1/files/read", dependencies=auth)
    async def files_read(user_id: str, path: str, max_bytes: int = Query(default=10_000_000)):
        root = _root(user_id)
        try:
            data = await run_in_threadpool(fileapi.read_file, root, path, min(max_bytes, cfg.max_payload_bytes))
        except fileapi.PathEscape as e:
            raise HTTPException(status_code=400, detail=str(e)) from None
        except FileNotFoundError:
            raise HTTPException(status_code=404, detail="not found") from None
        except ValueError as e:
            raise HTTPException(status_code=413, detail=str(e)) from None
        return Response(content=data, media_type="application/octet-stream")

    @app.put("/v1/files/write", dependencies=auth)
    async def files_write(user_id: str, path: str, request: Request):
        root = _root(user_id)
        data = await request.body()
        try:
            n = await run_in_threadpool(fileapi.write_file, root, path, data)
        except fileapi.PathEscape as e:
            raise HTTPException(status_code=400, detail=str(e)) from None
        return {"path": path, "bytes": n}

    @app.post("/v1/files/grep", dependencies=auth)
    async def files_grep(body: dict = Body(...)):
        root = _root(body["user_id"])
        try:
            results = await run_in_threadpool(
                fileapi.grep, root, body["query"], body.get("path", ""),
                body.get("include", "*"), body.get("max_count", 200))
        except fileapi.PathEscape as e:
            raise HTTPException(status_code=400, detail=str(e)) from None
        return {"results": results}

    @app.get("/v1/files/pull_tar", dependencies=auth)
    async def files_pull(user_id: str, path: str = ""):
        root = _root(user_id)
        try:
            data = await run_in_threadpool(_with_user_lock, user_id, fileapi.pull_tar, root, path)
        except fileapi.PathEscape as e:
            raise HTTPException(status_code=400, detail=str(e)) from None
        return StreamingResponse(iter([data]), media_type="application/x-tar")

    @app.put("/v1/files/push_tar", dependencies=auth)
    async def files_push(user_id: str, request: Request, path: str = ""):
        root = _root(user_id)
        data = await request.body()
        try:
            n = await run_in_threadpool(_with_user_lock, user_id, fileapi.push_tar, root, data, path)
        except fileapi.PathEscape as e:
            raise HTTPException(status_code=400, detail=str(e)) from None
        return {"extracted": n}

    # --- CDP proxy (authenticated; the only network path to Chrome) ---
    app.include_router(build_cdp_router(cfg, require_auth))

    return app


def _warn_if_opencode_unauthenticated(cfg) -> None:
    """Best-effort: if OpenCode answers WITHOUT the password, log loudly."""
    try:
        import requests
        r = requests.get(f"http://{cfg.opencode_host}:4096/global/health", timeout=2)
        if r.status_code == 200:
            logger.warning(
                "OpenCode answered /global/health WITHOUT auth — it may be running "
                "password-less. Ensure OPENCODE_SERVER_PASSWORD is set in the container."
            )
    except Exception:
        pass  # container may still be starting; not fatal
