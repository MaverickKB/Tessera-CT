# Jev readiness and advisory evidence review

Jev is optional. An unconfigured or switched-off judge makes no judge requests.
The existing approval policies, action gate and explicit session gates keep their
own settings; enabling advisory reviews does not enable or replace those gates.

The status page and `/api/status` show one of four Jev states:

| State | Meaning |
| --- | --- |
| Off | The Jev switch is off. |
| Not configured | The selected judge connection, model or credential configuration is incomplete. |
| Ready | Local configuration is complete. This is not a remote health check or proof that a judge answered. |
| Unavailable | An already-running judge observed an unavailable response. Polling status does not retry it. |

Status is passive: it does not create a judge, start a model, resolve a keychain
secret, refresh an OAuth grant, or send a network probe. Saved keychain selectors
can establish configuration, but their availability is learned only during real
use. A successful subsequent judge response clears its observed outage. An
unavailable judge is a judge observation, not evidence that the user's task
failed. The reason is safe status text, not the provider's private error body.

## Enable selected checkpoints

Configure a Jev service and credential, or a dedicated `roles.jev` backend, using
the existing Session judge settings. Credentials remain references, never values
in TOML. Then enable only the advisory checkpoints you want:

```toml
[jev]
enabled = true
completion_review_enabled = true
plan_review_enabled = false
handoff_review_enabled = false
summary_review_enabled = false
lesson_review_enabled = false
review_max_calls_per_turn = 2
review_max_input_chars = 24000
```

Edit the existing `[jev]` table rather than adding a duplicate. All five review
switches default to false. They are also available in Settings under Session
judge (Jev). The two bounds limit optional review attempts and the serialized
input sent to one review. The call limit counts review batches; the existing
single transport retry can make up to two HTTP attempts for a batch. Cached
reviews still count against this conservative limit. Existing judge timeouts
and batch deadlines still apply. These are request and input limits, not a
monetary spending estimate.
Exhausting an advisory budget never disables an approval or safety gate.

Reviews run at meaningful checkpoints rather than for every small tool action.
Completion reviews compare the request and claimed result with supplied tool
evidence and a bounded checkout diff when available. A plan is not proof of
execution; a handoff is not independent verification; summary and lesson reviews
do not establish completion of the user's task.

| Switch | Checkpoint and saved report |
| --- | --- |
| `completion_review_enabled` | Final reply; `advisory_review` in agent-message metadata and an advisory note. |
| `plan_review_enabled` | A permitted `board_set_steps` tool call supplies actual plan steps; advisory note. |
| `handoff_review_enabled` | Worker dispatch and completed worker reports; advisory note or report metadata. |
| `summary_review_enabled` | A compactor builds a summary from its source records; `advisory_review` in summary-node metadata. |
| `lesson_review_enabled` | An actual matching lesson is considered at turn end; advisory note after the deterministic learning update. |

The checkout diff is the tracked current diff against `HEAD`. It can include
pre-existing changes and omits untracked files and changes already committed;
its report names these limits rather than attributing every changed line to the
current turn. Review notes use the normal saved log and event replay; they do
not add a second enforcement channel.

## Read a report

Every report is explicitly advisory. Its independent assessments cover requested
outcomes, preservation of the requested functionality, and support for completion
claims. A concern names the requirement, cites an ID from the evidence actually
sent to the judge, and gives a concrete direction for repair. The judge cannot
invent an evidence record or turn an unsupported assessment into verified fact.

Tool failures remain deterministic facts, separate from model judgments. A model
rating cannot erase a failed command, denied tool, or explicit error, and an
advisory review cannot change turn success, authorize a tool or absorb a lesson.

Reports mark omitted or truncated evidence. Positive assessments become uncertain
when the evidence is incomplete; no diff is not proof of preserved behavior.
Worker reports remain self-reports, separate from observed worker tool results.
Missing or invalid judge answers are reported as unavailable or partial, with no
heuristic review substituted. A skipped review identifies its disabled checkpoint,
missing configuration, or exhausted budget. None of these states means that the
work passed review.

The review intentionally starts without enforcement or automatic rewrites.
Compare its findings with the actual request, diff and checks before acting.
