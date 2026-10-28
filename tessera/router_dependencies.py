"""Read router decisions on its owning host; emit only serving metadata."""

import argparse
import json
import shlex
import subprocess


REMOTE_READER = '''
import json, pathlib, sys, urllib.request, urllib.parse, socket, subprocess
from datetime import datetime, timezone
origin, state_dir, client_id, dash_origin, dash_env = sys.argv[1:]
key = (pathlib.Path(state_dir) / 'operator-bootstrap.key').read_text().strip()
headers = {'Authorization': 'Bearer ' + key, 'Content-Type': 'application/json'}
def request(path, body=None):
    data = None if body is None else json.dumps(body).encode()
    # The router's own state and explain calls occasionally answer in over 3s
    # under load, and a bare timeout here kills the whole probe with no output,
    # which the caller sees only as
    # 'Router dependency read failed', so every rank comes back 'unknown' and a
    # session is told its serving ownership is unresolved. One bounded retry per
    # call, and the failing call is named rather than dying silently.
    last = None
    for attempt in range(2):
        try:
            req = urllib.request.Request(origin + path, data=data, headers=headers)
            with urllib.request.urlopen(req, timeout=5) as response:
                return json.load(response)
        except Exception as error:
            last = error
    raise RuntimeError('router call %s failed: %s: %s' % (path, type(last).__name__, last))
state = request('/api/v1/state')
engines = {engine['id']: engine for engine in state['engines']}
inventory = []
dash_token = ''
if dash_origin and dash_env:
    try:
        for line in pathlib.Path(dash_env).read_text().splitlines():
            if line.startswith('SPARKDASH_TOKEN='):
                dash_token = line.split('=', 1)[1].strip().strip(chr(34) + chr(39))
        def dash_get(path):
            req = urllib.request.Request(dash_origin + path, headers={'Authorization': 'Bearer ' + dash_token})
            with urllib.request.urlopen(req, timeout=3) as response:
                return json.load(response)
        inventory = dash_get('/api/sparks').get('sparks', [])
    except Exception:
        inventory = []
# Every address a Spark answers to. The Sparks are reachable by LAN IP,
# Tailscale IP, DNS name and MAC; a session that restarts one over SSH may
# name it by any of those, so the judge must see them all rather than one.
tailscale_by_host = {}
tailscale_error = ''
try:
    reported = subprocess.run(['tailscale', 'status', '--json'],
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=5)
    if reported.returncode != 0:
        err = reported.stderr.decode('utf-8', 'replace').strip().splitlines()
        tailscale_error = 'tailscale status exited %s' % reported.returncode
        if err:
            tailscale_error += ': ' + err[-1]
    else:
        status = json.loads(reported.stdout)
        entries = list((status.get('Peer') or {}).values()) + [status.get('Self') or {}]
        for peer in entries:
            host = (peer.get('HostName') or '').lower()
            if not host:
                continue
            names = tailscale_by_host.setdefault(host, [])
            names.extend(ip for ip in (peer.get('TailscaleIPs') or []) if ':' not in ip)
            dns = peer.get('DNSName') or ''
            if dns:
                names.append(dns.rstrip('.'))
except Exception as error:
    tailscale_error = '%s: %s' % (type(error).__name__, error)
def identifiers(unit):
    seen = []
    def add(value):
        if isinstance(value, str) and value and value not in seen:
            seen.append(value)
    add(unit.get('id'))
    add(unit.get('name'))
    add(unit.get('lanIp'))
    add((unit.get('ssh') or {}).get('host'))
    add(unit.get('detectedMacAddress'))
    for alias in tailscale_by_host.get((unit.get('name') or '').lower(), []):
        add(alias)
    return seen
def topology(engine, model):
    target = urllib.parse.urlsplit(engine.get('base_url') or '')
    for head in inventory:
        if target.hostname not in identifiers(head):
            continue
        try:
            report = dash_get('/api/sparks/' + urllib.parse.quote(head['id'], safe='') + '/metrics')
            endpoints = report.get('metrics', {}).get('llm') or []
            matches = [item for item in endpoints if item.get('port') == target.port
                       and item.get('available') and item.get('modelId') == model]
            if not matches:
                return {'rank_resolution': 'unknown'}
            ranks = [head] + [unit for unit in inventory if unit.get('workerHeadId') == head['id']]
            return {'rank_resolution': 'inventory_and_live_model',
                    'ownership_source': 'SparkDash workerHeadId and live endpoint modelId',
                    'ranks': [{'id': unit['id'], 'name': unit.get('name'), 'host': unit.get('lanIp'),
                               'role': unit.get('role'), 'identifiers': identifiers(unit)}
                              for unit in ranks]}
        except Exception:
            return {'rank_resolution': 'unknown'}
    return {'rank_resolution': 'unknown'}
routes = {}
for route in state['config']['routes']:
    if not route.get('enabled'):
        continue
    decision = request('/api/v1/explain', {'client_id': client_id, 'payload': {'model': route['name']}})
    candidates = []
    for candidate in decision.get('candidates', []):
        engine = engines.get(candidate['engine_id'], {})
        item = {'engine_id': candidate['engine_id'], 'engine': candidate.get('engine'),
                           'base_url': engine.get('base_url'), 'model': candidate.get('model'),
                           'tier': candidate.get('tier'), 'status': engine.get('status'),
                           'endpoint_host_namespace': socket.gethostname()}
        item.update(topology(engine, candidate.get('model')))
        candidates.append(item)
    routes[route['name']] = {'revision': decision.get('revision'), 'candidates': candidates}
payload = {'observed_at': datetime.now(timezone.utc).isoformat(), 'router_host': socket.gethostname(), 'routes': routes}
if tailscale_error:
    payload['tailscale_error'] = tailscale_error
print(json.dumps(payload))
'''


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--ssh', required=True)
    parser.add_argument('--origin', required=True)
    parser.add_argument('--state-dir', required=True)
    parser.add_argument('--client-id', required=True)
    parser.add_argument('--sparkdash-origin', default='')
    parser.add_argument('--sparkdash-env', default='')
    args = parser.parse_args()
    remote = shlex.join(['python3', '-', args.origin.rstrip('/'), args.state_dir, args.client_id,
                         args.sparkdash_origin.rstrip('/'), args.sparkdash_env])
    # This probe takes 4.7-5.0s end to end (three router calls, two SparkDash
    # calls, a tailscale read), and a 7s cap failed 2 of 5 runs. A failed probe
    # is not a slow judge:
    # it returns rank_resolution 'unknown', which makes every Spark look
    # unresolved and pushes the session at the ownership branch. The cap has to
    # sit above the measured cost with room for a loaded host, so 20s, and the
    # per-call timeouts stay short so a genuinely absent host still fails fast.
    result = subprocess.run(['ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=3', args.ssh, remote],
                            input=REMOTE_READER, text=True, capture_output=True, timeout=20)
    if result.returncode:
        # The remote reader's own message, not a bare 'read failed'. A silent
        # failure here is indistinguishable from a slow judge, and it pushes
        # every rank to 'unknown', which is the ownership branch.
        detail = (result.stderr or '').strip().splitlines()
        raise SystemExit('Router dependency read failed: %s' % (detail[-1] if detail else 'no output'))
    snapshot = json.loads(result.stdout)
    print(json.dumps(snapshot))


if __name__ == '__main__':
    main()
