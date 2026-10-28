"""Tracked public release inputs, including optional brand bundles and license files."""

import io
from pathlib import Path, PurePosixPath
import re
import subprocess
import tarfile
import tomllib
import zipfile

import pytest

from release_privacy import copyright_notice


ROOT = Path(__file__).resolve().parents[1]
PRIVATE_BRAND = re.compile(
    rb'''/(?:Users|home)/[^\s/"']+|https?://[^\s/]+\.(?:internal|lan|local|home|corp|ts\.net)\b'''
    rb'''|\b[A-Za-z0-9_-]+\.(?:internal|lan|local|home|corp|ts\.net)\b'''
    rb'''|\b(?:10\.[0-9]+|192\.168|172\.(?:1[6-9]|2[0-9]|3[01])|100\.[0-9]+)\.[0-9]+\.[0-9]+\b'''
    rb'''|\b(?:f[cd][0-9a-f]{2}|fe[89ab][0-9a-f])(?::[0-9a-f]*){2,}'''
    rb'''|\b(?:AetherMind|ledger)\s+layer[\s:="'#\[\]-]+[0-9]+\b'''
    rb'''|\blayer(?:[-_ ]?id)?[\s:="'#\[\]-]+(?:[0-9]{3,}\b|[0-9a-f]{8}-[0-9a-f-]{27,})''',
    re.IGNORECASE)


def tracked(*paths):
    from git_checkout import require_git_checkout

    require_git_checkout()
    result = subprocess.run(["git", "ls-files", "-z", "--", *paths], cwd=ROOT,
                            check=True, capture_output=True)
    return [name.decode() for name in result.stdout.split(b"\0") if name]


def assert_public_brand(name, content):
    personal_reply = any(PurePosixPath(part).name.casefold().startswith("personal-reply-") for part in name.split(":"))
    if personal_reply or PRIVATE_BRAND.search(content):
        raise AssertionError("Private brand provenance must stay outside release inputs: " + name)


def brand_contents(name, content):
    """Inspect archive members in memory; never extract paths or execute assets."""
    yield name, content
    if name.endswith((".tar", ".tgz", ".tar.gz")):
        with tarfile.open(fileobj=io.BytesIO(content)) as archive:
            for member in archive.getmembers():
                if member.isfile():
                    with archive.extractfile(member) as stream:
                        yield name + ":" + member.name, stream.read()
    elif name.endswith(".zip"):
        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            for member in archive.infolist():
                if not member.is_dir():
                    yield name + ":" + member.filename, archive.read(member)


def assert_tracked_brand(root, names):
    required = {"docs/brand/README.md", "docs/brand/board/index.html"}
    assert required <= set(names), "Tracked public brand inventory is missing its entrypoints"
    for name in names:
        path = root / name
        assert not path.is_symlink(), "Brand sources must be self-contained tracked files"
        for member, content in brand_contents(name, path.read_bytes()):
            assert_public_brand(member, content)


def test_tracked_public_brand_has_no_private_paths_hosts_or_layer_notes():
    assert_tracked_brand(ROOT, tracked("docs/brand"))


def test_empty_tracked_brand_inventory_is_not_a_clean_release(tmp_path):
    with pytest.raises(AssertionError, match="brand"):
        assert_tracked_brand(tmp_path, [])


@pytest.mark.parametrize("name,content", [
    ("docs/brand/README.md", b"AetherMind layer " + b"0007"),
    ("docs/brand/metadata.json", b'{"layer_id": "' + b"00000000-0000-0000-0000-000000000007" + b'"}'),
    ("docs/brand/personal-reply-placeholder.txt", b"Personal reply placeholder"),
    ("docs/brand/README.md", "/".join(["", "Users", "placeholder", "notes"]).encode()),
    ("docs/brand/README.md", "/".join(["", "home", "placeholder", "notes"]).encode()),
    ("docs/brand/README.md", b"https://node-placeholder." + b"internal/api"),
    ("docs/brand/README.md", ".".join(["10", "23", "45", "67"]).encode()),
    ("docs/brand/README.md", b"AetherMind layer " + b"7"),
    ("docs/brand/README.md", b"node-placeholder." + b"internal"),
    ("docs/brand/README.md", b"fd" + b"00::1234"),
    ("docs/brand/Personal-Reply-placeholder.txt", b"Personal reply placeholder"),
])
def test_brand_guard_rejects_generated_private_provenance_without_echoing_it(name, content):
    with pytest.raises(AssertionError) as caught:
        assert_public_brand(name, content)
    assert content.decode() not in str(caught.value)


@pytest.mark.parametrize("name,content", [
    ("README.md", "AetherMind layer " + "0007"),
    ("personal-reply-placeholder.txt", "Personal reply placeholder"),
])
@pytest.mark.parametrize("kind", ["zip", "tgz"])
def test_brand_archives_do_not_hide_private_provenance(name, content, kind):
    buffer = io.BytesIO()
    if kind == "zip":
        with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr(name, content)
    else:
        with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
            member = tarfile.TarInfo(name)
            member.size = len(content.encode())
            archive.addfile(member, io.BytesIO(content.encode()))
    with pytest.raises(AssertionError, match="Private brand provenance"):
        for name, content in brand_contents("docs/brand/fixture." + kind, buffer.getvalue()):
            assert_public_brand(name, content)


def test_public_brand_description_keeps_visual_layer_language_valid():
    assert_public_brand("docs/brand/README.md", b"Binary strata use compressed layer-stones.")


def assert_license_file(root, required):
    path = root / "LICENSE"
    notice = root / "NOTICE"
    if required or path.exists():
        assert not path.is_symlink(), "LICENSE must be a self-contained release file"
        assert path.is_file(), "Packaging requires the declared LICENSE file"
        content = path.read_text()
        if "Apache License" not in content or "Version 2.0" not in content:
            raise AssertionError("LICENSE does not contain the Apache License 2.0 text")
        if copyright_notice(root) not in content:
            raise AssertionError("LICENSE does not name the copyright holder")
        if "tessera/_assets/aethermind-engine" in content:
            raise AssertionError("LICENSE must not restate the vendored engine license")
        assert not notice.is_symlink(), "NOTICE must be a self-contained release file"
        assert notice.is_file(), "Packaging requires the declared NOTICE file"
        notice_text = notice.read_text()
        if copyright_notice(root) not in notice_text:
            raise AssertionError("NOTICE does not name the copyright holder")
        if "Apache License" not in notice_text:
            raise AssertionError("NOTICE does not declare the Apache License")
        if "tessera/_assets/aethermind-engine" not in notice_text:
            raise AssertionError("NOTICE does not keep the vendored engine on its own license")


def assert_release_license(root, names):
    assert_license_file(root, required=True)
    assert "LICENSE" in names, "LICENSE must be tracked for release"
    assert "NOTICE" in names, "NOTICE must be tracked for release"
    metadata = tomllib.loads((root / "pyproject.toml").read_text())
    assert metadata["project"]["license"] == "Apache-2.0"
    assert "LICENSE" in metadata["project"].get("license-files", []), "Packaging must include LICENSE"


def test_license_is_present_when_packaging_declares_or_tracks_it():
    assert_release_license(ROOT, tracked("LICENSE", "NOTICE"))


def test_removing_license_and_its_declarations_cannot_bypass_release_guard(tmp_path):
    (tmp_path / "pyproject.toml").write_text('[project]\nlicense = "Apache-2.0"\n')
    with pytest.raises(AssertionError, match="LICENSE"):
        assert_release_license(tmp_path, [])


def test_license_cannot_follow_a_link_outside_the_release(tmp_path):
    outside = tmp_path / "outside.txt"
    outside.write_text(
        copyright_notice(ROOT) + "\n"
        "# Apache License Version 2.0\n"
    )
    (tmp_path / "LICENSE").symlink_to(outside)
    with pytest.raises(AssertionError, match="LICENSE"):
        assert_license_file(tmp_path, required=True)


def test_notice_cannot_follow_a_link_outside_the_release(tmp_path):
    outside = tmp_path / "outside.txt"
    outside.write_text(
        copyright_notice(ROOT) + "\n"
        "Tessera is licensed under the Apache License, Version 2.0.\n"
        "tessera/_assets/aethermind-engine\n"
    )
    (tmp_path / "LICENSE").write_text(
        "Apache License\nVersion 2.0\n" + copyright_notice(ROOT) + "\n"
    )
    (tmp_path / "NOTICE").symlink_to(outside)
    with pytest.raises(AssertionError, match="NOTICE"):
        assert_license_file(tmp_path, required=True)


def test_license_must_not_claim_the_vendored_engine_uses_tessera_license(tmp_path):
    (tmp_path / "LICENSE").write_text(
        "Apache License\nVersion 2.0\n" + copyright_notice(ROOT) + "\n"
        "tessera/_assets/aethermind-engine\n"
    )
    with pytest.raises(AssertionError, match="vendor"):
        assert_license_file(tmp_path, required=True)


def test_bad_license_diagnostics_do_not_echo_file_contents(tmp_path):
    canary = "private-note-placeholder"
    (tmp_path / "LICENSE").write_text(canary)
    with pytest.raises(AssertionError) as caught:
        assert_license_file(tmp_path, required=True)
    assert canary not in str(caught.value)


def test_declared_license_cannot_disappear(tmp_path):
    with pytest.raises(AssertionError, match="LICENSE"):
        assert_license_file(tmp_path, required=True)


def test_tracked_release_inputs_pass_hardwire_scan():
    result = subprocess.run(["bash", "scripts/check_no_hardwires.sh"], cwd=ROOT,
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    assert "Hardwire check passed" in result.stdout
