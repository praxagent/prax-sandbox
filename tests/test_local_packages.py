"""Packages that survive the container: your own list baked into the image,
and a record of what was installed at runtime.

- ``sandbox/install-local-packages.sh`` installs ``sandbox/local-packages.txt``
  at image build. A bad entry is skipped and reported, never fatal, unless
  LOCAL_PACKAGES_STRICT=1.
- ``sandbox/record-apt-packages.sh`` is a dpkg hook: what was installed since
  the build goes to the workspace, and the record only grows.
- ``scripts/ensure-image.sh`` rebuilds only when the list changed.

apt-get, apt-mark and docker are fakes on the command line: no container,
no network.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
INSTALLER = REPO / "sandbox" / "install-local-packages.sh"
RECORDER = REPO / "sandbox" / "record-apt-packages.sh"
ENSURE = REPO / "scripts" / "ensure-image.sh"


def _script(path: Path, body: str) -> Path:
    path.write_text("#!/usr/bin/env bash\n" + textwrap.dedent(body))
    path.chmod(0o755)
    return path


# --- the installer ----------------------------------------------------------------

@pytest.fixture
def apt(tmp_path):
    """A fake apt-get: names in MISSING aren't in the archive, names in BROKEN
    fail to install; any bad name fails the whole transaction, like apt."""
    calls = tmp_path / "apt-calls"
    fake = _script(tmp_path / "apt-get", f"""
        echo "$*" >> {calls}
        [ "$1" = update ] && exit "${{UPDATE_FAILS:-0}}"
        shift 3   # install -y --no-install-recommends
        for p in "$@"; do
          case " $MISSING " in *" $p "*) echo "E: Unable to locate package $p"; exit 100;; esac
          case " $BROKEN " in *" $p "*) echo "dpkg: error processing package $p"; exit 100;; esac
        done
        exit 0
    """)
    return fake, calls


def _install(tmp_path, apt, listing, **env):
    fake, calls = apt
    lst = tmp_path / "local-packages.txt"
    if listing is not None:
        lst.write_text(listing)
    report = tmp_path / "report"
    run_env = {**os.environ, "APT_GET": str(fake), "REPORT": str(report),
               "MISSING": "", "BROKEN": "", **env}
    proc = subprocess.run(["bash", str(INSTALLER), str(lst)], capture_output=True,
                          text=True, env=run_env)
    lines = report.read_text().splitlines() if report.exists() else []
    installs = [c for c in calls.read_text().splitlines() if c.startswith("install")] \
        if calls.exists() else []
    return proc, lines, installs


def test_no_list_installs_nothing(tmp_path, apt):
    proc, lines, installs = _install(tmp_path, apt, None)
    assert proc.returncode == 0 and "none" in proc.stdout
    assert lines == [] and installs == []


def test_a_good_list_goes_in_as_one_transaction(tmp_path, apt):
    proc, lines, installs = _install(tmp_path, apt, "htop\n# a comment\nripgrep fd-find  # inline\nhtop\n")
    assert proc.returncode == 0
    assert lines == ["installed htop", "installed ripgrep", "installed fd-find"]
    assert len(installs) == 1
    assert "3 installed, 0 skipped" in proc.stdout


def test_bad_entries_are_skipped_and_reported_and_the_rest_still_install(tmp_path, apt):
    proc, lines, _ = _install(tmp_path, apt, "htop\nhtpo\nCurl\nfoo;rm\nbadpkg\nripgrep\n",
                              MISSING="htpo", BROKEN="badpkg")
    assert proc.returncode == 0
    assert set(lines) == {
        "installed htop", "installed ripgrep",
        "skipped htpo not-found", "skipped Curl invalid-name",
        "skipped foo;rm invalid-name", "skipped badpkg install-failed",
    }
    out = proc.stdout
    assert "2 installed, 4 skipped" in out
    assert "SKIPPED  htpo" in out and "renamed" in out          # says why, in words


def test_strict_mode_fails_the_build_over_a_skipped_entry(tmp_path, apt):
    proc, lines, _ = _install(tmp_path, apt, "htop\nhtpo\n", MISSING="htpo",
                              LOCAL_PACKAGES_STRICT="1")
    assert proc.returncode == 1
    assert "skipped htpo not-found" in lines and "installed htop" in lines


def test_no_network_skips_everything_with_the_reason(tmp_path, apt):
    proc, lines, installs = _install(tmp_path, apt, "htop\n", UPDATE_FAILS="100")
    assert proc.returncode == 0 and installs == []
    assert lines == ["skipped htop apt-update-failed"]


def test_version_pins_and_arch_qualifiers_are_valid_names(tmp_path, apt):
    _, lines, _ = _install(tmp_path, apt, "libc6:amd64 tzdata=2025a-1 g++\n")
    assert lines == ["installed libc6:amd64", "installed tzdata=2025a-1", "installed g++"]


# --- the dpkg hook ----------------------------------------------------------------

@pytest.fixture
def hook(tmp_path):
    manual = tmp_path / "manual"
    fake = _script(tmp_path / "apt-mark", f"cat {manual}\n")
    baseline = tmp_path / "baseline"
    out = tmp_path / "ws" / ".sandbox" / "installed-apt.txt"

    def run(installed_now: list[str], base: list[str] | None):
        manual.write_text("\n".join(installed_now) + "\n")
        if base is None:
            baseline.unlink(missing_ok=True)
        else:
            baseline.write_text("\n".join(sorted(base)) + "\n")
        env = {**os.environ, "APT_MARK": str(fake), "BASELINE": str(baseline), "OUT": str(out)}
        proc = subprocess.run(["sh", str(RECORDER)], capture_output=True, text=True, env=env)
        assert proc.returncode == 0
        if not out.exists():
            return None
        return [x for x in out.read_text().splitlines() if not x.startswith("#")]
    return run, out


def test_during_the_image_build_nothing_is_recorded(hook):
    run, out = hook
    assert run(["htop"], base=None) is None


def test_it_records_what_was_installed_since_the_build(hook):
    run, out = hook
    assert run(["bash", "htop", "jq"], base=["bash", "jq"]) == ["htop"]
    assert "local-packages.txt" in out.read_text()          # says what to do with it


def test_the_record_survives_a_new_container(hook):
    run, _ = hook
    run(["bash", "htop"], base=["bash"])
    # A recreated container no longer has htop; installing ripgrep must not erase it.
    assert run(["bash", "ripgrep"], base=["bash"]) == ["htop", "ripgrep"]


def test_packages_the_image_now_carries_drop_off(hook):
    run, _ = hook
    run(["bash", "htop", "ripgrep"], base=["bash"])
    assert run(["bash", "htop"], base=["bash", "htop"]) == ["ripgrep"]


# --- ensure-image -----------------------------------------------------------------

@pytest.fixture
def repo(tmp_path):
    """A copy of the scripts next to a scratch sandbox/ dir, with a fake docker."""
    (tmp_path / "scripts").mkdir()
    (tmp_path / "sandbox").mkdir()
    shutil.copy(ENSURE, tmp_path / "scripts" / "ensure-image.sh")
    calls = tmp_path / "docker-calls"
    fake = _script(tmp_path / "docker", f"""
        echo "$*" >> {calls}
        case "$1 $2" in
          "image inspect") [ -n "$IMAGE_MISSING" ] && exit 1; echo "$LABEL"; exit 0;;
          "build "*) exit "${{BUILD_FAILS:-0}}";;
          "run "*) printf '%b' "$REPORT"; exit 0;;
        esac
    """)

    def run(*args, listing=None, **env):
        lst = tmp_path / "sandbox" / "local-packages.txt"
        if listing is None:
            lst.unlink(missing_ok=True)
        else:
            lst.write_text(listing)
        calls.unlink(missing_ok=True)
        full = {**os.environ, "DOCKER": str(fake), "LABEL": "", "IMAGE_MISSING": "",
                "REPORT": "", **env}
        proc = subprocess.run(["bash", str(tmp_path / "scripts" / "ensure-image.sh"), *args],
                              capture_output=True, text=True, env=full)
        builds = [c for c in calls.read_text().splitlines() if c.startswith("build")] \
            if calls.exists() else []
        return proc, builds
    return run


def _sha_of(run, listing):
    _, builds = run(listing=listing, LABEL="stale")
    return builds[0].split("LOCAL_PACKAGES_SHA=")[1].split()[0]


def test_no_list_and_an_unlabelled_image_is_current(repo):
    proc, builds = repo(LABEL="<no value>")
    assert proc.returncode == 0 and builds == [] and "current" in proc.stdout


def test_adding_a_list_rebuilds_with_its_hash(repo):
    proc, builds = repo(listing="htop\n", REPORT="installed htop\\n")
    assert len(builds) == 1 and "LOCAL_PACKAGES_SHA=" in builds[0]
    assert "1 installed, none skipped" in proc.stdout


def test_an_unchanged_list_does_not_rebuild_and_comments_dont_count(repo):
    sha = _sha_of(repo, "htop\nripgrep\n")
    _, builds = repo(listing="# my tools\nhtop   ripgrep  # both\n", LABEL=sha)
    assert builds == []


def test_a_missing_image_is_built(repo):
    _, builds = repo(IMAGE_MISSING="1")
    assert len(builds) == 1


def test_rebuild_forces_a_build(repo):
    _, builds = repo("--rebuild")
    assert len(builds) == 1


def test_skipped_packages_are_shown_after_the_build(repo):
    proc, _ = repo(listing="htop\nhtpo\n", REPORT="installed htop\\nskipped htpo not-found\\n")
    assert "1 SKIPPED" in proc.stdout and "htpo not-found" in proc.stdout


def test_a_failed_build_fails_loudly(repo):
    proc, _ = repo(listing="htop\n", BUILD_FAILS="1")
    assert proc.returncode != 0


# --- xterm clipboard keys (verified live in the image; guard the wiring) ----------

def test_xterm_gets_clipboard_keys_from_its_app_defaults():
    res = (REPO / "sandbox" / "xterm.Xresources").read_text()
    for binding in ("Ctrl Shift <Key>C: copy-selection(CLIPBOARD)",
                    "Ctrl Shift <Key>V: insert-selection(CLIPBOARD)",
                    "Ctrl ~Shift <Key>v: insert-selection(CLIPBOARD)"):
        assert binding in res
    assert "Ctrl <Key>c" not in res.replace("Ctrl Shift <Key>C", "")   # Ctrl+C still interrupts
    dockerfile = (REPO / "sandbox" / "Dockerfile").read_text()
    assert ">> /etc/X11/app-defaults/XTerm" in dockerfile
