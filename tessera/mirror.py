"""Automatic append mirroring through the existing durable capture outbox."""

from dataclasses import asdict
import json
import threading
import time
import weakref

from .aethermind import AetherMindClient
from .capture import Capture


class AppendMirror:
    def __init__(self, config, *, client_factory=None, clock=time.monotonic):
        self.config = config
        self.client_factory = client_factory or self._client
        self.clock = clock
        self.lock = threading.RLock()
        self.outboxes = {}
        self.logs = weakref.WeakSet()
        self.failures = 0
        self.last_error = None
        self.next_retry = {}

    def _client(self):
        from .bundled_engine import AetherMindUnavailable, choose_memory, unavailable_message
        settings = self.config.aethermind
        choice = choose_memory(self.config)
        if not (choice['endpoints'] or choice['url']):
            raise AetherMindUnavailable(unavailable_message(choice))
        endpoint_factory = None
        if any(str(item or '').startswith('mcp:') for item in choice['endpoints']):
            from .mcp_servers import resolve_endpoint_factory
            endpoint_factory = resolve_endpoint_factory(self.config)
        return AetherMindClient(choice['url'] or '', token_env=settings.token_env, timeout=settings.timeout,
            endpoints=choice['endpoints'], bundled_path=settings.bundled_path,
            endpoint_factory=endpoint_factory,
            probe_timeout=settings.probe_timeout,
            probe_interval=settings.probe_interval, create_project=False,
            read_only_projects=[settings.personal_project] if settings.personal_project else [])

    def _outbox(self, slug, session_id):
        if slug not in self.outboxes:
            settings = self.config.aethermind
            from .passive_status import configured_path
            root = configured_path(self.config.home, self.config.daemon.mirror_dir) / slug
            names = [settings.token_env, self.config.jev.api_key_env, self.config.voice.api_key_env]
            for backend in self.config.backends.values():
                names.append(backend.api_key_env)
            for reference in self.config.credentials.values():
                names.append(reference.env)
            capture = Capture(self.client_factory(), root, settings.project, settings.personal_project,
                              settings.author, secret_envs=names)
            capture.session_id = session_id
            self.outboxes[slug] = capture
        return self.outboxes[slug]

    def record(self, slug, session_id, source, record):
        with self.lock:
            try:
                capture = self._outbox(slug, session_id)
                number = record['i'] if source == 'chat' else record['id']
                request_id = session_id + '-' + source + '-' + str(number)
                # Scrub values before JSON escaping can disguise a selected credential.
                cleaned = capture._snapshot(record)
                body = json.dumps({'source': source, 'record': cleaned}, ensure_ascii=False, sort_keys=True)
                capture._enqueue(request_id, capture._layer(body, 'append/' + source))
                self._retry(slug, capture)
            except Exception as error:
                # The primary log already committed; reattach reconciles any local gap.
                self.failures += 1
                from .bundled_engine import AetherMindUnavailable
                if isinstance(error, AetherMindUnavailable):
                    self.last_error = str(error)
                else:
                    detail = str(error).replace('\n', ' ').strip()
                    name = type(error).__name__
                    self.last_error = name if not detail or detail == name else name + ': ' + detail[:200]

    def _retry(self, slug, capture, force=False):
        if capture.project_id == self.config.aethermind.personal_project:
            return
        eligible = any(item['project_id'] == capture.project_id for item in capture.debt())
        if not eligible:
            self.next_retry.pop(slug, None)
            return
        if capture.client is None or (not force and self.clock() < self.next_retry.get(slug, 0)):
            return
        capture.retry_outbox()
        remaining = any(item['project_id'] == capture.project_id for item in capture.debt())
        delay = self.config.daemon.mirror_retry_seconds if remaining else 0
        self.next_retry[slug] = self.clock() + delay

    def recall(self, slug, session_id, *, query=None, level=None, index=None):
        with self.lock:
            return self._outbox(slug, session_id).memory_recall(query=query, level=level, index=index)

    def attach(self, slug, session_id, log):
        with self.lock:
            if log in self.logs:
                return
            self.logs.add(log)
            def append(message):
                self.record(slug, session_id, 'chat', asdict(message))
            log.subscribe(append)
            for message in log.iter():
                append(message)

    def sync(self):
        with self.lock:
            for slug, capture in self.outboxes.items():
                self._retry(slug, capture, force=True)
        return self.status()

    def status(self):
        with self.lock:
            debt = 0
            for capture in self.outboxes.values():
                debt += len(capture.debt())
            notice = 'Pending mirror writes; remote durability is unproven.' if debt or self.failures else None
            if self.last_error:
                notice = (notice + ' ' if notice else '') + self.last_error
            return {'enabled': True,
                    'configured': bool(self.config.aethermind.endpoints or self.config.aethermind.url),
                    'attached_outboxes': len(self.outboxes), 'write_debt': debt,
                    'local_failures': self.failures,
                    'notice': notice}
