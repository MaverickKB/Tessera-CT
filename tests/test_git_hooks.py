"""The detached-HEAD guard must survive a fresh clone, not just this checkout.

Why this test exists: the first version of the guard lived in `.git/hooks/pre-commit`,
which git does not track. It worked here and died on every clone. These tests run the
tracked hook inside a throwaway repo so they prove the behaviour, not the file's
existence.
"""

import os
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
HOOKS = ROOT / "scripts" / "git-hooks"
HOOK = HOOKS / "pre-commit"
INSTALLER = ROOT / "scripts" / "install-git-hooks.sh"
SCAN = ROOT / "scripts" / "check_no_hardwires.sh"

# An address reserved for documentation (RFC 5737 TEST-NET-1) and never routed.
# It is assembled from parts so this file contains no literal dotted quad: the scan
# refuses those on sight, and a test about that refusal has to obey it too.
DOC_HOST = ".".join(["192", "0", "2", "10"])
DOC_URL = f"http://{DOC_HOST}:8680/v1"


def git(cwd, *args, env=None, check=True):
    merged = {
        "GIT_AUTHOR_NAME": "test",
        "GIT_AUTHOR_EMAIL": "test@example.invalid",
        "GIT_COMMITTER_NAME": "test",
        "GIT_COMMITTER_EMAIL": "test@example.invalid",
        "HOME": str(cwd),
        "PATH": os.environ["PATH"],
    }
    if env:
        merged.update(env)
    return subprocess.run(["git", *args], cwd=cwd, check=check,
                          capture_output=True, text=True, env=merged)


@pytest.fixture
def repo(tmp_path):
    """A throwaway repo with the tracked hook installed, exactly as the installer does."""
    git(tmp_path, "init", "-q", "-b", "main")
    (tmp_path / "file.txt").write_text("one\n")
    git(tmp_path, "add", "file.txt")
    git(tmp_path, "commit", "-q", "-m", "first")
    hooks = tmp_path / "scripts" / "git-hooks"
    hooks.mkdir(parents=True)
    (hooks / "pre-commit").write_bytes(HOOK.read_bytes())
    (hooks / "pre-commit").chmod(0o755)
    # The hook now runs the release scan, so the fixture must carry it. A hook that
    # refused every commit for a missing script would hide the behaviour under test.
    (tmp_path / "scripts" / "check_no_hardwires.sh").write_bytes(SCAN.read_bytes())
    git(tmp_path, "config", "core.hooksPath", "scripts/git-hooks")
    return tmp_path


def test_the_hook_is_tracked_and_executable():
    from git_checkout import require_git_checkout

    require_git_checkout()
    tracked = subprocess.run(["git", "ls-files", "--", "scripts/git-hooks/pre-commit"],
                             cwd=ROOT, check=True, capture_output=True, text=True).stdout
    assert tracked.strip() == "scripts/git-hooks/pre-commit", (
        "The hook must be tracked; a copy in .git/hooks is invisible to a fresh clone")
    assert HOOK.stat().st_mode & 0o111, "Git refuses to run a non-executable hook"
    assert INSTALLER.exists() and INSTALLER.stat().st_mode & 0o111


def test_a_normal_commit_is_allowed(repo):
    (repo / "file.txt").write_text("two\n")
    git(repo, "add", "file.txt")
    result = git(repo, "commit", "-m", "second", check=False)
    assert result.returncode == 0, result.stderr


def test_a_detached_commit_is_refused(repo):
    git(repo, "checkout", "--detach", "-q")
    assert git(repo, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip() == "HEAD"
    result = git(repo, "commit", "--allow-empty", "-m", "dangling", check=False)
    assert result.returncode != 0, "A detached commit must be refused"
    assert "HEAD is detached" in result.stderr
    assert "git switch -c" in result.stderr


def test_the_escape_hatch_still_commits(repo):
    git(repo, "checkout", "--detach", "-q")
    result = git(repo, "commit", "--allow-empty", "-m", "intentional",
                 env={"TESSERA_ALLOW_DETACHED": "1"}, check=False)
    assert result.returncode == 0, result.stderr


def test_a_commit_embedding_a_private_address_is_refused(repo):
    """The release scan must run before the commit exists, not after it.

    A commit that embeds a private address must fail before it exists. Catching
    it only in the suite lets the commit land first.

    The address used here comes from the RFC 5737 documentation range, so it trips
    the scan's generic ip:port rule without putting a real host into this file --
    which is exactly what the scan exists to prevent, so the test obeys it too.
    """
    (repo / "leak.py").write_text(f"ROUTER = '{DOC_URL}'\n")
    git(repo, "add", "leak.py")
    result = git(repo, "commit", "-m", "leak", check=False)
    assert result.returncode != 0, "A commit with a private address must be refused"
    assert "Hardwire check failed" in result.stderr
    assert DOC_HOST not in result.stderr, (
        "The refusal must not echo the private value into a public log")
    assert git(repo, "rev-parse", "--verify", "HEAD").stdout.strip(), "history exists"
    assert git(repo, "log", "-1", "--format=%s").stdout.strip() == "first", (
        "Nothing may have been committed")


def test_a_clean_commit_passes_the_scan(repo):
    """The guard must not refuse ordinary work; loopback stays allowed."""
    (repo / "fine.py").write_text("URL = 'http://127.0.0.1:8680/v1'\n")
    git(repo, "add", "fine.py")
    result = git(repo, "commit", "-m", "fine", check=False)
    assert result.returncode == 0, result.stderr
    assert git(repo, "log", "-1", "--format=%s").stdout.strip() == "fine"


def test_the_hardwire_escape_hatch_still_commits(repo):
    (repo / "leak.py").write_text(f"ROUTER = '{DOC_URL}'\n")
    git(repo, "add", "leak.py")
    result = git(repo, "commit", "-m", "intentional", env={"TESSERA_ALLOW_HARDWIRES": "1"},
                 check=False)
    assert result.returncode == 0, result.stderr


def test_a_missing_scan_refuses_rather_than_passing(repo):
    """A checkout whose own release scan is gone must not commit anything.

    Silently passing would reproduce the fault: the guard would look active while
    checking nothing.
    """
    (repo / "scripts" / "check_no_hardwires.sh").unlink()
    (repo / "fine.py").write_text("x = 1\n")
    git(repo, "add", "fine.py")
    result = git(repo, "commit", "-m", "fine", check=False)
    assert result.returncode != 0, "A missing scan must refuse, not pass silently"
    assert "check_no_hardwires.sh is missing" in result.stderr


def test_the_installer_points_git_at_the_tracked_dir(tmp_path):
    """Run the real installer in a throwaway clone and check what it configured."""
    git(tmp_path, "init", "-q", "-b", "main")
    (tmp_path / "scripts" / "git-hooks").mkdir(parents=True)
    (tmp_path / "scripts" / "git-hooks" / "pre-commit").write_bytes(HOOK.read_bytes())
    # A stale copy in .git/hooks looks active but is ignored once hooksPath is set.
    stale = tmp_path / ".git" / "hooks" / "pre-commit"
    stale.parent.mkdir(parents=True, exist_ok=True)
    stale.write_text("#!/bin/sh\nexit 0\n")

    result = subprocess.run(["sh", str(INSTALLER)], cwd=tmp_path,
                            capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert git(tmp_path, "config", "--get", "core.hooksPath").stdout.strip() == "scripts/git-hooks"
    assert not stale.exists(), "The inert .git/hooks copy must be removed, not left to look active"
