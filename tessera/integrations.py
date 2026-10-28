"""Integrations inventory with real connection checks and persisted enable switches.

Every row comes from the live config. Checks contact the configured target (HTTP GET,
MCP tools/list, ssh, CLI --version, voice token mint) and report what actually happened.
Switches edit one boolean in config.toml (backed up first) and are re-validated.
"""

from .settings_ladder import credential_source
import json
import os
from pathlib import Path
import shutil
import subprocess
import threading
import time
import urllib.error
import urllib.request

from .credentials import CredentialResolver


def _get(url, timeout, headers=None, limit=1048576):
    request = urllib.request.Request(url, headers=headers or {}, method='GET')
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.status, response.read(limit)


def _host(url):
    from urllib.parse import urlsplit
    parts = urlsplit(url)
    return parts.netloc


class Integrations:
    def __init__(self, manager, *, config_path=None, clock=time.time):
        self.manager = manager
        self.config = manager.config
        self.config_path = Path(config_path) if config_path else Path(self.config.home) / 'config.toml'
        self.clock = clock
        self._results = {}
        self._pending = set()
        self._lock = threading.Lock()

    # ---- inventory -------------------------------------------------------------
    def _rows(self):
        c = self.config
        rows = []
        for name, b in c.backends.items():
            kind = c.accounts[b.account].provider_kind if b.account else (b.provider_kind or 'openai_chat')
            cli = (b.extra or {}).get('cli') or {}
            target = cli.get('vendor', '') + ' CLI' if kind == 'cli_worker' else _host(b.base_url)
            # Which sign-in can repair this backend. A Failed row that offers
            # nothing but Test is a dead end. The repair is a sign-in, and it is
            # only available when the credential is an OAuth
            # grant this harness can refresh; an API key is repaired by editing
            # the credential, not by a button here.
            repair = ''
            ref = c.credentials.get(b.credential_ref) if b.credential_ref else None
            if ref is not None and ref.oauth in ('xai', 'openai-codex'):
                repair = ref.oauth
            rows.append({'id': 'backend:' + name, 'group': 'Backends', 'name': name,
                         'type': 'CLI' if kind == 'cli_worker' else 'HTTP model', 'target': target,
                         'detail': ' · '.join(filter(None, (b.model or cli.get('vendor', ''), credential_source(c, name)['label']))),
                         'credential': credential_source(c, name), 'switch': 'Chat', 'enabled': b.chat,
                         'path': ('backends', name), 'key': 'chat', 'checkable': True,
                         'main': c.roles.get('main') == name, 'repair': repair})
        m = c.aethermind
        rows.append({'id': 'mcp:aethermind', 'group': 'MCP', 'name': 'AetherMind', 'type': 'MCP',
                     'target': ', '.join(_host(e) for e in (m.endpoints or ([m.url] if m.url else []))),
                     'detail': m.project, 'switch': 'On', 'enabled': True, 'path': ('aethermind',),
                     'key': 'enabled', 'checkable': bool(m.endpoints or m.url)})
        for name in ('ember', 'atlas'):
            comp = getattr(c.companions, name)
            endpoints = (list(comp.helmet_endpoints) + list(comp.companion_endpoints)) if name == 'ember' else list(comp.endpoints)
            if not endpoints:
                continue  # not configured: nothing real to show or switch
            rows.append({'id': 'mcp:' + name, 'group': 'Companions', 'name': name.capitalize(), 'type': 'MCP',
                         'target': ', '.join(self._target(e) for e in endpoints), 'detail': '', 'switch': 'Enabled',
                         'enabled': comp.enabled, 'path': ('companions', name), 'key': 'enabled', 'checkable': True})
        for name, server in c.mcp_servers.items():
            rows.append({'id': 'server:' + name, 'group': 'MCP', 'name': name, 'type': 'MCP ' + ('HTTP' if server.url else 'stdio'),
                         'target': _host(server.url) if server.url else ' '.join(server.command)[:120],
                         'detail': server.description, 'switch': 'Enabled', 'enabled': server.enabled,
                         'path': ('mcp_servers', name), 'key': 'enabled', 'checkable': True})
        for name, r in c.remotes.items():
            rows.append({'id': 'remote:' + name, 'group': 'Machines', 'name': name, 'type': 'SSH',
                         'target': f'{r.user}@{r.host}:{r.port}', 'detail': r.description, 'switch': 'Enabled',
                         'enabled': r.enabled, 'path': ('remotes', name), 'key': 'enabled', 'checkable': True,
                         'tools': ['remote_shell', 'remote_read_file', 'remote_write_file']})
        for name, r in c.remotes.items():
            if getattr(r, 'rdp_port', 0):
                rows.append({'id': 'rdp:' + name, 'group': 'Machines', 'name': name + ' RDP', 'type': 'RDP',
                             'target': f'{r.rdp_host or r.host}:{r.rdp_port}', 'detail': r.rdp_doc, 'switch': 'Enabled',
                             'enabled': r.rdp_enabled, 'path': ('remotes', name), 'key': 'rdp_enabled',
                             'checkable': True})
            if r.desk or getattr(r, 'rdp_port', 0):
                rows.append({'id': 'desk:' + name, 'group': 'Machines', 'name': name + ' desk', 'type': 'Desk',
                             'target': f'{r.user}@{r.host}', 'detail': 'desk_look, desk_act, desk_status over SSH',
                             'switch': 'Enabled', 'enabled': r.desk, 'path': ('remotes', name), 'key': 'desk',
                             'checkable': False, 'tools': ['desk_look', 'desk_act', 'desk_status']})
        inf = c.infrastructure
        if inf.prometheus_url or inf.enabled:
            rows.append({'id': 'monitoring:prometheus', 'group': 'Monitoring', 'name': 'Prometheus', 'type': 'HTTP',
                         'target': _host(inf.prometheus_url), 'detail': f'{len(c.hosts)} hosts',
                         'switch': 'Enabled', 'enabled': inf.enabled, 'path': ('infrastructure',), 'key': 'enabled',
                         'checkable': bool(inf.prometheus_url)})
        g = c.grafana
        if g.base_url or g.enabled:
            rows.append({'id': 'monitoring:grafana', 'group': 'Monitoring', 'name': 'Grafana', 'type': 'HTTP',
                         'target': _host(g.base_url), 'detail': '', 'switch': 'Enabled', 'enabled': g.enabled,
                         'path': ('grafana',), 'key': 'enabled', 'checkable': bool(g.base_url)})
        v = c.voice
        rows.append({'id': 'voice:xai', 'group': 'Voice', 'name': 'Voice', 'type': 'Realtime',
                     'target': _host(v.websocket_url), 'detail': ' / '.join(x for x in (v.model, str((v.session_options or {}).get('voice', ''))) if x),
                     'switch': 'Enabled', 'enabled': v.enabled, 'path': ('voice',), 'key': 'enabled',
                     'checkable': bool(v.api_base_url)})
        j = c.jev
        if j.url or j.enabled:
            rows.append({'id': 'judge:jev', 'group': 'Checks', 'name': 'Jev', 'type': 'HTTP', 'target': _host(j.url),
                         'detail': j.model, 'switch': 'Enabled', 'enabled': j.enabled, 'path': ('jev',),
                         'key': 'enabled', 'checkable': bool(j.url)})
        return rows

    def _target(self, endpoint):
        if endpoint.startswith('mcp:'):
            server = self.config.mcp_servers.get(endpoint[4:])
            if server is not None:
                return _host(server.url) if server.url else ' '.join(server.command)[:120]
        return _host(endpoint)

    def list(self):
        rows = []
        with self._lock:
            for row in self._rows():
                public = {k: v for k, v in row.items() if k not in ('path', 'key')}
                public['result'] = self._results.get(row['id'])
                public['pending_restart'] = row['id'] in self._pending
                rows.append(public)
        return {'integrations': rows, 'config_file': str(self.config_path)}

    def _row(self, item):
        for row in self._rows():
            if row['id'] == item:
                return row
        raise KeyError(item)

    # ---- checks ----------------------------------------------------------------
    def check(self, item):
        row = self._row(item)
        if not row['checkable']:
            raise ValueError('Nothing to check: no target configured')
        started = time.monotonic()
        try:
            ok, detail, tools = self._probe(row)
        except Exception as error:  # report what happened, never a stack or secret
            ok, detail, tools = False, type(error).__name__ + ': ' + str(error)[:160], None
        result = {'ok': bool(ok), 'detail': detail[:300], 'ms': int((time.monotonic() - started) * 1000),
                  'checked_at': self.clock()}
        if tools is not None:
            result['tools'] = tools[:200]
        with self._lock:
            self._results[item] = result
        return {**{k: v for k, v in row.items() if k not in ('path', 'key')}, 'result': result,
                'pending_restart': item in self._pending}

    def _probe(self, row):
        kind, name = row['id'].split(':', 1)
        c = self.config
        if kind == 'backend':
            b = c.backends[name]
            cli = (b.extra or {}).get('cli')
            if cli:
                command = cli.get('command') or ''
                if not shutil.which(command) and not os.access(command, os.X_OK):
                    return False, f'CLI not found: {command}', None
                done = subprocess.run([command, '--version'], capture_output=True, text=True, timeout=20,
                                      stdin=subprocess.DEVNULL)
                text = (done.stdout or done.stderr).strip().splitlines()
                return done.returncode == 0, (text[0] if text else f'exit {done.returncode}'), None
            headers = {}
            # A backend reaches its credential through credential_ref, not only
            # through an environment variable. Reading os.environ alone reported
            # every backend that uses a named credential as "HTTPError: HTTP Error
            # 401", which is what the page drew as Failed. That false failure is
            # what happens when a backend has a named credential and no
            # api_key_env: its grant is current and the provider answers, while
            # this probe sent no header at all.
            #
            # oauth_home matters: an OAuth reference resolves only when the
            # resolver is told where the grants live (backend.py does the same).
            key, why = '', ''
            if b.credential_ref or b.api_key_env:
                try:
                    key = CredentialResolver(c.credentials, oauth_home=str(c.home)).resolve(
                        b.credential_ref, env=b.api_key_env).value or ''
                except Exception as error:
                    why = str(error)[:160]
            if key:
                headers['Authorization'] = 'Bearer ' + key
            try:
                status, raw = _get(b.base_url.rstrip('/') + '/models', 10, headers)
            except urllib.error.HTTPError as error:
                # A 401 with no credential sent is not the endpoint's fault; say
                # which reference failed to resolve instead of echoing the 401.
                if error.code in (401, 403) and not key and why:
                    return False, f'credential unavailable: {why}', None
                if error.code in (401, 403):
                    # A rejected OAuth grant is repairable from the page; say what
                    # the saved sign-in looks like so the reader knows whether to
                    # refresh or sign in again. Never report the token itself.
                    note = ''
                    ref = c.credentials.get(b.credential_ref) if b.credential_ref else None
                    if ref is not None and ref.oauth:
                        try:
                            from .account_oauth import grant_report
                            note = '; saved sign-in: ' + grant_report(c.home, ref.oauth)['status']
                        except Exception:
                            note = ''
                    return False, f'HTTP {error.code}: the credential was rejected' + note, None
                return False, f'HTTP {error.code}: {error.reason}', None
            ids = [m.get('id') for m in json.loads(raw).get('data', []) if isinstance(m, dict)]
            listed = b.model in ids
            if listed:
                return True, f'model {b.model} listed', ids
            return False, f'model {b.model} not in {len(ids)} served', ids
        if kind == 'mcp':
            from .mcp_servers import resolve_endpoint_factory
            if name == 'aethermind':
                endpoints, token_env = c.aethermind.endpoints or [c.aethermind.url], c.aethermind.token_env
            elif name == 'ember':
                comp = c.companions.ember
                endpoints, token_env = list(comp.helmet_endpoints) + list(comp.companion_endpoints), comp.token_env
            else:
                endpoints, token_env = c.companions.atlas.endpoints, c.companions.atlas.token_env
            factory = resolve_endpoint_factory(c)
            found, errors, ok = [], [], True
            for endpoint in endpoints:
                try:
                    tools = factory(endpoint, token_env=token_env, timeout=10).list_tools()
                    names = sorted(t.get('name') for t in tools if isinstance(t, dict) and isinstance(t.get('name'), str))
                    found.append(f'{self._target(endpoint)}: {len(names)} tools')
                    errors.extend(names)
                except Exception as error:
                    ok = False
                    found.append(f'{self._target(endpoint)}: {type(error).__name__}')
            return ok and bool(endpoints), '; '.join(found) or 'no endpoint', errors
        if kind == 'server':
            from .mcp_servers import CATALOG, selected
            server = c.mcp_servers[name]
            CATALOG.forget(name)
            tools = CATALOG.client(server).list_tools()
            names = sorted(t['name'] for t in selected(server, tools))
            if not server.session_tools:
                return True, f'{len(tools)} tools, none added to sessions (companion only)', names
            return True, f'{len(tools)} tools, {len(names)} in sessions', names
        if kind == 'rdp':
            import socket
            r = c.remotes[name]
            with socket.create_connection((r.rdp_host or r.host, r.rdp_port), timeout=5):
                pass
            return True, f'TCP {r.rdp_port} open', None
        if kind == 'remote':
            from .remote import run_remote
            result = run_remote(c.remotes[name], c.home, 'uname -sn', 20, 2000)
            out = result['output'].strip()
            if result['timed_out']:
                return False, 'ssh timed out', None
            return result['exit_code'] == 0, out.splitlines()[-1] if out else f'exit {result["exit_code"]}', row.get('tools')
        if kind == 'monitoring' and name == 'prometheus':
            base = c.infrastructure.prometheus_url.rstrip('/')
            _, raw = _get(base + '/api/v1/status/buildinfo', 8)
            version = json.loads(raw)['data']['version']
            _, raw = _get(base + '/api/v1/targets?state=active', 8, limit=8388608)
            targets = json.loads(raw)['data']['activeTargets']
            up = sum(1 for t in targets if t.get('health') == 'up')
            return True, f'v{version}, {up}/{len(targets)} targets up', None
        if kind == 'monitoring' and name == 'grafana':
            base = c.grafana.base_url.rstrip('/')
            _, raw = _get(base + '/api/health', 8)
            health = json.loads(raw)
            try:
                _get(base + c.grafana.datasources_path, 8)
                access = 'API readable'
            except urllib.error.HTTPError as error:
                access = f'API needs auth ({error.code})'
            return health.get('database') == 'ok', f'v{health.get("version")}, {access}', None
        if kind == 'voice':
            from .voice import VoiceClient, VoiceError
            try:
                minted = VoiceClient(c.voice, credentials=CredentialResolver(c.credentials)).mint()
            except VoiceError as error:
                return False, 'token mint failed: ' + error.category, None
            return True, 'session token minted (expires in %ds)' % int(minted['expires_at'] - self.clock()), None
        if kind == 'judge':
            # Jev's endpoint takes inference as POST, so a bare GET answers 405
            # Method Not Allowed and urllib raises on it. The old one-liner therefore
            # reported every healthy judge as "HTTPError: HTTP Error 405" and the page
            # drew it as Failed. GET answers 405, POST with no key answers 403, POST
            # with a bad key answers 401, and POST with the configured key and an empty
            # body answers 422 (model and questions are required) -- all while the judge
            # is answering tool gates in 191ms.
            #
            # Authentication is checked BEFORE request validation on this endpoint,
            # so an empty-body POST is a real credential test that costs no inference:
            #   422 -> authenticated and reachable; the judge is healthy
            #   401/403 -> the credential is the problem; that IS a failure
            #   405 -> reachable but this deployment refuses POST; report the method,
            #           do not call it broken
            key = ''
            try:
                key = CredentialResolver(c.credentials).resolve(
                    c.jev.credential_ref, env=c.jev.api_key_env).value or ''
            except Exception:
                key = ''  # an unresolvable credential is reported by the status below
            body = b'{}'
            request = urllib.request.Request(c.jev.url, data=body, method='POST',
                                             headers={'Content-Type': 'application/json',
                                                      **({'Authorization': 'Bearer ' + key} if key else {})})
            try:
                status, _ = urllib.request.urlopen(request, timeout=8).status, None
            except urllib.error.HTTPError as error:
                status = error.code
            except Exception as error:
                return False, type(error).__name__ + ': ' + str(error)[:120], None
            if status == 422:
                return True, 'authenticated; endpoint answered 422 (valid body required, no inference run)', None
            if status in (401, 403):
                return False, f'HTTP {status}: the credential was rejected', None
            if status == 405:
                return True, 'reachable (this deployment refuses POST; cannot test the credential)', None
            return status < 400, f'HTTP {status}', None
        raise ValueError('Unknown integration')

    # ---- switches --------------------------------------------------------------
    def set_enabled(self, item, enabled):
        if not isinstance(enabled, bool):
            raise ValueError('enabled must be true or false')
        row = self._row(item)
        if item == 'mcp:aethermind':
            raise ValueError('AetherMind has no off state; choose embedded or external in Settings')
        if row['main'] if 'main' in row else False:
            if not enabled:
                raise ValueError('roles.main uses this backend; it must stay a chat backend')
        from .config_editor import ConfigEditor
        editor = getattr(self.manager, 'config_editor', None)
        if editor is None or Path(editor.config_path) != Path(self.config_path):
            editor = ConfigEditor(self.config_path)
        reply = editor.apply(
            [{'path': [*row['path'], row['key']], 'value': bool(enabled)}],
            label='integration-' + item)
        if reply.get('changed'):
            target = self.config
            for part in row['path']:
                target = target.get(part) if isinstance(target, dict) else getattr(target, part)
            setattr(target, row['key'], enabled)
            with self._lock:
                self._pending.add(item)
                self._results.pop(item, None)
        return {'id': item, 'enabled': enabled, 'backup': reply.get('backup'),
                'pending_restart': bool(reply.get('changed'))}


def set_toml_bool(text, path, key, value):
    from .config_editor import set_toml_value
    return set_toml_value(text, path, key, bool(value))
