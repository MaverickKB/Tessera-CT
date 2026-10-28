"""Opt-in swarm acceptance using private configuration and synthetic challenges.

Run with ``TESSERA_LIVE_SWARM_SMOKE=1 python -m demo.swarm_live_smoke``.
Configuration is read only from the operator's private live home. The private
JSON spec has ``workers`` containing explicit ``backend`` and positive token
``budget`` declarations. No endpoint, model, account or credential is supplied
by this helper. CLI commands and working directories must be private paths;
the operator owns the configured CLI's authentication and execution boundary.
For each selected lane, the spec shape is
``{"backend": "<CONFIGURED_BACKEND>", "budget": {"tokens": "<POSITIVE_NUMBER>"}}``;
replace the token-budget placeholder with a number. Select at least two
distinct configured backends and accounts. How many of them run at once is each model's own request cap.
Configured API credential selectors must resolve from the environment; the
helper never falls back to a keychain. Existing policies and gates still apply.

Relative locations are configurable with TESSERA_LIVE_SWARM_SPEC (default
swarm-smoke.json), TESSERA_LIVE_SWARM_REPORT (swarm-smoke-report.json), and
TESSERA_LIVE_SWARM_RUNS (swarm-smoke-runs). TESSERA_LIVE_SWARM_INPUT_BYTES
bounds each input file. Outputs contain finite counts and decisions only.
Each invocation creates a fresh isolated job and makes one explicit run wave.
It neither resumes existing work nor invokes a tree builder or memory client.
"""

from copy import deepcopy
import json
import math
import os
from pathlib import Path
import re
import uuid

from tessera.backend import Backend
from tessera.config import load_config
from tessera.jobs import SwarmJobs
from tessera.swarm_bounds import job_task_limit
from tessera.log import ChatLog
from tessera.tasks import nonnegative_numbers
from demo.evidence import sanitize_evidence, write_json


class SmokeFailure(RuntimeError):
    """Only fixed public categories cross the private evidence boundary."""

    categories = frozenset({
        'at_least_two_backends_and_accounts_required', 'challenge_mismatch',
        'environment_credential_required', 'evidence_incomplete', 'execution_failed',
        'input_output_overlap', 'invalid_private_spec', 'job_incomplete',
        'positive_input_bound_required',
        'positive_worker_budget_required', 'private_cli_configuration_required',
        'private_input_missing', 'private_input_too_large', 'private_path_required',
        'provider_configuration_required', 'swarm_and_board_must_be_enabled',
    })

    def __init__(self, category='execution_failed'):
        if not isinstance(category, str) or category not in self.categories:
            category = 'execution_failed'
        super().__init__(category)


def final_answer(text):
    """Compare the visible final answer while retaining exact saved artifacts."""
    remaining = text.strip()
    while remaining.lower().startswith("<think>"):
        depth = 0
        end = None
        for tag in re.finditer(r"</?think>", remaining, re.IGNORECASE):
            if tag.group().lower() == "<think>":
                depth += 1
            else:
                depth -= 1
            if depth == 0:
                end = tag.end()
                break
        if end is None:
            raise SmokeFailure("challenge_mismatch")
        remaining = remaining[end:].strip()
    if re.search(r"</?think>", remaining, re.IGNORECASE):
        raise SmokeFailure("challenge_mismatch")
    return remaining


def private_path(root, value):
    if not isinstance(value, str) or not value or ".." in Path(value).parts:
        raise SmokeFailure("private_path_required")
    path = Path(value)
    if not path.is_absolute():
        path = root / path
    try:
        relative = path.relative_to(root)
    except ValueError:
        raise SmokeFailure("private_path_required") from None
    current = root
    if current.is_symlink():
        raise SmokeFailure("private_path_required")
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            raise SmokeFailure("private_path_required")
    return path


def bounded_input(path, limit):
    if not path.is_file():
        raise SmokeFailure("private_input_missing")
    if path.stat().st_size > limit:
        raise SmokeFailure("private_input_too_large")


def environment_credential(config, settings):
    """Do not search keychains or third-party login stores for API credentials."""
    if settings.api_key_env and os.environ.get(settings.api_key_env, "").strip():
        return
    reference = settings.credential_ref
    if not reference and not settings.api_key_env:
        return
    seen = set()
    while reference and reference not in seen:
        seen.add(reference)
        source = config.credentials.get(reference)
        if source is None:
            break
        if source.env and os.environ.get(source.env, "").strip():
            return
        if source.keychain_service:
            break
        reference = source.config_ref
    raise SmokeFailure("environment_credential_required")


def prepare(root):
    config_path = private_path(root, "config.toml")
    spec_path = private_path(root, os.environ.get("TESSERA_LIVE_SWARM_SPEC", "swarm-smoke.json"))
    report_path = private_path(root, os.environ.get("TESSERA_LIVE_SWARM_REPORT", "swarm-smoke-report.json"))
    runs_path = private_path(root, os.environ.get("TESSERA_LIVE_SWARM_RUNS", "swarm-smoke-runs"))
    inputs = (config_path, spec_path)
    if (report_path in inputs or runs_path in inputs or report_path == runs_path
            or report_path == root or report_path.is_dir()):
        raise SmokeFailure("input_output_overlap")
    limit = int(os.environ.get("TESSERA_LIVE_SWARM_INPUT_BYTES", "65536"))
    if limit <= 0:
        raise SmokeFailure("positive_input_bound_required")
    bounded_input(config_path, limit)
    config = load_config(root)
    if len(config.backends) < 2 or len(config.accounts) < 2:
        raise SmokeFailure("at_least_two_backends_and_accounts_required")
    if not config.swarm.enabled or not config.board.enabled:
        raise SmokeFailure("swarm_and_board_must_be_enabled")
    bounded_input(spec_path, limit)
    spec = json.loads(spec_path.read_text(encoding="utf-8"))
    if not isinstance(spec, dict) or set(spec) != {"workers"}:
        raise SmokeFailure("invalid_private_spec")
    workers = spec["workers"]
    limit = job_task_limit(config)
    if not isinstance(workers, list) or len(workers) < 2 or (limit is not None and len(workers) > limit):
        raise SmokeFailure("at_least_two_backends_and_accounts_required")
    backends = set()
    accounts = set()
    models = set()
    cli_count = 0
    for worker in workers:
        if not isinstance(worker, dict) or set(worker) != {"backend", "budget"}:
            raise SmokeFailure("invalid_private_spec")
        name = worker["backend"]
        if not isinstance(name, str) or name not in config.backends:
            raise SmokeFailure("at_least_two_backends_and_accounts_required")
        settings = config.backends[name]
        if settings.account not in config.accounts or name in backends or settings.account in accounts:
            raise SmokeFailure("at_least_two_backends_and_accounts_required")
        backends.add(name)
        accounts.add(settings.account)
        selected = deepcopy(config)
        selected.roles = {"main": name}
        resolved = selected.backend_for()
        if resolved.model:
            models.add(resolved.model)
        budget = worker["budget"]
        nonnegative_numbers(budget)
        tokens = budget.get("tokens")
        if type(tokens) not in (int, float) or not math.isfinite(tokens) or tokens <= 0:
            raise SmokeFailure("positive_worker_budget_required")
        if resolved.provider_kind == "cli_worker":
            cli = resolved.extra.get("cli", {})
            command = private_path(root, cli.get("command", ""))
            cwd = private_path(root, cli.get("cwd", ""))
            if not command.is_file() or not cwd.is_dir():
                raise SmokeFailure("private_cli_configuration_required")
            private_cli = deepcopy(cli)
            private_cli.update(command=str(command), cwd=str(cwd))
            config.backends[name].extra["cli"] = private_cli
            cli_count += 1
        else:
            if not resolved.base_url or not resolved.model:
                raise SmokeFailure("provider_configuration_required")
            environment_credential(config, resolved)
    if config.swarm.gates_enabled and config.jev.enabled:
        environment_credential(config, config.jev)
    # Preserve configured relative storage names while isolating existing debt.
    storage = []
    for settings, field in ((config.swarm, "tasks_file"), (config.swarm, "jobs_dir"),
                            (config.board, "journal_file")):
        path = private_path(root, str(getattr(settings, field)))
        storage.append((settings, field, path.relative_to(root)))
    return config, workers, report_path, runs_path, storage, len(models), cli_count


def run(root, backend_factory, report):
    config, workers, report_path, runs_path, storage, model_count, cli_count = prepare(root)
    run_id = uuid.uuid4().hex
    run_root = runs_path / run_id
    run_root.mkdir(parents=True, mode=0o700)
    config.home = run_root
    config.cwd = run_root
    for settings, field, relative in storage:
        setattr(settings, field, str(run_root / relative))
    report.update(execution="offline_fake" if backend_factory is not None else "live",
                  backend_count=len(workers), account_count=len(workers), configured_model_count=model_count,
                  cli_worker_count=cli_count)
    expected = []
    requests = []
    for index, worker in enumerate(workers):
        token = "smoke-token-" + uuid.uuid4().hex
        expected.append(token)
        requests.append({"backend": worker["backend"], "budget": worker["budget"],
                         "objective": "Reply with exactly: " + token,
                         "scope": [run_id + "/lane-" + str(index)]})

    def factory(settings):
        if backend_factory is not None:
            return backend_factory(settings)
        return Backend(settings.name, settings.base_url, settings.model, settings.api_key_env, settings.extra)

    try:
        jobs = SwarmJobs(config, backend_factory=factory)
        with ChatLog(run_root / "log") as log:
            job = jobs.create("Join independent smoke challenge artifacts", requests,
                              session_slug="swarm-smoke", log=log)
            result = jobs.run(job["id"], log=log)
            if result["status"] != "succeeded":
                raise SmokeFailure("job_incomplete")
            merged = jobs.artifact(job["id"])
            artifacts = merged["artifacts"]
            if len(artifacts) != len(expected):
                raise SmokeFailure("challenge_mismatch")
            for artifact, token in zip(artifacts, expected):
                if final_answer(artifact["content"]) != token:
                    raise SmokeFailure("challenge_mismatch")
            tasks = jobs.tasks.list()
            card = jobs.board.get(job["id"])
            references = set(card["artifacts"])
            if (len(tasks) != len(workers) or any(task["status"] != "succeeded" for task in tasks)
                    or card["status"] != "succeeded" or len(references) != len(workers) + 1
                    or result["merged"]["ref"] not in references
                    or merged["gate"]["allowed"] is not True or not merged["checks"]):
                raise SmokeFailure("evidence_incomplete")
            for verified in merged["checks"].values():
                if verified is not True:
                    raise SmokeFailure("evidence_incomplete")
            for task in tasks:
                if task["result_ref"] not in references:
                    raise SmokeFailure("evidence_incomplete")
            report.update(status="passed", reason="verified", completed_count=len(tasks),
                          artifact_count=len(references), challenge_verified=True, board_verified=True)
    except SmokeFailure as exc:
        report.update(status="failed", reason=str(exc))
    except Exception:
        report.update(status="failed", reason="execution_failed")
    report_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    write_json(report_path, report)


def main(*, backend_factory=None):
    report = {"status": "failed", "reason": "opt_in_required", "execution": "not_started"}
    if os.environ.get("TESSERA_LIVE_SWARM_SMOKE") != "1":
        print(json.dumps(report, sort_keys=True))
        return 2
    try:
        root = Path.home() / ".tessera-live"
        run(root, backend_factory, report)
    except SmokeFailure as exc:
        report.update(status="failed", reason=str(exc))
    except Exception:
        report.update(status="failed", reason="configuration_or_evidence_error")
    report = sanitize_evidence(report)
    print(json.dumps(report, sort_keys=True))
    return 0 if report["status"] == "passed" else 2


if __name__ == "__main__":
    raise SystemExit(main())
