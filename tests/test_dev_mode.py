"""The development-mode escape hatch must survive a fresh clone.

Why this test exists: scripts/dev-mode.sh is the documented way for a session to
oversee its own core reload from outside the backend. docs/SETUP.md tells a reader
to run it, but the script lived untracked for a day -- so a fresh clone shipped
the instructions and no script. That is the same failure the tracked git-hook
test guards against: a file that works here and is invisible to everyone else.

These tests prove the script ships, is executable, refuses bad input, and -- the
part that matters -- that a clone of HEAD can actually run the rejoin path against
a live core. The last one is skipped when no core is listening, because a
developer machine without the core running should not fail the suite.
"""

import json
import os
import shutil
import subprocess
import sys
import tarfile
import urllib.error
import urllib.request
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "dev-mode.sh"
SETUP = ROOT / "docs" / "SETUP.md"
PORT = os.environ.get("TESSERA_PORT", "8740")
BASE = f"http://127.0.0.1:{PORT}"


def run(script, *args, cwd=None):
    return subprocess.run(["sh", str(script), *args], cwd=cwd or ROOT,
                          capture_output=True, text=True, timeout=60)


def test_the_escape_hatch_is_tracked_and_executable():
    from git_checkout import require_git_checkout

    require_git_checkout()
    tracked = subprocess.run(
        ["git", "ls-files", "--", "scripts/dev-mode.sh"],
        cwd=ROOT, check=True, capture_output=True, text=True).stdout
    assert tracked.strip() == "scripts/dev-mode.sh", (
        "scripts/dev-mode.sh must be tracked; docs/SETUP.md documents it, so an "
        "untracked copy means a fresh clone has the instructions and no script")
    assert SCRIPT.stat().st_mode & 0o111, "The script must be executable"


def test_setup_documents_a_script_that_exists():
    text = SETUP.read_text(encoding="utf-8")
    assert "scripts/dev-mode.sh" in text, (
        "SETUP.md is the reader's entry point for the escape hatch")
    assert SCRIPT.exists(), "SETUP.md names scripts/dev-mode.sh but it does not exist"


def shells_that_must_run_dev_mode():
    """dash is /bin/sh on Linux. bash 3.2 is /bin/bash on macOS."""
    bash = shutil.which("bash")
    if bash is None:
        pytest.fail("bash is required; macOS bash 3.2 must be able to run dev-mode.sh")
    shells = [bash]
    dash = shutil.which("dash")
    if dash:
        shells.append(dash)
    elif sys.platform.startswith("linux"):
        pytest.fail("Linux must run dev-mode.sh under dash, not only bash")
    return shells


def test_rejoin_runs_under_dash_and_bash():
    """A closed port must be a timeout, not a shell syntax error."""
    env = os.environ.copy()
    env["TESSERA_PORT"] = "9"
    env["TESSERA_RESTART_TIMEOUT"] = "1"
    env["TESSERA_KEY"] = "synthetic-dev-mode-key"
    for shell in shells_that_must_run_dev_mode():
        syntax = subprocess.run([shell, "-n", str(SCRIPT)], cwd=ROOT,
                                capture_output=True, text=True, timeout=20)
        assert syntax.returncode == 0, (shell, syntax.stderr)
        result = subprocess.run(
            [shell, str(SCRIPT), "infinity", "--rejoin"],
            cwd=ROOT, env=env, capture_output=True, text=True, timeout=30)
        assert result.returncode == 1, (shell, result.returncode, result.stderr)
        assert "did not expose" in result.stderr, (shell, result.stderr)
        assert "Syntax error" not in result.stderr, (shell, result.stderr)


def test_it_refuses_bad_input_loudly():
    assert run(SCRIPT).returncode == 2, "no arguments must be a usage error"
    assert run(SCRIPT, "bad/slug").returncode == 2, "a slug with a slash must be refused"
    assert run(SCRIPT, "infinity", "--bogus").returncode == 2, "an unknown mode must be refused"


def test_a_clone_of_head_can_run_the_rejoin_path(tmp_path):
    """The point of tracking it: the script must work from a plain checkout."""
    from git_checkout import require_git_checkout

    require_git_checkout()
    archive = tmp_path / "head.tar"
    subprocess.run(["git", "archive", "-o", str(archive), "HEAD"], cwd=ROOT, check=True)
    clone = tmp_path / "clone"
    clone.mkdir()
    with tarfile.open(archive) as tar:
        tar.extractall(clone)
    assert (clone / "scripts" / "dev-mode.sh").exists(), (
        "HEAD must carry the escape hatch, not just this working tree")

    # The rejoin path only reads; it does not restart anything.
    try:
        urllib.request.urlopen(f"{BASE}/api/status", timeout=3).read()
    except (urllib.error.URLError, OSError):
        pytest.skip("no core listening; the rejoin path needs one")

    result = subprocess.run(["sh", str(clone / "scripts" / "dev-mode.sh"), "infinity", "--rejoin"],
                            cwd=clone, capture_output=True, text=True, timeout=120)
    assert result.returncode == 0, result.stderr
    assert "core ready" in result.stdout, result.stdout
    assert "rejoin:" in result.stdout, "the controller must print how to get back to the session"


def test_the_status_it_reads_is_the_shape_the_core_reports():
    """The script parses /api/status; keep the contract it depends on visible."""
    try:
        raw = urllib.request.urlopen(f"{BASE}/api/status", timeout=3).read()
    except (urllib.error.URLError, OSError):
        pytest.skip("no core listening")
    status = json.loads(raw)
    assert isinstance(status.get("sessions"), dict), (
        "dev-mode.sh reads status['sessions'][slug]['busy']; a missing or "
        "reshaped map would make the rejoin report silently wrong")
    assert status.get("generation"), "the script prints the new core generation"
