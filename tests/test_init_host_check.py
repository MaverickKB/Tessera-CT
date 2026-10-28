import socket
from tessera.cli import _missing_memory_engine, _unusable_backends
from tessera.config import Config, BackendConfig
from tessera import bundled_engine

def test_doctor_names_unresolvable_backend_host(monkeypatch):
    def missing(*args, **kwargs):
        raise socket.gaierror('Name does not resolve')
    monkeypatch.setattr(socket, 'getaddrinfo', missing)
    config = Config()
    config.backends = {'primary': BackendConfig(base_url='http://missing.invalid/v1', model='test')}
    config.roles = {'main': 'primary'}
    gaps = _unusable_backends(config, resolve_hosts=True)
    assert any('missing.invalid' in gap and 'resolve' in gap for gap in gaps)

def test_doctor_accepts_resolvable_backend_host():
    config = Config()
    config.backends = {'primary': BackendConfig(base_url='http://127.0.0.1:8761/v1', model='test')}
    config.roles = {'main': 'primary'}
    assert _unusable_backends(config, resolve_hosts=True) == []


def test_init_names_missing_memory_engine(monkeypatch):
    config = Config()
    monkeypatch.setattr(bundled_engine, '_engine_root', lambda config: None)
    # The checkout vendors the engine, so a real doctor call is quiet. This
    # case is an install with neither a receipt nor that tree.
    monkeypatch.setattr('tessera.engine_manifest.vendored_status', lambda root=None: 'absent')
    message = _missing_memory_engine(config)
    assert message == ('This install has no memory engine bundled. Configure an AetherMind '
                       'endpoint or install a release wheel, which vendors the embedded engine.')


def test_init_does_not_name_missing_engine_when_endpoint_configured(monkeypatch):
    config = Config()
    config.aethermind.endpoints = ['https://memory.example/mcp']
    monkeypatch.setattr(bundled_engine, '_engine_root', lambda config: None)
    assert _missing_memory_engine(config) is None


def test_init_names_a_missing_engine_for_an_untouched_config(monkeypatch):
    config = Config()
    monkeypatch.setattr(bundled_engine, '_engine_root', lambda config: None)
    monkeypatch.setattr('tessera.engine_manifest.vendored_status', lambda root=None: 'absent')
    message = _missing_memory_engine(config)
    assert message == ('This install has no memory engine bundled. Configure an AetherMind '
                       'endpoint or install a release wheel, which vendors the embedded engine.')
