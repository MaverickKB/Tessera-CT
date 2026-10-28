"""Background sessions with explicit tool allowlists and shared limits."""

from dataclasses import replace
from pathlib import Path
import re
import threading
import time

from .agent_selection import select_backend
from .backend import Backend
from .settings_ladder import no_credential
from .passive_status import configured_path
from .tools import AUTO_APPROVE
from .worker_branch import WorkerBranch, repository_root


# Tools that change files or run commands. A worker holding any of them is a writer.
WRITE_TOOLS = {'shell', 'process', 'write_file', 'edit_file', 'apply_patch',
               'move_path', 'copy_path', 'delete_path', 'mkdir'}


class AgentPool:
    def __init__(self):
        self.lock = threading.RLock()
        self.running = 0
        self.running_by_kind = {}
        self.primary = None


class AgentManager:
    def __init__(self, session, pool=None):
        self.session = session
        self.pool = pool or AgentPool()
        if session.depth == 0:
            self.pool.primary = session
        self._threads = {}
        self._children = {}
        self._cancellations = {}

    def spawn(self, name, task, tools=None, backend=None, capability='routine', repo=None):
        if not re.fullmatch(r'[A-Za-z0-9_-]{1,64}', name):
            raise ValueError('Agent name must be 1..64 letters, numbers, underscores, or hyphens')
        if not task.strip():
            raise ValueError('Agent task cannot be empty')
        parent = self.session
        if parent.depth >= parent.config.agents.max_depth:
            raise RuntimeError('Subagent depth limit reached')
        # A named list is validated against the parent's actual capability set before a
        # thread starts. The default is deliberately left as None: the child builds its
        # own registry, and that registry is what "this session's tools" means for it.
        # Copying the parent's schema names does not work, because some tools exist only
        # at depth 0 (MCP servers, companions) and a child can never hold them, so
        # restricted() would reject the default list and every spawn would fail.
        if tools is not None:
            parent.tools.restricted(tools)
        # The parent's effective set is the ceiling for everything below it. Passed even
        # when tools is None, because omitting a list must not restore unrestricted access.
        ceiling = parent.tool_ceiling if getattr(parent, 'tool_ceiling', None) else set(parent.tools.names())
        inherited = parent.view.prompt_render(parent.config.view.prompt_bytes)
        selected = select_backend(parent.config, backend, capability)
        roles = dict(parent.config.roles)
        if selected:
            roles['main'] = selected
        if parent.config.backends:
            settings = replace(parent.config, roles=roles).backend_for()
            kind = settings.provider_kind
            lane_name = settings.name
        else:
            # Injected backends (including offline fixtures) need no config entry.
            kind = getattr(parent.backend, 'provider_kind', 'openai_chat')
            lane_name = None
        lane, capacity, frontier = self._lane_for(parent, lane_name, kind)
        # The root session's own thread is one of the local model's serving
        # slots. It does NOT consume a slot out of a blunt worker cap: an
        # operator writing max_concurrent = 1 means one worker, not zero.
        root_counts = self._root_occupies_slot(parent, lane, frontier)
        branch = self._writer_branch(name, tools, ceiling, repo)
        try:
            self._reserve_and_start(name, task, tools, inherited, ceiling, selected, lane,
                                    capacity, branch, root_counts)
        except Exception:
            if branch is not None:
                branch.discard()
            raise
        model = f' on {selected}' if selected else ''
        if branch is None:
            return f'Started [{name}]{model}. Its report will be appended to this chat.'
        return (f'Started [{name}]{model} in worktree {branch.path} on branch {branch.branch}. '
                'Its report, with the branch to merge, will be appended to this chat.')

    @staticmethod
    def _lane_for(parent, backend_name, kind):
        """The admission lane one backend occupies: (lane_key, lane_capacity).

        A lane is NOT a provider kind. Every configured backend here reports
        provider_kind 'openai_chat', so keying admission on kind alone puts a
        model that names a credential and a model with none in one bucket. A
        model with a credential stays unbounded.

        Locality is resolved for THIS backend:
        - a locally served backend gets its own lane, keyed by SERVING ENDPOINT
          (aliases of one process share that process's cap; keying by alias
          would give each alias its own copy of the same cap);
        - the lane width is that model's max_concurrent_requests when the
          number is set. The chat holds one slot. When the number is omitted,
          the lane does not refuse a subagent. Safe mode still allows one
          request at a time on the model itself;
        - a non-local backend keeps the plain provider-kind lane, so an
          advertised cap such as {'cli_worker': 6} remains one shared bucket as
          written;
        - an unbounded lane is 0, and 0 never refuses.
        """
        agents = parent.config.agents
        settings = (parent.config.backends or {}).get(backend_name)
        kind = kind or 'openai_chat'
        # Precedence, unchanged from _capacity_for: an advertised per-kind lane
        # is used exactly as written; otherwise the blunt scalar is an operator
        # ceiling over every lane; only then does locality decide.
        advertised = agents.concurrency_by_kind.get(kind)
        if advertised is not None:
            return kind, advertised, True
        if agents.max_concurrent > 0:
            return kind, agents.max_concurrent, False
        if settings is None or kind == 'cli_worker':
            # No config entry (injected/offline backend), or a CLI worker that
            # is a local subprocess: unbounded.
            return kind, 0, False
        try:
            local = no_credential(parent.config, backend_name)
        except Exception:
            # Classification failed. Fall back to the kind lane at its written
            # width rather than silently opening it.
            local = False
        if local:
            # A local lane is the SERVING ENDPOINT, not the backend alias.
            # Aliases of one process share one cap. The width is that model's
            # max_concurrent_requests, not a hidden slot count.
            endpoint = (getattr(settings, 'base_url', '') or '').strip().rstrip('/')
            lane = f'{kind}|local:{endpoint or backend_name}'
            # The Safe default of one in-flight request is the model slot, not
            # this lane. Using it here, with the chat holding that one slot,
            # refused every local subagent when max_concurrent_requests was unset.
            explicit = getattr(settings, 'max_concurrent_requests', None)
            if type(explicit) is not int or isinstance(explicit, bool) or explicit < 1:
                return lane, 0, False
            return lane, explicit, True
        # Cloud/API backend: unbounded. Never the local serving width.
        return kind, 0, False

    @staticmethod
    def _root_occupies_slot(parent, lane, frontier):
        """Whether the calling session's own thread uses one of this lane's slots.

        A finite local lane counts the root thread as one of its requests.
        That holds for an advertised lane and for a local lane whose width is
        a hand-set max_concurrent_requests. An omitted number does not count
        the root, and it does not refuse a subagent.

        It does not hold for the blunt max_concurrent scalar: a cap written as 1
        there means one worker, and letting the root eat it refuses every worker,
        which is the defect d900fd6 fixed. frontier says which of those this
        lane is, decided where the lane was resolved rather than re-guessed here.
        """
        return bool(frontier)

    def _reserve_and_start(self, name, task, tools, inherited, ceiling, selected, lane, capacity, branch, root_counts=True):
        parent = self.session
        with self.pool.lock:
            if parent._closed:
                raise RuntimeError('Session is closed')
            if name in self._threads:
                raise ValueError(f'Agent name already used: {name}')
            # A positive advertised kind limit denotes a finite serving lane.
            # The root occupies one slot, even when a descendant does the spawning.
            primary = self.pool.primary
            primary_lane = None
            if primary is not None and not primary._closed:
                if primary.config.backends:
                    try:
                        primary_name = primary.config.roles.get('main')
                        if primary_name is None:
                            primary_name = next(iter(primary.config.backends))
                        primary_kind = primary.config.backend_for().provider_kind
                        # Only a root on a LOCALLY served model consumes serving
                        # slots. A cloud root holds no local slot, which is what
                        # test_cloud_primary_does_not_occupy_local_slots asserts.
                        if no_credential(primary.config, primary_name):
                            primary_lane = self._lane_for(primary, primary_name, primary_kind)[0]
                    except Exception:
                        primary_lane = None
                else:
                    # Injected root backend: its kind is the only lane identity.
                    primary_lane = getattr(primary.backend, 'provider_kind', 'openai_chat')
            occupancy = int(capacity > 0 and primary_lane == lane and root_counts)
            if capacity > 0 and self.pool.running_by_kind.get(lane, 0) + occupancy >= capacity:
                # Name the setting that produced this width. A local lane is
                # max_concurrent_requests. An advertised kind map is
                # concurrency_by_kind. The blunt scalar is agents.max_concurrent.
                if '|local:' in str(lane):
                    detail = (
                        f'max_concurrent_requests on this model is {capacity}, '
                        'and the chat holds one slot'
                    )
                elif root_counts:
                    detail = f'concurrency_by_kind on this lane is {capacity}'
                else:
                    detail = f'agents.max_concurrent is {capacity}'
                raise RuntimeError(f'Subagent concurrency limit reached: {detail}')
            self.pool.running += 1
            self.pool.running_by_kind[lane] = self.pool.running_by_kind.get(lane, 0) + 1
            cancelled = threading.Event()
            self._cancellations[name] = cancelled
            thread = threading.Thread(target=self._run, args=(name, task, None if tools is None else list(tools), inherited, ceiling, selected, cancelled, lane, branch), name=f'tessera-{name}', daemon=True)
            self._threads[name] = thread
            try:
                thread.start()
            except Exception:
                self.pool.running -= 1
                self.pool.running_by_kind[lane] -= 1
                del self._threads[name]
                raise
        return thread

    def _writer_branch(self, name, tools, ceiling, repo):
        """A private worktree for a worker that can write into a git checkout, or None.

        repo names the checkout explicitly. Without it, the parent's workspace is used
        when that is a git checkout. A read-only worker never gets a worktree.
        """
        parent = self.session
        if tools is None:
            effective_tools = set(ceiling)
        else:
            effective_tools = set(ceiling).intersection(tools)
        is_writer = bool(effective_tools.intersection(WRITE_TOOLS))
        git_timeout = parent.config.agents.worktree_timeout
        if repo:
            repo_root = repository_root(repo, git_timeout)
            if repo_root is None:
                raise ValueError(f'repo is not inside a git checkout: {repo}')
        else:
            workspace = Path(parent.workspace.path).expanduser()
            repo_root = repository_root(workspace, git_timeout)
            # repository_root() walks up to the enclosing checkout, so a session
            # working in a subdirectory of an unrelated or far-too-large repo
            # silently roots the worker there. A session at ~/.tessera-live spawned
            # workers that landed in the operator's home directory because it was the
            # enclosing git repo, and every report
            # named a base sha that does not exist in the intended project.
            # The documented contract is that the workspace ITSELF is a checkout;
            # anything else has to be named with repo=.
            #
            # Only a WRITER is refused. A read-only worker never creates a branch,
            # so the enclosing checkout is not a guess it can get wrong: refusing
            # it would break every read-only spawn from an unrooted workspace
            # (measured: ~/.tessera-live refused BOTH, before this guard narrowed).
            if (is_writer and repo_root is not None
                    and repo_root != workspace.resolve()):
                raise ValueError(
                    f'workspace {workspace} is inside git checkout {repo_root} but is not its root; '
                    f'pass repo= explicitly to name the checkout the worker should branch from')
        if not is_writer or repo_root is None:
            return None
        worktrees_root = configured_path(parent.config.home, parent.config.agents.worktrees_dir) / 'spawn'
        return WorkerBranch.create(repo_root, worktrees_root, name, git_timeout)

    def _run(self, name, task, tools, inherited, ceiling, selected, cancelled, lane, branch=None):
        from .loop import Session
        parent = self.session
        meta = {'agent': name, 'backend': selected, 'success': False, 'session_id': parent.session_id}
        report = None
        try:
            roles = dict(parent.config.roles)
            if selected:
                roles['main'] = selected
                roles['fast'] = selected
            if branch is not None:
                workspace = branch.path
                task = branch.task_preface() + task
            else:
                workspace = parent.workspace.path
            config = replace(parent.config, agent_name=name, roles=roles,
                             cwd=workspace, workspace=str(workspace))
            if parent.agent_backend_factory:
                backend = parent.agent_backend_factory()
            elif isinstance(parent.backend, Backend) or parent.config.agents.backend_policy:
                settings = config.backend_for()
                extra = dict(settings.extra)
                if settings.provider_kind == 'cli_worker':
                    extra['cli'] = self._cli_options(extra.get('cli', {}), workspace, tools, ceiling, branch)
                backend = Backend(settings.name, settings.base_url, settings.model,
                                  settings.api_key_env, extra)
                backend.bind_endpoint(parent._config_owner, settings.name)
            else:
                backend = parent.backend

            def inherit_approval(registry):
                registry.defer = parent.tools.defer

            with Session(config, backend=backend, root=parent.root / 'agents' / name,
                         inherited_view=inherited, depth=parent.depth + 1,
                         allowed_tools=tools, pool=self.pool,
                         tool_ceiling=ceiling,
                         agent_backend_factory=parent.agent_backend_factory,
                         approve=parent.approve, parent_view=parent.view,
                         configure_tools=inherit_approval,
                         config_owner=parent._config_owner) as child:
                child._parent_cancelled = cancelled
                with self.pool.lock:
                    self._children[name] = child
                reply = child.run_turn(task)
                child.agents.wait()
            report = f'[{name}] {reply}'
            meta['success'] = child.last_turn_success
        except Exception as exc:
            labels = []
            category = getattr(exc, 'category', '')
            if isinstance(category, str) and category:
                labels.append(category)
            info = {}
            diagnostics = getattr(exc, 'diagnostics', None)
            if callable(diagnostics):
                result = diagnostics()
                if isinstance(result, dict):
                    info = result
                    meta.update(info)
                    labels = []
                    for key in ('error_category', 'error_kind'):
                        value = info.get(key)
                        if isinstance(value, str) and value and value not in labels:
                            labels.append(value)
                    if 'http_status' in info:
                        labels.append(f"http_status={info['http_status']}")
            from .bundled_engine import AetherMindUnavailable
            if isinstance(exc, AetherMindUnavailable):
                report = f'[{name}] {exc}'
                meta['success'] = False
            else:
                detail = ''.join(', ' + label for label in labels)
                error_detail = info.get('error_detail')
                if (info.get('error_category') == info.get('error_kind') == 'provider'
                        and not error_detail):
                    detail += ' — no diagnostic detail'
                report = f'[{name}] Agent failed ({type(exc).__name__}{detail}).'
                if isinstance(error_detail, str) and error_detail.strip():
                    report += '\n' + error_detail[:200]
        finally:
            try:
                if branch is not None:
                    report = (report or f'[{name}] Agent ended without a report.') + self._close_branch(name, branch, meta)
                if report is not None:
                    parent.log.append('work', report, meta)
            finally:
                with self.pool.lock:
                    self._children.pop(name, None)
                    self.pool.running -= 1
                    self.pool.running_by_kind[lane] -= 1

    def _cli_options(self, options, workspace, tools, ceiling, branch):
        """CLI worker options: cwd, engine tools, and the native sandbox for this worker.

        A writer in its own worktree always gets workspace-write: its native writes are
        confined to that private checkout and integrated by the parent. Outside a
        worktree, workspace-write needs an auto-approve policy, because native CLI
        writes cannot go through the engine's approval callback.
        """
        parent = self.session
        if tools is None:
            effective_tools = set(ceiling)
        else:
            effective_tools = set(ceiling).intersection(tools)
        is_writer = bool(effective_tools.intersection(WRITE_TOOLS))
        sandbox = 'read-only'
        if is_writer and branch is not None:
            sandbox = 'workspace-write'
        elif is_writer and parent.tools.approval in AUTO_APPROVE:
            sandbox = 'workspace-write'
        cli = dict(options)
        cli['cwd'] = str(workspace)
        cli['engine_tools'] = True
        cli['sandbox'] = sandbox
        if sandbox == 'workspace-write' and branch is not None:
            cli['add_dirs'] = branch.writable_git_paths()
        return cli

    def _close_branch(self, name, branch, meta):
        """Commit leftovers, then describe the branch for the parent's merge."""
        lines = []
        try:
            note = branch.commit_leftovers(name)
            summary = branch.summary()
        except ValueError as exc:
            meta['worktree'] = str(branch.path)
            return f'\nWorktree {branch.path} (branch {branch.branch}) could not be summarized: {exc}'
        meta['worktree'] = summary['worktree']
        meta['branch'] = summary['branch']
        meta['head'] = summary['head']
        lines.append('')
        lines.append(f"Branch: {summary['branch']} in worktree {summary['worktree']}")
        lines.append(f"Base {summary['base'][:12]}, head {summary['head'][:12]}, "
                     f"{summary['commits']} commit(s), {len(summary['files'])} file(s) changed.")
        if note:
            lines.append('Note: ' + note + '.')
        if summary['commits'] == 0:
            lines.append('Nothing to merge.')
        else:
            lines.append('Merge it into the target checkout after review; the harness does not merge.')
        return '\n'.join(lines)

    def cancel(self, reason='user'):
        with self.pool.lock:
            for name, thread in self._threads.items():
                if thread.is_alive():
                    self._cancellations[name].set()
            children = list(self._children.values())
        for child in children:
            child.interrupt(reason)

    def wait(self, timeout=None):
        deadline = None if timeout is None else time.monotonic() + timeout
        with self.pool.lock:
            threads = list(self._threads.values())
        for thread in threads:
            remaining = None if deadline is None else max(0, deadline - time.monotonic())
            thread.join(remaining)
        for thread in threads:
            if thread.is_alive():
                return False
        return True

    def wait_for_reports(self, timeout=None):
        finished = self.wait(timeout)
        reports = []
        for message in self.session.log.iter():
            if message.kind == 'work':
                reports.append(message.text)
        if not finished:
            text = 'Workers are still running.'
        elif not reports:
            text = 'No worker reports.'
        else:
            text = 'Worker reports:'
        for report in reports:
            text = text + '\n' + report
        return text

    def active_details(self):
        with self.pool.lock:
            rows = []
            for name, thread in self._threads.items():
                if not thread.is_alive():
                    continue
                child = self._children.get(name)
                backend = child.config.roles.get('main') if child else None
                rows.append({'name': name, 'backend': backend})
            return rows

    def active(self):
        with self.pool.lock:
            count = 0
            for thread in self._threads.values():
                if thread.is_alive():
                    count += 1
            return count
