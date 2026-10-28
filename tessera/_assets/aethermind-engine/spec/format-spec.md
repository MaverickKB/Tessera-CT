# AetherMind `.aem` Format Specification

**Version:** 1.2.0
**Last Updated:** 2026-08-05

**v1.2 additions:** `corrects` and `prev_hash` fields, append receipts,
hash-chain integrity, anchor-scoped deterministic `brief(anchor, budget)`,
gate contract (`must_know` + `gate_check`), split ledger (`events.aem`), and
archive-as-migration (`archive.aem` tombstone + continuation header). All
additions are backward compatible; existing records remain valid.

---

## 1. Directory Convention

Every project that adopts AetherMind must contain a hidden directory named
**`.aethermind/`** at the top level of the repository, side-by-side with source
code, build scripts, and other project artifacts. This directory holds all
persistent knowledge layers.

```
project_root/
├─ src/                # source code
├─ .aethermind/        # AetherMind knowledge store
│   ├─ layers.aem      # TOML‑encoded machine‑readable layers
│   └─ texture.aem     # Plain‑text felt‑sense entries
└─ ...
```

The directory is version‑controlled like any other source file, but **never edited manually** except through the prescribed tooling.

### Transport Boundary

AetherMind is a project-local file format and library contract. The durable
store lives where the project source lives, and the writer must run on the host
that owns that filesystem.

Transport mechanisms are adapters:

- local Python/library calls;
- the `aethermind` CLI, including a remote shell on the machine that owns the files;
- an optional host-local MCP server wrapping the same library.

Do not require every remote checkout or embedded project to host an MCP server.
The writer must run on the host that owns the project files. Do not send a
project root from one host to a writer running on another host.

---

## 2. `layers.aem` – Structured TOML Schema

`layers.aem` is an **append‑only** TOML file containing an array of tables named `[[layer]]`. Each table represents a single knowledge layer.

### Required Fields
| Field | Type | Description |
|------|------|-------------|
| `id` | string | Zero‑padded incremental identifier (e.g. `0011`). Must be unique within the file.
| `ts` | string (ISO‑8601 UTC) | Timestamp of creation, e.g. `2026-05-07T02:57:11Z`.
| `author` | string | Agent or human identifier (e.g. `example-agent`).
| `ctx` | string | Hierarchical context path, slash‑separated (e.g. `design/encoding`).
| `type` | string | Extensible lowercase semantic token matching `[a-z][a-z0-9-]{0,63}`. Common core values are `fork`, `friction`, `discovery`, `uncertainty`, `correction`, and `load-bearing`; domain values such as `decision` remain visible rather than being silently discarded. Structural behavior belongs in `primitive`, not `type`.
| `conf` | float (0‑1) | Confidence rating; 1.0 means absolutely certain.
| `body` | string | Free‑form textual payload. May contain **semantic markers** (`?`, `!`, `~`, `>`). Must be a single‑line string (line‑breaks are escaped as `\n`).

### Optional Light v1 Fields

AetherMind-Light v1 keeps every primitive representable as a normal `[[layer]]`
record plus optional fields. This is intentional: a small writer can append
useful machine-readable continuity without a separate ranking service,
an embedding index, a host registry, or extra TOML tables.

| Field | Type | Description |
|------|------|-------------|
| `primitive` | string | Optional primitive name. Defaults to `layer` when absent. Current v1 names are `layer`, `artifact-reference`, `anchor`, `supersession`, `rollback`, and `pressure-event`.
| `markers` | array[string] | Optional explicit semantic-marker cache. Values should use the defined marker set.
| `thread_key` | string | Optional stable claim or lineage identity. Within one store, the newest record for a non-empty thread key is the derived active head; prior records remain immutable history.
| `inherit` | string | Selection posture: `posture`, `fact`, or `both`. Facts are referred to source/fact backends rather than injected as posture when a selector supports the seam.
| `action_rule` | string | Bounded behavioral consequence used instead of truncating the body under a tight brief budget.
| `review_after` | string | ISO date or timezone-bearing timestamp after which a reader should flag the record for review, never silently delete it.
| `nutrition` | string | Advisory write-gate verdict: `dense`, `questionable`, `memory_shaped`, or `transcript`. It is not a truth score.
| `force_reason` | string | Reason a writer intentionally overrode a nutrition gate. Forced writes remain visible in health metrics.
| `supersedes` | array[string] | IDs this record supersedes. On ordinary `layer` records this is a relation; on `primitive = "supersession"` it is required. |
| `rollback_of` | array[string] | IDs, anchors, artifact refs, commits, or route IDs rolled back by this record. On ordinary `layer` records this is a relation; on `primitive = "rollback"` it is required. |
| `corrects` | array[string] | IDs of prior layers this record corrects. **Validated:** a correction targeting a layer that does not exist in the store is a hard error. |
| `prev_hash` | string | Canonical sha256 of the immediately preceding record. The writer carries it forward so audit is a scan, not a trust exercise. Legacy records without `prev_hash` begin the chain at the first post-upgrade write (the migration point). |
| `archived_to` | string | Archive destination (e.g. `archive.aem`) referenced by an archive tombstone. |
| `evidence` | array[string] | Short source paths, commands, URLs, task IDs, log refs, or other retrievable proof.
| `recurrence_of` | array[string] | Prior pressure or continuity IDs this event resembles.
| `verification` | array[string] | Commands or evidence proving a rollback or repair.
| `artifact` | string | Project-local artifact reference, usually a path, service name, host path, URL, generated artifact ID, or similar source-owned object.
| `artifact_ref` | string | ID of a separate `artifact-reference` record when one exists.
| `anchor` | string | Stable artifact-local anchor ID. It must not be a line number.
| `ref` | string | Required by `primitive = "artifact-reference"`; points to the artifact being referenced.
| `kind` | string | Optional artifact kind such as `file`, `directory`, `service`, `host-path`, `url`, `commit`, `generated-artifact`, or `other`.
| `label` | string | Short display label for an artifact reference.
| `host` | string | Host name only when needed to avoid cross-host ambiguity.
| `repo_root` | string | Project root relative to the current store when useful.
| `content_id` | string | Commit, checksum, release ID, or other version/content identifier.
| `selector` | string | Advisory selector for an anchor, such as a symbol, heading, test name, or service stanza.
| `span_hint` | string | Small text hint to help a reader find an anchored region after edits. Not source truth.
| `inline` | bool | Whether an anchor also exists in artifact text.
| `domain` | string | Required by `primitive = "pressure-event"`; area under pressure.
| `symptom` | string | Required by `primitive = "pressure-event"`; what was observed.
| `next_verification` | string | Required by `primitive = "pressure-event"`; smallest next check to confirm or retire the pressure.
| `suspected_mechanism` | string | Current best explanation for a pressure event.
| `scope` | string | Scope classification. For pressure events: `local`, `cross-domain`, `cross-host`, `profile`, `format`, or `unknown`. For supersession: `claim`, `anchor`, `artifact`, `route`, `format`, or `other`.
| `severity` | string | Pressure-event severity: `low`, `medium`, `high`, or `critical`.
| `owner_hint` | string | Human-readable possible owner or subsystem for a pressure event.
| `repair` | string | Repair already applied, if any.
| `reason` | string | Required by `primitive = "supersession"` and `primitive = "rollback"`; explains why the chain record exists.
| `replacement` | string | Optional ID of the replacement record for a standalone supersession.
| `restored_to` | string | Optional ID, commit, version, or state restored by a rollback.

Unknown additional fields are allowed only when their names begin with `x_`.
Readers must ignore unknown `x_` fields and must not rely on unprefixed fields
that are not defined in this specification.

### Primitive-Specific Requirements

| Primitive | Additional requirements |
|-----------|-------------------------|
| `layer` | No additional fields required. Legacy records without `primitive` are treated as `layer`.
| `artifact-reference` | Requires `ref`. Common optional fields: `kind`, `label`, `host`, `repo_root`, `content_id`.
| `anchor` | Requires `anchor` and either `artifact` or `artifact_ref`. Optional: `selector`, `span_hint`, `inline`.
| `supersession` | Requires non-empty `supersedes` and `reason`. Optional: `scope`, `replacement`, `artifact`, `anchor`.
| `rollback` | Requires non-empty `rollback_of` and `reason`. Optional: `artifact`, `anchor`, `restored_to`, `verification`.
| `pressure-event` | Requires `type = "friction"`, `domain`, `symptom`, non-empty `evidence`, and `next_verification`. Optional: `suspected_mechanism`, `scope`, `severity`, `recurrence_of`, `owner_hint`, `repair`.

Derived fields such as bucket IDs, reachability, freshness, rank,
retrieval score, pressure score, canonical glyphs, dedupe keys, and briefing
state are not Light source truth and must not be required for a conforming
Light writer.

### Derived currentness

Readers must preserve every record as immutable history while deriving a live
projection. Explicit `supersedes` and `rollback_of` edges deactivate their
targets. A non-empty `thread_key` defines one lineage and only its newest record
is the active head. Digest contexts (`*/digest`) are implicit lineages. A
normal briefing must omit inactive records from live posture while keeping the
history and inactive reason inspectable through a currentness surface. Digest
watermarks apply only to their own context subtree; an unrelated newer digest
must not hide another subtree's delta.

### Validation Rules
- **Append‑only:** Do not modify or delete existing tables.
- **Unique `id`:** Enforced by tooling; duplicate IDs cause a validation error.
- **Timestamp format:** New writes must use RFC 3339 with seconds (`YYYY-MM-DDTHH:MM:SSZ`). Readers accept historical timezone-bearing minute precision only for compatibility.
- **`type` value:** Must be a lowercase semantic token matching `[a-z][a-z0-9-]{0,63}`. Common values are listed below. Structural behavior belongs in `primitive`, not `type`.
- **`conf` range:** Must be between `0.0` and `1.0` inclusive.
- **`primitive` value:** If present, must be one of the six Light v1 primitive names.
- **`thread_key` value:** If present, must be a non-empty string.
- **Array fields:** `markers`, `supersedes`, `rollback_of`, `evidence`, `recurrence_of`, and `verification` must be arrays of strings.
- **Primitive requirements:** Standalone primitives must satisfy the additional requirements above.
- **Semantic markers:** Only the four defined markers may appear in `body`. Any other punctuation that resembles a marker must be escaped.

### Example Table
```toml
[[layer]]
id = "0011"
ts = "2026-05-07T02:57:11Z"
author = "example-agent"
ctx = "design/specification"
type = "load-bearing"
conf = 1.0
body = "!The TOML schema defined here is the contract for all AetherMind consumers. Any deviation breaks interoperability."
markers = ["!"]
primitive = "layer"
```

### Example Standalone Chain Record
```toml
[[layer]]
id = "0012"
ts = "2026-05-23T18:11:04Z"
author = "example-agent"
ctx = "design/chain"
type = "correction"
conf = 0.95
body = "A standalone rollback records that an earlier continuity claim no longer has current force."
markers = []
primitive = "rollback"
rollback_of = ["0011"]
reason = "The replacement introduced a regression."
restored_to = "commit:def456"
verification = ["pytest lib/tests/test_core_append_contract.py -q"]
```

---

## 3. `texture.aem` – Plain‑Text Felt‑Sense Entries

`texture.aem` captures the **subjective, invitation-to-attention** aspect of
AetherMind. It is deliberately human-readable and stores no machine-parsable
structure beyond the header line. It is a compatibility and attention surface,
not machine source truth for primitives.

### Line Format
```
YYYY-MM-DD [author | context]
<free‑form prose possibly containing semantic markers>
---
```
- The date **must** be UTC ISO date (`YYYY‑MM‑DD`).
- The header contains the author identifier and a slash‑separated context.
- Blank lines separate logical paragraphs.
- A line with three hyphens (`---`) terminates the entry.

### Semantic Markers
Same as `layers.aem`: `?` (uncertain), `!` (load‑bearing), `~` (felt sense), `>` (investigate). They are **advisory** for humans; parsers may ignore them.

### Example Entry
```
2026-05-07 [example-agent | design/specification]
!The format spec feels like a lighthouse: solid enough to guide ships, but simple enough that the light can be seen from far away.
---
```

---

## 4. Layer Type Definitions with Examples

| Type | Purpose | Example `type` value in `layers.aem` |
|------|---------|--------------------------------------|
| `fork` | Decision point where alternatives were considered and one was chosen. | `type = "fork"`
| `friction` | Known obstacle or friction point that required mitigation. | `type = "friction"`
| `discovery` | Insight or observation that was not previously known. | `type = "discovery"`
| `uncertainty` | Open question or unknown that requires future investigation. | `type = "uncertainty"`
| `correction` | Fix or amendment to a prior layer. | `type = "correction"`
| `load-bearing` | Fundamental principle that all downstream work depends on. | `type = "load-bearing"`

---

## 5. Semantic Marker Semantics

| Marker | Meaning | Recommended Usage |
|--------|---------|-------------------|
| `?` | Uncertainty – the author is not sure about the statement. | Use in `body` when the claim is speculative.
| `!` | Load‑bearing – the statement is essential; breaking it breaks the system. | Use for architectural contracts, invariants, versioning rules.
| `~` | Felt sense – a subjective impression, intuition, or aesthetic quality. | Use in `texture.aem` or when conveying human‑centric context.
| `>` | Investigate – a prompt to explore further, a TODO for future research. | Use sparingly; pair with a concrete follow‑up task.

**Combination Rules:**
- Markers may be combined in a single `body` line (e.g., `!?`), but the **primary** meaning must be the leftmost character.
- Do **not** place contradictory markers together (e.g., `?!`). Choose the dominant intent.
- `texture.aem` entries may contain any markers; they are informational only.

---

## 6. Versioning Strategy for the Format

1. **Semantic version** stored at the top of `format-spec.md` (e.g., `1.0.0`).
2. **Breaking change** → increment major version, add migration note.
3. **Additive change** (new optional field) → increment minor version.
4. **Documentation typo / clarification** → increment patch version.
5. Projects must **pin** a major version in their CI configuration. Minor/patch upgrades are optional.

---

## 7. Write Discipline

### What to Write (in `layers.aem`)
- **Orientation:** why the layer exists, the problem it solves.
- **Decision rationale:** arguments considered, why the chosen path is load‑bearing.
- **Concrete facts:** timestamps, identifiers, references.
- **Confidence score** reflecting certainty.

### What NOT to Write
- Operational logs, debug output, or transient state.
- TODO lists without an accompanying `>` marker (keep those in your task tracker).
- Full code snippets – reference them in a separate repository and link via URL.

---

## 8. Multi‑Author Convention

- Each author appends new layers; they never edit prior ones.
- `author` field uniquely identifies the creator (e.g., `example-agent`).
- When multiple agents contribute to the same logical decision, create **parallel layers** with the same `ctx` and a `?` or `!` indicating consensus status.
- Conflicts are resolved by adding a **correction** layer that references the conflicting `id`s.

---

## 9. Encoding Reliability Guidance

- Use **pure UTF‑8** text; avoid non‑ASCII symbols except the four semantic markers.
- Keep lines under **200 characters** to ensure readability across terminals.
- Do **not** rely on hidden whitespace; all significance must be explicit.
- When serialising to other formats (JSON, YAML), preserve field names exactly.
- The `.aem` extension signals to downstream tools that the file follows this spec.

---

*End of Specification*
