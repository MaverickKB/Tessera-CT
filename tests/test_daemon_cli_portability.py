"""The daemon/terminal core must import without native macOS dependencies."""

import ast
import os
from pathlib import Path
import subprocess
import sys
import tomllib

import pytest


ROOT = Path(__file__).resolve().parents[1]
MAC_MODULES = {"AppKit", "Foundation", "Cocoa", "Quartz", "AVFoundation", "SwiftUI", "objc"}


def core_paths():
    files = []
    for name in ("__main__", "cli", "terminal", "rpc_client", "rpc", "daemon", "app_server", "mirror"):
        path = ROOT / "tessera" / (name + ".py")
        if path.is_file():
            files.append(path)
    metadata = tomllib.loads((ROOT / "pyproject.toml").read_text())
    entry = metadata["project"]["scripts"]["tessera"].split(":")[0]
    path = ROOT.joinpath(*entry.split(".")).with_suffix(".py")
    assert path.is_file(), "The console entrypoint must exist"
    if path not in files:
        files.append(path)
    return files


def mac_imports(source):
    violations = []
    for node in ast.walk(ast.parse(source)):
        names = []
        if isinstance(node, ast.Import):
            for alias in node.names:
                names.append(alias.name)
        elif isinstance(node, ast.ImportFrom):
            names.append(node.module or "")
        elif isinstance(node, ast.Call) and node.args and isinstance(node.args[0], ast.Constant):
            function = node.func
            dynamic = isinstance(function, ast.Name) and function.id == "__import__"
            dynamic = dynamic or isinstance(function, ast.Attribute) and function.attr == "import_module"
            if dynamic and isinstance(node.args[0].value, str):
                names.append(node.args[0].value)
        for name in names:
            if name.split(".")[0] in MAC_MODULES:
                violations.append(node.lineno)
    return violations


@pytest.mark.parametrize("source", [
    "import AppKit", "from Foundation import NSObject", "import objc as native",
    "__import__('SwiftUI')", "importlib.import_module('Quartz')",
])
def test_native_dependency_check_rejects_direct_and_dynamic_imports(source):
    assert mac_imports(source) == [1]


def test_core_entrypoints_have_no_native_mac_imports_or_platform_gates():
    for path in core_paths():
        source = path.read_text()
        assert not mac_imports(source), path.name
        tree = ast.parse(source)
        aliases = {"sys": "sys", "platform": "platform"}
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for name in node.names:
                    aliases[name.asname or name.name] = name.name
            elif isinstance(node, ast.ImportFrom) and node.module in ("platform", "sys"):
                for name in node.names:
                    aliases[name.asname or name.name] = node.module + "." + name.name
        for node in ast.walk(tree):
            if not isinstance(node, (ast.If, ast.IfExp, ast.While, ast.Assert)):
                continue
            for expression in ast.walk(node.test):
                if isinstance(expression, ast.Name):
                    selected = aliases.get(expression.id)
                    assert selected not in {"sys.platform", "platform.system", "platform.mac_ver"}, path.name
                if isinstance(expression, ast.Attribute) and isinstance(expression.value, ast.Name):
                    selected = aliases.get(expression.value.id, expression.value.id) + "." + expression.attr
                    assert selected not in {"sys.platform", "platform.system", "platform.mac_ver"}, path.name


def test_linux_import_simulation_never_loads_native_helpers_or_network(tmp_path):
    metadata = tomllib.loads((ROOT / "pyproject.toml").read_text())
    entry = metadata["project"]["scripts"]["tessera"]
    probe = r"""
import atexit
import builtins
import importlib
import importlib.util
import os
import platform
import socket
import sys

attempts = []
def reject(*args, **kwargs):
    attempts.append(True)
    raise AssertionError('Core import must be offline and platform independent')

def finish():
    if attempts:
        os._exit(75)

atexit.register(finish)
original = builtins.__import__
def portable_import(name, *args, **kwargs):
    if name.split('.')[0] in {'AppKit', 'Foundation', 'Cocoa', 'Quartz', 'AVFoundation', 'SwiftUI', 'objc'}:
        reject()
    return original(name, *args, **kwargs)

builtins.__import__ = portable_import
socket.socket.connect = reject
socket.socket.connect_ex = reject
socket.getaddrinfo = reject
sys.platform = 'linux'
platform.system = lambda: 'Linux'
module, function = sys.argv[1].split(':')
main = getattr(importlib.import_module(module), function)
for module in ('tessera.daemon', 'tessera.app_server'):
    if importlib.util.find_spec(module) is not None:
        importlib.import_module(module)
try:
    result = main(['--help'])
except SystemExit as stopped:
    result = stopped.code
assert result in (None, 0)
"""
    environment = dict(os.environ)
    for name in list(environment):
        if name.startswith("TESSERA_"):
            environment.pop(name)
    environment["TESSERA_HOME"] = str(tmp_path / "home")
    result = subprocess.run([sys.executable, "-c", probe, entry], cwd=ROOT, env=environment,
                            capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stderr
    assert "usage:" in result.stdout.lower()
    assert not (tmp_path / "home").exists(), "Import/help cannot initialize persistent state"
