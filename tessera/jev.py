"""Typed TypeSafe decisions with bounded caching and offline heuristics."""

import copy
from . import locking as fcntl
import hashlib
import http.client
import json
import math
import os
import re
import shlex
import threading
import time
import uuid
import urllib.error
import urllib.request
from urllib.parse import urlsplit
from collections import OrderedDict
from dataclasses import asdict

from .credentials import CredentialError, CredentialResolver
from .backend import BackendError
from .gates import conflicting_tasks, failed_checks
from .http_cancel import CancellableHTTP
from datetime import datetime, timezone
from pathlib import Path


# Failures that mean the wire did not deliver an answer. These are worth one
# retry, because the judge may simply be slower than the per-request timeout.
# A wrong or rejected answer is NOT here: that is the judge disagreeing, and
# retrying it would just ask the same question twice.
TRANSPORT_FAILURES = (TimeoutError, ConnectionError, http.client.HTTPException, urllib.error.URLError)


def credential_available(resolver, reference='', env=''):
    """Check local credential configuration without invoking helpers or refreshing OAuth.

    Saved-keychain selectors cannot be verified passively. Their configured presence
    permits a real request to resolve them; any failure is then observed, not guessed.
    """
    if env and resolver.environ.get(env, '').strip():
        return True
    seen = set()
    while reference and reference not in seen:
        seen.add(reference)
        settings = resolver.references.get(reference)
        if settings is None:
            return False
        if settings.env and resolver.environ.get(settings.env, '').strip():
            return True
        if settings.oauth:
            from .account_oauth import grant_report
            report = grant_report(resolver.oauth_home, settings.oauth)
            if report['state'] == 'current' or report['has_refresh']:
                return True
        if settings.keychain_service and settings.keychain_account:
            return True
        reference = settings.config_ref
    return False


def connection_endpoint(value):
    try:
        url = urlsplit(value)
        # Reading port also validates malformed/non-numeric port declarations.
        port = url.port
        return (url.scheme in ('http', 'https') and bool(url.hostname)
                and port != 0 and url.username is None and url.password is None)
    except (ValueError, TypeError):
        return False


def config_readiness(config):
    """Project configuration into a safe status without constructing or probing a judge."""
    if not config.jev.enabled:
        return {'state': 'off', 'reason': 'Jev is switched off'}
    if 'jev' in config.roles:
        from .judge_backend import backend_configured
        try:
            configured = backend_configured(config.backend_for('jev'))
        except (ValueError, KeyError):
            configured = False
    else:
        settings = config.jev
        resolver = CredentialResolver(config.credentials, oauth_home=str(config.home))
        configured = bool(connection_endpoint(settings.url) and settings.model and
                          credential_available(resolver, settings.credential_ref, settings.api_key_env))
    if not configured:
        return {'state': 'not_configured', 'reason': 'Complete the judge connection and credential settings'}
    return {'state': 'ready', 'reason': 'Configured locally; remote availability is checked only on use'}


class JudgeKit:
    def __init__(self, api_key_env=None, model=None, enabled=None,
                 timeout=8, risk_threshold=.7, approval="ask", approve=None, cache_size=256,
                 decision_log=None, url=None, gated_tools=None, credential_ref=None, credential_sources=None,
                 gate_threshold=.7, goal_threshold=.8, loop_window=4, batch_items=64, human_gate=False,
                 evaluator=None, action_gate_enabled=False, router_probes=None, batch_deadline=None,
                 completion_review_enabled=False, plan_review_enabled=False, handoff_review_enabled=False,
                 summary_review_enabled=False, lesson_review_enabled=False,
                 review_max_calls_per_turn=None, review_max_input_chars=None):
        if timeout <= 0 or not 0 <= risk_threshold <= 1 or cache_size <= 0:
            raise ValueError("Invalid Jev timeout, threshold or cache size")
        if approval not in ("ask", "never", "deny_risky", "yolo"):
            raise ValueError("Invalid approval policy")
        self.api_key_env = api_key_env if api_key_env is not None else os.environ.get("TESSERA_JEV_API_KEY_ENV", "")
        self.model = model if model is not None else os.environ.get("TESSERA_JEV_MODEL", "")
        self.url = url if url is not None else os.environ.get("TESSERA_JEV_URL", "")
        self.enabled = enabled if enabled is not None else os.environ.get("TESSERA_JEV_ENABLED", "false").lower() == "true"
        self.action_gate_enabled = action_gate_enabled
        if not 0 <= gate_threshold <= 1 or not 0 <= goal_threshold <= 1 or loop_window < 2 or batch_items <= 0:
            raise ValueError('Invalid Jev gate policy')
        self.credential_ref = credential_ref if credential_ref is not None else os.environ.get('TESSERA_JEV_CREDENTIAL_REF', '')
        self._credentials = CredentialResolver(credential_sources)
        self.gate_threshold = gate_threshold
        self.goal_threshold = goal_threshold
        self.loop_window = loop_window
        self.batch_items = batch_items
        self.human_gate = human_gate
        self.timeout = timeout
        # Total wall-clock budget for one batch, across the first attempt and its
        # single retry. Without it, a retry doubles the worst case and a slow judge
        # turns into a long stall. Default: the per-request timeout plus half again,
        # so a genuine slow answer still fits but a dead judge cannot hold a turn.
        # 0 means "derive from timeout", so a config that predates this field is
        # still valid.
        self.batch_deadline = float(batch_deadline) if batch_deadline else timeout * 1.5
        if self.batch_deadline < timeout:
            raise ValueError('Jev batch deadline must be at least one request timeout')
        self.risk_threshold = risk_threshold
        self.approval = approval
        self.approve = approve
        self.gated_tools = set(("shell", "write_file", "edit_file") if gated_tools is None else gated_tools)
        self.cache_size = cache_size
        self.decisions = []
        self._cache = OrderedDict()
        self._lock = threading.RLock()
        self.decision_log = Path(decision_log) if decision_log else None
        self.session_id = None
        # Bound by bind_session once a session exists. A router with no binding has no
        # lane, so the conscience hook stays quiet rather than guessing.
        self.session_root = None
        self.floor_slug = ''
        self.log_errors = []
        if evaluator is not None and not callable(evaluator):
            raise ValueError('Jev evaluator must be callable')
        self.evaluator = evaluator
        self._last_judge_unavailable = False
        from .config import JevConfig
        review_defaults = JevConfig()
        if review_max_calls_per_turn is None:
            review_max_calls_per_turn = review_defaults.review_max_calls_per_turn
        if review_max_input_chars is None:
            review_max_input_chars = review_defaults.review_max_input_chars
        for checkpoint, value in (('completion', completion_review_enabled), ('plan', plan_review_enabled),
                                  ('handoff', handoff_review_enabled), ('summary', summary_review_enabled),
                                  ('lesson', lesson_review_enabled)):
            if type(value) is not bool:
                raise ValueError('Jev advisory review switches must be true or false')
            setattr(self, checkpoint + '_review_enabled', value)
        if (type(review_max_calls_per_turn) is not int or not 1 <= review_max_calls_per_turn <= 64
                or type(review_max_input_chars) is not int or not 1 <= review_max_input_chars <= 1048576):
            raise ValueError('Jev advisory review bound is outside its supported range')
        self.review_max_calls_per_turn = review_max_calls_per_turn
        self.review_max_input_chars = review_max_input_chars

    @classmethod
    def from_config(cls, config, **options):
        settings = asdict(config.jev)
        settings.pop("session_gates_enabled", None)
        if config.jev.enabled and 'jev' in config.roles:
            from .judge_backend import BackendJudge
            settings['evaluator'] = BackendJudge(config.backend_for('jev'))
        settings.update(options)
        kit = cls(credential_sources=config.credentials, **settings)
        kit._credentials.oauth_home = str(config.home)
        return kit

    def bind_session(self, root, session_id=None, floor_slug=''):
        self.decision_log = Path(root) / "decisions.jsonl"
        self.session_id = session_id
        # Which thread this router belongs to. Consciousness is one lane: the floor.
        # Every other session gets the turn-end judge and nothing else.
        self.session_root = Path(root)
        self.floor_slug = floor_slug or ''
        return self

    def configured(self):
        """Whether the enabled judge has an evaluator or usable connection settings.

        This checks local configuration only; it never probes the judge.
        A configured judge may still fail remotely and use its existing fallback.
        """
        if not self.enabled:
            return False
        if self.evaluator is not None:
            check = getattr(self.evaluator, 'configured', None)
            return bool(check()) if check is not None else True
        if not (connection_endpoint(self.url) and self.model and (self.api_key_env or self.credential_ref)):
            return False
        return credential_available(self._credentials, self.credential_ref, self.api_key_env)

    def readiness(self):
        """A passive, secret-free local readiness snapshot; never a health probe."""
        if not self.enabled:
            return {'state': 'off', 'reason': 'Jev is switched off'}
        if not self.configured():
            return {'state': 'not_configured', 'reason': 'Complete the judge connection and credential settings'}
        if self._last_judge_unavailable:
            return {'state': 'unavailable', 'reason': 'The last judge request did not return valid judgments'}
        return {'state': 'ready', 'reason': 'Configured locally; remote availability is checked only on use'}

    def intake(self, message, view_tail=""):
        state = {"message": message, "view_tail": view_tail}
        questions = {
            "route": {"type": "choice", "instructions": "Choose how to handle the user's request.", "criteria": {
                "direct": "A self-contained answer without tools or external facts.",
                "tools": "Needs tools, files, retrieval, execution or uncertain facts.",
                "delegate": "The user explicitly asks for delegation or background work.",
            }},
            "needs_memory": {"type": "noul", "instructions": "Does answering require prior decisions or project/personal history?"},
            "needs_clarification": {"type": "noul", "instructions": "Is essential information missing that cannot reasonably be inferred, requiring a question before acting?"},
        }
        answers = self._batch("intake", state, questions)
        route = answers["route"]
        return {"route": route["choice"], "p": route["probabilities"][route["choice"]],
                "probabilities": route["probabilities"], "source": route["source"],
                "needs_memory": answers["needs_memory"]["noul"],
                "needs_clarification": answers["needs_clarification"]["noul"]}

    def tool_gate(self, name, arguments, cwd=None):
        state = {"name": name, "arguments": arguments, "cwd": cwd}
        questions = {"risk": {"type": "noul", "instructions": "Could this tool destroy or modify data, write outside the working directory, execute unknown code or create externally visible effects?"}}
        answer = self._batch("tool_gate", state, questions)["risk"]
        return {"risk": answer["noul"], "source": answer["source"]}

    def verify_output(self, request, reply, tool_outputs=None):
        state = {"request": request, "reply": reply, "tool_outputs": tool_outputs or []}
        questions = {
            "completion": {"type": "score", "instructions": "How completely does the reply accomplish the request, based on the supplied tool evidence? Unsupported claims are not proof.",
                           "criteria": ["Failed or unanswered", "Partial accomplishment", "Fully accomplished"]},
            "scope_creep": {"type": "noul", "instructions": "Did the agent change or do things outside the user's requested scope?"},
            "claims_supported": {"type": "noul", "instructions": "Are factual action/completion claims supported by tool_outputs or the supplied request? Treat absent evidence for a claimed action as unsupported."},
            "claims_done_but_failed": {"type": "noul", "instructions": "Does the reply claim completion or passing tests while tool_outputs report failure, blocking or errors?"},
        }
        answers = self._batch("verification", state, questions)
        result = {"completion": answers["completion"]["score"] / 2}
        for name in ("scope_creep", "claims_supported", "claims_done_but_failed"):
            result[name] = answers[name]["noul"]
        result["source"] = self._sources(answers)
        return result

    def memory(self, message, reply="", tool_outputs=None, verification=None, existing_layers=None):
        state = {"message": message, "reply": reply, "tool_outputs": tool_outputs or [], "verification": verification or {}}
        questions = {
            "keep": {"type": "noul", "instructions": "Is this turn useful lasting memory rather than a greeting or acknowledgement?"},
            "layer_type": {"type": "choice", "instructions": "Choose the most useful memory category for this turn.", "criteria": {
                "decision": "An agreed choice or plan", "correction": "The user corrects prior understanding",
                "fact": "A useful observation", "failure": "An unsuccessful action and its outcome",
                "preference": "A lasting user preference",
            }},
            "is_correction": {"type": "noul", "instructions": "Does the user correct the agent's prior understanding or behavior?"},
        }
        if existing_layers is not None:
            if len(existing_layers) > self.batch_items:
                raise ValueError('Memory candidate batch exceeds configured bound')
            state['existing_layers'] = existing_layers
            questions['duplicate_of_existing_layer'] = {'type': 'noul', 'instructions':
                'Does this turn repeat an existing layer without adding a decision, correction or useful new evidence?'}
        answers = self._batch("memory", state, questions)
        result = {"keep": answers["keep"]["noul"], "layer_type": answers["layer_type"]["choice"],
                "is_correction": answers["is_correction"]["noul"], "source": self._sources(answers)}
        if existing_layers is not None:
            result['duplicate_of_existing_layer'] = answers['duplicate_of_existing_layer']['noul']
        return result

    def _looping(self, steps):
        if len(steps) < self.loop_window:
            return False
        window = steps[-self.loop_window:]
        serialized = []
        for step in window:
            serialized.append(json.dumps(step, sort_keys=True, ensure_ascii=False))
        return len(set(serialized)) == 1

    def _gate_result(self, values, *, phase, human=None):
        result = dict(values)
        allowed = (result['independent'] >= self.gate_threshold
                   and result['merge_conflict'] < self.gate_threshold
                   and result['looping'] < self.gate_threshold)
        if phase in ('merge', 'completion', 'session'):
            allowed = allowed and result['goal_met'] >= self.goal_threshold
        needs_human = self.human_gate or result['escalate_to_human'] >= self.gate_threshold
        result['requires_human'] = needs_human
        if allowed and needs_human and human is not None:
            try:
                approved = human(copy.deepcopy(result)) is True
            except Exception:
                approved = False
            result['requires_human'] = not approved
        result['allowed'] = bool(allowed and not result['requires_human'])
        return result

    def _record_gate(self, point, state, result):
        record = {'point': point, 'kind': point, 'question_id': '_gate', 'model_id': result['source'],
                  'batch_id': uuid.uuid4().hex, 'text_hash': hashlib.sha256(
                      json.dumps(state, sort_keys=True, ensure_ascii=False).encode()).hexdigest(),
                  'at': datetime.now(timezone.utc).isoformat(), 'source': result['source'],
                  'allowed': result['allowed'], 'requires_human': result['requires_human']}
        for name in ('independent', 'merge_conflict', 'goal_met', 'looping', 'escalate_to_human', 'needs_reply', 'ask_human', 'needs_authority'):
            if name in result:
                record[name] = result[name]
        with self._lock:
            self.decisions.append(record)
            if len(self.decisions) > self.cache_size:
                del self.decisions[0]
            try:
                self._log_decision(record, '')
            except OSError as error:
                self.log_errors.append(type(error).__name__)
        return result

    def swarm_gates(self, objective, *, tasks=None, artifacts=None, steps=None, checks=None,
                    phase='completion', human=None):
        if phase not in ('admission', 'merge', 'completion', 'session'):
            raise ValueError('Unknown swarm gate phase')
        tasks, artifacts, steps = tasks or [], artifacts or [], steps or []
        if len(tasks) * max(0, len(tasks) - 1) // 2 + 4 > self.batch_items:
            raise ValueError('Swarm gate batch exceeds configured bound')
        state = {'objective': objective, 'tasks': tasks, 'artifacts': artifacts,
                 'steps': steps[-self.loop_window:], 'checks': checks or {}}
        conflict = conflicting_tasks(tasks)
        if failed_checks(checks) or conflict or self._tools_failed(artifacts) or self._looping(steps):
            result = {'independent': 0 if conflict else 1, 'merge_conflict': 1 if conflict else 0,
                      'goal_met': 0, 'looping': float(self._looping(steps)), 'escalate_to_human': 1,
                      'allowed': False, 'requires_human': True, 'source': 'deterministic'}
            return self._record_gate('swarm_gate', state, result)
        questions = {}
        for index in range(len(tasks)):
            for other in range(index + 1, len(tasks)):
                questions[f'independent_{index}_{other}'] = {'type': 'noul', 'instructions':
                    f'Can tasks {index} and {other} proceed independently without relying on each others unfinished work?'}
        questions.update({
            'merge_conflict': {'type': 'noul', 'instructions': 'Do the actual artifacts or stated constraints conflict? Use artifact content, not reports claiming success.'},
            'goal_met': {'type': 'score', 'instructions': 'Does the supplied artifact evidence and checks accomplish the objective? Missing evidence is not success.',
                         'criteria': ['Not accomplished', 'Partial or unproven', 'Accomplished with evidence']},
            'looping': {'type': 'noul', 'instructions': 'Do recent steps repeat without useful progress?'},
            'escalate_to_human': {'type': 'noul', 'instructions': 'Does work require human authority or unresolved judgment rather than another autonomous attempt?'},
        })
        answers = self._batch('swarm', state, questions)
        independent = 1.0
        for name, answer in answers.items():
            if name.startswith('independent_'):
                independent = min(independent, answer['noul'])
        values = {'independent': independent, 'goal_met': answers['goal_met']['score'] / 2,
                  'merge_conflict': answers['merge_conflict']['noul'], 'looping': answers['looping']['noul'],
                  'escalate_to_human': answers['escalate_to_human']['noul'], 'source': self._sources(answers)}
        if phase in ('merge', 'completion'):
            material = bool(artifacts)
            for artifact in artifacts:
                if not artifact.get('content'):
                    material = False
            if not material:
                values['goal_met'] = min(values['goal_met'], .5)
        result = self._gate_result(values, phase=phase, human=human)
        return self._record_gate('swarm_gate', state, result)

    def ask_human_gate(self, question, *, context=None, required=False):
        if not isinstance(question, str) or not question.strip() or type(required) is not bool:
            raise ValueError('Ask-human gate requires a question and boolean requirement')
        state = {'question': question, 'context': context or {}}
        if required:
            result = {'ask_human': 1, 'needs_authority': 1, 'source': 'deterministic',
                      'allowed': False, 'requires_human': True}
            return self._record_gate('ask_human_gate', state, result)
        questions = {
            'ask_human': {'type': 'noul', 'instructions': 'Does this question require a useful operator decision or essential missing information, rather than something the agent can resolve from its evidence?'},
            'needs_authority': {'type': 'noul', 'instructions': 'Does proceeding require human permission, account access or authority the agent does not have?'},
        }
        answers = self._batch('ask_human_gate', state, questions)
        return {'ask_human': answers['ask_human']['noul'], 'needs_authority': answers['needs_authority']['noul'],
                'source': self._sources(answers)}

    def message_gate(self, text, *, hop, max_hops):
        if type(hop) is not int or type(max_hops) is not int or hop < 0 or max_hops <= 0:
            raise ValueError('Message gate requires nonnegative hop and positive limit')
        state = {'text': text, 'hop': hop, 'max_hops': max_hops}
        acknowledgment = re.fullmatch(
            r'\s*(thanks|thank you|received|acknowledged|ok|okay|understood)'
            r'(?:[\s,.!]+(?:received|thanks|understood|ok|okay))*[\s,.!]*', text.lower())
        if hop >= max_hops or acknowledgment:
            result = {'needs_reply': 0, 'looping': 1 if hop >= max_hops else 0,
                      'source': 'deterministic', 'allowed': False, 'requires_human': False}
            return self._record_gate('message_gate', state, result)
        questions = {
            'needs_reply': {'type': 'noul', 'instructions': 'Does this inbound work message need a useful response or action, rather than another acknowledgment or unsolicited bot chatter?'},
            'looping': {'type': 'noul', 'instructions': 'Would replying to this message continue a conversational loop without useful progress?'},
        }
        answers = self._batch('message_gate', state, questions)
        return {'needs_reply': answers['needs_reply']['noul'], 'looping': answers['looping']['noul'],
                'source': self._sources(answers)}

    def output_gate(self, request, reply, tool_outputs=None, *, checks=None, verification=None, human=None):
        outputs = tool_outputs or []
        state = {'request': request, 'reply': reply, 'tool_outputs': outputs, 'checks': checks or {}}
        if failed_checks(checks) or self._tools_failed(outputs):
            result = {'allowed': False, 'requires_human': False, 'goal_met': 0, 'source': 'deterministic'}
            return self._record_gate('output_gate', state, result)
        verification = verification or self.verify_output(request, reply, outputs)
        values = {'independent': 1, 'merge_conflict': verification['scope_creep'],
                  'goal_met': verification['completion'], 'looping': 0,
                  'escalate_to_human': verification['claims_done_but_failed'], 'source': verification['source']}
        if verification['claims_supported'] < self.gate_threshold:
            values['goal_met'] = 0
        result = self._gate_result(values, phase='session', human=human)
        return self._record_gate('output_gate', state, result)

    def verify_outputs(self, items):
        if len(items) * 4 > self.batch_items:
            raise ValueError('Output batch exceeds configured bound')
        normalized = []
        for item in items:
            normalized.append({**item, 'tool_outputs': item.get('tool_outputs') or []})
        items = normalized
        questions = {}
        descriptions = {
            'completion': {'type': 'score', 'instructions': 'How completely does this reply accomplish its request based on actual evidence?',
                           'criteria': ['Failed', 'Partial', 'Accomplished']},
            'scope_creep': {'type': 'noul', 'instructions': 'Did this output exceed the requested scope?'},
            'claims_supported': {'type': 'noul', 'instructions': 'Do actual tool outputs support this reply?'},
            'claims_done_but_failed': {'type': 'noul', 'instructions': 'Does this reply claim success despite failed checks?'},
        }
        blocked = set()
        for index, item in enumerate(items):
            if failed_checks(item.get('checks')) or self._tools_failed(item.get('tool_outputs', [])):
                blocked.add(index)
                continue
            for name, question in descriptions.items():
                questions[f'{index}_{name}'] = {**question, 'instructions': f'For items[{index}]: ' + question['instructions']}
        answers = self._batch('verification_batch', {'items': items}, questions) if questions else {}
        results = []
        for index, item in enumerate(items):
            if index in blocked:
                result = {'completion': 0, 'scope_creep': 0, 'claims_supported': 0,
                          'claims_done_but_failed': 1, 'source': 'deterministic'}
            else:
                selected = {}
                for name in descriptions:
                    selected[name] = answers[f'{index}_{name}']
                result = {'completion': selected['completion']['score'] / 2, 'source': self._sources(selected)}
                for name in ('scope_creep', 'claims_supported', 'claims_done_but_failed'):
                    result[name] = selected[name]['noul']
            result['allowed'] = self.output_gate(item['request'], item['reply'], item.get('tool_outputs'),
                                                 checks=item.get('checks'), verification=result)['allowed']
            results.append(result)
        return results

    def faithfulness(self, source, summary, **context):
        state = {"source": source, "summary": summary, "context": context}
        questions = {"faithfulness": {"type": "noul", "instructions": "Is the summary faithful to source, retaining its important user decisions, constraints and negations without inventing claims?"}}
        return self._batch("compaction", state, questions)["faithfulness"]["noul"]

    def compaction_judge(self, summary, source):
        """Adapter to Compactor's summary-first callback contract."""
        return self.faithfulness(source, summary)

    @staticmethod
    def _sources(answers):
        sources = set()
        for answer in answers.values():
            sources.add(answer["source"])
        return next(iter(sources)) if len(sources) == 1 else "mixed"

    def route(self, message, view_tail=""):
        state = {"message": message, "view_tail": view_tail}
        question = {"type": "choice", "instructions": "Choose how to handle the user's request.", "criteria": {
            "direct": "A self-contained answer needing no tools or outside facts.",
            "tools": "Use the main backend with tools for files, commands, retrieval or uncertain work.",
            "delegate": "The user explicitly requests delegation or an independent background task.",
        }}
        result = self._decide("route", state, question)
        choice = result["choice"]
        return {"route": choice, "p": result["probabilities"][choice],
                "probabilities": result["probabilities"], "source": result["source"]}

    def needs_memory(self, message):
        question = {"type": "noul", "instructions": "Does answering require recalling prior decisions, history or personal/project memory?"}
        return self._decide("needs_memory", message, question)["noul"]

    def tool_risk(self, command):
        question = {"type": "noul", "instructions": "Could this shell command destroy or modify data, change permissions, run unknown code, or create an externally visible effect?"}
        return self._decide("tool_risk", command, question)["noul"]

    def judge(self, request, reply):
        question = {"type": "score", "instructions": "How completely does the reply accomplish the request? Do not treat unsupported claims as evidence.",
                    "criteria": ["Fails or does not answer the request", "Partially accomplishes the request", "Fully accomplishes the request"]}
        decision = self._decide("judge", {"request": request, "reply": reply}, question)
        return decision["score"] / (len(question["criteria"]) - 1)

    def _decide(self, kind, state, question):
        return self._batch(kind, state, {"decision": question})["decision"]

    def _batch(self, point, state, questions, *, strict=False, registry=None):
        if registry is None:
            registry = getattr(self, 'cancellation_registry', None)
        use_backend = bool(self.enabled and self.evaluator is not None and self.configured())
        requested_model = '' if use_backend else self.model
        serialized = json.dumps({"point": point, "state": state, "questions": questions, "model": requested_model},
                                ensure_ascii=False, sort_keys=True, allow_nan=False)
        text_hash = hashlib.sha256(serialized.encode("utf-8")).hexdigest()
        configured = self.configured() and not use_backend and self.url and self.model and (self.api_key_env or self.credential_ref)
        key = ''
        if configured:
            try:
                key = self._credentials.resolve(self.credential_ref, env=self.api_key_env).value
            except CredentialError:
                self._last_judge_unavailable = True
        key_hash = hashlib.sha256(key.encode()).hexdigest() if key else ''
        cache_key = (text_hash, self.url, self.api_key_env, self.credential_ref, key_hash, strict,
                     bool(self.enabled), id(self.evaluator))
        with self._lock:
            cached = self._cache.get(cache_key)
            started = time.monotonic()
            cache_hit = bool(cached and cached[0] > started)
            if cache_hit:
                self._cache.move_to_end(cache_key)
                results, models = copy.deepcopy(cached[1])
            else:
                envelope = {}
                results = {}
                models = {}
                if use_backend or key:
                    try:
                        if use_backend:
                            envelope = self.evaluator(state, questions)
                        elif registry is not None:
                            envelope = self._evaluate_batch(state, questions, key, registry=registry)
                        else:
                            envelope = self._evaluate_batch(state, questions, key)
                    except (BackendError, OSError, http.client.HTTPException, ValueError, KeyError, TypeError, OverflowError):
                        pass
                if registry is not None:
                    registry.check_cancelled()
                for name, question in questions.items():
                    answer = None
                    if envelope:
                        try:
                            answer = self._validate(envelope["answers"][name], question)
                        except (ValueError, KeyError, TypeError, OverflowError):
                            pass
                    if answer is None:
                        if strict:
                            answer = {"type": "noul", "noul": 0.5, "source": "unavailable"}
                            models[name] = "unavailable"
                        else:
                            answer = self._fallback_question(point, name, state, question)
                            answer["source"] = "heuristic"
                            models[name] = "heuristic"
                    else:
                        answer["source"] = "jev"
                        model = envelope.get("model", requested_model)
                        models[name] = model if isinstance(model, str) else requested_model
                    results[name] = answer
                ttl = 5 if (use_backend or key) and self._sources(results) != "jev" else 300
                if use_backend or key:
                    self._last_judge_unavailable = self._sources(results) != 'jev'
                self._cache[cache_key] = (time.monotonic() + ttl, copy.deepcopy((results, models)))
                while len(self._cache) > self.cache_size:
                    self._cache.popitem(last=False)
            elapsed = (time.monotonic() - started) * 1000
            batch_id = uuid.uuid4().hex
            for name, answer in results.items():
                record = {"point": point, "kind": point, "question_id": name, "model_id": models[name],
                          "requested_model": requested_model, "text_hash": text_hash, "batch_id": batch_id,
                          "transport": "backend" if use_backend else "typesafe" if key else "heuristic",
                          "latency_ms": elapsed, "cache_hit": cache_hit, "session_id": self.session_id,
                          "at": datetime.now(timezone.utc).isoformat(), **copy.deepcopy(answer)}
                if key:
                    for field in ("model_id", "requested_model", "session_id"):
                        if isinstance(record[field], str):
                            record[field] = record[field].replace(key, "[REDACTED]")
                if "probabilities" not in record:
                    probability = record["noul"]
                    record["probabilities"] = {"true": probability, "false": 1 - probability}
                self.decisions.append(record)
                if len(self.decisions) > self.cache_size:
                    del self.decisions[0]
                try:
                    self._log_decision(record, key)
                except OSError as error:
                    self.log_errors.append(type(error).__name__)
            return results

    def _log_decision(self, record, key):
        if self.decision_log is None:
            return
        self.decision_log.parent.mkdir(parents=True, exist_ok=True)
        text = json.dumps(record, ensure_ascii=False, allow_nan=False)
        if key:
            text = text.replace(key, "[REDACTED]")
        with self.decision_log.open("a", encoding="utf-8") as stream:
            fcntl.flock(stream, fcntl.LOCK_EX)
            try:
                stream.write(text + "\n")
                stream.flush()
                os.fsync(stream.fileno())
            finally:
                fcntl.flock(stream, fcntl.LOCK_UN)

    def _evaluate(self, state, question, key):
        return self._evaluate_batch(state, {"decision": question}, key)["answers"]["decision"]

    def _evaluate_batch(self, state, questions, key, registry=None):
        body = json.dumps({"state": state, "model": self.model, "questions": questions},
                          ensure_ascii=False, allow_nan=False).encode("utf-8")
        request = urllib.request.Request(self.url, data=body, method="POST", headers={
            "Content-Type": "application/json", "Authorization": "Bearer " + key,
        })
        if len(body) > 60000:
            request.add_header('TypeSafe-Danger-Zone-Long-Context', 'true')
        # One bounded retry, and only for a transport failure. A slow judge and a
        # dead judge look identical on the first timeout, and telling a session
        # "inspect or repair the judge" when the judge merely answered in 8.1s is a
        # false instruction that blocks every mutating tool for that call. Whole
        # batches once stopped at ~8.07-8.15s against an 8s timeout and were
        # recorded with source='unavailable'. The retry is capped by
        # batch_deadline so a genuinely absent judge still fails fast enough for the
        # session to act on it.
        deadline = time.monotonic() + self.batch_deadline
        last_transport_failure = None
        for attempt in range(2):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            request_timeout = min(self.timeout, remaining)
            try:
                return self._read_judge(request, request_timeout, registry)
            except TRANSPORT_FAILURES as error:
                last_transport_failure = error
                if attempt == 0 and registry is not None:
                    registry.check_cancelled()
        if last_transport_failure is not None:
            raise last_transport_failure

    def _read_judge(self, request, timeout, registry):
        if registry is not None:
            transport = CancellableHTTP()
            transport.cancel_event = registry.cancelled
            try:
                with registry.cancellation(transport.cancel):
                    with transport.opener.open(request, timeout=timeout) as response:
                        transport.response = response
                        transport.check_cancelled()
                        raw = response.read(1024 * 1024 + 1)
                        transport.check_cancelled()
                        if len(raw) > 1024 * 1024:
                            raise ValueError('TypeSafe response too large')
                        return json.loads(raw)
            finally:
                transport.finish()
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                raw = response.read(1024 * 1024 + 1)
                if len(raw) > 1024 * 1024:
                    raise ValueError("TypeSafe response too large")
                return json.loads(raw)
        except urllib.error.HTTPError as error:
            error.close()
            raise ValueError("TypeSafe HTTP failure") from None

    @staticmethod
    def _number(value, high=1):
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError("Invalid numeric decision")
        if not math.isfinite(value) or not 0 <= value <= high:
            raise ValueError("Decision outside range")
        return float(value)

    def _validate(self, answer, question):
        kind = question["type"]
        if answer["type"] != kind:
            raise ValueError("TypeSafe answer has wrong type")
        if kind == "noul":
            return {"noul": self._number(answer["noul"])}
        probabilities = answer["probabilities"]
        if not isinstance(probabilities, dict) or not probabilities:
            raise ValueError("TypeSafe answer lacks probabilities")
        total = 0
        for probability in probabilities.values():
            total += self._number(probability)
        if abs(total - 1) > .02:
            raise ValueError("TypeSafe probabilities do not sum to one")
        confidence = self._number(answer.get("confidence", 0))
        if kind == "choice":
            choice = answer["choice"]
            if set(probabilities) != set(question["criteria"]) or choice not in question["criteria"]:
                raise ValueError("TypeSafe returned an unknown choice")
            return {"choice": choice, "probabilities": probabilities, "confidence": confidence}
        high = len(question["criteria"]) - 1
        expected = set()
        for index in range(high + 1):
            expected.add(str(index))
        if set(probabilities) != expected:
            raise ValueError("TypeSafe score has invalid levels")
        return {"score": self._number(answer["score"], high), "probabilities": probabilities,
                "confidence": confidence}

    def _fallback(self, kind, state):
        if kind == "route":
            message = state["message"].lower()
            route = "direct"
            if re.search(r"\b(delegate|subagent|background task)\b", message):
                route = "delegate"
            elif self._workspace_request(message) or re.search(r"\b(file|files|read|write|edit|shell|run|test|search|browse|inspect|memory|recall|remember|directory|folder|tool|tools|fix|implement|build|create|date|time|weather|latest|today|current|repo|repos|repository|codebase|code|audit|source)\b", message):
                route = "tools"
            elif self._memory_probability(message) > .5:
                route = "tools"
            probabilities = {"direct": .1, "tools": .1, "delegate": .1}
            probabilities[route] = .8
            return {"choice": route, "probabilities": probabilities, "confidence": .5}
        if kind == "needs_memory":
            return {"noul": self._memory_probability(state)}
        if kind == "tool_risk":
            return {"noul": self._command_risk(state)}
        request = state["request"].lower()
        reply = state["reply"].lower()
        score = 1.3
        if not reply.strip():
            score = 0
        elif re.search(r"\b(unable|cannot|failed|failure|error|stopped|blocked)\b", reply):
            score = .2
        else:
            words = set(re.findall(r"\w{4,}", request))
            matches = 0
            for word in words:
                if word in reply:
                    matches += 1
            if words and matches / len(words) >= .3:
                score = 1.8
        # Heuristic scoring is an advisory text signal, never proof of task success.
        return {"score": score, "probabilities": {"0": 1 - score / 2, "1": 0, "2": score / 2}, "confidence": .25}

    def _fallback_question(self, point, name, state, question):
        if name == "decision":
            return self._fallback(point, state)
        if point == 'ask_human_gate':
            authority = bool(re.search(r'\b(approval|permission|authorize|authority|credential|access)\b', state['question'].lower()))
            if name == 'needs_authority':
                return {'noul': .95 if authority else .1}
            trivial = bool(re.fullmatch(r'\s*(hi|hello|thanks|what is \d+\s*[+*/-]\s*\d+)[.!?\s]*', state['question'].lower()))
            if trivial:
                return {'noul': .1}
            if authority:
                return {'noul': .9}
            return {'noul': .8}
        if point == 'message_gate':
            return {'noul': .8 if name == 'needs_reply' else .05}
        if point == 'swarm':
            looping = self._looping(state.get('steps', []))
            if name.startswith('independent_'):
                return {'noul': .9}
            if name == 'merge_conflict':
                conflict = any(item.get('conflict') is True for item in state['artifacts'])
                return {'noul': .95 if conflict else .1}
            if name == 'goal_met':
                proven = bool(state['checks']) and all(state['checks'].values()) and bool(state['artifacts'])
                for artifact in state['artifacts']:
                    if not artifact.get('content'):
                        proven = False
                score = 1.8 if proven else 1.0
                return {'score': score, 'probabilities': {'0': 1 - score / 2, '1': 0, '2': score / 2}, 'confidence': .25}
            return {'noul': .95 if looping else .05}
        if point == 'verification_batch':
            index, original = name.split('_', 1)
            return self._fallback_question('verification', original, state['items'][int(index)], question)
        if point == "intake":
            if name == "route":
                return self._fallback("route", state)
            if name == "needs_memory":
                return self._fallback("needs_memory", state["message"])
            ambiguous = re.fullmatch(r"\s*(do it|fix that|make it better)[.!?\s]*", state["message"].lower())
            return {"noul": .9 if ambiguous else .1}
        if point == "tool_gate":
            tool = state["name"]
            risk = .1
            if tool == "shell":
                risk = self._command_risk(state["arguments"].get("command", ""))
            elif tool in ("write_file", "edit_file", "send_to_session", "memory_note"):
                risk = .9
            return {"noul": risk}
        if point == "verification":
            failed = self._tools_failed(state["tool_outputs"])
            claimed = bool(re.search(r"\b(done|fixed|passed|created|deleted|sent|updated|saved|wrote|completed)\b", state["reply"].lower()))
            if name == "completion":
                result = self._fallback("judge", state)
                if failed and claimed:
                    result = {"score": 0.0, "probabilities": {"0": 1, "1": 0, "2": 0}, "confidence": .5}
                return result
            if name == "claims_done_but_failed":
                return {"noul": .95 if failed and claimed else .05}
            if name == "claims_supported":
                unsupported = claimed and (failed or not state["tool_outputs"])
                return {"noul": .1 if unsupported else .9}
            return {"noul": .1}
        if point == "memory":
            message = state["message"].lower()
            correction = bool(re.search(r"\b(actually|correction|instead|wrong|misunderstood)\b|\bno[,!]", message))
            category = "fact"
            if correction:
                category = "correction"
            elif re.search(r"\b(prefer|always|never|favorite)\b", message):
                category = "preference"
            elif self._tools_failed(state["tool_outputs"]):
                category = "failure"
            elif re.search(r"\b(decide|decision|agreed|choose|chose)\b", message):
                category = "decision"
            if name == 'duplicate_of_existing_layer':
                duplicate = False
                for layer in state.get('existing_layers', []):
                    body = layer.get('body', '') if isinstance(layer, dict) else str(layer)
                    if message.strip() == body.strip().lower():
                        duplicate = True
                        break
                return {'noul': .95 if duplicate and not correction else .05}
            if name == "is_correction":
                return {"noul": .95 if correction else .05}
            if name == "keep":
                greeting = bool(re.fullmatch(r"\s*(hi|hello|thanks|thank you)[.!\s]*", message))
                return {"noul": .1 if greeting else .9 if category != "fact" else .6}
            probabilities = {}
            for option in question["criteria"]:
                probabilities[option] = .92 if option == category else .02
            return {"choice": category, "probabilities": probabilities, "confidence": .5}
        if point == "compaction":
            source = str(state["source"]).lower()
            summary = str(state["summary"]).lower()
            source_numbers = set(re.findall(r"\b\d+\b", source))
            summary_numbers = set(re.findall(r"\b\d+\b", summary))
            if not summary.strip() or not summary_numbers.issubset(source_numbers):
                return {"noul": .1}
            negations = ("never", "must not", "do not")
            for word in negations:
                if word in source and word not in summary:
                    return {"noul": .2}
            return {"noul": .8}
        raise ValueError("Unknown JudgeKit decision point")

    @staticmethod
    def _tools_failed(outputs):
        for output in outputs:
            if isinstance(output, dict):
                if output.get("success") is False or output.get("blocked") or output.get("error"):
                    return True
                if output.get("exit_code", 0) not in (0, None):
                    return True
                nested = output.get("result")
                if isinstance(nested, dict) and JudgeKit._tools_failed([nested]):
                    return True
        return False

    @staticmethod
    def _workspace_request(message):
        """The answer is the working tree: an inspection or a list of changes.

        That is what the tools route is for. It is separate from the bag of
        words that also covers the date and the weather.
        """
        text = message.lower()
        if re.search(r"\bwhat\s+(?:needs|should|must)\s+(?:to\s+be\s+|be\s+)?chang\w*", text):
            return True
        if re.search(r"\bchanges?\s+worth\b", text):
            return True
        if re.search(r"\b(?:list|find|show)\b(?:\s+\w+){0,6}\s+changes?\b", text):
            return True
        if re.search(r"\bshakedown\b", text):
            return True
        tree = r"(?:checkout|repo|repos|repository|codebase|code|source|tree|files?|directory|folder)"
        if re.search(r"\b(?:audit|review|inspect)\b", text) and re.search(rf"\b{tree}\b", text):
            return True
        return bool(re.search(r"\b(?:this|the)\s+checkout\b", text))

    @staticmethod
    def _memory_probability(message):
        if re.search(r"\b(remember|recall|memory|history|earlier|previous|previously|decide|decided|decision|again)\b|\blast time\b", message.lower()):
            return .9
        return .1

    @staticmethod
    def _command_risk(command):
        if re.search(r"[;|&><`\n]|\$\(", command):
            return .95
        try:
            parts = shlex.split(command)
        except ValueError:
            return 1.0
        if not parts:
            return 0.0
        name = os.path.basename(parts[0])
        safe = {"pwd", "ls", "cat", "head", "tail", "wc", "rg", "grep", "date", "echo", "true", "false"}
        if name in safe:
            # rg/grep preprocessors and output options can execute code or write files.
            for part in parts[1:]:
                if part.startswith(("--pre", "--hostname-bin", "--output", "--file=")):
                    return .9
            return .1
        if name == "git" and len(parts) > 1 and parts[1] in ("status", "diff", "log", "show"):
            return .2
        return .9

    def install(self, hookbus):
        hookbus.register("turn_start", self.turn_start)
        hookbus.register("pre_tool", self.pre_tool)
        hookbus.register("pre_tool", self.conscience_hook)

    def conscience_hook(self, payload):
        """Cloud Jev on a pressure wake's tool. A clear no blocks. Anything else proceeds.

        Consciousness is one lane: the floor thread. A normal session gets the turn-end
        judge, not a per-tool conscience call. This hook also reads the pressure from the
        turn that is running, not from any earlier line still sitting in the log; the old
        scan kept firing on a stale pressure line and wrote a note for every tool call.
        """
        if self.action_gate_enabled:
            return None
        name = payload.get("name") or payload.get("tool") or ""
        arguments = payload.get("arguments") or payload.get("args") or {}
        settings = getattr(self, "computer_use_jev", None)
        if settings is None or not getattr(settings, "enabled", False):
            return None
        if not self.floor_slug:
            return None
        root = getattr(self, "session_root", None)
        if root is None or Path(root).name != self.floor_slug:
            return None
        log = getattr(self, "chat_log", None)
        if log is None:
            return None
        pressure = ""
        current = ""
        for item in log.iter():
            if getattr(item, "kind", "") == "user":
                current = getattr(item, "text", "") or ""
        if "Salience" in current and "Pressure:" in current:
            pressure = current
        if not pressure:
            return None
        from .cua_jev import api_key, conscience
        try:
            key = api_key(settings, getattr(self, "credentials", None))
            verdict = conscience(pressure, name, arguments, settings, key)
        except Exception as error:
            return {"conscience": True, "verdict": "abstain", "reason": type(error).__name__}
        if verdict.get("verdict") == "no":
            return {"conscience": True, "block": True, "reason": verdict.get("reason") or "cloud Jev said no",
                    "probability": verdict.get("probability")}
        return {"conscience": True, "verdict": verdict.get("verdict"), "probability": verdict.get("probability"),
                "reason": verdict.get("reason") or ""}


    def turn_start(self, payload):
        message = payload.get("message", payload.get("text", ""))
        decision = self.intake(message, payload.get("view_tail", ""))
        payload["routing"] = decision
        payload["needs_memory"] = decision["needs_memory"]
        payload["needs_clarification"] = decision["needs_clarification"]
        return {**decision, "needs_memory": payload["needs_memory"],
                "context": "Routing decision: " + json.dumps(decision, sort_keys=True)}

    def pre_tool(self, payload):
        if self.action_gate_enabled:
            # The contextual action gate runs at the execution owner. The registry
            # continues to own the operator's ordinary approval policy.
            return None
        name = payload.get("name", payload.get("tool", ""))
        arguments = payload.get("arguments", payload.get("args", {}))
        if name == "shell" and "command" not in arguments:
            arguments = {**arguments, "command": payload.get("command", "")}
        decision = self.tool_gate(name, arguments, payload.get("cwd"))
        risk = decision["risk"]
        payload["tool_risk"] = risk
        policy = payload.get("approval_policy", payload.get("policy", self.approval))
        threshold = payload.get("risk_threshold", self.risk_threshold)
        result = {**decision, "threshold": threshold}
        # A session's live ToolRegistry owns operator approval and deferral.
        # Keep the risk observation; do not apply a second, stale policy here.
        if payload.get('approval_managed') is True:
            return result
        if name not in self.gated_tools:
            return result
        if risk < threshold or policy in ("never", "yolo"):
            return result
        approved = payload.get("approved") is True
        if policy == "ask" and not approved and self.approve is not None:
            approved = bool(self.approve(name, arguments))
        if policy == "ask" and approved:
            return result
        return {**result, "block": True, "requires_approval": policy == "ask",
                "reason": f"Tool risk {risk:.2f} meets threshold {threshold:.2f} (policy: {policy})"}


class JevRouter(JudgeKit):
    """Compatibility name for the shared router and judgment kit."""


def calibration_report(records, outcomes, *, threshold=.5):
    """Join labels by (batch_id, question_id), never infer labels from model claims."""
    if not 0 <= threshold <= 1:
        raise ValueError('Calibration threshold must be a probability')
    groups = {}
    seen = set()
    for record in records:
        identity = (record.get('batch_id'), record.get('question_id'))
        if identity in seen or identity not in outcomes:
            continue
        seen.add(identity)
        outcome = outcomes[identity]
        if type(outcome) is not bool:
            raise ValueError('Calibration outcomes must be explicitly labeled booleans')
        if 'noul' in record:
            probability = JudgeKit._number(record['noul'])
        elif 'choice' in record:
            probability = JudgeKit._number(record['probabilities'][record['choice']])
        elif 'score' in record:
            high = len(record['probabilities']) - 1
            probability = JudgeKit._number(record['score'], high) / high
        else:
            continue
        group = groups.setdefault(identity[1], {'n': 0, 'squared_error': 0, 'positive': 0, 'true_positive': 0})
        group['n'] += 1
        group['squared_error'] += (probability - int(outcome)) ** 2
        if probability >= threshold:
            group['positive'] += 1
            group['true_positive'] += int(outcome)
    result = {}
    for question, group in groups.items():
        precision = group['true_positive'] / group['positive'] if group['positive'] else None
        result[question] = {'n': group['n'], 'brier': group['squared_error'] / group['n'], 'precision': precision}
    return result
