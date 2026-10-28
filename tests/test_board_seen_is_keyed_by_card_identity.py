"""`board_seen` is keyed by card identity, not by ordinal.

These tests exercise `_own_board_lines` (tessera/autonomy.py) with the
`operations` slug. An ordinal is a position assigned at read time. The
journal does not store one. The seen record is keyed by card id, so it
follows the card when the list is reordered.
"""

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
    board_offer_limit = 5


class _Board:
    enabled = True


class _Config:
    def __init__(self, home):
        self.home = home
        self.autonomy = _Settings()
        self.board = _Board()


def _card(card_id, ordinal, status='blocked', version=6, owner='operations', title='A card'):
    return {
        'card_id': card_id,
        'ordinal': ordinal,
        'status': status,
        'version': version,
        'owner': owner,
        'title': title,
        'note': '',
        'updated_by': owner,
    }


class _Manager:
    """Returns whatever cards the test sets, so the ordinal can be made to drift."""

    def __init__(self, config):
        self.config = config
        self.cards = []

    def board_brief(self):
        return {'cards': list(self.cards)}


def _autonomy(home, cards):
    manager = _Manager(_Config(home))
    manager.cards = cards
    return Autonomy(manager), manager


def _offer(auto, now=None):
    """One offer pass, wired the way the real caller wires it.

    `_own_board_lines` reads `state['board_seen']` and returns the records to
    merge; the caller merges them (tessera/autonomy.py:1404). A test that skips
    the merge leaves `seen` empty and can never observe suppression.
    """
    lines, record = auto._own_board_lines('operations', now or time.time())
    auto.state.setdefault('board_seen', {}).setdefault('operations', {}).update(record)
    return lines


def test_a_different_card_at_the_same_ordinal_is_still_offered(tmp_path):
    """The discriminator.

    Card A is offered from ordinal 100. It is then removed and card B takes
    ordinal 100 with the same status and version — which is exactly what happens
    when a card is closed and a new one is filed at the same position. Card B has
    never been offered and must appear.
    """
    card_a = _card('aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa', 100, title='Card A')
    auto, manager = _autonomy(str(tmp_path), [card_a])

    first = _offer(auto)
    assert len(first) == 1, 'control: the first card must be offered'

    card_b = _card('bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb', 100, title='Card B')
    manager.cards = [card_b]

    second = _offer(auto)
    assert len(second) == 1, (
        'a different card at the same ordinal was suppressed by the previous '
        "card's record: %r" % (second,)
    )
    assert 'Card B' in second[0]


def test_the_record_follows_a_card_that_moves_position(tmp_path):
    """A card that shifts position must not be offered again.

    Card A is offered at ordinal 100. A card before it is removed, so A becomes
    ordinal 99. Nothing about A changed, so it is not news.
    """
    filler = _card('ffffffffffffffffffffffffffffffff', 99, title='Filler')
    card_a = _card('aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa', 100, title='Card A')
    auto, manager = _autonomy(str(tmp_path), [filler, card_a])

    first = _offer(auto)
    assert len(first) == 2, 'control: both cards are offered once'

    # The filler is closed; card A slides from 100 to 99.
    moved = dict(card_a)
    moved['ordinal'] = 99
    manager.cards = [moved]

    second = _offer(auto)
    assert second == [], (
        'the card was re-offered after only changing position: %r' % (second,)
    )


def test_the_stored_key_is_the_card_id(tmp_path):
    """The record must name the card, so a reader can tell what it refers to."""
    card_a = _card('aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa', 100)
    auto, _ = _autonomy(str(tmp_path), [card_a])
    _offer(auto)
    seen = auto.state['board_seen']['operations']
    assert list(seen) == ['aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa'], seen
