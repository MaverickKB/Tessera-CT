"""A scoped, exact-match edit protocol; the model never supplies executable hooks."""

import json
from pathlib import Path

from .jobs import atomic_write


def owned_path(root, name):
    if not isinstance(name, str) or not name or Path(name).is_absolute() or '..' in Path(name).parts:
        raise ValueError('Invalid relative model edit path')
    path = root / name
    if path.is_symlink() or not path.resolve().is_relative_to(root.resolve()):
        raise ValueError('Model file path escaped its owned worktree')
    return path


def edit_prompt(config, worker):
    root = Path(worker['workspace']['path'])
    files = {}
    for name in dict.fromkeys([*worker['scope'], *config.agents.worktree_context_files]):
        path = owned_path(root, name)
        if path.is_dir():
            raise ValueError('Structured edits require exact file scopes/context paths')
        files[name] = path.read_text() if path.exists() else None
    packet = json.dumps({'objective': worker['objective'], 'scope': worker['scope'], 'files': files}, ensure_ascii=False)
    if len(packet.encode()) > config.agents.worktree_context_bytes:
        raise ValueError('Structured worktree context exceeds configured byte bound')
    return (
        'Implement the task using the exact current source below. Do not use tools, shell commands, '
        'network, filesystem access or profile inspection. Return ONLY JSON with one edits array. '
        'Each edit has exactly path, old and new strings. Every path must be inside scope. '
        'For an existing file, old must match exactly once; new replaces that occurrence. '
        'For a new file (source is null), old must be empty and new is its complete content. '
        'Use at most one edit per file; choose a sufficient unique anchor or full source when needed. '
        'Implement meaningful offline tests in the assigned test file. Do not weaken existing tests. '
        'Context files outside scope are read-only evidence. The engine applies edits, runs checks, '
        'commits the child and gates the merge. Do not claim test execution or success yourself. '
        'Do not include markdown, prose, omitted code, credentials, private endpoints/models or paths.\n' + packet)


def apply_edits(config, worker, text):
    if len(text.encode()) > config.swarm.max_artifact_bytes:
        raise ValueError('Model edits exceed configured artifact bound')
    value = json.loads(text)
    if not isinstance(value, dict) or set(value) != {'edits'} or not isinstance(value['edits'], list) or not value['edits']:
        raise ValueError('Model work requires a nonempty edits array')
    root = Path(worker['workspace']['path'])
    prepared = {}
    for edit in value['edits']:
        if not isinstance(edit, dict) or set(edit) != {'path', 'old', 'new'} or any(not isinstance(v, str) for v in edit.values()):
            raise ValueError('Model edit requires path, old and new text')
        name, old, new = edit['path'], edit['old'], edit['new']
        if name in prepared or not any(name == s.rstrip('/') or name.startswith(s.rstrip('/') + '/') for s in worker['scope']):
            raise ValueError('Model edit repeats a file or exceeds admitted scope')
        path = owned_path(root, name)
        if path.exists():
            source = path.read_text()
            if not old or source.count(old) != 1:
                raise ValueError('Model edit anchor does not match exact current source')
            result = source.replace(old, new, 1)
        else:
            if old:
                raise ValueError('New model file requires an empty old value')
            result = new
        if len(result.encode()) > config.swarm.max_artifact_bytes:
            raise ValueError('Model file exceeds configured artifact bound')
        prepared[name] = (path, result)
    # Validate the entire packet before changing any file.
    for path, content in prepared.values():
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write(path, content.encode())
    return list(prepared)
