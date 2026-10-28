"""HTTP lease lifecycle contracts with in-memory mint replies and recorded events."""

import json

import pytest

from tessera.log import ChatLog
from test_server import call
from test_status_projection_consumers import disk_snapshot
from test_voice_consumers import (
    AUTH, EPHEMERAL_KEY, PROVIDER_KEY, fake_mint, forbid_live_voice,
    voice_config, voice_server,
)


def transcript():
    return {"type": "conversation.item.input_audio_transcription.completed", "item_id": "lease-item-placeholder",
            "transcript": "Synthetic speech " + PROVIDER_KEY + " " + EPHEMERAL_KEY}


def assert_private_data_absent(tmp_path, *responses):
    for response in responses:
        raw = json.dumps(response)
        assert PROVIDER_KEY not in raw and EPHEMERAL_KEY not in raw
    for path in tmp_path.rglob("*"):
        if path.is_file():
            raw = path.read_bytes()
            assert PROVIDER_KEY.encode() not in raw and EPHEMERAL_KEY.encode() not in raw


def test_http_lease_capacity_stop_and_restart_preserve_logs_without_reusing_identity(tmp_path, monkeypatch):
    config = voice_config(tmp_path)
    config.voice.max_sessions = 1
    config.voice.max_session_seconds = 90
    with voice_server(config, monkeypatch) as (server, _):
        slug = server.manager.create("Lease fixture")["slug"]
        other = server.manager.create("Other lease fixture")["slug"]
        prefix = f"/api/sessions/{slug}/voice"
        calls, resolver = fake_mint(server.manager, monkeypatch)
        code, started = call(server, "POST", prefix + "/start", {}, AUTH)
        assert code == 200 and started["expires_at"] == 160 and started["session_expires_at"] == 190
        assert started["protocols"] == ["client-placeholder." + EPHEMERAL_KEY]
        assert PROVIDER_KEY not in json.dumps(started)
        assert server.manager.voice.status()["active_count"] == 1
        before = disk_snapshot(tmp_path)
        code, rejected = call(server, "POST", f"/api/sessions/{other}/voice/start", {}, AUTH)
        assert code == 429 and rejected["error_category"] == "limit"
        assert disk_snapshot(tmp_path) == before and len(calls) == 1
        code, recorded = call(server, "POST", prefix + "/events",
                              {"voice_id": started["voice_id"], "events": [transcript()]}, AUTH)
        assert code == 200 and recorded["accepted"] == 1 and len(recorded["messages"]) == 1
        assert recorded["messages"][0]["text"] == "Synthetic speech [REDACTED] [REDACTED]"
        code, replay = call(server, "POST", prefix + "/events",
                            {"voice_id": started["voice_id"], "events": [transcript()]}, AUTH)
        assert code == 200 and replay["messages"] == [] and replay["send"] == []
        for _ in range(2):
            code, stopped = call(server, "POST", prefix + "/stop", {"voice_id": started["voice_id"]}, AUTH)
            assert code == 200 and stopped == {"stopped": True}
        assert server.manager.voice.status()["active_count"] == 0
        assert server.manager.voice._leases[started["voice_id"]]["secrets"] == []
        code, restarted = call(server, "POST", prefix + "/start", {}, AUTH)
        assert code == 200 and restarted["voice_id"] != started["voice_id"]
        # Instructions are the transcription-only prompt; the session view is not primed
        # into the provider (e528f29). The recorded line lives in the Tessera log instead.
        # The provider is the voice agent: it gets the real prompt and the live view.
        assert restarted["session_update"]["session"]["instructions"].startswith("You are ")
        assert len(calls) == 2 and resolver.resolve.call_count == 2
        # The recorded line queues a turn; let it finish before measuring disk so the
        # assertion compares a quiet session against a quiet session.
        server.manager.wait(slug, 5)
        settle = lambda: disk_snapshot(tmp_path)
        before = settle()
        code, stale = call(server, "POST", prefix + "/events",
                           {"voice_id": started["voice_id"], "events": [transcript()]}, AUTH)
        assert code == 409 and stale["error_category"] == "expired"
        # A rejected lease must not write. atomic_json leaves a .tmp file visible for a
        # moment during an unrelated write, so compare the settled state, not a snapshot
        # caught mid-rename.
        import time
        time.sleep(0.2)
        assert disk_snapshot(tmp_path) == before
        code, fresh = call(server, "POST", prefix + "/events",
                           {"voice_id": restarted["voice_id"], "events": [transcript()]}, AUTH)
        assert code == 200 and len(fresh["messages"]) == 1
        code, stopped = call(server, "POST", prefix + "/stop", {"voice_id": restarted["voice_id"]}, AUTH)
        assert code == 200 and server.manager.voice.status()["active_count"] == 0
        code, status = call(server, "GET", "/api/status", headers=AUTH)
        assert code == 200 and status["voice"]["active_count"] == 0
        assert_private_data_absent(tmp_path, recorded, replay, rejected, stale, fresh, status)
        # The restarted session's runtime holds the log handle, so read through it
        # rather than opening a second ChatLog on the same directory.
        server.manager.wait(slug, 5)
        rows = list(server.manager.runtime(slug).session.log.iter())
        # Each lease leaves one spoken line. The turn those lines start mirrors the
        # prompt and records its own failure (this fixture has no chat backend), so
        # scope the assertion to the voice rows rather than the whole log.
        spoken = [row for row in rows if row.meta.get("provenance") == "client_reported"]
        assert len(spoken) == 2 and all(row.kind == "user" for row in spoken)
        assert {row.meta["voice_id"] for row in spoken} == {started["voice_id"], restarted["voice_id"]}
        # The spoken line starts a turn, so this session stays loaded -- and only it.
        assert set(server.manager._runtimes) == {slug}


def test_foreign_or_unknown_stop_cannot_terminate_the_active_session_lease(tmp_path, monkeypatch):
    config = voice_config(tmp_path)
    with voice_server(config, monkeypatch) as (server, _):
        slug = server.manager.create("Lease fixture")["slug"]
        other = server.manager.create("Other lease fixture")["slug"]
        prefix = f"/api/sessions/{slug}/voice"
        calls, resolver = fake_mint(server.manager, monkeypatch)
        code, started = call(server, "POST", prefix + "/start", {}, AUTH)
        assert code == 200
        before = disk_snapshot(tmp_path)
        for target, voice_id in ((other, started["voice_id"]), (slug, "unknown-lease-placeholder"), (slug, {})):
            code, rejected = call(server, "POST", f"/api/sessions/{target}/voice/stop", {"voice_id": voice_id}, AUTH)
            assert code >= 400
            assert server.manager.voice.status()["active_count"] == 1
            assert disk_snapshot(tmp_path) == before
            assert_private_data_absent(tmp_path, rejected)
        code, accepted = call(server, "POST", prefix + "/events",
                              {"voice_id": started["voice_id"], "events": [transcript()]}, AUTH)
        assert code == 200 and len(accepted["messages"]) == 1
        code, stopped = call(server, "POST", prefix + "/stop", {"voice_id": started["voice_id"]}, AUTH)
        assert code == 200 and stopped == {"stopped": True}
        assert len(calls) == 1 and resolver.resolve.call_count == 1
        assert_private_data_absent(tmp_path, accepted, stopped)


@pytest.mark.parametrize("boundary", ["deadline", "archived", "configuration", "disabled", "shutdown"])
def test_existing_lease_cannot_log_after_its_owning_boundary_changes(tmp_path, monkeypatch, boundary):
    config = voice_config(tmp_path)
    with voice_server(config, monkeypatch) as (server, _):
        slug = server.manager.create("Lease fixture")["slug"]
        prefix = f"/api/sessions/{slug}/voice"
        calls, resolver = fake_mint(server.manager, monkeypatch)
        code, started = call(server, "POST", prefix + "/start", {}, AUTH)
        assert code == 200
        if boundary == "deadline":
            server.manager.voice.clock = lambda: started["session_expires_at"]
        elif boundary == "archived":
            server.manager.update(slug, archived=True)
        elif boundary == "configuration":
            config.voice.model = "changed-voice-model-placeholder"
        elif boundary == "disabled":
            config.voice.enabled = False
        else:
            server.manager.close()
        before = disk_snapshot(tmp_path)
        code, rejected = call(server, "POST", prefix + "/events",
                              {"voice_id": started["voice_id"], "events": [transcript()]}, AUTH)
        assert code >= 400 and rejected["error_category"] in {"expired", "session", "configuration", "disabled"}
        assert disk_snapshot(tmp_path) == before
        assert len(calls) == 1 and resolver.resolve.call_count == 1
        assert_private_data_absent(tmp_path, rejected, server.manager.voice.status())
        assert server.manager._runtimes == {}


@pytest.mark.parametrize("boundary", ["batch_limit", "invalid_tail"])
def test_invalid_event_batch_is_atomic_and_does_not_consume_the_lease(tmp_path, monkeypatch, boundary):
    config = voice_config(tmp_path)
    config.voice.max_events = 1
    with voice_server(config, monkeypatch) as (server, _):
        slug = server.manager.create("Lease fixture")["slug"]
        prefix = f"/api/sessions/{slug}/voice"
        calls, _ = fake_mint(server.manager, monkeypatch)
        if boundary == "invalid_tail":
            config.voice.max_events = 2
        code, started = call(server, "POST", prefix + "/start", {}, AUTH)
        assert code == 200
        tail = {**transcript(), "item_id": "second-item-placeholder"}
        if boundary == "invalid_tail":
            tail["transcript"] = []
        before = disk_snapshot(tmp_path)
        code, rejected = call(server, "POST", prefix + "/events",
                              {"voice_id": started["voice_id"], "events": [transcript(), tail]}, AUTH)
        assert code >= 400 and rejected["error_category"] == ("event" if boundary == "invalid_tail" else "limit")
        assert disk_snapshot(tmp_path) == before
        code, accepted = call(server, "POST", prefix + "/events",
                              {"voice_id": started["voice_id"], "events": [transcript()]}, AUTH)
        assert code == 200 and accepted["accepted"] == 1 and len(accepted["messages"]) == 1
        assert len(calls) == 1
        assert_private_data_absent(tmp_path, rejected, accepted)


@pytest.mark.parametrize("failure", ["timeout", "reflected_key"])
def test_failed_mint_does_not_reserve_a_lease_or_expose_secrets(tmp_path, monkeypatch, failure):
    config = voice_config(tmp_path)
    config.voice.max_sessions = 1
    with voice_server(config, monkeypatch) as (server, _):
        slug = server.manager.create("Lease fixture")["slug"]
        prefix = f"/api/sessions/{slug}/voice"
        if failure == "reflected_key":
            calls, resolver = fake_mint(server.manager, monkeypatch, {"value": PROVIDER_KEY, "expires_at": 160})
        else:
            calls, resolver = fake_mint(server.manager, monkeypatch)

            def timeout(request, timeout):
                calls.append(request)
                raise TimeoutError(PROVIDER_KEY + EPHEMERAL_KEY)

            server.manager.voice.client.opener = timeout
        code, rejected = call(server, "POST", prefix + "/start", {}, AUTH)
        assert code == (504 if failure == "timeout" else 502)
        assert len(calls) == 1 and resolver.resolve.call_count == 1
        assert server.manager.voice.status()["active_count"] == 0
        assert server.manager.voice._leases == {}
        assert_private_data_absent(tmp_path, rejected)
        recovered_calls, _ = fake_mint(server.manager, monkeypatch)
        code, started = call(server, "POST", prefix + "/start", {}, AUTH)
        assert code == 200 and len(recovered_calls) == 1
        code, stopped = call(server, "POST", prefix + "/stop", {"voice_id": started["voice_id"]}, AUTH)
        assert code == 200 and stopped == {"stopped": True}
        assert server.manager.voice.status()["active_count"] == 0


def test_only_a_keepalive_lease_that_went_silent_is_reaped(tmp_path, monkeypatch):
    """Only a keepalive lease that went silent is reaped.

    A bare "no events for N seconds" reap would kill every healthy quiet call.
    The core never holds a provider connection, so a silent client and a dead
    one are identical from here. The reap is therefore opt-in: only a lease
    that declared keepalive and then stopped beating is reclaimed. A lease that
    never opted in (the iOS app) is left alone and keeps the deadline as its
    backstop.
    """
    config = voice_config(tmp_path)
    config.voice.max_sessions = 1
    config.voice.max_session_seconds = 600
    config.voice.idle_reap_seconds = 150
    with voice_server(config, monkeypatch) as (server, _):
        manager = server.manager
        slug = manager.create("Keepalive fixture")["slug"]
        fake_mint(manager, monkeypatch)

        # A lease that never opted into keepalive must survive silence.
        plain = manager.voice.start(slug)
        manager.voice.clock = lambda: 100.0 + 300
        assert manager.voice.status()["active_count"] == 1
        manager.voice.stop(slug, plain["voice_id"])

        # An opted-in lease that stops beating is reclaimed.
        manager.voice.clock = lambda: 100.0
        beating = manager.voice.start(slug, keepalive=True)
        manager.voice.clock = lambda: 100.0 + config.voice.idle_reap_seconds
        assert manager.voice.status()["active_count"] == 0
        # And the slot is free again without waiting for the 600s deadline.
        again = manager.voice.start(slug, keepalive=True)
        assert again["voice_id"] != beating["voice_id"]

        # A beat renews it, so a healthy quiet call is not reaped.
        manager.voice.clock = lambda: 100.0 + config.voice.idle_reap_seconds + 10
        assert manager.voice.heartbeat(slug, again["voice_id"]) == {"keepalive": True, "reaped": False}
        manager.voice.clock = lambda: 100.0 + config.voice.idle_reap_seconds + 20
        assert manager.voice.status()["active_count"] == 1


def test_a_non_keepalive_lease_is_never_reaped_and_says_so(tmp_path, monkeypatch):
    config = voice_config(tmp_path)
    config.voice.max_sessions = 2
    config.voice.idle_reap_seconds = 150
    with voice_server(config, monkeypatch) as (server, _):
        manager = server.manager
        slug = manager.create("iOS-style fixture")["slug"]
        fake_mint(manager, monkeypatch)
        started = manager.voice.start(slug)  # no keepalive: the iOS shape
        manager.voice.clock = lambda: 100.0 + 300
        assert manager.voice.heartbeat(slug, started["voice_id"]) == {"keepalive": False, "reaped": False}
        assert manager.voice.status()["active_count"] == 1


def test_heartbeat_route_renews_without_draining_task_completions(tmp_path, monkeypatch):
    """A beat must not be events([]): that call also marks tasks delivered."""
    config = voice_config(tmp_path)
    config.voice.max_sessions = 2
    config.voice.idle_reap_seconds = 150
    with voice_server(config, monkeypatch) as (server, _):
        slug = server.manager.create("Beat fixture")["slug"]
        fake_mint(server.manager, monkeypatch)
        prefix = f"/api/sessions/{slug}/voice"
        code, started = call(server, "POST", prefix + "/start", {"keepalive": True}, AUTH)
        assert code == 200
        code, beat = call(server, "POST", prefix + "/heartbeat", {"voice_id": started["voice_id"]}, AUTH)
        assert code == 200 and beat == {"keepalive": True, "reaped": False}
        code, bad = call(server, "POST", prefix + "/start", {"keepalive": "yes"}, AUTH)
        assert code == 400


def test_a_zero_reap_window_disables_reaping_rather_than_reaping_instantly(tmp_path, monkeypatch):
    config = voice_config(tmp_path)
    config.voice.max_sessions = 2
    config.voice.idle_reap_seconds = 0
    with voice_server(config, monkeypatch) as (server, _):
        manager = server.manager
        slug = manager.create("Disabled reap fixture")["slug"]
        fake_mint(manager, monkeypatch)
        started = manager.voice.start(slug, keepalive=True)
        manager.voice.clock = lambda: 100.0 + 400
        assert manager.voice.status()["active_count"] == 1


def test_existing_voice_session_adopts_settings_and_records_one_write(tmp_path, monkeypatch):
    from unittest.mock import Mock
    from tessera import sessions

    config = voice_config(tmp_path)
    with voice_server(config, monkeypatch) as (server, _):
        manager = server.manager
        slug = config.voice.dedicated_slug
        manager.update(slug, title='Stale name', reasoning='high', thinking_budget=8192, archived=True)
        # Emulate a pre-card-162 meta.json, with no reconciliation record.
        manager._metadata[slug].pop('voice_applied', None)
        manager._metadata[slug]['voice'] = False
        config.voice.dedicated_title = 'Current voice'
        config.voice.dedicated_reasoning = 'low'
        config.voice.dedicated_thinking_budget = 256
        write = Mock(wraps=sessions.atomic_json)
        monkeypatch.setattr(sessions, 'atomic_json', write)
        actual = manager.ensure_voice()
        assert actual['title'] == 'Current voice'
        assert actual['reasoning'] == 'low'
        assert actual['thinking_budget'] == 256
        assert actual['voice'] is True and actual['archived'] is False
        saved = json.loads((manager.path(slug) / 'meta.json').read_text())
        assert saved['voice_applied'] == {'title': 'Current voice', 'reasoning': 'low', 'thinking_budget': 256}
        assert write.call_count == 1


def test_matching_voice_settings_do_not_rewrite_metadata(tmp_path, monkeypatch):
    from unittest.mock import Mock
    from tessera import sessions

    config = voice_config(tmp_path)
    with voice_server(config, monkeypatch) as (server, _):
        manager = server.manager
        before = disk_snapshot(tmp_path)
        write = Mock(side_effect=AssertionError('Unchanged voice settings must not write'))
        monkeypatch.setattr(sessions, 'atomic_json', write)
        manager.ensure_voice()
        manager.ensure_voice()
        write.assert_not_called()
        assert disk_snapshot(tmp_path) == before


def test_changed_settings_override_hand_tuning_and_update_loaded_runtime_on_start(tmp_path, monkeypatch):
    config = voice_config(tmp_path)
    with voice_server(config, monkeypatch) as (server, _):
        manager = server.manager
        slug = config.voice.dedicated_slug
        runtime = manager.runtime(slug)
        manager.update(slug, title='Hand tuned', reasoning='high', thinking_budget=8192)
        config.voice.dedicated_title = 'Settings wins'
        config.voice.dedicated_reasoning = 'medium'
        config.voice.dedicated_thinking_budget = 512
        fake_mint(manager, monkeypatch)
        started = manager.voice.start(slug)
        saved = json.loads((manager.path(slug) / 'meta.json').read_text())
        expected = {'title': 'Settings wins', 'reasoning': 'medium', 'thinking_budget': 512}
        assert saved['voice_applied'] == expected
        for field, value in expected.items():
            assert saved[field] == value
        assert runtime.session.reasoning == 'medium'
        assert runtime.session.thinking_budget == 512
        manager.voice.stop(slug, started['voice_id'])


def test_unchanged_settings_preserve_later_hand_tuning_across_restart(tmp_path, monkeypatch):
    config = voice_config(tmp_path)
    with voice_server(config, monkeypatch) as (server, _):
        slug = config.voice.dedicated_slug
        server.manager.update(slug, title='Hand tuned', reasoning='high', thinking_budget=8192)
        original = json.loads((server.manager.path(slug) / 'meta.json').read_text())['voice_applied']
    with voice_server(config, monkeypatch) as (server, _):
        actual = server.manager.ensure_voice()
        assert actual['title'] == 'Hand tuned'
        assert actual['reasoning'] == 'high'
        assert actual['thinking_budget'] == 8192
        saved = json.loads((server.manager.path(slug) / 'meta.json').read_text())
        assert saved['voice_applied'] == original


def test_settings_voice_reaches_next_start_unless_explicitly_overridden(tmp_path, monkeypatch):
    config = voice_config(tmp_path)
    config.voice.voices = ['first', 'second']
    config.voice.session_options['voice'] = 'first'
    with voice_server(config, monkeypatch) as (server, _):
        manager = server.manager
        slug = config.voice.dedicated_slug
        fake_mint(manager, monkeypatch)
        for configured, override, expected in [('first', None, 'first'), ('second', None, 'second'),
                                                ('second', 'first', 'first')]:
            config.voice.session_options['voice'] = configured
            started = manager.voice.start(slug, voice=override)
            assert started['session_update']['session']['voice'] == expected
            manager.voice.stop(slug, started['voice_id'])


def test_only_changed_settings_replace_hand_tuned_fields(tmp_path, monkeypatch):
    config = voice_config(tmp_path)
    with voice_server(config, monkeypatch) as (server, _):
        manager = server.manager
        slug = config.voice.dedicated_slug
        manager.update(slug, title='Personal name', reasoning='high', thinking_budget=8192)
        config.voice.dedicated_reasoning = 'medium'
        actual = manager.ensure_voice()
        assert actual['title'] == 'Personal name'
        assert actual['reasoning'] == 'medium'
        assert actual['thinking_budget'] == 8192


def test_empty_settings_title_does_not_erase_existing_voice_name(tmp_path, monkeypatch):
    config = voice_config(tmp_path)
    with voice_server(config, monkeypatch) as (server, _):
        manager = server.manager
        slug = config.voice.dedicated_slug
        title = manager.ensure_voice()['title']
        config.voice.dedicated_title = '   '
        assert manager.ensure_voice()['title'] == title
