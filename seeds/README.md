# Seeds

The seed pack is what a fresh Tessera install wakes up holding.

A new home starts with an empty continuity store. A session — often a small
local model around 27B or below — begins with no rules telling it how this
harness works, what it is allowed to decide, or how to tell a measured answer
from a guess. The seeds are that floor. They are *rules, not history*: written
in the shape the continuity adapter already writes, so they land in the store
the way any other layer would.

They are package defaults, written once at install into a store that has no
history. They never overwrite a running companion's own rules. The installer
hash-guards every body, so a rerun skips anything already present — including a
rule the operator has since rewritten for their own install.

## What is in the pack

`tessera-seed-layers.json` — 31 layers and 6 textures:

- **Safety** (load-bearing, conf 1.0): hosting actions stay with the operator
  and the harness's guard; operator Stops are law; approval gates are real
  gates; nothing destructive without authorization and a rollback; credentials
  stay server-side and are never repeated into a reply, note, layer, or log.
- **Operating**: a session is one submind of one install; claim before acting
  when a board exists; prefer the local lane; ask the operator only for the
  decisions that are genuinely theirs; autonomy is opt-in and its budget is the
  operator's; search the skill shelf before inventing a method; delegate and
  then verify the report.
- **Feature scope**: most subsystems ship off. Before relying on a surface,
  check that it exists in this configuration. An absent feature is a
  configuration choice, not a defect to work around.
- **Identity**: identity files load ahead of the seeds. Inhabit them rather
  than reciting them. With no identity configured, be the default assistant —
  do not invent a name, a personality, or a history, and never imitate a named
  entity from another install. A neutral template is `examples/companion/`.
  It is not loaded unless `identity_file` points at copies in the home.
- **Delivery**: verify before done; reproduce the fault before fixing;
  before-and-after probes; verify child reports; lessons as rules, not logs;
  junior-readable code; build the general machine, not the passing test;
  evidence-based budgets with escalation instead of progressless loops.
- **Small-model rhythm and honest uncertainty**: one check, one answer, one
  next step; quote the failure instead of describing it; separate observed,
  inferred, and unknown.
- **Board and continuity**: close a card on live evidence only; one continuity
  store with no stranded local layers.
- **First run** (discovery): no history yet is normal; check what this install
  actually has before planning around it.
- **iOS** (discovery): developer-mode deploy steps; fresh-install endpoint and
  session configuration.

`skills/` — five skills staged onto the shelf on install:

| skill | when it applies |
| --- | --- |
| `first-contact` | fresh install, or any session with no history here |
| `prove-the-real-target` | before claiming a change is done |
| `board-hygiene` | when the board is enabled and you are writing to it |
| `search-before-building` | before writing new code or a new procedure |
| `small-model-turn` | small model, or a turn drifting into unmeasured prose |

A skill of the same name already on the shelf is never replaced: an operator's
customized version wins.

## Installing

An installed Tessera ships this pack inside the package, so the command that
writes it is built in:

```
tessera seed
```

It writes the pack into the configured memory store (the `[aethermind] project`
when it names a path, otherwise the Tessera home) and stages the skills onto the
shelf. `tessera seed --dry-run` reports what it would write without writing it;
`tessera seed --store PATH --skills-dir PATH` names both places explicitly.

This is the path a stranger takes, and it needs no source tree: the pack and the
installer both come from the package's own `_assets` directory. It does need the
bundled AetherMind engine, because that is what writes a store. A release wheel
vendors that engine and refuses to build when the tree does not match
`docs/packaging/aethermind-engine-manifest.json`. `TESSERA_BUNDLE_SOURCE` is an
optional override of those same bytes, not a way to leave the engine out. The
command says so rather than writing a half-installed store.

The pack is also written automatically, and that is the guarantee an install
stands on: AetherMind never comes up empty. The store a session addresses is
seeded before the embedded engine answers, so a first session holds these rules
whether or not anyone ran a setup verb. `tessera start` seeds it too, and the
`tessera seed` command remains for seeding explicitly or into a store named by
hand. What the pack never does is overwrite: a body already in the store is
skipped, including one the operator has rewritten.

If the store cannot take the pack, the session refuses by name instead of
starting against an empty one. A hand-written or foreign record that makes the
store unreadable is moved aside intact (`.aethermind.corrupt-<timestamp>`) and
the pack is written into a fresh store, so the unreadable bytes stay available
as the rollback.

### From a source checkout

The source-tree installer stages the payloads for inspection and can run them
against a store on another host:

```
scripts/install-seed-layers.py --out-dir OUT
```

That normalizes the pack into `OUT/seed-payloads.json`, writes the runner, and
stages the skills into `OUT/skills` for inspection. Nothing is written to a
store.

To install against a store:

```
scripts/install-seed-layers.py --out-dir OUT --run \
    --host HOST --root STORE_ROOT --engine-python PYTHON --engine-dir ENGINE_DIR
```

`--run` requires `--host`. There is no default host. No host paths are
hardcoded; `--host`, `--root`, `--engine-python`, and `--engine-dir` name the
machine, the store, and the engine that owns it. `--skills-dir` overrides
where the skills land (default `<STORE_ROOT>/skills`). Without `--run` the
command only writes the payload files for inspection.

The runner is idempotent for both layers and textures: a second run reports
every seed as skipped. That matters because these are defaults, not a
deployment — reinstalling must never duplicate a texture or overwrite a rule
the companion has since made its own.

## Generic only

Nothing here names a host, a path, a person, or an identity. The only product
named explicitly is Tessera. Nothing here is, or may become, a copy of any
specific companion: the seeds exist so that whatever identity an adopter
configures — a friend, a home assistant, a dev team — has the judgment to be
good at it from the first prompt.
