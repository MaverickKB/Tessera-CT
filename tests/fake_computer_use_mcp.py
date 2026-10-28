"""Stdio stand-in for the Codex computer-use MCP server. Not the real client."""

import json
import struct
import sys
import zlib


TOOLS = [
    ('list_apps', 'List running apps.', {}),
    ('get_app_state', 'Start an app session and return the key window screenshot plus its accessibility tree.',
     {'app': {'type': 'string'}}),
    ('click', 'Click an element index or a pixel in the app.', {'element': {'type': 'integer'}, 'x': {'type': 'number'}, 'y': {'type': 'number'}}),
    ('perform_secondary_action', 'Secondary click on an element.', {'element': {'type': 'integer'}}),
    ('set_value', 'Set an element value.', {'element': {'type': 'integer'}, 'value': {'type': 'string'}}),
    ('select_text', 'Select text in an element.', {'element': {'type': 'integer'}}),
    ('scroll', 'Scroll an element or pixel.', {'element': {'type': 'integer'}, 'dx': {'type': 'number'}, 'dy': {'type': 'number'}}),
    ('drag', 'Drag within the app.', {'from_element': {'type': 'integer'}, 'to_element': {'type': 'integer'}}),
    ('press_key', 'Press a key in the app.', {'key': {'type': 'string'}}),
    ('type_text', 'Type text into the app.', {'text': {'type': 'string'}}),
]


def tiny_png():
    def chunk(tag, data):
        return struct.pack('>I', len(data)) + tag + data + struct.pack('>I', zlib.crc32(tag + data) & 0xffffffff)

    raw = b'\x00' + bytes((1, 2, 3))
    ihdr = struct.pack('>IIBBBBB', 1, 1, 8, 2, 0, 0, 0)
    return b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', ihdr) + chunk(b'IDAT', zlib.compress(raw)) + chunk(b'IEND', b'')


def tools():
    listed = []
    for name, description, properties in TOOLS:
        listed.append({'name': name, 'description': description, 'inputSchema': {
            'type': 'object', 'properties': properties, 'additionalProperties': False}})
    return listed


def reply(message_id, result):
    sys.stdout.write(json.dumps({'jsonrpc': '2.0', 'id': message_id, 'result': result}) + '\n')
    sys.stdout.flush()


def call(name, arguments):
    if name == 'get_app_state':
        tree = 'AXWindow TextEdit\n' + ('AXTextField hello\n' * 400)
        import base64
        return {'content': [
            {'type': 'text', 'text': tree},
            {'type': 'image', 'data': base64.b64encode(tiny_png()).decode('ascii'), 'mimeType': 'image/png'},
        ]}
    return {'content': [{'type': 'text', 'text': f'{name} ok'}]}


def main():
    log_path = sys.argv[1] if len(sys.argv) > 1 else ''
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except ValueError:
            continue
        if log_path and isinstance(message, dict):
            with open(log_path, 'a', encoding='utf-8') as stream:
                stream.write(json.dumps({'method': message.get('method'), 'params': message.get('params')}) + '\n')
        if not isinstance(message, dict) or 'id' not in message:
            continue
        method = message.get('method')
        if method == 'initialize':
            reply(message['id'], {'protocolVersion': '2025-06-18', 'capabilities': {'tools': {}},
                                  'serverInfo': {'name': 'Computer Use', 'version': 'test'}})
        elif method == 'tools/list':
            reply(message['id'], {'tools': tools()})
        elif method == 'tools/call':
            params = message.get('params') or {}
            reply(message['id'], call(params.get('name'), params.get('arguments') or {}))
        else:
            reply(message['id'], {})


if __name__ == '__main__':
    main()
