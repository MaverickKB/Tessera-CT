"""Skill shelf: Agent Skills directories (agentskills.io), loaded by tool, never injected.

The catalog is not in the system prompt or the tool schemas, so editing a skill
cannot move the cached prefix. A session-start note lists names and descriptions
up to a byte cap; the rest is found with skill_search. Scripts in a skill run
through the ordinary shell tool and its approval gate.
"""

from pathlib import Path
import hashlib

NOTE_LABEL = 'skill-shelf'
NAME_LIMIT = 64
DESCRIPTION_LIMIT = 1024


def valid_name(name):
    if not isinstance(name, str) or not name or len(name) > NAME_LIMIT:
        return False
    if name.startswith('-') or name.endswith('-') or '--' in name:
        return False
    return all(char == '-' or ('a' <= char <= 'z') or char.isdigit() for char in name)


def _unquote(value):
    text = value.strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in ('"', "'"):
        return text[1:-1].strip()
    return text


def parse_skill_text(raw, directory_name):
    """Return {name, description, body}, or raise ValueError naming what is wrong."""
    if not raw.startswith('---\n'):
        raise ValueError('SKILL.md must start with a --- frontmatter line')
    end = raw.find('\n---', 3)
    if end < 0:
        raise ValueError('SKILL.md frontmatter is not closed with ---')
    body = raw[end + 4:]
    body = body[1:] if body.startswith('\n') else body
    name = description = ''
    for line in raw[4:end].splitlines():
        if line.startswith('name:'):
            name = _unquote(line[5:])
        elif line.startswith('description:'):
            description = _unquote(line[12:])
    if not valid_name(name):
        raise ValueError('name must be 1-64 lowercase letters, digits and single hyphens')
    if name != directory_name:
        raise ValueError(f'name {name!r} must match the skill directory {directory_name!r}')
    if not description or len(description) > DESCRIPTION_LIMIT:
        raise ValueError('description must be 1-1024 characters')
    return {'name': name, 'description': description, 'body': body}


class Shelf:
    """The user shelf plus any trusted project shelves. Read from disk on every call."""

    def __init__(self, home, cwd, config):
        self.config = config
        self.cwd = Path(cwd).resolve()
        self.user_root = Path(config.shelf).expanduser() if config.shelf else Path(home) / 'skills'
        # Optional () -> current SkillsConfig. A reload replaces the session config;
        # the shelf object stays, and the next call reads the new switches.
        self.live = None

    def _settings(self):
        if self.live is not None:
            current = self.live()
            if current is not None:
                return current
        return self.config

    def _disabled(self):
        raw = getattr(self._settings(), 'disabled', ()) or ()
        if not isinstance(raw, (list, tuple, set)):
            return set()
        return {item for item in raw if isinstance(item, str) and item}

    def project_trusted(self):
        for entry in self._settings().trusted_projects:
            root = Path(entry).expanduser().resolve()
            if self.cwd == root or root in self.cwd.parents:
                return True
        return False

    def roots(self):
        settings = self._settings()
        roots = [(self.user_root, 'user')]
        if self.project_trusted():
            roots += [(self.cwd / relative, 'project') for relative in settings.project_dirs]
        for extra in getattr(settings, 'extra_dirs', ()) or ():
            if isinstance(extra, str) and extra.strip():
                roots.append((Path(extra).expanduser(), 'added'))
        return roots

    def _collect(self):
        """Every parseable skill, including ones the operator turned off."""
        by_name = {}
        for root, source in self.roots():
            if not root.is_dir():
                continue
            for child in sorted(root.iterdir()):
                path = child / 'SKILL.md'
                if not path.is_file():
                    continue
                try:
                    parsed = parse_skill_text(path.read_text(encoding='utf-8'), child.name)
                except (OSError, UnicodeDecodeError, ValueError):
                    continue
                by_name[parsed['name']] = {**parsed, 'dir': child, 'source': source}
        return [by_name[name] for name in sorted(by_name)]

    def skills(self):
        """Sorted by name. A trusted project skill overrides a user skill of the same name.

        The shelf switch and the per-skill switch apply in every approval mode,
        including YOLO, so search, the session note and load all see the same
        set the page saved.
        """
        collected = self._collect()
        if not self._settings().enabled:
            return []
        disabled = self._disabled()
        return [skill for skill in collected if skill['name'] not in disabled]

    def find(self, name):
        found = next((skill for skill in self._collect() if skill['name'] == name), None)
        if found is None:
            raise ValueError(f'Unknown skill: {name}. Use skill_search.')
        if not self._settings().enabled:
            raise ValueError('Skills shelf is off')
        if name in self._disabled():
            raise ValueError(f'Skill disabled: {name}')
        return found

    def one_line(self, description):
        text = ' '.join(description.split())
        limit = self._settings().note_description_chars
        return text if len(text) <= limit else text[:limit - 3] + '...'

    def note(self):
        """Byte-capped index with a count of what did not fit; None for an empty shelf."""
        skills = self.skills()
        if not skills:
            return None
        limit = self._settings().note_bytes
        lines = [f'Skill shelf: {len(skills)} skills. skill_load a name before acting on it.']
        shown = 0
        for skill in skills:
            line = f"- {skill['name']}: {self.one_line(skill['description'])}"
            left = len(skills) - shown - 1
            tail = f'\n{left} more not listed; find them with skill_search.' if left else ''
            if len('\n'.join(lines + [line]).encode('utf-8')) + len(tail.encode('utf-8')) > limit:
                break
            lines.append(line)
            shown += 1
        if shown < len(skills):
            lines.append(f'{len(skills) - shown} more not listed; find them with skill_search.')
        return '\n'.join(lines)

    def search(self, query):
        """Plain text, deterministic: rank by matched words, name hits over description over body."""
        words = query.lower().split()
        scored = []
        for skill in self.skills():
            fields = (skill['name'].lower(), skill['description'].lower(), skill['body'].lower())
            matched = 0
            score = 0
            for word in words:
                weights = [weight for weight, text in zip((4, 2, 1), fields) if word in text]
                if weights:
                    matched += 1
                    score += max(weights)
            if matched:
                scored.append((-matched, -score, skill['name'], skill))
        scored.sort(key=lambda item: item[:3])
        hits = [item[3] for item in scored[:self._settings().search_limit]]
        if not hits:
            return f'No skills matched {query!r}. {len(self.skills())} on the shelf.'
        lines = [f"- {skill['name']} ({skill['source']}): {self.one_line(skill['description'])}" for skill in hits]
        more = len(scored) - len(hits)
        footer = f'\n{more} more matched; narrow the query.' if more else ''
        return f'{len(scored)} matched:\n' + '\n'.join(lines) + footer

    def stamp(self, name):
        """Hash of SKILL.md. An edit changes the pin without putting the body in the prompt."""
        skill = self.find(name)
        raw = (skill['dir'] / 'SKILL.md').read_bytes()
        return hashlib.sha256(raw).hexdigest()

    def load(self, name, clip_chars):
        skill = self.find(name)
        files = sorted(str(path.relative_to(skill['dir'])) for path in skill['dir'].rglob('*')
                       if path.is_file() and path.name != 'SKILL.md')
        header = f"Skill {name} ({skill['source']} shelf, {skill['dir']}): {skill['description']}\n"
        if files:
            header += 'Linked files (skill_read; run scripts with the shell tool):\n' + '\n'.join(files) + '\n'
        body = skill['body']
        if len(body) > clip_chars:
            body = body[:clip_chars] + '\n[clipped]'
        return header + '\n' + body

    def read(self, name, path, clip_chars):
        skill = self.find(name)
        root = skill['dir'].resolve()
        target = (root / path).resolve()
        if Path(path).is_absolute() or (target != root and root not in target.parents):
            raise ValueError('path must stay inside the skill directory')
        if not target.is_file():
            raise ValueError(f'No file {path} in skill {name}')
        text = target.read_text(encoding='utf-8', errors='replace')
        return text if len(text) <= clip_chars else text[:clip_chars] + '\n[clipped]'

    def write(self, name, text):
        if not valid_name(name):
            raise ValueError('name must be 1-64 lowercase letters, digits and single hyphens')
        parse_skill_text(text, name)
        folder = self.user_root / name
        folder.mkdir(parents=True, exist_ok=True)
        (folder / 'SKILL.md').write_text(text, encoding='utf-8')
        return f'Wrote {folder / "SKILL.md"} on the user shelf. The system prompt is unchanged; skill_load reads it now.'


def register_skill_tools(registry, shelf, clip_chars):
    """Four tools with constant descriptions and schemas; nothing on the shelf reaches them."""
    string = {'type': 'string'}
    from .tools import object_schema
    registry.register('skill_search', 'Search the skill shelf (procedures) by plain text over name, description and body. '
                      'Returns names and one-line descriptions.', object_schema({'query': string}, ['query']),
                      shelf.search)
    registry.register('skill_load', 'Load one skill procedure by name: its instructions and the list of its linked files.',
                      object_schema({'name': string}, ['name']), lambda name: shelf.load(name, clip_chars))
    registry.register('skill_read', 'Read one linked file of a skill by its path relative to the skill directory.',
                      object_schema({'name': string, 'path': string}, ['name', 'path']),
                      lambda name, path: shelf.read(name, path, clip_chars))
    registry.register('skill_write', 'Create or replace a skill on the user shelf. text is the full SKILL.md with name '
                      'and description frontmatter; name must match. Writes only the user shelf.',
                      object_schema({'name': string, 'text': string}, ['name', 'text']), shelf.write, risky=True)


def append_shelf_note(log, shelf):
    """One note per chat log, at its first session start. It then ages like any message."""
    for message in log.iter():
        if message.kind == 'note' and message.meta.get('label') == NOTE_LABEL:
            return None
    text = shelf.note()
    if text is None:
        return None
    return log.append('note', text, {'label': NOTE_LABEL, 'count': len(shelf.skills())})
