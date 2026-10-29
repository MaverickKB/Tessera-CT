"""One chat session, stable prompt prefix, and the bounded model/tool turn loop."""

from datetime import datetime, timezone
import json
from urllib.parse import urlparse
import os
import re
import subprocess
from queue import Empty, Queue, SimpleQueue
from pathlib import Path
import threading
import uuid

from .agents import AgentManager
from .aethermind import AetherMindClient, EMBEDDED_LOCK_REFUSAL, tree_read_display
from dataclasses import replace

from .backend import Backend, BackendError
from .capture import Capture
from .compactor import Compactor
from .config import load_config
from .hooks import HookBus
from .jev import JevRouter
from .attachments import describe as describe_attachments, read_path as read_attachment_path
from .log import ChatLog
from .prompts import SYSTEM_PROMPT_TEMPLATE
from .host_cua import register_host_cua
from .tools import (ApprovalRefused, WorkingDirectory, approval_rule, build_registry, clip_output,
                    clip_records, object_schema)
from .remote import register_remote_tools
from .skills import Shelf, append_shelf_note, register_skill_tools
from .tree import SummaryTree
from .view import View

INTERRUPT_REPLIES = {'user': 'Interrupted by the user.', 'restart': 'Interrupted by a core restart.'}

# A tool call the provider left unparsed in the text: GLM <tool_call>name<arg_key>..., or <tool_call>{"name": ...}.
TEXT_TOOL_CALL = re.compile(r'<tool_call>\s*(?:\{\s*"name"\s*:\s*")?([A-Za-z_][\w.-]*)')
# The engine-tool text protocol a CLI chat model (for example Grok over its OAuth login) replies with.
CLI_TOOL_CALL = re.compile(r'"tool_calls"\s*:\s*\[\s*\{\s*"name"\s*:\s*"([A-Za-z_][\w.-]*)"')


class Session:
    def __init__(self, config=None, backend=None, *, root=None, hooks=None, capture=None,
                 router=None, compactor_backend=None, approve=None, inherited_view='',
                 depth=0, allowed_tools=None, pool=None, agent_backend_factory=None, parent_view=None, session_id=None, configure_tools=None, judge_human=None, agent_factory=None,
                 companion_client_factory=None, tool_ceiling=None, config_owner=None):
        self.config = (config or load_config()).validate()
        # The root session owns the live config. A descendant passes that same
        # callable, so its registry and shelf read the object a reload just installed.
        self._config_owner = config_owner if config_owner is not None else (lambda: self.config)
        self.root = Path(root or self.config.home / 'chat')
        self.session_id = session_id or uuid.uuid4().hex
        self.turn_meta = {}
        self.turn = 0
        self.last_turn_success = False
        self.last_turn_silent = False
        self.rest_requested = False
        self.depth = depth
        self.approve = approve
        self.judge_human = judge_human
        self._approval_decisions = {}
        self._pending_work = SimpleQueue()
        # Mid-turn user steering and interrupts, applied at iteration boundaries.
        self._steers = Queue()
        self._interrupt = threading.Event()
        self._parent_cancelled = None
        self.last_turn_interrupted = False
        self.interrupt_reason = 'user'
        self._in_turn = threading.Event()
        self._session_context = []
        # Unclipped companion briefs. session_briefs pages these; turn state gets one page.
        self._brief_full = []
        # Session briefs reach the model once, in the next turn after they land; later turns
        # carry a one-line pointer and the model rereads them on demand with session_briefs.
        self._session_context_sent = 0
        # name -> hash of the SKILL.md last loaded in this process. A restart rebuilds it from the log.
        self._loaded_skills = {}
        # Names the turn-state block told the model not to reload, computed once per
        # turn by _skill_pin_line(). observe_turn needs this: a skill the prompt
        # pinned is available to the turn even though no skill_load call appears in
        # it, and scoring that as "skill not loaded" punished obedience.
        self._pinned_skills = []
        self._next_backend = None
        self._turn_failover = None
        self.backend_builder = None
        self.delta_sink = None
        self.reasoning = 'low'
        self.thinking_budget = 0
        self._default_main = self.config.roles.get('main')
        self.inherited_view = inherited_view
        self.agent_backend_factory = agent_backend_factory
        self._turn_lock = threading.RLock()
        self._closed = False
        self.hooks = hooks or HookBus(self.config.hooks)
        injected_backend = backend is not None
        self.backend = backend if injected_backend else self._backend('main')
        self.fast_backend = self.backend
        if not injected_backend and 'fast' in self.config.roles:
            self.fast_backend = self._backend('fast')
        secret_values = []
        names = [self.config.aethermind.token_env, self.config.jev.api_key_env, 'TESSERA_SERVER_TOKEN']  # legacy variable stays redacted; it grants nothing
        for settings in self.config.backends.values():
            names.append(settings.api_key_env)
        for name in names:
            if name and os.environ.get(name):
                secret_values.append(os.environ[name])
        self.log = ChatLog(self.root / 'log', secret_values=secret_values)
        for previous in self.log.iter():
            if previous.meta.get('session_id') == self.session_id:
                self.turn = max(self.turn, previous.meta.get('turn', 0))
        self.log.subscribe(self._receive_work)
        try:
            self.tree = SummaryTree(self.root, self.log, node_bytes=self.config.view.node_bytes)
            self.view = View(self.root, self.tree, high_bytes=self.config.view.high_bytes, low_bytes=self.config.view.low_bytes)
            self.log.subscribe(self.view.on_message)
            self.agents = agent_factory(self) if agent_factory else AgentManager(self, pool)
            self.workspace = WorkingDirectory(self.config.workspace or self.config.cwd)
            self.tools = build_registry(self.workspace, self.view, self.agents.spawn,
                                        self.config.tools.approval, self._approve_once,
                                        self.config.tools.clip_chars, self.config.tools.timeout, parent_view=parent_view,
                                        policy=self.config.tools, agent_wait=self.agents.wait_for_reports,
                                        file_reader=lambda path: read_attachment_path(Path(self.log.root).parent, path))
            if self.config.agents.backend_policy:
                from .agent_selection import backend_catalog
                self.tools.register('agent_backends',
                                    'List configured worker models, capability levels and relative cost ranks. '
                                    'Choose the cheapest capable worker; escalate when task difficulty or a failed attempt warrants it. '
                                    'Ranks express operator preference, not dollar prices.',
                                    object_schema({}), lambda: backend_catalog(self.config))
            tools_config = self.config.tools
            self.tools.allowed = lambda name: approval_rule(name, tools_config.approval_classes,
                                                            tools_config.approval_rules) == 'allow'
            register_remote_tools(self.tools, self.config.remotes, self.config.home, self.config.tools.clip_chars)
            self._plugin_tools = ()
            self._plugin_servers = ()
            if depth == 0:
                from .mcp_servers import register_mcp_server_tools, session_plugin_servers
                # session_tools and enabled are the switches. A computer-use provider
                # is registered on its own path, not again as a generic plugin.
                servers = session_plugin_servers(self.config)
                self._plugin_tools = tuple(register_mcp_server_tools(
                    self.tools, servers, self.config.tools.clip_chars))
                self._plugin_servers = tuple(self.config.mcp_servers)
            client = None
            memory = self.config.aethermind
            self.memory_choice = None
            self.uses_embedded_engine = False
            # An injected capture already owns the client. Choosing here would
            # probe or launch before discarding that client. A capture with no
            # client is refused at the turn; it does not run memoryless.
            if capture is None:
                from .bundled_engine import choose_memory
                self.memory_choice = choose_memory(self.config)
                client = self._open_memory_client(self.memory_choice)
                self._remember_embedded_use()
            self.capture = capture or Capture(client, self.root / 'outbox', memory.project, memory.personal_project, memory.author,
                                              note_project=memory.note_project, recall_limit=memory.recall_limit,
                                              omit_outcomes=memory.capture_omit_outcomes,
                                              inheritance_refresh_seconds=memory.inheritance_refresh_seconds)
            self.capture.install(self.hooks)
            self.router = router or JevRouter.from_config(self.config, approval=self.config.tools.approval, approve=self._approve_once)
            self.router.bind_session(self.root, self.session_id,
                                     floor_slug=getattr(self.config.autonomy, 'infinity_slug', ''))
            self.router.computer_use_jev = self.config.computer_use.jev
            self.router.credentials = self.config.credentials
            self.router.chat_log = self.log
            self.router.cancellation_registry = self.tools
            from .action_judge import DependencyResolver
            self.dependency_resolver = DependencyResolver(self.config.jev)
            self.capture.judgekit = self.router
            self.router.install(self.hooks)
            self._register_memory_tools()
            # The shelf switch is the operator's. Unleashed does not attach skill tools
            # to a session that started with the shelf off.
            self.shelf = Shelf(self.config.home, self.workspace.path, self.config.skills) if self.config.skills.enabled else None
            if self.shelf is not None:
                self.shelf.live = lambda: self._config_owner().skills
                register_skill_tools(self.tools, self.shelf, self.config.tools.clip_chars)
            register_host_cua(self.tools, self.config)
            if configure_tools is not None:
                configure_tools(self.tools)
            self.companion_identity = self._identity()
            from .companions import Companions
            from .mcp_client import McpClient
            paused = (self.config.home / 'run' / 'companions-paused.json').exists()
            # Companions serve the resident session only; subagents never dial them.
            self.companions = Companions(self.config if depth == 0 else _companions_off(self.config),
                                         client_factory=companion_client_factory, paused=paused)
            self.companions.install(self)
            if allowed_tools is not None:
                self.tools = self.tools.restricted(allowed_tools)
            # Delegation narrows, never widens. A session built as a subagent receives the
            # parent's effective tool set as a ceiling and is intersected with it after it
            # builds its own registry, so a grandchild spawned with no explicit list cannot
            # regain tools its parent was denied.
            if tool_ceiling is not None:
                self.tools = self.tools.restricted([name for name in self.tools.names() if name in tool_ceiling])
            # Record the ceiling after allowlist and ceiling intersection. Nothing
            # added later, including a mode change, may widen it.
            self._bind_live_tools()
            self.tool_ceiling = set(self.tools.names())
            self.system_prompt = SYSTEM_PROMPT_TEMPLATE.format(agent_name=self.config.agent_name,
                                                               operator=self.config.operator_label(),
                                                               user_instructions=self._instructions())
            guidance = self._guidance()
            if guidance:
                self.system_prompt += '\n' + guidance + '\n'
            if compactor_backend is None and not injected_backend and 'compactor' in self.config.roles:
                compactor_backend = self._backend('compactor')
            settings = self.config.compactor
            self.compactor = Compactor(self.tree, self.view, compactor_backend, self.system_prompt, self.tools.schemas(),
                                       workers=settings.workers, retries=settings.retries, context_bytes=settings.context_bytes,
                                       judge_threshold=settings.judge_threshold,
                                       judge=self.router.compaction_judge if settings.judge_enabled else None)
            self.tree.subscribe(self._on_compaction)
            _, results = self._emit('session_start', {'session_id': self.session_id})
            self.turn = max(self.turn, self.capture.turn_number)
            inheritance = self.capture.inheritance_snapshot
            for result in results:
                if isinstance(result, dict) and isinstance(result.get('full'), str) and result['full'].strip():
                    self._brief_full.append(result['full'])
            for context in self._contexts(results):
                if context != inheritance:
                    self._session_context.append(context)
            if self.shelf is not None and self.depth == 0:
                append_shelf_note(self.log, self.shelf)
            self._start_compactor()
        except Exception:
            if hasattr(self, 'compactor'):
                self.compactor.stop()
            self.log.close()
            raise

    def _remember_embedded_use(self):
        """The lock sentence follows the service this session is actually using."""
        choice = self.memory_choice if isinstance(getattr(self, 'memory_choice', None), dict) else {}
        self.uses_embedded_engine = (
            choice.get('source') == 'embedded'
            and bool(choice.get('url') or choice.get('endpoints'))
        )

    def _embedded_lock_refusal(self):
        """Refuse before the model runs when the embedded engine cannot write.

        The capabilities report is the engine's. The sentence applies only
        when this session's client is the embedded engine.
        """
        if not getattr(self, 'uses_embedded_engine', False):
            return None
        client = self.capture.client
        if client is None or not hasattr(client, 'reported_lock_backend'):
            return None
        if client.reported_lock_backend() == 'unavailable':
            return EMBEDDED_LOCK_REFUSAL
        return None

    def _identity(self):
        title = self.root.name
        meta = self.root / 'meta.json'
        if meta.is_file():
            try:
                title = json.loads(meta.read_text(encoding='utf-8')).get('title') or title
            except (OSError, ValueError):
                pass
        return {'slug': self.root.name, 'title': str(title)}

    def _has_tool(self, name):
        return any(schema['function']['name'] == name for schema in self.tools.schemas())

    def _bind_live_tools(self):
        """Point this registry at the shared config owner.

        The callable is the root session's. A reload replaces that session's
        config; every descendant registry reads the new tools object on the
        next offer and the next call. The delegation ceiling is not touched.
        """
        owner = self._config_owner

        def live_tools():
            current = owner()
            if current is None:
                return None
            return getattr(current, 'tools', None)

        self.tools.policy_of = live_tools
        self.tools.disabled_of = lambda: tuple(getattr(live_tools() or self.config.tools, 'disabled', ()) or ())

    def _route_schema(self, route):
        """Every route offers the same registered tools. The model decides when to use them."""
        return self.tools.schemas()

    @staticmethod
    def _tools_block(schema):
        """Tool schemas after the gist. A new tool changes this block, not the gist prefix."""
        lines = ['<tools>']
        for item in schema or []:
            function = item['function']
            payload = {'name': function['name'],
                       'description': function.get('description', ''),
                       'parameters': function.get('parameters', {})}
            lines.append(json.dumps(payload, sort_keys=True, ensure_ascii=False))
        lines.append('</tools>')
        return '\n'.join(lines)

    @staticmethod
    def _offered_names(schema):
        return {item['function']['name'] for item in schema or []}

    def _text_for_offered(self, text, offered):
        """Drop clauses that name a tool this route does not offer."""
        if not text:
            return ''
        registered = self._offered_names(self.tools.schemas())
        kept = []
        for piece in re.split(r'(\n|;)', text):
            if piece in ('\n', ';'):
                kept.append(piece)
                continue
            names = set(re.findall(r'\b[a-z][a-z0-9_]*\b', piece))
            if 'build_tree' in names or (names & registered) - set(offered):
                continue
            kept.append(piece)
        cleaned = ''.join(kept)
        cleaned = re.sub(r'[ \t]*;[ \t]*;', ';', cleaned)
        cleaned = re.sub(r'(?:[ \t]*;)+[ \t]*$', '', cleaned, flags=re.M)
        cleaned = re.sub(r'\n{3,}', '\n\n', cleaned)
        return cleaned.strip()

    def _guidance(self):
        """Stable sections. Only tools offered on every route may be named here."""
        prompt = self.config.prompt
        parts = []
        if prompt.body_map:
            body = self._body_map()
            if body:
                parts.append(body)
        if prompt.architecture:
            parts.append(prompt.architecture)
        if prompt.skill_guidance and self._has_tool('skill_load'):
            parts.append(prompt.skill_guidance)
        # The prefix is cached. The turn keeps facts.
        notes = self._route_notes(self._offered_names(self.tools.schemas()))
        if notes:
            parts.append(notes)
        return '\n\n'.join(parts)

    def _route_notes(self, offered):
        """Memory and delegation text for this route, limited to tools it actually offers."""
        prompt = self.config.prompt
        parts = []
        memory_tools = {'memory_recall', 'memory_tree', 'memory_note'}
        if prompt.memory_guidance and (memory_tools & set(offered)):
            memory = self.config.aethermind
            personal = ''
            if memory.personal_project:
                canonical = memory.note_project == memory.personal_project
                role = 'canonical personal store; typed notes go here' if canonical else 'personal, read-only'
                personal = f', {memory.personal_project} ({role})'
            text = self._text_for_offered(prompt.memory_guidance.replace('{project}', memory.project or '(no store configured)')
                                          .replace('{personal}', personal), offered)
            if text:
                parts.append(text)
        lines = [line for name, line in prompt.delegation_guidance.items() if name in offered]
        if lines:
            intro = self._text_for_offered(prompt.delegation_intro, offered)
            block = [intro] if intro else []
            block.extend('- ' + line for line in lines)
            parts.append('\n'.join(block))
        return '\n\n'.join(parts)

    def set_workspace(self, path):
        """Point this session's file tools and checkout line at a chosen tree."""
        resolved = self.workspace.set(path or self.config.workspace or self.config.cwd)
        if self.shelf is not None:
            self.shelf.cwd = resolved
        return str(resolved)

    def _checkout_line(self):
        """The session workspace, computed when the turn starts. Nothing here is a fixed path or SHA."""
        cwd = str(self.workspace.path)
        sha, dirty = self._read_checkout(cwd)
        return f'Checkout: cwd {cwd}; git HEAD {sha}; dirty {dirty}'

    @staticmethod
    def _popen_is_real():
        popen = subprocess.Popen
        return getattr(popen, '__module__', '') == 'subprocess' and getattr(popen, '__name__', '') == 'Popen'

    @classmethod
    def _read_checkout(cls, cwd):
        git_dir = cls._git_dir(cwd)
        if git_dir is None:
            return 'not a git checkout', 'unknown'
        sha = cls._read_head(git_dir)
        dirty = 'unknown'
        if cls._popen_is_real():
            try:
                status = subprocess.run(['git', '-C', cwd, 'status', '--porcelain'],
                                        capture_output=True, text=True, timeout=2, check=False)
                if status.returncode == 0:
                    dirty = 'dirty' if status.stdout.strip() else 'clean'
            except (OSError, subprocess.SubprocessError):
                pass
        return sha, dirty

    @staticmethod
    def _git_dir(cwd):
        path = Path(cwd) / '.git'
        try:
            if path.is_dir():
                return path
            if path.is_file():
                text = path.read_text(encoding='utf-8', errors='replace').strip()
                if text.startswith('gitdir:'):
                    raw = text.split(':', 1)[1].strip()
                    located = Path(raw)
                    if not located.is_absolute():
                        located = (path.parent / located).resolve()
                    if located.is_dir():
                        return located
        except OSError:
            return None
        return None

    @staticmethod
    def _read_head(git_dir):
        try:
            head = (git_dir / 'HEAD').read_text(encoding='utf-8', errors='replace').strip()
        except OSError:
            return 'unavailable'
        if not head.startswith('ref:'):
            return head or 'unavailable'
        ref_name = head.split(':', 1)[1].strip()
        bases = [git_dir]
        common = git_dir / 'commondir'
        try:
            if common.is_file():
                raw = common.read_text(encoding='utf-8', errors='replace').strip()
                located = Path(raw)
                if not located.is_absolute():
                    located = (git_dir / located).resolve()
                bases.append(located)
        except OSError:
            pass
        for base in bases:
            try:
                ref = base / ref_name
                if ref.is_file():
                    value = ref.read_text(encoding='utf-8', errors='replace').strip()
                    if value:
                        return value
            except OSError:
                pass
            packed = base / 'packed-refs'
            try:
                if not packed.is_file():
                    continue
                for line in packed.read_text(encoding='utf-8', errors='replace').splitlines():
                    if not line or line.startswith('#') or line.startswith('^'):
                        continue
                    sha, _, name = line.partition(' ')
                    if name == ref_name and sha:
                        return sha
            except OSError:
                continue
        return 'unavailable'

    def _body_map(self):
        """Models, services and machines as configured, so the agent knows where its parts run."""
        config = self.config
        lines = []
        for role, name in sorted(config.roles.items()):
            backend = config.backends.get(name)
            if backend is not None:
                host = urlparse(backend.base_url).hostname or 'cli'
                lines.append(f'- model {role}: {name} ({backend.model or "default"} at {host})')
        for name, server in sorted(config.mcp_servers.items()):
            if server.enabled and server.session_tools:
                prefix = f' [{server.tool_prefix}*]' if server.tool_prefix else ''
                lines.append(f'- service {name}{prefix}: {server.description or "MCP server"}')
        for name, remote in sorted(config.remotes.items()):
            if remote.enabled or remote.desk:
                tools = (['remote_*'] if remote.enabled else []) + (['desk_*'] if remote.desk else [])
                lines.append(f'- machine {name} ({", ".join(tools)}): {remote.description or remote.host}')
        from .mcp_servers import computer_backend
        backend = computer_backend(config)
        if backend == 'mcp':
            lines.append('- machine this Mac (computer_*): the configured computer-use provider')
        elif backend == 'native':
            lines.append('- machine this Mac (computer_*): TesseraCUA background computer use on the host')
        elif backend == 'linux':
            lines.append('- machine this computer (computer_*): Linux desktop tools; a missing tool is reported as unavailable')
        elif backend == 'unsupported':
            lines.append('- machine this computer (computer_*): desktop computer use is not supported on this operating system')
        elif config.cua.mac_enabled:
            lines.append('- machine this Mac (mac_*): the host Tessera runs on')
        return 'Your body (from config):\n' + '\n'.join(lines) if lines else ''

    def _instructions(self):
        """User instructions plus the operator's identity files, in configured order."""
        text = self.config.user_instructions
        parts = []
        total = 0
        for item in self.config.identity_files():
            path = Path(item).expanduser()
            if not path.is_absolute():
                path = self.config.home / path
            raw = path.read_bytes()
            total += len(raw)
            # identity_max_bytes bounds the combined identity text, not each file.
            if total > self.config.identity_max_bytes:
                raise ValueError('Identity files exceed identity_max_bytes')
            parts.append(raw.decode('utf-8'))
        for part in parts:
            text = (text + '\n\n' if text else '') + part
        return text

    def _approve_once(self, name, arguments):
        key = json.dumps([name, arguments], sort_keys=True)
        if key not in self._approval_decisions:
            self._approval_decisions[key] = bool(self.approve and self.approve(name, arguments))
        return self._approval_decisions[key]

    def _receive_work(self, message):
        if message.kind == 'work' and not message.meta.get('work_id'):
            # Durable channel work enters through its guarded turn queue.
            # Injecting it into another active turn would lose its causal hop.
            self._pending_work.put(message)

    def _on_compaction(self, level, index):
        self._emit('compaction', {'phase': 'built', 'level': level, 'index': index})

    def _drain_work(self, messages, since):
        while True:
            try:
                message = self._pending_work.get_nowait()
            except Empty:
                return
            if message.i >= since:
                messages.append({'role': 'user', 'content': message.text})

    def steer(self, text, *, context=None, metadata=None):
        """Queue user guidance for the running turn; False when no turn is running."""
        if not isinstance(text, str) or not text.strip():
            raise ValueError('Steer text cannot be empty')
        if not self._in_turn.is_set():
            return False
        if context is not None and not isinstance(context, str):
            raise ValueError('Steer context must be text')
        self._steers.put({'text': text, 'context': context, 'metadata': metadata or {}})
        return True

    def interrupt(self, reason='user'):
        """Cancel the running turn and its owned work now. reason: user | restart."""
        active = self._in_turn.is_set() or bool(self.agents.active())
        self.interrupt_reason = reason
        self._interrupt.set()
        self.tools.cancel()
        cancel_agents = getattr(self.agents, 'cancel', None)
        if callable(cancel_agents):
            cancel_agents(reason)
        # A CLI model call can run for minutes; stop it now rather than at the next boundary.
        for backend in {id(b): b for b in (getattr(self, 'backend', None), getattr(self, 'fast_backend', None), self._turn_failover) if b is not None}.values():
            cancel = getattr(backend, 'cancel', None)
            if callable(cancel):
                try:
                    cancel()
                except Exception:
                    pass
        return active

    def take_unapplied_steers(self):
        """Guidance that arrived too late for its turn; the caller queues it as a new turn."""
        texts = []
        while True:
            try:
                texts.append(self._steers.get_nowait())
            except Empty:
                return texts

    def _drain_steers(self, messages):
        applied = 0
        while True:
            try:
                guidance = self._steers.get_nowait()
            except Empty:
                return applied
            text = self.log.redact(guidance['text'])
            metadata = {'turn': self.turn, 'session_id': self.session_id, 'steer': True,
                        **self.turn_meta, **guidance['metadata']}
            self.log.append('user', text, metadata)
            content = '[Steer from the user during this turn] ' + text
            if guidance['context']:
                content += '\n\n' + self.log.redact(guidance['context'])
            messages.append({'role': 'user', 'content': content})
            applied += 1

    @staticmethod
    def _reaches_past(schema, content, calls):
        # Unparsed markup means the model wanted a tool; it guesses names it was never shown, so any name counts.
        offered = {s['function']['name'] for s in schema or []}
        return (bool(TEXT_TOOL_CALL.search(content)) or bool(CLI_TOOL_CALL.search(content))
                or any(call.get('name') not in offered for call in calls))

    def _interrupted(self):
        if self._parent_cancelled is not None and self._parent_cancelled.is_set():
            self._interrupt.set()
        if not self._interrupt.is_set():
            return None
        reply = INTERRUPT_REPLIES[self.interrupt_reason]
        self.last_turn_interrupted = True
        interrupted_meta = {'turn': self.turn, 'success': False, 'interrupted': True}
        request_id = self.turn_meta.get('request_id')
        if request_id:
            interrupted_meta['request_id'] = request_id
        self.log.append('agent', reply, interrupted_meta)
        return reply

    def _backend(self, role):
        settings = self.config.backend_for(role)
        backend = Backend(settings.name, settings.base_url, settings.model, settings.api_key_env, settings.extra)
        backend.bind_endpoint(self._config_owner, settings.name)
        return backend

    def queue_backend(self, name, backend=None):
        self._next_backend = (name, backend)

    def _apply_queued_backend(self):
        if self._next_backend is None:
            return
        name, backend = self._next_backend
        self._next_backend = None
        roles = dict(self.config.roles)
        if name:
            roles['main'] = roles['fast'] = name
        else:
            roles.pop('fast', None)
            roles['main'] = self._default_main
        self.config = replace(self.config, roles=roles)
        self.backend = backend if backend is not None else self._backend('main')
        self.fast_backend = self.backend

    def _build(self, name):
        if self.backend_builder is not None:
            return self.backend_builder(name)
        config = replace(self.config, roles={**self.config.roles, 'main': name})
        settings = config.backend_for('main')
        backend = Backend(settings.name, settings.base_url, settings.model, settings.api_key_env, settings.extra)
        backend.bind_endpoint(self._config_owner, settings.name)
        return backend

    @staticmethod
    def _unavailable(exc):
        # Only an unreachable or failing service fails over; a model's own refusal or bad request does not.
        if not isinstance(exc, BackendError):
            return False
        if exc.category in ('connection', 'timeout'):
            return True
        if exc.kind == 'incomplete_response':
            # A length-capped answer is a failed response, not a refusal: a reasoning model can spend
            # the whole token ceiling thinking and return no content at all (GLM-5.3-Flash through
            # a router route loses live turns this way). Re-ask on the failover model.
            return True
        return exc.kind == 'http' and type(exc.http_status) is int and 500 <= exc.http_status <= 599

    @staticmethod
    def _incomplete(response):
        # The turn loop raises the same error after _chat returns, which is outside the failover
        # wrapper. Raise the length case here instead so it can fail over. content_filter stays a
        # refusal: the loop guard raises it, and a refusal must not silently re-ask another model.
        if not response.get('tool_calls') and response.get('finish_reason') == 'length':
            raise BackendError('Backend returned an incomplete answer', kind='incomplete_response')
        return response

    def _chat(self, backend, messages, schema):
        if self._turn_failover is not None:
            return self._incomplete(self._streamed(self._turn_failover, messages, schema))
        try:
            return self._incomplete(self._streamed(backend, messages, schema))
        except BackendError as exc:
            target = self.config.roles.get('failover')
            primary = self.config.roles.get('main', '')
            if not target or target == primary or target not in self.config.backends or not self._unavailable(exc):
                raise
            self._turn_failover = self._build(target)
            label = self.config.backends[target].model or target
            self.log.append('note', f'{primary or "Default model"} unavailable; used {label}.',
                            {'label': 'failover', 'from': primary, 'to': target, 'turn': self.turn, **exc.diagnostics()})
            return self._streamed(self._turn_failover, messages, schema)

    def _streamed(self, backend, messages, schema):
        options = self._reasoning_options()
        if hasattr(backend, 'cancel_event'):
            backend.cancel_event = self._interrupt
        if self.delta_sink is None or not hasattr(backend, 'on_delta'):
            return backend.chat(messages, tools=schema, **options)
        backend.on_delta = lambda kind, text: self.delta_sink(kind, self.log.redact(text))
        try:
            return backend.chat(messages, tools=schema, **options)
        finally:
            backend.on_delta = None

    def _reasoning_options(self):
        level = self.reasoning if self.reasoning in ('off', 'low', 'medium', 'high') else 'low'
        if level == 'off':
            return {'chat_template_kwargs': {'enable_thinking': False}}
        options = {'reasoning_effort': level, 'chat_template_kwargs': {'enable_thinking': True}}
        if self.thinking_budget > 0:
            options['thinking_budget'] = self.thinking_budget
        return options

    def _session_briefs(self, offset=0):
        if self._brief_full:
            limit = self.config.companions.atlas.max_brief_bytes
            shown = clip_records('\n\n'.join(self._brief_full), limit, 'session_briefs', offset=offset or 0)
            return {'briefs': shown, 'offset': offset or 0}
        return {'briefs': list(self._session_context)} if self._session_context else {'briefs': [], 'note': 'No session briefs.'}

    def _register_memory_tools(self):
        self.tools.register('session_briefs', 'Reread the companion briefs delivered at session start. Pass offset to page a clipped brief. They are not repeated every turn.', object_schema({'offset': {'type': 'integer', 'minimum': 0}}), self._session_briefs)
        if hasattr(self.capture, 'register_tools'):
            self.capture.register_tools(self.tools)
            return
        if hasattr(self.capture, 'memory_recall'):
            self.tools.register('memory_recall', 'Read external memory by query or tree node.', object_schema({'query': {'type': 'string'}, 'level': {'type': 'integer', 'minimum': 0}, 'index': {'type': 'integer', 'minimum': 0}}), self.capture.memory_recall)
        if hasattr(self.capture, 'memory_note'):
            self.tools.register('memory_note', 'Queue an explicit note in external memory.', object_schema({'text': {'type': 'string'}}, ['text']), self.capture.memory_note)

    def _emit(self, event, payload):
        base = {'session_id': self.session_id, 'turn': self.turn, 'turn_id': self.turn,
                'cwd': str(self.workspace.path), 'approval_policy': self.tools.approval,
                'policy': self.tools.approval}
        base.update(self.log.redact(self.turn_meta))
        base.update(self.log.redact(payload))
        results = self.hooks.emit(event, base)
        return self.log.redact(base), self.log.redact(results)

    @staticmethod
    def _contexts(results):
        contexts = []
        for result in results:
            if isinstance(result, dict) and result.get('context'):
                contexts.append(str(result['context']))
        return contexts

    def _attachment_content(self, refs):
        if not refs:
            return [], []
        root = getattr(self.log, 'root', None)
        if root is None:
            return ['Attached files could not be opened.'], []
        try:
            return describe_attachments(Path(root).parent, refs)
        except (OSError, ValueError) as exc:
            return [f'Attached files could not be opened ({exc}).'], []

    def _turn_state(self, route, contexts, schema=None):
        offered = self._offered_names(schema if schema is not None else self.tools.schemas())
        memory = self.capture.inheritance_block()
        state = ['<turn-state>', f'Date: {datetime.now(timezone.utc).isoformat()}',
                 self._checkout_line(), f'Turn: {self.turn}',
                 'Routing: ' + json.dumps(route, sort_keys=True)]
        state.append('Tessera engine tool policy: ' + self.tools.approval + '. '
                     'Advertised tools execute on the Tessera host with this policy. '
                     'Native CLI sandbox permissions apply to the CLI process separately. '
                     'A requested approval is pending, not a completed action. '
                     'Use actual tool results as evidence for edits, tests and permission failures.')
        if self.config.agents.backend_policy and 'spawn_agent' in offered:
            state.append('Delegation: select the cheapest capable worker. Use routine for simple '
                         'inspection or narrow edits, standard for implementation/debugging, complex '
                         'for difficult architecture/reasoning. Read agent_backends for configured '
                         'choices. Escalate with a task-specific reason or after a failed cheaper attempt. '
                         'Use spawn_agent for implementation; swarm_job collects response-only evidence. '
                         'For a change to a git repository pass repo: the worker writes and commits on its '
                         'own worktree branch, and you merge the branch its report names. '
                         'Use engine tools instead of starting a second Tessera process on this live home.')
        if self.turn_meta.get('from_slug'):
            state.append('Session message provenance: ' + json.dumps(self.turn_meta, sort_keys=True))
        if self.turn_meta.get('awareness'):
            state.append('Presence while the operator was already here (context only; no reply needed): '
                         + self.turn_meta['awareness'])
        if self.depth:
            state.append('You are already the assigned background agent. Execute your task with your available tools.')
        if self.inherited_view:
            if {'zoom', 'date'} & offered:
                state.append('Inherited parent view (use source=parent for its zoom/date IDs):')
            else:
                state.append('Inherited parent view:')
            state.append(self.inherited_view)
        if memory:
            state.append(memory)
        brief = self.capture.turn_brief() if hasattr(self.capture, 'turn_brief') else ''
        if brief:
            state.append(brief)
        choice = getattr(self, 'memory_choice', None) or {}
        if choice.get('external_error'):
            if choice.get('url'):
                follow = 'The embedded engine is active.'
            elif 'bytes present, not started' in (choice.get('reason') or ''):
                follow = choice['reason']
            else:
                follow = 'The embedded engine is not in this install.'
            state.append('AetherMind external service was not used (' + choice['external_error']
                         + '). ' + follow)
        fresh = self._session_context[self._session_context_sent:]
        if self._session_context_sent and self._session_context and 'session_briefs' in offered:
            state.append(f'Session briefs: {self._session_context_sent} delivered earlier; call session_briefs to reread them.')
        state.extend(fresh)
        self._session_context_sent = len(self._session_context)
        state.extend(contexts)
        pin = self._skill_pin_line()
        if pin:
            state.append(pin)
        if self.turn == 1:
            notes = self._route_notes(offered)
            if notes:
                state.append(notes)
            state.append('A clipped task is already in the chat. Do not open prompt dumps under .grok/sessions to recover it.')
        if self.turn == 1 and self.config.prompt.turn_reminder and 'memory_recall' in offered:
            reminder = self._text_for_offered(self.config.prompt.turn_reminder, offered)
            if reminder:
                state.append(reminder)
        state.append('</turn-state>')
        return '\n'.join(state)

    def _loaded_from_log(self):
        """Last successful skill_load per name, from the chat log. Survives a process restart."""
        loaded = {}
        pending = None
        for message in self.log.iter():
            if message.kind == 'tool':
                try:
                    body = json.loads(message.text)
                except json.JSONDecodeError:
                    pending = None
                    continue
                if body.get('name') == 'skill_load':
                    pending = (body.get('arguments') or {}).get('name')
                else:
                    pending = None
                continue
            if message.kind == 'echo' and pending and message.meta.get('success'):
                loaded[pending] = message.meta.get('skill_stamp')
            pending = None
        return loaded

    def _skill_pin_line(self):
        """One line when a previously loaded skill is still current. Never the procedure itself.

        Also records the pinned names in self._pinned_skills, because "already loaded,
        do not reload" is a statement about what the turn already has. Anything that
        scores a turn's skill use has to agree with the prompt, not contradict it.
        """
        self._pinned_skills = []
        if self.shelf is None or not self._has_tool('skill_load'):
            return ''
        known = self._loaded_from_log()
        for name, stamp in self._loaded_skills.items():
            known[name] = stamp
        if not known:
            return ''
        current = []
        stale = []
        for name in sorted(known):
            try:
                stamp = self.shelf.stamp(name)
            except (ValueError, OSError):
                stale.append(name)
                continue
            remembered = known.get(name)
            if remembered is None or remembered == stamp:
                current.append(name)
            else:
                stale.append(name)
        parts = []
        if current:
            parts.append('Already loaded, do not reload unless the file changed: ' + ', '.join(current) + '.')
            self._pinned_skills = current
        if stale:
            parts.append('Reload, the file changed: ' + ', '.join(stale) + '.')
        return ' '.join(parts)

    def _open_memory_client(self, choice):
        memory = self.config.aethermind
        choice = choice or {}
        memory_url = choice.get('url') or ''
        endpoints = list(choice.get('endpoints') or [])
        if not endpoints and not memory_url:
            return None
        endpoint_factory = None
        if any((ep or '').startswith('mcp:') for ep in endpoints):
            from .mcp_servers import resolve_endpoint_factory
            endpoint_factory = resolve_endpoint_factory(self.config)
        return AetherMindClient(memory_url or '', token_env=memory.token_env, timeout=memory.timeout,
                                endpoints=endpoints, bundled_path=memory.bundled_path,
                                endpoint_factory=endpoint_factory,
                                probe_timeout=memory.probe_timeout,
                                probe_interval=memory.probe_interval, create_project=memory.create_project,
                                read_only_projects=[memory.personal_project] if memory.personal_project and memory.personal_read_only else [],
                                recall_scan=memory.recall_scan)

    def _require_aethermind(self):
        """Refuse the turn when no working memory is attached.

        An injected capture with a client counts. A caller-supplied capture
        with no client does not. A choice is rechecked when this process
        started that engine and it has exited: a relaunch that fails refuses
        the turn even if the old client object is still set. Approval mode,
        including Unleashed, is not an input.
        """
        from .bundled_engine import AetherMindUnavailable, unavailable_message
        choice = getattr(self, 'memory_choice', None)
        if isinstance(choice, dict):
            refreshed = self.refresh_attached_memory()
            if not (refreshed.get('url') or refreshed.get('endpoints')):
                raise AetherMindUnavailable(unavailable_message(refreshed))
            if getattr(self.capture, 'client', None) is None:
                raise AetherMindUnavailable(unavailable_message(refreshed))
            self._start_compactor()
            return
        if getattr(getattr(self, 'capture', None), 'client', None) is not None:
            self._start_compactor()
            return
        raise AetherMindUnavailable(unavailable_message(None))

    def refresh_attached_memory(self, *, peers=True):
        """Re-check this session's memory service and point it at the result.

        The choice is process-wide. One probe is shared by every open session,
        including this one. A failed external service falls back to the embedded
        engine. A recovered external service replaces that fallback.
        """
        from .bundled_engine import refresh_shared_memory
        choice = getattr(self, 'memory_choice', None)
        if not isinstance(choice, dict):
            return choice
        notify = getattr(self, '_refresh_peers', None)
        if peers and notify is not None:
            notify(self)
            return self.memory_choice
        refreshed = refresh_shared_memory(self.config, choice)
        self._apply_memory_choice(refreshed)
        return self.memory_choice

    def _apply_memory_choice(self, refreshed):
        previous = getattr(self, 'memory_choice', None) or {}
        self.memory_choice = refreshed if isinstance(refreshed, dict) else previous
        if not isinstance(refreshed, dict):
            return
        moved = (previous.get('url') or '') != (refreshed.get('url') or '')
        moved = moved or list(previous.get('endpoints') or []) != list(refreshed.get('endpoints') or [])
        moved = moved or (previous.get('source') or '') != (refreshed.get('source') or '')
        if getattr(self.capture, 'client', None) is None or moved:
            self.capture.client = self._open_memory_client(refreshed)
        self._remember_embedded_use()

    def _memory_confirmed(self):
        """A service URL, an endpoint list, or an injected client is working memory."""
        choice = getattr(self, 'memory_choice', None)
        if isinstance(choice, dict) and (choice.get('url') or choice.get('endpoints')):
            return True
        return getattr(getattr(self, 'capture', None), 'client', None) is not None

    def _start_compactor(self):
        if self._memory_confirmed() and getattr(self, 'compactor', None) is not None:
            self.compactor.start()

    def run_turn(self, message, turn_meta=None):
        with self._turn_lock:
            if self._closed:
                raise RuntimeError('Session is closed')
            if not isinstance(message, str) or not message.strip():
                raise ValueError('Message cannot be empty')
            self._require_aethermind()
            if self._parent_cancelled is not None and self._parent_cancelled.is_set():
                self.last_turn_interrupted = True
                return INTERRUPT_REPLIES['user']
            self.turn_meta = dict(turn_meta or {})
            self._apply_queued_backend()
            self._turn_failover = None
            self._interrupt.clear()
            self.tools.reset_cancel()
            self.last_turn_interrupted = False
            self.rest_requested = False
            self.last_turn_silent = False
            wake = self.turn_meta.get('wake')
            self._in_turn.set()
            message = self.log.redact(message)
            self._action_goal = message
            self.turn += 1
            # Cleared here so a turn that never renders its state block cannot inherit
            # the previous turn's pin set.
            self._pinned_skills = []
            # Render before appending: the current user message appears exactly once.
            snapshot_count = self.log.count()
            gist = self.view.render()
            attached = self.turn_meta.pop('attachments', None) or []
            user_meta = {'turn': self.turn, 'session_id': self.session_id, **self.turn_meta}
            if attached:
                user_meta['attachments'] = attached
            self.log.append('user', message, user_meta)
            reply = ''
            success = False
            route = {}
            tool_records = []
            progress_steps = []
            failure = None
            judge_score = None
            verification = None
            needs_memory = 0.0
            try:
                refusal = self._embedded_lock_refusal()
                if refusal:
                    reply = refusal
                    failure = refusal
                    self.log.append('agent', refusal, {'turn': self.turn, 'success': False})
                    return reply
                payload, start_results = self._emit('turn_start', {'message': message, 'user': message, 'text': message, 'view_tail': gist[-8000:]})
                route = payload.get('routing') or payload.get('route') or {}
                if not isinstance(route, dict):
                    route = {'route': str(route)}
                if not route:
                    for result in start_results:
                        if isinstance(result, dict) and 'route' in result:
                            route = result if isinstance(result['route'], str) else result['route']
                            break
                if wake:
                    # A wake needs tools so the agent can act. It never routes to delegation.
                    route = {'route': 'tools', 'source': 'wake'}
                if not route:
                    route = self.router.route(message, gist[-8000:])
                if route.get('source') == 'heuristic' and route.get('route') == 'direct':
                    for schema in self.tools.schemas():
                        name = schema['function']['name']
                        if re.search(r'\b' + re.escape(name) + r'\b', message, re.IGNORECASE):
                            route = {**route, 'route': 'tools',
                                     'p': route.get('probabilities', {}).get('tools', 0.0),
                                     'override': 'Explicit registered tool reference'}
                            break
                needs_memory = payload.get('needs_memory', 0.0)
                contexts = self._contexts(start_results)
                if needs_memory > .5 and self.capture.client is not None and self.config.aethermind.auto_recall:
                    recalled = self.log.redact(self.capture.memory_recall(query=message))
                    recall_text = clip_output(json.dumps(recalled, ensure_ascii=False), self.config.tools.clip_chars)
                    self.log.append('note', 'Memory recall: ' + recall_text, {'needs_memory': needs_memory})
                    contexts.append('Memory recall: ' + recall_text)
                schema = self._route_schema(route)
                attachment_notes, attachment_images = self._attachment_content(attached)
                if attachment_notes:
                    message = message + '\n\nAttached:\n' + '\n\n'.join(attachment_notes)
                tools_block = self._tools_block(schema)
                session_view = self._turn_state(route, contexts, schema)
                messages = [{'role': 'system', 'content': self.system_prompt},
                            {'role': 'user', 'content': gist},
                            {'role': 'user', 'content': tools_block},
                            {'role': 'user', 'content': session_view},
                            {'role': 'user', 'content': message}]
                for image in attachment_images:
                    messages.append({'role': 'user', 'content': [
                        {'type': 'text', 'text': image['caption']},
                        {'type': 'image_url', 'image_url': {'url': image['data_url']}}]})
                if (route.get('route') == 'delegate' and self.depth == 0
                        and self.config.agents.max_depth > 0 and not self.config.agents.backend_policy):
                    reply = self.agents.spawn(f'turn-{self.turn}', message)
                    self.log.append('agent', reply, {'route': route, 'turn': self.turn})
                    success = True
                    return reply
                backend = self.fast_backend if route.get('route') == 'direct' else self.backend
                # A long session ends when the model answers, the user interrupts, or a real error stops it.
                # A counted iteration budget is not a stop.
                while True:
                    self._drain_work(messages, snapshot_count)
                    self._drain_steers(messages)
                    stopped = self._interrupted()
                    if stopped:
                        reply = stopped
                        return reply
                    if self.config.jev.session_gates_enabled and progress_steps:
                        progress_gate = self.router.swarm_gates(message, steps=progress_steps,
                                                                phase='admission', human=self.judge_human)
                        self.log.append('note', 'Session progress gate', {'gate': progress_gate})
                        if not progress_gate['allowed']:
                            reply = 'Stopped by the session progress gate; review the recorded decision.'
                            self.log.append('agent', reply, {'turn': self.turn, 'success': False, 'gate': progress_gate})
                            return reply
                    try:
                        response = self.log.redact(self._chat(backend, messages, schema))
                    except Exception:
                        stopped = self._interrupted()
                        if stopped:
                            reply = stopped
                            return reply
                        raise
                    stopped = self._interrupted()
                    if stopped:
                        reply = stopped
                        return reply
                    content = response.get('content') or ''
                    calls = response.get('tool_calls') or []
                    if route.get('route') == 'direct' and self.config.tools.promote_direct_route \
                            and self._reaches_past(schema, content, calls):
                        # The direct route is a guess; a model reaching for a tool it was not offered proves it wrong.
                        route = {**route, 'route': 'tools', 'override': 'Model reached for a tool the direct route lacks'}
                        backend = self.backend
                        schema = self.tools.schemas()
                        if self.delta_sink is not None:
                            self.delta_sink('reset', '')
                        continue
                    if not calls and response.get('finish_reason') in ('length', 'content_filter'):
                        raise BackendError('Backend returned an incomplete answer', kind='incomplete_response')
                    metadata = {'turn': self.turn, 'tool_calls': calls, 'usage': response.get('usage', {}),
                                'route': route, 'needs_memory': needs_memory}
                    request_id = self.turn_meta.get('request_id')
                    if request_id:
                        metadata['request_id'] = request_id
                    served = self._turn_failover or backend
                    if getattr(served, 'last_auth', ''):
                        metadata['served'] = {'backend': getattr(served, 'name', ''),
                                              'auth': served.last_auth}
                    if response.get('reasoning'):
                        metadata['thought'] = response['reasoning']
                    if not calls:
                        # Turn scoring and the output gate are separate, opt-in judge
                        # features. With jev.enabled off there is no judge and no turn
                        # scoring; with session_gates_enabled off the approval policy
                        # alone decides. Running either unconditionally made a fresh
                        # build fall back to the heuristic judge and record refusals
                        # the operator never asked for.
                        judge_on = self.config.jev.enabled
                        gates_on = self.config.jev.session_gates_enabled
                        if judge_on or gates_on:
                            if self.router._tools_failed(tool_records):
                                verification = self.router.verify_outputs([{'request': message, 'reply': content,
                                                                            'tool_outputs': tool_records}])[0]
                            else:
                                verification = self.router.verify_output(message, content, tool_records)
                            if judge_on:
                                judge_score = verification['completion']
                                metadata['verification'] = verification
                                metadata['judge_score'] = judge_score
                                metadata['judge_source'] = verification['source']
                            # The output gate is recorded whenever the judge or the
                            # session gate is on; session_gates_enabled only decides
                            # whether its verdict enforces success below.
                            metadata['output_gate'] = self.router.output_gate(
                                message, content, tool_records,
                                verification=verification, human=self.judge_human)
                        if wake:
                            self.last_turn_silent = self.rest_requested or not content.strip()
                            metadata['silent' if self.last_turn_silent else 'proactive'] = True
                    self.log.append('agent', content, metadata)
                    stopped = self._interrupted()
                    if stopped:
                        reply = stopped
                        return reply
                    if not calls and not self._steers.empty():
                        # Guidance arrived while the model answered: continue the turn with it.
                        messages.append({'role': 'assistant', 'content': content})
                        continue
                    if not calls:
                        reply = content
                        success = not self.config.jev.session_gates_enabled or metadata['output_gate']['allowed']
                        return reply
                    assistant = {'role': 'assistant', 'content': content or None, 'tool_calls': []}
                    for call in calls:
                        assistant['tool_calls'].append({'id': call['id'], 'type': 'function', 'function': {'name': call['name'], 'arguments': json.dumps(call['arguments'])}})
                    messages.append(assistant)
                    batch_contexts = []
                    images = []
                    for call in calls:
                        stopped = self._interrupted()
                        if stopped:
                            reply = stopped
                            return reply
                        result, record, contexts = self._call_tool(call, permitted=schema is not None and call.get('name') in {x['function']['name'] for x in schema})
                        tool_records.append(record)
                        messages.append({'role': 'tool', 'tool_call_id': call['id'], 'content': result})
                        batch_contexts.extend(contexts)
                        batch = record.get('images')
                        if batch:
                            images.extend(batch)
                        elif record.get('image'):
                            images.append(record['image'])
                    step = []
                    for record in tool_records[-len(calls):]:
                        step.append({'name': record['name'], 'arguments': record['arguments'],
                                     'result': record['result'], 'success': record['success']})
                    progress_steps.append(step)
                    progress_steps = progress_steps[-self.config.jev.loop_window:]
                    if batch_contexts:
                        messages.append({'role': 'user', 'content': '\n'.join(batch_contexts)})
                    for image in images:
                        # Images ride only in this turn's request; the log keeps the text result.
                        messages.append({'role': 'user', 'content': [
                            {'type': 'text', 'text': image['caption']},
                            {'type': 'image_url', 'image_url': {'url': image['data_url']}}]})
            except Exception as exc:
                stopped = self._interrupted()
                if stopped:
                    reply = stopped
                    failure = stopped
                    self.log.append('agent', stopped, {'turn': self.turn, 'success': False})
                    return reply
                failure = f'Turn failed ({type(exc).__name__})'
                # Keep the error category and kind on the log line: a worker that dies
                # mid-turn loses them otherwise, and sessions.py already carries them
                # for session turns (four codex workers were unrecoverable this way).
                details = {}
                if isinstance(exc, BackendError):
                    details = exc.diagnostics()
                    if details.get('error_detail'):
                        # The CLI worker's stderr can carry credential fragments.
                        details['error_detail'] = self.log.redact(details['error_detail'])
                self.log.append('agent', failure, {'turn': self.turn, 'success': False, **details})
                raise
            finally:
                self._in_turn.clear()
                self._interrupt.clear()
                self.last_turn_success = success
                flagged = not success
                self._emit('turn_end', {'message': message, 'user': message, 'reply': reply,
                                       'response': reply, 'success': success, 'failed': flagged, 'judge_score': judge_score, 'error': failure,
                                       'tools': tool_records, 'pinned_skills': list(self._pinned_skills),
                                       'route': route, 'verification': verification})
                self._emit('compaction', {'phase': 'scheduled', 'upto_i': self.log.count() - 1, 'view_bytes': len(self.view.render().encode('utf-8'))})

    def _note_conscience(self, tool, result):
        """The harness records the score. Jev does not write."""
        verdict = result.get('verdict') or ('no' if result.get('block') else 'abstain')
        probability = result.get('probability')
        # An abstain with no probability is not a score: the judge never got a question.
        # Logging those put one line in the transcript for every tool call in every session
        # and told nobody anything. A real yes or no still records.
        if verdict == 'abstain' and not isinstance(probability, (int, float)):
            return
        line = f'Conscience {verdict} on {tool}'
        if isinstance(probability, (int, float)):
            line += f' ({float(probability):.2f})'
        reason = str(result.get('reason') or '')
        if reason:
            line += ': ' + reason[:200]
        self.log.append('note', line, {'conscience': {'verdict': verdict, 'tool': tool}})

    def _open_conscience_lesson(self, tool, reason):
        """A failed score becomes a lesson. The harness writes it. Jev does not."""
        if 'cloud Jev' not in reason:
            return
        learning = getattr(getattr(self, 'manager', None), 'autonomy', None)
        store = getattr(learning, 'learning', None) if learning is not None else None
        if store is None:
            return
        try:
            store.record_lesson(
                self.root.name,
                f'pressure wake tool {tool}',
                reason[:300],
                'the next call on this pressure either answers it or is not made',
                source='conscience')
        except ValueError:
            return

    def _call_tool(self, call, permitted=True):
        self._approval_decisions.clear()
        name = call['name']
        arguments = call.get('arguments', {})
        self.log.append('tool', json.dumps({'name': name, 'arguments': arguments}), {'turn': self.turn, 'call_id': call['id']})
        payload, results = self._emit('pre_tool', {'name': name, 'tool': name, 'tool_name': name,
                                                'id': call['id'], 'arguments': arguments, 'args': arguments,
                                                'approval_policy': self.tools.approval,
                                                'approval_managed': True,
                                                'command': arguments.get('command', '')})
        blocked = not permitted
        reason = 'Tools are unavailable on the direct route' if blocked else ''
        action_judgment = None
        if permitted and self.config.jev.action_gate_enabled:
            from .action_judge import judge_action
            if self.delta_sink is not None:
                self.delta_sink('status', 'Checking action with Jev')
            try:
                action_judgment = judge_action(self, name, arguments, self.dependency_resolver)
            except Exception as error:
                self.tools.check_cancelled()
                tool = self.tools._tools.get(name)
                action_judgment = {'block': tool is None or tool.risky, 'source': 'unavailable',
                                   'reason': 'Jev action check unavailable (' + type(error).__name__ + '). '
                                             'Inspect or repair the judge before retrying this mutation.'}
            if action_judgment['block']:
                blocked = True
                reason = action_judgment['reason']
        for result in results:
            if isinstance(result, dict) and result.get('conscience'):
                self._note_conscience(name, result)
            if isinstance(result, dict) and result.get('block'):
                blocked = True
                reason = str(result.get('reason', 'Blocked by pre_tool hook'))
                self._open_conscience_lesson(name, reason)
        success = False
        attached_image = None
        attached = []
        if blocked:
            output = 'Blocked: ' + reason
        else:
            try:
                if self.delta_sink is not None:
                    self.delta_sink('status', 'Running ' + name)
                value = self.tools.execute(name, arguments)
                attached = []
                if isinstance(value, dict):
                    image = value.pop('image', None)
                    extra = value.pop('images', None)
                    if isinstance(image, dict):
                        attached.append(image)
                    if isinstance(extra, list):
                        attached.extend(item for item in extra if isinstance(item, dict))
                    if not (self.config.cua.attach_images and getattr(self.backend, 'worker', None) is None):
                        attached = []
                attached_image = attached[0] if attached else None
                value = self.log.redact(value)
                if name == 'memory_recall':
                    value = tree_read_display(value)
                output = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
                success = True
                if name == 'skill_load' and self.shelf is not None:
                    loaded_name = arguments.get('name')
                    if loaded_name:
                        self._loaded_skills[loaded_name] = self.shelf.stamp(loaded_name)
                if isinstance(value, dict):
                    success = not value.get('error') and value.get('exit_code', 0) in (0, None) and not value.get('timed_out', False)
            except ApprovalRefused as refusal:
                # A refusal is not a failure: the side effect never happened. Report
                # it as blocked so a hook, a board card or a judge reading `blocked`
                # does not conclude the mutation went through.
                blocked = True
                reason = f'Approval required for {refusal.tool} (policy: {refusal.policy})'
                output = 'Blocked: ' + reason
            except Exception as exc:
                output = f'Tool error ({type(exc).__name__}): {exc}'
        output = clip_output(self.log.redact(output), self.config.tools.clip_chars)
        echo_meta = {'turn': self.turn, 'call_id': call['id'], 'success': success,
                     'blocked': blocked, 'tool_risk': payload.get('tool_risk')}
        if action_judgment is not None:
            echo_meta['action_judgment'] = action_judgment
        loaded_name = arguments.get('name') if isinstance(arguments, dict) else None
        if name == 'skill_load' and success and loaded_name in self._loaded_skills:
            echo_meta['skill_stamp'] = self._loaded_skills[loaded_name]
        self.log.append('echo', output, echo_meta)
        record = {'id': call['id'], 'name': name, 'tool': name, 'tool_name': name, 'arguments': arguments,
                  'result': output, 'output': output, 'success': success, 'blocked': blocked,
                  'error': None if success else output, 'image': attached_image,
                  'images': attached if success else []}
        _, post_results = self._emit('post_tool', record)
        self._approval_decisions.clear()
        return output, record, self._contexts(results) + self._contexts(post_results)

    def status(self):
        names = list(self.config.backends)
        return {'backends': names, 'roles': self.config.roles, 'messages': self.log.count(),
                'view_bytes': len(self.view.render().encode('utf-8')),
                'write_debt': len(self.capture.debt()), 'active_agents': self.agents.active(),
                'companions': self.companions.status()}

    def close(self, end_session=True):
        with self._turn_lock:
            if self._closed:
                return
            self._closed = True
            try:
                self.agents.wait()
                release = getattr(self.tools, 'release', None)
                if release is not None:
                    release()
                if end_session:
                    self._emit('session_end', {})
                self.compactor.stop()
                self.view.save()
            finally:
                self.log.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


def _companions_off(config):
    from dataclasses import replace
    companions = replace(config.companions, atlas=replace(config.companions.atlas, enabled=False),
                         ember=replace(config.companions.ember, enabled=False))
    return replace(config, companions=companions)


def run_turn(session, message):
    return session.run_turn(message)
