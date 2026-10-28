"""The embedded engine's reported lock_backend, without starting the process.

A session that is not using the embedded engine has no report here. A
bundled_path that is not an engine tree has no report. The value is the same
field the capabilities tool returns. This module does not write into the
vendored tree: a bytecode file beside core.py would fail the manifest check.
An import failure is labeled; it does not surface as the engine's own exception.
"""

_LOCK_BACKENDS = {}


class EmbeddedEngineLockError(RuntimeError):
    """The embedded engine's lock report could not be read."""


def embedded_lock_backend(config, session=None):
    memory = getattr(config, 'aethermind', None)
    if memory is None:
        return None
    if session is not None and not getattr(session, 'uses_embedded_engine', False):
        return None
    from .bundled_engine import _engine_root
    root = _engine_root(config)
    core = None if root is None else root / 'lib' / 'aethermind' / 'core.py'
    if core is None or not core.is_file():
        if root is not None:
            return None
        from .engine_manifest import vendored_dir
        core = vendored_dir() / 'lib' / 'aethermind' / 'core.py'
        if not core.is_file():
            return None
    key = str(core)
    if key not in _LOCK_BACKENDS:
        _LOCK_BACKENDS[key] = _load_lock_backend(core)
    return _LOCK_BACKENDS[key]


def _load_lock_backend(core):
    import importlib.util
    import sys
    previous = sys.dont_write_bytecode
    sys.dont_write_bytecode = True
    try:
        try:
            spec = importlib.util.spec_from_file_location('tessera_embedded_engine_lock', core)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            report = module.runtime_capabilities()
        except Exception as error:
            raise EmbeddedEngineLockError(
                'embedded AetherMind lock report could not be read (' + type(error).__name__ + ')'
            ) from error
    finally:
        sys.dont_write_bytecode = previous
    backend = report.get('lock_backend') if isinstance(report, dict) else None
    return backend if isinstance(backend, str) else None
