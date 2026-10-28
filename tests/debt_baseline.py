"""Exact debt accounting: additions and stale entries both fail the gate."""

import sys


def _applicable(reason, platform):
    """A ``platform=<sys.platform>`` prefix keeps debt that exists on one OS.

    ``computer_use.provider`` is a real duplicate on macOS and a live choice on
    Linux. One baseline file has to say both without failing the other machine.
    """
    text = reason.strip()
    if not text.startswith('platform='):
        return True, text
    tag, separator, rest = text.partition(' ')
    name = tag.split('=', 1)[1]
    if not separator or not rest.strip() or not name:
        raise AssertionError('platform tag needs a platform name and a reason')
    return name == platform, rest.strip()


def assert_matches_baseline(current, path, *, platform=None):
    current = set(current)
    platform = sys.platform if platform is None else platform
    baseline = set()
    for line in path.read_text(encoding='utf-8').splitlines():
        item, _, reason = line.partition('#')
        item = item.strip()
        if not item:
            continue
        applies, reason = _applicable(reason, platform)
        assert reason, f'{path.name}: {item} needs a reason'
        if not applies:
            continue
        assert item not in baseline, f'{path.name}: duplicate entry {item}'
        baseline.add(item)

    messages = [f'outstanding: {len(current)} (was {len(baseline)})']
    new = current - baseline
    fixed = baseline - current
    if new:
        messages.append('NEW: ' + ', '.join(sorted(new)))
    if fixed:
        messages.append('FIXED: ' + ', '.join(sorted(fixed)) + '\nRemove these lines from ' + path.name)
    output = '\n'.join(messages)
    print(path.name + ': ' + output)
    assert current == baseline, output
