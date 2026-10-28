"""Numeric provider metadata only; never retain arbitrary headers or usage fields."""

from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import math
import re
import time


def number(value):
    if isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return result if math.isfinite(result) and result >= 0 else None


def reset_at(value, now):
    if not isinstance(value, str):
        return None
    value = value.strip()
    seconds = number(value)
    if seconds is not None:
        return now + seconds
    parts = re.findall(r'(\d+(?:\.\d+)?)(ms|s|m|h|d)', value)
    if parts and ''.join(amount + unit for amount, unit in parts) == value:
        seconds = 0
        for amount, unit in parts:
            seconds += float(amount) * {'ms': .001, 's': 1, 'm': 60, 'h': 3600, 'd': 86400}[unit]
        return number(now + seconds)
    try:
        stamp = datetime.fromisoformat(value.replace('Z', '+00:00'))
        if stamp.tzinfo is None:
            stamp = parsedate_to_datetime(value)
    except ValueError:
        try:
            stamp = parsedate_to_datetime(value)
        except (ValueError, TypeError, OverflowError):
            return None
    if stamp.tzinfo is None:
        return None
    return number(stamp.timestamp())


def parse_rate_limits(headers, *, now=None):
    now = time.time() if now is None else now
    headers = {key.lower(): value for key, value in (headers or {}).items()}
    result = {}
    for prefix in ('x-ratelimit', 'anthropic-ratelimit'):
        for resource in ('requests', 'tokens', 'input-tokens', 'output-tokens'):
            name = resource.replace('-', '_')
            if prefix == 'x-ratelimit':
                remaining = headers.get(f'{prefix}-remaining-{resource}')
                reset = headers.get(f'{prefix}-reset-{resource}')
            else:
                remaining = headers.get(f'{prefix}-{resource}-remaining')
                reset = headers.get(f'{prefix}-{resource}-reset')
            count = number(remaining)
            deadline = reset_at(reset, now)
            if count is not None:
                result[name + '_remaining'] = count
            if deadline is not None:
                result[name + '_reset_at'] = deadline
    retry = reset_at(headers.get('retry-after'), now)
    if retry is not None:
        result['retry_at'] = retry
    return result


def usage_totals(usage):
    """Missing counters stay missing, so admission can conservatively charge bounds."""
    usage = usage if isinstance(usage, dict) else {}
    result = {}
    total = number(usage.get('total_tokens', usage.get('tokens')))
    if total is None:
        components = [('input_tokens', 'prompt_tokens'), ('output_tokens', 'completion_tokens'),
                      ('cache_creation_input_tokens',), ('cache_read_input_tokens',)]
        values = []
        for aliases in components:
            for name in aliases:
                value = number(usage.get(name))
                if value is not None:
                    values.append(value)
                    break
        if values:
            total = sum(values)
    if total is not None:
        result['tokens'] = total
    for name in ('money', 'seconds'):
        value = number(usage.get(name))
        if value is not None:
            result[name] = value
    return result


def day_key(stamp):
    return datetime.fromtimestamp(stamp, timezone.utc).date().isoformat()
