"""Egress policy: which destinations the sandbox may reach, and on what terms.

A policy is a JSON document::

    {
      "default": "ask",                       # allow | deny | ask
      "rules": [
        {"host": "pypi.org", "action": "allow"},
        {"host": "*.pythonhosted.org", "action": "allow"},
        {"host": "api.example.com", "methods": ["GET"], "action": "allow"},
        {"host": "*.example.org", "action": "allow", "clean_only": true},
        {"host": "evil.example", "action": "deny"}
      ]
    }

Rules are matched first-to-last; the first whose host pattern (exact, or
``*.suffix``), optional ``ports`` and optional ``methods`` match decides. No
match → ``default``.

``methods`` can only be judged for plain-HTTP requests: an HTTPS request
reaches the gate as ``CONNECT host:port`` and its method and path are inside
the TLS tunnel. So for HTTPS a rule with ``methods`` does not match, and the
request falls through to later rules or the default — it never silently
passes as if the method had been checked.

``allow_private_addresses`` lists exact IPs that may be reached although they
are not public (an internal package mirror, say). Empty by default: every
other private, loopback or link-local destination is refused after DNS
resolution, whatever the rules say.

``clean_only`` rules apply only while the sandbox is *clean*. The harness marks
it tainted when the work in flight has read private data; tainted, those rules
are skipped, so the same destination falls back to the default (usually
"ask"). This is a per-container, coarse version of per-process taint tracking.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field

ALLOW, DENY, ASK = "allow", "deny", "ask"
_ACTIONS = {ALLOW, DENY, ASK}


@dataclass(frozen=True)
class Rule:
    host: str
    action: str
    ports: frozenset[int] = frozenset()
    methods: frozenset[str] = frozenset()
    clean_only: bool = False

    def matches_host(self, host: str) -> bool:
        host = host.lower().rstrip(".")
        pattern = self.host.lower().rstrip(".")
        if pattern.startswith("*."):
            suffix = pattern[1:]  # ".example.com"
            return host.endswith(suffix) and len(host) > len(suffix)
        return host == pattern


@dataclass(frozen=True)
class Verdict:
    action: str
    reason: str


@dataclass
class Policy:
    default: str = ASK
    rules: list[Rule] = field(default_factory=list)
    allow_private_addresses: tuple = ()   # ip_network objects: exact IPs or CIDRs

    def private_allowed(self, ip: str) -> bool:
        import ipaddress
        addr = ipaddress.ip_address(ip)
        return any(addr in net for net in self.allow_private_addresses)

    @classmethod
    def from_dict(cls, data: dict) -> Policy:
        default = str(data.get("default", ASK)).lower()
        if default not in _ACTIONS:
            raise ValueError(f"policy default must be one of {sorted(_ACTIONS)}, not {default!r}")
        rules = []
        for i, raw in enumerate(data.get("rules", [])):
            action = str(raw.get("action", "")).lower()
            if action not in _ACTIONS or not raw.get("host"):
                raise ValueError(f"rule {i}: needs a host and an action in {sorted(_ACTIONS)}")
            rules.append(Rule(
                host=str(raw["host"]),
                action=action,
                ports=frozenset(int(p) for p in raw.get("ports", [])),
                methods=frozenset(str(m).upper() for m in raw.get("methods", [])),
                clean_only=bool(raw.get("clean_only", False)),
            ))
        import ipaddress
        nets = tuple(ipaddress.ip_network(str(a), strict=False)
                     for a in data.get("allow_private_addresses", []))
        return cls(default=default, rules=rules, allow_private_addresses=nets)

    @classmethod
    def load(cls, path: str) -> Policy:
        with open(path) as f:
            return cls.from_dict(json.load(f))

    def decide(self, host: str, port: int, method: str | None, *, tainted: bool) -> Verdict:
        """Decide one request. *method* is None for an HTTPS (CONNECT) tunnel."""
        for i, rule in enumerate(self.rules):
            if not rule.matches_host(host):
                continue
            if rule.ports and port not in rule.ports:
                continue
            if rule.methods and (method is None or method.upper() not in rule.methods):
                continue  # a method we cannot see is never assumed to match
            if rule.clean_only and tainted:
                continue
            return Verdict(rule.action, f"rule {i} ({rule.host})")
        return Verdict(self.default, "default")
