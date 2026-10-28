from tessera.aethermind import _match_layers


def test_query_words_match_body_newest_first_with_limit():
    result = {'success': True, 'layers': [
        {'id': '1', 'timestamp': '2026-01-01T00:00:00Z', 'body': 'restore marker AM-1', 'ctx': 'tessera/turn/captures/a'},
        {'id': '2', 'timestamp': '2026-01-02T00:00:00Z', 'body': 'unrelated', 'ctx': 'tessera/turn/captures/b'},
        {'id': '3', 'timestamp': '2026-01-03T00:00:00Z', 'body': 'The Restore MARKER is AM-3', 'ctx': 'x'},
    ]}
    matched = _match_layers(result, 'restore marker', 1)
    assert [layer['id'] for layer in matched['layers']] == ['3'] and matched['count'] == 1
    partial = _match_layers(result, 'marker zebra', None)
    assert [layer['id'] for layer in partial['layers']] == ['3', '1']
    assert _match_layers(result, 'zebra', 5)['count'] == 0
