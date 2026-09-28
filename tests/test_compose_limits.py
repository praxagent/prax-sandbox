"""The opt-in limits overlay keeps every bound it exists to provide.

Text checks, like the remote-compose ones: the overlay is small and each line
is a guarantee somebody relies on (the 2026-07-08 disk-full outage was a
runaway write to the container's /tmp).
"""
from __future__ import annotations

import pathlib

ROOT = pathlib.Path(__file__).parent.parent
LIMITS = (ROOT / "docker-compose.limits.yml").read_text()


def test_bounds_memory_without_swap_on_top():
    assert "mem_limit: ${SANDBOX_MEM_LIMIT:-" in LIMITS
    assert "memswap_limit: ${SANDBOX_MEM_LIMIT:-" in LIMITS


def test_bounds_process_count():
    assert "pids_limit: ${SANDBOX_PIDS_LIMIT:-" in LIMITS


def test_tmp_is_a_sized_executable_tmpfs():
    line = next(ln for ln in LIMITS.splitlines() if ln.strip().startswith("- /tmp:"))
    assert "size=${SANDBOX_TMP_SIZE:-" in line
    assert "exec" in line.split(",")  # Docker's tmpfs default is noexec


def test_drops_capabilities_and_privilege_gain():
    for cap in ("NET_RAW", "MKNOD", "SYS_CHROOT"):
        assert f"- {cap}" in LIMITS
    assert "no-new-privileges:true" in LIMITS


def test_base_compose_is_unchanged_by_default():
    # The overlay is opt-in: the base file must not carry the limits itself,
    # or "default-off" would be false.
    base = (ROOT / "docker-compose.yml").read_text()
    assert "mem_limit" not in base and "tmpfs" not in base


def test_host_ports_are_overridable_with_historical_defaults():
    # A dev tree beside production passes SANDBOX_*_PORT; ignoring them made
    # the two sandboxes collide on the same loopback ports.
    base = (ROOT / "docker-compose.yml").read_text()
    for var, port in (("SANDBOX_CDP_PORT", 9223), ("SANDBOX_VNC_PORT", 6080),
                      ("SANDBOX_CLIPBOARD_PORT", 6090)):
        assert f'"127.0.0.1:${{{var}:-{port}}}:{port}"' in base


# --- the egress overlay -----------------------------------------------------------

EGRESS = (ROOT / "docker-compose.egress.yml").read_text()


def test_egress_overlay_puts_the_sandbox_on_an_internal_network_only():
    assert "networks: !override [cell]" in EGRESS
    assert "internal: true" in EGRESS
    assert "ports: !reset []" in EGRESS  # nothing published from the cell itself


def test_egress_overlay_routes_all_proxy_variables_through_the_gate():
    for var in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
        assert f"{var}: http://egress-gate:3128" in EGRESS


def test_egress_gate_refuses_to_start_without_a_token_and_publishes_on_loopback_only():
    assert "EGRESS_ADMIN_TOKEN: ${EGRESS_ADMIN_TOKEN:?" in EGRESS
    published = [ln.strip() for ln in EGRESS.splitlines() if ln.strip().startswith('- "') and ":" in ln]
    assert published and all(p.startswith('- "127.0.0.1:') for p in published)


def test_example_policy_parses_and_asks_by_default():
    import json

    from prax_sandbox.egress_gate.policy import Policy
    policy = Policy.from_dict(json.loads((ROOT / "egress-policy.example.json").read_text()))
    assert policy.default == "ask"
    assert policy.decide("clients2.google.com", 443, None, tainted=False).action == "deny"
    assert policy.decide("pypi.org", 443, None, tainted=False).action == "allow"
    assert policy.decide("github.com", 443, None, tainted=True).action == "ask"  # clean_only


def test_chromium_keeps_the_loopback_bypass_and_one_disable_features():
    launch = (ROOT / "sandbox" / "chromium-launch.sh").read_text()
    assert "<-loopback>" not in launch
    assert launch.count("--disable-features") == 1 + launch.count("# Chrome honours only the LAST --disable-features")
