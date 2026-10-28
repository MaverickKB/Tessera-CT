"""A static configuration floor, not proof of runtime configuration coverage.

Module/class literals retain the existing Config-name contract check. Function
parameters instead use actual calls across tessera/: a matching Config field name
alone cannot excuse a default. Call resolution covers imports, constructors,
self methods and uniquely named methods; dynamic dispatch/**kwargs stay explicit
unknowns. Defaults never omitted by a resolved caller are reported as overridden;
uncalled defaults are unproven, not demonstrated runtime policy. Config provenance
follows scoped assignments and explicit arguments,
not arbitrary names like ``settings``. This is a may-flow analysis, not a proof
that every branch or every external caller supplies configuration.
"""

import ast
from dataclasses import dataclass, fields, is_dataclass
from functools import cache
import operator
from pathlib import Path
import re

from debt_baseline import assert_matches_baseline

from tessera import config

ROOT = Path(__file__).resolve().parents[1]
POLICY = re.compile(r'(?:^|[_.])(?:threshold|timeout|budget|retries|retry|hold|cap|limit|margin|floor|probability|model|provider|policy|approval|wake|bytes|chars|steps|capability|check)(?:$|[_.])', re.I)
PLACEHOLDER = re.compile(r'\{[^{}]*\}')
OPS = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul,
       ast.Div: operator.truediv, ast.FloorDiv: operator.floordiv, ast.Pow: operator.pow}
FUNCTIONS = (ast.FunctionDef, ast.AsyncFunctionDef)


@dataclass
class Candidate:
    path: str
    line: int
    name: str
    value: object
    contract: object = None
    function: object = None
    parameter: str = ''


def literal(node):
    if isinstance(node, ast.Constant) and type(node.value) in (int, float, str):
        return node.value
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.USub, ast.UAdd)):
        value = literal(node.operand)
        if type(value) in (int, float):
            return -value if isinstance(node.op, ast.USub) else value
    if isinstance(node, ast.BinOp) and type(node.op) in OPS:
        left = literal(node.left)
        right = literal(node.right)
        if type(left) in (int, float) and type(right) in (int, float):
            return OPS[type(node.op)](left, right)
    return None


def candidates(trees):
    rows = []
    excluded = []

    def add(path, name, node, contract=None, function=None, parameter=''):
        value = literal(node)
        if value is None:
            return
        if isinstance(value, str):
            # Count prose/template exclusions before the policy-name filter.
            if PLACEHOLDER.search(value) or len(value) > 200 or '\n' in value:
                excluded.append((path, node.lineno, name))
                return
            if not value or not POLICY.search(name):
                return
        rows.append(Candidate(path, node.lineno, name, value, contract, function, parameter))

    def scope(path, body, prefix='', dataclass_name=None):
        for node in body:
            if isinstance(node, (ast.Assign, ast.AnnAssign)):
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                for target in targets:
                    if isinstance(target, ast.Name) and node.value is not None:
                        contract = (dataclass_name, target.id) if dataclass_name else None
                        add(path, prefix + target.id, node.value, contract)
            elif isinstance(node, ast.ClassDef):
                cls = getattr(config, node.name, None) if path == 'tessera/config.py' else None
                class_contract = node.name if cls is not None and is_dataclass(cls) else None
                scope(path, node.body, prefix + node.name + '.', class_contract)
            elif isinstance(node, FUNCTIONS):
                positional = node.args.posonlyargs + node.args.args
                pairs = []
                if node.args.defaults:
                    pairs.extend(zip(positional[-len(node.args.defaults):], node.args.defaults))
                pairs.extend(zip(node.args.kwonlyargs, node.args.kw_defaults))
                for arg, default in pairs:
                    if default is not None and POLICY.search(arg.arg):
                        add(path, prefix + node.name + '.' + arg.arg, default,
                            function=node, parameter=arg.arg)

    for path, tree in trees.items():
        scope(path, tree.body)
    return rows, excluded


def dotted(node):
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        base = dotted(node.value)
        if base:
            return base + '.' + node.attr
    return ''


class CallIndex:
    """Index call identity separately from scoped config provenance."""

    def __init__(self, trees):
        self.parents = {}
        self.paths = {}
        self.definitions = {}
        self.methods = {}
        self.calls = []
        self.assignments = []
        self.aliases = {}
        self.config_names = {}
        for path, tree in trees.items():
            for parent in ast.walk(tree):
                self.paths[parent] = path
                for child in ast.iter_child_nodes(parent):
                    self.parents[child] = parent
            module = Path(path).stem
            for node in ast.walk(tree):
                if isinstance(node, FUNCTIONS):
                    owner = self.parents[node]
                    if isinstance(owner, ast.ClassDef):
                        key = (module, owner.name + '.' + node.name)
                        self.methods.setdefault(node.name, []).append(node)
                    elif isinstance(owner, ast.Module):
                        key = (module, node.name)
                    else:
                        continue
                    self.definitions[key] = node
                elif isinstance(node, ast.Call):
                    self.calls.append(node)
                elif isinstance(node, (ast.Assign, ast.AnnAssign)):
                    self.assignments.append(node)
                elif isinstance(node, ast.ImportFrom):
                    source = (node.module or '').split('.')[-1]
                    for alias in node.names:
                        self.aliases[(path, alias.asname or alias.name)] = (source, alias.name)
        self.propagate_config()

    def scope(self, node):
        while not isinstance(node, (ast.Module,) + FUNCTIONS):
            node = self.parents[node]
        return node

    def class_owner(self, node):
        while node in self.parents:
            node = self.parents[node]
            if isinstance(node, ast.ClassDef):
                return node
        return None

    @cache
    def resolve(self, call):
        path = self.paths[call]
        module = Path(path).stem
        if isinstance(call.func, ast.Name):
            if call.func.id == 'cls':
                owner = self.class_owner(call)
                if owner:
                    return self.definitions.get((module, owner.name + '.__init__'))
            key = self.aliases.get((path, call.func.id), (module, call.func.id))
            return self.definitions.get(key) or self.definitions.get((key[0], key[1] + '.__init__'))
        if not isinstance(call.func, ast.Attribute):
            return None
        receiver = call.func.value
        if isinstance(receiver, ast.Name) and receiver.id in ('self', 'cls'):
            owner = self.class_owner(call)
            if owner:
                return self.definitions.get((module, owner.name + '.' + call.func.attr))
        if isinstance(receiver, ast.Call) and isinstance(receiver.func, ast.Name):
            key = self.aliases.get((path, receiver.func.id), (module, receiver.func.id))
            return self.definitions.get((key[0], key[1] + '.' + call.func.attr))
        # Module-qualified functions, including ``from . import module``.
        if isinstance(receiver, ast.Name):
            key = self.aliases.get((path, receiver.id))
            target_module = key[1] if key and not key[0] else receiver.id
            found = self.definitions.get((target_module, call.func.attr))
            if found:
                return found
        methods = self.methods.get(call.func.attr, [])
        if len(methods) == 1:
            return methods[0]
        return None

    def config_derived(self, expression, scope):
        if expression is None:
            return False
        for node in ast.walk(expression):
            name = dotted(node)
            # config, self.config and manager.config are explicit roots.
            if name and 'config' in name.split('.'):
                return True
            parts = name.split('.')
            for end in range(1, len(parts) + 1):
                prefix = '.'.join(parts[:end])
                if prefix in self.config_names.get(scope, set()):
                    return True
                owner = self.class_owner(scope)
                if prefix.startswith(('self.', 'cls.')) and prefix in self.config_names.get(owner, set()):
                    return True
        return False

    def argument(self, call, function, parameter):
        for keyword in call.keywords:
            if keyword.arg == parameter:
                return keyword.value, 'explicit'
        positional = function.args.posonlyargs + function.args.args
        if positional and positional[0].arg in ('self', 'cls'):
            positional = positional[1:]
        for index, arg in enumerate(positional):
            if arg.arg == parameter and index < len(call.args):
                if any(isinstance(value, ast.Starred) for value in call.args[:index + 1]):
                    return None, 'unknown (*args)'
                return call.args[index], 'explicit'
        for keyword in call.keywords:
            if keyword.arg is None:
                expanded = self.expanded_config_field(call, keyword.value, parameter)
                if expanded is not None:
                    return expanded, 'explicit'
                return keyword.value, 'unknown (**kwargs)'
        if any(isinstance(value, ast.Starred) for value in call.args):
            return None, 'unknown (*args)'
        return None, 'default'

    def expanded_config_field(self, call, mapping, parameter):
        # Recognize **settings created by asdict(config.<section>). Unlike a
        # generic config-derived dict, this has a known set of parameter keys.
        for assignment in self.assignments:
            if self.scope(assignment) is not self.scope(call):
                continue
            if assignment.lineno >= call.lineno:
                continue
            targets = assignment.targets if isinstance(assignment, ast.Assign) else [assignment.target]
            if not any(dotted(target) == dotted(mapping) for target in targets):
                continue
            value = assignment.value
            if not isinstance(value, ast.Call) or dotted(value.func) != 'asdict' or len(value.args) != 1:
                continue
            section = value.args[0]
            if not isinstance(section, ast.Attribute) or not self.config_derived(section, self.scope(call)):
                continue
            for cls in vars(config).values():
                if not isinstance(cls, type) or not is_dataclass(cls):
                    continue
                if cls.__name__.lower() != section.attr.lower() + 'config':
                    continue
                if parameter not in {field.name for field in fields(cls)}:
                    continue
                # A subsequent pop/delete of this key defeats the expansion.
                removed = False
                for node in ast.walk(self.scope(call)):
                    if not assignment.lineno < getattr(node, 'lineno', 0) < call.lineno:
                        continue
                    if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                        if dotted(node.func.value) == dotted(mapping) and node.func.attr == 'pop':
                            if node.args and literal(node.args[0]) == parameter:
                                removed = True
                    if isinstance(node, ast.Delete):
                        for target in node.targets:
                            if isinstance(target, ast.Subscript) and dotted(target.value) == dotted(mapping):
                                if literal(target.slice) == parameter:
                                    removed = True
                if not removed:
                    return ast.Attribute(value=section, attr=parameter, ctx=ast.Load())
        return None

    def remember(self, scope, name):
        names = self.config_names.setdefault(scope, set())
        if name in names:
            return False
        names.add(name)
        return True

    def propagate_config(self):
        changed = True
        while changed:
            changed = False
            for node in self.assignments:
                scope = self.scope(node)
                if not self.config_derived(node.value, scope):
                    continue
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                for target in targets:
                    name = dotted(target)
                    if not name:
                        continue
                    target_scope = self.class_owner(node) if name.startswith(('self.', 'cls.')) else scope
                    changed = self.remember(target_scope, name) or changed
            for call in self.calls:
                function = self.resolve(call)
                if function is None:
                    continue
                parameters = function.args.posonlyargs + function.args.args + function.args.kwonlyargs
                for parameter in parameters:
                    value, _ = self.argument(call, function, parameter.arg)
                    if self.config_derived(value, self.scope(call)):
                        changed = self.remember(function, parameter.arg) or changed

    def callers(self, row):
        sites = []
        for call in self.calls:
            if self.resolve(call) is not row.function:
                continue
            value, kind = self.argument(call, row.function, row.parameter)
            if kind == 'explicit':
                kind = 'config' if self.config_derived(value, self.scope(call)) else 'explicit non-config'
            sites.append((self.paths[call], call.lineno, kind))
        return sorted(sites)


def default_category(sites):
    # No observed omission means the definition's literal is not demonstrated
    # to run. Explicit non-config arguments are not default-taking calls.
    if not sites:
        return 'unproven'
    kinds = [site[2] for site in sites]
    if 'default' not in kinds:
        if any(kind.startswith('unknown') for kind in kinds):
            return 'unproven'
        return 'overridden'
    return 'partial' if 'config' in kinds else 'defaults'


def audit(root=ROOT):
    trees = {}
    for path in sorted((root / 'tessera').rglob('*.py')):
        relative = path.relative_to(root).as_posix()
        # The vendored engine is an upstream commit. Its constants are not
        # Tessera tunables, and this audit must not treat them as ours.
        if relative.startswith('tessera/_assets/aethermind-engine/'):
            continue
        trees[relative] = ast.parse(path.read_text(encoding='utf-8'))
    index = CallIndex(trees)
    contracts = set()
    for cls in vars(config).values():
        if isinstance(cls, type) and is_dataclass(cls):
            for field in fields(cls):
                contracts.add((cls.__name__, field.name))
    names = {name for _, name in contracts}
    allow = {}
    for line in (root / 'tessera/tunable_allowlist.txt').read_text().splitlines():
        entry, _, reason = line.partition('#')
        if entry.strip():
            assert reason.strip(), 'Allowlist entries require a reason'
            allow[entry.strip()] = reason.strip()
    rows, excluded = candidates(trees)
    counts = {'module': 0, 'defaults': 0, 'partial': 0, 'excluded': len(excluded),
              'config': 0, 'allowlist': 0, 'overridden': 0, 'unproven': 0}
    findings = []
    for row in rows:
        if row.path + ':' + row.name in allow:
            counts['allowlist'] += 1
            continue
        if row.function is None:
            if row.contract in contracts or row.name.rsplit('.', 1)[-1].lower().lstrip('_') in names:
                counts['config'] += 1
                continue
            category, verdict, sites = 'module', 'HARDWIRED', []
        else:
            sites = index.callers(row)
            category = default_category(sites)
            if category in ('overridden', 'unproven'):
                counts[category] += 1
                continue
            verdict = 'PARTIAL' if category == 'partial' else 'HARDWIRED'
        counts[category] += 1
        findings.append((category, row, verdict, sites))
    return counts, findings


def report(counts, findings):
    lines = []
    for category, title in (
        ('module', 'HARDWIRED module constants'),
        ('defaults', 'HARDWIRED parameter defaults (no caller passes config)'),
        ('partial', 'PARTIAL parameter defaults (one path gets config, another does not)'),
    ):
        lines.append(f'{title}: {counts[category]}')
        for group, row, verdict, sites in findings:
            if group != category:
                continue
            lines.append(f'{row.path}:{row.line} {row.name} = {row.value!r}  [{verdict}]')
            for path, line, kind in sites:
                lines.append(f'    {path}:{line} [{kind}]')
            if row.function is not None and not sites:
                lines.append('    no statically resolved calls in tessera/ (external/unused unknown)')
    lines.append(f"EXCLUDED (format strings, prose): {counts['excluded']}")
    lines.append(f"OVERRIDDEN parameter defaults (all observed calls supply a value): {counts['overridden']}")
    lines.append(f"UNPROVEN parameter defaults (no resolved calls or only dynamic calls): {counts['unproven']}")
    return '\n'.join(lines)


def test_no_hardwired_tunables():
    counts, findings = audit()
    output = report(counts, findings)
    print(output)
    current = set()
    for category, row, verdict, sites in findings:
        current.add(row.path + ':' + row.name)
    baseline = Path(__file__).with_name('hardwired_tunables_baseline.txt')
    assert_matches_baseline(current, baseline)


def test_scanner_excludes_templates_and_long_prose_but_keeps_policy_tokens():
    tree = ast.parse("RETRY = 'try {again}'\nMODEL = 'grok-transcribe'\nCHECK = '" + 'x' * 201 + "'\n")
    rows, excluded = candidates({'tessera/sample.py': tree})
    assert [(row.name, row.value) for row in rows] == [('MODEL', 'grok-transcribe')]
    assert [name for _, _, name in excluded] == ['RETRY', 'CHECK']


def test_scanner_traces_keyword_positional_and_scoped_config_aliases():
    tree = ast.parse('''
def clip(text, limit=300):
    return text[:limit]
def configured(config):
    settings = config.tools
    budget = settings.clip_chars
    clip('a', limit=budget)
    clip('b', budget)
def other():
    budget = 20
    clip('c', limit=budget)
    clip('d')
''')
    trees = {'tessera/sample.py': tree}
    rows, _ = candidates(trees)
    sites = CallIndex(trees).callers(rows[0])
    assert [kind for _, _, kind in sites] == ['config', 'config', 'explicit non-config', 'default']
    assert default_category(sites) == 'partial'


def test_scanner_resolves_import_aliases_constructors_and_same_named_functions():
    trees = {
        'tessera/one.py': ast.parse('def choose(capability="routine"): pass'),
        'tessera/two.py': ast.parse('def choose(capability="routine"): pass'),
        'tessera/client.py': ast.parse('class Client:\n def __init__(self, limit=20): pass'),
        'tessera/caller.py': ast.parse('''
from .one import choose as selected
from .client import Client as Peer
def run(config):
    selected(capability=config.capability)
    Peer(limit=config.limit)
'''),
    }
    rows, _ = candidates(trees)
    index = CallIndex(trees)
    by_path = {row.path: index.callers(row) for row in rows}
    assert by_path['tessera/one.py'][0][2] == 'config'
    assert by_path['tessera/two.py'] == []
    assert by_path['tessera/client.py'][0][2] == 'config'


def test_scanner_requires_an_actual_default_call_for_a_default_finding():
    assert default_category([('x.py', 1, 'config')]) == 'overridden'
    assert default_category([('x.py', 1, 'explicit non-config')]) == 'overridden'
    assert default_category([('x.py', 1, 'default')]) == 'defaults'
    assert default_category([]) == 'unproven'
    assert default_category([('x.py', 1, 'unknown (**kwargs)')]) == 'unproven'


def test_scanner_traces_config_between_methods_and_through_forwarded_arguments():
    tree = ast.parse('''
class Client:
    def __init__(self, config):
        self.settings = config.tools
    def run(self):
        forward(self.settings.limit)
def forward(budget):
    clip(limit=budget)
def clip(limit=20): pass
''')
    trees = {'tessera/sample.py': tree}
    rows, _ = candidates(trees)
    row = next(row for row in rows if row.name == 'clip.limit')
    assert CallIndex(trees).callers(row)[0][2] == 'config'


def test_scanner_recognizes_known_config_expansion_but_not_removed_keys():
    tree = ast.parse('''
class Judge:
    def __init__(self, risk_threshold=.7, goal_threshold=.8): pass
    @classmethod
    def from_config(cls, config):
        settings = asdict(config.jev)
        settings.pop('goal_threshold', None)
        return cls(**settings)
''')
    trees = {'tessera/sample.py': tree}
    rows, _ = candidates(trees)
    index = CallIndex(trees)
    assert index.callers(rows[0])[0][2] == 'config'
    assert index.callers(rows[1])[0][2] == 'unknown (**kwargs)'
