"""Lifecycle hooks. Policy failures fail closed before a tool executes."""

import json
import os
import signal
import subprocess
import threading

EVENTS = ('session_start', 'turn_start', 'pre_tool', 'post_tool', 'turn_end', 'session_end', 'compaction')


class HookBus:
    def __init__(self, shell_hooks=None):
        self._handlers = {}
        self._lock = threading.RLock()
        for hook in shell_hooks or []:
            self.register(hook['event'], self._shell_handler(hook))

    def register(self, event, fn):
        if event not in EVENTS:
            raise ValueError(f'Unknown hook event: {event}')
        with self._lock:
            self._handlers.setdefault(event, []).append(fn)
        return fn

    def emit(self, event, payload):
        if event not in EVENTS:
            raise ValueError(f'Unknown hook event: {event}')
        with self._lock:
            handlers = list(self._handlers.get(event, []))
        results = []
        for handler in handlers:
            try:
                result = handler(payload)
            except Exception as exc:
                # Exception messages may contain credentials supplied by a plugin.
                result = {'error': f'Hook failed ({type(exc).__name__})'}
                if event == 'pre_tool':
                    result.update(block=True, reason='Pre-tool hook failed')
            if result is not None:
                results.append(result)
        return results

    @staticmethod
    def _shell_handler(hook):
        def run(payload):
            from .procutil import kill_group, popen
            process = popen(hook['command'], shell=True, stdin=subprocess.PIPE,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                            cwd=hook.get('cwd'))
            try:
                stdout, stderr = process.communicate(json.dumps(payload), timeout=hook.get('timeout', 10))
            except BaseException:
                try:
                    kill_group(process.pid)
                except ProcessLookupError:
                    pass
                process.communicate()
                raise
            output = stdout.strip()
            if process.returncode == 2:
                return {'block': True, 'reason': output or 'Shell hook blocked tool'}
            if process.returncode != 0:
                raise RuntimeError('Shell hook failed')
            if not output:
                return None
            try:
                parsed = json.loads(output)
                if isinstance(parsed, dict):
                    return parsed
            except json.JSONDecodeError:
                pass
            return {'context': output}
        return run
