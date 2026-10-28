# Explicit durable swarm job

This milestone uses offline fakes and generated official-CLI protocol fixtures. It makes no live multi-provider acceptance claim.

Configure named accounts and backends, then enable the existing swarm setting. At least two workers must choose different backends and account pools with independent scopes. API credentials stay on HTTP adapters; CLI workers use the selected official client's own login. No worker extracts tokens from client stores.

Settings belong in the operator's home configuration or corresponding settings environment variables:

```toml
[swarm]
enabled = true
jobs_dir = "<JOB_STORE_DIRECTORY>"
max_job_tasks = 8
max_artifact_bytes = 65536
gates_enabled = false

[board]
enabled = true
```

Set endpoint, model, credential references and provider kind on the chosen settings as documented for existing accounts/backends. Account-bound backends inherit provider kind and credentials from their account. Empty provider configuration and disabled swarm cannot execute jobs. Jev is optional; enabling swarm gates alone does not configure its endpoint/model/credentials.

Save an operator-authored JSON job specification:

```json
{
  "objective": "Join two independent fixture inspections",
  "workers": [
    {"backend": "<FIRST_BACKEND>", "objective": "Inspect first fixture", "scope": ["<FIRST_SCOPE>"], "budget": {"tokens": 100}},
    {"backend": "<SECOND_BACKEND>", "objective": "Inspect second fixture", "scope": ["<SECOND_SCOPE>"], "budget": {"tokens": 100}}
  ]
}
```

Use an existing saved session slug. Creating a job saves it without starting workers:

```sh
python -m tessera --home "<TESSERA_HOME>" swarm-create "<SPEC_FILE>" --session "<SESSION_SLUG>"
python -m tessera --home "<TESSERA_HOME>" swarm-run "<JOB_ID>"
python -m tessera --home "<TESSERA_HOME>" swarm-show "<JOB_ID>"
```

The equivalent Python calls are SessionManager.create_swarm_job(slug, objective, workers) and run_swarm_job(job_id). These methods do not load a chat model. When opening a manager solely for explicit jobs, set server.resume_turns=false to leave unrelated saved chat turns pending. The swarm CLI applies that setting to its own configuration copy automatically; ordinary chat/server recovery keeps its configured behavior. A job run starts one thread per saved worker and joins them. How many requests run at once is each model's max_concurrent_requests. Further waves or retries require another run. Per-account policy controls budget reservations, rate limits, retries and circuits. It never takes an unrelated queued task. Adapter internal retries are disabled for jobs; scheduler attempt limits control retries. Missing usage conservatively charges the declared budget and may stop retries.

Children return tool-free text artifacts. This is an evidence-producing fan-out/merge pipeline; it does not grant workers filesystem or shell tools. Scopes declare independence for admission rather than providing an execution sandbox. Each artifact is saved atomically with a SHA-256 reference and a task/attempt identity. The merge rereads and verifies those bytes, checks all children succeeded, and submits actual content to deterministic/optional semantic gates. Incomplete/refused finals, overlapping scopes, changed provider bindings, missing/tampered artifacts, exhausted budgets or denied gates prevent successful completion. Goal-met scoring applies to merge; human callbacks cannot approve failed deterministic checks.

The shared TaskStore contains child attempts/result references. The board contains the session log range and child/merged artifact references; session work messages retain queued/running/final transitions. The job snapshot and immutable merged artifact survive restart. Accepted merge references/gates are checkpointed before board publication; recovery reuses that accepted decision and content-addressed artifact instead of rejudging it or duplicating board references. Completed jobs verify saved evidence and do not replay provider calls. After an uncertain process failure, leased claims stay fenced until expiry, with capped recovery attempts and conservative budget settlement. A process crash cannot guarantee exactly-once remote provider calls; no automatic daemon retries are started.

The existing sanitized task_status and board_status show the durable work. Additive job_status reports only ordinal states/counts and finite merge/gate states. Passive status and swarm-show never construct adapters, resolve credentials, probe endpoints, run tasks or repair journals. Unreadable job snapshots remain unreported. A job with more workers than the current mode allows stays listed and carries the reason.

Offline evidence is enforced through owner tests/test_swarm_jobs.py and tests/test_job_status.py plus the independent test_swarm_job_consumers.py. Tests prove concurrent distinct lanes, a generated CLI protocol process, persisted artifact contents, CLI/session launch, board/source links, restart recovery without child replay, incomplete-final rejection, budget/retry limits and optional Jev/human merge gates. Local SummaryTree/View/CLI zoom proof is separate from native memory-tree availability.

Observed offline mixed-lane result: openai_chat selected for the fake lane and cli_worker selected for the generated Claude protocol fixture; both task attempts succeeded once. The merged artifact rereads the exact strings "alpha fake evidence" and "beta CLI evidence". The board has three references (two children and the merge), status succeeded, and source log lines 0 through 2. Reopening the completed job verifies the same bytes without constructing another backend. These observations come from the executable SessionManager regression, with no live provider accounts.
