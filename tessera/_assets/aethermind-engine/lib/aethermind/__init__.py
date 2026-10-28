"""Public AetherMind package API.

The canonical implementation lives in :mod:`aethermind.core`. Keep this module
as a thin re-export so imports cannot drift into a second writer/reader path.
"""

from .core import (
    AetherLayer,
    AetherMind,
    AetherMindParseError,
    AutoInitBlocked,
    LockUnavailableError,
    RegistryError,
    LayerStore,
    LayerReadIssue,
    LayerReadReport,
    TextureStore,
    derive_currentness,
    error_payload,
    initialize_store,
    read_layers,
    register_project_key,
    runtime_capabilities,
    write_layer,
    write_texture,
)

__all__ = [
    "AetherLayer",
    "AetherMind",
    "AetherMindParseError",
    "AutoInitBlocked",
    "LockUnavailableError",
    "RegistryError",
    "LayerStore",
    "LayerReadIssue",
    "LayerReadReport",
    "TextureStore",
    "derive_currentness",
    "error_payload",
    "initialize_store",
    "read_layers",
    "register_project_key",
    "runtime_capabilities",
    "write_layer",
    "write_texture",
]
