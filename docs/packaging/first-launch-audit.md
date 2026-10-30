# First-launch and seed validation audit

Audited 2026-10-30 against `23e7de9585a28578e3ba483afa53c41fa5b84db1`
on Linux with Python 3.12. Changes leave the pinned vendored engine bytes intact.

## Findings and repairs

1. **Existing seed bodies could conceal invalid records on relaunch.** The
   installer previously hashed raw TOML bodies, then skipped every matching
   seed without invoking the engine reader. An invalid author or other record
   field could survive indefinitely. The installer now uses the engine's
   strict reader before deduplication and reads back the complete rule set
   after writing. Parse failures reach the existing recovery path: preserve
   the entire old `.aethermind` directory under `.aethermind.corrupt-*`, then
   seed a fresh store. A write that silently loses a rule now refuses startup.
2. **An open TCP port was sufficient to report the engine ready.** Startup
   now requires MCP initialization and discovery of `read_layers` and
   `write_layer`, while checking that the child is still alive. Incomplete
   startup is retried within the existing readiness deadline.
3. **A web test expected obsolete Linux service guidance.** The Linux shell
   correctly tells users to control an externally started core where it was
   launched. Its test now checks that guidance instead of macOS LaunchAgent
   wording; product behavior is unchanged.

## Verification

- Built a wheel, installed it without dependencies in an isolated venv, and
  launched from outside the checkout with an entirely new Tessera home.
  Startup provisioned its own runtime using the packaged hashed MCP lock.
- Used the production `AetherMindClient` over HTTP MCP to verify every seed
  rule: **31 layers, 6 textures, 5 staged skills**.
- Relaunched in a separate Python process using the same home. The layers
  file's SHA-256 stayed identical, with no duplicate layers or textures.
- Deliberately emptied an existing record's author while preserving all seed
  bodies. A subsequent installed-wheel launch quarantined the damaged store
  intact and exposed all 31 valid seed layers again.
- `scripts/verify-bundled-sandbox.py` now checks full seed readback and
  byte-identical layers and textures after stop/relaunch, in addition to its
  existing capability, status and memory write/read checks. It passed against
  the installed wheel's bundle.
- `python scripts/smoke.py` passed health, turn and memory-write checks.
- `node --test web/tests/*.test.js`: **126 passed**.
- Targeted seed, startup, session-launch and wheel-contract tests: **74 passed**.
- The broad Python run completed with **2,845 passed, 17 skipped, 12 failed**.
  All 12 failures were test-environment setup issues: a repository-wide asset
  scan saw `build/lib` left by the wheel build (one failure), and subprocess
  guard tests could not import Tessera before editable installation (11).
  Removing generated build output and installing the checkout resolved them;
  the affected files plus hardwire checks reran with **22 passed**. The entire
  broad suite was not repeated after those setup corrections.

## Supported scope and remaining limits

The embedded engine starts on demand when a session requests memory. Config
loading and passive status reads intentionally do not start it. `tessera start`
creates/heals config and seeds the configured store before serving the UI;
the engine launch path also seeds, covering sessions created without that verb.

First-time provisioning needs Python's `venv`/pip and access to the distributions
in the packaged requirements lock. This audit verified that download path;
it does not establish offline installation on an empty machine.

Native Windows cannot write seeds with the pinned engine's unavailable locking
backend. The current product labels that state and refuses memory-dependent
turns; WSL follows Linux behavior. No native Windows or macOS desktop bundle
was executed in this Linux audit. Provider credentials, live external model
services, microphone/voice hardware and native desktop packaging require their
own platform or service acceptance runs.
