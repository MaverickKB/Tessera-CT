"""Synthesize pointer and keyboard input through Linux uinput (stdlib only).

Runs on the remote as root: `python3 - '<json>'` with this file on stdin. The JSON is
{"width": W, "height": H, "actions": [...]} with coordinates in screenshot pixels.
A virtual absolute pointer (like a VM tablet) and a keyboard exist only for one call.
"""

import fcntl
import json
import os
import struct
import sys
import time

EV_SYN, EV_KEY, EV_REL, EV_ABS = 0, 1, 2, 3
SYN_REPORT, REL_WHEEL, REL_HWHEEL, ABS_X, ABS_Y = 0, 8, 6, 0, 1
BTN = {'left': 0x110, 'right': 0x111, 'middle': 0x112}
UI_SET_EVBIT, UI_SET_KEYBIT, UI_SET_RELBIT, UI_SET_ABSBIT = 0x40045564, 0x40045565, 0x40045566, 0x40045567
UI_DEV_CREATE, UI_DEV_DESTROY = 0x5501, 0x5502
ABS_MAX = 32767

KEYS = {'esc': 1, 'escape': 1, 'backspace': 14, 'tab': 15, 'enter': 28, 'return': 28, 'ctrl': 29, 'control': 29,
        'shift': 42, 'alt': 56, 'space': 57, 'capslock': 58, 'super': 125, 'meta': 125, 'win': 125, 'cmd': 125,
        'home': 102, 'up': 103, 'pageup': 104, 'left': 105, 'right': 106, 'end': 107, 'down': 108,
        'pagedown': 109, 'insert': 110, 'delete': 111, 'menu': 139}
for index, name in enumerate(['f1', 'f2', 'f3', 'f4', 'f5', 'f6', 'f7', 'f8', 'f9', 'f10']):
    KEYS[name] = 59 + index
KEYS['f11'], KEYS['f12'] = 87, 88
ROWS = {'1234567890-=': 2, 'qwertyuiop[]': 16, "asdfghjkl;'": 30, 'zxcvbnm,./': 44}
SHIFTED = {'!': '1', '@': '2', '#': '3', '$': '4', '%': '5', '^': '6', '&': '7', '*': '8', '(': '9', ')': '0',
           '_': '-', '+': '=', '{': '[', '}': ']', ':': ';', '"': "'", '<': ',', '>': '.', '?': '/', '|': '\\', '~': '`'}
for row, start in ROWS.items():
    for offset, char in enumerate(row):
        KEYS[char] = start + offset
KEYS['`'], KEYS['\\'], KEYS['\n'], KEYS['\t'], KEYS[' '] = 41, 43, 28, 15, 57


def device(name, keys, rel=(), absolute=False):
    handle = os.open('/dev/uinput', os.O_WRONLY | os.O_NONBLOCK)
    fcntl.ioctl(handle, UI_SET_EVBIT, EV_KEY)
    for key in keys:
        fcntl.ioctl(handle, UI_SET_KEYBIT, key)
    if rel:
        fcntl.ioctl(handle, UI_SET_EVBIT, EV_REL)
        for axis in rel:
            fcntl.ioctl(handle, UI_SET_RELBIT, axis)
    absmax = [0] * 64
    if absolute:
        fcntl.ioctl(handle, UI_SET_EVBIT, EV_ABS)
        for axis in (ABS_X, ABS_Y):
            fcntl.ioctl(handle, UI_SET_ABSBIT, axis)
            absmax[axis] = ABS_MAX
    setup = struct.pack('80sHHHHi', name.encode()[:79], 3, 0x1234, 0x5678, 1, 0)
    setup += struct.pack('64i', *absmax) + struct.pack('64i', *([0] * 64)) * 3
    os.write(handle, setup)
    fcntl.ioctl(handle, UI_DEV_CREATE)
    return handle


def emit(handle, kind, code, value):
    now = time.time()
    os.write(handle, struct.pack('llHHi', int(now), int(now % 1 * 1e6), kind, code, value))


def sync(handle):
    emit(handle, EV_SYN, SYN_REPORT, 0)


def main():
    request = json.loads(sys.argv[1])
    width, height = request['width'], request['height']
    pointer = device('tessera-desk-pointer', BTN.values(), rel=(REL_WHEEL, REL_HWHEEL), absolute=True)
    keyboard = device('tessera-desk-keyboard', range(1, 249))
    time.sleep(float(request.get('settle', 0.8)))
    done = []

    def move(x, y):
        if not (0 <= x < width and 0 <= y < height):
            raise ValueError(f'Point {x},{y} is outside the {width}x{height} screen')
        emit(pointer, EV_ABS, ABS_X, round(x * ABS_MAX / max(1, width - 1)))
        emit(pointer, EV_ABS, ABS_Y, round(y * ABS_MAX / max(1, height - 1)))
        sync(pointer)
        time.sleep(0.03)

    def button(name, value):
        emit(pointer, EV_KEY, BTN[name], value)
        sync(pointer)
        time.sleep(0.03)

    def tap(codes):
        for code in codes:
            emit(keyboard, EV_KEY, code, 1)
            sync(keyboard)
        time.sleep(0.02)
        for code in reversed(codes):
            emit(keyboard, EV_KEY, code, 0)
            sync(keyboard)
        time.sleep(0.02)

    try:
        for action in request['actions']:
            kind = action['type']
            if kind == 'move':
                move(action['x'], action['y'])
            elif kind in ('click', 'double_click'):
                if 'x' in action:
                    move(action['x'], action['y'])
                for _ in range(2 if kind == 'double_click' else 1):
                    button(action.get('button', 'left'), 1)
                    button(action.get('button', 'left'), 0)
            elif kind == 'drag':
                move(action['x'], action['y'])
                button('left', 1)
                move(action['to_x'], action['to_y'])
                button('left', 0)
            elif kind == 'scroll':
                if 'x' in action:
                    move(action['x'], action['y'])
                for axis, amount in ((REL_WHEEL, -int(action.get('dy', 0))), (REL_HWHEEL, int(action.get('dx', 0)))):
                    if amount:
                        emit(pointer, EV_REL, axis, amount)
                        sync(pointer)
            elif kind == 'key':
                codes = []
                for part in action['keys'].lower().split('+'):
                    if part not in KEYS:
                        raise ValueError(f'Unknown key: {part}')
                    codes.append(KEYS[part])
                tap(codes)
            elif kind == 'type':
                for char in action['text']:
                    base = SHIFTED.get(char, char.lower())
                    if base not in KEYS:
                        raise ValueError(f'Cannot type {char!r} with a US layout')
                    tap(([KEYS['shift']] if char in SHIFTED or char.isupper() else []) + [KEYS[base]])
            elif kind == 'wait':
                time.sleep(min(10.0, float(action.get('seconds', 0.5))))
            else:
                raise ValueError(f'Unknown action: {kind}')
            done.append(kind)
        time.sleep(0.1)
    finally:
        for handle in (pointer, keyboard):
            fcntl.ioctl(handle, UI_DEV_DESTROY)
            os.close(handle)
    print(json.dumps({'done': done}))


main()
