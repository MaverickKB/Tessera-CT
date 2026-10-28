"""
Core AetherMind implementation.

Implements:
- AetherLayer: dataclass for structured layer entries
- LayerStore: read/write layers.aem (TOML, append-only)
- TextureStore: read/write texture.aem (append-only plain text)
- AetherMind: convenience class for project context
- write_layer, write_texture, read_layers: high-level API
"""

import io
import hashlib
import importlib
import json
import os
import re
import tempfile
import uuid

# External writers must identify layers with UUIDs (see LayerStore.append).
_UUID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
from datetime import datetime, timezone
from dataclasses import dataclass, asdict, field
from pathlib import Path
from typing import List, Optional, Sequence, Union, cast

try:
    tomllib = importlib.import_module("tomllib")  # Python 3.11+
except ImportError:
    try:
        tomllib = importlib.import_module("tomli")  # type: ignore
    except ImportError:
        from . import tomllib  # bundled fallback


class AetherMindParseError(ValueError):
    """Raised when an existing AetherMind store cannot be parsed safely."""

    code = "STORE_CORRUPT"

    def __init__(self, message: str, report=None):
        super().__init__(message)
        self.report = report


class AutoInitBlocked(ValueError):
    """Raised when a write targets a project root with no existing
    AetherMind store and no explicit authorization to create one.

    Authorization comes from either passing create=True to AetherMind(...),
    or having previously called register_project_key(project_root, key),
    which is itself the explicit registration step and creates the store
    directory as part of registering.
    """

    code = "AUTO_INIT_BLOCKED"


class LockUnavailableError(RuntimeError):
    """Raised instead of writing when no safe local lock backend is present."""

    code = "LOCK_UNAVAILABLE"


class RegistryError(ValueError):
    """Raised when project-key metadata is corrupt or inconsistent."""

    code = "REGISTRY_CORRUPT"


RUNTIME_ID = "aethermind-python"
RUNTIME_VERSION = "0.3.0"
FORMAT_VERSION = "light-v1"


def error_payload(exc: Exception) -> dict:
    """Return one stable error envelope for CLI and MCP adapters."""
    if hasattr(exc, "code"):
        code = getattr(exc, "code")
    elif isinstance(exc, (TypeError, ValueError)):
        code = "INVALID_ARGUMENT"
    else:
        code = "INTERNAL_ERROR"
    return {
        "success": False,
        "error": str(exc),
        "error_code": code,
        "error_type": type(exc).__name__,
    }


ALLOWED_LAYER_TYPES = {
    "fork",
    "friction",
    "discovery",
    "uncertainty",
    "correction",
    "load-bearing",
    "decision",
    "thought",
    "runtime-validation",
    "validation",
    "init",
    "verification",
    "review",
}

ALLOWED_LIGHT_PRIMITIVES = {
    "layer",
    "artifact-reference",
    "anchor",
    "pressure-event",
    "supersession",
    "rollback",
}
ALLOWED_PRESSURE_SCOPES = {"local", "cross-domain", "cross-host", "profile", "format", "unknown"}
ALLOWED_PRESSURE_SEVERITIES = {"low", "medium", "high", "critical"}
ALLOWED_SUPERSESSION_SCOPES = {"claim", "anchor", "artifact", "route", "format", "other"}


def _toml_string(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


_RFC3339_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(?::\d{2}(?:\.\d{1,9})?)?(?:Z|[+-]\d{2}:\d{2})$"
)
_TYPE_NAME_RE = re.compile(r"^[a-z][a-z0-9-]{0,63}$")


def _required_string(value, field_name: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{field_name} must be a non-empty string")
    return value


def _string_list(value: Sequence[str], field_name: str) -> List[str]:
    if isinstance(value, (str, bytes)) or not isinstance(value, (list, tuple)):
        raise ValueError(f"{field_name} must be an array of strings")
    if any(not isinstance(item, str) for item in value):
        raise ValueError(f"{field_name} must contain only strings")
    return list(value)


def _optional_string(value: Optional[str], field_name: str = "value") -> Optional[str]:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"{field_name} must be a string")
    return value


def _parse_rfc3339(value: str, field_name: str = "ts") -> datetime:
    if not isinstance(value, str) or not _RFC3339_RE.fullmatch(value):
        raise ValueError(f"{field_name} must be an RFC3339 timestamp with timezone")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00" if value.endswith("Z") else value)
    except ValueError as exc:
        raise ValueError(f"{field_name} must be a valid RFC3339 timestamp") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"{field_name} must include a timezone")
    return parsed


def _id_order_key(layer_id: str):
    return (0, int(layer_id)) if layer_id.isdigit() else (1, layer_id)


def _layer_order_key(layer: "AetherLayer"):
    return (_parse_rfc3339(layer.ts).timestamp(), _id_order_key(layer.id))


def _append_toml_string(text: str, key: str, value: Optional[str]) -> str:
    if value is None:
        return text
    return text[:-1] + f"{key} = {_toml_string(value)}\n\n"


def _append_toml_bool(text: str, key: str, value: Optional[bool]) -> str:
    if value is None:
        return text
    return text[:-1] + f"{key} = {str(value).lower()}\n\n"


def _append_toml_string_list(text: str, key: str, value: Sequence[str]) -> str:
    if not value:
        return text
    items = ", ".join(_toml_string(str(item)) for item in value)
    return text[:-1] + f"{key} = [{items}]\n\n"


def _format_layer(layer: "AetherLayer") -> str:
    layer_dict = asdict(layer)
    markers = ", ".join(_toml_string(str(marker)) for marker in layer_dict["markers"])
    text = (
        "[[layer]]\n"
        f"id       = {_toml_string(layer_dict['id'])}\n"
        f"ts       = {_toml_string(layer_dict['ts'])}\n"
        f"author   = {_toml_string(layer_dict['author'])}\n"
        f"type     = {_toml_string(layer_dict['type'])}\n"
        f"body     = {_toml_string(layer_dict['body'])}\n"
        f"ctx      = {_toml_string(layer_dict['ctx'])}\n"
        f"conf     = {layer_dict['conf']}\n"
        f"markers  = [{markers}]\n"
        f"primitive = {_toml_string(layer_dict['primitive'])}\n\n"
    )
    text = _append_toml_string(text, "thread_key", layer.thread_key)
    text = _append_toml_string_list(text, "supersedes", layer.supersedes)
    text = _append_toml_string_list(text, "rollback_of", layer.rollback_of)
    text = _append_toml_string_list(text, "corrects", layer.corrects)
    text = _append_toml_string(text, "prev_hash", layer.prev_hash)
    text = _append_toml_string(text, "archived_to", layer.archived_to)
    text = _append_toml_string_list(text, "evidence", layer.evidence)
    text = _append_toml_string_list(text, "recurrence_of", layer.recurrence_of)
    text = _append_toml_string_list(text, "verification", layer.verification)
    for key in (
        "inherit",
        "action_rule",
        "review_after",
        "nutrition",
        "force_reason",
        "artifact",
        "artifact_ref",
        "anchor",
        "ref",
        "kind",
        "label",
        "host",
        "repo_root",
        "content_id",
        "selector",
        "span_hint",
        "domain",
        "symptom",
        "next_verification",
        "suspected_mechanism",
        "scope",
        "severity",
        "owner_hint",
        "repair",
        "reason",
        "replacement",
        "restored_to",
    ):
        text = _append_toml_string(text, key, getattr(layer, key))
    text = _append_toml_bool(text, "inline", layer.inline)
    return text


def _layer_hash(layer: "AetherLayer") -> str:
    """Canonical sha256 of the serialized record (append receipt hash)."""
    return hashlib.sha256(_format_layer(layer).encode("utf-8")).hexdigest()


try:
    import fcntl
except ImportError:  # pragma: no cover - Windows or unsupported host
    fcntl = None  # type: ignore[assignment]


def _lock_backend() -> str:
    return "fcntl" if fcntl is not None else "unavailable"


def _require_lock_backend() -> None:
    if fcntl is None:
        raise LockUnavailableError(
            "no safe local file locking backend is available; refusing to write"
        )


class _FileLock:
    def __init__(self, file_obj):
        self.file_obj = file_obj

    def __enter__(self):
        _require_lock_backend()
        assert fcntl is not None
        fcntl.flock(self.file_obj.fileno(), fcntl.LOCK_EX)
        return self.file_obj

    def __exit__(self, _exc_type, _exc, _tb):
        assert fcntl is not None
        fcntl.flock(self.file_obj.fileno(), fcntl.LOCK_UN)

# --- Data Models ---

@dataclass
class AetherLayer:
    """Structured layer entry per spec."""
    id: str
    ts: str
    author: str
    type: str
    body: str
    ctx: str
    conf: float
    markers: List[str]
    primitive: str = "layer"
    thread_key: Optional[str] = None
    inherit: Optional[str] = None
    action_rule: Optional[str] = None
    review_after: Optional[str] = None
    nutrition: Optional[str] = None
    force_reason: Optional[str] = None
    supersedes: List[str] = field(default_factory=list)
    rollback_of: List[str] = field(default_factory=list)
    corrects: List[str] = field(default_factory=list)
    prev_hash: Optional[str] = None
    archived_to: Optional[str] = None
    evidence: List[str] = field(default_factory=list)
    recurrence_of: List[str] = field(default_factory=list)
    verification: List[str] = field(default_factory=list)
    artifact: Optional[str] = None
    artifact_ref: Optional[str] = None
    anchor: Optional[str] = None
    ref: Optional[str] = None
    kind: Optional[str] = None
    label: Optional[str] = None
    host: Optional[str] = None
    repo_root: Optional[str] = None
    content_id: Optional[str] = None
    selector: Optional[str] = None
    span_hint: Optional[str] = None
    domain: Optional[str] = None
    symptom: Optional[str] = None
    next_verification: Optional[str] = None
    suspected_mechanism: Optional[str] = None
    scope: Optional[str] = None
    severity: Optional[str] = None
    owner_hint: Optional[str] = None
    repair: Optional[str] = None
    reason: Optional[str] = None
    replacement: Optional[str] = None
    restored_to: Optional[str] = None
    inline: Optional[bool] = None

    def __post_init__(self):
        """Validate required fields on creation."""
        self.id = _required_string(self.id, "id")
        self.ts = _required_string(self.ts, "ts")
        _parse_rfc3339(self.ts)
        self.author = _required_string(self.author, "author")
        self.type = _required_string(self.type, "type")
        if not _TYPE_NAME_RE.fullmatch(self.type):
            raise ValueError(
                "layer type must be a lowercase semantic token "
                "([a-z][a-z0-9-]{0,63})"
            )
        self.body = _required_string(self.body, "body")
        self.ctx = _required_string(self.ctx, "ctx")
        self.primitive = _required_string(self.primitive, "primitive")
        self.thread_key = _optional_string(self.thread_key, "thread_key")
        if self.thread_key == "":
            raise ValueError("thread_key must not be empty")
        if self.primitive not in ALLOWED_LIGHT_PRIMITIVES:
            raise ValueError(f"unsupported light primitive in this slice: {self.primitive}")
        self.markers = _string_list(self.markers, "markers")
        self.supersedes = _string_list(self.supersedes, "supersedes")
        self.rollback_of = _string_list(self.rollback_of, "rollback_of")
        self.evidence = _string_list(self.evidence, "evidence")
        self.recurrence_of = _string_list(self.recurrence_of, "recurrence_of")
        self.verification = _string_list(self.verification, "verification")
        for field_name in (
            "artifact",
            "inherit",
            "action_rule",
            "review_after",
            "nutrition",
            "force_reason",
            "artifact_ref",
            "anchor",
            "ref",
            "kind",
            "label",
            "host",
            "repo_root",
            "content_id",
            "selector",
            "span_hint",
            "domain",
            "symptom",
            "next_verification",
            "suspected_mechanism",
            "scope",
            "severity",
            "owner_hint",
            "repair",
            "reason",
            "replacement",
            "restored_to",
        ):
            setattr(
                self,
                field_name,
                _optional_string(getattr(self, field_name), field_name),
            )
        if self.inherit not in {None, "posture", "fact", "both"}:
            raise ValueError("inherit must be posture, fact, or both")
        if self.nutrition not in {
            None,
            "dense",
            "questionable",
            "memory_shaped",
            "transcript",
        }:
            raise ValueError("nutrition has an unsupported verdict")
        if self.review_after:
            try:
                dt_review = datetime.fromisoformat(self.review_after)
            except ValueError as exc:
                raise ValueError("review_after must be an ISO date or timestamp") from exc
            if dt_review.tzinfo is None and "T" in self.review_after:
                raise ValueError("review_after timestamp must include a timezone")
        if self.inline is not None and not isinstance(self.inline, bool):
            raise ValueError("inline must be a bool")
        if self.primitive == "artifact-reference" and not self.ref:
            raise ValueError("ref is required for artifact-reference primitive")
        if self.primitive == "anchor":
            if not self.anchor:
                raise ValueError("anchor is required for anchor primitive")
            if not (self.artifact or self.artifact_ref):
                raise ValueError("artifact or artifact_ref is required for anchor primitive")
        if self.primitive == "pressure-event":
            if self.type != "friction":
                raise ValueError("pressure-event primitive requires type friction")
            if not self.domain:
                raise ValueError("domain is required for pressure-event primitive")
            if not self.symptom:
                raise ValueError("symptom is required for pressure-event primitive")
            if not self.evidence:
                raise ValueError("evidence is required for pressure-event primitive")
            if not self.next_verification:
                raise ValueError("next_verification is required for pressure-event primitive")
            if self.scope and self.scope not in ALLOWED_PRESSURE_SCOPES:
                raise ValueError(f"invalid pressure-event scope: {self.scope}")
            if self.severity and self.severity not in ALLOWED_PRESSURE_SEVERITIES:
                raise ValueError(f"invalid pressure-event severity: {self.severity}")
        if self.primitive == "supersession":
            if not self.supersedes:
                raise ValueError("supersedes is required for supersession primitive")
            if not self.reason:
                raise ValueError("reason is required for supersession primitive")
            if self.scope and self.scope not in ALLOWED_SUPERSESSION_SCOPES:
                raise ValueError(f"invalid supersession scope: {self.scope}")
        if self.primitive == "rollback":
            if not self.rollback_of:
                raise ValueError("rollback_of is required for rollback primitive")
            if not self.reason:
                raise ValueError("reason is required for rollback primitive")
        if isinstance(self.conf, bool) or not isinstance(self.conf, float):
            raise ValueError("conf must be a float, not bool or another scalar type")
        if not 0.0 <= self.conf <= 1.0:
            raise ValueError("conf must be between 0.0 and 1.0")


@dataclass(frozen=True)
class LayerReadIssue:
    """One explicit record-level integrity failure in a layer store."""

    record_index: int
    layer_id: Optional[str]
    byte_offset: int
    message: str


@dataclass
class LayerReadReport:
    """Salvage result that never hides malformed or partial records."""

    layers: List[AetherLayer] = field(default_factory=list)
    issues: List[LayerReadIssue] = field(default_factory=list)
    reserved_ids: List[str] = field(default_factory=list)

    @property
    def healthy(self) -> bool:
        return not self.issues


def derive_currentness(
    layers: Sequence[AetherLayer], as_of: Optional[str] = None
) -> dict:
    """Pure currentness projection shared by core and bounded selectors."""
    as_of_value = _parse_rfc3339(as_of) if as_of else None
    history = sorted(layers, key=_layer_order_key)
    if as_of_value is not None:
        history = [
            layer
            for layer in history
            if _parse_rfc3339(layer.ts) <= as_of_value
        ]

    active: dict[str, AetherLayer] = {}
    inactive: dict[str, dict] = {}
    thread_heads: dict[str, str] = {}
    for layer in history:
        implicit_thread = (
            f"digest:{layer.ctx}" if layer.ctx.endswith("/digest") else None
        )
        thread = layer.thread_key or implicit_thread
        if thread:
            previous_id = thread_heads.get(thread)
            if previous_id and previous_id in active:
                active.pop(previous_id)
                inactive[previous_id] = {
                    "reason": "thread-replaced",
                    "by": layer.id,
                    "thread_key": thread,
                }
            thread_heads[thread] = layer.id

        active[layer.id] = layer
        inactive.pop(layer.id, None)
        for relation, targets in (
            ("superseded", layer.supersedes),
            ("rolled-back", layer.rollback_of),
        ):
            for target in targets:
                active.pop(target, None)
                inactive[target] = {
                    "reason": relation,
                    "by": layer.id,
                    "thread_key": thread,
                }

    # Reapply relation edges after every target has been seen. A malformed
    # forward reference cannot silently reactivate a stale record.
    for layer in history:
        for relation, targets in (
            ("superseded", layer.supersedes),
            ("rolled-back", layer.rollback_of),
        ):
            for target in targets:
                active.pop(target, None)
                inactive[target] = {
                    "reason": relation,
                    "by": layer.id,
                    "thread_key": layer.thread_key,
                }

    active_heads = sorted(active.values(), key=_layer_order_key)
    unresolved = [
        layer
        for layer in active_heads
        if layer.type == "correction"
        and not layer.supersedes
        and not layer.rollback_of
        and not layer.thread_key
    ]
    return {
        "history": history,
        "active_heads": active_heads,
        "inactive": inactive,
        "unresolved_currentness": unresolved,
        "as_of": as_of,
    }


# --- Store Implementations ---

class TextureStore:
    """Append-only plain-text store for texture.aem."""

    def __init__(self, path: Union[str, os.PathLike]):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def write(self, entry: str):
        """Append one preformatted texture entry without rewriting history."""
        if not isinstance(entry, str) or not entry.strip():
            raise ValueError("texture entry must be a non-empty string")
        _require_lock_backend()
        payload = entry.rstrip("\n") + "\n"
        with self.path.open('a', encoding='utf-8') as f:
            with _FileLock(f):
                if f.write(payload) != len(payload):
                    raise OSError("short write while appending texture entry")
                f.flush()
                os.fsync(f.fileno())

    def read(self) -> str:
        """Return full texture file content."""
        if not self.path.exists():
            return ''
        with self.path.open('r', encoding='utf-8') as f:
            return f.read()


class EventStore:
    """Append-only TOML store for cheap routine events (split ledger).

    Events are excluded from currentness and default briefs; they exist so
    routine writes do not dilute the decision ledger's density. Each event
    carries the same deterministic id/ts/author/ctx/type/body spine with no
    chain semantics; the ledger remains the authority for decisions.
    """

    _record_header = re.compile(
        br"(?m)^[ \t]*\[\[(?:event|events)\]\][ \t]*(?:\#[^\r\n]*)?(?:\r?\n|$)"
    )
    _record_id = re.compile(
        br"(?m)^[ \t]*id[ \t]*=[ \t]*(?:\"([^\"\r\n]+)\"|'([^'\r\n]+)')"
    )

    def __init__(self, path: Union[str, os.PathLike]):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def read_report(self) -> dict:
        """Return valid events plus explicit issues without hiding either."""
        report = {"events": [], "issues": []}
        if not self.path.exists():
            return report
        content = self.path.read_bytes()
        if not content.strip():
            return report
        matches = list(self._record_header.finditer(content))
        if not matches:
            report["issues"].append("no [[event]] record header")
            return report
        seen_ids = set()
        for index, match in enumerate(matches, start=1):
            end = matches[index].start() if index < len(matches) else len(content)
            record = content[match.start():end]
            id_hint = None
            id_match = self._record_id.search(record)
            if id_match:
                id_hint = (id_match.group(1) or id_match.group(2)).decode(
                    "utf-8", errors="replace"
                )
            try:
                data = tomllib.load(io.BytesIO(record))
                items = data.get("event", data.get("events"))
                if not isinstance(items, list) or len(items) != 1:
                    raise ValueError("event record must contain exactly one table")
                item = items[0]
                if not isinstance(item, dict):
                    raise ValueError("event record must be a table")
                raw_id = item.get("id")
                if raw_id is not None:
                    id_hint = str(raw_id)
                if id_hint is not None:
                    if id_hint in seen_ids:
                        raise ValueError(f"duplicate event id: {id_hint}")
                    seen_ids.add(id_hint)
                report["events"].append(
                    {
                        "id": id_hint,
                        "ts": item.get("ts", item.get("timestamp")),
                        "author": item.get("author"),
                        "ctx": item.get("ctx"),
                        "type": item.get("type"),
                        "body": item.get("body"),
                    }
                )
            except (KeyError, TypeError, ValueError) as exc:
                report["issues"].append(
                    f"event record {index} (id {id_hint}): {exc}"
                )
        return report

    def append(
        self,
        *,
        author: str,
        type: str,
        body: str,
        ctx: str,
        ts: Optional[str] = None,
    ) -> dict:
        """Append one minimal event record and return {event_id, hash}."""
        if not author or not type or not body or not ctx:
            raise ValueError("event requires author, type, body, ctx")
        timestamp = ts or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        _require_lock_backend()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open('a+b') as f:
            with _FileLock(f):
                f.seek(0)
                report = self.read_report()
                if report["issues"]:
                    raise AetherMindParseError(
                        f"failed to parse corrupt events store {self.path}: "
                        f"{report['issues'][0]}"
                    )
                existing = report["events"]
                next_num = max(
                    (int(ev["id"]) for ev in existing if ev.get("id", "").isdigit()),
                    default=0,
                ) + 1
                event_id = str(next_num).zfill(4)
                while any(ev.get("id") == event_id for ev in existing):
                    next_num += 1
                    event_id = str(next_num).zfill(4)
                entry = (
                    "[[event]]\n"
                    f"id     = {_toml_string(event_id)}\n"
                    f"ts     = {_toml_string(timestamp)}\n"
                    f"author = {_toml_string(author)}\n"
                    f"ctx    = {_toml_string(ctx)}\n"
                    f"type   = {_toml_string(type)}\n"
                    f"body   = {_toml_string(body)}\n\n"
                )
                encoded = entry.encode("utf-8")
                f.seek(0, os.SEEK_END)
                if f.write(encoded) != len(encoded):
                    raise OSError("short write while appending event")
                f.flush()
                os.fsync(f.fileno())
        return {
            "event_id": event_id,
            "hash": hashlib.sha256(encoded).hexdigest(),
        }


class LayerStore:

    _record_header = re.compile(
        br"(?m)^[ \t]*\[\[(?:layer|layers)\]\][ \t]*(?:\#[^\r\n]*)?(?:\r?\n|$)"
    )
    _record_id = re.compile(
        br"(?m)^[ \t]*id[ \t]*=[ \t]*(?:\"([^\"\r\n]+)\"|'([^'\r\n]+)')"
    )

    def __init__(self, path: Union[str, os.PathLike]):
        self.path = Path(path)
        self._ensure_dir()

    def _ensure_dir(self):
        """Ensure parent directory exists."""
        self.path.parent.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def _layer_from_item(item: dict) -> AetherLayer:
        return AetherLayer(
            id=item['id'],
            ts=item.get('ts', item.get('timestamp')),
            author=item['author'],
            type=item['type'],
            body=item['body'],
            ctx=item['ctx'],
            conf=item['conf'],
            markers=item.get('markers', []),
            primitive=item.get('primitive', 'layer'),
            thread_key=item.get('thread_key'),
            inherit=item.get('inherit'),
            action_rule=item.get('action_rule'),
            review_after=item.get('review_after'),
            nutrition=item.get('nutrition'),
            force_reason=item.get('force_reason'),
            supersedes=item.get('supersedes', []),
            rollback_of=item.get('rollback_of', []),
            corrects=item.get('corrects', []),
            prev_hash=item.get('prev_hash'),
            archived_to=item.get('archived_to'),
            evidence=item.get('evidence', []),
            recurrence_of=item.get('recurrence_of', []),
            verification=item.get('verification', []),
            artifact=item.get('artifact'),
            artifact_ref=item.get('artifact_ref'),
            anchor=item.get('anchor'),
            ref=item.get('ref'),
            kind=item.get('kind'),
            label=item.get('label'),
            host=item.get('host'),
            repo_root=item.get('repo_root'),
            content_id=item.get('content_id'),
            selector=item.get('selector'),
            span_hint=item.get('span_hint'),
            domain=item.get('domain'),
            symptom=item.get('symptom'),
            next_verification=item.get('next_verification'),
            suspected_mechanism=item.get('suspected_mechanism'),
            scope=item.get('scope'),
            severity=item.get('severity'),
            owner_hint=item.get('owner_hint'),
            repair=item.get('repair'),
            reason=item.get('reason'),
            replacement=item.get('replacement'),
            restored_to=item.get('restored_to'),
            inline=item.get('inline'),
        )

    def read_report_bytes(self, content: bytes) -> LayerReadReport:
        """Parse each record alone so a corrupt tail cannot erase valid history."""
        report = LayerReadReport()
        if not content.strip():
            return report

        matches = list(self._record_header.finditer(content))
        if not matches:
            report.issues.append(
                LayerReadIssue(1, None, 0, "no [[layer]] record header")
            )
            return report

        prefix = content[:matches[0].start()]
        prefix_has_data = any(
            line.strip() and not line.lstrip().startswith(b"#")
            for line in prefix.splitlines()
        )
        if prefix_has_data:
            report.issues.append(
                LayerReadIssue(0, None, 0, "data before first [[layer]] record")
            )

        seen_ids = set()
        for index, match in enumerate(matches, start=1):
            end = matches[index].start() if index < len(matches) else len(content)
            record = content[match.start():end]
            id_hint_match = self._record_id.search(record)
            id_hint = (
                (id_hint_match.group(1) or id_hint_match.group(2)).decode(
                    "utf-8", errors="replace"
                )
                if id_hint_match else None
            )
            try:
                data = tomllib.load(io.BytesIO(record))
                items = data.get('layer', data.get('layers'))
                if not isinstance(items, list) or len(items) != 1:
                    raise ValueError("record must contain exactly one layer table")
                item = items[0]
                if not isinstance(item, dict):
                    raise ValueError("layer record must be a table")
                raw_id = item.get('id')
                if raw_id is not None:
                    id_hint = str(raw_id)
                if id_hint is not None:
                    if id_hint not in report.reserved_ids:
                        report.reserved_ids.append(id_hint)
                    if id_hint in seen_ids:
                        raise ValueError(f"duplicate layer id: {id_hint}")
                    seen_ids.add(id_hint)
                layer = self._layer_from_item(item)
                report.layers.append(layer)
            except (KeyError, TypeError, ValueError) as exc:
                if id_hint is not None and id_hint not in report.reserved_ids:
                    report.reserved_ids.append(id_hint)
                report.issues.append(
                    LayerReadIssue(index, id_hint, match.start(), str(exc))
                )
        return report

    def _raise_report(self, report: LayerReadReport) -> None:
        issue = report.issues[0]
        identity = f" id {issue.layer_id}" if issue.layer_id else ""
        raise AetherMindParseError(
            f"failed to parse corrupt AetherMind store {self.path}: record "
            f"{issue.record_index}{identity}: {issue.message}",
            report=report,
        )

    def _parse_bytes(self, content: bytes) -> List[AetherLayer]:
        report = self.read_report_bytes(content)
        if report.issues:
            self._raise_report(report)
        return report.layers

    def _verified_serialized_record(self, layer: AetherLayer) -> bytes:
        encoded = _format_layer(layer).encode('utf-8')
        report = self.read_report_bytes(encoded)
        if not report.healthy or len(report.layers) != 1:
            raise ValueError("serialized layer failed isolated TOML round-trip")
        if asdict(report.layers[0]) != asdict(layer):
            raise ValueError("serialized layer changed during isolated TOML round-trip")
        return encoded

    def _verify_acknowledged_write(self, file_obj, expected: AetherLayer) -> None:
        file_obj.seek(0)
        layers = self._parse_bytes(file_obj.read())
        matches = [layer for layer in layers if layer.id == expected.id]
        if len(matches) != 1 or asdict(matches[0]) != asdict(expected):
            raise AetherMindParseError(
                f"write acknowledgment failed read-back for layer {expected.id}"
            )

    def write(self, layer: AetherLayer):
        """Append a fully-formed layer after isolated and full-store read-back."""
        encoded = self._verified_serialized_record(layer)
        _require_lock_backend()
        self._ensure_dir()
        with self.path.open('a+b') as f:
            with _FileLock(f):
                f.seek(0)
                report = self.read_report_bytes(f.read())
                if report.issues:
                    self._raise_report(report)
                if layer.id in report.reserved_ids:
                    raise ValueError(f"duplicate layer id: {layer.id}")
                f.seek(0, os.SEEK_END)
                if f.write(encoded) != len(encoded):
                    raise OSError("short write while appending layer")
                f.flush()
                os.fsync(f.fileno())
                self._verify_acknowledged_write(f, layer)

    def append(
        self,
        *,
        author: str,
        type: str,
        body: str,
        ctx: str,
        conf: float,
        markers: Sequence[str],
        primitive: str = "layer",
        thread_key: Optional[str] = None,
        inherit: Optional[str] = None,
        action_rule: Optional[str] = None,
        review_after: Optional[str] = None,
        nutrition: Optional[str] = None,
        force_reason: Optional[str] = None,
        supersedes: Optional[Sequence[str]] = None,
        rollback_of: Optional[Sequence[str]] = None,
        corrects: Optional[Sequence[str]] = None,
        prev_hash: Optional[str] = None,
        evidence: Optional[Sequence[str]] = None,
        recurrence_of: Optional[Sequence[str]] = None,
        verification: Optional[Sequence[str]] = None,
        artifact: Optional[str] = None,
        artifact_ref: Optional[str] = None,
        anchor: Optional[str] = None,
        ref: Optional[str] = None,
        kind: Optional[str] = None,
        label: Optional[str] = None,
        host: Optional[str] = None,
        repo_root: Optional[str] = None,
        content_id: Optional[str] = None,
        selector: Optional[str] = None,
        span_hint: Optional[str] = None,
        domain: Optional[str] = None,
        symptom: Optional[str] = None,
        next_verification: Optional[str] = None,
        suspected_mechanism: Optional[str] = None,
        scope: Optional[str] = None,
        severity: Optional[str] = None,
        owner_hint: Optional[str] = None,
        repair: Optional[str] = None,
        reason: Optional[str] = None,
        replacement: Optional[str] = None,
        restored_to: Optional[str] = None,
        inline: Optional[bool] = None,
        ts: Optional[str] = None,
        layer_id: Optional[str] = None,
    ) -> AetherLayer:
        """Create and append a layer while holding the store lock."""
        timestamp = ts or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

        def build_layer(layer_id: str) -> AetherLayer:
            return AetherLayer(
                id=layer_id,
                ts=timestamp,
                author=author,
                type=type,
                body=body,
                ctx=ctx,
                conf=conf,
                markers=cast(List[str], markers),
                primitive=primitive,
                thread_key=thread_key,
                inherit=inherit,
                action_rule=action_rule,
                review_after=review_after,
                nutrition=nutrition,
                force_reason=force_reason,
                supersedes=cast(List[str], supersedes if supersedes is not None else []),
                rollback_of=cast(List[str], rollback_of if rollback_of is not None else []),
                corrects=cast(List[str], corrects if corrects is not None else []),
                prev_hash=prev_hash,
                evidence=cast(List[str], evidence if evidence is not None else []),
                recurrence_of=cast(
                    List[str], recurrence_of if recurrence_of is not None else []
                ),
                verification=cast(
                    List[str], verification if verification is not None else []
                ),
                artifact=artifact,
                artifact_ref=artifact_ref,
                anchor=anchor,
                ref=ref,
                kind=kind,
                label=label,
                host=host,
                repo_root=repo_root,
                content_id=content_id,
                selector=selector,
                span_hint=span_hint,
                domain=domain,
                symptom=symptom,
                next_verification=next_verification,
                suspected_mechanism=suspected_mechanism,
                scope=scope,
                severity=severity,
                owner_hint=owner_hint,
                repair=repair,
                reason=reason,
                replacement=replacement,
                restored_to=restored_to,
                inline=inline,
            )

        # Validate and round-trip every caller-controlled field before opening
        # the append target. Invalid input therefore cannot create or alter it.
        self._verified_serialized_record(build_layer("0000"))
        _require_lock_backend()
        self._ensure_dir()
        with self.path.open('a+b') as f:
            with _FileLock(f):
                f.seek(0)
                report = self.read_report_bytes(f.read())
                if report.issues:
                    self._raise_report(report)
                corrects_list = list(corrects or [])
                known = set(report.reserved_ids)
                for target in corrects_list:
                    if target not in known:
                        raise ValueError(
                            f"correction target layer {target!r} does not exist "
                            f"in this store; a correction must reference a known layer"
                        )
                resolved_prev = prev_hash
                if resolved_prev is None and report.layers:
                    resolved_prev = _layer_hash(report.layers[-1])
                if layer_id is not None:
                    # External writers must supply a UUID. Numeric ids belong
                    # to this writer's counter, so the two id spaces cannot collide.
                    if _UUID_RE.fullmatch(layer_id) is None:
                        raise ValueError(
                            "explicit layer_id must be a lowercase UUID, got "
                            f"{layer_id!r}"
                        )
                    if layer_id in known:
                        raise ValueError(
                            f"layer id {layer_id} already exists in this store"
                        )
                    chosen_id = layer_id
                else:
                    chosen_id = self.next_id(report.layers, report.reserved_ids)
                layer = build_layer(chosen_id)
                layer.prev_hash = resolved_prev
                encoded = self._verified_serialized_record(layer)
                f.seek(0, os.SEEK_END)
                end = f.tell()
                if end > 0:
                    # A store whose final byte is not a newline would
                    # fuse the next record header onto the last line
                    # and corrupt both. Guard the boundary.
                    f.seek(end - 1)
                    last_byte = f.read(1)
                    f.seek(end)
                    if last_byte != b"\n":
                        f.write(b"\n")
                        encoded = b"\n" + encoded
                written = f.write(encoded)
                if written != len(encoded):
                    # Roll the file back to its pre-append size so a
                    # partial record can never poison later writes.
                    f.truncate(end)
                    f.flush()
                    os.fsync(f.fileno())
                    raise OSError(
                        "short write while appending layer; file "
                        f"restored to pre-append size {end}"
                    )
                f.flush()
                os.fsync(f.fileno())
                self._verify_acknowledged_write(f, layer)
                setattr(layer, "receipt_hash", _layer_hash(layer))
                return layer

    def read(self) -> List[AetherLayer]:
        """Strictly read all layers; corruption raises with a salvage report."""
        if not self.path.exists():
            return []
        with self.path.open('rb') as f:
            return self._parse_bytes(f.read())

    def read_report(self) -> LayerReadReport:
        """Return valid records plus explicit issues without hiding either."""
        if not self.path.exists():
            return LayerReadReport()
        with self.path.open('rb') as f:
            return self.read_report_bytes(f.read())

    def next_id(
        self,
        layers: Optional[Sequence[AetherLayer]] = None,
        reserved_ids: Optional[Sequence[str]] = None,
    ) -> str:
        """Return the next numeric id after every reserved textual id."""
        if layers is None:
            report = self.read_report()
            if report.issues:
                self._raise_report(report)
            layers = report.layers
            reserved_ids = report.reserved_ids
        all_ids = set(reserved_ids or []) | {layer.id for layer in layers}
        numeric_ids = []
        for layer_id in all_ids:
            try:
                numeric_ids.append(int(layer_id))
            except ValueError:
                continue
        if not numeric_ids:
            return "0001"
        return f"{max(numeric_ids) + 1:04d}"

# --- Project-key aliasing registry ---
#
# A store may declare a stable project key (e.g. "sample-project") in its own
# .aethermind/project.json metadata. A central, host-local registry maps
# that key to the store's filesystem root so every API can address the
# store by key as an alternative to the filesystem root. This is addressing
# only: two stores that happen to declare the same key are never merged.
# register_project_key() raises on a conflicting re-registration unless the
# caller passes overwrite=True.

_REGISTRY_PATH = Path.home() / ".aethermind" / "project_registry.json"


def registry_path() -> Path:
    """Host-local alias file. Tests and other installs can point it elsewhere."""
    override = os.environ.get("AETHERMIND_REGISTRY", "").strip()
    if override:
        return Path(override)
    return _REGISTRY_PATH


def _atomic_write_json(path: Path, payload: dict) -> None:
    """Durably replace one small JSON control file in its own directory."""
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
        delete=False,
    )
    tmp_path = Path(handle.name)
    try:
        with handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, path)
        try:
            directory_fd = os.open(path.parent, os.O_RDONLY)
        except OSError:
            return
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if tmp_path.exists():
            tmp_path.unlink()


def _load_registry() -> dict:
    path = registry_path()
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise RegistryError(f"failed to read project registry {path}: {exc}") from exc
    if not isinstance(data, dict) or any(
        not isinstance(key, str) or not isinstance(value, str)
        for key, value in data.items()
    ):
        raise RegistryError(f"project registry {path} must map strings to strings")
    return data


def _save_registry(registry: dict) -> None:
    _atomic_write_json(registry_path(), registry)


def _project_metadata(root: Path) -> dict:
    path = root / ".aethermind" / "project.json"
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise RegistryError(f"failed to read project metadata {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise RegistryError(f"project metadata {path} must be a JSON object")
    return payload


def _ensure_project_id(meta: dict, root: Path) -> str:
    """Issue once on explicit initialization; validate on every later write."""
    raw = meta.get("project_id")
    if raw is None:
        project_id = str(uuid.uuid4())
        meta["project_id"] = project_id
        return project_id
    try:
        project_id = str(uuid.UUID(str(raw)))
    except (ValueError, AttributeError, TypeError) as exc:
        raise RegistryError(
            f"project metadata at {root} has invalid project_id {raw!r}"
        ) from exc
    meta["project_id"] = project_id
    return project_id


def _initialize_store_metadata(root: Path, key: Optional[str] = None) -> bool:
    aethermind_dir = root / ".aethermind"
    created = not aethermind_dir.exists()
    aethermind_dir.mkdir(parents=True, exist_ok=True)
    meta = _project_metadata(root)
    _ensure_project_id(meta, root)
    if key is not None:
        existing_key = meta.get("key") or meta.get("project_key")
        if existing_key and existing_key != key:
            raise RegistryError(
                f"project metadata at {root} declares key {existing_key!r}, not {key!r}"
            )
        meta["key"] = key
        meta["project_key"] = key
    meta.pop("project_root", None)
    meta.update(
        {
            "format_version": FORMAT_VERSION,
            "runtime_id": RUNTIME_ID,
        }
    )
    _atomic_write_json(aethermind_dir / "project.json", meta)
    return created


def initialize_store(
    project_root: Union[str, os.PathLike], project_key: Optional[str] = None
) -> dict:
    """Explicitly initialize an AetherMind store without writing continuity data."""
    root = Path(project_root).expanduser().resolve()
    created = not (root / ".aethermind").exists()
    if project_key is not None:
        result = register_project_key(root, project_key)
        return {**result, "created": created}
    _initialize_store_metadata(root)
    return {
        "project_root": str(root),
        "project_id": _project_metadata(root)["project_id"],
        "created": created,
    }


def _resolve_project_key(root_or_key: str) -> str:
    """Resolve a simple registered key while keeping literal paths independent."""
    candidate = Path(root_or_key).expanduser()
    if candidate.is_absolute() or os.sep in root_or_key or root_or_key.startswith("."):
        return str(candidate)
    resolved = _load_registry().get(root_or_key)
    if resolved is None:
        return root_or_key
    root = Path(resolved).expanduser().resolve()
    metadata = _project_metadata(root)
    declared_key = metadata.get("key") or metadata.get("project_key")
    if declared_key != root_or_key:
        raise RegistryError(
            f"registry key {root_or_key!r} points to {root}, but project metadata "
            f"declares {declared_key!r}"
        )
    return str(root)


def register_project_key(
    project_root: Union[str, os.PathLike], key: str, overwrite: bool = False
) -> dict:
    """Register a host-local alias only after writing source-local identity."""
    if not isinstance(key, str) or not key:
        raise ValueError("key must be a non-empty string")
    if os.sep in key or key in {".", ".."}:
        raise ValueError("key must be a simple name, not a filesystem path")

    _require_lock_backend()
    resolved_root = str(Path(project_root).expanduser().resolve())
    registry_file = registry_path()
    registry_file.parent.mkdir(parents=True, exist_ok=True)
    lock_path = registry_file.with_suffix(registry_file.suffix + ".lock")
    with lock_path.open("a+b") as lock_file:
        with _FileLock(lock_file):
            registry = _load_registry()
            existing = registry.get(key)
            if existing and existing != resolved_root and not overwrite:
                raise ValueError(
                    f"project key {key!r} is already registered to {existing!r}; "
                    f"pass overwrite=True to repoint it to {resolved_root!r}. "
                    "Stores sharing a key are never auto-merged."
                )
            _initialize_store_metadata(Path(resolved_root), key)
            registry[key] = resolved_root
            _save_registry(registry)

    return {
        "key": key,
        "project_id": _project_metadata(Path(resolved_root))["project_id"],
        "project_root": resolved_root,
    }


# --- Convenience Class ---

class AetherMind:
    """Convenience class to manage AetherMind context."""

    def __init__(self, project_root: Union[str, os.PathLike], create: bool = False):
        resolved_root = _resolve_project_key(str(project_root))
        self.project_root = Path(resolved_root).expanduser().resolve()
        self.aethermind_dir = self.project_root / '.aethermind'
        self.layers_path = self.aethermind_dir / 'layers.aem'
        self.texture_path = self.aethermind_dir / 'texture.aem'
        self.events_path = self.aethermind_dir / 'events.aem'
        self.archive_path = self.aethermind_dir / 'archive.aem'

        # Every adapter now defaults to read-only/non-creating behavior. A new
        # store appears only after initialize_store(), register_project_key(),
        # or an explicit create=True call.
        self._store_existed = self.aethermind_dir.exists()
        self._create_authorized = bool(create) or self._store_existed

        if create and not self._store_existed:
            _initialize_store_metadata(self.project_root)
        if self._create_authorized:
            self.aethermind_dir.mkdir(parents=True, exist_ok=True)
            self.layer_store = LayerStore(self.layers_path)
            self.texture_store = TextureStore(self.texture_path)
            self.event_store = EventStore(self.events_path)
        else:
            self.layer_store = None
            self.texture_store = None
            self.event_store = None

    def _require_store(self) -> None:
        if self.layer_store is None or self.texture_store is None:
            raise AutoInitBlocked(
                f"No AetherMind store exists at {self.project_root} and "
                "creation was not authorized. Pass create=True to "
                "AetherMind(...)/the write_layer or write_texture tool, or "
                "call register_project_key(project_root, key) first, to "
                "initialize a new store."
            )

    def write_layer(
        self,
        type: str,
        body: str,
        ctx: str,
        conf: float = 1.0,
        markers: Optional[List[str]] = None,
        author: Optional[str] = None,
        primitive: str = "layer",
        thread_key: Optional[str] = None,
        inherit: Optional[str] = None,
        action_rule: Optional[str] = None,
        review_after: Optional[str] = None,
        nutrition: Optional[str] = None,
        force_reason: Optional[str] = None,
        supersedes: Optional[List[str]] = None,
        rollback_of: Optional[List[str]] = None,
        corrects: Optional[List[str]] = None,
        prev_hash: Optional[str] = None,
        evidence: Optional[List[str]] = None,
        recurrence_of: Optional[List[str]] = None,
        verification: Optional[List[str]] = None,
        artifact: Optional[str] = None,
        artifact_ref: Optional[str] = None,
        anchor: Optional[str] = None,
        ref: Optional[str] = None,
        kind: Optional[str] = None,
        label: Optional[str] = None,
        host: Optional[str] = None,
        repo_root: Optional[str] = None,
        content_id: Optional[str] = None,
        selector: Optional[str] = None,
        span_hint: Optional[str] = None,
        domain: Optional[str] = None,
        symptom: Optional[str] = None,
        next_verification: Optional[str] = None,
        suspected_mechanism: Optional[str] = None,
        scope: Optional[str] = None,
        severity: Optional[str] = None,
        owner_hint: Optional[str] = None,
        repair: Optional[str] = None,
        reason: Optional[str] = None,
        replacement: Optional[str] = None,
        restored_to: Optional[str] = None,
        inline: Optional[bool] = None,
        layer_id: Optional[str] = None,
    ):
        """Write a new layer with auto-generated metadata."""
        self._require_store()
        if markers is None:
            markers = []

        return self.layer_store.append(
            author=author or os.environ.get("AETHERMIND_AUTHOR", "unknown"),
            layer_id=layer_id,
            type=type,
            body=body,
            ctx=ctx,
            conf=conf,
            markers=markers,
            primitive=primitive,
            thread_key=thread_key,
            inherit=inherit,
            action_rule=action_rule,
            review_after=review_after,
            nutrition=nutrition,
            force_reason=force_reason,
            supersedes=supersedes,
            rollback_of=rollback_of,
            corrects=corrects,
            prev_hash=prev_hash,
            evidence=evidence,
            recurrence_of=recurrence_of,
            verification=verification,
            artifact=artifact,
            artifact_ref=artifact_ref,
            anchor=anchor,
            ref=ref,
            kind=kind,
            label=label,
            host=host,
            repo_root=repo_root,
            content_id=content_id,
            selector=selector,
            span_hint=span_hint,
            domain=domain,
            symptom=symptom,
            next_verification=next_verification,
            suspected_mechanism=suspected_mechanism,
            scope=scope,
            severity=severity,
            owner_hint=owner_hint,
            repair=repair,
            reason=reason,
            replacement=replacement,
            restored_to=restored_to,
            inline=inline,
        )

    def write_texture(
        self,
        body: str,
        ctx: str,
        marker: Optional[str] = None,
        author: Optional[str] = None,
    ) -> dict:
        """Append one canonical texture entry; legacy entries remain untouched."""
        self._require_store()
        body = _required_string(body, "body")
        ctx = _required_string(ctx, "ctx")
        author = _required_string(
            author or os.environ.get("AETHERMIND_AUTHOR", "unknown"), "author"
        )
        if marker is not None and marker not in {"?", "!", "~", ">"}:
            raise ValueError("marker must be one of ?, !, ~, >")
        prose = body if marker is None or body.startswith(marker) else marker + body
        date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        entry = f"{date} [{author} | {ctx}]\n{prose}\n---"
        self.texture_store.write(entry)
        return {
            "date": date,
            "author": author,
            "ctx": ctx,
            "marker": marker,
            "body": body,
            "entry": entry,
        }

    def read_texture(self) -> str:
        """Return the full texture file content."""
        if self.texture_store is None:
            return ''
        return self.texture_store.read()

    def write_event(
        self,
        type: str,
        body: str,
        ctx: str,
        author: Optional[str] = None,
    ) -> dict:
        """Append a cheap routine event to events.aem (split ledger).

        Events never enter currentness or the default brief; they keep the
        decision ledger dense. Returns {event_id, hash}.
        """
        self._require_store()
        assert self.event_store is not None
        return self.event_store.append(
            author=author or os.environ.get("AETHERMIND_AUTHOR", "unknown"),
            type=_required_string(type, "type"),
            body=_required_string(body, "body"),
            ctx=_required_string(ctx, "ctx"),
        )

    def read_events(self) -> dict:
        """Return events plus explicit issues; empty store is healthy."""
        if self.event_store is None:
            return {"events": [], "issues": []}
        return self.event_store.read_report()

    def archive(self, layer_ids: Sequence[str], reason: str = "") -> dict:
        """Append-only archive migration: tombstone + continuation header.

        The archived records stay in layers.aem as immutable history; a
        tombstone layer marks them inactive via `archived_to` and the full
        records are copied to archive.aem with a continuation header. The
        ledger is never rewritten in place.
        """
        self._require_store()
        if self.layer_store is None:
            raise AutoInitBlocked("no store to archive")
        layers = self.layer_store.read()
        by_id = {layer.id: layer for layer in layers}
        missing = [layer_id for layer_id in layer_ids if layer_id not in by_id]
        if missing:
            raise ValueError(f"cannot archive unknown layers: {missing}")
        _require_lock_backend()
        self.aethermind_dir.mkdir(parents=True, exist_ok=True)
        with self.archive_path.open('a', encoding='utf-8') as af:
            with _FileLock(af):
                first = af.tell() == 0
                if first:
                    af.write(
                        "# AetherMind archive.aem continuation ledger\n"
                        f"# source = {self.layers_path}\n"
                        f"# created = {datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')}\n"
                        f"# header_hashes = {json.dumps(sorted(layer_ids))}\n"
                    )
                for layer_id in sorted(layer_ids):
                    layer = by_id[layer_id]
                    af.write(_format_layer(layer))
                af.flush()
                os.fsync(af.fileno())
        tombstone = self.write_layer(
            type="correction",
            body=(
                f"!Archived {len(layer_ids)} record(s) to archive.aem."
                + (f" Reason: {reason}" if reason else "")
            ),
            ctx="archive/migration",
            conf=1.0,
            markers=["!"],
            author=os.environ.get("AETHERMIND_AUTHOR", "unknown"),
            supersedes=list(layer_ids),
            evidence=[str(self.archive_path)],
        )
        # Mark archived_to on the tombstone via a direct update is not
        # allowed (append-only); the evidence + supersedes edge carry the
        # audit trail, matching the archive-as-migration contract.
        return {
            "archived": sorted(layer_ids),
            "archive_path": str(self.archive_path),
            "tombstone_id": tombstone.id,
            "tombstone_hash": getattr(tombstone, "receipt_hash", None),
        }

    def read_layers(
        self,
        ctx: Optional[str] = None,
        type: Optional[str] = None,
        author: Optional[str] = None,
        since_ts: Optional[str] = None,
        last_n: Optional[int] = None,
        ctx_prefix: Optional[str] = None,
        markers_any: Optional[List[str]] = None,
        text: Optional[str] = None,
    ) -> List[AetherLayer]:
        """Read layers with optional filters, composable together.

        ctx: substring match against ctx (original behavior, unchanged).
        type: exact match against layer type.
        author: exact match against author.
        since_ts: ISO-8601 UTC timestamp string. Only layers with ts
            strictly greater than since_ts are returned (exclusive — "what
            happened after this point"). Timestamps are RFC3339 UTC
            ('...Z'), which sort lexicographically in chronological order,
            so this is a plain string comparison.
        last_n: after all other filters are applied, keep only the most
            recent `last_n` layers, returned newest-first. Without last_n,
            layers are returned in store (append/oldest-first) order,
            matching prior behavior exactly.
        ctx_prefix: path-segment prefix match against ctx. Matches when
            layer.ctx == ctx_prefix or layer.ctx starts with
            f"{ctx_prefix}/". Unlike `ctx` (raw substring match), this will
            not match "book4x/..." when given ctx_prefix="book4".
        markers_any: include a layer if it has at least one marker in this
            list.
        text: case-insensitive substring of the body, the context, or a
            marker. Empty means no text filter.
        """
        if self.layer_store is None:
            return []
        layers = self.layer_store.read()
        since_value = _parse_rfc3339(since_ts) if since_ts else None
        needle = text.lower() if text else ""

        filtered = []
        for layer in layers:
            if ctx and ctx not in layer.ctx:
                continue
            if type and type != layer.type:
                continue
            if author and author != layer.author:
                continue
            if since_value and not (_parse_rfc3339(layer.ts) > since_value):
                continue
            if ctx_prefix and not (
                layer.ctx == ctx_prefix or layer.ctx.startswith(f"{ctx_prefix}/")
            ):
                continue
            if markers_any and not (set(layer.markers or []) & set(markers_any)):
                continue
            if needle:
                body_text = layer.body or ""
                context_text = layer.ctx or ""
                marker_hit = False
                for marker in layer.markers or []:
                    if needle in str(marker).lower():
                        marker_hit = True
                        break
                body_hit = needle in body_text.lower()
                context_hit = needle in context_text.lower()
                if not body_hit and not context_hit and not marker_hit:
                    continue
            filtered.append(layer)

        if last_n is not None:
            filtered = sorted(filtered, key=_layer_order_key, reverse=True)
            filtered = filtered[: max(0, int(last_n))]

        return filtered

    def status_summary(self) -> dict:
        """Return layer count, last author, last timestamp, and type
        breakdown. Store-existence-safe: an uninitialized/unauthorized
        store reports as empty rather than raising.
        """
        layers = self.read_layers()
        if not layers:
            return {
                "layer_count": 0,
                "last_author": None,
                "last_timestamp": None,
                "last_ts": None,
                "type_breakdown": {},
            }
        sorted_layers = sorted(layers, key=_layer_order_key, reverse=True)
        last_layer = sorted_layers[0]
        type_breakdown: dict = {}
        for layer in layers:
            type_breakdown[layer.type] = type_breakdown.get(layer.type, 0) + 1
        return {
            "layer_count": len(layers),
            "last_author": last_layer.author,
            "last_timestamp": last_layer.ts,
            "last_ts": last_layer.ts,
            "type_breakdown": type_breakdown,
        }

    def currentness(self, as_of: Optional[str] = None) -> dict:
        """Derive active heads without deleting or rewriting immutable history."""
        if self.layer_store is None:
            return derive_currentness([], as_of=as_of)
        return derive_currentness(self.layer_store.read(), as_of=as_of)

    def brief(self, as_of: Optional[str] = None) -> dict:
        """Return active digests and per-scope post-digest deltas.

        Each digest is a watermark only for its own context subtree. The most
        specific matching digest controls a nested layer, so a newer unrelated
        digest can never hide a valid delta in another scope. Superseded,
        rolled-back, and thread-replaced records remain in history but do not
        enter the normal briefing as live posture.
        """
        projection = self.currentness(as_of=as_of)
        history = projection["history"]
        active_heads = projection["active_heads"]
        if not history:
            return {
                "digests": [],
                "deltas": [],
                "newest_digest_ts": None,
                "layer_count": 0,
                "type_breakdown": {},
            }

        digest_by_scope: dict[str, AetherLayer] = {}
        for layer in active_heads:
            if not layer.ctx.endswith("/digest"):
                continue
            scope = layer.ctx[: -len("/digest")].rstrip("/")
            current = digest_by_scope.get(scope)
            if current is None or _layer_order_key(layer) > _layer_order_key(current):
                digest_by_scope[scope] = layer

        digests = sorted(digest_by_scope.values(), key=_layer_order_key)
        deltas: List[AetherLayer] = []
        for layer in active_heads:
            if layer.ctx.endswith("/digest"):
                continue
            matching = [
                (scope, digest)
                for scope, digest in digest_by_scope.items()
                if layer.ctx == scope or layer.ctx.startswith(f"{scope}/")
            ]
            if not matching:
                deltas.append(layer)
                continue
            _, watermark = max(matching, key=lambda pair: len(pair[0]))
            if _layer_order_key(layer) > _layer_order_key(watermark):
                deltas.append(layer)
        deltas.sort(key=_layer_order_key)

        type_breakdown: dict = {}
        for layer in history:
            type_breakdown[layer.type] = type_breakdown.get(layer.type, 0) + 1
        newest_digest_ts = (
            max(digests, key=_layer_order_key).ts if digests else None
        )
        return {
            "digests": digests,
            "deltas": deltas,
            "newest_digest_ts": newest_digest_ts,
            "layer_count": len(history),
            "type_breakdown": type_breakdown,
        }

    # --- Anchor-scoped deterministic read contract ---

    # Types that are unconditionally load-bearing for retrieval. Informational
    # records below this line are thresholded by budget.
    _BRIEF_KEEP_TYPES = frozenset(
        {
            "decision",
            "correction",
            "load-bearing",
            "fork",
            "rollback",
            "supersession",
            "pressure-event",
        }
    )
    _BRIEF_INFORMATIONAL_TYPES = frozenset(
        {"discovery", "friction", "thought", "uncertainty", "observation"}
    )

    def _brief_relevant(self, layer: AetherLayer, anchor: Optional[str]) -> bool:
        """Return whether a layer is in scope for an anchor."""
        if anchor is None:
            return True
        if layer.ctx == anchor or layer.ctx.startswith(f"{anchor}/"):
            return True
        if layer.anchor and anchor in {layer.anchor, f"anchor:{layer.anchor}"}:
            return True
        if anchor in (layer.corrects or []):
            return True
        if anchor in (layer.supersedes or []):
            return True
        return False

    @staticmethod
    def _brief_priority(layer: AetherLayer) -> tuple:
        """Deterministic salience key for thresholding informational records.

        Higher is more load-bearing. Marker weight dominates, then recency so a
        fresh record outranks a stale one with the same marker class.
        """
        marker_weight = 0
        for marker in layer.markers or []:
            if marker == "!":
                marker_weight = max(marker_weight, 4)
            elif marker == ">":
                marker_weight = max(marker_weight, 3)
            elif marker == "?":
                marker_weight = max(marker_weight, 2)
            elif marker == "~":
                marker_weight = max(marker_weight, 1)
        return (marker_weight, _layer_order_key(layer))

    def brief_anchor(
        self,
        anchor: Optional[str] = None,
        budget: int = 4000,
        as_of: Optional[str] = None,
    ) -> dict:
        """Return the anchor-scoped, budget-bounded, deterministic brief.

        Rule-based thinning only; never LLM condensation. The same ledger and
        the same budget produce a byte-identical brief (locked by test via
        ``determinism_key``).

        Keep unconditionally: decision/correction/load-bearing/fork/rollback/
        supersession/pressure-event records relevant to the anchor.
        Threshold: informational records by salience until the budget ceiling.
        Dropped records emit tombstones so the brief is self-describing about
        what it left out.
        """
        projection = self.currentness(as_of=as_of)
        history = projection["history"]
        active_heads = projection["active_heads"]
        budget = max(0, int(budget))

        if not history:
            return {
                "anchor": anchor,
                "kept": [],
                "tombstones": [],
                "must_know": [],
                "unabsorbed_debt": [],
                "budget": budget,
                "budget_used": 0,
                "layer_count": 0,
                "determinism_key": None,
            }

        active_ids = {layer.id for layer in active_heads}
        relevant = [
            layer for layer in history if self._brief_relevant(layer, anchor)
        ]
        must_know_ids: List[str] = []
        for layer in active_heads:
            if layer.type == "correction" and not layer.supersedes:
                if self._brief_relevant(layer, anchor):
                    must_know_ids.append(layer.id)
        must_know_ids.sort()

        kept: List[AetherLayer] = []
        tombstones: List[dict] = []
        budget_used = 0

        unconditional = [
            layer
            for layer in relevant
            if layer.type in self._BRIEF_KEEP_TYPES or layer.id in must_know_ids
        ]
        unconditional.sort(key=_layer_order_key)
        for layer in unconditional:
            kept.append(layer)
            budget_used += len(_format_layer(layer))

        unconditional_ids = {layer.id for layer in unconditional}
        informational = [
            layer
            for layer in relevant
            if layer.id not in unconditional_ids and layer.id in active_ids
        ]
        informational.sort(key=self._brief_priority, reverse=True)
        for layer in informational:
            cost = len(_format_layer(layer))
            if budget_used + cost > budget:
                tombstones.append(
                    {
                        "id": layer.id,
                        "type": layer.type,
                        "ctx": layer.ctx,
                        "dropped": "budget",
                    }
                )
                continue
            kept.append(layer)
            budget_used += cost

        inactive_relevant = [
            layer for layer in relevant if layer.id not in active_ids
        ]
        inactive_relevant.sort(key=_layer_order_key)
        for layer in inactive_relevant:
            tombstones.append(
                {
                    "id": layer.id,
                    "type": layer.type,
                    "ctx": layer.ctx,
                    "dropped": "inactive",
                }
            )

        # Propagation debt: corrections whose blast radius over dependent
        # anchors is not yet absorbed. A correction is absorbed once it is the
        # target of a supersession or rollback. A dependent is any layer that
        # references the corrected target via anchor/supersedes/rollback_of and
        # is not itself superseded/rolled back.
        superseded_ids = {
            target
            for layer in history
            for target in (layer.supersedes or [])
        }
        rolled_back_ids = {
            target
            for layer in history
            for target in (layer.rollback_of or [])
        }
        absorbed_ids = superseded_ids | rolled_back_ids
        debt: List[dict] = []
        for layer in relevant:
            if not layer.corrects:
                continue
            if layer.id in absorbed_ids:
                continue  # absorbed: some later record superseded/rolled it back
            dependents = []
            for other in history:
                if other.id == layer.id or other.id in layer.corrects:
                    continue
                references_target = bool(
                    set(other.corrects or []) & set(layer.corrects)
                    or set(other.supersedes or []) & set(layer.corrects)
                    or set(other.rollback_of or []) & set(layer.corrects)
                )
                if references_target and not other.supersedes:
                    dependents.append(other.id)
            debt.append(
                {
                    "correction": layer.id,
                    "targets": list(layer.corrects),
                    "dependents": sorted(set(dependents)),
                    "unabsorbed": True,
                }
            )
        debt.sort(key=lambda item: item["correction"])

        determinism_payload = json.dumps(
            {
                "anchor": anchor,
                "kept": [layer.id for layer in kept],
                "tombstones": tombstones,
                "must_know": must_know_ids,
                "debt": debt,
            },
            sort_keys=True,
        )
        determinism_key = hashlib.sha256(
            determinism_payload.encode("utf-8")
        ).hexdigest()

        return {
            "anchor": anchor,
            "kept": kept,
            "tombstones": tombstones,
            "must_know": must_know_ids,
            "unabsorbed_debt": debt,
            "budget": budget,
            "budget_used": budget_used,
            "layer_count": len(history),
            "determinism_key": determinism_key,
        }

    def audit(self) -> dict:
        """Hash-chain integrity scan over the ledger.

        Recompute each record's canonical hash and verify the ``prev_hash``
        carry. Legacy records without ``prev_hash`` begin the chain at the
        first post-upgrade record (the migration point) and are reported as
        ``legacy_prefix``.
        """
        layers = self.layer_store.read() if self.layer_store is not None else []
        if not layers:
            return {
                "healthy": True,
                "record_count": 0,
                "legacy_prefix": 0,
                "chain_start": None,
                "chain_breaks": [],
            }
        chain_breaks = []
        legacy_prefix = 0
        chain_start = None
        for index, layer in enumerate(layers):
            if layer.prev_hash is None:
                if chain_start is None:
                    # First record is the chain ROOT, not a legacy gap.
                    chain_start = layer.id
                else:
                    legacy_prefix += 1
                continue
            if chain_start is None:
                chain_start = layer.id
            if index == 0:
                chain_breaks.append(
                    {"id": layer.id, "reason": "first record cannot carry prev_hash"}
                )
                continue
            expected = _layer_hash(layers[index - 1])
            if layer.prev_hash != expected:
                chain_breaks.append(
                    {
                        "id": layer.id,
                        "expected_prev_hash": expected,
                        "stored_prev_hash": layer.prev_hash,
                    }
                )
        return {
            "healthy": not chain_breaks,
            "record_count": len(layers),
            "legacy_prefix": legacy_prefix,
            "chain_start": chain_start,
            "chain_breaks": chain_breaks,
        }

    def gate_check(self, plan: dict, anchor: Optional[str] = None) -> dict:
        """Re-apply retrieval friction at decision time.

        ``plan`` carries the proposed mutation: ``{anchor, planned_type,
        planned_ctx, enforce_gate}``. Advisory by default; when
        ``enforce_gate`` is true, unabsorbed must-know corrections reject the
        plan with the layer IDs a future agent must account for first.
        """
        brief = self.brief_anchor(anchor=anchor or plan.get("anchor"), budget=4000)
        must_know = brief["must_know"]
        debt = brief["unabsorbed_debt"]
        enforce = bool(plan.get("enforce_gate", False))
        blockers = [
            item["correction"]
            for item in debt
            if item["unabsorbed"] and item["correction"] in must_know
        ]
        blockers = sorted(set(blockers))
        verdict = "blocked" if enforce and blockers else "advisory" if blockers else "clear"
        return {
            "verdict": verdict,
            "blockers": blockers,
            "must_know": must_know,
            "unabsorbed_debt": debt,
            "enforce_gate": enforce,
            "determinism_key": brief["determinism_key"],
        }


def runtime_capabilities(
    project_root: Optional[Union[str, os.PathLike]] = None,
) -> dict:
    """Return one machine-readable identity used by every adapter."""
    source_path = Path(__file__).resolve()
    runtime = {
        "runtime_id": RUNTIME_ID,
        "runtime_version": RUNTIME_VERSION,
        "format_version": FORMAT_VERSION,
        "contract_version": "adapter-v1",
        "implementation_path": str(source_path),
        "implementation_sha256": hashlib.sha256(source_path.read_bytes()).hexdigest(),
        "safe_default_create": True,
        "lock_backend": _lock_backend(),
        "locking_scope": "local-posix-advisory" if fcntl is not None else "unavailable",
        "capabilities": [
            "acknowledged-layer-append",
            "explicit-store-init",
            "record-salvage-report",
            "reserved-textual-ids",
            "canonical-texture-write",
            "project-key-metadata-verification",
            "active-head-currentness",
            "scoped-digest-briefing",
            "as-of-projection",
        ],
        "layer_types": sorted(ALLOWED_LAYER_TYPES),
        "primitives": sorted(ALLOWED_LIGHT_PRIMITIVES),
    }
    if project_root is not None:
        aether = AetherMind(project_root, create=False)
        metadata = _project_metadata(aether.project_root) if aether.aethermind_dir.exists() else {}
        runtime["store"] = {
            "initialized": aether.aethermind_dir.exists(),
            "project_root": str(aether.project_root),
            "project_key": metadata.get("key") or metadata.get("project_key"),
        }
    return runtime


# --- High-Level API ---

def write_layer(
    type: str,
    body: str,
    ctx: str,
    conf: float = 1.0,
    markers: Optional[List[str]] = None,
    author: Optional[str] = None,
    primitive: str = "layer",
    thread_key: Optional[str] = None,
    inherit: Optional[str] = None,
    action_rule: Optional[str] = None,
    review_after: Optional[str] = None,
    nutrition: Optional[str] = None,
    force_reason: Optional[str] = None,
    supersedes: Optional[List[str]] = None,
    rollback_of: Optional[List[str]] = None,
    evidence: Optional[List[str]] = None,
    recurrence_of: Optional[List[str]] = None,
    verification: Optional[List[str]] = None,
    artifact: Optional[str] = None,
    artifact_ref: Optional[str] = None,
    anchor: Optional[str] = None,
    ref: Optional[str] = None,
    kind: Optional[str] = None,
    label: Optional[str] = None,
    host: Optional[str] = None,
    repo_root: Optional[str] = None,
    content_id: Optional[str] = None,
    selector: Optional[str] = None,
    span_hint: Optional[str] = None,
    domain: Optional[str] = None,
    symptom: Optional[str] = None,
    next_verification: Optional[str] = None,
    suspected_mechanism: Optional[str] = None,
    scope: Optional[str] = None,
    severity: Optional[str] = None,
    owner_hint: Optional[str] = None,
    repair: Optional[str] = None,
    reason: Optional[str] = None,
    replacement: Optional[str] = None,
    restored_to: Optional[str] = None,
    inline: Optional[bool] = None,
):
    """Write a layer to the default AetherMind context."""
    project_root = os.getcwd()
    aethermind = AetherMind(project_root)
    return aethermind.write_layer(
        type,
        body,
        ctx,
        conf,
        markers,
        author=author,
        primitive=primitive,
        thread_key=thread_key,
        inherit=inherit,
        action_rule=action_rule,
        review_after=review_after,
        nutrition=nutrition,
        force_reason=force_reason,
        supersedes=supersedes,
        rollback_of=rollback_of,
        evidence=evidence,
        recurrence_of=recurrence_of,
        verification=verification,
        artifact=artifact,
        artifact_ref=artifact_ref,
        anchor=anchor,
        ref=ref,
        kind=kind,
        label=label,
        host=host,
        repo_root=repo_root,
        content_id=content_id,
        selector=selector,
        span_hint=span_hint,
        domain=domain,
        symptom=symptom,
        next_verification=next_verification,
        suspected_mechanism=suspected_mechanism,
        scope=scope,
        severity=severity,
        owner_hint=owner_hint,
        repair=repair,
        reason=reason,
        replacement=replacement,
        restored_to=restored_to,
        inline=inline,
    )

def write_texture(body: str, ctx: str, marker: Optional[str] = None):
    """Write a texture entry to the default AetherMind context."""
    project_root = os.getcwd()
    aethermind = AetherMind(project_root)
    return aethermind.write_texture(body, ctx, marker)

def read_layers(
    ctx: Optional[str] = None,
    type: Optional[str] = None,
    author: Optional[str] = None,
    since_ts: Optional[str] = None,
    last_n: Optional[int] = None,
    ctx_prefix: Optional[str] = None,
    markers_any: Optional[List[str]] = None,
    text: Optional[str] = None,
) -> List[AetherLayer]:
    """Read layers from default AetherMind context with filters."""
    project_root = os.getcwd()
    aethermind = AetherMind(project_root)
    return aethermind.read_layers(
        ctx,
        type,
        author,
        since_ts=since_ts,
        last_n=last_n,
        ctx_prefix=ctx_prefix,
        markers_any=markers_any,
        text=text,
    )
