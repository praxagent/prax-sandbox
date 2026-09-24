"""Run the egress gate (and the ingress relay) inside its container.

Environment:
  EGRESS_POLICY         path to the JSON policy (see policy.py)
  EGRESS_ADMIN_TOKEN    bearer token for the admin API (required)
  EGRESS_PROXY_PORT     proxy port on the sandbox network   (default 3128)
  EGRESS_ADMIN_PORT     admin API port                       (default 8790)
  EGRESS_ASK_TIMEOUT    seconds a request waits for an answer (default 120)
  EGRESS_ALLOW_TTL      seconds a person's allow is remembered (default 600)
  INGRESS_RELAY         "port:host:port,..." — TCP relays from the outside
                        network into the sandbox (CDP, noVNC, clipboard), since
                        a container on an internal network cannot publish ports.
"""
from __future__ import annotations

import asyncio
import functools
import logging
import os
import sys

from prax_sandbox.egress_gate.gate import Gate, GateConfig, handle_admin
from prax_sandbox.egress_gate.policy import Policy


async def _relay(listen_port: int, host: str, port: int) -> asyncio.base_events.Server:
    async def pipe(src, dst):
        try:
            while chunk := await src.read(65536):
                dst.write(chunk)
                await dst.drain()
        except (ConnectionError, OSError):
            pass
        finally:
            dst.close()

    async def handle(reader, writer):
        try:
            up_r, up_w = await asyncio.open_connection(host, port)
        except OSError:
            writer.close()
            return
        await asyncio.gather(pipe(reader, up_w), pipe(up_r, writer))

    return await asyncio.start_server(handle, "0.0.0.0", listen_port)


async def main() -> None:
    logging.basicConfig(level=logging.INFO, stream=sys.stderr)
    token = os.environ.get("EGRESS_ADMIN_TOKEN", "")
    if not token:
        raise SystemExit("egress gate: refusing to start without EGRESS_ADMIN_TOKEN")
    policy = Policy.load(os.environ.get("EGRESS_POLICY", "/etc/egress/policy.json"))
    gate = Gate(GateConfig(
        policy=policy, admin_token=token,
        ask_timeout=float(os.environ.get("EGRESS_ASK_TIMEOUT", "120")),
        allow_ttl=float(os.environ.get("EGRESS_ALLOW_TTL", "600")),
    ))
    servers = [
        await asyncio.start_server(gate.handle, "0.0.0.0", int(os.environ.get("EGRESS_PROXY_PORT", "3128"))),
        await asyncio.start_server(functools.partial(handle_admin, gate), "0.0.0.0",
                                   int(os.environ.get("EGRESS_ADMIN_PORT", "8790"))),
    ]
    for spec in filter(None, os.environ.get("INGRESS_RELAY", "").split(",")):
        listen, host, port = spec.split(":")
        servers.append(await _relay(int(listen), host, int(port)))
    logging.info("egress gate up: default=%s, %d rules, %d relays",
                 policy.default, len(policy.rules), len(servers) - 2)
    await asyncio.gather(*(s.serve_forever() for s in servers))


if __name__ == "__main__":
    asyncio.run(main())
