"""The core's own output file must not grow without a bound.

Measured on a live home: core.out reached 6.6 MB and 85,162 lines, nearly all
of them the same autonomy suppression line repeated every tick. Rotation
happens once at startup, on the file the process's own descriptors point at.
These tests use a real child process writing to a real file, because the thing
being proved is what happens to the descriptors across a rename.
"""

import os
import subprocess
import sys
from pathlib import Path

from tessera.log_rotation import rotate_core_streams, rotate_file, stream_file

ROOT = Path(__file__).resolve().parents[1]

# A child that writes a line, rotates its own stdout, and writes again. The
# second half must land in the live file, not in the renamed one.
WRITER = '''
import sys
sys.path.insert(0, {root!r})
from tessera.log_rotation import rotate_core_streams

for step in range(3):
    print('line ' + str(step), flush=True)
    if step == 1:
        print('rotating: ' + str(rotate_core_streams(1, 2, fds=(1,))), flush=True)
print('done', flush=True)
'''


def test_a_child_keeps_writing_to_its_named_file_after_rotation(tmp_path):
    log = tmp_path / 'core.log'
    script = WRITER.format(root=str(ROOT))
    with log.open('a') as stream:
        completed = subprocess.run(
            [sys.executable, '-c', script], stdout=stream, stderr=subprocess.STDOUT,
            timeout=60, check=True, cwd=str(ROOT),
        )
    assert completed.returncode == 0
    rotated = tmp_path / 'core.log.1'
    assert rotated.is_file(), 'the oversized log was not moved aside'
    assert log.is_file(), 'the core lost its named log file after rotation'
    # Everything written after the rename must be in the live file. A rotation
    # that leaves the descriptor on the old inode looks successful and sends
    # every later line to the archive.
    assert 'line 0' in rotated.read_text()
    live = log.read_text()
    assert 'line 2' in live
    assert 'done' in live


def test_a_small_file_is_left_alone(tmp_path):
    log = tmp_path / 'core.log'
    log.write_text('one short line\n')
    assert rotate_file(str(log), 10_000, 3) is None
    assert not (tmp_path / 'core.log.1').exists()


def test_rotation_keeps_the_declared_number_of_older_files(tmp_path):
    log = tmp_path / 'core.log'
    for number in range(4):
        log.write_text('run ' + str(number) + '\n' * 100)
        rotated = rotate_file(str(log), 10, 2)
        assert rotated is not None
    names = sorted(path.name for path in tmp_path.iterdir())
    assert names == ['core.log.1', 'core.log.2']


def test_a_pipe_is_not_a_file_to_rotate(tmp_path):
    read_fd, write_fd = os.pipe()
    try:
        assert stream_file(write_fd) is None
        result = rotate_core_streams(1, 1, fds=(write_fd,))
        assert result['rotated'] == ''
    finally:
        os.close(read_fd)
        os.close(write_fd)


def test_serve_rotates_the_core_stream_file_at_startup(tmp_path):
    """The server path rotates, not just the helper."""
    from tessera import server
    from tessera.config import Config

    log = tmp_path / 'core.out'
    log.write_text('x' * 200)
    config = Config(home=tmp_path, cwd=tmp_path)
    config.server.log_max_bytes = 10
    config.server.log_keep = 2

    with log.open('a') as stream:
        saved = os.dup(1)
        os.dup2(stream.fileno(), 1)
        try:
            result = server._rotate_core_output(config)
        finally:
            os.dup2(saved, 1)
            os.close(saved)

    assert result['rotated'] == str(log) + '.1'
    assert (tmp_path / 'core.out.1').is_file()


def test_rotation_is_off_when_the_cap_is_zero(tmp_path):
    log = tmp_path / 'core.log'
    log.write_text('x' * 200)
    assert rotate_core_streams(0, 3, fds=())['rotated'] == ''
    assert log.is_file() and not (tmp_path / 'core.log.1').exists()


def test_a_repeated_tick_failure_is_reported_once(tmp_path, capsys):
    """A missing backend made every tick fail identically, one line per tick.

    The first line is the only one that names the fix; the rest buried it. The
    line repeats only when the failure itself changes or after a success.
    """
    from test_autonomy import open_manager

    manager, _slug, _clock, _backend = open_manager(
        tmp_path, ['quiet'], self_prompt_gap_seconds=0, standing_floor=False)
    with manager:
        autonomy = manager.autonomy
        for _ in range(6):
            autonomy._note_tick_failure(ValueError('No backend configured for main'))
        printed = capsys.readouterr().err
        assert printed.count('tick failed') == 1
        autonomy._clear_tick_failure()
        autonomy._note_tick_failure(ValueError('No backend configured for main'))
        autonomy._note_tick_failure(RuntimeError('something else'))
        later = capsys.readouterr().err
        assert later.count('tick failed') == 2
