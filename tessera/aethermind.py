"""AetherMind service tools. This module never opens memory tree files."""

import json
import math
import hashlib
import re
import threading
import time

from .mcp_client import McpClient, McpError
from .endpoints import EndpointPool, EndpointUnavailable


class CaptureRejected(McpError):
    """The server will never accept this capture under this reference.

    Distinct from a transport failure: retrying cannot help, and the caller must
    be able to skip the item rather than starve everything queued behind it.
    A capture whose request_id already named a layer with different content used
    to raise a bare McpError, so the outbox walk stopped and every later turn's
    capture was blocked behind one unfixable item.
    """


NO_TREE_MESSAGE = 'No summary tree for this project yet; use read_layers or prepare_inheritance.'
# The vendored engine's capabilities report. Native Windows has no fcntl, so
# lock_backend is "unavailable" and every write is refused. The turn stops on
# this sentence before that write. Linux and macOS report fcntl and do not use it.
EMBEDDED_LOCK_REFUSAL = (
    'embedded AetherMind needs file locking that native Windows lacks; '
    'run Tessera under WSL, or configure an external AetherMind'
)


def _missing_tree(result):
    if isinstance(result, dict):
        if result.get('success') is True:
            return False
        text = result.get('error')
        if result.get('tree_available') is False:
            text = text or result.get('message')
    else:
        text = result
    if not isinstance(text, str):
        return False
    lowered = text.strip().lower()
    return lowered.startswith('no summary tree') or 'run build_tree first' in lowered


def superseded_ids(layers):
    """Ids an explicit archive has superseded.

    Only a layer that NAMES a target in `supersedes` is a tombstone. The `!`
    marker alone is not enough: load-bearing layers carry it too, and 90 of 200
    layers marked `!` on the live store name no target. The ids this returns match
    the server's own `currentness` inactive set exactly.

    Deliberately narrow: `thread-replaced` layers are NOT included. That reason
    is normal per-turn history (1003 of 1005 inactive entries on the live
    store), so dropping it would gut recall.
    """
    found = set()
    for layer in layers:
        if not isinstance(layer, dict):
            continue
        targets = layer.get("supersedes")
        if not isinstance(targets, list):
            continue
        for target in targets:
            if target is not None:
                found.add(str(target))
    return found


def drop_superseded(result):
    """Remove layers an explicit archive superseded, keeping the rest intact.

    `read_layers` returns immutable history: an archived row stays in the store
    with no self-signal (markers, replacement and reason all empty). The only
    pointer is the tombstone, which is always NEWER than its target, so it sits
    in the same window. That is what makes a zero-extra-call filter possible.
    """
    if isinstance(result, dict) and isinstance(result.get("layers"), list):
        layers = result["layers"]
        wrapper = result
    elif isinstance(result, list):
        layers = result
        wrapper = None
    else:
        return result
    gone = superseded_ids(layers)
    if not gone:
        return result
    kept = [layer for layer in layers
            if not (isinstance(layer, dict) and str(layer.get("id")) in gone)]
    omitted = len(layers) - len(kept)
    if not omitted:
        return result
    if wrapper is None:
        return {"layers": kept, "omitted_superseded": omitted}
    return {**wrapper, "layers": kept, "omitted_superseded": omitted}


def rank_layers(result, query, limit):
    terms = [t for t in re.findall(r"[a-z0-9][a-z0-9_.-]+", query.lower()) if len(t) >= 3]
    layers = result.get("layers", []) if isinstance(result, dict) else result if isinstance(result, list) else []
    # The scan is wider than the returned page, so a superseded target can rank
    # into the page while its tombstone falls outside it. Compute the superseded
    # set from the WHOLE scan, before truncating, or the filter silently misses.
    gone = superseded_ids(layers)
    scored = []
    for layer in layers:
        if not isinstance(layer, dict):
            continue
        if str(layer.get("id")) in gone:
            continue
        text = " ".join(str(layer.get(key, "")) for key in ("body", "ctx", "type", "label", "author")).lower()
        hits = sum(1 for term in terms if term in text)
        if hits:
            scored.append((hits, str(layer.get("ts") or layer.get("timestamp") or ""), layer))
    scored.sort(key=lambda item: (item[0], item[1]), reverse=True)
    return {"success": True, "query": query, "scanned": len(layers), "matched": len(scored),
            "omitted_superseded": len(gone), "layers": [layer for _, _, layer in scored[:limit]]}


def tree_read_display(result):
    """Render absence as a normal state, without forwarding remote build advice.

    Raw client results remain available to the explicit diagnostic caller. This
    projection is for session context and tool echoes, never for repairing trees.
    """
    if _missing_tree(result):
        return {'state': 'no_tree', 'message': NO_TREE_MESSAGE}
    return result


class AetherMindClient:
    def __init__(self, url="", token_env="", timeout=15, *, endpoints=None, bundled_path="",
                 endpoint_factory=None, probe_timeout=2, probe_interval=30, create_project=False,
                 read_only_projects=(), recall_scan=400, dedupe_limit=1000, history_limit=256,
                 clock=time.monotonic):
        configured = list(endpoints or [])
        if not configured and url:
            configured = [url]
        # bundled_path sits on the config for the local bundled-engine launcher.
        # This client talks to running MCP services only, so the bundle itself
        # is resolved by the launcher, never passed here as an endpoint.
        self._clients = {}
        for endpoint in configured:
            if isinstance(endpoint, str) and endpoint.startswith('mcp:'):
                # A bundled or locally managed stdio server, named after its
                # mcp_servers entry. Resolving these needs the shared catalog,
                # which callers hand in as a factory; without one the name
                # cannot become a client and must fail loudly at construction.
                if endpoint_factory is None:
                    raise ValueError('aethermind mcp: endpoints require an endpoint factory')
                self._clients[endpoint] = endpoint_factory(endpoint, token_env, timeout)
                continue
            self._clients[endpoint] = McpClient(endpoint, token_env=token_env, timeout=timeout)
        self._current = configured[0] if configured else None
        self.mcp = self._clients.get(self._current)
        self._tools = None
        self._toolsets = {}
        self._layer_types = {}
        self._lock_backends = {}
        self._lock = threading.RLock()
        self._in_operation = False
        self.create_project = create_project
        self.read_only_projects = set(read_only_projects)
        self.recall_scan = max(1, int(recall_scan))
        self.dedupe_limit = dedupe_limit
        self.history_limit = history_limit
        self.calls = []
        self.last_endpoint = None
        self._clock = clock
        self._tree_observations = {}
        self._tree_observation_lock = threading.Lock()
        self._pool = EndpointPool(configured, self._probe, probe_timeout, probe_interval,
                                  retry_on=(McpError, OSError), clock=clock)

    def _probe(self, endpoint, timeout):
        client = self._clients[endpoint]
        previous = client.timeout
        client.timeout = timeout
        try:
            tools = client.list_tools()
            detected = self._validate_tools(tools)
            capabilities = self._unwrap(client.call_tool("capabilities", {}))
            runtime = capabilities.get("runtime") if isinstance(capabilities, dict) else None
            if (not isinstance(capabilities, dict) or capabilities.get("success") is not True
                    or not isinstance(runtime, dict)
                    or runtime.get("runtime_id") != "aethermind-python"
                    or runtime.get("contract_version") != "adapter-v1"):
                raise McpError("AetherMind endpoint requires aethermind-python adapter-v1")
            allowed = runtime.get("layer_types")
            if (not isinstance(allowed, list) or not allowed
                    or any(not isinstance(value, str) or not value for value in allowed)):
                raise McpError("AetherMind endpoint has invalid layer type capabilities")
        except Exception:
            self._toolsets.pop(endpoint, None)
            self._layer_types.pop(endpoint, None)
            self._lock_backends.pop(endpoint, None)
            if endpoint == self._current:
                self._tools = None
            raise
        finally:
            client.timeout = previous
        # Publish only after this endpoint's schemas and capability reply pass.
        self._toolsets[endpoint] = detected
        self._layer_types[endpoint] = list(allowed)
        self._lock_backends[endpoint] = runtime.get('lock_backend')
        if endpoint == self._current:
            self._tools = detected

    @staticmethod
    def _validate_tools(tools):
        signatures = {
            "capabilities": ({"project_root": "string"}, set()),
            "prepare_inheritance": ({"project_root": "string", "anchor": "string",
                "budget": "integer", "surface": "string", "session_id": "string"}, {"project_root"}),
            "view": ({"project_root": "string", "budget_chars": "integer",
                "recent_window": "integer"}, {"project_root"}),
            "zoom": ({"project_root": "string", "level": "integer", "index": "integer"},
                {"project_root", "level", "index"}),
            "read_layers": ({"project_root": "string", "ctx": "string", "type": "string",
                "author": "string", "since_ts": "string", "last_n": "integer",
                "ctx_prefix": "string", "markers_any": "array"}, {"project_root"}),
            "write_layer": ({"project_root": "string", "layer_type": "string", "body": "string",
                "ctx": "string", "conf": "number", "markers": "array", "author": "string",
                "ref": "string", "evidence": "array", "create": "boolean"},
                {"project_root", "layer_type", "body", "ctx", "conf", "markers"}),
            "status": ({"project_root": "string"}, {"project_root"}),
        }
        detected = {}
        for tool in tools:
            if not isinstance(tool, dict) or not isinstance(tool.get("name"), str):
                raise McpError("AetherMind endpoint has an invalid tool entry")
            name = tool["name"]
            if name in detected:
                raise McpError("AetherMind endpoint has duplicate tool names")
            detected[name] = tool
        for name, (fields, expected_required) in signatures.items():
            schema = detected.get(name, {}).get("inputSchema", {})
            properties = schema.get("properties") if isinstance(schema, dict) else None
            required = schema.get("required", []) if isinstance(schema, dict) else None
            if (not isinstance(schema, dict) or schema.get("type") != "object"
                    or not isinstance(properties, dict) or not isinstance(required, list)
                    or any(not isinstance(key, str) for key in required)
                    or set(required) != expected_required):
                raise McpError(f"AetherMind adapter-v1 schema mismatch: {name}")
            for field, kind in fields.items():
                definition = properties.get(field, {})
                variants = definition.get("anyOf", [definition]) if isinstance(definition, dict) else []
                matches = [item for item in variants if isinstance(item, dict) and item.get("type") == kind]
                if not matches or (kind == "array" and not any(
                        item.get("items", {}).get("type") == "string" for item in matches)):
                    raise McpError(f"AetherMind adapter-v1 schema mismatch: {name}.{field}")
        return detected

    def _activate(self, endpoint):
        if endpoint != self._current:
            self._current = endpoint
            self.mcp = self._clients[endpoint]
            self._tools = self._toolsets.get(endpoint)

    def _run(self, operation, label):
        with self._lock:
            def perform(endpoint):
                self._activate(endpoint)
                self._in_operation = True
                try:
                    return operation()
                finally:
                    self._in_operation = False
            try:
                value, endpoint = self._pool.run(perform)
            except EndpointUnavailable as unavailable:
                cause = unavailable.__cause__
                if cause is not None:
                    raise cause
                raise McpError("No configured memory endpoint is reachable") from unavailable
            self.last_endpoint = endpoint
            self.calls.append({"operation": label, "endpoint": endpoint})
            if len(self.calls) > self.history_limit:
                del self.calls[0]
            return value

    def status(self):
        return {"endpoint": self.last_endpoint, "endpoints": self._pool.status(), "calls": list(self.calls)}

    def reported_lock_backend(self):
        """The lock_backend field from the engine's capabilities report.

        Missing means the report did not name one. ``unavailable`` is the
        engine saying it will refuse every write.
        """
        if not self._in_operation:
            return self._run(self.reported_lock_backend, 'capabilities')
        if self._current not in self._lock_backends:
            self._probe(self._current, self.mcp.timeout)
        return self._lock_backends.get(self._current)

    def detect_tools(self, refresh=False):
        if not self._in_operation:
            return self._run(lambda: self.detect_tools(refresh), "tools/list")
        if self._tools is None or refresh:
            self._probe(self._current, self.mcp.timeout)
        mappings = {"inheritance": ("prepare_inheritance",), "overview": ("view",),
                    "zoom": ("zoom",), "write": ("write_layer",),
                    "read": ("read_layers",), "projects": ("status",),
                    "brief": ("brief",)}
        detected = {}
        for operation, candidates in mappings.items():
            detected[operation] = None
            for name in candidates:
                if name in self._tools:
                    detected[operation] = name
                    break
        return detected

    def _mapped_call(self, operation, args):
        if not self._in_operation:
            return self._run(lambda: self._mapped_call(operation, args), operation)
        name = self.detect_tools().get(operation)
        if name is None:
            raise McpError(f"AetherMind server does not expose {operation}")
        schema = self._tools[name].get("inputSchema", {})
        properties = schema.get("properties")
        adapted = dict(args)
        if isinstance(properties, dict):
            if "project_root" in properties and "project_id" in adapted:
                adapted["project_root"] = adapted.pop("project_id")
            if "budget" in properties and "budget_chars" in adapted and "budget_chars" not in properties:
                adapted["budget"] = adapted.pop("budget_chars")
            if "layer" in adapted and "layer" not in properties:
                layer = adapted.pop("layer")
                request_id = adapted.pop("request_id")
                layer = dict(layer)
                scope = hashlib.sha256(request_id.encode("utf-8")).hexdigest()
                layer["ctx"] = layer["ctx"].rstrip("/") + "/captures/" + scope
                existing = self._native_duplicate(adapted["project_root"], request_id, layer)
                if existing is not None:
                    return existing
                adapted.update(layer)
                adapted["layer_type"] = self._native_layer_type(adapted.pop("type"))
                adapted.setdefault("conf", 1.0)
                adapted.setdefault("markers", [])
                if "ref" not in properties:
                    raise McpError("AetherMind path write lacks stable ref support")
                adapted["ref"] = request_id
                evidence = list(adapted.get("evidence", []))
                if request_id not in evidence:
                    evidence.append(request_id)
                adapted["evidence"] = evidence
                adapted["create"] = self.create_project
            body_query = None
            if "query" in adapted and "query" not in properties:
                # Path servers filter ctx only; capture ctx is a hash path. Scan recent layers
                # and match the query words against body and ctx on the client.
                body_query = adapted.pop("query")
                wanted = adapted.get("last_n")
                if "last_n" in properties:
                    adapted["last_n"] = self.dedupe_limit
            filtered = {}
            for key, value in adapted.items():
                if key in properties:
                    filtered[key] = value
            adapted = filtered
            for required in schema.get("required", []):
                if required not in adapted:
                    raise McpError(f"AetherMind tool requires argument {required}")
            if body_query is not None:
                return _match_layers(self.call(name, adapted), body_query, wanted)
        return self.call(name, adapted)

    def _native_layer_type(self, category):
        allowed = self._layer_types.get(self._current)
        if allowed is None:
            if "capabilities" not in self._tools:
                raise McpError("Path server must advertise layer type capabilities")
            capabilities = self.call("capabilities", {})
            allowed = capabilities.get("runtime", {}).get("layer_types", []) if isinstance(capabilities, dict) else []
            self._layer_types[self._current] = allowed
        desired = {"fact": "discovery", "preference": "discovery", "failure": "friction"}.get(category, category)
        if desired in allowed:
            return desired
        if category not in ("decision", "correction", "failure") and "discovery" in allowed:
            return "discovery"
        raise McpError("Memory server does not support the required layer type")

    def _native_duplicate(self, project, request_id, layer):
        result = self.read_layers(project, ctx_prefix=layer["ctx"], last_n=self.dedupe_limit)
        if isinstance(result, dict):
            if result.get("success") is False or result.get("error"):
                # A store that has not been created yet has no prior capture.
                # The write that follows is what creates it when that is allowed.
                if result.get("error_code") == "AUTO_INIT_BLOCKED" or result.get("error_type") == "AutoInitBlocked":
                    return None
                detail = result.get("error") or result.get("error_code") or "read failed"
                raise McpError(f"Cannot reconcile the native capture reference: {detail}")
            layers = result.get("layers")
        else:
            layers = result
        if not isinstance(layers, list):
            raise McpError("Native reference lookup returned no layer list")
        for existing in layers:
            if not isinstance(existing, dict):
                continue
            matched = existing.get("ref") == request_id or request_id in existing.get("evidence", [])
            for key in ("meta", "metadata", "attrs"):
                extra = existing.get(key, {})
                if isinstance(extra, dict) and (extra.get("ref") == request_id or request_id in extra.get("evidence", [])):
                    matched = True
            if matched:
                if existing.get("body") != layer["body"]:
                    # Permanent, not transient. The reference is already spent on
                    # different content, so no number of retries can make this
                    # capture land. Say so with a type the caller can act on.
                    raise CaptureRejected("Native capture reference belongs to different content")
                return {"success": True, "request_id": request_id, "deduplicated": True,
                        "layer_id": existing.get("id")}
        return None

    def call(self, tool, args):
        if not self._in_operation:
            return self._run(lambda: self.call(tool, args), tool)
        result = self.mcp.call_tool(tool, args)
        return self._unwrap(result)

    @staticmethod
    def _unwrap(result):
        for _ in range(8):
            if isinstance(result, dict) and "structuredContent" in result:
                result = result["structuredContent"]
            elif isinstance(result, dict) and "content" in result:
                texts = []
                for item in result["content"]:
                    if item.get("type") == "text":
                        texts.append(item.get("text", ""))
                result = "\n".join(texts)
            elif isinstance(result, dict) and set(result) == {"result"}:
                result = result["result"]
            elif isinstance(result, str):
                try:
                    result = json.loads(result)
                except ValueError:
                    break
            else:
                break
        return result

    def brief(self, project_id, anchor=None, budget=4000):
        """Active digests for one turn. A server without this tool is an error, not a stand-in."""
        tools = self._tools
        if isinstance(tools, dict) and "brief" not in tools:
            # The probe already succeeded. Missing brief is a capability gap, not a dead endpoint,
            # so this raises before the pool can mark the endpoint down.
            raise McpError("AetherMind server does not expose brief")
        return self._mapped_call("brief", {"project_id": project_id, "anchor": anchor, "budget": budget})

    def prepare_inheritance(self, project_id, anchor=None, budget=4000, *, surface=None, session_id=None):
        args = {
            "project_id": project_id, "anchor": anchor, "budget": budget,
        }
        if surface is not None:
            args["surface"] = surface
        if session_id is not None:
            args["session_id"] = session_id
        return self._mapped_call("inheritance", args)

    def tree_view(self, project_id, budget_chars=2000, *, recent_window=None):
        with self._lock:
            args = {"project_id": project_id, "budget_chars": budget_chars}
            if recent_window is not None:
                args["recent_window"] = recent_window
            result = self._mapped_call("overview", args)
            self._observe_tree(project_id, 'view_available', result)
            return result

    def tree_zoom(self, project_id, level, index):
        if level < 0 or index < 0:
            raise ValueError("tree level and index must be nonnegative")
        with self._lock:
            result = self._mapped_call("zoom", {"project_id": project_id, "level": level, "index": index})
            self._observe_tree(project_id, 'zoom_available', result)
            return result

    def _observe_tree(self, project_id, operation, result):
        previous = self._tree_observations.get(project_id)
        state = dict(previous[1]) if previous else {
            'present': None, 'view_available': None, 'zoom_available': None}
        if _missing_tree(result):
            state.update(present=False, view_available=False, zoom_available=False)
        elif isinstance(result, dict) and (result.get('error') or result.get('success') is False):
            # A missing node or other error does not establish a missing tree.
            state[operation] = False
        elif isinstance(result, str) and result.strip().lower().startswith('no node'):
            state[operation] = False
        else:
            content = result if isinstance(result, str) else None
            if isinstance(result, dict):
                for key in ('view', 'text', 'node', 'nodes', 'lines'):
                    if result.get(key):
                        content = result[key]
                        break
            if content:
                if state['present'] is False:
                    state.update(view_available=None, zoom_available=None)
                state.update(present=True)
                state[operation] = True
            else:
                state[operation] = None
        with self._tree_observation_lock:
            self._tree_observations[project_id] = (self._clock(), state)

    def tree_status_snapshot(self, project_id, *, endpoints=None):
        """Return only cached finite observations, without waiting for network I/O."""
        with self._tree_observation_lock:
            if endpoints is not None and list(endpoints) != list(self._clients):
                return None, None
            observation = self._tree_observations.get(project_id)
            if observation is None:
                return None, None
            stamp, state = observation
            return stamp, dict(state)

    def read_layers(self, project_id, **filters):
        if not self._in_operation:
            return self._run(lambda: self.read_layers(project_id, **filters), "read")
        args = dict(filters)
        args["project_id"] = project_id
        query = args.get("query")
        if query and not self._read_accepts("query"):
            # The server filters by ctx/type/time only; asking it for `query` silently returned the
            # newest layers. Scan a recent window and rank by term hits instead.
            limit = int(args.pop("last_n", 10) or 10)
            args.pop("query")
            args["last_n"] = self.recall_scan
            return rank_layers(self._mapped_call("read", args), query, limit)
        return self._mapped_call("read", args)

    def _read_accepts(self, name):
        tools = self.detect_tools()
        tool = self._tools.get(tools.get("read") or "", {}) if self._tools else {}
        properties = tool.get("inputSchema", {}).get("properties")
        return not isinstance(properties, dict) or name in properties

    def write_layer(self, project_id, request_id, layer):
        if project_id in self.read_only_projects:
            raise ValueError("Memory project is configured read-only")
        if not project_id or not request_id:
            raise ValueError("project_id and request_id are required for writes")
        for field in ("type", "body", "ctx", "author"):
            if not isinstance(layer.get(field), str) or not layer[field].strip():
                raise ValueError(f"layer {field} must be a nonempty string")
        if "conf" in layer:
            conf = layer["conf"]
            if isinstance(conf, bool) or not isinstance(conf, (int, float)) or not math.isfinite(conf) or not 0 <= conf <= 1:
                raise ValueError("layer conf must be between zero and one")
        result = self._mapped_call("write", {
            "project_id": project_id, "request_id": request_id, "layer": layer,
        })
        if not isinstance(result, dict) or result.get("ok") is False or result.get("success") is False:
            detail = result.get("error") if isinstance(result, dict) else ""
            message = "AetherMind write did not acknowledge success"
            if isinstance(detail, str) and detail.strip():
                message = f"{message}: {' '.join(detail.split())[:500]}"
            raise McpError(message)
        if "request_id" in result and result["request_id"] != request_id:
            raise McpError("AetherMind write acknowledged a different request")
        if not (result.get("ok") is True or result.get("success") is True or result.get("layer_id")):
            raise McpError("AetherMind write acknowledgement is ambiguous")
        return result

    def list_projects(self, project_id=None):
        args = {"project_id": project_id} if project_id is not None else {}
        return self._mapped_call("projects", args)


def _match_layers(result, query, limit):
    """Newest-first layers whose body or ctx contain every query word (else any word)."""
    if not isinstance(result, dict) or not isinstance(result.get("layers"), list):
        return result
    words = [word for word in re.findall(r"[\w-]+", str(query).lower()) if len(word) > 2]
    if not words:
        return result
    layers = [layer for layer in result["layers"] if isinstance(layer, dict)]
    layers.sort(key=lambda layer: str(layer.get("timestamp") or layer.get("ts") or ""), reverse=True)
    def text(layer):
        return (str(layer.get("body", "")) + " " + str(layer.get("ctx", ""))).lower()
    matched = [layer for layer in layers if all(word in text(layer) for word in words)]
    if not matched:
        scored = [(sum(word in text(layer) for word in words), index, layer) for index, layer in enumerate(layers)]
        matched = [layer for score, index, layer in sorted(scored, key=lambda item: (-item[0], item[1])) if score]
    if type(limit) is int and limit > 0:
        matched = matched[:limit]
    return {**result, "layers": matched, "count": len(matched)}
