"""Offline consumers of durable channel identities, guards and work delivery."""

import json
from unittest.mock import Mock, patch

import pytest

from tessera.channels import ChannelError, ChannelStore
from tessera.config import Config
from tessera.credentials import CredentialResolver
from tessera.sessions import SessionManager
from fake_backend import FakeBackend


@pytest.fixture(autouse=True)
def forbid_external_work(monkeypatch):
    tripwires = []
    for owner, name in ((CredentialResolver, "resolve"),):
        tripwire = Mock(side_effect=AssertionError("Channel contracts must remain offline"))
        monkeypatch.setattr(owner, name, tripwire)
        tripwires.append(tripwire)
    for target in ("urllib.request.urlopen", "urllib.request.OpenerDirector.open", "subprocess.Popen"):
        tripwire = Mock(side_effect=AssertionError("Channel contracts must remain offline"))
        monkeypatch.setattr(target, tripwire)
        tripwires.append(tripwire)
    yield
    for tripwire in tripwires:
        tripwire.assert_not_called()


@pytest.mark.parametrize("operation,args", [
    ("create", ("research",)), ("subscribe", ("research", "one")),
    ("publish", ("research", "one", "Inspect synthetic artifact")),
    ("direct", ("one", "two", "Inspect synthetic artifact")),
    ("handoff", ("one", "two", "Inspect synthetic artifact", "<chat>synthetic view</chat>")),
    ("acknowledge", ("message-placeholder", "two", "incoming")),
])
def test_disabled_channel_operations_never_create_storage_or_consult_clock(tmp_path, operation, args):
    def unused_clock():
        raise AssertionError("Disabled channels must not consult time")

    root = tmp_path / "absent"
    store = ChannelStore(root / "channels.jsonl", clock=unused_clock)
    assert store.list_channels() == store.pending() == []
    assert store.delivery_result("message-placeholder", "two") is None
    with pytest.raises(ChannelError, match="disabled"):
        getattr(store, operation)(*args)
    assert not root.exists()


def test_rate_override_pause_dedupe_and_exact_window_are_durable_per_channel(tmp_path):
    clock = [100.0]
    path = tmp_path / "channels.jsonl"

    def reopen():
        return ChannelStore(path, enabled=True, clock=lambda: clock[0], rate_limit=2,
                            rate_window=10, rate_overrides={"hot": 1, "paused": 0})

    store = reopen()
    for channel in ("hot", "cold", "paused"):
        store.create(channel)
        store.subscribe(channel, "one")
        store.subscribe(channel, "two")
    first = store.publish("hot", "one", "Inspect artifact", message_id="stable-placeholder")
    before = path.read_bytes()
    assert reopen().publish("hot", "one", "Inspect artifact", message_id="stable-placeholder") == first
    assert path.read_bytes() == before
    for stamp in (100.0, 109.999):
        clock[0] = stamp
        with pytest.raises(ChannelError, match="rate"):
            reopen().publish("hot", "two", "Another artifact")
        assert path.read_bytes() == before
    store.publish("cold", "one", "First cold artifact")
    store.publish("cold", "two", "Second cold artifact")
    before = path.read_bytes()
    for channel in ("cold", "paused"):
        with pytest.raises(ChannelError, match="rate"):
            reopen().publish(channel, "one", "Blocked artifact")
        assert path.read_bytes() == before
    clock[0] = 110.0
    assert reopen().publish("hot", "two", "Window reopened")["hop"] == 1


def test_recipient_snapshots_and_acknowledgments_survive_tampering_and_restart(tmp_path):
    path = tmp_path / "channels.jsonl"
    store = ChannelStore(path, enabled=True)
    store.create("research")
    store.subscribe("research", "one")
    store.subscribe("research", "two")
    sent = store.publish("research", "one", "Inspect synthetic source", message_id="message-placeholder")
    store.subscribe("research", "two", subscribed=False)
    store.subscribe("research", "three")
    sent["recipients"][:] = ["three"]
    sent["text"] = "mutated caller copy"
    pending = store.pending()
    pending[0]["message"]["recipients"].clear()
    reopened = ChannelStore(path, enabled=True)
    pending = reopened.pending()
    assert [(row["session"], row["direction"]) for row in pending] == [("one", "outgoing"), ("two", "incoming")]
    for row in pending:
        assert row["message"]["recipients"] == ["two"]
        assert row["message"]["text"] == "Inspect synthetic source"
    before = path.read_bytes()
    with pytest.raises(ChannelError, match="recipient"):
        reopened.acknowledge("message-placeholder", "three", "incoming")
    assert path.read_bytes() == before
    reopened.acknowledge("message-placeholder", "one", "outgoing")
    reopened.acknowledge("message-placeholder", "two", "incoming", turn_id="turn-placeholder")
    before = path.read_bytes()
    assert reopened.acknowledge("message-placeholder", "two", "incoming", turn_id="different-turn") == "turn-placeholder"
    assert path.read_bytes() == before
    restored = ChannelStore(path, enabled=True)
    assert restored.pending() == []
    assert restored.delivery_result("message-placeholder", "two") == "turn-placeholder"


def test_handoff_combined_utf8_payload_obeys_configured_byte_limit(tmp_path):
    expected = "Work handoff\n\nBrief:\nInspect artifact\n\nSender view:\n<chat>A🙂</chat>"
    store = ChannelStore(tmp_path / "channels.jsonl", enabled=True, max_text_bytes=len(expected.encode()))
    message = store.handoff("one", "two", "Inspect artifact", "<chat>A🙂</chat>", task_ref="task-placeholder")
    assert message["text"] == expected and message["task_ref"] == "task-placeholder"
    before = store.path.read_bytes()
    with pytest.raises(ChannelError, match="byte limit"):
        store.handoff("one", "two", "Inspect artifact", "<chat>A🙂Z</chat>")
    assert store.path.read_bytes() == before


def test_causal_hop_guard_survives_restart_even_with_cycle_guard_disabled(tmp_path):
    path = tmp_path / "channels.jsonl"
    store = ChannelStore(path, enabled=True, max_hops=2, block_cycles=False)
    first = store.direct("one", "two", "Inspect A")
    second = store.direct("two", "three", "Inspect B", parent_id=first["id"])
    assert second["hop"] == 2
    before = path.read_bytes()
    reopened = ChannelStore(path, enabled=True, max_hops=2, block_cycles=False)
    with pytest.raises(ChannelError, match="hop"):
        reopened.direct("three", "four", "Inspect C", parent_id=second["id"])
    assert path.read_bytes() == before


def test_causal_sender_cycle_and_parent_spoof_cannot_append_messages(tmp_path):
    store = ChannelStore(tmp_path / "channels.jsonl", enabled=True, max_hops=4)
    first = store.direct("one", "two", "Inspect A")
    second = store.direct("two", "one", "Found A", parent_id=first["id"])
    before = store.path.read_bytes()
    with pytest.raises(ChannelError, match="cycle"):
        store.direct("one", "two", "Repeat A", parent_id=second["id"])
    with pytest.raises(ChannelError, match="recipient"):
        store.direct("outsider", "three", "Spoofed A", parent_id=first["id"])
    assert store.path.read_bytes() == before


def message_config(tmp_path, *, auto_reply=False):
    config = Config(home=tmp_path, cwd=tmp_path)
    config.channels.enabled = True
    config.channels.auto_deliver = False
    config.channels.auto_reply = auto_reply
    config.jev.enabled = False
    return config


def work_rows(manager, slug):
    rows = []
    for path in sorted((manager.path(slug) / "log").glob("*.jsonl")):
        for line in path.read_text().splitlines():
            row = json.loads(line)
            if row["kind"] == "work":
                rows.append(row)
    return rows


def test_explicit_handoff_logs_redacted_snapshot_on_both_sides_without_models(tmp_path, monkeypatch):
    key = "synthetic-work-key"
    monkeypatch.setenv("EXAMPLE_WORK_KEY", key)
    config = message_config(tmp_path)
    config.credentials = {"reference-placeholder": {"env": "EXAMPLE_WORK_KEY"}}
    factory = Mock(side_effect=AssertionError("Explicit snapshot logging must not load a model"))
    with SessionManager(config, backend_factory=factory) as manager:
        manager.create("one")
        manager.create("two")
        result = manager.handoff("one", "two", "Inspect artifact using " + key,
                                 view="<chat>Selected synthetic view " + key + "</chat>",
                                 task_ref="task-" + key, message_id="handoff-placeholder")
        assert result["message_id"] == "handoff-placeholder" and result["recipients"] == ["two"]
        assert work_rows(manager, "one") == work_rows(manager, "two") == []
        assert manager.deliver_work(limit=1) == {"delivered": 1}
        assert len(work_rows(manager, "one")) == 1 and work_rows(manager, "two") == []
        assert manager.deliver_work(limit=1) == {"delivered": 1}
        assert manager.deliver_work() == {"delivered": 0}
        for name, direction in (("one", "outgoing"), ("two", "incoming")):
            rows = work_rows(manager, name)
            assert len(rows) == 1
            assert rows[0]["meta"]["work_id"] == "handoff-placeholder"
            assert rows[0]["meta"]["direction"] == direction
            assert rows[0]["meta"]["task_ref"] == "task-[REDACTED]"
            assert "Inspect artifact using [REDACTED]" in rows[0]["text"]
            assert "<chat>Selected synthetic view [REDACTED]</chat>" in rows[0]["text"]
        assert manager.channels.pending() == []
    with SessionManager(config, backend_factory=factory) as reopened:
        assert reopened.deliver_work() == {"delivered": 0}
        assert len(work_rows(reopened, "one")) == len(work_rows(reopened, "two")) == 1
    factory.assert_not_called()
    for path in tmp_path.rglob("*"):
        if path.is_file():
            assert key.encode() not in path.read_bytes()


def test_log_before_ack_interruption_recovers_once_after_manager_restart(tmp_path):
    config = message_config(tmp_path)
    factory = Mock(side_effect=AssertionError("Notifications must not load a model"))
    with SessionManager(config, backend_factory=factory) as manager:
        manager.create("one")
        manager.create("two")
        manager.direct_work("one", "two", "Synthetic artifact is ready", message_id="message-placeholder")
        acknowledge = manager.channels.acknowledge

        def interrupted(message_id, session, direction, **options):
            if direction == "incoming":
                raise OSError("synthetic interruption after durable work log")
            return acknowledge(message_id, session, direction, **options)

        with patch.object(manager.channels, "acknowledge", side_effect=interrupted):
            with pytest.raises(OSError, match="synthetic interruption"):
                manager.deliver_work()
        assert len(work_rows(manager, "one")) == len(work_rows(manager, "two")) == 1
        assert len(manager.channels.pending()) == 1
    with SessionManager(config, backend_factory=factory) as reopened:
        assert reopened.deliver_work() == {"delivered": 1}
        assert reopened.deliver_work() == {"delivered": 0}
        assert len(work_rows(reopened, "one")) == len(work_rows(reopened, "two")) == 1
    factory.assert_not_called()


def test_needs_reply_fallback_can_reply_once_and_log_acknowledgment_without_live_jev(tmp_path):
    config = message_config(tmp_path, auto_reply=True)
    backend = FakeBackend(["Synthetic artifact inspected"])
    factory = Mock(return_value=backend)
    with SessionManager(config, backend_factory=factory) as manager:
        manager.create("one")
        manager.create("two")
        manager.direct_work("one", "two", "Can you inspect the synthetic artifact?", message_id="question-placeholder")
        assert manager.deliver_work() == {"delivered": 2}
        assert manager.wait("two", 5)
        assert len(backend.calls) == factory.call_count == 1
        manager.direct_work("one", "two", "Thanks, received.", message_id="ack-placeholder")
        assert manager.deliver_work() == {"delivered": 2}
        assert len(backend.calls) == factory.call_count == 1
        assert [row["meta"]["work_id"] for row in work_rows(manager, "two")] == [
            "question-placeholder", "ack-placeholder"]
        assert manager.channels.pending() == []


def test_last_allowed_hop_logs_without_reply_or_judge_lookup(tmp_path):
    config = message_config(tmp_path, auto_reply=True)
    config.channels.max_hops = 1
    config.jev.enabled = True
    config.jev.url = "https://judge.example.com/evaluate"
    config.jev.model = "judge-placeholder"
    config.jev.credential_ref = "reference-placeholder"
    config.credentials = {"reference-placeholder": {"env": "EXAMPLE_WORK_KEY"}}
    factory = Mock(side_effect=AssertionError("Hop exhaustion must prevent a model reply"))
    with SessionManager(config, backend_factory=factory) as manager:
        manager.create("one")
        manager.create("two")
        result = manager.direct_work("one", "two", "Can you inspect the synthetic artifact?")
        assert result["hop"] == 1
        assert manager.deliver_work() == {"delivered": 2}
        assert len(work_rows(manager, "one")) == len(work_rows(manager, "two")) == 1
        assert manager.channels.pending() == []
    factory.assert_not_called()


def test_notification_only_recipient_reopens_with_work_visible_in_its_view(tmp_path):
    config = message_config(tmp_path)
    backend = FakeBackend([])
    factory = Mock(return_value=backend)
    with SessionManager(config, backend_factory=factory) as manager:
        manager.create("one")
        manager.create("two")
        manager.direct_work("one", "two", "Synthetic artifact is ready", message_id="notification-placeholder")
        assert manager.deliver_work() == {"delivered": 2}
    factory.assert_not_called()
    with SessionManager(config, backend_factory=factory) as reopened:
        session = reopened.get("two")
        assert session.view.zoom(0, 1) == "[one] Synthetic artifact is ready"
        assert len(work_rows(reopened, "two")) == 1
        assert reopened.deliver_work() == {"delivered": 0}
        assert backend.calls == []
