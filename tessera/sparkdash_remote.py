"""Executed on SparkDash's host over SSH stdin. Only GET telemetry leaves the host."""
import argparse
import json
from pathlib import Path
import time
import urllib.parse
import urllib.request


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--env-file', required=True)
    parser.add_argument('--url', required=True)
    parser.add_argument('range', choices=['1h', '6h', '24h', '7d', '30d'])
    args = parser.parse_args()
    token = ''
    for line in Path(args.env_file).read_text().splitlines():
        if line.startswith('SPARKDASH_TOKEN='):
            token = line.split('=', 1)[1].strip().strip('\"\x27')
    opener = urllib.request.build_opener()

    def get(path):
        request = urllib.request.Request(args.url.rstrip('/') + path,
                                        headers={'Authorization': 'Bearer ' + token})
        with opener.open(request, timeout=6) as response:
            raw = response.read(2 * 1024 * 1024 + 1)
        if len(raw) > 2 * 1024 * 1024:
            raise ValueError('Response too large')
        return json.loads(raw)

    result = {'sampled_at': time.time(), 'sparks': []}
    inventory = get('/api/sparks')
    for unit in inventory.get('sparks', []):
        if unit.get('kind') != 'spark':
            continue
        path = '/api/sparks/' + urllib.parse.quote(unit['id'], safe='')
        try:
            report = get(path + '/metrics')
            metrics = report.get('metrics', {})
            selected = {}
            for key in ['gpu', 'cpu', 'ram', 'storage', 'network', 'roce', 'unifiedMemory', 'llm']:
                selected[key] = metrics.get(key)
            row = {'id': report['id'], 'name': report['name'], 'online': report.get('online'),
                   'role': report.get('role'), 'metrics': selected}
            row['history'] = get(path + '/history?range=' + args.range)
            row['gpu_history'] = get(path + '/gpu-history?windowMs=3600000')
            result['sparks'].append(row)
        except Exception:
            result['sparks'].append({'id': unit['id'], 'name': unit['name'], 'error': 'Telemetry unavailable'})
    try:
        result['tokens'] = get('/api/llm-token-totals/history')
    except Exception:
        result['tokens'] = {'error': 'Token history unavailable'}
    print(json.dumps(result))


if __name__ == '__main__':
    main()
