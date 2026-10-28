"""A public release copy drops the exclude manifest and rejects leftover references."""

import importlib.util
import json
import os
from pathlib import Path
import subprocess

ROOT = Path(__file__).resolve().parents[1]
# A release input, so every fixture repository carries one. The real patterns are
# exercised by tests/test_release_privacy.py; these are enough to run the step.
PRIVACY = {
    "copyright_line": "^[ \t]*(?:Required Notice: )?Copyright [0-9]{4} [A-Z].*$",
    "skip_suffixes": [".png"],
    "allowed_email_domains": ["example.com"],
    "allowed_matches": [],
    "patterns": [
        {"label": "user home path", "regex": "(?<![A-Za-z0-9])/(?:Users|home)/"},
        {"label": "private IPv4 address",
         "regex": "(?<![0-9.])192\\.168\\.[0-9]{1,3}\\.[0-9]{1,3}(?![0-9.])"},
    ],
}


def load_builder():
    path = ROOT / "scripts" / "build_release.py"
    spec = importlib.util.spec_from_file_location("build_release", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def commit(repo):
    write(repo, "release/privacy_patterns.json", json.dumps(PRIVACY))
    env = os.environ.copy()
    env.update({
        "GIT_AUTHOR_NAME": "Release Test",
        "GIT_AUTHOR_EMAIL": "release-test@example.com",
        "GIT_COMMITTER_NAME": "Release Test",
        "GIT_COMMITTER_EMAIL": "release-test@example.com",
    })
    subprocess.run(["git", "init", "-q", "--template=", str(repo)], check=True, env=env)
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True, env=env)
    subprocess.run(["git", "commit", "-q", "-m", "fixture"], cwd=repo, check=True, env=env)
    return env


def write(repo, relative, text):
    path = repo / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def test_beta_roadmap_items_ship_outside_the_excluded_plans():
    builder = load_builder()
    patterns = builder.load_patterns(ROOT / "release" / "exclude.txt")
    for relative in ("README.md", "docs/DESIGN.md"):
        assert not builder.path_excluded(relative, patterns)
        text = (ROOT / relative).read_text(encoding="utf-8")
        assert "Strict verification gates (beta)" in text
        assert "user-defined tests" in text
        assert "Unleashed included" in text


def test_copy_omits_excluded_paths_and_fails_on_a_remaining_link(tmp_path):
    builder = load_builder()
    repo = tmp_path / "repo"
    write(repo, "README.md", "See [notes](notes/PRIVATE.md).\n")
    write(repo, "notes/PRIVATE.md", "private\n")
    write(repo, "notes/public.md", "Public note.\n")
    write(repo, "notes/review/nested/note.md", "See [log](../../PRIVATE.md).\n")
    write(repo, "release/exclude.txt", "notes/PRIVATE.md\nnotes/review/**\n")
    write(repo, "tests/test_ok.py", "def test_ok():\n    assert True\n")
    commit(repo)
    dest = tmp_path / "out"
    code = builder.main(["--repo", str(repo), "--output", str(dest), "--commit", "HEAD"])
    assert code == 1
    assert not (dest / "notes" / "PRIVATE.md").exists()
    assert not (dest / "notes" / "review" / "nested" / "note.md").exists()
    assert (dest / "notes" / "public.md").is_file()
    assert (dest / "tests" / "test_ok.py").is_file()


def test_clean_tree_copies_and_passes_the_reference_check(tmp_path):
    builder = load_builder()
    repo = tmp_path / "repo"
    write(repo, "README.md", "See [notes](notes/public.md).\n")
    write(repo, "notes/public.md", "Public note. See [readme](../README.md).\n")
    write(repo, "notes/PRIVATE.md", "See [also](notes/review/note.md).\n")
    write(repo, "notes/review/note.md", "private\n")
    write(repo, "release/exclude.txt", "notes/PRIVATE.md\nnotes/review/**\n")
    write(repo, "tests/test_ok.py", "def test_ok():\n    assert 'notes/public.md'\n")
    commit(repo)
    dest = tmp_path / "out"
    code = builder.main(["--repo", str(repo), "--output", str(dest), "--commit", "HEAD"])
    assert code == 0
    assert (dest / "README.md").is_file()
    assert not (dest / "notes" / "PRIVATE.md").exists()
    assert not (dest / "notes" / "review" / "note.md").exists()


def test_a_test_reference_to_an_excluded_path_fails(tmp_path):
    builder = load_builder()
    repo = tmp_path / "repo"
    write(repo, "README.md", "Public.\n")
    write(repo, "notes/PRIVATE.md", "private\n")
    write(repo, "release/exclude.txt", "notes/PRIVATE.md\n")
    write(repo, "tests/test_link.py", "PATH = 'notes/PRIVATE.md'\n")
    commit(repo)
    dest = tmp_path / "out"
    code = builder.main(["--repo", str(repo), "--output", str(dest), "--commit", "HEAD"])
    assert code == 1
    assert not (dest / "notes" / "PRIVATE.md").exists()


def test_dotfiles_keep_their_leading_dot(tmp_path):
    builder = load_builder()
    repo = tmp_path / "repo"
    write(repo, ".gitignore", ".venv/\n")
    write(repo, ".gitattributes", "* text=auto\n")
    write(repo, ".github/workflows/ci.yml", "name: ci\n")
    write(repo, ".cursor/dev.json", "{}\n")
    write(repo, "release/exclude.txt", "notes/PRIVATE.md\n")
    write(repo, "README.md", "Public.\n")
    commit(repo)
    dest = tmp_path / "out"
    code = builder.main(["--repo", str(repo), "--output", str(dest), "--commit", "HEAD"])
    assert code == 0
    assert (dest / ".gitignore").read_text(encoding="utf-8") == ".venv/\n"
    assert (dest / ".gitattributes").is_file()
    assert (dest / ".github" / "workflows" / "ci.yml").is_file()
    assert (dest / ".cursor" / "dev.json").is_file()
    assert not (dest / "gitignore").exists()
    assert not (dest / "github").exists()
    assert not (dest / "cursor").exists()


def test_prose_with_a_slash_is_not_a_path_reference(tmp_path):
    builder = load_builder()
    repo = tmp_path / "repo"
    private = "docs/" + "plan"
    write(repo, "docs/daemon-contract.md", "No provider is discovered by a plan/list read.\n")
    write(repo, "release/exclude.txt", private + "/**\n")
    write(repo, private + "/daemon-cli.md", "private\n")
    write(repo, "README.md", "See [daemon](docs/daemon-contract.md).\n")
    commit(repo)
    dest = tmp_path / "out"
    code = builder.main(["--repo", str(repo), "--output", str(dest), "--commit", "HEAD"])
    assert code == 0
    assert not (dest / private / "daemon-cli.md").exists()
    assert (dest / "docs" / "daemon-contract.md").is_file()
