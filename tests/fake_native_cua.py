#!/usr/bin/env python3
"""Stdio stand-in for the native macOS computer-use helper. Not a Mac."""

import base64
import json
import os
import struct
import sys
import zlib


def tiny_png():
    def chunk(tag, data):
        return struct.pack('>I', len(data)) + tag + data + struct.pack('>I', zlib.crc32(tag + data) & 0xffffffff)

    raw = b'\x00' + bytes((1, 2, 3))
    ihdr = struct.pack('>IIBBBBB', 1, 1, 8, 2, 0, 0, 0)
    return b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', ihdr) + chunk(b'IDAT', zlib.compress(raw)) + chunk(b'IEND', b'')


PNG = base64.b64encode(tiny_png()).decode('ascii')


def reply(payload):
    sys.stdout.write(json.dumps(payload) + '\n')
    sys.stdout.flush()


def handle(message):
    op = message.get('op')
    body = {'id': message.get('id'), 'ok': True}
    app = message.get('app')
    if app == 'Denied':
        body['ok'] = False
        body['error'] = ('TesseraCUA: Accessibility is not granted to TesseraCUA. '
                         'System Settings > Privacy & Security > Accessibility, enable TesseraCUA.')
        return body
    if app == 'NoCapture':
        body['ok'] = False
        body['error'] = ('TesseraCUA: Screen Recording is not granted to TesseraCUA. '
                         'System Settings > Privacy & Security > Screen & System Audio Recording, enable TesseraCUA.')
        body['text'] = '0 AXWindow partial'
        return body
    if op == 'list_apps':
        body['apps'] = [
            {'name': 'TextEdit', 'pid': 42, 'bundle_id': 'com.apple.TextEdit'},
            {'name': 'Notes', 'pid': 43, 'bundle_id': 'com.apple.Notes'},
        ]
        return body
    if op == 'get_app_state':
        if app == 'LongTree':
            text = 'AXWindow TextEdit\n' + ('AXTextField hello\n' * 400)
        elif app == 'Picker':
            fixture = os.path.join(os.path.dirname(__file__), 'fixtures', 'textedit_tree.txt')
            with open(fixture, encoding='utf-8') as stream:
                text = stream.read()
        else:
            text = '0 AXWindow TextEdit 0,0,80,40 AXPress'
        body['text'] = text
        body['pid'] = 42
        body['image'] = {'mimeType': 'image/png', 'data': PNG}
        return body
    body['text'] = f'{op} ok'
    return body


def main():
    log_path = sys.argv[1] if len(sys.argv) > 1 else ''
    if log_path:
        with open(log_path + '.pid', 'w', encoding='utf-8') as stream:
            stream.write(str(os.getpid()))
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except ValueError:
            continue
        if not isinstance(message, dict):
            continue
        if log_path:
            with open(log_path, 'a', encoding='utf-8') as stream:
                stream.write(json.dumps(message) + '\n')
        if 'id' not in message:
            continue
        reply(handle(message))


if __name__ == '__main__':
    main()
