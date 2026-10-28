# AetherMind

Project-local append-only continuity layers.

A layer is one record: who wrote it, when, which context it belongs to, how
sure they were, and the fact itself. Records live in `.aethermind/` beside the
project they describe. Later sessions read them. They do not replace them.

Installing this package does not start a server, open a port, or create a store
in this repository. The license is public and noncommercial. See `LICENSE`.

## Install

Python 3.9 or newer.

```bash
pip install -e .
```

The command `aethermind` is installed with the package. Optional pieces:

```bash
pip install -e '.[mcp]'    # MCP server
pip install -e '.[test]'   # pytest
```

## Start a store

From the project you want to describe:

```bash
aethermind init --seed
aethermind status
aethermind read-layers
```

`--seed` appends three generic layers and one texture note that explain the
format. It refuses a store that already has layers. The seed is not project
history. Delete `.aethermind/` if you want an empty start instead:

```bash
aethermind init
```

`init` without `--seed` creates store metadata and writes no layers.

## Write your own layer

```bash
aethermind write-layer \
  --project-root . \
  --create \
  --type discovery \
  --ctx work/note \
  --body "The writer must run on the host that owns these files." \
  --conf 1.0 \
  --author example-agent
```

A correction is a new layer. The old layer stays.

Other commands: `read-texture`, `brief`, `brief-anchor`, `audit`, `gate-check`,
`currentness`, `write-event`, `read-events`, `archive`, `capabilities`.
Run `aethermind --help` for the current list.

## Tests

The server tests import the optional MCP extra.

```bash
pip install -e '.[mcp,test]'
python -m pytest lib/tests -q
```

## Files

| Path | Role |
|---|---|
| `.aethermind/layers.aem` | Append-only layer records |
| `.aethermind/texture.aem` | Attention notes, not decision authority |
| `.aethermind/events.aem` | Routine events, separate from layers |
| `.aethermind/archive.aem` | Archived copies plus a tombstone in the layer file |
| `.aethermind/project.json` | Store metadata |
| `.aethermind/project.identity.json` | Optional identity file. Absent is valid |
| `.aethermind/tree.aem` | Derived summary. Rebuildable. Never the authority |

The format contract is `spec/format-spec.md`.

## Optional summary tree

Compaction calls an OpenAI-compatible chat endpoint that you configure.
There is no built-in URL or model.

```bash
export AETHERMIND_COMPACTOR_URL=http://127.0.0.1:8000
export AETHERMIND_COMPACTOR_MODEL=your-model-name
```

Importing the library does not call that endpoint.

## Optional project-key registry

`register_project_key` writes a host-local alias file so a short key can stand
in for a filesystem path. The default path is `~/.aethermind/project_registry.json`.
Set `AETHERMIND_REGISTRY` to another file if that path is already in use.
Registration does not merge two stores.

## Optional MCP server

See `mcp/README.md`. The default bind is `127.0.0.1:8452`. The server does not
start on install.

## What this repository is not

It does not ship a running service, a filled store, account credentials, or a
required remote dependency. A fresh clone plus `aethermind init --seed` in a
directory you choose is the whole bootstrap.
