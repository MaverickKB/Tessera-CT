# Integration scenarios

Run from the checkout with `.venv/bin/python -m evals.run_evals`. The runner uses
the real `Session` and the core `tests/fake_backend.py` fixture. It creates
temporary chats and files and writes `evals/results.json`. No credentials,
configured backends or live memory services are used.

Scenarios check direct replies, a file tool and captured outcome, risk blocking
before deletion, offline turn/session debt and recovery, registered memory
tools, and backend failure capture. Pass/fail comes from observable checks.
Each scenario also calls `JevRouter.judge()` with the offline heuristic; that
score is advisory and does not establish model quality. The deliberately
blocked and failed scenarios can pass their checks while receiving low reply
scores.

`.venv/bin/python -m evals.smoke_integrations` is a separate optional live smoke.
It makes one typed Score call when the judgment integration is explicitly enabled
and its endpoint, model and credential environment reference are configured through
the `TESSERA_JEV_*` settings. A credential alone does not enable remote calls. If
`TESSERA_AETHERMIND_URL` is set, it initializes MCP and lists tools, following
pagination. It uses `TESSERA_AETHERMIND_TOKEN_ENV` (default `AETHERMIND_TOKEN`)
as the environment variable name for the optional bearer. It never writes a
live memory layer. Results go to `evals/smoke_results.json`; absent credentials
or URL produce explicit skipped records.

Integration configuration details:

- `Capture(..., anchor=..., inheritance_budget=..., personal_budget=...)` bounds
  inherited context. A session-start hook may also supply `anchor`.
- `Capture(..., secret_envs=[...])` redacts additional configured credential
  values before journaling. The standard memory and TypeSafe variables are
  covered by default. Session also applies its configured credential redaction.
- `JevRouter(..., risk_threshold=0.7, approval="ask", approve=callback)` configures
  the risk hook. Session supplies the native policy and approval callback.
  A supplied router can set a different threshold. The threshold is currently
  a Python API option rather than a TOML option.
- Debt keeps the original target project and immutable layer payload. Retries
  require server-side deduplication of `request_id`; the local journal cannot
  provide exactly-once delivery across a lost network acknowledgement alone.

This is a working starting point. The append-only outbox currently replays its
journal to inspect debt; it has no automatic journal compaction. A failed oldest
write stops that drain attempt, avoiding a timeout for every queued item when
the service is down. Complete corrupt journal records fail visibly; an
incomplete final append is recovered. Known offline captures remain visible as
write-debt until a correctly configured service acknowledges them.
