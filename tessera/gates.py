"""Deterministic gate checks run before any semantic judgment or human callback."""

import posixpath


def scopes_overlap(left, right):
    for first in left:
        first = posixpath.normpath(first.replace('\\', '/'))
        for second in right:
            second = posixpath.normpath(second.replace('\\', '/'))
            if first in ('.', '/') or second in ('.', '/'):
                return True
            if first == second or first.startswith(second.rstrip('/') + '/') or second.startswith(first.rstrip('/') + '/'):
                return True
    return False


def conflicting_tasks(tasks):
    for index, left in enumerate(tasks):
        for right in tasks[index + 1:]:
            if not left.get('scope') or not right.get('scope'):
                return True
            if scopes_overlap(left['scope'], right['scope']):
                return True
    return False


def failed_checks(checks):
    if checks is None:
        return False
    if not isinstance(checks, dict):
        raise ValueError('Gate checks must be named boolean results')
    for value in checks.values():
        if type(value) is not bool:
            raise ValueError('Gate checks must be named boolean results')
        if not value:
            return True
    return False
