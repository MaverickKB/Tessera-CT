"""Regressions built from real recorded CLI outputs (paths/ids redacted)."""

import json
from pathlib import Path
import sys

import pytest

from cli_fixtures import synthetic_codex_auth
from test_cli_worker import fake_cli
from tessera.cli_worker import CliWorker, WorkerError
from tessera.scheduler import Scheduler

FIXTURES = Path(__file__).parent / "fixtures" / "cli_recorded"


def replay(tmp_path, vendor, name, args):
    data = (FIXTURES / name).read_text()
    command = fake_cli(tmp_path, vendor, "import sys\nsys.stdin.read()\nsys.stdout.write(" + repr(data) + ")\n")
    options = {"vendor": vendor, "command": command, "cwd": str(tmp_path), "timeout": 5, "args": args}
    if vendor == "codex":
        options["auth_path"] = synthetic_codex_auth(tmp_path)
    return CliWorker(options)


def test_recorded_codex_stream_with_cli_warning_items_decodes(tmp_path):
    worker = replay(tmp_path, "codex", "codex_exec_success_with_warning_items.jsonl", ["exec", "--json", "-"])
    result = worker.chat([{"role": "user", "content": "Synthetic task"}])
    assert result["content"] == "OK" and result["usage"]["input_tokens"] == 14317


def test_recorded_grok_streaming_json_decodes_and_records_cli_login(tmp_path):
    args = ["--prompt-file", "{prompt_file}", "--output-format", "streaming-json"]
    worker = replay(tmp_path, "grok", "grok_streaming_json_end_turn.jsonl", args)
    result = worker.chat([{"role": "user", "content": "Synthetic task"}])
    assert result["content"] == (FIXTURES / "grok_streaming_json_end_turn.expected.txt").read_text()
    assert json.loads(result["content"])["edits"]
    assert result["usage"]["total_tokens"] == 62714
    assert worker.auth_path == "cli_login"


def test_truncated_grok_stream_is_incomplete_not_success(tmp_path):
    lines = (FIXTURES / "grok_streaming_json_end_turn.jsonl").read_text().splitlines()[:-1]
    (tmp_path / "cut.jsonl").write_text("\n".join(lines) + "\n")
    command = fake_cli(tmp_path, "grok", "import sys\nsys.stdout.write(open(" + repr(str(tmp_path / "cut.jsonl")) + ").read())\n")
    worker = CliWorker({"vendor": "grok", "command": command, "cwd": str(tmp_path), "timeout": 5,
                        "args": ["--prompt-file", "{prompt_file}", "--output-format", "streaming-json"]})
    with pytest.raises(WorkerError):
        worker.chat([{"role": "user", "content": "Synthetic task"}])
    assert worker.auth_path == "" and worker.last_failure["category"] == "provider"


def test_nonzero_exit_keeps_exit_code_and_stderr_tail_as_evidence(tmp_path):
    command = fake_cli(tmp_path, "grok", "import sys\nsys.stderr.write('Error: max turns reached')\nsys.exit(3)\n")
    worker = CliWorker({"vendor": "grok", "command": command, "cwd": str(tmp_path), "timeout": 5,
                        "args": ["--prompt-file", "{prompt_file}", "--output-format", "json"]})
    with pytest.raises(WorkerError, match="exit 3"):
        worker.chat([{"role": "user", "content": "Synthetic task"}])
    assert worker.last_failure["exit_code"] == 3
    assert "max turns reached" in worker.last_failure["stderr_tail"]


@pytest.mark.parametrize("category", ["timeout", "provider", "configuration"])
def test_unreported_full_reservation_charge_keeps_real_failure_category(tmp_path, category):
    # Recorded failure: CLI timeouts/errors charged the conservative full reservation
    # and were relabeled 'quota', hiding the real cause.
    from test_swarm_policy import configured
    _, scheduler = configured(tmp_path, [0.0])
    task = scheduler.store.create("Synthetic", ["a.py"], {"tokens": 100, "seconds": 60}, "one")
    scheduler.enqueue(task["id"])
    claim = scheduler.claim_next("one", "owner")
    failed = scheduler.fail(claim, category, usage={"tokens": 100, "seconds": 1})
    assert failed["status"] == "failed" and failed["error_category"] == category


def test_actual_overrun_is_still_quota(tmp_path):
    from test_swarm_policy import configured
    _, scheduler = configured(tmp_path, [0.0])
    task = scheduler.store.create("Synthetic", ["a.py"], {"tokens": 100, "seconds": 60}, "one")
    scheduler.enqueue(task["id"])
    claim = scheduler.claim_next("one", "owner")
    assert scheduler.fail(claim, "provider", usage={"tokens": 101, "seconds": 1})["error_category"] == "quota"
