"""Model-independent turn capture and a durable, retryable memory write outbox."""

from . import locking as fcntl
from .durability import sync_directory
import json
import re
import os
import threading
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from .aethermind import CaptureRejected, drop_superseded, tree_read_display
from .jev import JudgeKit
from .mcp_client import McpError
from .tools import clip_records, drop_inactive_chains, present_inheritance

_CLASS_NAME = re.compile(r"^[A-Za-z][A-Za-z0-9]{0,40}$")


def _category_for_text(head, text):
    lowered = text.lower()
    if "http 401" in lowered or "http 403" in lowered or "authentication" in lowered:
        return "authentication"
    if head == "TimeoutError" or any(
            phrase in lowered for phrase in (
                "timeouterror", "timed out", "timeout after", "deadline")):
        return "timeout"
    if head in ("OSError", "ConnectionError", "ConnectionRefusedError", "EndpointUnavailable", "URLError") or any(
            phrase in lowered for phrase in (
                "not reachable", "endpoint is reachable", "transport", "connection refused")):
        return "unreachable"
    return "tool"


def _category_for(error):
    return _category_for_text(type(error).__name__, str(error))


def service_error_name(label):
    if isinstance(label, str) and _CLASS_NAME.fullmatch(label):
        return label
    return None


def _returned_error(value):
    if not isinstance(value, dict) or value.get("state") == "no_tree" or not value.get("error"):
        return False
    text = value.get("error")
    if not isinstance(text, str):
        return True
    lowered = text.strip().lower()
    return not (lowered.startswith("no summary tree") or "run build_tree first" in lowered)


def _now():
    return datetime.now(timezone.utc).isoformat()


def _text(value):
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)



NOTE_TYPES = ("note", "decision", "correction", "lock", "lesson")


def _result_as_dict(result):
    """A tool result as a dict. MCP tools often return their JSON object as text."""
    if isinstance(result, dict):
        return result
    if not isinstance(result, str):
        return result
    text = result.strip()
    if not text.startswith("{"):
        return result
    try:
        parsed = json.loads(text)
    except ValueError:
        return result
    if isinstance(parsed, dict):
        return parsed
    return result

class Capture:
    def __init__(self, client, outbox_dir, project_id, personal_project="", author="tessera",
                 anchor=None, inheritance_budget=4000, personal_budget=2000, secret_envs=None,
                 judgekit=None, note_project="", recall_limit=10,
                 omit_outcomes=("memory_recall", "memory_tree", "session_briefs"),
                 inheritance_refresh_seconds=900.0):
        self.client = client
        # A long session re-reads its inheritance after this many seconds; 0 reads it once.
        self.inheritance_refresh_seconds = max(0.0, float(inheritance_refresh_seconds))
        # Deliberate notes (decisions, corrections, locks) may target a canonical store other than
        # the per-harness capture project; both are configured, never inferred.
        self.note_project = note_project or project_id
        self.recall_limit = max(1, int(recall_limit))
        # Memory reads return earlier layers; capturing them again nests every recall inside the next layer.
        self.omit_outcomes = frozenset(omit_outcomes)
        self.outbox_dir = Path(outbox_dir)
        self.outbox_dir.mkdir(parents=True, exist_ok=True)
        self.project_id = project_id
        self.personal_project = personal_project
        self.author = author
        self.anchor = anchor
        self.inheritance_budget = max(0, int(inheritance_budget))
        self.personal_budget = max(0, int(personal_budget))
        self.journal = self.outbox_dir / "outbox.jsonl"
        self._lock_path = self.outbox_dir / "outbox.lock"
        self._lock = threading.RLock()
        self._secret_envs = ["TYPESAFE_API_KEY", "AETHERMIND_TOKEN"]
        self._secret_envs.extend(secret_envs or [])
        token_env = getattr(getattr(client, "mcp", None), "token_env", None)
        if isinstance(token_env, str) and token_env:
            self._secret_envs.append(token_env)
        self.session_id = uuid.uuid4().hex
        self.turn_number = 0
        self._events = []
        self._turn_active = False
        self._turn_request_id = None
        self._completed = []
        self._note_number = 0
        self._inheritance = None
        self._inheritance_at = 0.0
        self._inheritance_snapshot = None
        # Per-turn brief. The refresh runs off the turn; the turn only reads what is already known.
        self._brief = None
        self._brief_error = None
        self._brief_at = 0.0
        self._brief_thread = None
        self._brief_lock = threading.Lock()
        self.service_error = None
        self.service_error_category = None
        self.service_error_at = 0.0
        # This turn's write failures, with the message. The journal keeps the class name only.
        self._delivery_errors = {}
        log_root = self.outbox_dir.parent if self.outbox_dir.name == "outbox" else self.outbox_dir
        self.judgekit = judgekit or JudgeKit(enabled=False, decision_log=log_root / "decisions.jsonl")
        # Validate the journal and repair only an incomplete final append.
        with self._journal_lock():
            self._load()

    def install(self, hookbus):
        for event in ("session_start", "turn_start", "pre_tool", "post_tool", "turn_end", "session_end"):
            hookbus.register(event, getattr(self, event))

    def session_start(self, payload):
        with self._lock:
            self.session_id = str(payload.get("session_id") or self.session_id)
            self.anchor = payload.get("anchor", self.anchor)
            self.turn_number = 0
            self._completed = []
            self._turn_active = False
            self._inheritance = None
            self._inheritance_snapshot = None
            with self._journal_lock():
                items, _ = self._load()
            prefix = self.session_id + "-t"
            for request_id, item in items.items():
                if not request_id.startswith(prefix):
                    continue
                match = re.fullmatch(r"(\d+)(?:-[0-9a-f]{32})?", request_id[len(prefix):])
                if not match:
                    continue
                number = int(match.group(1))
                self.turn_number = max(self.turn_number, number)
                self._completed.append({"request_id": request_id, "reply": "Restored turn capture.",
                                        "failed": "?" in item["layer"].get("markers", [])})
        self.retry_outbox()
        return {"context": self.inheritance_block()}

    def turn_start(self, payload):
        payload = self._snapshot(payload)
        with self._lock:
            number = payload.get("turn_number", payload.get("turn", payload.get("turn_id")))
            if isinstance(number, int) and not isinstance(number, bool) and number > 0:
                # A caller can pass a number lower than the counter this session
                # has already reached (a resumed session, a replayed hook). Taking
                # it as-is would walk the counter backwards onto a spent ref.
                self.turn_number = max(self.turn_number, number)
            else:
                self.turn_number += 1
            # The display counter can repeat after resume or compaction. Identity
            # belongs to this turn, and stays unchanged through outbox retries.
            self._turn_request_id = f"{self.session_id}-t{self.turn_number}-{uuid.uuid4().hex}"
            self._events = []
            self._turn_active = True
            message = payload.get("message", payload.get("text", ""))
            self._events.append({"kind": "user", "text": _text(message)})

    def pre_tool(self, payload):
        payload = self._snapshot(payload)
        with self._lock:
            if self._turn_active:
                self._events.append({"kind": "tool", "name": payload.get("name", "unknown"),
                                     "id": payload.get("id", ""),
                                     "arguments": payload.get("arguments", {})})

    def post_tool(self, payload):
        payload = self._snapshot(payload)
        with self._lock:
            if self._turn_active:
                self._events.append({"kind": "result", "name": payload.get("name", "unknown"),
                                     "id": payload.get("id", ""),
                                     "result": payload.get("result", payload.get("output", "")),
                                     "error": payload.get("error"),
                                     "success": payload.get("success"),
                                     "blocked": bool(payload.get("blocked", False))})

    def turn_end(self, payload):
        payload = self._snapshot(payload)
        collision = None
        with self._lock:
            if self._turn_active:
                reply = _text(payload.get("reply", payload.get("response", "")))
                user_words = []
                outputs = []
                for event in self._events:
                    if event["kind"] == "user":
                        user_words.append(event["text"])
                    elif event["kind"] == "result":
                        outputs.append(event)
                message = "\n".join(user_words)
                verification = payload.get("verification")
                if not isinstance(verification, dict):
                    verification = self.judgekit.verify_output(message, reply, outputs)
                candidates = []
                for completed in self._completed[-self.judgekit.batch_items:]:
                    if completed.get('message'):
                        candidates.append(completed['message'])
                classification = self.judgekit.memory(message, reply, outputs, verification,
                                                       existing_layers=candidates if candidates else None)
                failed = self._unresolved(payload, verification, outputs, reply)
                lines, outcome = self._summary_lines(message, reply, outputs, classification, verification, failed)
                request_id = self._turn_request_id
                layer = self._layer("\n\n".join(lines), "turn", failed=failed)
                layer["type"] = classification["layer_type"]
                if classification.get("is_correction", 0) >= .5 or layer["type"] == "correction":
                    layer["markers"].append("!")
                # A ref that already holds different content cannot be written.
                # Recording it as a collision keeps the loss visible: the item
                # stays in debt, so a lost capture never reads as a clean turn.
                try:
                    self._enqueue(request_id, layer)
                except ValueError:
                    collision = request_id
                    # Keep the body. The ref is spent on different content, so the
                    # item can never be delivered under this request_id, but the
                    # capture itself must not vanish from the record.
                    collision_id = request_id + "!collision-" + uuid.uuid4().hex
                    with self._journal_lock():
                        self._append({"event": "collision", "request_id": collision_id,
                                      "item": {"project_id": self.project_id, "request_id": request_id,
                                               "layer": layer, "created_at": _now(), "attempts": 0,
                                               "last_error": None,
                                               "permanent_error": "CaptureCollision"}})
                self._completed.append({"request_id": request_id, "message": self._bounded(message, 2000),
                                        "reply": self._bounded(outcome, 400), "failed": failed,
                                        "collision": bool(collision)})
                self._turn_active = False
        self.retry_outbox()
        result = {"write_debt": len(self.debt())}
        if collision:
            result["lost"] = [collision]
        return result

    def session_end(self, payload):
        if self._turn_active:
            self.turn_end({"error": "session ended before turn completed", **payload})
        with self._lock:
            lines = [f"Session {self.session_id}", f"Completed turns: {len(self._completed)}"]
            for turn in self._completed:
                lines.append(f"{turn['request_id']}: {turn['reply']}")
                if turn["failed"]:
                    lines.append("Failure recorded in this turn.")
            self._enqueue(f"{self.session_id}-session", self._layer("\n".join(lines), "session"))
        self.retry_outbox()
        return {"write_debt": len(self.debt())}

    def _unresolved(self, payload, verification, outputs, reply):
        """A finished reply is not an open item. Low completion alone does not make it one."""
        tool_failed = False
        for event in outputs:
            if event.get("name") in self.omit_outcomes and self._soft_memory(event.get("result")):
                continue
            if event.get("error") or event.get("blocked") or event.get("success") is False:
                tool_failed = True
            result = event.get("result")
            if isinstance(result, dict) and result.get("state") != "no_tree":
                if result.get("error") or result.get("exit_code", 0) not in (None, 0):
                    tool_failed = True
        claimed = verification.get("claims_done_but_failed", 0) >= .5
        finished = bool(reply.strip()) and payload.get("success") is True and not payload.get("error")
        if finished and not tool_failed and not claimed:
            return False
        if tool_failed or claimed or payload.get("error") or payload.get("success") is False or payload.get("failed"):
            return True
        return verification.get("completion", 1) < .5

    @staticmethod
    def _soft_memory(result):
        if isinstance(result, dict) and result.get("state") == "no_tree":
            return True
        text = result if isinstance(result, str) else json.dumps(result, ensure_ascii=False, default=str)
        lowered = text.lower()
        return "no summary tree" in lowered or "recalled recent layers" in lowered

    def _summary_lines(self, message, reply, outputs, classification, verification, failed):
        """Decision and outcome only. Raw tool JSON and earlier recall bodies are not stored."""
        recalled = []
        notes = []
        for event in outputs:
            name = event.get("name", "tool")
            if name in self.omit_outcomes:
                self._collect_text(event.get("result"), recalled)
                notes.append(f"{name}: recalled, not stored")
                continue
            result = _result_as_dict(event.get("result"))
            route = ""
            if name == "memory_note" and isinstance(result, dict) and isinstance(result.get("project"), str) and result["project"]:
                route = ", routed to " + result["project"]
            failed_call = bool(event.get("error") or event.get("blocked") or event.get("success") is False)
            if not failed_call and isinstance(result, dict) and result.get("state") != "no_tree" and (
                    result.get("error") or result.get("exit_code", 0) not in (None, 0)):
                failed_call = True
            if failed_call:
                label = self._failure_label(event, result)
                notes.append(f"{name}: failed" + (f" ({label})" if label else "") + route)
            else:
                notes.append(f"{name}: ok" + route)
        outcome = self._distill(self._without_recall(reply, recalled))
        if not outcome.strip():
            outcome = "No separate outcome."
        decision = classification.get("layer_type") or "fact"
        lines = [f"Decision: {decision}", "Outcome: " + outcome]
        if notes:
            lines.append("Tools: " + "; ".join(notes))
        if classification.get("duplicate_of_existing_layer", 0) >= .5:
            lines.append("Duplicate of an existing layer.")
        if verification.get("claims_done_but_failed", 0) >= .5:
            lines.append("Reply claimed success while a tool failed.")
        if failed:
            lines.append("Failure recorded during this turn.")
        return lines, outcome

    def _collect_text(self, value, found):
        if isinstance(value, str):
            parsed = None
            if value[:1] in "{[":
                try:
                    parsed = json.loads(value)
                except ValueError:
                    parsed = None
            if parsed is not None:
                self._collect_text(parsed, found)
            elif len(value) >= 24:
                found.append(value)
            return
        if isinstance(value, dict):
            body = value.get("body")
            if isinstance(body, str) and body.strip():
                found.append(body)
            for item in value.values():
                self._collect_text(item, found)
            return
        if isinstance(value, (list, tuple)):
            for item in value:
                self._collect_text(item, found)

    def _distill(self, text):
        """A decision, not the turn. Tool-argument objects and the user transcript stay out."""
        text = re.sub(r"\{[^{}]*\}", " ", text or "")
        text = re.sub(r"(?i)\b(?:arguments|args)\b\s*[:=]?", " ", text)
        text = " ".join(text.split())
        return self._clip_sentence(text, 400)

    @staticmethod
    def _clip_sentence(text, limit):
        if len(text) <= limit:
            return text
        cut = text[:limit]
        for mark in (". ", "! ", "? "):
            index = cut.rfind(mark)
            if index >= 40:
                return cut[:index + 1]
        if " " in cut:
            return cut.rsplit(" ", 1)[0]
        return cut

    def _without_recall(self, reply, recalled):
        text = reply
        for blob in sorted(set(recalled), key=len, reverse=True):
            if blob and blob in text:
                text = text.replace(blob, "")
        return " ".join(text.split())

    def _layer(self, body, kind, failed=False):
        return {"type": "discovery", "body": body,
                "ctx": f"tessera/{kind}/session/{self.session_id}", "author": self.author,
                "markers": ["?"] if failed else [], "thread_key": self.session_id}

    @contextmanager
    def _journal_lock(self):
        with self._lock:
            with self._lock_path.open("a+b") as lock:
                fcntl.flock(lock, fcntl.LOCK_EX)
                try:
                    yield
                finally:
                    fcntl.flock(lock, fcntl.LOCK_UN)

    def _load(self):
        items = {}
        acknowledged = set()
        if not self.journal.exists():
            return items, acknowledged
        with self.journal.open("r+b") as stream:
            while True:
                offset = stream.tell()
                line = stream.readline()
                if not line:
                    break
                if not line.endswith(b"\n"):
                    stream.truncate(offset)
                    stream.flush()
                    os.fsync(stream.fileno())
                    break
                try:
                    event = json.loads(line)
                    request_id = event["request_id"]
                    kind = event["event"]
                    if kind == "enqueue":
                        items[request_id] = dict(event["item"])
                    elif kind == "collision":
                        # A capture that could not be enqueued because its ref
                        # already held different content. Kept as debt so the loss
                        # stays visible; permanent_error keeps it out of the walk.
                        items[request_id] = dict(event["item"])
                    elif kind == "attempt":
                        items[request_id]["attempts"] += 1
                        items[request_id]["last_attempt"] = event["at"]
                    elif kind == "failure":
                        items[request_id]["last_error"] = event["error"]
                    elif kind == "rejected":
                        # A permanent refusal. Recorded so the walk skips the item
                        # on later wakes instead of paying a round trip for a
                        # capture the server will never accept.
                        items[request_id]["permanent_error"] = event["error"]
                    elif kind == "ack":
                        acknowledged.add(request_id)
                    else:
                        raise ValueError("unknown journal event")
                except (ValueError, KeyError, TypeError):
                    raise ValueError(f"Invalid outbox journal record at byte {offset}") from None
        return items, acknowledged

    def _append(self, event):
        existed = self.journal.exists()
        encoded = (json.dumps(event, ensure_ascii=False, sort_keys=True, allow_nan=False) + "\n").encode("utf-8")
        with self.journal.open("ab") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        if not existed:
            sync_directory(self.outbox_dir)

    def _enqueue(self, request_id, layer, project_id=None):
        layer = self._snapshot(layer)
        with self._journal_lock():
            items, acknowledged = self._load()
            if request_id in items:
                if items[request_id]["layer"] != layer:
                    raise ValueError("Capture request_id already belongs to a different layer")
                return
            item = {"project_id": project_id or self.project_id, "request_id": request_id, "layer": layer,
                    "created_at": _now(), "attempts": 0, "last_error": None}
            self._append({"event": "enqueue", "request_id": request_id, "item": item})

    def _snapshot(self, value):
        if isinstance(value, str):
            for name in self._secret_envs:
                if not isinstance(name, str) or not name:
                    continue
                secret = os.environ.get(name, "")
                if secret:
                    value = value.replace(secret, "[REDACTED]")
            return value
        if isinstance(value, dict):
            result = {}
            for key, item in value.items():
                result[self._snapshot(str(key))] = self._snapshot(item)
            return result
        if isinstance(value, (list, tuple)):
            result = []
            for item in value:
                result.append(self._snapshot(item))
            return result
        if value is None or isinstance(value, (bool, int, float)):
            return value
        return self._snapshot(str(value))

    def debt(self):
        with self._journal_lock():
            items, acknowledged = self._load()
            pending = []
            for request_id, item in items.items():
                if request_id not in acknowledged:
                    pending.append(item)
            return pending

    def retry_outbox(self):
        if self.client is None:
            return
        with self._journal_lock():
            items, acknowledged = self._load()
            for request_id, item in items.items():
                if request_id in acknowledged:
                    continue
                # A config change cannot authorize delivery to an older target.
                # Preserve its original item so an operator can resolve it later.
                if not self.project_id or item["project_id"] not in (self.project_id, self.note_project):
                    continue
                # A capture the server will never accept must not starve the
                # ones behind it. Skip it, keep it in debt, and let a human
                # resolve the reference collision.
                if item.get("permanent_error"):
                    continue
                self._append({"event": "attempt", "request_id": request_id, "at": _now()})
                try:
                    result = self.client.write_layer(item["project_id"], request_id, item["layer"])
                    if not self._acknowledged(result):
                        raise ValueError("ambiguous write acknowledgement")
                except CaptureRejected as error:
                    # Permanent: the reference is spent on different content.
                    # Keep the item so nothing is lost, and move on.
                    self._append({"event": "rejected", "request_id": request_id,
                                  "error": type(error).__name__, "at": _now()})
                    continue
                except Exception as error:
                    # The journal stores the class name. A message can carry a private
                    # diagnostic, and it stays in memory for this turn's tool result.
                    self._record_service_error(error)
                    self._delivery_errors[request_id] = self._service_error_text(error)
                    self._append({"event": "failure", "request_id": request_id,
                                  "error": type(error).__name__})
                    # Avoid one timeout per queued item when the service is down.
                    break
                self._append({"event": "ack", "request_id": request_id, "at": _now()})

    @staticmethod
    def _acknowledged(result):
        if not isinstance(result, dict):
            return False
        if result.get("ok") is False or result.get("success") is False or result.get("isError"):
            return False
        return bool(result.get("ok") is True or result.get("success") is True or result.get("layer_id"))

    @staticmethod
    def _bounded(text, limit):
        if len(text) <= limit:
            return text
        suffix = "\n[truncated]"
        if limit < len(suffix):
            return text[:limit]
        return text[:limit - len(suffix)] + suffix

    @property
    def inheritance_snapshot(self):
        """Last explicit read, including honest unavailable notices and write debt.

        Startup consumers share that read. Explicit inheritance_block calls keep
        their existing refresh behavior, including recovery after a failed read.
        """
        if self._inheritance_snapshot is None:
            return self.inheritance_block()
        return self._inheritance_snapshot

    def inheritance_block(self):
        if self._inheritance is not None and self.inheritance_refresh_seconds > 0:
            age = time.monotonic() - self._inheritance_at
            if age >= self.inheritance_refresh_seconds:
                self._inheritance = None
        if self._inheritance is None:
            parts = []
            if self.client is not None:
                complete = True
                try:
                    inherited = self.client.prepare_inheritance(self.project_id, self.anchor, self.inheritance_budget)
                    raw = present_inheritance(self._snapshot(inherited))
                    parts.append("Project inheritance:\n" + clip_records(raw, self.inheritance_budget, "memory_recall"))
                except Exception as error:
                    complete = False
                    self._record_service_error(error)
                    parts.append(self._unavailable("Project inheritance", error))
                if self.personal_project:
                    try:
                        personal = tree_read_display(self.client.tree_view(self.personal_project, self.personal_budget))
                        raw = drop_inactive_chains(_text(self._snapshot(personal)))
                        parts.append("Personal memory (server cover, not a verified fact):\n" + clip_records(raw, self.personal_budget, "memory_recall"))
                    except Exception as error:
                        complete = False
                        self._record_service_error(error)
                        parts.append(self._unavailable("Personal memory", error))
                if complete:
                    self._inheritance = "\n".join(parts)
                    self._inheritance_at = time.monotonic()
                    self._clear_service_error()
            else:
                parts.append("Memory service unavailable.")
            inherited_text = "\n".join(parts)
        else:
            inherited_text = self._inheritance
        debt = self.debt()
        if debt:
            notice = f"Write-debt: {len(debt)} unsent captures (oldest {debt[0]['created_at']})"
            other_targets = sum(not self.project_id or item["project_id"] != self.project_id for item in debt)
            if other_targets:
                notice += f"\n{other_targets} captures target a different project; retained without sending."
            self._inheritance_snapshot = inherited_text + "\n" + notice
        else:
            self._inheritance_snapshot = inherited_text
        return self._inheritance_snapshot

    def _own_capture(self, layer):
        """A layer this session wrote. Other sessions' captures stay in recall."""
        if not isinstance(layer, dict) or not self.session_id:
            return False
        if str(layer.get("thread_key") or "") == self.session_id:
            return True
        prefix = self.session_id + "-"
        if str(layer.get("ref") or "").startswith(prefix):
            return True
        evidence = layer.get("evidence")
        if isinstance(evidence, list) and any(str(item).startswith(prefix) for item in evidence):
            return True
        ctx = str(layer.get("ctx") or "")
        return f"/session/{self.session_id}" in ctx

    def _without_own(self, result):
        if isinstance(result, dict) and isinstance(result.get("layers"), list):
            layers = result["layers"]
            wrapper = result
        elif isinstance(result, list):
            layers = result
            wrapper = None
        else:
            return result
        kept = []
        omitted = 0
        for layer in layers:
            if self._own_capture(layer):
                omitted += 1
            else:
                kept.append(layer)
        if not omitted:
            return result
        if wrapper is None:
            return {"layers": kept, "omitted_session_captures": omitted}
        return {**wrapper, "layers": kept, "omitted_session_captures": omitted}

    def _recalled(self, result):
        """What the model is allowed to see as live memory.

        `read_layers` returns immutable history, so a layer an explicit archive
        superseded comes back with no self-signal. Handing it to the model as
        live memory is wrong: half the layers in this session's recall window can
        be inactive, including a probe layer an archive had already superseded. Drop the superseded ones, then drop this
        session's own captures. Order matters: a superseded layer must not
        consume one of the model's slots.
        """
        return self._without_own(drop_superseded(result))

    def _unavailable(self, label, error):
        return f"{label} unavailable ({self._service_error_text(error)})."

    def _service_error_text(self, error):
        name = type(error).__name__
        message = self._snapshot(str(error)).replace("\n", " ").strip()
        if len(message) > 200:
            message = message[:200]
        if not message or message == name:
            return name
        return f"{name}: {message}"

    def _record_service_error(self, error):
        self.service_error = type(error).__name__
        self.service_error_category = _category_for(error)
        self.service_error_at = time.monotonic()

    def _record_labeled_error(self, label):
        head = str(label).split(":", 1)[0].strip()
        if not _CLASS_NAME.fullmatch(head):
            return
        self.service_error = head
        self.service_error_category = _category_for_text(head, str(label))
        self.service_error_at = time.monotonic()

    def _clear_service_error(self):
        self.service_error = None
        self.service_error_category = None

    def turn_brief(self):
        """The latest brief, or a labeled error. Returns immediately; it does not wait on the service."""
        if self.client is None:
            return ""
        self.request_brief()
        return self._brief_line()

    def request_brief(self):
        if self.client is None:
            return
        method = getattr(self.client, "brief", None)
        if not callable(method):
            message = "AetherMind client does not expose brief"
            with self._brief_lock:
                self._brief_error = "McpError: " + message
            self._record_service_error(McpError(message))
            return
        with self._brief_lock:
            thread = self._brief_thread
            if thread is not None and thread.is_alive():
                return
            if self._brief and not self._brief_error:
                if self.inheritance_refresh_seconds == 0:
                    return
                if time.monotonic() - self._brief_at < self.inheritance_refresh_seconds:
                    return
            self._brief_thread = threading.Thread(target=self._refresh_brief, name="aethermind-brief", daemon=True)
            self._brief_thread.start()

    def settle_brief(self, timeout=2):
        """Join the current refresh. Tests use this; a turn does not."""
        with self._brief_lock:
            thread = self._brief_thread
        if thread is None:
            return True
        thread.join(timeout)
        return not thread.is_alive()

    def _refresh_brief(self):
        try:
            text = self._accept_brief(self._call_brief())
        except Exception as error:
            labeled = self._service_error_text(error)
            with self._brief_lock:
                self._brief_error = labeled
            self._record_service_error(error)
            return
        with self._brief_lock:
            self._brief = text
            self._brief_error = None
            self._brief_at = time.monotonic()
        self._clear_service_error()

    def _call_brief(self):
        return self.client.brief(self.project_id, anchor=self.anchor, budget=self.inheritance_budget)

    def _accept_brief(self, result):
        if isinstance(result, dict) and (result.get("success") is False or result.get("error") or result.get("isError")):
            detail = result.get("error") or result.get("message") or "brief was not successful"
            if not isinstance(detail, str):
                detail = type(detail).__name__
            raise McpError(self._snapshot(detail) or "brief was not successful")
        if isinstance(result, str):
            text = self._snapshot(result).strip()
            if not text:
                raise McpError("AetherMind brief was empty")
            return text
        if isinstance(result, dict):
            shown = present_inheritance(self._snapshot(result)).strip()
            if shown:
                return shown
            raw = _text(self._snapshot(result)).strip()
            if raw and raw not in ("{}", "[]"):
                return raw
            raise McpError("AetherMind brief was empty")
        raise McpError("AetherMind brief returned an unreadable result")

    def _brief_line(self):
        with self._brief_lock:
            parts = []
            if self._brief:
                parts.append("AetherMind brief:\n" + self._brief)
            if self._brief_error:
                parts.append("AetherMind brief error: " + self._brief_error)
            elif not self._brief:
                parts.append("AetherMind brief: refresh in progress.")
            return "\n".join(parts)

    def _note_delivery(self, request_id, target, note_type):
        pending = next((item for item in self.debt() if item["request_id"] == request_id), None)
        result = {"request_id": request_id, "project": target, "type": note_type,
                  "queued": pending is not None, "delivered": pending is None}
        if pending is None:
            if self.client is not None:
                self._clear_service_error()
            return result
        if self.client is None:
            result["error"] = "memory service unavailable"
            return result
        label = (self._delivery_errors.get(request_id)
                 or pending.get("permanent_error") or pending.get("last_error") or "not acknowledged")
        result["error"] = str(label)
        self._record_labeled_error(result["error"])
        return result

    @staticmethod
    def _failure_label(event, result):
        if isinstance(result, dict):
            err = result.get("error")
            if isinstance(err, str) and err.strip():
                head = err.split(":", 1)[0].strip()
                if _CLASS_NAME.fullmatch(head):
                    return head
        text = str(event.get("error") or "")
        match = re.match(r"Tool error \(([A-Za-z_][A-Za-z0-9_]*)\)", text)
        if match:
            return match.group(1)
        return ""

    def _resolve_project(self, requested):
        """Map a requested project id onto a configured store, or refuse it.

        The engine resolves a relative project name against its own working
        directory, so a model that writes `default` gets
        `<runtime>/default`, which no store backs, and the read reports an empty
        store. The harness knows the real stores, so it maps the names it
        configured and refuses anything else instead of forwarding a string that
        resolves somewhere else.

        An absolute path is passed through: it already names one directory, and
        pointing a read at another store stays possible.
        """
        known = [p for p in dict.fromkeys(
            [self.project_id, self.note_project, self.personal_project]) if p]
        text = str(requested or '').strip()
        if not text:
            return self.project_id
        if text in known:
            return text
        path = Path(text)
        if path.is_absolute():
            return text
        # A bare name for a configured store is that store: `default` means the
        # configured `<home>/projects/default`, not a directory beside the
        # engine's runtime.
        matches = [p for p in known if Path(p).name == path.name]
        if len(matches) == 1:
            return matches[0]
        raise ValueError(
            'project_id ' + repr(text) + ' is not a configured store; pass one of '
            + ', '.join(known) + ' exactly as written, or an absolute store path.')

    def memory_recall(self, query=None, level=None, index=None, project_id=None):
        if (level is None) != (index is None):
            raise ValueError("memory_recall requires both level and index")
        if query is None and level is None:
            raise ValueError("memory_recall requires query or level/index")
        if query is not None and level is not None:
            raise ValueError("memory_recall accepts query or level/index, not both")
        if self.client is None:
            return {"error": "memory service unavailable"}
        # A wrong project name is a bad argument, not a memory service failure:
        # resolve it before the try so it cannot dirty the service health.
        project = self._resolve_project(project_id)
        try:
            if level is not None:
                value = self.client.tree_zoom(project, level, index)
            else:
                projects = [project] if project_id else [p for p in dict.fromkeys(
                    [self.project_id, self.note_project, self.personal_project]) if p]
                if len(projects) == 1:
                    value = self._recalled(self.client.read_layers(projects[0], query=query, last_n=self.recall_limit))
                else:
                    merged = []
                    for project in projects:
                        result = self.client.read_layers(project, query=query, last_n=self.recall_limit)
                        layers = result.get("layers", []) if isinstance(result, dict) else result if isinstance(result, list) else []
                        merged.extend({**layer, "project": project} for layer in layers if isinstance(layer, dict))
                    value = self._recalled({"success": True, "query": query, "projects": projects, "layers": merged})
        except Exception as error:
            self._record_service_error(error)
            return {"error": self._service_error_text(error)}
        if _returned_error(value):
            self._record_labeled_error(str(value.get("error")))
            return value
        self._clear_service_error()
        return value

    def memory_tree(self, project_id=None, budget_chars=2000):
        if self.client is None:
            return {"error": "memory service unavailable"}
        project = self._resolve_project(project_id)
        try:
            result = self.client.tree_view(project, budget_chars)
        except Exception as error:
            self._record_service_error(error)
            return {"error": self._service_error_text(error)}
        shown = tree_read_display(result)
        if not (isinstance(shown, dict) and shown.get("state") == "no_tree"):
            if _returned_error(shown):
                self._record_labeled_error(str(shown.get("error")))
            else:
                self._clear_service_error()
            return shown
        # A missing tree.aem is a normal empty state. Recall instead of asking for build_tree.
        try:
            recalled = self.client.read_layers(project, last_n=self.recall_limit)
        except Exception as error:
            self._record_service_error(error)
            return {"state": "no_tree",
                    "message": "No summary tree; recall is unavailable (" + self._service_error_text(error) + ")."}
        self._clear_service_error()
        return {"state": "no_tree", "message": "No summary tree; recalled recent layers instead.",
                "recall": self._recalled(recalled)}

    def memory_note(self, text, type="note"):
        if not isinstance(text, str) or not text.strip():
            raise ValueError("memory_note text must be nonempty")
        if type not in NOTE_TYPES:
            raise ValueError("memory_note type must be one of " + ", ".join(NOTE_TYPES))
        with self._lock:
            self._note_number += 1
            request_id = f"{self.session_id}-note-{self._note_number}-{uuid.uuid4().hex}"
            layer = self._layer(text, "note")
            target = self.project_id
            if type != "note":
                layer["type"] = type
                target = self.note_project
            self._enqueue(request_id, layer, target)
        self.retry_outbox()
        return self._note_delivery(request_id, target, type)

    def register_tools(self, registry):
        registry.register("memory_recall", "Recall memory by query or open a memory tree node by level and index.", {
            "type": "object", "properties": {
                "query": {"type": "string"}, "level": {"type": "integer", "minimum": 0},
                "index": {"type": "integer", "minimum": 0}, "project_id": {"type": "string"},
            }, "additionalProperties": False,
        }, self.memory_recall)
        registry.register("memory_tree", "Overview of a memory project's summary tree. A missing tree is normal: this falls back to recent layers and says so in one line.", {
            "type": "object", "properties": {
                "project_id": {"type": "string"}, "budget_chars": {"type": "integer", "minimum": 200, "maximum": 20000},
            }, "additionalProperties": False,
        }, self.memory_tree)
        registry.register("memory_note", "Write a durable memory layer: a decision, correction, lock, lesson or note. A plain note goes to the capture project; a decision, correction, lock or lesson goes to the configured note project. The result names that project and whether the write was delivered. Turn capture also runs automatically.", {
            "type": "object", "properties": {"text": {"type": "string"}, "type": {"type": "string", "enum": list(NOTE_TYPES)}},
            "required": ["text"], "additionalProperties": False,
        }, self.memory_note)
