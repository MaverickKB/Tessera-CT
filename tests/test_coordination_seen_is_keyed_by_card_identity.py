"""`coordination_seen` must be keyed by card identity, not by ordinal.

`coordination_offers` (tessera/autonomy.py) records what it has already offered in
`state['coordination_seen'][slug]`. The key must be the card's identity, not `str(ordinal)`.

An ordinal is a position, not a card property. It is assigned by enumeration at
read time (`tessera/board.py:403`, `for ordinal, item in enumerate(...)`), the
journal never stores one (measured: 0 of 217 records carry an `ordinal`), and
compaction renumbers every card while resetting versions.

So a record left by one card suppressed whichever card later landed on that
number. Unlike the own-card lane, this lane's suppression is bounded by
`hold_seconds`, so the cost is a delayed offer rather than a permanent one -- but
it is still a card nobody was told about, and it is invisible because the record
looks legitimate.

`coordination_seen` can be empty, so the mismatch is latent rather than active.
The key is still the card's identity, so a later offer is not suppressed by a
position that moved. No stored record has to be rewritten.
"""
import pytest

from tessera.autonomy import coordination_offers


def _card(card_id, ordinal, title, status='queued', version=1, project='alpha'):
    return {'card_id': card_id, 'ordinal': ordinal, 'title': title,
            'status': status, 'version': version, 'project': project}


def test_the_stored_key_is_the_card_id():
    """The record must name the card, so a reader can tell what it refers to."""
    card = _card('aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa', 40, 'Card A')
    _, seen = coordination_offers({'cards': [card]}, 3, {}, now=1_000.0)
    assert list(seen) == ['aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa'], seen


def test_a_different_card_on_the_same_ordinal_is_not_suppressed():
    """The defect: card A's record must not silence card B at the same position.

    Compaction renumbers cards. A card that was never offered must be offered even
    if its position was once occupied by a card that was.
    """
    card_a = _card('aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa', 40, 'Card A')
    first, seen = coordination_offers({'cards': [card_a]}, 3, {}, now=1_000.0)
    assert len(first) == 1

    # A is gone; B now sits at the same ordinal, inside the hold window.
    card_b = _card('bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb', 40, 'Card B')
    second, _ = coordination_offers({'cards': [card_b]}, 3, seen, now=1_000.0 + 60)
    assert len(second) == 1, 'card B was suppressed by card A\'s record at ordinal 40'
    assert 'Card B' in second[0]


def test_the_same_card_is_still_held_inside_the_window():
    """Regression guard: the fix must not remove the dedup it exists to preserve."""
    card = _card('aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa', 40, 'Card A')
    first, seen = coordination_offers({'cards': [card]}, 3, {}, now=1_000.0)
    assert len(first) == 1
    held, _ = coordination_offers({'cards': [card]}, 3, seen, now=1_000.0 + 60)
    assert held == []
    # A version move re-arms immediately, because the card actually changed.
    moved = dict(card, version=2)
    rearmed, _ = coordination_offers({'cards': [moved]}, 3, seen, now=1_000.0 + 60)
    assert len(rearmed) == 1


def test_a_card_without_an_id_falls_back_to_position():
    """A brief that omits card_id must still dedup, so the fallback stays honest."""
    card = {'ordinal': 40, 'title': 'No id', 'status': 'queued', 'version': 1,
            'project': 'alpha'}
    first, seen = coordination_offers({'cards': [card]}, 3, {}, now=1_000.0)
    assert len(first) == 1
    assert list(seen) == ['40'], seen
    held, _ = coordination_offers({'cards': [card]}, 3, seen, now=1_000.0 + 60)
    assert held == []
