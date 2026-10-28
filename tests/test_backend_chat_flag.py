import pytest

from fake_backend import FakeBackend
from tessera.config import load_config
from tessera.sessions import SessionManager


def write(home, body):
    home.mkdir(parents=True, exist_ok=True)
    (home / 'config.toml').write_text(body, encoding='utf-8')
    return load_config(home)


BASE = """
cwd = "{cwd}"
[backends.chat]
base_url = "http://127.0.0.1:9/v1"
model = "auto"
[backends.struct]
base_url = "http://127.0.0.1:9/v1"
model = "struct"
chat = false
[roles]
main = "chat"
[jev]
enabled = false
[aethermind]
"""


def test_chat_flag_parses_and_defaults_true(tmp_path):
    config = write(tmp_path / 'home', BASE.format(cwd=tmp_path))
    assert config.backends['chat'].chat is True
    assert config.backends['struct'].chat is False
    assert 'chat' not in config.backends['struct'].extra


def test_chat_flag_must_be_boolean(tmp_path):
    with pytest.raises(ValueError, match='chat'):
        write(tmp_path / 'home', BASE.format(cwd=tmp_path).replace('chat = false', 'chat = "no"'))


def test_utility_backend_is_not_offered_or_accepted_as_chat_brain(tmp_path):
    config = write(tmp_path / 'home', BASE.format(cwd=tmp_path))
    with SessionManager(config, backend_factory=lambda *a: FakeBackend(['ok'])) as manager:
        status = manager.status()
        assert status['backends'] == ['chat', 'struct']
        assert status['chat_backends'] == ['chat']
        with pytest.raises(ValueError, match='chat brain'):
            manager.create('Struct chat', 'struct')
        assert manager.create('Default chat')['backend'] is None  # follows the default, not pinned
        assert manager.create('Explicit chat', 'chat')['backend'] == 'chat'
