"""The egress gate: the sandbox's only way out, deciding every connection.

With ``docker-compose.egress.yml`` the sandbox container sits on an internal
Docker network with no route to anywhere. This process, on that network *and*
the outside one, is the only path out. Every request is decided before a
single byte leaves:

* **HTTPS** arrives as ``CONNECT host:port`` and is judged on host and port (the
  method and path are inside TLS; see ``policy.py``).
* **Plain HTTP** arrives in absolute form and is judged on host, port, method
  and path.
* **deny** → ``403`` with the reason, and **no DNS lookup**: a secret spelled
  into a hostname cannot leave as a DNS query.
* **allow** → only now is the host resolved, *here*; every address must be
  public (no loopback, private, link-local, multicast or reserved ranges — so
  a public-looking name that resolves inside cannot be used for SSRF), and the
  connection is made to the address that was checked, never re-resolved.
  Plain HTTP goes out with the ``Host`` header set to the judged host.
* **ask** → the request waits while the harness puts the question to a person
  (the admin API below); concurrent requests for the same destination share
  one question, and the answer is cached for a while so one page load does not
  become fifty prompts. No answer in time → deny.

The admin API (a second port, bearer-token only, published on loopback for
the harness) lists pending questions, records answers, and sets the taint
flag the policy's ``clean_only`` rules depend on.

Every decision is logged as one JSON line on stdout.
"""
from __future__ import annotations

import asyncio
import hmac
import ipaddress
import itertools
import json
import logging
import socket
import time
from collections import deque
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from prax_sandbox.egress_gate.policy import ALLOW, ASK, DENY, Policy

logger = logging.getLogger("egress_gate")

_MAX_HEAD = 64 * 1024
_COPY_CHUNK = 64 * 1024


def is_public(ip: str) -> bool:
    addr = ipaddress.ip_address(ip)
    if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped:
        addr = addr.ipv4_mapped
    return addr.is_global and not addr.is_multicast


def _dest_key(host: str, port: int, method: str | None, path: str | None) -> str:
    """What an answer covers. HTTPS: host and port (method and path are inside
    TLS). Plain HTTP: also the method and path, so allowing ``GET /status`` does
    not allow ``POST /upload`` to the same host."""
    base = f"{host.lower()}:{port}"
    return base if method is None else f"{base} {method.upper()} {path or '/'}"


@dataclass
class Pending:
    id: str
    key: str
    host: str
    port: int
    method: str | None
    path: str | None
    tainted: bool
    created: float = field(default_factory=time.monotonic)
    future: asyncio.Future | None = None


@dataclass
class GateConfig:
    policy: Policy
    admin_token: str
    # Raise-only credential for the party whose traffic is being judged (the
    # harness): it may mark the sandbox tainted, never answer a question and
    # never clear taint. The admin token belongs to whoever relays a PERSON's
    # answers — not to the agent, or the agent could approve its own requests.
    taint_token: str = ""
    ask_timeout: float = 120.0
    allow_ttl: float = 600.0     # how long a person's "allow" covers a destination
    deny_ttl: float = 60.0       # how long a "deny"/no-answer is remembered
    taint_ttl: float = 900.0     # taint lapses if the harness stops refreshing it
    max_connections: int = 256


class Gate:
    def __init__(self, config: GateConfig, resolver=None):
        self.cfg = config
        self._resolver = resolver or self._resolve
        # dest -> (action, expires, why, granted_while_tainted)
        self._decisions: dict[str, tuple[str, float, str, bool]] = {}
        self._pending: dict[str, Pending] = {}                   # id -> question
        self._by_dest: dict[str, str] = {}                        # dest -> pending id
        self._ids = itertools.count(1)
        self._tainted_until = 0.0
        self._taint_reason = ""
        self._log: deque[dict] = deque(maxlen=500)
        self._slots = asyncio.Semaphore(config.max_connections)

    # --- taint ---------------------------------------------------------------

    @property
    def tainted(self) -> bool:
        return time.monotonic() < self._tainted_until

    def set_taint(self, tainted: bool, reason: str = "", ttl: float | None = None,
                  raise_only: bool = False) -> None:
        was = self.tainted
        if tainted:
            until = time.monotonic() + (ttl or self.cfg.taint_ttl)
            # A raise-only caller can extend taint, never shorten it.
            self._tainted_until = max(self._tainted_until, until) if raise_only else until
            self._taint_reason = reason
            if not was:
                # A person's earlier "allow" was given while nothing private had
                # been read; do not carry it into a tainted period.
                self._decisions = {k: v for k, v in self._decisions.items() if v[0] != ALLOW}
        else:
            self._tainted_until = 0.0
            self._taint_reason = ""

    # --- decisions -------------------------------------------------------------

    def policy(self, host: str, port: int, method: str | None):
        return self.cfg.policy.decide(host, port, method, tainted=self.tainted)

    async def decide(self, host: str, port: int, method: str | None, path: str | None) -> tuple[str, str]:
        tainted = self.tainted
        verdict = self.cfg.policy.decide(host, port, method, tainted=tainted)
        if verdict.action != ASK:
            return verdict.action, verdict.reason
        dest = _dest_key(host, port, method, path)
        cached = self._decisions.get(dest)
        if cached and time.monotonic() < cached[1]:
            action, _, why, granted_tainted = cached
            # An allow given while nothing private had been read does not carry
            # into a tainted period: ask again.
            if not (action == ALLOW and tainted and not granted_tainted):
                return action, f"remembered: {why}"
        return await self._ask(dest, host, port, method, path, tainted)

    async def _ask(self, dest, host, port, method, path, tainted) -> tuple[str, str]:
        pid = self._by_dest.get(dest)
        pending = self._pending.get(pid) if pid else None
        if pending is None:
            pending = Pending(id=str(next(self._ids)), key=dest, host=host, port=port,
                              method=method, path=path, tainted=tainted,
                              future=asyncio.get_running_loop().create_future())
            self._pending[pending.id] = pending
            self._by_dest[dest] = pending.id
        remaining = max(0.0, self.cfg.ask_timeout - (time.monotonic() - pending.created))
        try:
            action, why = await asyncio.wait_for(asyncio.shield(pending.future), remaining)
        except TimeoutError:
            # Resolve for EVERY waiter: a late joiner must not wait on a question
            # no one can see or answer any more.
            self._forget(pending, dest)
            self._decisions[dest] = (DENY, time.monotonic() + self.cfg.deny_ttl, "no answer", tainted)
            if not pending.future.done():
                pending.future.set_result((DENY, "asked; no answer in time"))
            return DENY, "asked; no answer in time"
        return action, why

    def _forget(self, pending: Pending, dest: str) -> None:
        self._pending.pop(pending.id, None)
        if self._by_dest.get(dest) == pending.id:
            self._by_dest.pop(dest, None)

    def answer(self, pending_id: str, allow: bool, ttl: float | None = None, by: str = "") -> bool:
        pending = self._pending.get(pending_id)
        if pending is None:
            return False
        dest = pending.key
        action = ALLOW if allow else DENY
        why = f"{'allowed' if allow else 'denied'} by {by or 'the harness'}"
        if allow and self.tainted and not pending.tainted:
            # The person answered a question asked while the sandbox was clean;
            # it has read private data since. Refuse this request but remember
            # nothing, so the retry is a fresh question, not a cached deny.
            action, why = DENY, "tainted since this was asked; the next attempt asks again"
        else:
            hold = ttl if ttl is not None else (self.cfg.allow_ttl if action == ALLOW else self.cfg.deny_ttl)
            self._decisions[dest] = (action, time.monotonic() + hold, why, pending.tainted)
        self._forget(pending, dest)
        if pending.future and not pending.future.done():
            pending.future.set_result((action, why))
        return True

    def pending(self) -> list[dict]:
        now = time.monotonic()
        return [{"id": p.id, "host": p.host, "port": p.port, "method": p.method,
                 "path": p.path, "tainted": p.tainted, "age_seconds": round(now - p.created, 1),
                 # How long an answer can still make a difference.
                 "expires_in_seconds": max(0.0, round(self.cfg.ask_timeout - (now - p.created), 1))}
                for p in self._pending.values()]

    # --- resolution (the SSRF check) --------------------------------------------

    async def _resolve(self, host: str, port: int) -> list[str]:
        infos = await asyncio.get_running_loop().getaddrinfo(
            host, port, type=socket.SOCK_STREAM)
        return list(dict.fromkeys(info[4][0] for info in infos))

    def literal_refusal(self, host: str) -> str:
        """Why an IP-literal destination can never be reached, or ``""`` —
        judged without DNS (a name is only resolved once something allowed it)."""
        try:
            ip = str(ipaddress.ip_address(host))
        except ValueError:
            return ""
        if not is_public(ip) and not self.cfg.policy.private_allowed(ip):
            return f"{host} is a non-public address"
        return ""

    async def public_address(self, host: str, port: int) -> str:
        """An address for *host* that is public — checked, then pinned."""
        try:
            addrs = [str(ipaddress.ip_address(host))]
        except ValueError:
            addrs = await self._resolver(host, port)
        if not addrs:
            raise PermissionError(f"{host} did not resolve")
        bad = [a for a in addrs if not is_public(a) and not self.cfg.policy.private_allowed(a)]
        if bad:
            raise PermissionError(f"{host} resolves to a non-public address ({bad[0]})")
        return addrs[0]

    # --- the proxy ---------------------------------------------------------------

    def record(self, **entry) -> None:
        entry["ts"] = round(time.time(), 3)
        entry["tainted"] = self.tainted
        self._log.append(entry)
        print(json.dumps(entry, separators=(",", ":")), flush=True)

    async def handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        async with self._slots:
            try:
                await self._handle(reader, writer)
            except Exception as exc:  # noqa: BLE001 — one bad client must not stop the gate
                logger.debug("proxy connection failed: %s", exc)
            finally:
                writer.close()

    async def _handle(self, reader, writer) -> None:
        got = await _read_head(reader)
        if got is None:
            return
        head, leftover = got
        request_line, _, rest = head.partition(b"\r\n")
        try:
            method, target, version = request_line.decode("latin-1").split(" ", 2)
        except ValueError:
            await _respond(writer, 400, "Bad request")
            return
        method = method.upper()
        if method == "CONNECT":
            host, _, port_s = target.rpartition(":")
            host = host.strip("[]")
            port, path, judged_method = int(port_s or 443), None, None
        else:
            url = urlsplit(target)
            if url.scheme != "http" or not url.hostname:
                await _respond(writer, 400, "Only absolute http:// requests or CONNECT are proxied")
                return
            host, port = url.hostname, url.port or 80
            path = (url.path or "/") + (f"?{url.query}" if url.query else "")
            judged_method = method

        # Order matters for what leaks:
        # 1. the policy, which needs no DNS — a denied name is never looked up,
        #    so a secret spelled into a hostname cannot leave as a DNS query;
        # 2. an IP literal that can never be reached is refused without
        #    putting it to a person;
        # 3. only then ask (still no DNS), and resolve + SSRF-check after an allow.
        verdict = self.policy(host, port, judged_method)
        if verdict.action == DENY:
            why = verdict.reason
            self.record(host=host, port=port, method=method, path=path, verdict="deny", reason=why)
            await _respond(writer, 403, f"Blocked by the sandbox egress policy: {why}")
            return
        refusal = self.literal_refusal(host)
        if refusal:
            self.record(host=host, port=port, method=method, path=path, verdict="deny",
                        reason=f"ssrf: {refusal}")
            await _respond(writer, 403, f"Blocked: {refusal}")
            return
        action, why = await self.decide(host, port, judged_method, path)
        if action != ALLOW:
            self.record(host=host, port=port, method=method, path=path, verdict="deny", reason=why)
            await _respond(writer, 403, f"Blocked by the sandbox egress policy: {why}")
            return
        try:
            ip = await self.public_address(host, port)
        except (PermissionError, OSError) as exc:
            self.record(host=host, port=port, method=method, path=path, verdict="deny",
                        reason=f"ssrf: {exc}")
            await _respond(writer, 403, f"Blocked: {exc}")
            return
        self.record(host=host, port=port, method=method, path=path, verdict="allow",
                    reason=why, ip=ip)

        up_reader, up_writer = await asyncio.open_connection(ip, port)
        try:
            if method == "CONNECT":
                writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
                await writer.drain()
                if leftover:
                    up_writer.write(leftover)
            else:
                up_writer.write(_origin_form(method, path, version, rest, host, port) + leftover)
            await up_writer.drain()
            await asyncio.gather(_copy(reader, up_writer), _copy(up_reader, writer))
        finally:
            up_writer.close()


def _origin_form(method: str, path: str, version: str, rest: bytes, host: str, port: int) -> bytes:
    """Rewrite for the upstream. The Host header is REPLACED with the host that
    was judged: on a shared front-end (CDN) a client-chosen Host would reach a
    different site than the one the policy allowed."""
    headers = [h for h in rest.split(b"\r\n")
               if h and not h.lower().startswith((b"proxy-", b"connection:", b"host:"))]
    host_header = host if port == 80 else f"{host}:{port}"
    return (f"{method} {path} {version}\r\nHost: {host_header}\r\n".encode("latin-1")
            + b"\r\n".join(headers) + b"\r\nConnection: close\r\n\r\n")


async def _read_head(reader) -> tuple[bytes, bytes] | None:
    """``(head, leftover)``: the request head, and any bytes read past it."""
    data = b""
    while b"\r\n\r\n" not in data:
        chunk = await reader.read(4096)
        if not chunk:
            return None
        data += chunk
        if len(data) > _MAX_HEAD:
            return None
    head, _, leftover = data.partition(b"\r\n\r\n")
    return head, leftover


async def _copy(src, dst) -> None:
    try:
        while True:
            chunk = await src.read(_COPY_CHUNK)
            if not chunk:
                break
            dst.write(chunk)
            await dst.drain()
    except (ConnectionError, asyncio.IncompleteReadError):
        pass
    finally:
        try:
            dst.write_eof()
        except (OSError, RuntimeError, AttributeError):
            pass


async def _respond(writer, status: int, text: str) -> None:
    reason = {400: "Bad Request", 401: "Unauthorized", 403: "Forbidden", 404: "Not Found"}.get(status, "OK")
    body = (text + "\n").encode()
    writer.write(f"HTTP/1.1 {status} {reason}\r\nContent-Type: text/plain\r\n"
                 f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n".encode() + body)
    await writer.drain()


# --- the admin API ---------------------------------------------------------------

async def handle_admin(gate: Gate, reader, writer) -> None:
    try:
        got = await _read_head(reader)
        if got is None:
            return
        head, leftover = got
        lines = head.decode("latin-1").split("\r\n")
        method, path, _ = lines[0].split(" ", 2)
        headers = {k.strip().lower(): v.strip() for k, _, v in (ln.partition(":") for ln in lines[1:])}
        token = headers.get("authorization", "").removeprefix("Bearer ").strip()
        if gate.cfg.admin_token and hmac.compare_digest(token, gate.cfg.admin_token):
            role = "admin"
        elif gate.cfg.taint_token and hmac.compare_digest(token, gate.cfg.taint_token):
            role = "taint"
        else:
            await _json(writer, 401, {"error": "unauthorized"})
            return
        length = int(headers.get("content-length") or 0)
        raw = leftover + (await reader.readexactly(length - len(leftover))
                          if length > len(leftover) else b"")
        body = json.loads(raw[:length]) if length else {}

        if role == "taint":
            # The judged party may only make things stricter.
            if method == "POST" and path == "/taint" and body.get("tainted"):
                gate.set_taint(True, str(body.get("reason", "")), body.get("ttl"), raise_only=True)
                await _json(writer, 200, {"tainted": gate.tainted})
            else:
                await _json(writer, 403, {"error": "the taint token can only raise taint"})
            return

        if method == "GET" and path == "/status":
            await _json(writer, 200, {"tainted": gate.tainted, "taint_reason": gate._taint_reason,
                                      "pending": len(gate._pending)})
        elif method == "GET" and path == "/pending":
            await _json(writer, 200, {"pending": gate.pending()})
        elif method == "POST" and path.startswith("/pending/"):
            ok = gate.answer(path.rsplit("/", 1)[-1], bool(body.get("allow")),
                             ttl=body.get("ttl"), by=str(body.get("by", "")))
            await _json(writer, 200 if ok else 404, {"answered": ok})
        elif method == "POST" and path == "/taint":
            gate.set_taint(bool(body.get("tainted")), str(body.get("reason", "")), body.get("ttl"))
            await _json(writer, 200, {"tainted": gate.tainted})
        elif method == "GET" and path == "/log":
            await _json(writer, 200, {"log": list(gate._log)[-100:]})
        else:
            await _json(writer, 404, {"error": "not found"})
    except Exception as exc:  # noqa: BLE001
        await _json(writer, 400, {"error": str(exc)[:200]})
    finally:
        writer.close()


async def _json(writer, status: int, obj) -> None:
    body = json.dumps(obj).encode()
    writer.write(f"HTTP/1.1 {status} X\r\nContent-Type: application/json\r\n"
                 f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n".encode() + body)
    await writer.drain()
