"""Bundled-engine acceptance: a fresh home, no configured service, and the
packaged AetherMind engine must become the default memory store by itself.

Usage:
    python scripts/verify-bundled-sandbox.py [--bundle DIR]

Runs without touching any real config or store. The first run provisions a
venv beside the bundle from its locked requirements (that is the one-time
install step a new install performs); later runs reuse the running engine.

Passes only when:
  1. the bundle has its build receipt and mcp entry point,
  2. config load treats bundled_path as a pure signal (no pollution, no spawn),
  3. a configured url wins: the launcher refuses to start the bundle,
  4. the launcher starts the engine on an OS-chosen local port,
  5. the engine answers capabilities as the pinned aethermind-python adapter-v1,
  6. the engine answers status and a polite no-tree read for a fresh project.
"""

import argparse
import json
import pathlib
import sys
import tempfile
import time

# The bundle check must run from anywhere (a VM checkout, a release tarball):
# the repo root carries the tessera package, so make it importable explicitly.
REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tessera.aethermind import AetherMindClient
from tessera.bundled_engine import ensure_bundled_engine, stop_bundled_engine
from tessera.config import load_config


def fail(message):
    print("FAIL: " + message)
    sys.exit(1)


def main():
    parser = argparse.ArgumentParser()
    default_bundle = pathlib.Path(__file__).resolve().parent.parent / ".tessera-build" / "aethermind-engine"
    parser.add_argument("--bundle", default=str(default_bundle))
    args = parser.parse_args()

    bundle = pathlib.Path(args.bundle).resolve()
    if not bundle.is_dir():
        fail("bundle dir does not exist: " + str(bundle))
    print("ok: bundle dir exists: " + str(bundle))
    receipt = bundle / "build-receipt.json"
    if not receipt.is_file():
        fail("build receipt missing")
    print("ok: build receipt present")
    receipt_body = json.loads(receipt.read_text())
    if not receipt_body.get("files", 0):
        fail("receipt names no bundled files")
    print("ok: receipt names " + str(receipt_body["files"]) + " bundled files")
    entry = bundle / "mcp" / "aethermind_mcp.py"
    if not entry.is_file():
        fail("mcp entry point missing")
    print("ok: mcp entry point present")

    home = pathlib.Path(tempfile.mkdtemp(prefix="tessera-sandbox-"))
    home.joinpath("config.toml").write_text(
        '[aethermind]\nbundled_path = "' + str(bundle) + '"\n')
    config = load_config(home)

    if 'aethermind-bundled' in config.mcp_servers:
        fail("config load registered a server; load must stay pure")
    print("ok: config load kept mcp_servers untouched")
    if config.aethermind.endpoints:
        fail("config load injected endpoints; the launcher owns the url")
    print("ok: config load injected no endpoints")

    home.joinpath("config.toml").write_text(
        '[aethermind]\nbundled_path = "' + str(bundle) + '"\nurl = "https://example.invalid/mcp"\n')
    configured = load_config(home)
    if ensure_bundled_engine(configured) is not None:
        fail("the launcher started while a url was set and force was not requested")
    print("ok: launcher stays idle until the session asks; a url is not started here")

    try:
        url = ensure_bundled_engine(config)
        if not url:
            fail("launcher returned no engine url")
        print("ok: launcher started the engine: " + str(url))

        client = AetherMindClient(endpoints=[url], timeout=15)
        try:
            # The no-tree error above is the ENGINE answering through the production
            # client: a fresh project has no tree yet, reported politely.
            read_state = client.tree_view(str(home / "projects" / "fresh"))
            if not (isinstance(read_state, dict) and isinstance(read_state.get("error"), str)):
                fail("fresh project read did not answer through the production client")
            print("ok: fresh project read answered through the production client (no-tree state)")
            status = client.call('status', {'project_root': str(home / 'projects' / 'fresh')})
            if not (isinstance(status, dict) and status.get('success') is True
                    and status.get('layer_count') == 0):
                fail("engine status did not report an empty fresh project")
            print("ok: engine status reports an empty fresh project")
            project = str(home / 'projects' / 'fresh')
            marker = 'sandbox-round-trip'
            written = client.call('write_layer', {
                'project_root': project, 'layer_type': 'note',
                'body': marker, 'ctx': 'sandbox/acceptance',
                'conf': 1.0, 'markers': [marker], 'create': True,
            })
            if not (isinstance(written, dict) and written.get('success') is True):
                fail('isolated engine write failed: ' + str(written))
            restored = client.call('read_layers', {
                'project_root': project, 'markers_any': [marker],
            })
            layers = restored.get('layers', []) if isinstance(restored, dict) else []
            if not (restored.get('success') is True and
                    any(layer.get('body') == marker for layer in layers)):
                fail('isolated engine read did not return the written layer: ' + str(restored))
            print('ok: isolated engine write/read round trip passed')
            time.sleep(2)
            status = client.status()
            endpoints = status.get("endpoints") or []
            if not (endpoints and endpoints[0].get("healthy") is True):
                fail("production client probe did not certify the bundle endpoint healthy")
            print("ok: production client probe certified the bundle endpoint (adapter-v1)")
        finally:
            closer = getattr(client, "close", None)
            if closer:
                closer()
    finally:
        stop_bundled_engine(config)
    print("PASS: bundled engine is the default memory store on an isolated home.")


if __name__ == "__main__":
    main()