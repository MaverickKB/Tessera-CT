"""The three config inventories must name the same things.

The defect this exists to catch: Settings served a toggle for
`identity.enabled` while `load_config` did not read the `identity` table at all,
so saving reported "Saved" for a key the loader then discarded. The toggle looked
like it worked and the feature stayed off.

Three inventories describe the config, and nothing compared them:

  loader    -- what `load_config` accepts (tessera/config.py)
  template  -- what `config.example.toml` ships
  settings  -- what the Settings page offers (tessera/settings_model.py specs)

"Accepts" here means a top-level table OR a top-level scalar: `agent_name` and
`operator_name` are bare keys in the file and first-class Settings fields, so a
comparison against tables alone would report them as orphans when they are fine.

The loader inventory is parsed from the source with `ast` rather than imported,
because there is no single exported constant to import: `load_config` accepts
tables through three different mechanisms (an env-prefix tuple, a name/Config-class
loop, and direct `data.get` calls) plus a handful of top-level scalars. Parsing the
literals keeps this test reading the same lines a person would read, and it fails
loudly if the shape of those lines changes enough to be unparseable.
"""

import ast
import importlib.util
from pathlib import Path
import tempfile
import re
import subprocess
import sys
import tomllib

import pytest

from tessera.config import load_config
from tessera.settings_model import catalog

ROOT = Path(__file__).resolve().parents[1]
CONFIG_SOURCE = ROOT / "tessera" / "config.py"
TEMPLATE = ROOT / "config.example.toml"
REFERENCE = ROOT / "docs" / "reference" / "CONFIG.md"

# Top-level scalars load_config reads directly, as bare keys rather than tables.
SCALARS = {
    "cwd",
    "workspace",
    "agent_name",
    "operator_name",
    "user_instructions",
    "identity_file",
    "identity_max_bytes",
    "desktop_install_timeout",
    "desktop_startup_timeout",
    "desktop_appimage_extract",
}


def _str_tuple(node):
    """String literals from a tuple/list literal node, or None if not literal."""
    if not isinstance(node, (ast.Tuple, ast.List)):
        return None
    values = []
    for item in node.elts:
        if not isinstance(item, ast.Constant) or not isinstance(item.value, str):
            return None
        values.append(item.value)
    return values


def loader_inventory():
    """What `load_config` accepts, read from its source.

    Returns (tables, scalars). Raises if the source shape is not parseable, so a
    refactor that moves these declarations fails the test instead of silently
    producing an empty inventory that would pass every assertion.
    """
    tree = ast.parse(CONFIG_SOURCE.read_text())
    load = next(
        node for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "load_config"
    )

    tables = set()

    # 1. `sections = (...)` -- tables reachable through TESSERA_<SECTION>_<KEY>.
    env_sections = None
    for node in ast.walk(load):
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            target = node.targets[0]
            if isinstance(target, ast.Name) and target.id == "sections":
                env_sections = _str_tuple(node.value)
    assert env_sections is not None, (
        "load_config no longer declares `sections = (...)`; the env-prefix table "
        "inventory cannot be read. Update this test to match the new shape."
    )
    tables |= set(env_sections)

    # 2. `for name, cls in ((...), (...)):` -- dataclass-backed tables.
    dataclass_tables = set()
    for node in ast.walk(load):
        if not isinstance(node, ast.For) or not isinstance(node.iter, ast.Tuple):
            continue
        prefix = ""
        for child in ast.walk(node):
            if isinstance(child, ast.Call) and isinstance(child.func, ast.Attribute):
                base = child.func.value
                if isinstance(base, ast.Name) and base.id == "companions" and child.func.attr == "get":
                    prefix = "companions."
        for entry in node.iter.elts:
            if isinstance(entry, ast.Tuple) and len(entry.elts) == 2:
                name_node = entry.elts[0]
                if isinstance(name_node, ast.Constant) and isinstance(name_node.value, str):
                    dataclass_tables.add(prefix + name_node.value)
    assert dataclass_tables, (
        "load_config no longer declares the name/Config-class loop; the "
        "dataclass table inventory cannot be read."
    )
    tables |= dataclass_tables

    # 3. `data.get('x', ...)` -- tables loaded directly, e.g. backends, hosts.
    for node in ast.walk(load):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
            continue
        if node.func.attr != "get" or not node.args:
            continue
        base = node.func.value
        if not (isinstance(base, ast.Name) and base.id == "data"):
            continue
        arg = node.args[0]
        if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
            tables.add(arg.value)

    # 4. The scalar loop.
    scalars = set()
    for node in ast.walk(load):
        if not isinstance(node, ast.For):
            continue
        values = _str_tuple(node.iter)
        if values and set(values) == SCALARS:
            scalars |= set(values)

    return tables, scalars


def accepted(loader):
    tables, scalars = loader
    names = set(scalars)
    for table in tables:
        names.add(table.split(".")[0])
    return names


def template_keys():
    return set(tomllib.loads(TEMPLATE.read_text()))


def settings_paths():
    """Top-level names named by the Settings catalog's spec paths.

    A spec with no path (e.g. `permissions.mode`) is computed, not stored under a
    top-level key, so it contributes nothing here.
    """
    config = load_config(Path(tempfile.mkdtemp()))
    return {spec.path[0] for spec, _ in catalog(config, {}) if spec.path}


@pytest.fixture(scope="module")
def loader():
    return loader_inventory()


def test_a_settings_field_never_writes_a_key_the_loader_drops(loader):
    """Direction 1: every Settings spec path lands where load_config looks.

    This is the exact `identity.enabled` failure. A spec whose top-level name is
    absent from the loader saves into a key nothing reads: the page says Saved and
    the behavior does not change.
    """
    orphans = settings_paths() - accepted(loader)
    assert not orphans, (
        "Settings offers fields under top-level names load_config never reads, so "
        f"saving them reports Saved for a discarded key: {sorted(orphans)}. Either "
        "add the name to load_config or remove the Settings section."
    )


def test_the_shipped_template_never_carries_a_key_the_loader_drops(loader):
    """Direction 2: the template only ships keys load_config actually reads.

    A template key nothing reads is a documented setting that does nothing -- the
    same lie as the Settings toggle, one layer earlier.
    """
    orphans = template_keys() - accepted(loader)
    assert not orphans, (
        "config.example.toml ships top-level keys load_config never reads, so they "
        f"are documented settings that do nothing: {sorted(orphans)}."
    )


def test_the_loader_inventory_is_not_silently_empty(loader):
    """A guard on the guard.

    Every assertion above passes trivially against an empty inventory. If a
    refactor moves load_config's declarations, the parse must fail loudly rather
    than produce an empty set and report success.
    """
    tables, scalars = loader
    assert {"server", "voice", "identity", "backends", "companions"} <= tables, (
        f"loader table inventory looks wrong: {sorted(tables)}"
    )
    assert SCALARS <= scalars, f"scalar inventory incomplete: {sorted(scalars)}"


def test_settings_and_template_are_both_subsets_of_the_loader(loader):
    """Cross-check: both consumer inventories agree with the loader.

    The reverse direction and reference freshness are checked below.
    This preserves the guard that a Settings save lands where the loader reads.
    """
    names = accepted(loader)
    assert settings_paths() <= names
    assert template_keys() <= names
    # And the template should carry at least the tables a reader needs first.
    assert {"backends", "roles", "credentials"} <= template_keys()


def test_loader_template_and_reference_tables_agree(loader):
    """All top-level tables and the companions children appear in all three."""
    loader_tables, scalars = loader
    loader_tables = loader_tables - scalars
    template = tomllib.loads(TEMPLATE.read_text())
    template_tables = set()
    for name, value in template.items():
        if isinstance(value, dict):
            template_tables.add(name)
    for name in template.get("companions", {}):
        template_tables.add("companions." + name)
    reference_tables = set(re.findall(r"^#{2,3} `([^`]+)`$", REFERENCE.read_text(), re.MULTILINE))
    assert loader_tables == template_tables == reference_tables
    assert {"companions.atlas", "companions.ember"} <= loader_tables
    assert not {"atlas", "ember"} & loader_tables
    # Scalars are equally discoverable, without pretending they are tables.
    assert template_keys() == accepted(loader)
    reference_scalars = set(re.findall(r"^\| `([^`.]+)` \|", REFERENCE.read_text(), re.MULTILINE))
    assert reference_scalars == scalars


def test_setup_mentions_every_reference_table():
    """Every table the reference documents is either walked through or deferred.

    A plain substring search is not a test: ``ui`` is inside "guide", ``view``
    inside "review", ``status`` inside "status surface", so a lowercased
    `in` check passes for most of the 34 headings whether or not SETUP.md
    mentions them at all. A naive substring check still passes when the deferred
    table is deleted entirely.

    So count a table as covered only when SETUP.md names it as a config table:
    a TOML header ``[name]`` or ``[name.child]``, standalone or inside
    backticks, or a row in the deferred-section table. A passing mention of the
    word in prose ("the UI", "run status") is not coverage, because it does not
    tell a reader that a config table of that name exists.
    """
    headings = re.findall(r"^#{2,3} `([^`]+)`$", REFERENCE.read_text(), re.MULTILINE)
    assert headings, "No configuration table headings found in the reference"
    # companions.atlas and companions.ember are children of companions; the
    # parent row covers them, so compare on the top-level name.
    tables = {name.split(".")[0] for name in headings}

    setup = (ROOT / "docs" / "SETUP.md").read_text()
    deferred = set(re.findall(r"^\|\s*`([^`]+)`\s*\|", setup, re.MULTILINE))
    headers = set(re.findall(r"\[([A-Za-z_][A-Za-z0-9_]*)", setup))

    missing = sorted(tables - deferred - headers)
    assert not missing, (
        "SETUP.md must name every configuration table, either with its own "
        "walkthrough section or in the deferred-section table; missing: "
        f"{missing}"
    )


def test_the_setup_coverage_check_can_fail(tmp_path, monkeypatch):
    """The coverage test must go red when a deferred row is removed.

    A guard nobody has seen fail is not evidence. This asserts the predicate
    above actually discriminates, by feeding it a SETUP.md with one row
    deleted.
    """
    setup = (ROOT / "docs" / "SETUP.md").read_text()
    broken = setup.replace("| `mcp_servers` |", "| removed |")
    assert broken != setup, "fixture did not change SETUP.md"
    stub = tmp_path / "SETUP.md"
    stub.write_text(broken)

    headings = re.findall(r"^#{2,3} `([^`]+)`$", REFERENCE.read_text(), re.MULTILINE)
    tables = {name.split(".")[0] for name in headings}
    deferred = set(re.findall(r"^\|\s*`([^`]+)`\s*\|", broken, re.MULTILINE))
    headers = set(re.findall(r"\[([A-Za-z_][A-Za-z0-9_]*)", broken))
    assert "mcp_servers" in sorted(tables - deferred - headers)


def test_generated_config_reference_is_current(tmp_path, monkeypatch):
    output = tmp_path / "CONFIG.md"
    # Generation must not depend on an operator's config, cwd, or env overrides.
    home = tmp_path / "operator-home"
    home.mkdir()
    (home / "config.toml").write_text('operator_name = "PRIVATE_HOST_VALUE"\n')
    monkeypatch.setenv("TESSERA_HOME", str(home))
    monkeypatch.setenv("TESSERA_OPERATOR_NAME", "PRIVATE_ENV_VALUE")
    subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "gen-config-reference.py"), "--output", str(output)],
        cwd=tmp_path, check=True,
    )
    assert output.read_bytes() == REFERENCE.read_bytes(), (
        "Configuration reference is stale; run .venv/bin/python scripts/gen-config-reference.py"
    )


def test_reference_defaults_and_env_paths_follow_loader_rules():
    text = REFERENCE.read_text()
    assert '| `agents.allowed_scopes` | `[]` | `TESSERA_AGENTS_ALLOWED_SCOPES` |' in text
    assert '| `agents.backend_policy` | `{}` | `TESSERA_AGENTS_BACKEND_POLICY` |' in text
    assert '| `backends.*.model` | `""` | `TESSERA_BACKENDS__*__MODEL` |' in text
    assert '| `companions.atlas.enabled` | `false` | `TESSERA_COMPANIONS__ATLAS__ENABLED` |' in text
    assert '| `computer_use.jev.enabled` | `false` | `TESSERA_COMPUTER_USE__JEV__ENABLED` |' in text
    assert '| `ui.theme` | `"system"` | `TESSERA_UI__THEME` |' in text
    assert 'TESSERA_AETHERMIND_TOKEN_ENV' not in text
    assert 'TESSERA_BACKENDS__*__API_KEY_ENV' not in text
    assert 'default_factory' not in text


@pytest.fixture
def reference_generator():
    spec = importlib.util.spec_from_file_location(
        "config_reference_generator", ROOT / "scripts" / "gen-config-reference.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_generated_reference_preserves_availability_annotations(tmp_path):
    output = tmp_path / "CONFIG.md"
    subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "gen-config-reference.py"), "--output", str(output)],
        check=True,
    )
    rows = {}
    for line in output.read_text().splitlines():
        if line.startswith('| `'):
            path = line.split('`')[1]
            rows[path] = line
    retired = rows['tools.direct_route_tools']
    assert 'retired: every route offers the full registry; this list does not hide tools' in retired
    assert 'accepted for compatibility; ignored' in retired
    assert 'TESSERA_TOOLS_DIRECT_ROUTE_TOOLS' in retired
    file_only = 'file-only: no Settings row — set this in config.toml'
    assert file_only in rows['computer_use.text_chars']
    assert file_only in rows['companions.atlas.enabled']
    assert file_only in rows['cwd']
    assert file_only not in rows['ui.theme']
    assert file_only not in rows['computer_use.jev.enabled']
    # args is the tail of command in config.toml, so it has a consumer row and no key of its own.
    for key in ('enabled', 'url', 'command', 'env', 'token_env', 'session_tools'):
        assert 'Settings row' in rows[f'mcp_servers.*.{key}']
        assert 'file-only:' not in rows[f'mcp_servers.*.{key}']
    assert file_only in rows['mcp_servers.*.timeout']
    assert file_only in rows['mcp_servers.*.description']
    assert 'retired: memory is required' in rows['aethermind.enabled']


def test_file_only_annotations_follow_catalog_and_record_editors(reference_generator, monkeypatch):
    from tessera.config import Config
    from tessera.settings_model import Spec

    items = catalog(Config(), {})
    # Catalog specs, the generator's named record-row editors, and the Plugins
    # MCP fields. backends.*.max_concurrent_requests and mcp_servers.*.session_tools
    # are Settings rows even though catalog() does not list those per-record keys.
    paths = reference_generator.settings_paths()
    for line in reference_generator.render().splitlines():
        if line.startswith('| `'):
            path = line.split('`')[1]
            assert ('file-only:' in line) == (path not in paths), path

    # A newly represented key loses its marker; removing a spec adds one.
    changed = []
    for spec, value in items:
        if spec.path != ('ui', 'theme'):
            changed.append((spec, value))
    changed.append((Spec('computer_use.text_chars', 'computer', 'int', 'Text limit',
                         'restart', ('computer_use', 'text_chars')), 12000))
    monkeypatch.setattr(reference_generator, 'catalog', lambda config, approval: changed)
    rows = {}
    for line in reference_generator.render().splitlines():
        if line.startswith('| `'):
            rows[line.split('`')[1]] = line
    assert 'file-only:' not in rows['computer_use.text_chars']
    assert 'file-only:' in rows['ui.theme']
    assert 'file-only:' not in rows['mcp_servers.*.session_tools']
    assert 'file-only:' not in rows['mcp_servers.*.token_env']


def test_retired_direct_route_tools_still_loads(tmp_path):
    (tmp_path / 'config.toml').write_text('[tools]\ndirect_route_tools = ["read_file"]\n')
    config = load_config(tmp_path)
    assert config.tools.direct_route_tools == ['read_file']


def test_computer_use_text_chars_example_is_discoverable():
    text = TEMPLATE.read_text()
    computer_use = text.split('# [computer_use]\n', 1)[1].split('# [', 1)[0]
    assert '# text_chars = 12000' in computer_use
    assert 'File-only (no Settings row)' in computer_use


def test_a_wired_key_is_not_documented_as_retired(reference_generator):
    """autonomy.watch_sources is read now, so the reference must not call it retired.

    autonomy._endpoints_due() reads it with its own per-entry interval, and
    _poll_endpoints() fetches it. A reference entry that calls the key retired
    is a false claim, so the entry stays out.
    """
    rows = {}
    for line in reference_generator.render().splitlines():
        if line.startswith('| `'):
            rows[line.split('`')[1]] = line
    row = rows['autonomy.watch_sources']
    assert 'retired:' not in row, row
