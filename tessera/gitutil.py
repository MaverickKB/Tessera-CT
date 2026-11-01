"""The git-dir probe shared by the session builder and the shell tool."""
from pathlib import Path


def git_dir(cwd):
    """The real .git directory for cwd, following a worktree's gitdir file."""
    path = Path(cwd) / '.git'
    try:
        if path.is_dir():
            return path
        if path.is_file():
            text = path.read_text(encoding='utf-8', errors='replace').strip()
            if text.startswith('gitdir:'):
                raw = text.split(':', 1)[1].strip()
                located = Path(raw)
                if not located.is_absolute():
                    located = (path.parent / located).resolve()
                if located.is_dir():
                    return located
    except OSError:
        return None
    return None
