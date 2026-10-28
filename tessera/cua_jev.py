"""Jev questions over a native accessibility tree. Text in, typed answers out."""

import json
import math
import os
import re
import urllib.error
import urllib.request

from .credentials import CredentialError, CredentialResolver

# index, role, subrole, name, value, x,y,width,height, actions.
# Name and value may contain spaces. Role stays the second token.
_LINE = re.compile(r'^(\d+)\s+(\S+)\s+(\S+)\s+(.*?)\s+(-?\d+,-?\d+,-?\d+,-?\d+)\s+(\S+)\s*$')
_WORDS = re.compile(r'[A-Za-z0-9]+')
_ACTIONABLE = frozenset({
    'AXButton', 'AXTextField', 'AXTextArea', 'AXCheckBox', 'AXRadioButton',
    'AXPopUpButton', 'AXMenuItem', 'AXMenuBarItem', 'AXLink', 'AXSlider',
    'AXComboBox', 'AXSearchField', 'AXIncrementor', 'AXToggle',
})
_CHOICE_LIMIT = 255


def missing_key_message(settings):
    if getattr(settings, 'credential_ref', ''):
        return (f'computer_use.jev is enabled but credential {settings.credential_ref} is unavailable '
                f'and {settings.api_key_env} is not set')
    return f'computer_use.jev is enabled but {settings.api_key_env} is not set'


def key_available(settings):
    if getattr(settings, 'credential_ref', ''):
        return True
    value = os.environ.get(settings.api_key_env, '')
    return isinstance(value, str) and bool(value.strip())


def api_key(settings, credentials=None):
    resolver = credentials if isinstance(credentials, CredentialResolver) else CredentialResolver(credentials)
    try:
        resolved = resolver.resolve(getattr(settings, 'credential_ref', '') or '', env=settings.api_key_env)
    except CredentialError:
        raise ValueError(missing_key_message(settings)) from None
    if not resolved.value:
        raise ValueError(missing_key_message(settings))
    return resolved.value


def redact(text, key):
    if key and key in text:
        return text.replace(key, '[REDACTED]')
    return text


def parse_tree(text):
    rows = []
    for raw in str(text).splitlines():
        line = raw.strip()
        if not line or line == 'truncated':
            continue
        match = _LINE.match(line)
        if not match:
            continue
        index, role, subrole, _middle, _box, actions = match.groups()
        rows.append({'index': index, 'role': role, 'subrole': subrole, 'actions': actions, 'line': line})
    return rows


def _overlap(line, words):
    hay = line.casefold()
    return sum(1 for word in words if word in hay)


def select_candidates(text, description, role, max_chars):
    rows = parse_tree(text)
    if not rows:
        raise ValueError('no indexed accessibility elements')
    hint = (role or '').strip()
    if hint:
        chosen = [row for row in rows if hint.casefold() in row['role'].casefold()]
        if not chosen:
            raise ValueError(f'no elements with role {hint}')
    else:
        chosen = [row for row in rows if row['actions'] != '-' or row['role'] in _ACTIONABLE]
        if not chosen:
            chosen = list(rows)
    words = {word.casefold() for word in _WORDS.findall(description) if len(word) > 1}
    chosen.sort(key=lambda row: (-_overlap(row['line'], words), int(row['index'])))
    trimmed = len(chosen) > _CHOICE_LIMIT
    chosen = chosen[:_CHOICE_LIMIT]
    kept = []
    used = 0
    for row in chosen:
        line = row['line']
        if not kept and len(line) > max_chars:
            kept.append({**row, 'line': line[:max_chars]})
            trimmed = True
            break
        extra = len(line) + (1 if kept else 0)
        if used + extra > max_chars:
            trimmed = True
            break
        kept.append(row)
        used += extra
    kept.sort(key=lambda row: int(row['index']))
    return kept, trimmed


def tree_state(text, max_chars):
    rows = parse_tree(text)
    if not rows:
        raise ValueError('no indexed accessibility elements')
    kept = []
    used = 0
    trimmed = False
    for row in rows:
        extra = len(row['line']) + (1 if kept else 0)
        if kept and used + extra > max_chars:
            trimmed = True
            break
        if not kept and len(row['line']) > max_chars:
            kept.append({**row, 'line': row['line'][:max_chars]})
            return kept, True
        kept.append(row)
        used += extra
    return kept, trimmed


def endpoint(base_url):
    root = str(base_url or '').strip().rstrip('/')
    if not root:
        raise ValueError(
            'configuration error: computer_use.jev.base_url must be set when computer_use.jev is enabled')
    if root.endswith('/v1/systemone'):
        return root
    return root + '/v1/systemone'


def evaluate(settings, key, state, questions):
    from .config import computer_use_jev_configuration_error
    message = computer_use_jev_configuration_error(settings)
    if message:
        raise ValueError(message)
    body = json.dumps(
        {'state': state, 'model': settings.model, 'questions': questions},
        ensure_ascii=False, allow_nan=False,
    ).encode('utf-8')
    headers = {'Content-Type': 'application/json', 'Authorization': 'Bearer ' + key}
    if settings.long_context:
        headers['TypeSafe-Danger-Zone-Long-Context'] = 'true'
    request = urllib.request.Request(endpoint(settings.base_url), data=body, method='POST', headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=settings.timeout) as response:
            raw = response.read(1024 * 1024 + 1)
    except urllib.error.HTTPError as error:
        error.close()
        raise ValueError(f'Jev request failed (HTTP {error.code})') from None
    except (urllib.error.URLError, TimeoutError, OSError):
        raise ValueError('Jev request failed') from None
    if len(raw) > 1024 * 1024:
        raise ValueError('Jev response is too large')
    try:
        payload = json.loads(raw)
    except ValueError:
        raise ValueError('Jev response was not JSON') from None
    answers = payload.get('answers') if isinstance(payload, dict) else None
    if not isinstance(answers, dict):
        raise ValueError('Jev response has no answers')
    return answers


def _probability(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError('Jev returned a non-numeric probability')
    if not math.isfinite(value) or not 0 <= value <= 1:
        raise ValueError('Jev returned a probability outside 0 to 1')
    return float(value)


def _choice_confidence(probabilities, reported):
    if reported is not None:
        return _probability(reported)
    values = list(probabilities.values())
    count = len(values)
    if count < 2:
        return 1.0
    peak = max(values)
    return (peak - (1 / count)) / (1 - (1 / count))


def find_in_tree(text, description, settings, key, role=None):
    wanted = str(description or '').strip()
    if not wanted:
        raise ValueError('find_element needs a description')
    candidates, trimmed = select_candidates(text, wanted, role, settings.max_state_chars)
    by_index = {row['index']: row for row in candidates}
    questions = {'element': {
        'type': 'choice',
        'instructions': (
            'Which indexed accessibility element matches this description: '
            f'{wanted}? Each line of the state starts with the element index. Choose that index.'
        ),
        'criteria': {row['index']: None for row in candidates},
    }}
    answer = evaluate(settings, key, '\n'.join(row['line'] for row in candidates), questions).get('element')
    if not isinstance(answer, dict) or answer.get('type') != 'choice':
        raise ValueError('Jev returned no choice')
    choice = answer.get('choice')
    if not isinstance(choice, str) or choice not in by_index:
        raise ValueError('Jev returned an unknown element index')
    probabilities = answer.get('probabilities')
    if not isinstance(probabilities, dict) or set(probabilities) != set(by_index):
        raise ValueError('Jev choice probabilities do not match the elements')
    parsed = {name: _probability(value) for name, value in probabilities.items()}
    total = sum(parsed.values())
    if abs(total - 1) > 0.02:
        raise ValueError('Jev choice probabilities do not sum to one')
    confidence = _choice_confidence(parsed, answer.get('confidence'))
    others = sorted((name for name in parsed if name != choice), key=lambda name: (-parsed[name], int(name)))
    runner = by_index[others[0]] if others else None
    return {
        'unsure': confidence < settings.min_confidence,
        'index': choice,
        'line': by_index[choice]['line'],
        'probability': parsed[choice],
        'confidence': confidence,
        'runner_index': None if runner is None else runner['index'],
        'runner_line': None if runner is None else runner['line'],
        'runner_probability': None if runner is None else parsed[runner['index']],
        'trimmed': trimmed,
    }


def check_tree(text, question, settings, key):
    asked = str(question or '').strip()
    if not asked:
        raise ValueError('check needs a question')
    rows, trimmed = tree_state(text, settings.max_state_chars)
    questions = {'landed': {
        'type': 'noul',
        'instructions': asked,
        'criteria': {
            'true': 'Yes. The accessibility tree shows this.',
            'false': 'No. The accessibility tree does not show this.',
        },
    }}
    answer = evaluate(settings, key, '\n'.join(row['line'] for row in rows), questions).get('landed')
    if not isinstance(answer, dict) or answer.get('type') != 'noul':
        raise ValueError('Jev returned no yes/no answer')
    probability = _probability(answer.get('noul'))
    return {'answer': 'yes' if probability >= 0.5 else 'no', 'probability': probability, 'trimmed': trimmed}


def conscience(pressure, tool, arguments, settings, key):
    """Cloud Jev on one interpretive question. Unsure or unreachable is not a block."""
    asked = str(pressure or '').strip()
    if not asked or (not asked.startswith('Pressure:') and not asked.startswith('Salience')):
        return {'verdict': 'abstain', 'probability': None, 'reason': 'no pressure line'}
    shown = json.dumps(arguments, ensure_ascii=False)
    if len(shown) > 600:
        shown = shown[:600]
    state = f'{asked}\nProposed tool: {tool}\nArguments: {shown}'
    questions = {'answers_pressure': {
        'type': 'noul',
        'instructions': 'Does this tool call answer the pressure line, without continuing a previous wake and without costing the operator something they did not ask for?',
        'criteria': {
            'true': 'Yes. It answers the named pressure and does not spend his attention on something else.',
            'false': 'No. It continues a previous wake, invents work, or costs him something he did not ask for.',
        },
    }}
    try:
        answer = evaluate(settings, key, state, questions).get('answers_pressure')
    except ValueError as error:
        return {'verdict': 'abstain', 'probability': None, 'reason': str(error)}
    if not isinstance(answer, dict) or answer.get('type') != 'noul':
        return {'verdict': 'abstain', 'probability': None, 'reason': 'no yes/no answer'}
    try:
        probability = _probability(answer.get('noul'))
    except ValueError as error:
        return {'verdict': 'abstain', 'probability': None, 'reason': str(error)}
    if probability >= 0.85:
        return {'verdict': 'yes', 'probability': probability, 'reason': ''}
    if probability <= 0.15:
        return {'verdict': 'no', 'probability': probability, 'reason': 'cloud Jev says this call does not answer the pressure'}
    return {'verdict': 'abstain', 'probability': probability, 'reason': 'unsure'}


def format_find(result):
    lines = ['not sure'] if result['unsure'] else []
    lines.extend([
        f"index {result['index']}",
        result['line'],
        f"probability {result['probability']:.3f}",
        f"confidence {result['confidence']:.3f}",
    ])
    if result['runner_index'] is None:
        lines.append('runner-up none')
    else:
        lines.extend([
            f"runner-up {result['runner_index']}",
            result['runner_line'],
            f"probability {result['runner_probability']:.3f}",
        ])
    if result['trimmed']:
        lines.append('trimmed')
    return '\n'.join(lines)


def format_check(result):
    lines = [result['answer'], f"probability {result['probability']:.3f}"]
    if result['trimmed']:
        lines.append('trimmed')
    return '\n'.join(lines)
