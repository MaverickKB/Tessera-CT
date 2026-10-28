"""Intake: an artifact from outside becomes a card that is not work yet.

The collector's shape: a PR "is gathered and becomes a card. They do not automatically
become work, they are assigned to an intake desk that reviews the PR and notes the
compatibility, the subject matter ... and submits it for acceptance -- this acceptance gate
can be human or session ... then that can be either ignored or signed off on for actual
work."
"""
from pathlib import Path

import pytest

from tessera.board import CLAIMABLE_STATES, BoardStore
from tessera import intake


def board_at(tmp_path):
    return BoardStore(tmp_path / 'board.jsonl', enabled=True)


def test_a_gathered_artifact_is_not_work_and_nobody_can_claim_it(tmp_path):
    board = board_at(tmp_path)
    card = board.create('Gathered PR', 'ops', 0, 0, card_id='intake-1',
                        intake={'source': 'github:owner/repo', 'reference': 'owner/repo#12',
                                'summary': 'adds a null check'})
    assert card['status'] == 'intake'
    # The state is what keeps it out of the work pool, so assert that, not just the label.
    assert 'intake' not in CLAIMABLE_STATES
    assert card['status'] not in CLAIMABLE_STATES


def test_an_intake_card_needs_a_reference(tmp_path):
    board = board_at(tmp_path)
    with pytest.raises(Exception) as refused:
        board.create('Nameless', 'ops', 0, 0, intake={'source': 'github:owner/repo'})
    assert 'reference' in str(refused.value)


def test_intake_refuses_an_unknown_subject(tmp_path):
    board = board_at(tmp_path)
    with pytest.raises(Exception) as refused:
        board.create('Bad subject', 'ops', 0, 0,
                     intake={'reference': 'owner/repo#1', 'subject': 'whatever'})
    assert 'subject must be one of' in str(refused.value)


def test_triage_records_the_subject_and_compatibility_the_decision_reads(tmp_path):
    board = board_at(tmp_path)
    board.create('Gathered PR', 'ops', 0, 0, card_id='intake-1',
                 intake={'reference': 'owner/repo#12', 'summary': 'body'})
    board.triage_intake('intake-1', subject='feature-expansion',
                        compatibility='applies cleanly to HEAD', summary='extends the exporter')
    intake_record = board.get('intake-1')['intake']
    assert intake_record['subject'] == 'feature-expansion'
    assert intake_record['compatibility'] == 'applies cleanly to HEAD'
    assert intake_record['summary'] == 'extends the exporter'
    # Triaging does not make it work.
    assert board.get('intake-1')['status'] == 'intake'


def test_triage_records_the_reviewer_as_the_last_writer(tmp_path):
    """A triage is a board write by a reviewer, so the card must name that reviewer.

    The page and the board tool both pass the acting session through to triage. Before
    this test, the store dropped it: the card kept the gathering session as
    `updated_by`, so the board's history credited the wrong thread for the review.
    """
    board = board_at(tmp_path)
    board.create('Gathered PR', 'ops', 0, 0, card_id='intake-1',
                 intake={'reference': 'owner/repo#12', 'summary': 'body'})
    assert board.get('intake-1')['updated_by'] == 'ops'
    card = board.triage_intake('intake-1', subject='fix', compatibility='clean',
                               summary='null deref fix', actor='reviewer')
    assert card['updated_by'] == 'reviewer'
    # The reviewer is on the durable record, not only in the returned copy.
    stored = board_at(tmp_path).get('intake-1')
    assert stored is not None and stored['updated_by'] == 'reviewer'


def test_triage_without_an_actor_keeps_the_card_writer(tmp_path):
    """actor=None is the operator, which changes no attribution. Every board writer agrees."""
    board = board_at(tmp_path)
    board.create('Gathered PR', 'ops', 0, 0, card_id='intake-1',
                 intake={'reference': 'owner/repo#12'})
    card = board.triage_intake('intake-1', subject='fix')
    assert card['updated_by'] == 'ops'


def test_accepting_intake_makes_it_work_and_keeps_the_acceptance(tmp_path):
    board = board_at(tmp_path)
    board.create('Gathered PR', 'ops', 0, 0, card_id='intake-1',
                 intake={'reference': 'owner/repo#12', 'summary': 'body'})
    board.triage_intake('intake-1', subject='fix', compatibility='clean')
    board.accept_intake('intake-1', 'reviewer', note='we want this')
    card = board.get('intake-1')
    assert card['status'] == 'queued'
    assert card['status'] in CLAIMABLE_STATES
    assert card['owner'] == ''
    acceptance = card['acceptances'][0]
    assert acceptance['by'] == 'reviewer'
    assert acceptance['evidence'] == 'owner/repo#12'
    assert acceptance['kind'] == 'intake'


def test_intake_cannot_be_accepted_before_it_is_triaged(tmp_path):
    """The decision reads the subject, so there is nothing to decide on until it exists."""
    board = board_at(tmp_path)
    board.create('Gathered PR', 'ops', 0, 0, card_id='intake-1',
                 intake={'reference': 'owner/repo#12'})
    with pytest.raises(Exception) as refused:
        board.accept_intake('intake-1', 'reviewer')
    assert 'Triage the artifact' in str(refused.value)


def test_ignoring_keeps_the_card_as_evidence_with_the_reason(tmp_path):
    board = board_at(tmp_path)
    board.create('Gathered PR', 'ops', 0, 0, card_id='intake-1',
                 intake={'reference': 'owner/repo#12'})
    board.ignore_intake('intake-1', 'reviewer', reason='out of scope for this release')
    card = board.get('intake-1')
    # Ignored is not deleted: the record stays readable.
    assert card['status'] == 'cancelled'
    assert card['ignored'][0]['reason'] == 'out of scope for this release'
    assert card['ignored'][0]['by'] == 'reviewer'
    assert [c['id'] for c in board.list()] == ['intake-1']


def test_ignoring_needs_a_reason(tmp_path):
    board = board_at(tmp_path)
    board.create('Gathered PR', 'ops', 0, 0, card_id='intake-1',
                 intake={'reference': 'owner/repo#12'})
    with pytest.raises(Exception) as refused:
        board.ignore_intake('intake-1', 'reviewer', reason='   ')
    assert 'needs a reason' in str(refused.value)


def test_a_card_can_only_leave_intake_once(tmp_path):
    board = board_at(tmp_path)
    board.create('Gathered PR', 'ops', 0, 0, card_id='intake-1',
                 intake={'reference': 'owner/repo#12'})
    board.triage_intake('intake-1', subject='fix')
    board.accept_intake('intake-1', 'reviewer')
    with pytest.raises(Exception) as refused:
        board.accept_intake('intake-1', 'reviewer')
    assert 'Only a card in intake' in str(refused.value)
    with pytest.raises(Exception) as ignored:
        board.ignore_intake('intake-1', 'reviewer', reason='too late')
    assert 'Only a card in intake' in str(ignored.value)


def test_the_collector_refuses_to_guess_a_repository_or_provider():
    """The collector should be explicit about which repo and which provider."""
    for spec in (intake.GatherSpec('github', 'not-a-repo'),
                 intake.GatherSpec('github', 'owner/name/extra'),
                 intake.GatherSpec('gitlab', 'owner/name'),
                 intake.GatherSpec('github', 'owner/name', limit=0)):
        with pytest.raises(intake.IntakeError):
            spec.validate()


def test_a_card_id_is_stable_per_reference_so_polling_does_not_duplicate(tmp_path):
    """A collector that polls must not multiply cards for one pull request."""
    board = board_at(tmp_path)
    record = {'source': 'github:owner/repo', 'reference': 'owner/repo#12',
              'title': 'Fix', 'summary': 'body'}
    first = intake.gather_into_board(board, intake.GatherSpec('github', 'owner/repo'),
                                     'ops', numbers=None) if False else None
    # Create twice with the same derived id, the way a poll would.
    card_id = intake._card_id_for(record['reference'])
    board.create('Fix', 'ops', 0, 0, card_id=card_id, intake=intake.intake_record(record))
    board.create('Fix', 'ops', 0, 0, card_id=card_id, intake=intake.intake_record(record))
    assert len([c for c in board.list() if c['status'] == 'intake']) == 1


def test_intake_record_carries_only_the_fields_acceptance_reads():
    record = {'source': 'github:owner/repo', 'reference': 'owner/repo#12',
              'summary': 'body', 'url': 'https://example.com', 'author': 'someone'}
    built = intake.intake_record(record)
    assert set(built) == {'source', 'reference', 'subject', 'compatibility', 'summary'}
    assert built['reference'] == 'owner/repo#12'
    with pytest.raises(intake.IntakeError):
        intake.intake_record({'source': 'x'})


# The gather path itself, which no test touched until now. Every gather raised NameError
# inside list_pull_requests, because it tested `config` while `config` was not in its
# scope; and every gather without an explicit timeout was rejected by validate() before
# the config was ever consulted, under a message ("must be a positive number") that named
# the wrong problem. Both were invisible because the suite only tested the board states an
# intake card can occupy, never the call that creates one.

class _Settings:
    limit = 20
    timeout_seconds = 60.0


class _Config:
    intake = _Settings()


def _stub_gh(monkeypatch, rows):
    calls = []

    def fake(*args, timeout=None):
        calls.append((args, timeout))
        return rows
    monkeypatch.setattr(intake, '_gh', fake)
    return calls


def test_a_gather_with_unstated_bounds_resolves_them_instead_of_refusing(monkeypatch):
    calls = _stub_gh(monkeypatch, [{'number': 7, 'title': 'A PR', 'author': {'login': 'k'},
                                    'mergeable': 'MERGEABLE', 'headRefName': 'b', 'url': 'u'}])
    spec = intake.GatherSpec('github', 'owner/repo')
    records = intake.list_pull_requests(spec, _Config())
    # The bounds came from config, and reached the provider call.
    assert spec.limit == 20 and spec.timeout == 60.0
    assert calls and calls[0][1] == 60.0
    assert records and records[0]['reference'] == 'owner/repo#7'


def test_gathering_one_named_pull_request_uses_the_same_resolved_bounds(monkeypatch):
    calls = _stub_gh(monkeypatch, {'number': 9, 'title': 'One', 'author': {'login': 'k'},
                                   'mergeable': 'MERGEABLE', 'headRefName': 'b', 'url': 'u'})
    spec = intake.GatherSpec('github', 'owner/repo')
    record = intake.gather_record(spec, 9, config=_Config())
    assert calls and calls[0][1] == 60.0
    assert record['reference'] == 'owner/repo#9'


def test_a_gather_without_config_still_refuses_rather_than_inventing_bounds():
    # No config means nobody stated the bounds. It must say so, not default silently.
    spec = intake.GatherSpec('github', 'owner/repo')
    with pytest.raises(intake.IntakeError) as refused:
        intake.list_pull_requests(spec)
    # Which of the two unstated bounds it names first is not the point; that it refuses
    # instead of inventing a default is.
    assert 'must be a positive' in str(refused.value)
