"""Confined workspace file operations.

Used both by the daemon (over HTTP) and by the in-process transport (locally),
so it lives at the package root — pure Python (stdlib only), no FastAPI import,
so importing it never pulls in the optional ``[daemon]`` deps.

Every path the caller supplies is confined to a per-user root with realpath
containment, and tar extraction is hardened against zip-slip / in-tar symlink
escape (reject symlink/hardlink/device members, re-confine every member, write
with ``O_NOFOLLOW``).
"""
from __future__ import annotations

import fnmatch
import io
import os
import tarfile

_MAX_LIST_ENTRIES = 20_000
_MAX_TAR_MEMBERS = 50_000


class PathEscape(ValueError):
    """A requested path resolved outside the confined root."""


def resolve_user_root(workspace_dir: str, user_id: str) -> str:
    """Return the realpath'd per-user workspace root, created if missing.

    Rejects a user_id that would escape and a root that is itself a symlink
    pointing outside the workspace dir.
    """
    safe_id = (user_id or "").lstrip("+")
    if not safe_id or safe_id in {".", ".."} or "/" in safe_id or "\\" in safe_id or os.path.isabs(safe_id):
        raise PathEscape(f"bad user_id: {user_id!r}")
    base = os.path.realpath(workspace_dir)
    root = os.path.join(base, safe_id)
    os.makedirs(root, exist_ok=True)
    real = os.path.realpath(root)
    if real != base and not real.startswith(base + os.sep):
        raise PathEscape(f"user root escapes workspace: {user_id!r}")
    return real


def confine(root_real: str, rel: str) -> str:
    """Resolve *rel* under *root_real*; raise PathEscape if it escapes.

    realpath resolves symlinks in existing path components, so a symlinked
    parent pointing outside the root is caught here.
    """
    rel = rel or ""
    if os.path.isabs(rel):
        raise PathEscape(rel)
    rel = rel.lstrip("/")
    target = os.path.realpath(os.path.join(root_real, rel))
    if target != root_real and not target.startswith(root_real + os.sep):
        raise PathEscape(rel)
    return target


def list_dir(root_real: str, rel: str = "", recursive: bool = False) -> list[dict]:
    start = confine(root_real, rel)
    entries: list[dict] = []
    if not os.path.exists(start):
        return entries
    if recursive:
        for dirpath, _dirs, files in os.walk(start):
            for name in files:
                full = os.path.join(dirpath, name)
                entries.append(_entry(root_real, full))
                if len(entries) >= _MAX_LIST_ENTRIES:
                    return entries
    else:
        for name in sorted(os.listdir(start)):
            entries.append(_entry(root_real, os.path.join(start, name)))
            if len(entries) >= _MAX_LIST_ENTRIES:
                break
    return entries


def _entry(root_real: str, full: str) -> dict:
    try:
        st = os.lstat(full)
        is_dir = os.path.isdir(full)
        return {
            "path": os.path.relpath(full, root_real),
            "type": "dir" if is_dir else "file",
            "size": st.st_size,
            "mtime": st.st_mtime,
        }
    except OSError:
        return {"path": os.path.relpath(full, root_real), "type": "unknown", "size": 0, "mtime": 0}


def read_file(root_real: str, rel: str, max_bytes: int) -> bytes:
    target = confine(root_real, rel)
    if not os.path.isfile(target):
        raise FileNotFoundError(rel)
    if os.path.getsize(target) > max_bytes:
        raise ValueError(f"file exceeds max_bytes ({max_bytes})")
    with open(target, "rb") as f:
        return f.read(max_bytes + 1)[:max_bytes]


def write_file(root_real: str, rel: str, data: bytes) -> int:
    target = confine(root_real, rel)
    parent = os.path.dirname(target)
    # confine the parent too (defends a non-existent leaf under a bad parent)
    confine(root_real, os.path.relpath(parent, root_real))
    os.makedirs(parent, exist_ok=True)
    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    try:
        os.write(fd, data)
    finally:
        os.close(fd)
    return len(data)


def grep(root_real: str, query: str, rel: str = "", include: str = "*", max_count: int = 200) -> list[dict]:
    start = confine(root_real, rel)
    out: list[dict] = []
    if not query or not os.path.exists(start):
        return out
    needle = query.lower()
    for dirpath, _dirs, files in os.walk(start):
        for name in files:
            if not fnmatch.fnmatch(name, include):
                continue
            full = os.path.join(dirpath, name)
            if os.path.islink(full):
                continue
            try:
                with open(full, encoding="utf-8", errors="ignore") as f:
                    text = f.read(1_000_000)
            except OSError:
                continue
            if needle in text.lower():
                snippet = _first_match(text, needle)
                out.append({
                    "path": os.path.relpath(full, root_real),
                    "session_id": os.path.basename(os.path.dirname(full)),
                    "snippet": snippet,
                })
                if len(out) >= max_count:
                    return out
    return out


def _first_match(text: str, needle: str) -> str:
    for line in text.splitlines():
        if needle in line.lower():
            return line.strip()[:500]
    return ""


def pull_tar(root_real: str, rel: str = "") -> bytes:
    """Build a tar of regular files under *rel* (symlinks/devices stripped)."""
    start = confine(root_real, rel)
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        if not os.path.exists(start):
            return buf.getvalue()
        for dirpath, _dirs, files in os.walk(start):
            for name in files:
                full = os.path.join(dirpath, name)
                if os.path.islink(full) or not os.path.isfile(full):
                    continue  # skip symlinks/devices/fifos — no zip-slip for the consumer
                arc = os.path.relpath(full, root_real)
                tar.add(full, arcname=arc, recursive=False)
    return buf.getvalue()


def push_tar(root_real: str, tar_bytes: bytes, rel: str = "") -> int:
    """Extract a tar under *rel*, rejecting every unsafe member."""
    dest_root = confine(root_real, rel)
    os.makedirs(dest_root, exist_ok=True)
    count = 0
    with tarfile.open(fileobj=io.BytesIO(tar_bytes), mode="r:*") as tar:
        members = tar.getmembers()
        if len(members) > _MAX_TAR_MEMBERS:
            raise ValueError("tar has too many members")
        for m in members:
            if not (m.isfile() or m.isdir()):
                raise PathEscape(f"unsafe tar member type: {m.name}")
            if os.path.isabs(m.name) or ".." in m.name.split("/"):
                raise PathEscape(f"unsafe tar member path: {m.name}")
            # Re-confine AFTER prior members materialized (catches planted symlinks).
            target = confine(dest_root, m.name)
            if m.isdir():
                os.makedirs(target, exist_ok=True)
                continue
            parent = os.path.dirname(target)
            confine(dest_root, os.path.relpath(parent, dest_root))
            os.makedirs(parent, exist_ok=True)
            extracted = tar.extractfile(m)
            data = extracted.read() if extracted else b""
            fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
            try:
                os.write(fd, data)
            finally:
                os.close(fd)
            count += 1
    return count
