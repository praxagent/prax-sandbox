"""The egress gate decides every connection out of the sandbox.

Policy semantics are tested directly; the proxy is tested end to end against
local upstream servers through a real socket, so "allowed" means bytes
actually flowed and "denied" means they did not.
"""
from __future__ import annotations

import asyncio
import json

import pytest

from prax_sandbox.egress_gate.gate import Gate, GateConfig, handle_admin, is_public
from prax_sandbox.egress_gate.policy import Policy

TOKEN = "t0ken"


# --- policy ---------------------------------------------------------------------

def _policy(**kw):
    return Policy.from_dict({"default": "ask", **kw})


def test_first_matching_rule_wins_and_default_applies():
    p = _policy(rules=[{"host": "evil.example", "action": "deny"},
                       {"host": "*.example", "action": "allow"}])
    assert p.decide("evil.example", 443, None, tainted=False).action == "deny"
    assert p.decide("pkg.example", 443, None, tainted=False).action == "allow"
    assert p.decide("example", 443, None, tainted=False).action == "ask"  # *.x is not x
    assert p.decide("other.org", 443, None, tainted=False).action == "ask"


def test_method_rules_never_match_https_whose_method_is_hidden():
    p = _policy(default="deny", rules=[{"host": "api.example", "methods": ["GET"], "action": "allow"}])
    assert p.decide("api.example", 80, "GET", tainted=False).action == "allow"
    assert p.decide("api.example", 80, "POST", tainted=False).action == "deny"
    assert p.decide("api.example", 443, None, tainted=False).action == "deny"


def test_clean_only_rules_lapse_while_tainted():
    p = _policy(rules=[{"host": "news.example", "action": "allow", "clean_only": True}])
    assert p.decide("news.example", 443, None, tainted=False).action == "allow"
    assert p.decide("news.example", 443, None, tainted=True).action == "ask"


def test_bad_policies_are_rejected():
    with pytest.raises(ValueError):
        Policy.from_dict({"default": "maybe"})
    with pytest.raises(ValueError):
        Policy.from_dict({"rules": [{"host": "x", "action": "sometimes"}]})


@pytest.mark.parametrize("ip,public", [
    ("8.8.8.8", True), ("127.0.0.1", False), ("10.1.2.3", False), ("169.254.169.254", False),
    ("192.168.1.1", False), ("::1", False), ("::ffff:127.0.0.1", False), ("224.0.0.1", False),
])
def test_public_address_check(ip, public):
    assert is_public(ip) is public


# --- end to end ---------------------------------------------------------------------

async def _upstream():
    """A tiny HTTP server that answers every request with 'hello'."""
    async def handle(reader, writer):
        await reader.readuntil(b"\r\n\r\n")
        writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 5\r\nConnection: close\r\n\r\nhello")
        await writer.drain()
        writer.close()
    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    return server, server.sockets[0].getsockname()[1]


async def _gate(policy: dict, **cfg):
    data = {"allow_private_addresses": ["127.0.0.1"], **policy}
    gate = Gate(GateConfig(policy=Policy.from_dict(data), admin_token=TOKEN, **cfg),
                resolver=_resolver)
    proxy = await asyncio.start_server(gate.handle, "127.0.0.1", 0)
    admin = await asyncio.start_server(lambda r, w: handle_admin(gate, r, w), "127.0.0.1", 0)
    return gate, proxy.sockets[0].getsockname()[1], admin.sockets[0].getsockname()[1]


async def _resolver(host, port):
    return {"internal.example": ["10.0.0.9"], "rebind.example": ["8.8.8.8", "127.0.0.2"]}.get(host, ["127.0.0.1"])


async def _get_via_proxy(proxy_port, host, up_port, method="GET"):
    r, w = await asyncio.open_connection("127.0.0.1", proxy_port)
    w.write(f"{method} http://{host}:{up_port}/x HTTP/1.1\r\nHost: {host}\r\n\r\n".encode())
    await w.drain()
    data = await r.read()
    w.close()
    return data


async def _connect_via_proxy(proxy_port, host, up_port):
    r, w = await asyncio.open_connection("127.0.0.1", proxy_port)
    w.write(f"CONNECT {host}:{up_port} HTTP/1.1\r\n\r\n".encode())
    await w.drain()
    status = await r.readuntil(b"\r\n\r\n")
    if b" 200 " not in status:
        body = await r.read()
        w.close()
        return status + body
    w.write(b"GET / HTTP/1.1\r\nHost: x\r\n\r\n")  # what TLS would carry
    await w.drain()
    data = await r.read()
    w.close()
    return status + data


async def _admin(admin_port, method, path, body=None, token=TOKEN):
    r, w = await asyncio.open_connection("127.0.0.1", admin_port)
    raw = json.dumps(body).encode() if body is not None else b""
    w.write(f"{method} {path} HTTP/1.1\r\nAuthorization: Bearer {token}\r\n"
            f"Content-Length: {len(raw)}\r\n\r\n".encode() + raw)
    await w.drain()
    resp = await r.read()
    w.close()
    status = int(resp.split(b" ", 2)[1])
    return status, json.loads(resp.split(b"\r\n\r\n", 1)[1] or b"{}")


def test_allowed_http_and_connect_reach_upstream():
    async def run():
        server, up = await _upstream()
        _, proxy, _ = await _gate({"default": "deny", "rules": [{"host": "ok.example", "action": "allow"}]})
        assert (await _get_via_proxy(proxy, "ok.example", up)).endswith(b"hello")
        assert (await _connect_via_proxy(proxy, "ok.example", up)).endswith(b"hello")
        server.close()
    asyncio.run(run())


def test_denied_request_never_leaves():
    async def run():
        server, up = await _upstream()
        _, proxy, _ = await _gate({"default": "deny"})
        assert b"403" in (await _get_via_proxy(proxy, "nope.example", up))
        assert b"403" in (await _connect_via_proxy(proxy, "nope.example", up))
        server.close()
    asyncio.run(run())


def test_ssrf_names_resolving_inside_are_refused_even_when_allowed():
    async def run():
        server, up = await _upstream()
        _, proxy, _ = await _gate({"default": "allow"})
        for host in ("internal.example", "rebind.example", "169.254.169.254"):
            out = await _connect_via_proxy(proxy, host, up)
            assert b"403" in out and b"non-public" in out, host
        server.close()
    asyncio.run(run())


def test_ask_waits_for_a_person_and_remembers_the_answer():
    async def run():
        server, up = await _upstream()
        gate, proxy, admin = await _gate({"default": "ask"})
        first = asyncio.create_task(_connect_via_proxy(proxy, "ask.example", up))
        second = asyncio.create_task(_connect_via_proxy(proxy, "ask.example", up))
        for _ in range(50):
            await asyncio.sleep(0.02)
            status, body = await _admin(admin, "GET", "/pending")
            if body["pending"]:
                break
        assert len(body["pending"]) == 1  # two requests, one question
        pid = body["pending"][0]["id"]
        assert (await _admin(admin, "POST", f"/pending/{pid}", {"allow": True, "by": "tj"}))[0] == 200
        assert (await first).endswith(b"hello") and (await second).endswith(b"hello")
        # Remembered: a third request does not ask again.
        assert (await _connect_via_proxy(proxy, "ask.example", up)).endswith(b"hello")
        assert (await _admin(admin, "GET", "/pending"))[1]["pending"] == []
        server.close()
    asyncio.run(run())


def test_unanswered_ask_is_denied():
    async def run():
        server, up = await _upstream()
        _, proxy, _ = await _gate({"default": "ask"}, ask_timeout=0.2)
        assert b"403" in (await _connect_via_proxy(proxy, "silent.example", up))
        server.close()
    asyncio.run(run())


def test_taint_turns_clean_only_rules_into_questions():
    async def run():
        server, up = await _upstream()
        gate, proxy, admin = await _gate(
            {"default": "deny", "rules": [{"host": "web.example", "action": "allow", "clean_only": True}]})
        assert (await _connect_via_proxy(proxy, "web.example", up)).endswith(b"hello")
        await _admin(admin, "POST", "/taint", {"tainted": True, "reason": "read private data"})
        assert b"403" in (await _connect_via_proxy(proxy, "web.example", up))
        await _admin(admin, "POST", "/taint", {"tainted": False})
        assert (await _connect_via_proxy(proxy, "web.example", up)).endswith(b"hello")
        server.close()
    asyncio.run(run())


def test_admin_api_needs_the_token():
    async def run():
        _, _, admin = await _gate({"default": "deny"})
        assert (await _admin(admin, "GET", "/pending", token="wrong"))[0] == 401
        assert (await _admin(admin, "POST", "/taint", {"tainted": False}, token=""))[0] == 401
    asyncio.run(run())


def test_every_decision_is_logged(capsys):
    async def run():
        server, up = await _upstream()
        _, proxy, _ = await _gate({"default": "deny", "rules": [{"host": "ok.example", "action": "allow"}]})
        await _get_via_proxy(proxy, "ok.example", up)
        await _get_via_proxy(proxy, "no.example", up)
        server.close()
    asyncio.run(run())
    lines = [json.loads(ln) for ln in capsys.readouterr().out.splitlines() if ln.startswith("{")]
    assert [(e["host"], e["verdict"]) for e in lines] == [("ok.example", "allow"), ("no.example", "deny")]


def test_unreachable_destinations_are_refused_without_asking_anyone():
    async def run():
        server, up = await _upstream()
        gate, proxy, admin = await _gate({"default": "ask"})
        out = await _connect_via_proxy(proxy, "169.254.169.254", up)
        assert b"403" in out
        assert (await _admin(admin, "GET", "/pending"))[1]["pending"] == []
        server.close()
    asyncio.run(run())


# --- review follow-ups ---------------------------------------------------------

def test_a_denied_name_is_never_looked_up():
    looked_up = []

    async def spying_resolver(host, port):
        looked_up.append(host)
        return ["127.0.0.1"]

    async def run():
        server, up = await _upstream()
        gate = Gate(GateConfig(policy=Policy.from_dict({"default": "deny"}), admin_token=TOKEN),
                    resolver=spying_resolver)
        proxy = await asyncio.start_server(gate.handle, "127.0.0.1", 0)
        out = await _connect_via_proxy(proxy.sockets[0].getsockname()[1], "s3cret.attacker.example", up)
        assert b"403" in out
        server.close()
    asyncio.run(run())
    assert looked_up == []  # no DNS query carried the name out


def test_a_name_is_not_looked_up_while_it_is_only_being_asked_about():
    looked_up = []

    async def spying_resolver(host, port):
        looked_up.append(host)
        return ["127.0.0.1"]

    async def run():
        gate = Gate(GateConfig(policy=Policy.from_dict({"default": "ask", "allow_private_addresses": ["127.0.0.1"]}),
                               admin_token=TOKEN, ask_timeout=0.2), resolver=spying_resolver)
        proxy = await asyncio.start_server(gate.handle, "127.0.0.1", 0)
        await _connect_via_proxy(proxy.sockets[0].getsockname()[1], "unanswered.example", 443)
    asyncio.run(run())
    assert looked_up == []


def test_an_allow_given_while_clean_is_not_reused_once_tainted():
    async def run():
        server, up = await _upstream()
        gate, proxy, admin = await _gate({"default": "ask"}, ask_timeout=0.5)
        task = asyncio.create_task(_connect_via_proxy(proxy, "news.example", up))
        for _ in range(50):
            await asyncio.sleep(0.02)
            pend = (await _admin(admin, "GET", "/pending"))[1]["pending"]
            if pend:
                break
        await _admin(admin, "POST", f"/pending/{pend[0]['id']}", {"allow": True})
        assert (await task).endswith(b"hello")
        gate.set_taint(True, "read private data")
        # Remembered allow was granted clean: tainted, it must be asked again
        # (and here nobody answers, so it is denied).
        assert b"403" in (await _connect_via_proxy(proxy, "news.example", up))
        server.close()
    asyncio.run(run())


def test_an_answer_to_a_clean_question_is_void_if_taint_arrived_meanwhile():
    async def run():
        server, up = await _upstream()
        gate, proxy, admin = await _gate({"default": "ask"})
        task = asyncio.create_task(_connect_via_proxy(proxy, "late.example", up))
        for _ in range(50):
            await asyncio.sleep(0.02)
            pend = (await _admin(admin, "GET", "/pending"))[1]["pending"]
            if pend:
                break
        gate.set_taint(True, "read private data")
        await _admin(admin, "POST", f"/pending/{pend[0]['id']}", {"allow": True})
        assert b"403" in (await task)
        server.close()
    asyncio.run(run())


def test_plain_http_answers_cover_only_that_method_and_path():
    async def run():
        server, up = await _upstream()
        gate, proxy, admin = await _gate({"default": "ask"}, ask_timeout=0.3)
        task = asyncio.create_task(_get_via_proxy(proxy, "api.example", up, "GET"))
        for _ in range(50):
            await asyncio.sleep(0.02)
            pend = (await _admin(admin, "GET", "/pending"))[1]["pending"]
            if pend:
                break
        await _admin(admin, "POST", f"/pending/{pend[0]['id']}", {"allow": True})
        assert (await task).endswith(b"hello")
        # Same host, different method: a new question (unanswered -> denied).
        assert b"403" in (await _get_via_proxy(proxy, "api.example", up, "POST"))
        server.close()
    asyncio.run(run())


def test_the_host_header_is_the_judged_host():
    from prax_sandbox.egress_gate.gate import _origin_form
    out = _origin_form("GET", "/x", "HTTP/1.1", b"Host: evil.example\r\nAccept: */*", "ok.example", 80)
    assert b"Host: ok.example\r\n" in out and b"evil.example" not in out


def test_pending_questions_say_how_long_an_answer_still_counts():
    async def run():
        gate, proxy, admin = await _gate({"default": "ask"}, ask_timeout=30)
        task = asyncio.create_task(_connect_via_proxy(proxy, "q.example", 443))
        for _ in range(50):
            await asyncio.sleep(0.02)
            pend = (await _admin(admin, "GET", "/pending"))[1]["pending"]
            if pend:
                break
        assert 0 < pend[0]["expires_in_seconds"] <= 30
        await _admin(admin, "POST", f"/pending/{pend[0]['id']}", {"allow": False})
        await task
    asyncio.run(run())
