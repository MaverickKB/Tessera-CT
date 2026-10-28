# Packaging provenance

This directory holds the bundle provenance artifacts for a Tessera release.

## Vendored engine (release wheel)

The embedded AetherMind ships inside this repository.
`tessera/_assets/aethermind-engine` is public commit
`ec1011eebe00e8197e29acbbb3ae710090f53899`, byte for byte. Its license, kept with the
copy, is the AetherMind Noncommercial Public License.

`aethermind-engine-manifest.json` records the upstream repository, the full commit, and a
sha256 of every vendored file. A wheel build verifies the tree against that
manifest and refuses to build on any miss. `TESSERA_BUNDLE_SOURCE` may name a
different directory holding the same bytes. It is an override, not a
requirement, and it is held to the same manifest.

`aethermind-mcp-requirements.lock` is Tessera's pinned, hashed requirements
file for the engine's MCP server. It is not an engine file: the vendored
commit does not ship `mcp/requirements-mcp.lock`. The lock is built from the
vendored `pyproject.toml` extra `mcp==1.27.1`, which is what
`mcp/aethermind_mcp.py` imports (`mcp.server.fastmcp` and `pydantic`), plus
the dependencies that extra requires. A wheel copies it to
`tessera/_assets/aethermind-mcp-requirements.lock`. The first session that
uses the embedded engine installs that file into a fresh home venv. A missing
or unhashed lock fails the wheel build, and a missing lock at startup names
this file. `TESSERA_BUNDLE_SOURCE` does not supply it.

The release wheel does not build from an older pin of a tree served somewhere else.

## Source checkout (no wheel receipt)

A source checkout has the same vendored bytes but no `build-receipt.json`. The
embedded engine would not start if that receipt were the only gate. `tessera
init` and `tessera serve` call `ensure_local_engine_bundle`: it verifies the
vendored tree against the manifest, copies it once into
`<home>/runtime/aethermind/bundles/<manifest-hash>`, and writes a build receipt
in that copy. `engine_packaging` reports `present` before that copy exists and
`bundled` after it. A tampered tree is not copied. An explicit
`aethermind.bundled_path` is still receipt-gated, so a named copy is trusted
only when it has a receipt.

## Older engine pins

The release wheel checks `aethermind-engine-manifest.json` only.
`scripts/build-aethermind-bundle.sh` and `scripts/verify-aethermind-manifest.sh`
are for an operator who still has an older pin's bytes; the manifest they use,
`aethermind-engine-manifest-20261023.json`, ships beside them in this directory.
They do not name a host. Set `TESSERA_PACKAGING_HOST` when the tree is on
another machine. With no host, set `TESSERA_ENGINE_SOURCE=dir` or
`TESSERA_ENGINE_LOCAL=1` and pass `TESSERA_ENGINE_ROOT` for a tree on this
machine.

## Where the pinned bytes come from

`scripts/build-aethermind-bundle.sh` takes the pinned bytes from one of three sources,
selected by `TESSERA_ENGINE_SOURCE`:

| Source | Needs | Use when |
| --- | --- | --- |
| `host` (default) | `TESSERA_ENGINE_ROOT` and `TESSERA_PACKAGING_HOST` (required; no default host) | The serving host still holds the pinned bytes. |
| `dir` | `TESSERA_ENGINE_ROOT` on this machine | A verified copy of the pin survives locally. |
| `archive` | `TESSERA_ENGINE_ARCHIVE` (a tarball) | Rebuilding from the provenance archive kept off-machine. |

The `archive` source exists because the only surviving copy of that historical
pin was, for a while, in a gitignored directory on one development machine.
A newer public commit is vendored in this repository instead; the script still
checks the older pin and is not how the release wheel is built.

`scripts/verify-aethermind-manifest.sh` takes `TESSERA_ENGINE_LOCAL=1` to check
a tree on this machine, so the pin can be verified without a serving host.

Every source is verified against the manifest before the build tree is
accepted, and the build receipt records which source was used.

## Building the public release copy

`scripts/build_release.py` writes one commit's tree minus the paths in
`release/exclude.txt`, then runs two checks over the result:

```sh
python scripts/build_release.py --output ../tessera-release
```

- **References.** No README, doc, or test may name an omitted path. A hit fails
  the build and prints the file and line.
- **Privacy.** `scripts/check_release_privacy.py` refuses home paths, private and
  tailnet addresses, mDNS hostnames, and email addresses outside the example
  domains. Its patterns are in `release/privacy_patterns.json`, which describes
  classes of private data and holds no private value. A gitignored
  `release/privacy_terms.local.txt` adds literal words (one term or `re:` regex
  per line) that must never ship; a build on a machine without that file still
  runs every class pattern. A copyright line is the one place a holder's name is
  allowed, and the class patterns still run on that line.

Build the copy outside a home directory that is itself a git checkout. `git` and
`scripts/check_no_hardwires.sh` resolve the enclosing repository, so a copy inside
one is scanned as part of that repository instead of as a standalone tree.

The copy has no `.git`. A test that asks git which files are tracked, or archives
`HEAD`, skips there with "release copy: this tree is not a git checkout" and the
suite reports it as a skip, not a failure. Everything else runs unchanged:

```sh
cd ../tessera-release
PYTHONDONTWRITEBYTECODE=1 python -m pytest -q -p no:cacheprovider
node --test web/tests/*.test.js
```

A wheel, a service unit, and an installer are packaging work; this script only
produces the source tree a release starts from.
