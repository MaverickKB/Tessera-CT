"""The autonomy dedup records must survive a restart.

`agenda_seen` and `board_seen` are written to disk by `_save()`, but they are
created lazily by `setdefault()` at compose time rather than in the default
state dict. The load loop in `Autonomy.__init__` iterates `self.state`'s default
keys, so a key that is not in that default is not restored — it is silently
dropped on every restart, and the file keeps the data.

The consequence is not cosmetic. Both records are what stop a wake from
re-delivering a lesson or re-offering a card that was already handled. Lose them
and the first wake after a restart re-delivers every open lesson and re-offers
every own card.
"""

import json
import time

from tessera.autonomy import Autonomy


class _Settings:
    state_file = 'autonomy/state.json'
    tasks_dir = 'autonomy/tasks'
    lesson_file = 'learning/lessons.json'
    lesson_pass_score = 0.7
    max_open_lessons = 12
    lesson_fail_limit = 3
    infinity_slug = 'infinity'
    enabled = False
    standing_floor = False
    self_prompt_enabled = False


class _Config:
    def __init__(self, home):
        self.home = home
        self.autonomy = _Settings()


class _Manager:
    def __init__(self, config):
        self.config = config


def _autonomy(home):
    """A real Autonomy, via the real constructor — not a hand-built replica.

    The constructor builds a LearningStore too, which reads lesson_file. It is
    absent in the temp home, and an absent lesson file is an empty store.
    """
    return Autonomy(_Manager(_Config(home)))


def test_dedup_records_survive_a_restart(tmp_path):
    first = _autonomy(str(tmp_path))
    first.state.setdefault('agenda_seen', {}).setdefault('infinity', {})['ls-TEST'] = {
        'fp': 'written|0|X', 'at': time.time()}
    first.state.setdefault('board_seen', {}).setdefault('infinity', {})['100'] = '100:blocked:6'
    first._save()

    # What is on disk is the evidence that the write path is fine.
    disk = json.loads(first.path.read_text())
    assert disk['agenda_seen']['infinity']['ls-TEST']['fp'] == 'written|0|X'
    assert disk['board_seen']['infinity']['100'] == '100:blocked:6'

    # A second constructor over the same file is exactly what a reload does.
    second = _autonomy(str(tmp_path))

    assert second.state.get('agenda_seen', {}).get('infinity', {}).get('ls-TEST', {}).get('fp') == 'written|0|X'
    assert second.state.get('board_seen', {}).get('infinity', {}).get('100') == '100:blocked:6'


def test_the_load_loop_covers_every_key_the_save_path_writes(tmp_path):
    """The shape of the defect rather than one instance of it.

    The restorable set is whatever a fresh constructor initializes — that is the
    set the load loop iterates. Comparing against the saving object's own state
    would be tautological, because the caller just put the keys there.

    This walks the live store's own key list too, so a key that some other code
    path creates lazily is caught here rather than discovered after a restart.
    """
    fresh = _autonomy(str(tmp_path / 'fresh'))
    restorable = set(fresh.state)

    a = _autonomy(str(tmp_path))
    a.state.setdefault('agenda_seen', {}).setdefault('infinity', {})['ls-TEST'] = {
        'fp': 'written|0|X', 'at': time.time()}
    a.state.setdefault('board_seen', {}).setdefault('infinity', {})['100'] = '100:blocked:6'
    a.state.setdefault('coordination_seen', {}).setdefault('infinity', {})['100'] = '100:blocked:6'
    a.state['pressure_at'] = {'infinity': time.time()}
    a.state['pressure_seen'] = {'mac-disk': time.time()}
    a._save()

    on_disk = set(json.loads(a.path.read_text()))
    assert on_disk <= restorable, f'persisted but never restored: {sorted(on_disk - restorable)}'


def test_a_fresh_home_still_starts_empty(tmp_path):
    """Control: the fix must not invent state on a first run."""
    a = _autonomy(str(tmp_path))
    assert a.state['agenda_seen'] == {}
    assert a.state['board_seen'] == {}
