"""Task claims survive restart and stale workers cannot finish newer attempts."""

import json

import pytest

from tessera.tasks import TaskError, TaskStore


def test_disabled_board_does_not_create_runtime_files(tmp_path):
    path = tmp_path / "absent" / "tasks.jsonl"
    board = TaskStore(path)
    assert board.list() == []
    with pytest.raises(TaskError, match="disabled"):
        board.create("Read evidence", ["scope-placeholder"], {}, "account-placeholder")
    assert not path.parent.exists()


def test_journal_recovery_and_attempt_fencing_after_lease_expiry(tmp_path):
    clock = [100.0]
    path = tmp_path / "tasks.jsonl"
    board = TaskStore(path, enabled=True, clock=lambda: clock[0])
    created = board.create("Read the source artifact", ["artifact-placeholder"], {"tokens": 50}, "account-placeholder")
    first = board.claim(created["id"], "first", lease_seconds=5)
    assert first["attempt"] == 1 and first["status"] == "running"
    assert board.claim(created["id"], "second", lease_seconds=5) is None
    clock[0] = 106
    restored = TaskStore(path, enabled=True, clock=lambda: clock[0])
    assert restored.recover_expired() == 1
    second = restored.claim(created["id"], "second", lease_seconds=5)
    assert second["attempt"] == 2
    with pytest.raises(TaskError, match="stale"):
        board.finish(created["id"], "first", 1, "succeeded")
    done = restored.finish(created["id"], "second", 2, "succeeded", result_ref="artifact-result")
    assert done["objective"] == created["objective"] and done["scope"] == ["artifact-placeholder"]
    assert done["budget"] == {"tokens": 50} and done["result_ref"] == "artifact-result"
    assert TaskStore(path, enabled=True).get(created["id"])["status"] == "succeeded"


def test_incomplete_append_is_preserved_and_complete_corruption_fails(tmp_path):
    path = tmp_path / "tasks.jsonl"
    board = TaskStore(path, enabled=True)
    task = board.create("Read source", ["scope-placeholder"], {}, "account-placeholder")
    with path.open("ab") as stream:
        stream.write(b'{"partial":')
    assert board.get(task["id"])["status"] == "queued"
    assert list(tmp_path.glob("tasks.jsonl.incomplete-*"))
    with path.open("ab") as stream:
        stream.write(b'not-json\n')
    with pytest.raises(TaskError, match="journal"):
        board.list()


@pytest.mark.parametrize("budget", [{"tokens": -1}, {"seconds": float("nan")}, {"money": "unbounded"}])
def test_invalid_budget_fails_before_enqueue(tmp_path, budget):
    board = TaskStore(tmp_path / "tasks.jsonl", enabled=True)
    with pytest.raises(ValueError):
        board.create("Read source", ["scope-placeholder"], budget, "account-placeholder")
    assert board.list() == []
