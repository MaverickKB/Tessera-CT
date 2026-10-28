"""Public-source scanner contracts using disposable git repos and generated sentinels."""

import os
from pathlib import Path
import shutil
import subprocess

import pytest


SCANNER = Path(__file__).resolve().parents[1] / "scripts/check_no_hardwires.sh"
ROOT = SCANNER.parents[1]
SENSITIVE = {
    "private_ten": ".".join(["10", "23", "45", "67"]),
    "private_172": ".".join(["172", "20", "45", "67"]),
    "private_192": ".".join(["192", "168", "45", "67"]),
    "private_cgnat": ".".join(["100", "64", "45", "67"]),
    "link_local": ".".join(["169", "254", "45", "67"]),
    "private_ipv6": "https://[" + "fd" + "00::1234]/api",
    "link_local_ipv6": "https://[" + "fe" + "80::1234]/api",
    "mac_home": "/".join(["", "Users", "placeholder", "private.txt"]),
    "linux_home": "/".join(["", "home", "placeholder", "private.txt"]),
    "private_host": "https://node-placeholder." + "internal/api",
    "token": "sk-" + "synthetic" * 4,
    "bearer": "Bearer " + "synthetic" * 4,
    "git_token": "gh" + "p_" + "synthetic" * 4,
}


def scanner_bash():
    """Interpreter for the scanner. TESSERA_HARDWIRE_BASH overrides it.

    The default is whatever `bash` is on PATH. A bash 3.2 binary can be
    passed by path so the suite exercises the macOS shell without baking
    that path into the tree.
    """
    configured = os.environ.get("TESSERA_HARDWIRE_BASH", "").strip()
    return configured or "bash"


def scanner_environment():
    environment = dict(os.environ)
    for name in list(environment):
        if name.startswith("TESSERA_HARDWIRE_") or name in {"GIT_DIR", "GIT_WORK_TREE"}:
            environment.pop(name)
    # Tiny fixture repos need one grep worker, including on busy shared hosts.
    environment.update(GIT_CONFIG_COUNT="1", GIT_CONFIG_KEY_0="grep.threads", GIT_CONFIG_VALUE_0="1")
    return environment


def run_scanner(cwd, env=None, timeout=60):
    return subprocess.run([scanner_bash(), str(SCANNER)], cwd=cwd, env=env or scanner_environment(),
                          capture_output=True, text=True, timeout=timeout)


def scan(tmp_path, relative, content, *, custom=None, from_file=False, working_content=None, staged=True):
    environment = scanner_environment()
    subprocess.run(["git", "init", "-q", "--template=", str(tmp_path)], check=True,
                   env=environment, capture_output=True)
    path = tmp_path / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    if staged:
        subprocess.run(["git", "add", "--", relative], cwd=tmp_path, env=environment, check=True, capture_output=True)
    if working_content is not None:
        path.write_text(working_content)
    if custom:
        if from_file:
            (tmp_path / ".tessera-hardwire-patterns.local").write_text("# Synthetic local pattern\n" + custom + "\n")
        else:
            environment["TESSERA_HARDWIRE_PATTERNS"] = custom
    return subprocess.run([scanner_bash(), str(SCANNER)], cwd=tmp_path, env=environment,
                          capture_output=True, text=True, timeout=10)


@pytest.mark.parametrize("relative", ["tessera/fixture.py", "web/fixture.js", "tests/fixture.py", "docs/fixture.md",
                                      "README.md", "docs/SETUP.md", "config.example.toml",
                                      "docs/brand/README.md", "docs/brand/icon/fixture.svg"])
@pytest.mark.parametrize("kind", list(SENSITIVE))
def test_sensitive_literals_fail_even_beside_a_reserved_example_url(tmp_path, relative, kind):
    sensitive = SENSITIVE[kind]
    result = scan(tmp_path, relative, "reference=https://models.example.com actual=" + sensitive + "\n")
    assert result.returncode == 1
    assert relative + ":1" in result.stderr
    assert sensitive not in result.stdout + result.stderr


@pytest.mark.parametrize("content", [
    'url="https://models.example.com/v1"\n',
    'url="http://127.0.0.1:12345"\n',
    'url="http://localhost:12345"\n',
    'api_key_env="EXAMPLE_PROVIDER_KEY"\n',
    'endpoint="<CONFIGURED_ENDPOINT>"\n',
])
def test_public_placeholders_and_loopback_fixtures_pass(tmp_path, content):
    result = scan(tmp_path, "docs/fixture.md", content)
    assert result.returncode == 0 and "Hardwire check passed" in result.stdout


@pytest.mark.parametrize("from_file", [False, True])
def test_operator_patterns_apply_to_reserved_sentinels_without_disclosing_them(tmp_path, from_file):
    value = "private-placeholder.example.com"
    result = scan(tmp_path, "docs/fixture.md", value + "\n",
                  custom=r"private-placeholder\.example\.com", from_file=from_file)
    assert result.returncode == 1 and "docs/fixture.md:1" in result.stderr
    assert value not in result.stdout + result.stderr


@pytest.mark.parametrize("from_file", [False, True])
@pytest.mark.parametrize("kind", list(SENSITIVE))
def test_operator_patterns_preserve_every_builtin_guard(tmp_path, from_file, kind):
    result = scan(tmp_path, "docs/SETUP.md", SENSITIVE[kind] + "\n",
                  custom="unrelated-sentinel-placeholder", from_file=from_file)
    assert result.returncode == 1 and "docs/SETUP.md:1" in result.stderr
    assert SENSITIVE[kind] not in result.stdout + result.stderr


@pytest.mark.parametrize("unsafe_location", ["index", "worktree"])
def test_sanitizing_one_copy_cannot_hide_a_sensitive_value_in_the_other(tmp_path, unsafe_location):
    unsafe = SENSITIVE["private_ten"] + "\n"
    safe = 'endpoint="https://models.example.com/v1"\n'
    staged = unsafe if unsafe_location == "index" else safe
    working = safe if unsafe_location == "index" else unsafe
    result = scan(tmp_path, "config.example.toml", staged, working_content=working)
    assert result.returncode == 1 and "config.example.toml:1" in result.stderr
    assert SENSITIVE["private_ten"] not in result.stdout + result.stderr


def test_new_unstaged_source_is_scanned_before_an_agent_commits_it(tmp_path):
    result = scan(tmp_path, "tessera/new_provider.py", "endpoint = '" + SENSITIVE["private_ten"] + "'\n",
                  staged=False)
    assert result.returncode == 1
    assert "tessera/new_provider.py:1" in result.stderr
    assert SENSITIVE["private_ten"] not in result.stdout + result.stderr


@pytest.mark.parametrize("from_file", [False, True])
def test_invalid_operator_pattern_fails_closed_without_echoing_pattern(tmp_path, from_file):
    pattern = "private-pattern-placeholder["
    result = scan(tmp_path, "README.md", "Public fixture\n", custom=pattern, from_file=from_file)
    assert result.returncode == 2
    assert "could not scan" in result.stderr.lower()
    assert pattern not in result.stdout + result.stderr


def source_export(tmp_path):
    """A tarball of HEAD. git rev-parse must not see a work tree here."""
    export = tmp_path / "export"
    export.mkdir()
    from git_checkout import require_git_checkout

    require_git_checkout()
    archived = subprocess.run(["git", "archive", "--format=tar", "HEAD"], cwd=ROOT,
                              check=True, capture_output=True)
    subprocess.run(["tar", "-x", "-C", str(export)], input=archived.stdout, check=True)
    assert not (export / ".git").exists()
    outside = subprocess.run(["git", "rev-parse", "--is-inside-work-tree"], cwd=export,
                             capture_output=True, text=True)
    assert outside.returncode != 0
    return export


def test_source_export_without_a_work_tree_passes_when_clean(tmp_path):
    result = run_scanner(source_export(tmp_path))
    assert result.returncode == 0, result.stderr
    assert "Hardwire check passed" in result.stdout


def test_source_export_skips_ignored_artifacts(tmp_path):
    export = source_export(tmp_path)
    ignored = export / ".venv" / "lib" / "leak.py"
    ignored.parent.mkdir(parents=True)
    ignored.write_text(SENSITIVE["private_ten"] + "\n")
    bytecode = export / "pkg" / "ignored.pyc"
    bytecode.parent.mkdir(parents=True)
    bytecode.write_text(SENSITIVE["private_192"] + "\n")
    result = run_scanner(export)
    assert result.returncode == 0, result.stderr
    assert "Hardwire check passed" in result.stdout
    assert SENSITIVE["private_ten"] not in result.stdout + result.stderr
    assert SENSITIVE["private_192"] not in result.stdout + result.stderr


def test_source_export_fails_on_a_planted_hardwire(tmp_path):
    export = source_export(tmp_path)
    planted = export / "docs" / "planted-hardwire.md"
    planted.write_text("endpoint=" + SENSITIVE["private_ten"] + "\n")
    result = run_scanner(export)
    assert result.returncode == 1
    assert "docs/planted-hardwire.md:1" in result.stderr
    assert "Hardwire check failed" in result.stderr
    assert SENSITIVE["private_ten"] not in result.stdout + result.stderr


def test_ripgrep_config_cannot_hide_a_python_hardwire(tmp_path):
    """RIPGREP_CONFIG_PATH is not part of the exclude contract.

    A config of --glob=!*.py makes rg --files omit every Python file. The
    export walk must not treat that list as the files it is allowed to read,
    so the planted hardwire was never opened and the scan exited 0.
    """
    export = tmp_path / "export"
    export.mkdir()
    (export / "leak.py").write_text("endpoint=" + SENSITIVE["private_ten"] + "\n")
    config = tmp_path / "ripgreprc"
    config.write_text("--glob=!*.py\n")
    env = scanner_environment()
    env["RIPGREP_CONFIG_PATH"] = str(config)
    result = run_scanner(export, env=env)
    assert result.returncode == 1
    assert "leak.py:1" in result.stderr
    assert SENSITIVE["private_ten"] not in result.stdout + result.stderr


def test_inherited_git_dir_cannot_make_an_export_look_clean(tmp_path):
    export = tmp_path / "export"
    export.mkdir()
    (export / "leak.py").write_text("endpoint=" + SENSITIVE["private_ten"] + "\n")
    env = scanner_environment()
    env["GIT_DIR"] = str(ROOT / ".git")
    result = run_scanner(export, env=env)
    assert result.returncode == 1
    assert "leak.py:1" in result.stderr
    assert SENSITIVE["private_ten"] not in result.stdout + result.stderr


def test_source_export_fails_closed_when_rg_cannot_run(tmp_path):
    export = tmp_path / "export"
    export.mkdir()
    (export / "README.md").write_text("Public fixture\n")
    bindir = tmp_path / "bin"
    bindir.mkdir()
    for name in ("git", "grep", "paste", "tr", "find", "sed", "cut", "sort", "comm", "mktemp", "rm"):
        source = shutil.which(name)
        assert source, name
        (bindir / name).symlink_to(source)
    env = scanner_environment()
    env["PATH"] = str(bindir)
    # Absolute interpreter: the child PATH intentionally has no rg, and also no bash.
    bash = scanner_bash()
    if os.path.sep not in bash:
        bash = shutil.which(bash)
    assert bash
    result = subprocess.run([bash, str(SCANNER)], cwd=export, env=env,
                            capture_output=True, text=True, timeout=20)
    assert result.returncode == 2
    assert "could not scan" in result.stderr.lower()
    assert "Hardwire check passed" not in result.stdout


MACBOOK = "Owners-" + "Mac" + "Book-Pro"
TEAM = "AB" + "CD123456"


def test_personal_computer_hostname_fails(tmp_path):
    result = scan(tmp_path, "scripts/note.sh", "HOST=" + MACBOOK + "\n")
    assert result.returncode == 1
    assert "scripts/note.sh:1" in result.stderr
    assert MACBOOK not in result.stdout + result.stderr


def test_empty_signing_team_and_example_bundle_prefix_pass(tmp_path):
    content = "DEVELOPMENT_TEAM =\nPRODUCT_BUNDLE_IDENTIFIER_PREFIX = com.example\n"
    result = scan(tmp_path, "Signing.example.xcconfig", content)
    assert result.returncode == 0, result.stderr
    assert "Hardwire check passed" in result.stdout


@pytest.mark.parametrize("content", [
    "DEVELOPMENT_TEAM = " + TEAM + ";\n",
    'developmentTeam = "' + TEAM + '"\n',
    "<string>" + TEAM + "</string>\n",
])
def test_apple_team_id_patterns_fail(tmp_path, content):
    result = scan(tmp_path, "project.pbxproj", content)
    assert result.returncode == 1
    assert "project.pbxproj:1" in result.stderr
    assert TEAM not in result.stdout + result.stderr


def test_ten_digit_build_mask_is_not_a_team_id(tmp_path):
    result = scan(tmp_path, "project.pbxproj", "<string>2147483647</string>\n")
    assert result.returncode == 0, result.stderr


def test_private_host_file_matches_a_whole_token_only(tmp_path):
    host = "alp" + "ha"
    environment = scanner_environment()
    subprocess.run(["git", "init", "-q", "--template=", str(tmp_path)], check=True,
                   env=environment, capture_output=True)
    path = tmp_path / "notes.md"
    path.write_text("service alphabet is fine\nservice " + host + " is private\n")
    subprocess.run(["git", "add", "--", "notes.md"], cwd=tmp_path, env=environment,
                   check=True, capture_output=True)
    (tmp_path / ".tessera-private-hosts.local").write_text("# local\n\n" + host + "\n")
    result = run_scanner(tmp_path)
    assert result.returncode == 1
    assert "notes.md:2" in result.stderr
    assert "notes.md:1" not in result.stderr
    assert host not in result.stdout + result.stderr


def test_private_host_file_rejects_a_name_that_is_not_a_token(tmp_path):
    environment = scanner_environment()
    subprocess.run(["git", "init", "-q", "--template=", str(tmp_path)], check=True,
                   env=environment, capture_output=True)
    (tmp_path / "notes.md").write_text("fine\n")
    subprocess.run(["git", "add", "--", "notes.md"], cwd=tmp_path, env=environment,
                   check=True, capture_output=True)
    (tmp_path / ".tessera-private-hosts.local").write_text("not a host\n")
    result = run_scanner(tmp_path)
    assert result.returncode == 2
    assert "could not read the private host list" in result.stderr.lower()


def test_named_host_is_not_a_built_in_pattern(tmp_path):
    result = scan(tmp_path, "notes.md", "service " + ("jar" + "vis") + "\n")
    assert result.returncode == 0, result.stderr
