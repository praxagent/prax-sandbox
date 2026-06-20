"""Tests for the daemon's confined file API (M3 Step 2).

Pure-function tests — no FastAPI / daemon extra needed for these.
"""
import io
import os
import tarfile

import pytest

from prax_sandbox import fileops as fa


@pytest.fixture()
def root(tmp_path):
    return fa.resolve_user_root(str(tmp_path), "+15551234")


class TestConfinement:
    def test_resolve_user_root_rejects_traversal(self, tmp_path):
        for bad in ["../escape", "a/b", "..", "/abs"]:
            with pytest.raises(fa.PathEscape):
                fa.resolve_user_root(str(tmp_path), bad)

    def test_confine_rejects_escape(self, root):
        for bad in ["../../etc/passwd", "/etc/passwd", "a/../../b"]:
            with pytest.raises(fa.PathEscape):
                fa.confine(root, bad)

    def test_confine_allows_inside(self, root):
        assert fa.confine(root, "active/main.py").startswith(root + os.sep)

    def test_confine_catches_symlinked_parent(self, root):
        # a symlink inside the root pointing outside must not let writes escape
        os.symlink("/etc", os.path.join(root, "evil"))
        with pytest.raises(fa.PathEscape):
            fa.confine(root, "evil/passwd")


class TestReadWriteListGrep:
    def test_write_read_roundtrip(self, root):
        fa.write_file(root, "active/app.py", b"print('hi')")
        assert fa.read_file(root, "active/app.py", 1000) == b"print('hi')"

    def test_read_missing_raises(self, root):
        with pytest.raises(FileNotFoundError):
            fa.read_file(root, "nope.txt", 1000)

    def test_read_over_cap_raises(self, root):
        fa.write_file(root, "big.txt", b"x" * 100)
        with pytest.raises(ValueError):
            fa.read_file(root, "big.txt", 10)

    def test_list_and_grep(self, root):
        fa.write_file(root, "archive/code/abc/SOLUTION.md", b"## built a beamer deck\n")
        listed = {e["path"] for e in fa.list_dir(root, "", recursive=True)}
        assert "archive/code/abc/SOLUTION.md" in listed
        hits = fa.grep(root, "beamer", rel="archive/code", include="SOLUTION.md")
        assert hits and hits[0]["session_id"] == "abc"


class TestTar:
    def test_push_pull_roundtrip(self, root):
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w") as t:
            data = b"x = 1\n"
            ti = tarfile.TarInfo("active/x.py")
            ti.size = len(data)
            t.addfile(ti, io.BytesIO(data))
        assert fa.push_tar(root, buf.getvalue(), "") == 1
        assert fa.read_file(root, "active/x.py", 100) == b"x = 1\n"
        pulled = fa.pull_tar(root, "active")
        names = tarfile.open(fileobj=io.BytesIO(pulled)).getnames()
        assert any(n.endswith("active/x.py") for n in names)

    def test_push_rejects_symlink_member(self, root):
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w") as t:
            ti = tarfile.TarInfo("evil")
            ti.type = tarfile.SYMTYPE
            ti.linkname = "/etc/passwd"
            t.addfile(ti)
        with pytest.raises(fa.PathEscape):
            fa.push_tar(root, buf.getvalue(), "")

    def test_push_rejects_absolute_and_dotdot(self, root):
        for name in ["/etc/passwd", "../escape.txt"]:
            buf = io.BytesIO()
            with tarfile.open(fileobj=buf, mode="w") as t:
                ti = tarfile.TarInfo(name)
                ti.size = 1
                t.addfile(ti, io.BytesIO(b"x"))
            with pytest.raises(fa.PathEscape):
                fa.push_tar(root, buf.getvalue(), "")

    def test_pull_strips_symlinks(self, root):
        fa.write_file(root, "active/real.txt", b"ok")
        os.symlink("/etc/passwd", os.path.join(root, "active", "link"))
        pulled = fa.pull_tar(root, "active")
        names = tarfile.open(fileobj=io.BytesIO(pulled)).getnames()
        assert not any(n.endswith("link") for n in names)
        assert any(n.endswith("real.txt") for n in names)
