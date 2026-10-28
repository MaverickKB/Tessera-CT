"""Durable messaging is explicitly driven and has no transport or worker threads."""
import json
from unittest.mock import patch

import pytest


def store_at(tmp_path, clock=None, **options):
    from tessera.channels import ChannelStore
    return ChannelStore(tmp_path / 'channels.jsonl', enabled=True, clock=clock or (lambda: 100.0), **options)


def test_disabled_store_never_creates_storage(tmp_path):
    from tessera.channels import ChannelStore, ChannelError
    store = ChannelStore(tmp_path / 'absent' / 'channels.jsonl')
    assert store.pending() == []
    with pytest.raises(ChannelError, match='disabled'):
        store.create('research')
    assert not (tmp_path / 'absent').exists()


def test_shared_recipient_snapshot_ack_and_restart(tmp_path):
    store = store_at(tmp_path)
    store.create('research')
    store.subscribe('research', 'one')
    store.subscribe('research', 'two')
    message = store.publish('research', 'one', 'Inspect the source', message_id='stable-id')
    store.subscribe('research', 'three')
    assert message['recipients'] == ['two'] and message['hop'] == 1
    assert store.publish('research', 'one', 'Inspect the source', message_id='stable-id') == message
    pending = store.pending()
    assert {(item['session'], item['direction']) for item in pending} == {('one', 'outgoing'), ('two', 'incoming')}
    store.acknowledge('stable-id', 'one', 'outgoing')
    reopened = store_at(tmp_path)
    assert len(reopened.pending()) == 1 and reopened.pending()[0]['session'] == 'two'
    with pytest.raises(ValueError, match='id'):
        reopened.publish('research', 'one', 'Different body', message_id='stable-id')


def test_channel_rate_hops_and_causal_sender_cycle_are_durable(tmp_path):
    clock = [100.0]
    store = store_at(tmp_path, clock=lambda: clock[0], rate_limit=1, rate_window=10, max_hops=2)
    store.create('research')
    store.subscribe('research', 'one')
    store.subscribe('research', 'two')
    first = store.publish('research', 'one', 'Inspect')
    with pytest.raises(ValueError, match='rate'):
        store_at(tmp_path, clock=lambda: clock[0], rate_limit=1, rate_window=10).publish('research', 'two', 'Reply')
    clock[0] += 10
    second = store.direct('two', 'one', 'Found result', parent_id=first['id'])
    assert second['hop'] == 2
    with pytest.raises(ValueError, match='hop|cycle'):
        store.direct('one', 'two', 'Again', parent_id=second['id'])
    with pytest.raises(ValueError, match='recipient'):
        store.direct('outsider', 'one', 'Spoofed context', parent_id=first['id'])


def test_handoff_keeps_task_reference_brief_and_view_in_durable_envelope(tmp_path):
    store = store_at(tmp_path)
    handoff = store.handoff('one', 'two', 'Review the changed source', '<chat>view snapshot</chat>', task_ref='task-placeholder')
    restored = store_at(tmp_path).pending()[0]['message']
    assert restored == handoff
    assert handoff['kind'] == 'handoff' and handoff['brief'] == 'Review the changed source'
    assert handoff['view'] == '<chat>view snapshot</chat>' and handoff['task_ref'] == 'task-placeholder'
    assert all(item['message']['kind'] == 'handoff' for item in store.pending())


def test_torn_tail_preserved_on_mutation_and_corruption_is_not_discarded(tmp_path):
    store = store_at(tmp_path)
    store.create('research')
    with store.path.open('ab') as stream:
        stream.write(b'{torn')
    before = store.path.read_bytes()
    assert store.list_channels() == [{'name': 'research', 'members': []}]
    assert store.path.read_bytes() == before
    store.subscribe('research', 'one')
    assert list(tmp_path.glob('*.incomplete-*'))[0].read_bytes() == b'{torn'
    with store.path.open('ab') as stream:
        stream.write(b'invalid\n')
    with pytest.raises(ValueError, match='journal'):
        store.direct('one', 'two', 'Read')


@pytest.mark.parametrize('options', [{'parent_id': []}, {'message_id': ''}, {'message_id': 0}])
def test_invalid_work_identity_is_rejected_without_appending(tmp_path, options):
    store = store_at(tmp_path)
    first = store.direct('one', 'two', 'Inspect source')
    before = store.path.read_bytes()
    with pytest.raises(ValueError, match='id|parent'):
        store.direct('one', 'two', 'Inspect source', **options)
    assert store.path.read_bytes() == before
