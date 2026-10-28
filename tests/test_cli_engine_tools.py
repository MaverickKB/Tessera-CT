import pytest

from tessera.cli_worker import CliSettings, WorkerError, parse_tool_reply, tool_protocol

TOOLS = [{'type': 'function', 'function': {'name': 'read_file', 'description': 'Read', 'parameters': {'type': 'object'}}}]


def test_engine_tools_flag_is_validated():
    with pytest.raises(ValueError):
        CliSettings(vendor='codex', engine_tools='yes').validate()
    assert CliSettings(vendor='grok', engine_tools=True).validate().engine_tools is True


def test_protocol_lists_engine_tools():
    assert '"read_file"' in tool_protocol(TOOLS)


def test_parse_plain_final_and_tool_calls():
    assert parse_tool_reply({'content': 'Hello'}, TOOLS)['content'] == 'Hello'
    assert parse_tool_reply({'content': '{"final": "Done"}'}, TOOLS)['content'] == 'Done'
    reply = parse_tool_reply({'content': '```json\n{"tool_calls": [{"name": "read_file", "arguments": {"path": "a"}}]}\n```'}, TOOLS)
    assert reply['finish_reason'] == 'tool_calls'
    assert reply['tool_calls'][0]['name'] == 'read_file' and reply['tool_calls'][0]['arguments'] == {'path': 'a'}
    with pytest.raises(WorkerError):
        parse_tool_reply({'content': '{"tool_calls": [{"name": "rm_rf", "arguments": {}}]}'}, TOOLS)


def test_tool_calls_after_prose_preamble_are_parsed():
    # Recorded from a grok CLI wake turn: a sentence, then the tool_calls object on the same line.
    text = ("I'll look up what was written and confirm it aged the way it should."
            '{"tool_calls": [{"name": "read_file", "arguments": {"path": "a"}}, {"name": "read_file", "arguments": {}}]}')
    reply = parse_tool_reply({'content': text}, TOOLS)
    assert reply['finish_reason'] == 'tool_calls'
    assert [c['arguments'] for c in reply['tool_calls']] == [{'path': 'a'}, {}]
    assert reply['content'] == "I'll look up what was written and confirm it aged the way it should."


def test_prose_mentioning_json_without_trailing_call_stays_text():
    text = 'Use {"tool_calls": [...]} to call tools, then I answer in plain text.'
    assert parse_tool_reply({'content': text}, TOOLS)['content'] == text


def test_a_schema_valid_empty_reply_is_a_final_answer_not_a_failure():
    """The output schema advertises final as ["string","null"].

    A model that answers {"final": null, "tool_calls": []} is staying silent with
    nothing to call. That is schema-valid, so it must become an empty final reply
    rather than a provider error that fails the whole turn. This shape is a
    finished answer with nothing left to call.
    """
    from tessera.cli_worker import codex_tool_reply
    assert codex_tool_schema_allows_null_final()
    reply = codex_tool_reply({'content': '{"final": null, "tool_calls": []}', 'usage': {}})
    parsed = parse_tool_reply(reply, TOOLS)
    assert parsed['content'] == ''
    assert parsed['tool_calls'] == []


def codex_tool_schema_allows_null_final():
    from tessera.cli_worker import codex_tool_schema
    return codex_tool_schema(TOOLS)['properties']['final']['type'] == ['string', 'null']


def test_an_empty_tool_calls_list_with_a_string_final_is_a_final_answer():
    from tessera.cli_worker import codex_tool_reply
    reply = codex_tool_reply({'content': '{"final": "All done.", "tool_calls": []}', 'usage': {}})
    parsed = parse_tool_reply(reply, TOOLS)
    assert parsed['content'] == 'All done.'
    assert parsed['tool_calls'] == []


def test_a_malformed_tool_calls_value_is_still_rejected():
    """The fix must not swallow the case it was guarding: a non-list is a defect."""
    reply = {'content': '{"final": null, "tool_calls": "read_file"}', 'usage': {}}
    with pytest.raises(WorkerError):
        parse_tool_reply(reply, TOOLS)
