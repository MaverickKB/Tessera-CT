"""Opt-in read-only Grafana HTTP client. Passive status never resolves a token."""
import json
import math
import re
import threading
import urllib.error
import urllib.request
from urllib.parse import quote, urlencode, urlsplit

from .credentials import CredentialError, CredentialResolver, redact_secret

ERRORS = {'configuration', 'credential', 'timeout', 'access', 'http', 'unavailable', 'invalid_response'}


class GrafanaError(ValueError):
    def __init__(self, category):
        self.category = category if category in ERRORS else 'unavailable'
        super().__init__('Grafana operation failed: ' + self.category)


def http_url(value):
    try:
        parsed = urlsplit(value)
        if (parsed.scheme not in ('http', 'https') or not parsed.hostname or parsed.username is not None
                or parsed.password is not None or parsed.query or parsed.fragment or parsed.port == 0
                or any(c.isspace() for c in value)):
            raise ValueError()
    except (ValueError, TypeError):
        raise ValueError('HTTP endpoint must be a credential-free HTTP(S) base URL') from None
    return value.rstrip('/')


def safe_path(value, *, absolute=False):
    if (not isinstance(value, str) or not value or any(c.isspace() for c in value)
            or any(c in value for c in ('?', '#', '%', '\\', '\0'))):
        raise ValueError('Invalid configured API path')
    if value.startswith('/') != absolute:
        raise ValueError('Invalid configured API path')
    parts = value.lstrip('/').split('/')
    if any(part in ('', '.', '..') for part in parts):
        raise ValueError('Invalid configured API path')
    return value


def validate_settings(settings, credentials):
    if settings.base_url:
        http_url(settings.base_url)
    if settings.timeout <= 0 or settings.max_response_bytes <= 0 or settings.list_limit <= 0:
        raise ValueError('Invalid Grafana timeout or bounds')
    if settings.credential_ref and settings.credential_ref not in credentials:
        raise ValueError('Grafana credential_ref is not configured')
    for field in ('datasources_path', 'dashboards_path', 'proxy_path', 'embed_path'):
        route = getattr(settings, field)
        safe_path(route, absolute=True)
        expected = {'proxy_path': {'uid', 'path'}, 'embed_path': {'uid', 'slug'}}.get(field, set())
        fields = set(re.findall(r'\{([^{}]+)\}', route))
        if fields != expected:
            raise ValueError('Invalid configured Grafana route template')
        test = route
        for key in expected:
            test = test.replace('{' + key + '}', 'placeholder')
        if '{' in test or '}' in test:
            raise ValueError('Invalid configured Grafana route template')
    if not isinstance(settings.allowed_proxy_paths, list):
        raise ValueError('Grafana proxy paths must be an explicit read-only allowlist')
    for path in settings.allowed_proxy_paths:
        safe_path(path)


def segment(value):
    if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9_-]+', value):
        raise GrafanaError('configuration')
    return quote(value, safe='')


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, message, headers, newurl):
        return None


class GrafanaClient:
    def __init__(self, settings, *, credentials=None, opener=None):
        self.settings = settings
        self.credentials = credentials if credentials is not None else CredentialResolver()
        self.opener = opener
        self._lock = threading.RLock()
        self._observed = {'health': 'unknown'}

    def status(self):
        with self._lock:
            return {'enabled': self.settings.enabled,
                    'configured': bool(self.settings.base_url and (self.settings.token_env or self.settings.credential_ref)),
                    **self._observed}

    def _require(self):
        if not self.settings.enabled or not self.status()['configured']:
            raise GrafanaError('configuration')

    def _error(self, category):
        with self._lock:
            self._observed = {'health': 'unavailable', 'error_category': category}
        raise GrafanaError(category) from None

    def _get(self, path, params=None):
        self._require()
        try:
            safe_path(path, absolute=True)
            base = http_url(self.settings.base_url)
            token = self.credentials.resolve(self.settings.credential_ref, env=self.settings.token_env).value
            if not token:
                raise CredentialError('Missing token')
            suffix = '?' + urlencode(params) if params else ''
            request = urllib.request.Request(base + path + suffix, headers={
                'Accept': 'application/json', 'Authorization': 'Bearer ' + token}, method='GET')
            send = self.opener
            if send is None:
                send = urllib.request.build_opener(_NoRedirect()).open
            with send(request, timeout=self.settings.timeout) as response:
                raw = response.read(self.settings.max_response_bytes + 1)
            if len(raw) > self.settings.max_response_bytes:
                self._error('invalid_response')
            data = json.loads(raw, parse_constant=lambda value: self._invalid_constant())
            if not isinstance(data, (list, dict)):
                self._error('invalid_response')
        except CredentialError:
            self._error('credential')
        except urllib.error.HTTPError as error:
            category = 'access' if error.code in (401, 403) else 'http'
            error.close()
            self._error(category)
        except (TimeoutError,):
            self._error('timeout')
        except urllib.error.URLError as error:
            self._error('timeout' if isinstance(error.reason, TimeoutError) else 'unavailable')
        except (OSError,):
            self._error('unavailable')
        except GrafanaError:
            raise
        except (ValueError, UnicodeError, TypeError):
            self._error('invalid_response')
        except Exception:
            self._error('unavailable')
        with self._lock:
            self._observed['health'] = 'healthy'
            self._observed.pop('error_category', None)
        return redact_secret(data, token)

    @staticmethod
    def _invalid_constant():
        raise ValueError('Nonfinite JSON')

    def list_datasources(self):
        rows = self._get(self.settings.datasources_path)
        if not isinstance(rows, list):
            self._error('invalid_response')
        # list_limit is a bound, not a filter. The datasources route returns the whole
        # set, so a cut here drops real rows and a reader that only sees the count would
        # read it as the total.
        omitted = max(0, len(rows) - self.settings.list_limit)
        result = []
        for row in rows[:self.settings.list_limit]:
            if not isinstance(row, dict) or not all(isinstance(row.get(k), str) for k in ('uid', 'name', 'type')):
                self._error('invalid_response')
            result.append({key: row[key] for key in ('uid', 'name', 'type')})
        with self._lock:
            self._observed['datasource_count'] = len(result)
            if omitted:
                self._observed['datasource_omitted'] = omitted
            else:
                self._observed.pop('datasource_omitted', None)
        return result

    def list_dashboards(self, *, query='', page=1):
        if not isinstance(query, str) or type(page) is not int or page <= 0:
            raise GrafanaError('configuration')
        rows = self._get(self.settings.dashboards_path, {'type': 'dash-db', 'query': query,
                                                       'limit': self.settings.list_limit, 'page': page})
        if not isinstance(rows, list):
            self._error('invalid_response')
        result = []
        for row in rows[:self.settings.list_limit]:
            if not isinstance(row, dict) or not all(isinstance(row.get(k), str) for k in ('uid', 'title')):
                self._error('invalid_response')
            if row.get('type', 'dash-db') == 'dash-db':
                result.append({'uid': row['uid'], 'title': row['title']})
        with self._lock:
            self._observed['dashboard_count'] = len(result)
        return result

    def query_proxy(self, uid, path, params):
        self._require()
        uid = segment(uid)
        try:
            safe_path(path)
        except ValueError:
            raise GrafanaError('configuration') from None
        if path not in self.settings.allowed_proxy_paths or not isinstance(params, dict):
            raise GrafanaError('configuration')
        for key, value in params.items():
            if (not isinstance(key, str) or not key or key.lower() in
                    ('token', 'access_token', 'api_key', 'authorization', 'password')):
                raise GrafanaError('configuration')
            if type(value) not in (str, int, float, bool) or (type(value) is float and not math.isfinite(value)):
                raise GrafanaError('configuration')
        route = self.settings.proxy_path.format(uid=uid, path=path)
        return self._get(route, params)

    def embed_url(self, uid, slug, *, panel_id, from_time='', to_time='', theme='', org_id=None):
        self._require()
        route = self.settings.embed_path.format(uid=segment(uid), slug=segment(slug))
        if type(panel_id) is int:
            if panel_id <= 0:
                raise GrafanaError('configuration')
        elif not isinstance(panel_id, str) or not re.fullmatch(r'[A-Za-z0-9_-]+', panel_id):
            raise GrafanaError('configuration')
        params = {'panelId': panel_id}
        for key, value in (('from', from_time), ('to', to_time), ('theme', theme)):
            if not isinstance(value, str):
                raise GrafanaError('configuration')
            if value:
                params[key] = value
        if org_id is not None:
            if type(org_id) is not int or org_id <= 0:
                raise GrafanaError('configuration')
            params['orgId'] = org_id
        return http_url(self.settings.base_url) + route + '?' + urlencode(params)
