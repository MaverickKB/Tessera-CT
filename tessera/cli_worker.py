"""Explicit official-CLI subprocess workers; login and tools remain CLI-owned."""

from dataclasses import dataclass, field
import json
import sys
import math
import os
import re
from datetime import datetime, timezone
from pathlib import Path
import shutil
import signal
import subprocess
import selectors
import time
import tempfile


class WorkerError(RuntimeError):
    def __init__(self, message, category="provider"):
        super().__init__(message)
        self.category = category


@dataclass
class CliSettings:
    vendor: str = ""
    command: str = ""
    args: list = field(default_factory=list)
    cwd: str = ""
    timeout: float = 300.0
    max_output_bytes: int = 1048576
    strip_env: list = field(default_factory=list)
    fallback_credential_ref: str = ''
    fallback_api_key_env: str = ''
    fallback_env: str = ''
    # Optional local Codex auth file for the grant precheck. Empty uses ~/.codex/auth.json.
    auth_path: str = ''
    # The Grok API-key fallback bills per use; it runs only when this is explicitly true.
    allow_api_key_fallback: bool = False
    engine_tools: bool = False
    model: str = ''
    sandbox: str = ''
    # Extra writable directories for a codex workspace-write sandbox (codex --add-dir).
    add_dirs: list = field(default_factory=list)

    def validate(self):
        if self.vendor not in ("", "codex", "claude", "grok"):
            raise ValueError("Unsupported official CLI vendor")
        for value in (self.command, self.cwd):
            if not isinstance(value, str):
                raise ValueError("CLI command and cwd must be configured text")
        if self.command and Path(self.command).name not in (self.vendor, self.vendor + ".exe", self.vendor + ".cmd"):
            raise ValueError("CLI command must name the selected official executable")
        for value in (self.fallback_credential_ref, self.fallback_api_key_env, self.fallback_env):
            if not isinstance(value, str):
                raise ValueError('CLI fallback selectors must be text')
        if self.fallback_credential_ref or self.fallback_api_key_env or self.fallback_env:
            if self.vendor != 'grok' or not self.fallback_env or not (self.fallback_credential_ref or self.fallback_api_key_env):
                raise ValueError('Only Grok supports an explicitly configured API-key fallback')
        for values in (self.args, self.strip_env):
            if not isinstance(values, list) or any(not isinstance(value, str) for value in values):
                raise ValueError("CLI arguments and environment selectors must be lists of text")
        if type(self.allow_api_key_fallback) is not bool:
            raise ValueError('CLI allow_api_key_fallback must be true or false')
        if self.allow_api_key_fallback and not self.fallback_env:
            raise ValueError('CLI allow_api_key_fallback needs fallback_env and a fallback credential')
        if type(self.engine_tools) is not bool:
            raise ValueError('CLI engine_tools must be true or false')
        if self.sandbox not in ('', 'read-only', 'workspace-write'):
            raise ValueError('CLI sandbox must be read-only or workspace-write')
        if not isinstance(self.add_dirs, list):
            raise ValueError('CLI add_dirs must be a list of absolute paths')
        for directory in self.add_dirs:
            if not isinstance(directory, str) or not Path(directory).is_absolute():
                raise ValueError('CLI add_dirs must be a list of absolute paths')
        if self.add_dirs and self.sandbox != 'workspace-write':
            raise ValueError('CLI add_dirs needs the workspace-write sandbox')
        if not isinstance(self.model, str):
            raise ValueError('CLI model must be text')
        if not isinstance(self.auth_path, str):
            raise ValueError('CLI auth_path must be text')
        if type(self.timeout) not in (int, float) or not math.isfinite(self.timeout) or self.timeout <= 0:
            raise ValueError("CLI timeout must be positive and finite")
        if type(self.max_output_bytes) is not int or self.max_output_bytes <= 0:
            raise ValueError("CLI output limit must be a positive integer")
        return self


def cli_settings(options):
    if not isinstance(options, dict):
        raise ValueError("CLI options must be a table")
    try:
        return CliSettings(**options).validate()
    except TypeError:
        raise ValueError("Unknown CLI worker options") from None


class CliWorker:
    def __init__(self, options, *, strip_env=(), credential_sources=None):
        self.settings = cli_settings(options)
        # These are vendor protocol variable names, never credential values.
        self.credential_sources = credential_sources or {}
        self.auth_path = ''
        self.auth_failure = False
        # Set per call by Backend: receives ('thought'|'text', text) while a Grok stream arrives.
        self.on_delta = None
        self.strip_env = {"OPENAI_API_KEY", "CODEX_API_KEY", "ANTHROPIC_API_KEY"}
        self.strip_env.update(self.settings.strip_env)
        self.strip_env.update(strip_env)

    def _argv(self):
        settings = self.settings
        if not settings.command or not settings.vendor or not settings.cwd:
            raise WorkerError("CLI worker command, vendor and cwd must be configured", "configuration")
        executable = shutil.which(settings.command)
        if not executable:
            raise WorkerError("Configured CLI worker executable is unavailable", "configuration")
        arguments = list(settings.args)
        if not arguments:
            arguments = (["exec", "--json", "--sandbox", settings.sandbox or "read-only", "-"] if settings.vendor == "codex"
                         else ["--prompt-file", "{prompt_file}", "--output-format", "json"] if settings.vendor == "grok"
                         else ["--print", "--output-format", "json"])
        if settings.vendor == "codex":
            valid = arguments[0] == "exec" and "--json" in arguments and "-" in arguments
            forbidden = ("resume", "fork", "--last")
        else:
            if settings.vendor == "grok":
                valid = "--prompt-file" in arguments
                if valid:
                    index = arguments.index("--prompt-file")
                    valid = index + 1 < len(arguments) and arguments[index + 1] == "{prompt_file}"
            else:
                valid = ("--print" in arguments or "-p" in arguments)
            valid = valid and "--output-format" in arguments
            if valid:
                index = arguments.index("--output-format")
                allowed = ("json", "streaming-json") if settings.vendor == "grok" else ("json",)
                valid = index + 1 < len(arguments) and arguments[index + 1] in allowed
            forbidden = ("--resume", "--continue", "-r", "-c", "--worktree", "--fork-session", "--session-id")
        if not valid or any(argument.split("=", 1)[0] in forbidden for argument in arguments):
            raise WorkerError("CLI worker requires a fresh noninteractive JSON/stdin invocation", "configuration")
        if settings.vendor == "codex" and settings.sandbox:
            # Explicit inherited authority wins over a configured sandbox argument.
            filtered = []
            skip_value = False
            for argument in arguments:
                if skip_value:
                    skip_value = False
                    continue
                if argument in ('--sandbox', '-s'):
                    skip_value = True
                    continue
                if argument.startswith(('--sandbox=', '-s=')):
                    continue
                filtered.append(argument)
            arguments = filtered
            arguments[1:1] = ['--sandbox', settings.sandbox]
            for directory in settings.add_dirs:
                arguments[1:1] = ['--add-dir', directory]
            if arguments[0] != 'exec' or '--json' not in arguments or '-' not in arguments:
                raise WorkerError('CLI worker requires a fresh noninteractive JSON/stdin invocation', 'configuration')
        if settings.vendor == 'codex' and settings.model:
            configured_model = None
            for index, argument in enumerate(arguments):
                if argument in ('-m', '--model') and index + 1 < len(arguments):
                    configured_model = arguments[index + 1]
                elif argument.startswith('--model='):
                    configured_model = argument.split('=', 1)[1]
                if argument in ('-c', '--config') and index + 1 < len(arguments):
                    if arguments[index + 1].split('=', 1)[0].strip() == 'model':
                        configured_model = arguments[index + 1].split('=', 1)[1].strip().strip('\"\'')
                if configured_model is not None and configured_model != settings.model:
                    raise WorkerError('Codex CLI args conflict with the selected backend model', 'configuration')
            if configured_model is None:
                arguments[1:1] = ['--model', settings.model]
        return [executable] + arguments

    @staticmethod
    def _stop(process):
        """Kill the worker's process group. Best effort: never raise.

        This runs in a `finally`, so an exception here replaces whatever the
        caller was already raising. That masked a real WorkerError as a bare
        PermissionError, and it is not theoretical: on macOS killpg() on a
        group leader that has already exited returns EPERM, not ESRCH, so the
        cleanup raised whenever the child exited first. A child that is already
        gone is the success case for a stop, not a failure.
        """
        from .procutil import kill_group
        try:
            kill_group(process.pid)
        except ProcessLookupError:
            pass
        except PermissionError:
            # macOS reports a reaped or not-yet-reaped group leader as EPERM.
            # There is nothing left to kill, so this is the outcome we wanted.
            pass

    def cancel(self):
        """Kill the CLI process currently serving this worker, if any (user interrupt)."""
        process = getattr(self, '_current', None)
        if process is not None and process.poll() is None:
            self._stop(process)
            return True
        return False

    def chat(self, messages, tools=None, **kwargs):
        if tools:
            # CLI workers take no API tool schema. Only an explicitly configured engine_tools worker
            # gets engine tools, through the JSON text protocol below; the engine executes every call.
            if not self.settings.engine_tools:
                raise WorkerError("CLI workers do not accept the harness API tool schema unless engine_tools is configured", "configuration")
            if any(not isinstance(s, dict) or not isinstance(s.get('function'), dict)
                   or not isinstance(s['function'].get('name'), str) for s in tools):
                raise WorkerError("CLI engine tool schema entries must name a function", "configuration")
            instructions = tool_protocol(tools)
            if self.settings.vendor == 'codex':
                instructions += ('\nYour final output is consumed by the Tessera dispatcher. '
                                 'It is how you call the supplied engine tools, even though they are '
                                 'not native CLI tools. Follow the output schema: final is null for '
                                 'tool calls, tool_calls is empty for a final answer, and arguments_json '
                                 'contains a JSON-encoded argument object. '
                                 'The advertised engine tools execute in a separate Tessera host process '
                                 'under the operator\'s current permissions. Native CLI sandbox metadata '
                                 'describes the CLI process, not that host process. A read-only native '
                                 'sandbox does not make advertised engine write tools read-only. '
                                 'Report permission failures from actual tool results, not from inference.')
            messages = [{'role': 'system', 'content': instructions}, *messages]
        if self.settings.vendor == 'grok':
            from .grok_worker import chat
            result = chat(self, messages)
        else:
            prompt = "Task input (the caller owns this task and its scope):\n" + json.dumps(messages, ensure_ascii=False)
            argv = self._argv()
            # Read the grant only once this worker is actually configured to spawn.
            # An incomplete command, or a tool schema this worker will not run, has to
            # be reported as that configuration error. Checking ~/.codex/auth.json first
            # hid those errors on any machine without a Codex login file, and it made
            # every Codex test depend on that file.
            if self.settings.vendor == 'codex':
                self._require_grant()
            if tools and self.settings.vendor == 'codex':
                with tempfile.TemporaryDirectory(prefix='tessera-codex-tools-') as directory:
                    schema = Path(directory) / 'reply.json'
                    schema.write_text(json.dumps(codex_tool_schema(tools)), encoding='utf-8')
                    argv[2:2] = ['--output-schema', str(schema)]
                    result = self._invoke(argv, self.environment(), prompt.encode('utf-8'))
                result = codex_tool_reply(result)
            else:
                result = self._invoke(argv, self.environment(), prompt.encode('utf-8'))
        return parse_tool_reply(result, tools) if tools else result

    def _require_grant(self):
        """Fail before a subprocess when this host's Codex OAuth grant is not usable."""
        import socket
        from .codex_grant import grant_state
        state = grant_state(self.settings.auth_path or None)
        if state['state'] == 'ok':
            return
        host = socket.gethostname() or 'this host'
        expiry = ''
        if state['access_exp'] is not None:
            expiry = datetime.fromtimestamp(state['access_exp'], timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
            expiry = f' (expired {expiry})'
        raise WorkerError(
            f'Codex CLI worker on {host} has no usable OAuth grant{expiry}: {state["detail"]}',
            'configuration')

    def environment(self):
        environment = dict(os.environ)
        for name in self.strip_env:
            environment.pop(name, None)
        return environment

    def _invoke(self, argv, environment, prompt):
        settings = self.settings
        self.auth_failure = False
        self.last_failure = None
        self._stderr_tail = b""
        try:
            from .procutil import popen
            process = popen(argv, cwd=settings.cwd, env=environment, stdin=subprocess.PIPE,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        except (OSError, ValueError):
            raise WorkerError("CLI worker could not start", "configuration") from None
        self._current = process
        try:
            raw = self._collect(process, prompt)
        except WorkerError as error:
            self._record_failure(error.category, str(error), None, b"")
            raise
        except (OSError, ValueError):
            self._record_failure("connection", "transport failed", None, b"")
            raise WorkerError("CLI worker transport failed", "connection") from None
        finally:
            self._current = None
            self._stop(process)
            process.wait()
            for stream in (process.stdin, process.stdout, process.stderr):
                stream.close()
        if process.returncode:
            category = 'authentication' if settings.vendor == 'grok' and self.auth_failure else 'provider'
            reason = 'exited unsuccessfully'
            # Local OS denials are not evidence of a provider rejection.
            if re.search(rb'(?i)(operation not permitted|permission denied|\bEPERM\b|\bEACCES\b)', self._stderr_tail):
                category = 'permission'
                reason = 'sandbox/permission failure'
            self._record_failure(category, reason, process.returncode, raw)
            raise WorkerError(f"CLI worker {reason} (exit {process.returncode})", category)
        try:
            return self._decode(raw.decode("utf-8"))
        except (ValueError, KeyError, TypeError) as error:
            self._record_failure("provider", f"invalid result: {error}", process.returncode, raw)
            raise WorkerError(f"CLI worker returned an invalid or incomplete result ({error})") from None

    def _record_failure(self, category, reason, returncode, stdout):
        """Bounded evidence for operators; never part of model-visible text."""
        self.last_failure = {"category": category, "reason": reason, "exit_code": returncode,
                             "stderr_tail": self._stderr_tail[-2048:].decode("utf-8", "replace"),
                             "stdout_tail": bytes(stdout or b"")[-2048:].decode("utf-8", "replace")}

    def _collect(self, process, prompt):
        """Drain both pipes without storing stderr or exceeding the combined byte cap."""
        started = time.monotonic()
        deadline = started + self.settings.timeout
        offset = 0
        consumed = 0
        stdout_bytes = 0
        auth_tail = b""
        chunks = []
        stream_tail = b""
        self._streamed_text = None  # None: undecided; True: plain answer streamed; False: JSON, see FinalStream
        self._final = None
        next_progress = started + 30.0
        with selectors.DefaultSelector() as selector:
            for stream in (process.stdin, process.stdout, process.stderr):
                os.set_blocking(stream.fileno(), False)
                selector.register(stream, selectors.EVENT_WRITE if stream is process.stdin else selectors.EVENT_READ)
            while selector.get_map():
                now = time.monotonic()
                remaining = deadline - now
                if remaining <= 0:
                    raise WorkerError(
                        f"CLI worker timed out after {now - started:.1f}s "
                        f"with {stdout_bytes} stdout bytes before auth_path could be recorded",
                        "timeout")
                if self.settings.vendor == 'grok' and now >= next_progress:
                    print(
                        f"grok-cli progress: elapsed={now - started:.1f}s "
                        f"stdout_bytes={stdout_bytes} remaining={remaining:.1f}s",
                        file=sys.stderr, flush=True)
                    next_progress = now + 30.0
                wait = remaining if self.settings.vendor != 'grok' else min(remaining, max(0.1, next_progress - now))
                for key, _ in selector.select(wait):
                    stream = key.fileobj
                    if stream is process.stdin:
                        try:
                            offset += os.write(stream.fileno(), prompt[offset:offset + 65536])
                        except (BlockingIOError, InterruptedError):
                            continue
                        except BrokenPipeError:
                            offset = len(prompt)
                        if offset == len(prompt):
                            selector.unregister(stream)
                            stream.close()
                    else:
                        try:
                            chunk = os.read(stream.fileno(), 65536)
                        except (BlockingIOError, InterruptedError):
                            continue
                        if not chunk:
                            selector.unregister(stream)
                            continue
                        if self.settings.vendor == 'grok':
                            auth_tail = (auth_tail + chunk)[-2048:]
                            if re.search(rb'(?i)(\b(?:401|403)\b|not logged in|login required|unauthenticated|unauthorized|auth[^\n]{0,80}(?:failed|expired|required))', auth_tail):
                                self.auth_failure = True
                        if stream is process.stdout and self.settings.vendor == 'grok' and self.on_delta:
                            stream_tail = self._stream(stream_tail + chunk)
                        if stream is process.stdout:
                            consumed += len(chunk)
                            if consumed > self.settings.max_output_bytes:
                                raise WorkerError(f"CLI worker stdout exceeds its configured limit "
                                                  f"({self.settings.max_output_bytes} bytes)", "configuration")
                            chunks.append(chunk)
                            stdout_bytes += len(chunk)
                        else:
                            self._stderr_tail = (self._stderr_tail + chunk)[-4096:]
            try:
                process.wait(timeout=max(0, deadline - time.monotonic()))
            except subprocess.TimeoutExpired:
                raise WorkerError(
                    f"CLI worker timed out after {time.monotonic() - started:.1f}s "
                    f"with {stdout_bytes} stdout bytes before auth_path could be recorded",
                    "timeout") from None
        return b"".join(chunks)

    def _stream(self, data):
        """Forward complete streaming-json lines as live deltas; return the unfinished tail."""
        *lines, tail = data.split(b"\n")
        for line in lines:
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if not isinstance(event, dict) or not isinstance(event.get('data'), str):
                continue
            try:
                if event.get('type') == 'thought':
                    self.on_delta('thought', event['data'])
                elif event.get('type') == 'text':
                    if self._streamed_text is None and event['data'].strip():
                        # A reply opening with JSON or a fence may be the engine protocol: never shown raw.
                        self._streamed_text = not event['data'].lstrip().startswith(('{', '`'))
                        self._final = FinalStream() if not self._streamed_text else None
                    if self._streamed_text:
                        self.on_delta('text', event['data'])
                    elif self._final is not None:
                        text = self._final.feed(event['data'])
                        if text:
                            self.on_delta('text', text)
            except Exception:
                pass  # A display sink must never break the model call.
        return tail

    def _decode(self, raw):
        if self.settings.vendor == 'grok' and '"type":"end"' in raw.replace(' ', '') and '\n' in raw.strip():
            text, thought, result = [], [], None
            for line in raw.splitlines():
                if not line.strip():
                    continue
                event = json.loads(line)
                if event.get('type') == 'text' and isinstance(event.get('data'), str):
                    text.append(event['data'])
                elif event.get('type') == 'thought' and isinstance(event.get('data'), str):
                    thought.append(event['data'])
                elif event.get('type') == 'end':
                    result = event
            if result is None:
                raise ValueError('Unfinished Grok stream')
            result = {**result, 'text': ''.join(text), 'thought': ''.join(thought)}
            raw = json.dumps(result)
        if self.settings.vendor == 'grok':
            result = json.loads(raw)
            if isinstance(result, dict) and isinstance(result.get('error'), dict):
                error = result['error']
                if error.get('status', error.get('code')) in (401, 403):
                    raise WorkerError('Official Grok authentication was denied', 'authentication')
            if not isinstance(result, dict) or result.get('stopReason') != 'end_turn':
                raise ValueError('Unsuccessful or incomplete Grok result')
            text, usage = result.get('text'), result.get('usage', {})
        elif self.settings.vendor == "claude":
            result = json.loads(raw)
            if not isinstance(result, dict) or result.get("type") != "result" or result.get("subtype") != "success" or result.get("is_error") is not False:
                raise ValueError("Unsuccessful CLI result")
            text = result["result"]
            usage = result.get("usage", {})
        else:
            text = ""
            usage = {}
            completed = False
            for line in raw.splitlines():
                event = json.loads(line)
                if not isinstance(event, dict):
                    raise ValueError("Invalid CLI event")
                if event.get("type") == "turn.started":
                    completed = False
                    text = ""
                if event.get("type") in ("error", "turn.failed"):
                    raise ValueError("Unsuccessful CLI turn")
                if event.get("type") == "item.completed":
                    item = event.get("item")
                    if not isinstance(item, dict):
                        raise ValueError("Invalid CLI item")
                    if item.get("type") == "agent_message":
                        text = item["text"]
                if event.get("type") == "turn.completed":
                    completed = True
                    usage = event.get("usage", {})
            if not completed:
                raise ValueError("Unfinished CLI turn")
        if not isinstance(text, str) or not text.strip() or not isinstance(usage, dict):
            raise ValueError("Missing CLI result text")
        counters = {}
        for name in ("input_tokens", "output_tokens", "cached_input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens", "total_tokens"):
            value = usage.get(name)
            if type(value) in (int, float) and math.isfinite(value) and value >= 0:
                counters[name] = value
        reply = {"content": text, "tool_calls": [], "usage": counters, "finish_reason": "stop"}
        if self.settings.vendor == 'grok' and isinstance(result.get('thought'), str) and result['thought'].strip():
            reply["reasoning"] = result['thought']
        return reply


class FinalStream:
    """Incrementally decode the string inside a {"final": "..."} reply; anything else stays held back.

    Feed raw text chunks; each call returns newly decoded answer text. The wrapper, tool-call JSON and
    anything after the closing quote are never returned.
    """
    PREFIX = re.compile(r'\s*\{\s*"final"\s*:\s*"')
    # Strict JSON-shaped prefix of the wrapper: once the buffer cannot become it, stop for good.
    POSSIBLE = re.compile(r'\s*(\{\s*("(f(i(n(a(l("(\s*(:\s*)?)?)?)?)?)?)?)?)?)?')

    def __init__(self):
        self.buffer = ''
        self.state = 'prefix'  # prefix -> string -> done | rejected
        self.pending = ''      # an unfinished escape sequence

    def feed(self, chunk):
        if self.state in ('done', 'rejected'):
            return ''
        if self.state == 'prefix':
            self.buffer += chunk
            match = self.PREFIX.match(self.buffer)
            if not match:
                partial = self.POSSIBLE.match(self.buffer)
                if not partial or partial.end() != len(self.buffer):
                    self.state = 'rejected'  # tool_calls or other JSON: held back entirely
                return ''
            self.state = 'string'
            chunk, self.buffer = self.buffer[match.end():], ''
        out = []
        text = self.pending + chunk
        self.pending = ''
        index = 0
        while index < len(text):
            char = text[index]
            if char == '"':
                self.state = 'done'
                break
            if char != '\\':
                out.append(char)
                index += 1
                continue
            if index + 1 >= len(text):
                self.pending = text[index:]
                break
            code = text[index + 1]
            if code == 'u':
                if index + 6 > len(text):
                    self.pending = text[index:]
                    break
                digits = text[index + 2:index + 6]
                try:
                    value = int(digits, 16)
                except ValueError:
                    self.state = 'rejected'
                    break
                if 0xD800 <= value <= 0xDBFF:  # surrogate pair needs the low half too
                    if index + 12 > len(text):
                        self.pending = text[index:]
                        break
                    if text[index + 6:index + 8] == '\\u':
                        try:
                            low = int(text[index + 8:index + 12], 16)
                        except ValueError:
                            low = 0
                        if 0xDC00 <= low <= 0xDFFF:
                            out.append(chr(0x10000 + ((value - 0xD800) << 10) + (low - 0xDC00)))
                            index += 12
                            continue
                    self.state = 'rejected'
                    break
                out.append(chr(value))
                index += 6
                continue
            simple = {'"': '"', '\\': '\\', '/': '/', 'b': '\b', 'f': '\f', 'n': '\n', 'r': '\r', 't': '\t'}
            if code not in simple:
                self.state = 'rejected'
                break
            out.append(simple[code])
            index += 2
        return ''.join(out)


def tool_protocol(tools):
    """Engine tool contract for CLI models; the engine, not the CLI, executes every call."""
    listed = [{'name': s['function']['name'], 'description': s['function'].get('description', ''),
               'parameters': s['function'].get('parameters', {})} for s in tools]
    return ('You are running inside the Tessera engine. Your own CLI tools are not the engine tools. '
            'Engine tools available to you (the engine executes them and returns results in a later message):\n'
            + json.dumps(listed, ensure_ascii=False) +
            '\nTo call engine tools, reply with ONLY a JSON object {"tool_calls": [{"name": <tool name>, '
            '"arguments": {<object matching parameters>}}]}. To answer the user, reply with plain text '
            '(or {"final": <text>}). Messages with role "tool" are results of your earlier calls.')


def codex_tool_schema(tools):
    return {'type': 'object', 'properties': {
        'final': {'type': ['string', 'null']},
        'tool_calls': {'type': 'array', 'items': {
            'type': 'object', 'properties': {
                'name': {'type': 'string', 'enum': [s['function']['name'] for s in tools]},
                'arguments_json': {'type': 'string'}},
            'required': ['name', 'arguments_json'], 'additionalProperties': False}}},
        'required': ['final', 'tool_calls'], 'additionalProperties': False}


def codex_tool_reply(result):
    try:
        reply = json.loads(result['content'])
        calls = reply.get('tool_calls', [])
        if not calls and isinstance(reply.get('final'), str):
            normalized = {'final': reply['final']}
        else:
            normalized = {'tool_calls': []}
            for call in calls:
                arguments = call.get('arguments')
                if 'arguments_json' in call:
                    arguments = json.loads(call['arguments_json'])
                if not isinstance(arguments, dict):
                    raise ValueError('Tool arguments must be an object')
                normalized['tool_calls'].append({'name': call['name'], 'arguments': arguments})
        return {**result, 'content': json.dumps(normalized)}
    except (ValueError, KeyError, TypeError, AttributeError):
        raise WorkerError('Codex returned an invalid structured engine reply', 'provider') from None


TRAILING_TOOL_CALLS = re.compile(r'\{\s*"tool_calls"\s*:')


def _trailing_tool_calls(text):
    """Models often narrate first and then emit the call object; accept it only when it ends the reply."""
    for match in reversed(list(TRAILING_TOOL_CALLS.finditer(text))):
        try:
            value, end = json.JSONDecoder().raw_decode(text, match.start())
        except ValueError:
            continue
        if isinstance(value, dict) and not text[end:].strip():
            return value, text[:match.start()].strip()
    return None, ''


def parse_tool_reply(result, tools):
    text = (result.get('content') or '').strip()
    names = {s['function']['name'] for s in tools}
    candidate = text
    if candidate.startswith('```'):
        candidate = candidate.strip('`').split('\n', 1)[-1].rsplit('```', 1)[0].strip()
    preamble = ''
    try:
        value = json.loads(candidate)
    except ValueError:
        value, preamble = _trailing_tool_calls(candidate)
        if value is None:
            return result  # Plain-text answer is a valid final reply under the protocol.
    if isinstance(value, dict) and set(value) == {'final'} and isinstance(value['final'], str):
        return {**result, 'content': value['final'], 'tool_calls': []}
    if not isinstance(value, dict) or 'tool_calls' not in value:
        return result
    calls = value['tool_calls']
    if not isinstance(calls, list):
        raise WorkerError('CLI model returned an empty or invalid tool_calls list', 'provider')
    if not calls:
        # The output schema advertises final as ["string", "null"] with an
        # empty tool_calls array, so {"final": null, "tool_calls": []} is a
        # schema-valid reply meaning "nothing to say and nothing to call".
        # Raising here turned that legal shape into a provider error and
        # failed the whole turn.
        final = value.get('final')
        if final is None:
            return {**result, 'content': preamble, 'tool_calls': []}
        if isinstance(final, str):
            return {**result, 'content': (preamble + final) if preamble else final, 'tool_calls': []}
        raise WorkerError('CLI model returned an empty or invalid tool_calls list', 'provider')
    parsed = []
    for index, call in enumerate(calls):
        if (not isinstance(call, dict) or call.get('name') not in names
                or not isinstance(call.get('arguments', {}), dict)):
            raise WorkerError(f'CLI model requested an unknown or malformed engine tool call: {call!r:.200}', 'provider')
        parsed.append({'id': f'cli-call-{index}-{abs(hash(json.dumps(call, sort_keys=True))) % 10**12}',
                       'name': call['name'], 'arguments': call.get('arguments', {})})
    return {**result, 'content': preamble, 'tool_calls': parsed, 'finish_reason': 'tool_calls'}
