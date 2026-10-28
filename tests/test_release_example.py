"""Copied release configuration exposes optional features without activating them."""

import tomllib
from unittest.mock import Mock

import pytest

from tessera.backend import Backend
from tessera.cli import main
from tessera.config import load_config
from tessera.credentials import CredentialResolver
from tessera.mcp_client import McpClient
from tessera.sessions import SessionManager
from tessera.voice import VoiceClient


def test_init_optional_sections_remain_inert_with_a_provider_key_available(tmp_path, monkeypatch):
    monkeypatch.setenv('MODEL_API_KEY', 'provider-key-placeholder')
    tripwires = []
    for owner, name in ((Backend, '__init__'), (CredentialResolver, 'resolve'),
                        (McpClient, '__init__'), (VoiceClient, 'mint')):
        tripwire = Mock(side_effect=AssertionError('Release example must stay offline until configured'))
        monkeypatch.setattr(owner, name, tripwire)
        tripwires.append(tripwire)

    assert main(['--home', str(tmp_path), 'init']) == 0
    data = tomllib.loads((tmp_path / 'config.toml').read_text())
    assert {'credentials', 'swarm', 'board', 'voice'} <= data.keys()
    for reference in data['credentials'].values():
        assert set(reference) == {'env', 'keychain_service', 'keychain_account'}
        assert all(value == '' for value in reference.values())
    for section in ('swarm', 'board', 'voice'):
        assert data[section]['enabled'] is False
    for name in ('websocket_url', 'model', 'api_base_url', 'client_secret_path',
                 'client_protocol_prefix', 'credential_ref', 'api_key_env'):
        assert data['voice'][name] == ''

    config = load_config(tmp_path)
    with SessionManager(config) as manager:
        manager.create('release-example')
        assert manager.status()['voice'] == {'enabled': False, 'configured': False, 'active_count': 0, 'voices': [], 'voice': '',
                                             'pipe': '', 'pipes': {'duplex': False, 'local': False}}
        with pytest.raises(ValueError, match='disabled'):
            manager.voice.start('release-example')
        with pytest.raises(ValueError, match='disabled'):
            manager.create_swarm_job('release-example', 'Do not execute', [])
        assert manager._runtimes == {}
    for tripwire in tripwires:
        tripwire.assert_not_called()
