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
