"""Native macOS computer use through a stdio JSON helper built from this package.

The helper is TesseraCUA.app (``scripts/build-cua-helper.sh``). It reads an app's
accessibility tree, screenshots that app's window, and posts actions to the target
pid. list_apps and get_app_state are read-only. Every other tool follows Safe/YOLO.
When computer_use.jev is enabled and its key is set, find_element and check ask
Jev about that tree. Both are read-only.
"""

import json
import os
from pathlib import Path
import queue
import signal
import subprocess
import threading
import time

from .cua_jev import (
    api_key, check_tree, find_in_tree, format_check, format_find, key_available, missing_key_message, redact,
)
from .tools import clip_output, object_schema

_FIELDS = ('app', 'element', 'x', 'y', 'to_element', 'to_x', 'to_y', 'dx', 'dy', 'value', 'key', 'text', 'max_px', 'button')


def unavailable(reason):
    text = str(reason)
    if text.startswith('unavailable:'):
        return text
    return f'unavailable: {text}'


class NativeHelper:
    """One long-lived helper process. Restarted on the next call if it exits."""

    def __init__(self, config):
        self.config = config
        self.timeout = config.computer_use.timeout
        self._process = None
        self._lines = None
        self._next_id = 0
        self._lock = threading.RLock()

    def executable(self):
        raw = self.config.computer_use.helper or 'cua/TesseraCUA.app'
        path = Path(raw).expanduser()
        if not path.is_absolute():
            path = Path(self.config.home) / path
        if path.suffix == '.app':
            path = path / 'Contents' / 'MacOS' / 'TesseraCUA'
        return path

    def missing_message(self, path):
        return (f'TesseraCUA helper is not built at {path}. It runs on macOS. '
                'Build it with: bash scripts/build-cua-helper.sh')

    def close(self):
        with self._lock:
            process, self._process = self._process, None
            if process and process.poll() is None:
                try:
                    from .procutil import kill_group
                    kill_group(process.pid)
                except (ProcessLookupError, PermissionError, OSError):
                    process.kill()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    pass

    def request(self, op, **fields):
        payload = {'op': op}
        for key in _FIELDS:
            if key in fields and fields[key] is not None:
                payload[key] = fields[key]
        with self._lock:
            self._ensure()
            self._next_id += 1
            payload['id'] = self._next_id
            try:
                self._process.stdin.write(json.dumps(payload).encode('utf-8') + b'\n')
                self._process.stdin.flush()
            except (OSError, ValueError, AttributeError):
                self.close()
                raise OSError('TesseraCUA helper write failed') from None
            deadline = time.monotonic() + self.timeout
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    self.close()
                    raise TimeoutError(f'TesseraCUA helper timed out after {self.timeout}s')
                try:
                    line = self._lines.get(timeout=remaining)
                except queue.Empty:
                    self.close()
                    raise TimeoutError(f'TesseraCUA helper timed out after {self.timeout}s') from None
                if line is None:
                    self.close()
                    raise OSError('TesseraCUA helper exited')
                try:
                    reply = json.loads(line)
                except ValueError:
                    continue
                if isinstance(reply, dict) and reply.get('id') == payload['id']:
                    return reply

    def _ensure(self):
        if self._process is not None and self._process.poll() is None:
            return
        path = self.executable()
        if not path.is_file() or not os.access(path, os.X_OK):
            raise FileNotFoundError(self.missing_message(path))
        try:
            from .procutil import popen
            self._process = popen([str(path)], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                  stderr=subprocess.DEVNULL)
        except OSError as error:
            raise OSError(f'TesseraCUA helper failed to start ({type(error).__name__})') from None
        self._lines = queue.Queue()
        process, lines = self._process, self._lines

        def pump():
            try:
                while True:
                    line = process.stdout.readline(8 * 1024 * 1024 + 1)
                    if not line:
                        break
                    lines.put(line)
            finally:
                lines.put(None)

        threading.Thread(target=pump, name='tessera-cua', daemon=True).start()


def _text_limit(config):
    return min(config.computer_use.text_chars, max(64, config.tools.clip_chars - 80))


def _fail(reply, limit):
    message = unavailable(reply.get('error') or 'TesseraCUA helper failed')
    text = reply.get('text')
    if text:
        message += '\n' + clip_output(str(text), limit)
    return message


def register_native_tools(registry, config):
    """Register computer_* against the native helper and chain process cleanup."""
    helper = NativeHelper(config)
    previous = getattr(registry, 'release', None)

    def release():
        helper.close()
        if previous is not None:
            previous()

    registry.release = release
    registry.host_cua = None
    prefix = config.computer_use.tool_prefix
    limit = _text_limit(config)
    number = {'type': 'number'}
    integer = {'type': 'integer'}
    string = {'type': 'string'}
    app = {'type': 'string'}

    def call(op, **fields):
        try:
            reply = helper.request(op, **fields)
        except FileNotFoundError as error:
            return unavailable(error)
        except (OSError, TimeoutError, ValueError) as error:
            return unavailable(error)
        if not reply.get('ok'):
            return _fail(reply, limit)
        return reply

    def list_apps():
        reply = call('list_apps')
        if isinstance(reply, str):
            return reply
        lines = []
        for item in reply.get('apps') or []:
            if not isinstance(item, dict):
                continue
            lines.append(f"{item.get('name', '')} pid={item.get('pid', '')} {item.get('bundle_id', '')}".strip())
        return '\n'.join(lines) or 'no apps'

    def get_app_state(app):
        reply = call('get_app_state', app=app, max_px=config.cua.max_image_px)
        if isinstance(reply, str):
            return reply
        result = {'text': clip_output(str(reply.get('text') or ''), limit)}
        image = reply.get('image') if isinstance(reply.get('image'), dict) else None
        if image and image.get('data'):
            mime = image.get('mimeType') or 'image/png'
            result['image'] = {'caption': f'{app} window', 'data_url': f'data:{mime};base64,{image["data"]}'}
        return result

    def screenshot(display=None):
        """A whole-desktop shot. get_app_state captures one app's window; this captures
        the screen, so a caller can see everything on the display."""
        fields = {'max_px': config.cua.max_image_px}
        if display is not None:
            fields['display'] = display
        reply = call('screenshot', **fields)
        if isinstance(reply, str):
            return reply
        result = {'text': clip_output(str(reply.get('text') or ''), limit)}
        image = reply.get('image') if isinstance(reply.get('image'), dict) else None
        if image and image.get('data'):
            mime = image.get('mimeType') or 'image/png'
            result['image'] = {'caption': 'desktop', 'data_url': f'data:{mime};base64,{image["data"]}'}
        return result

    def click(element=None, x=None, y=None, app=None):
        if element is None and (x is None or y is None):
            raise ValueError('click needs an element index or x and y')
        reply = call('click', element=element, x=x, y=y, app=app)
        return reply if isinstance(reply, str) else (reply.get('text') or 'click ok')

    def perform_secondary_action(element=None, x=None, y=None, app=None):
        if element is None and (x is None or y is None):
            raise ValueError('perform_secondary_action needs an element index or x and y')
        reply = call('perform_secondary_action', element=element, x=x, y=y, app=app)
        return reply if isinstance(reply, str) else (reply.get('text') or 'perform_secondary_action ok')

    def set_value(element, value, app=None):
        reply = call('set_value', element=element, value=value, app=app)
        return reply if isinstance(reply, str) else (reply.get('text') or 'set_value ok')

    def select_text(element, app=None):
        reply = call('select_text', element=element, app=app)
        return reply if isinstance(reply, str) else (reply.get('text') or 'select_text ok')

    def scroll(element=None, x=None, y=None, dx=0, dy=0, app=None):
        reply = call('scroll', element=element, x=x, y=y, dx=dx, dy=dy, app=app)
        return reply if isinstance(reply, str) else (reply.get('text') or 'scroll ok')

    def drag(element=None, x=None, y=None, to_element=None, to_x=None, to_y=None, button='left', app=None):
        start = element is not None or (x is not None and y is not None)
        end = to_element is not None or (to_x is not None and to_y is not None)
        if not start or not end:
            raise ValueError('drag needs a start and end, as element indexes or x,y')
        reply = call('drag', element=element, x=x, y=y, to_element=to_element, to_x=to_x, to_y=to_y,
                     button=button, app=app)
        return reply if isinstance(reply, str) else (reply.get('text') or 'drag ok')

    def press_key(key, app=None):
        reply = call('press_key', key=key, app=app)
        return reply if isinstance(reply, str) else (reply.get('text') or 'press_key ok')

    def type_text(text, app=None):
        reply = call('type_text', text=text, app=app)
        return reply if isinstance(reply, str) else (reply.get('text') or 'type_text ok')

    specs = (
        ('list_apps', 'List running apps on this Mac by name, pid, and bundle id.',
         {}, (), list_apps, False),
        ('get_app_state', 'Read one app in the background: its accessibility tree with element indexes, and a screenshot of that app\'s window.',
         {'app': app}, ['app'], get_app_state, False),
        ('screenshot', 'Take a screenshot of the whole desktop. You receive the image. For one app\'s window instead, use get_app_state.',
         {'display': integer}, (), screenshot, False),
        ('click', 'Press an element index in the target app with AXPress. Optional x,y are pixels in that app\'s screenshot and are posted to the target process.',
         {'element': integer, 'x': number, 'y': number, 'app': app}, (), click, True),
        ('perform_secondary_action', 'Secondary action on an element in the target app (show menu, or a right-click posted to that process).',
         {'element': integer, 'x': number, 'y': number, 'app': app}, (), perform_secondary_action, True),
        ('set_value', 'Set an element\'s value in the target app. Requires approval under ask.',
         {'element': integer, 'value': string, 'app': app}, ['element', 'value'], set_value, True),
        ('select_text', 'Select the text of an element in the target app. Requires approval under ask.',
         {'element': integer, 'app': app}, ['element'], select_text, True),
        ('scroll', 'Scroll the target app. Positive dy scrolls up. Uses the element\'s scroll bar, or one wheel event posted to that process. Requires approval under ask.',
         {'element': integer, 'x': number, 'y': number, 'dx': number, 'dy': number, 'app': app}, (), scroll, True),
        ('drag', 'Drag inside the target app, from an element or x,y to to_element or to_x,to_y. Posted to that process. Requires approval under ask.',
         {'element': integer, 'x': number, 'y': number, 'to_element': integer, 'to_x': number, 'to_y': number,
          'button': {'type': 'string', 'enum': ['left', 'right']}, 'app': app}, (), drag, True),
        ('press_key', 'Press a key combo such as cmd+c in the target app, posted to that process. Requires approval under ask.',
         {'key': string, 'app': app}, ['key'], press_key, True),
        ('type_text', 'Type text into the target app, posted to that process. Requires approval under ask.',
         {'text': string, 'app': app}, ['text'], type_text, True),
    )
    registered = []
    for op, description, properties, required, handler, risky in specs:
        name = (prefix + op)[:64]
        registry.register(name, description, object_schema(properties, required), handler, risky=risky)
        registered.append(name)
    jev = config.computer_use.jev
    if jev.enabled:
        if not key_available(jev):
            raise ValueError(missing_key_message(jev))
        registered.extend(_register_jev_tools(registry, config, call, prefix))
    return registered


def _register_jev_tools(registry, config, call, prefix):
    jev = config.computer_use.jev
    string = {'type': 'string'}

    def tree(app):
        reply = call('get_app_state', app=app, max_px=config.cua.max_image_px)
        if isinstance(reply, str):
            return reply
        text = reply.get('text')
        if not isinstance(text, str) or not text.strip():
            return unavailable('TesseraCUA returned no accessibility tree')
        return text

    def find_element(app, description, role=None):
        text = tree(app)
        if isinstance(text, str) and text.startswith('unavailable:'):
            return text
        try:
            key = api_key(jev, config.credentials)
            return redact(format_find(find_in_tree(text, description, jev, key, role)), key)
        except ValueError as error:
            return unavailable(redact(str(error), os.environ.get(jev.api_key_env, '')))

    def check(app, question):
        text = tree(app)
        if isinstance(text, str) and text.startswith('unavailable:'):
            return text
        try:
            key = api_key(jev, config.credentials)
            return redact(format_check(check_tree(text, question, jev, key)), key)
        except ValueError as error:
            return unavailable(redact(str(error), os.environ.get(jev.api_key_env, '')))

    specs = (
        ('find_element',
         'Find an element index before computer_click or computer_set_value when you know what you want but not the index. '
         'Read-only. Returns the index, its accessibility line, the probability, and the runner-up. '
         'Returns "not sure" and those candidates when confidence is below the configured minimum.',
         {'app': string, 'description': string, 'role': string}, ['app', 'description'], find_element),
        ('check',
         'After computer_click, computer_set_value, or computer_type_text, ask a yes/no question about the app\'s current accessibility tree to confirm the action landed. '
         'Read-only. Returns yes or no and the probability.',
         {'app': string, 'question': string}, ['app', 'question'], check),
    )
    names = []
    for op, description, properties, required, handler in specs:
        name = (prefix + op)[:64]
        registry.register(name, description, object_schema(properties, required), handler, risky=False)
        names.append(name)
    return names
