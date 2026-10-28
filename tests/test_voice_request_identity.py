import uuid

from test_server import running_server, call


def test_voice_request_identity_survives_core_reopen(tmp_path):
    identity = str(uuid.uuid4())
    body = {'text': 'One durable user input', 'source': 'voice', 'client_request_id': identity}
    with running_server(tmp_path) as server:
        call(server, 'POST', '/api/sessions', {'title': 'Retained'})
        assert call(server, 'POST', '/api/sessions/retained/messages', body) == (202, {'turn_id': identity})
        assert server.manager.wait('retained', 5)
    with running_server(tmp_path) as server:
        assert call(server, 'POST', '/api/sessions/retained/messages', body) == (202, {'turn_id': identity})
        runtime = server.manager.runtime('retained')
        assert len(runtime.turns) == 1
        users = []
        for message in runtime.session.log.iter():
            if message.kind == 'user':
                users.append(message.text)
        assert users == ['One durable user input']
