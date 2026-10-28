"""The documented init command produces the same inert placeholder configuration."""

from pathlib import Path
import re
import tomllib
from urllib.parse import urlsplit
from unittest.mock import Mock

from tessera.backend import Backend
from tessera.cli import main
from tessera.config import load_config
from tessera.credentials import CredentialResolver


def test_readme_init_copies_valid_placeholders_and_refuses_to_replace_them(tmp_path, monkeypatch, capsys):
    root = Path(__file__).resolve().parents[1]
    readme = (root / "README.md").read_text()
    example = root / "config.example.toml"
    assert "(config.example.toml)" in readme and "python -m tessera init" in readme
    assert ".venv/bin/pytest -q" in readme
    tripwires = []
    for owner, name in ((Backend, "__init__"), (Backend, "chat"), (CredentialResolver, "resolve")):
        tripwire = Mock(side_effect=AssertionError("Init and config parsing must stay inert"))
        monkeypatch.setattr(owner, name, tripwire)
        tripwires.append(tripwire)
    monkeypatch.delenv("TESSERA_AETHERMIND_ENABLED", raising=False)
    assert main(["--home", str(tmp_path), "init"]) == 0
    target = tmp_path / "config.toml"
    assert target.read_bytes() == example.read_bytes()
    config = load_config(tmp_path)
    assert 'enabled' not in tomllib.loads(target.read_text()).get('aethermind', {})
    assert 'aethermind.enabled' not in config.retired_settings
    assert not hasattr(config.aethermind, 'enabled')
    assert not config.aethermind.url and not config.aethermind.endpoints
    assert config.aethermind.source == ''
    assert not config.jev.enabled and not config.jev.url and not config.jev.model
    assert not config.voice.enabled and not config.swarm.enabled
    for role, backend in config.roles.items():
        assert backend in config.backends and "`" + role + "`" in readme
    for backend in config.backends.values():
        assert urlsplit(backend.base_url).hostname == "models.example.com"
        assert backend.model.startswith("your-") and backend.api_key_env.isidentifier()
        assert backend.api_key_env in readme
    assert "credential" in readme.lower()
    saved = target.read_bytes(), target.stat().st_mtime_ns
    assert main(["--home", str(tmp_path), "init"]) == 1
    assert (target.read_bytes(), target.stat().st_mtime_ns) == saved
    assert "exists" in capsys.readouterr().err.lower()
    for tripwire in tripwires:
        tripwire.assert_not_called()


def test_readme_names_every_package_in_the_test_extra():
    root = Path(__file__).resolve().parents[1]
    readme = (root / 'README.md').read_text().lower()
    project = tomllib.loads((root / 'pyproject.toml').read_text())
    extra = project['project']['optional-dependencies']['test']
    names = []
    for spec in extra:
        names.append(spec.split('>', 1)[0].split('=', 1)[0].split('<', 1)[0].split('[', 1)[0].strip().lower())
    assert names == ['pytest', 'pygments', 'setuptools']
    for name in names:
        assert name in readme
    assert 'only test dependency' not in readme
    assert '.[test]' in (root / 'README.md').read_text()


def test_optional_example_sections_parse_as_disabled_and_empty_when_present(tmp_path, monkeypatch):
    root = Path(__file__).resolve().parents[1]
    example = (root / "config.example.toml").read_bytes()
    raw = tomllib.loads(example.decode())
    (tmp_path / "config.toml").write_bytes(example)
    resolve = Mock(side_effect=AssertionError("Example parsing cannot resolve credentials"))
    monkeypatch.setattr(CredentialResolver, "resolve", resolve)
    config = load_config(tmp_path)
    for name in ("swarm", "board", "voice"):
        assert raw.get(name, {}).get("enabled", False) is False
        assert getattr(config, name).enabled is False
    references = raw.get("credentials", {})
    assert references.keys() == config.credentials.keys()
    for name, reference in references.items():
        for selector in ("env", "keychain_service", "keychain_account", "config_ref", "keychain_command"):
            assert reference.get(selector, "") == ""
            assert getattr(config.credentials[name], selector) == ""
    for field in ("websocket_url", "api_base_url", "model", "api_key_env", "credential_ref",
                  "client_secret_path", "client_protocol_prefix"):
        assert raw.get("voice", {}).get(field, "") == ""
        assert getattr(config.voice, field) == ""
    setup = (root / "docs/SETUP.md").read_text().lower()
    assert "optional" in setup and "disabled" in setup
    for section in ("[swarm]", "[board]", "[voice]", "[credentials."):
        assert section in setup
    resolve.assert_not_called()


def test_setup_toml_examples_parse_as_reference_only_without_secret_values(monkeypatch):
    root = Path(__file__).resolve().parents[1]
    setup = (root / "docs/SETUP.md").read_text()
    examples = re.findall(r"```toml\s*\n(.*?)```", setup, re.DOTALL)
    assert examples
    resolve = Mock(side_effect=AssertionError("Documentation examples cannot resolve credentials"))
    monkeypatch.setattr(CredentialResolver, "resolve", resolve)
    sections = set()
    for example in examples:
        parsed = tomllib.loads(example)
        sections.update(parsed)
        CredentialResolver(parsed.get("credentials", {}))
        for backend in parsed.get("backends", {}).values():
            if backend.get("base_url"):
                assert urlsplit(backend["base_url"]).hostname == "models.example.com"
                assert backend["model"].startswith("<")
        if "voice" in parsed:
            voice = parsed["voice"]
            assert urlsplit(voice["websocket_url"]).hostname == "voice.example.com"
            assert voice["model"].startswith("<")
    assert {"credentials", "swarm", "board", "voice"} <= sections
    resolve.assert_not_called()
