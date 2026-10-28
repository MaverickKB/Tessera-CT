"""Board tool bindings use the acting session and existing log evidence only."""

from .board import BoardError, BoardStore, INTAKE_SUBJECTS, log_range
from .jev import JudgeKit
from .passive_status import configured_path
from .tools import object_schema


class BoardTools:
    def __init__(self, manager):
        self.manager = manager
        self.config = manager.config
        settings = self.config.board
        self.store = BoardStore(configured_path(self.config.home, settings.journal_file), enabled=settings.enabled,
                                max_text_bytes=settings.max_text_bytes, max_artifacts=settings.max_artifacts,
                                max_questions=settings.max_questions,
                                # The passive reader refuses a journal above this cap, so the
                                # writer compacts before the board becomes unreadable.
                                max_journal_bytes=self.config.status.max_file_bytes)
        self.store.acceptance_required = bool(settings.acceptance_required)
        self.store.acceptance_by = tuple(settings.acceptance_by or ())

    def _enabled(self):
        if not self.config.board.enabled:
            raise BoardError('WIP board is disabled')

    def _range(self, runtime, start, end):
        self._enabled()
        last = runtime.session.log.count() - 1
        if last < 0:
            # A board card is a plan item before it is evidence. An empty log links to 0,0
            # rather than refusing the card outright.
            return 0, 0
        end = last if end is None else end
        start = end if start is None else start
        log_range(start, end)
        if end > last:
            raise BoardError('Board log range exceeds existing session evidence')
        return start, end

    @staticmethod
    def _result(card):
        return {'card_id': card['id'], 'version': card['version'], 'status': card['status'], 'attempt': card['attempt'],
                'artifact_count': len(card['artifacts']) + len(card.get('evidence', [])), 'question_pending': any(q['pending'] for q in card['questions']),
                'project': card.get('project', '')}

    @staticmethod
    def _project_result(project):
        steps = project.get('steps') or []
        return {'project_id': project['id'], 'version': project['version'], 'title': project['title'],
                'status': project['status'], 'manager': project['manager'], 'kind': project.get('kind', 'planning'),
                'steps_done': sum(1 for step in steps if step['done']), 'steps_total': len(steps)}

    def register_tools(self, runtime, registry):
        if not self.config.board.enabled:
            return
        def create(title, log_start=None, log_end=None, attempt=0, card_id=None, note='', claim=True, project=''):
            start, end = self._range(runtime, log_start, log_end)
            if project:
                # A card may only be filed under a project that exists, so the board
                # never accumulates orphan project names nobody can open.
                self.store.get_project(project)
            card = self.store.create(self.manager.messaging._redact(title), runtime.slug, start, end,
                                     attempt=attempt, card_id=card_id,
                                     note=self.manager.messaging._redact(note),
                                     owner=runtime.slug if claim else '', project=project)
            return self._result(card)
        def update(card_id, status, attempt=None, expected_version=None, log_start=None, log_end=None):
            start, end = self._range(runtime, log_start, log_end)
            card = self.store.update_status(card_id, status, attempt=attempt, actor=runtime.slug,
                                            expected_version=expected_version, log_start=start, log_end=end)
            return self._result(card)
        def attach(card_id, reference, expected_version=None, log_start=None, log_end=None):
            start, end = self._range(runtime, log_start, log_end)
            card = self.store.attach_artifact(card_id, self.manager.messaging._redact(reference), actor=runtime.slug,
                                              expected_version=expected_version, log_start=start, log_end=end)
            return self._result(card)
        def ask(card_id, question, required=False, expected_version=None, log_start=None, log_end=None):
            start, end = self._range(runtime, log_start, log_end)
            question = self.manager.messaging._redact(question)
            self.store._text(question)
            card = self.store.get(card_id)
            if card.get('owner') and card['owner'] != runtime.slug:
                raise BoardError('Board card is claimed by another session')
            if expected_version is not None and (type(expected_version) is not int or expected_version != card['version']):
                raise BoardError('Board card version is stale')
            kit = JudgeKit.from_config(self.config, enabled=self.config.jev.enabled and self.config.board.judge_asks,
                                       decision_log=runtime.root / 'decisions.jsonl')
            gate = kit.ask_human_gate(question, context={'title': card['title'], 'status': card['status']}, required=required)
            pending = gate['ask_human'] >= self.config.board.ask_human_threshold or gate['needs_authority'] >= self.config.board.ask_human_threshold
            card = self.store.ask_human(card_id, question, pending=pending, gate=gate, actor=runtime.slug,
                                        expected_version=card['version'], log_start=start, log_end=end)
            return {**self._result(card), 'human_gate': gate}
        def claim(card_id, owner=None, expected_version=None):
            owner = runtime.slug if owner is None else self.manager.messaging._redact(owner)
            card = self.store.claim(card_id, owner, actor=runtime.slug, expected_version=expected_version)
            return self._result(card)
        def start(card_id, receipt='', expected_version=None):
            receipt = self.manager.messaging._redact(receipt)
            card = self.store.start_card(card_id, runtime.slug, receipt, expected_version=expected_version)
            return self._result(card)
        def release(card_id, expected_version=None):
            card = self.store.claim(card_id, '', actor=runtime.slug, expected_version=expected_version)
            return self._result(card)
        def accept(card_id, evidence='', note='', expected_version=None):
            # The accepting identity defaults to this session: a session may accept as
            # itself, and a person is named explicitly. Both must resolve when identity
            # records are on, so an acceptance cannot be signed by a name nobody holds.
            acceptor = runtime.slug
            evidence = self.manager.messaging._redact(evidence)
            note = self.manager.messaging._redact(note)
            card = self.store.accept(card_id, acceptor, evidence=evidence, note=note,
                                     expected_version=expected_version)
            return self._result(card)
        def gather_intake(repository, number=None, provider='github', limit=20, project=''):
            """Gather pull requests from ONE named repository into intake cards."""
            from . import intake as intake_mod
            # The caller may narrow the count; the install's [intake] bound applies when
            # it does not. A stated limit is used as given.
            spec = intake_mod.GatherSpec(provider, repository, limit if limit else None)
            numbers = [number] if number else None
            if project:
                self.store.get_project(project)
            cards = intake_mod.gather_into_board(self.store, spec, runtime.slug,
                                                 numbers=numbers, project=project,
                                                 config=self.config)
            return {'gathered': len(cards),
                    'cards': [{'card_id': card['id'], 'status': card['status'],
                               'reference': card['intake']['reference']} for card in cards]}
        def triage_intake(card_id, subject=None, compatibility=None, summary=None, expected_version=None):
            card = self.store.triage_intake(card_id, subject=subject, compatibility=compatibility,
                                            summary=summary, expected_version=expected_version)
            return self._result(card)
        def accept_intake(card_id, note='', expected_version=None):
            card = self.store.accept_intake(card_id, runtime.slug, note=note,
                                            expected_version=expected_version)
            return self._result(card)
        def ignore_intake(card_id, reason, expected_version=None):
            card = self.store.ignore_intake(card_id, runtime.slug, reason=reason,
                                            expected_version=expected_version)
            return self._result(card)
        def note(card_id, text, expected_version=None):
            text = self.manager.messaging._redact(text)
            card = self.store.get(card_id)
            card = self.store.append_note(card_id, text, actor=runtime.slug,
                                          expected_version=expected_version if expected_version is not None else card['version'])
            return self._result(card)
        def revise(card_id, title=None, note=None, expected_version=None):
            if title is not None:
                title = self.manager.messaging._redact(title)
                self.store._text(title)
            if note is not None:
                note = self.manager.messaging._redact(note)
                if note:
                    self.store._text(note)
            card = self.store.revise(card_id, title=title, note=note, actor=runtime.slug,
                                     expected_version=expected_version)
            return self._result(card)
        def file_card(card_id, project, expected_version=None):
            if project:
                self.store.get_project(project)
            card = self.store.file_card(card_id, project, actor=runtime.slug, expected_version=expected_version)
            return self._result(card)
        def remove_card(card_id, expected_version=None):
            card = self.store.remove(card_id, actor=runtime.slug, expected_version=expected_version)
            return self._result(card)
        def create_project(title, description='', note='', manager='', project_id=None, kind='planning'):
            project = self.store.create_project(self.manager.messaging._redact(title), runtime.slug,
                                                description=self.manager.messaging._redact(description),
                                                manager=manager, project_id=project_id,
                                                note=self.manager.messaging._redact(note), kind=kind)
            return self._project_result(project)
        def update_project(project_id, title=None, description=None, note=None, manager=None, status=None,
                           expected_version=None, kind=None):
            project = self.store.update_project(project_id, title=title, description=description, note=note,
                                                manager=manager, status=status, expected_version=expected_version, kind=kind)
            return self._project_result(project)
        def set_steps(project_id, steps, expected_version=None):
            cleaned = []
            for step in steps:
                if not isinstance(step, dict):
                    raise BoardError('Invalid project step')
                cleaned.append({'id': step.get('id'), 'text': self.manager.messaging._redact(step.get('text') or ''),
                                'done': bool(step.get('done', False))})
            project = self.store.set_steps(project_id, cleaned, expected_version=expected_version)
            return self._project_result(project)
        def check_step(project_id, step_id, done, expected_version=None):
            project = self.store.check_step(project_id, step_id, done, expected_version=expected_version)
            return self._project_result(project)
        range_fields = {'log_start': {'type': 'integer', 'minimum': 0}, 'log_end': {'type': 'integer', 'minimum': 0}}
        version_fields = {'expected_version': {'type': 'integer', 'minimum': 1}, **range_fields}
        registry.register('board_create_card', 'Create a durable WIP card linked to this session and existing log evidence.',
                          object_schema({'title': {'type': 'string'}, 'card_id': {'type': 'string'},
                                         'attempt': {'type': 'integer', 'minimum': 0}, 'note': {'type': 'string'},
                                         'claim': {'type': 'boolean'}, 'project': {'type': 'string'},
                                         **range_fields}, ['title']), create)
        registry.register('board_update_status', 'Update an owned WIP card status and optional attempt count.',
                          object_schema({'card_id': {'type': 'string'}, 'status': {'type': 'string', 'enum': [
                              'queued', 'running', 'blocked', 'succeeded', 'failed', 'cancelled']},
                                         'attempt': {'type': 'integer', 'minimum': 0}, **version_fields}, ['card_id', 'status']), update)
        registry.register('board_attach_artifact', 'Attach an opaque artifact reference to an owned card; never open the reference.',
                          object_schema({'card_id': {'type': 'string'}, 'reference': {'type': 'string'}, **version_fields},
                                        ['card_id', 'reference']), attach)
        registry.register('board_ask_human', 'Record a question and judge whether human input is needed. required=true preserves an explicit human boundary.',
                          object_schema({'card_id': {'type': 'string'}, 'question': {'type': 'string'},
                                         'required': {'type': 'boolean'}, **version_fields}, ['card_id', 'question']), ask)
        registry.register('board_claim_card', 'Claim a card from the shared board so other sessions leave it alone. Omit owner to claim it for this session.',
                          object_schema({'card_id': {'type': 'string'}, 'owner': {'type': 'string'},
                                         'expected_version': {'type': 'integer', 'minimum': 1}}, ['card_id']), claim)
        registry.register('board_start_card', 'Mark a card you own as actively being worked: the board moves it to running and records this session plus a short receipt of what is happening. Use it when work begins, so the project cockpit shows real execution state instead of a stale queued card.',
                          object_schema({'card_id': {'type': 'string'}, 'receipt': {'type': 'string'},
                                         'expected_version': {'type': 'integer', 'minimum': 1}}, ['card_id']), start)
        registry.register('board_release_card', 'Release a claim so the card is open again for any session to pick up.',
                          object_schema({'card_id': {'type': 'string'},
                                         'expected_version': {'type': 'integer', 'minimum': 1}}, ['card_id']), release)
        registry.register('board_accept_card', 'Record that an identity accepts a card as work. Required before a card can move to succeeded when board.acceptance_required is on. Acceptance is durable history, kept even if the card is reopened.',
                          object_schema({'card_id': {'type': 'string'}, 'evidence': {'type': 'string'},
                                         'note': {'type': 'string'},
                                         'expected_version': {'type': 'integer', 'minimum': 1}}, ['card_id']), accept)
        registry.register('board_gather_intake', 'Gather open pull requests from ONE named repository into intake cards. An intake card is not work: it is not claimable and wakes no worker until someone triages it and accepts it.',
                          object_schema({'repository': {'type': 'string'}, 'number': {'type': 'integer', 'minimum': 1},
                                         'provider': {'type': 'string', 'enum': ['github']},
                                         'limit': {'type': 'integer', 'minimum': 1}, 'project': {'type': 'string'}},
                                        ['repository']), gather_intake)
        registry.register('board_triage_intake', 'Record what an intake reviewer found: subject (bug/fix/feature-addition/feature-expansion/other), compatibility, and a summary. The acceptance decision reads these.',
                          object_schema({'card_id': {'type': 'string'},
                                         'subject': {'type': 'string', 'enum': list(INTAKE_SUBJECTS)},
                                         'compatibility': {'type': 'string'}, 'summary': {'type': 'string'},
                                         'expected_version': {'type': 'integer', 'minimum': 1}}, ['card_id']), triage_intake)
        registry.register('board_accept_intake', 'Accept a triaged intake card as work: it moves to queued and becomes claimable, with who accepted and why kept as history.',
                          object_schema({'card_id': {'type': 'string'}, 'note': {'type': 'string'},
                                         'expected_version': {'type': 'integer', 'minimum': 1}}, ['card_id']), accept_intake)
        registry.register('board_ignore_intake', 'Decline an intake card as work. It stays on the board as evidence with the reason recorded; nothing is deleted.',
                          object_schema({'card_id': {'type': 'string'}, 'reason': {'type': 'string'},
                                         'expected_version': {'type': 'integer', 'minimum': 1}}, ['card_id', 'reason']), ignore_intake)
        registry.register('board_note', 'Append a short progress line to a card. Keeps the plan and what was finished readable to every session.',
                          object_schema({'card_id': {'type': 'string'}, 'text': {'type': 'string'},
                                         'expected_version': {'type': 'integer', 'minimum': 1}}, ['card_id', 'text']), note)
        registry.register('board_revise_card', 'Correct a card title or replace its note before the plan changes.',
                          object_schema({'card_id': {'type': 'string'}, 'title': {'type': 'string'},
                                         'note': {'type': 'string'},
                                         'expected_version': {'type': 'integer', 'minimum': 1}}, ['card_id']), revise)
        registry.register('board_file_card', 'Tessera files a card under an existing subject or planning project. Reuse relevant subjects for related symptoms and fixes; an empty project leaves it awaiting filing.',
                          object_schema({'card_id': {'type': 'string'}, 'project': {'type': 'string'},
                                         'expected_version': {'type': 'integer', 'minimum': 1}}, ['card_id', 'project']), file_card)
        registry.register('board_remove_card', 'Take a card off the board when it is no longer work. The record is kept as evidence, flagged removed.',
                          object_schema({'card_id': {'type': 'string'},
                                         'expected_version': {'type': 'integer', 'minimum': 1}}, ['card_id']), remove_card)
        def project_artifact(project_id, path, title='', x=80, y=80):
            from pathlib import Path
            import math
            import uuid
            self.store.get_project(project_id)
            if not all(type(value) in (int, float) and math.isfinite(value) for value in (x, y)):
                raise BoardError('Artifact position must be finite')
            target = Path(path).expanduser()
            if not target.is_absolute():
                target = runtime.session.workspace.path / target
            attachment = self.manager.add_local_attachment(runtime.slug, target)
            node = {'id': uuid.uuid4().hex, 'kind': 'image' if attachment['media_type'].startswith('image/') else 'document',
                    'title': self.manager.messaging._redact(title or target.name), 'x': x, 'y': y,
                    'width': 320, 'height': 280, 'session_slug': runtime.slug,
                    'attachment_id': attachment['id'], 'media_type': attachment['media_type']}
            report = self.manager.board_project_canvas(project_id, {'nodes': [node]})
            return {'attachment': attachment, 'node_id': node['id'], 'workspace': report}
        registry.register('board_project_artifact',
                          'Reference an existing local image, document, PDF or text file as a project artifact and place it on its canvas. '
                          'Relative paths use the current session workspace. The original path is retained without copying; changes or removal are reported on use. '
                          'Use this after creating a visual or document with the available tools.',
                          object_schema({'project_id': {'type': 'string'}, 'path': {'type': 'string'},
                                         'title': {'type': 'string'}, 'x': {'type': 'number'}, 'y': {'type': 'number'}},
                                        ['project_id', 'path']), project_artifact, risky=True)
        def project_canvas(project_id, changes=None):
            return self.manager.board_project_canvas(project_id, changes)
        node_properties = {}
        for name in ('id', 'kind', 'title', 'body', 'parent', 'card_id', 'step_id',
                     'session_slug', 'attachment_id', 'media_type', 'url'):
            node_properties[name] = {'type': 'string'}
        node_properties['placed'] = {'type': 'boolean'}
        node_properties['kind']['enum'] = ['note', 'task', 'step', 'image', 'document', 'reference', 'group']
        for name in ('x', 'y', 'width', 'height', 'z'):
            node_properties[name] = {'type': 'number'}
        changes_schema = object_schema({
            'nodes': {'type': 'array', 'items': object_schema(node_properties, ['id'])},
            'links': {'type': 'array', 'items': object_schema({
                'id': {'type': 'string'}, 'from': {'type': 'string'}, 'to': {'type': 'string'},
                'label': {'type': 'string'}}, ['id'])},
            'remove_nodes': {'type': 'array', 'items': {'type': 'string'}},
            'remove_links': {'type': 'array', 'items': {'type': 'string'}},
            'expected_version': {'type': 'integer', 'minimum': 0}})
        registry.register('board_project_canvas',
                          'Read or edit a shared project canvas. Omit changes to read first. '
                          'Nodes are persistent pieces: note, task(card_id), step(step_id), '
                          'image/document(session_slug+attachment_id), reference(url), group. '
                          'Set x/y/width/height/z to arrange; parent groups pieces. placed=false stores a piece in the library without showing it on the canvas. '
                          'Directed labelled links connect node IDs for diagrams and maps. '
                          'Patches merge supplied fields and preserve unrelated pieces. '
                          'Use expected_version when replacing previously read content. '
                          'Work status belongs to existing board cards and plan steps.',
                          object_schema({'project_id': {'type': 'string'}, 'changes': changes_schema}, ['project_id']),
                          project_canvas)
        def read_evidence(card_id, evidence_id):
            card = self.store.get(card_id)
            for item in card.get('evidence', []):
                if item['id'] == evidence_id:
                    path, media = self.manager.attachment_file(item['session_slug'], item['id'])
                    return {**item, 'path': str(path), 'media_type': media,
                            'inspect_with': 'read_image' if media.startswith('image/') else 'read_file'}
            raise BoardError('Evidence does not belong to this card')
        registry.register('board_read_evidence', 'Resolve a card screenshot or file to its stored path. Inspect images with read_image and text/logs with read_file using the existing tools.',
                          object_schema({'card_id': {'type': 'string'}, 'evidence_id': {'type': 'string'}}, ['card_id', 'evidence_id']), read_evidence)
        registry.register('board_create_project', 'Create a shared work grouping. kind=subject is a granular subject, recurring problem or healing effort, with related cards and lessons and no required plan. kind=planning is a collaborative planning project. Tessera manages these categories; reuse a relevant group before creating another.',
                          object_schema({'title': {'type': 'string'}, 'description': {'type': 'string'},
                                         'note': {'type': 'string'}, 'manager': {'type': 'string'},
                                         'project_id': {'type': 'string'}, 'kind': {'type': 'string', 'enum': ['subject', 'planning']}}, ['title']), create_project)
        registry.register('board_update_project', 'Maintain a subject or planning project: revise its intent, classify its kind, hand it to another manager, or close it.',
                          object_schema({'project_id': {'type': 'string'}, 'title': {'type': 'string'},
                                         'description': {'type': 'string'}, 'note': {'type': 'string'},
                                         'manager': {'type': 'string'},
                                         'kind': {'type': 'string', 'enum': ['subject', 'planning']},
                                         'status': {'type': 'string', 'enum': ['active', 'done', 'dropped']},
                                         'expected_version': {'type': 'integer', 'minimum': 1}}, ['project_id']),
                          update_project)
        registry.register('board_set_steps', 'Replace a project plan with the full ordered step list. Each step is text plus done.',
                          object_schema({'project_id': {'type': 'string'},
                                         'steps': {'type': 'array', 'items': {'type': 'object', 'properties': {
                                             'id': {'type': 'string'}, 'text': {'type': 'string'},
                                             'done': {'type': 'boolean'}}}},
                                         'expected_version': {'type': 'integer', 'minimum': 1}}, ['project_id', 'steps']),
                          set_steps)
        registry.register('board_check_step', 'Tick or untick one step of a project plan as it is actually finished.',
                          object_schema({'project_id': {'type': 'string'}, 'step_id': {'type': 'string'},
                                         'done': {'type': 'boolean'},
                                         'expected_version': {'type': 'integer', 'minimum': 1}},
                                        ['project_id', 'step_id', 'done']), check_step)
