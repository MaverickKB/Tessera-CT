"""The seed pack is a package default for strangers, not a copy of this install.

These tests guard the properties an adopter depends on: the pack names no
person, host, or companion; every rule is imperative and carries its reason;
the installer is idempotent for layers, textures and skills; and a rerun never
overwrites a rule the operator has since made their own.
"""

import json
import subprocess
import sys
from pathlib import Path

import pytest

from release_privacy import privacy_hits

ROOT = Path(__file__).resolve().parent.parent
PACK = ROOT / "seeds" / "tessera-seed-layers.json"
INSTALLER = ROOT / "scripts" / "install-seed-layers.py"

# One other product identity must not ship inside a floor any install can adopt.
# Personal names and machine names come from the shared privacy scan.
PRODUCT_NAMES = ("TesseraCUA",)


def load_pack():
    return json.loads(PACK.read_text(encoding="utf-8"))


def test_pack_names_no_person_host_or_companion():
    raw = PACK.read_text(encoding="utf-8")
    pack = load_pack()
    assert "The only product named explicitly is Tessera." in pack["intent"]
    assert "do not invent a name" in raw
    for name in PRODUCT_NAMES:
        assert name not in raw, f"seed pack names {name!r}, which is one install's identity"
    assert privacy_hits(ROOT / "seeds") == []


def test_pack_skill_files_are_generic():
    skills = ROOT / "seeds" / "skills"
    assert skills.is_dir(), "the pack names skills, so they must exist on disk"
    names = [child.name for child in skills.iterdir() if child.is_dir()]
    assert names, "no skill directories staged"
    for child in skills.iterdir():
        if not child.is_dir():
            continue
        text = (child / "SKILL.md").read_text(encoding="utf-8")
        for name in PRODUCT_NAMES:
            assert name not in text, f"{child.name}/SKILL.md names {name!r}"
        assert text.startswith("---\n"), f"{child.name}/SKILL.md needs frontmatter"
        assert f"name: {child.name}" in text, f"{child.name}/SKILL.md must name itself"


def test_every_rule_is_imperative_and_carries_its_reason():
    """A seed is a rule, not a log line: it must say what to do and why."""
    pack = load_pack()
    assert pack["layers"], "pack has no layers"
    for layer in pack["layers"]:
        body = layer["body"].strip()
        assert body, "empty seed body"
        assert body.startswith(("Rule:", "Hint:")), (
            f"seed {layer['ctx']} does not open as a rule or hint: {body[:60]!r}")
        assert len(body) > 60, f"seed {layer['ctx']} is too short to carry a reason"
        assert layer.get("conf") is not None, f"seed {layer['ctx']} has no confidence"


def test_pack_declares_every_skill_it_ships():
    pack = load_pack()
    declared = set(pack.get("skills") or [])
    on_disk = {child.name for child in (ROOT / "seeds" / "skills").iterdir() if child.is_dir()}
    assert declared == on_disk, f"pack declares {declared}, disk holds {on_disk}"


def test_seed_contexts_live_in_one_namespace():
    """A seed context lives in one namespace: seed/, never seed/seed/."""
    pack = load_pack()
    for item in [*pack["layers"], *pack["textures"]]:
        ctx = item["ctx"]
        assert ctx.startswith("seed/"), ctx
        assert not ctx.startswith("seed/seed/"), f"double prefix: {ctx}"


def test_installer_stages_payloads_and_skills(tmp_path):
    out = tmp_path / "out"
    result = subprocess.run([sys.executable, str(INSTALLER), "--out-dir", str(out)],
                            capture_output=True, text=True, timeout=120)
    assert result.returncode == 0, result.stderr
    payloads = json.loads((out / "seed-payloads.json").read_text())
    assert payloads["layers"] and payloads["textures"]
    assert (out / "seed-runner.py").is_file()
    staged = {child.name for child in (out / "skills").iterdir() if child.is_dir()}
    assert staged == {child.name for child in (ROOT / "seeds" / "skills").iterdir() if child.is_dir()}
    # The runner is what a store host executes; it must not be written with a
    # store path baked in.
    runner = (out / "seed-runner.py").read_text()
    assert "--project-root" in runner and "--skills-dir" in runner


def test_texture_bodies_are_hashed_without_separator_noise():
    """The separator line is punctuation, so a rerun must still see a match."""
    import importlib.util
    spec = importlib.util.spec_from_file_location("install_seed_layers", INSTALLER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    # The runner must be standalone on the store host, so its parser lives in
    # the template. Execute the template itself, not a copy of it.
    namespace = {"__name__": "seed_runner_under_test"}
    exec(module.RUNNER_TEMPLATE, namespace)
    import hashlib
    import tempfile
    with tempfile.TemporaryDirectory() as scratch:
        store = Path(scratch) / ".aethermind"
        store.mkdir()
        body = "A short texture body."
        (store / "texture.aem").write_text(
            f"2026-01-01 [seed | seed/x]\n{body}\n---\n", encoding="utf-8")
        found = namespace["present_texture_bodies"](scratch)
        assert hashlib.sha256(body.encode()).hexdigest() in found


# The packaged installer is the half that makes the pack reachable from an
# installed wheel. These tests run it for real against a scratch store, using
# the bundled engine when the checkout has one.

ENGINE_LIB = ROOT / "build" / "lib" / "tessera" / "_assets" / "aethermind-engine"


def _engine_available():
    return (ENGINE_LIB / "lib" / "aethermind" / "cli.py").is_file()


@pytest.mark.skipif(not _engine_available(),
                    reason="no bundled engine in this checkout; the packaged installer needs one")
def test_packaged_installer_writes_the_pack_into_a_store(tmp_path, monkeypatch):
    """An installed wheel must be able to place the seeds it ships."""
    monkeypatch.setenv("TESSERA_SEED_ENGINE", str(ENGINE_LIB))
    from tessera import seeds as seed_pack
    store = tmp_path / "store"
    result = seed_pack.install(store, skills_dir=tmp_path / "skills")
    layers, textures = seed_pack.normalize(seed_pack.load_pack())
    assert result["layers_written"] == len(layers) and layers
    assert result["textures_written"] == len(textures) and textures
    assert result["skills_staged"] == len(seed_pack.load_pack()["skills"])
    assert (store / ".aethermind" / "layers.aem").is_file()
    assert (store / ".aethermind" / "texture.aem").is_file()
    assert (tmp_path / "skills" / "first-contact" / "SKILL.md").is_file()


@pytest.mark.skipif(not _engine_available(),
                    reason="no bundled engine in this checkout; the packaged installer needs one")
def test_packaged_installer_rerun_is_idempotent(tmp_path, monkeypatch):
    """A second run must add nothing: the pack is a floor, not a repeated append."""
    monkeypatch.setenv("TESSERA_SEED_ENGINE", str(ENGINE_LIB))
    from tessera import seeds as seed_pack
    store = tmp_path / "store"
    shelf = tmp_path / "skills"
    seed_pack.install(store, skills_dir=shelf)
    before = (store / ".aethermind" / "layers.aem").read_text()
    again = seed_pack.install(store, skills_dir=shelf)
    assert again["layers_written"] == 0 and again["textures_written"] == 0
    assert again["layers_skipped"] > 0 and again["textures_skipped"] > 0
    assert again["skills_staged"] == 0 and again["skills_kept"] > 0
    assert (store / ".aethermind" / "layers.aem").read_text() == before


@pytest.mark.skipif(not _engine_available(),
                    reason="no bundled engine in this checkout; the packaged installer needs one")
def test_packaged_installer_never_overwrites_a_customized_skill(tmp_path, monkeypatch):
    """An operator's own version of a seeded skill wins on every later run."""
    monkeypatch.setenv("TESSERA_SEED_ENGINE", str(ENGINE_LIB))
    from tessera import seeds as seed_pack
    store = tmp_path / "store"
    shelf = tmp_path / "skills"
    seed_pack.install(store, skills_dir=shelf)
    mine = shelf / "first-contact" / "SKILL.md"
    mine.write_text("---\nname: first-contact\ndescription: Mine.\n---\n\nMy body.\n",
                    encoding="utf-8")
    seed_pack.install(store, skills_dir=shelf)
    assert mine.read_text() == "---\nname: first-contact\ndescription: Mine.\n---\n\nMy body.\n"


def test_packaged_installer_reports_a_missing_engine_instead_of_writing(tmp_path, monkeypatch):
    """A wheel without the engine asset must refuse, not half-install."""
    monkeypatch.setenv("TESSERA_SEED_ENGINE", str(tmp_path / "no-engine"))
    from tessera import seeds as seed_pack
    with pytest.raises(RuntimeError, match="carries no AetherMind engine"):
        seed_pack.install(tmp_path / "store", skills_dir=tmp_path / "skills")
    assert not (tmp_path / "store" / ".aethermind").exists()


def test_the_missing_engine_refusal_names_a_remedy_a_downloader_can_take(tmp_path, monkeypatch):
    """The message is read by whoever downloaded the wheel, not by a builder.

    The text has to name a remedy that person can take: an engine directory to
    point at, and a wheel that carries the engine. "Build the wheel" is not that
    remedy.
    """
    monkeypatch.setenv("TESSERA_SEED_ENGINE", str(tmp_path / "no-engine"))
    from tessera import seeds as seed_pack
    with pytest.raises(RuntimeError) as caught:
        seed_pack.install(tmp_path / "store", skills_dir=tmp_path / "skills")
    message = str(caught.value)
    assert "TESSERA_SEED_ENGINE" in message
    assert "lib/aethermind/cli.py" in message
    assert "built with the engine bundled" in message
    assert "Build the wheel" not in message


def test_packaged_installer_dry_run_writes_nothing(tmp_path, monkeypatch):
    monkeypatch.setenv("TESSERA_SEED_ENGINE", str(ENGINE_LIB))
    from tessera import seeds as seed_pack
    store = tmp_path / "store"
    result = seed_pack.install(store, skills_dir=tmp_path / "skills", dry_run=True)
    assert result["layers_written"] > 0 and result["dry_run"]
    assert not store.exists()
    assert not (tmp_path / "skills").exists()
