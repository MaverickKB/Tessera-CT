"""TOML settings with named provider accounts and secret-source references."""

from dataclasses import asdict, dataclass, field, fields, replace
import logging
import os
import sys
import math
import re
from urllib.parse import urlsplit
from pathlib import Path
import tomllib

from .credentials import CredentialResolver
from .secret_names import flagged_env_value
from .providers import ADAPTERS
from .cli_worker import cli_settings
from .systems import load_systems

PROVIDER_KINDS = set(ADAPTERS) | {'cli_worker'}
_log = logging.getLogger(__name__)


@dataclass
class BackendConfig:
    name: str = 'default'
    base_url: str = ''
    model: str = ''
    api_key_env: str = ''
    extra: dict = field(default_factory=dict)
    provider_kind: str = ''
    credential_ref: str = ''
    account: str = ''
    # False marks a utility/structured-output backend that is never offered as a chat brain.
    chat: bool = True
    # False keeps the provider in config but out of chat and out of new turns.
    enabled: bool = True
    # None is unset. A positive integer set by hand is this model's cap in Safe
    # mode and in Unleashed mode. Safe mode then allows one request at a time on a
    # model with no credential. Unleashed mode drops that default and does
    # not add a number. A model that names a credential or a vendor CLI stays
    # unlimited until you set one.
    # This is not a swarm-wide limit and it is not the agent spawn lanes.
    max_concurrent_requests: int | None = None
    # Optional pace for this endpoint. None is unset, which is unlimited in Safe
    # mode and in Unleashed mode. There is no system default. A positive integer set
    # by hand is enforced in both modes. This paces one endpoint, not the swarm.
    throttle_requests_per_minute: int | None = None
    throttle_tokens_per_minute: int | None = None


NOT_CHAT_MODELS = {'jev', 'sys1'}


def offered_for_chat(name, backend):
    """Chat is the default. Jev and sys1 are judges, not chat models."""
    if not backend.enabled or backend.chat is False:
        return False
    model = (backend.model or '').strip().lower()
    if str(name).strip().lower() in NOT_CHAT_MODELS or model in NOT_CHAT_MODELS:
        return False
    return True


@dataclass
class AccountConfig:
    name: str = ''
    provider_kind: str = 'openai_chat'
    credential_ref: str = ''
    limits: dict = field(default_factory=dict)
    budget: dict = field(default_factory=dict)


@dataclass
class AetherMindConfig:
    url: str = ''
    # Pre-turn recall: when the message looks memory-dependent, query AetherMind before the model runs.
    # It blocks the turn for the recall's duration; false leaves recall to the agent's own memory tools
    # (the per-turn reminder still tells it to use them).
    auto_recall: bool = True
    endpoints: list = field(default_factory=list)
    token_env: str = 'AETHERMIND_TOKEN'
    project: str = ''
    personal_project: str = ''
    author: str = 'tessera'
    # Operator choice. 'embedded' is the packaged engine. 'external' uses the URL
    # or endpoint list only after that service validates. Empty follows a saved
    # URL or endpoint list, and otherwise the packaged engine.
    source: str = ''
    timeout: float = 15.0
    probe_timeout: float = 2.0
    probe_interval: float = 30.0
    create_project: bool = False
    # Where deliberate notes (decision, correction, lock, lesson) go; empty = project. May be the
    # canonical personal store; then personal_read_only must be false.
    note_project: str = ''
    personal_read_only: bool = True
    # Bundled engine copy: a packaged aethermind-engine directory with its
    # build-receipt.json. This is the memory service unless an external URL or
    # endpoint list validates.
    bundled_path: str = ''
    # stdout and stderr of one embedded-engine launch. Truncated when that
    # launch starts, then kept to this many bytes.
    engine_log_max_bytes: int = 1048576
    # memory_recall text search: layers scanned per project, and matches returned.
    recall_scan: int = 400
    recall_limit: int = 10
    # Tool outcomes kept out of turn layers: memory reads would otherwise nest old layers in new ones.
    capture_omit_outcomes: list = field(default_factory=lambda: ['memory_recall', 'memory_tree', 'session_briefs'])
    # A session re-reads its project inheritance and personal cover after this many seconds,
    # so a long-running session sees layers other sessions wrote. 0 reads it once per runtime.
    inheritance_refresh_seconds: float = 900.0


@dataclass
class JevConfig:
    enabled: bool = False
    url: str = ''
    model: str = ''
    api_key_env: str = ''
    credential_ref: str = ''
    timeout: float = 8.0
    risk_threshold: float = 0.7
    gate_threshold: float = 0.7
    goal_threshold: float = 0.8
    loop_window: int = 4
    batch_items: int = 64
    human_gate: bool = False
    session_gates_enabled: bool = False
    cache_size: int = 256
    gated_tools: list = field(default_factory=lambda: ['shell', 'write_file', 'edit_file'])
    action_gate_enabled: bool = False
    router_probes: dict = field(default_factory=dict)


@dataclass
class CompactorConfig:
    workers: int = 4
    retries: int = 2
    context_bytes: int = 16000
    judge_enabled: bool = False
    judge_threshold: float = 0.8


@dataclass
class ViewConfig:
    node_bytes: int = 512
    high_bytes: int = 128000
    low_bytes: int = 64000
    # The saved view stays at the sawtooth. A model call sends only this many
    # bytes of the newest lines. Older coverage stays on disk and opens by zoom.
    # 8000 is about fifteen full lines. A larger budget is a single-session choice.
    prompt_bytes: int = 8000


@dataclass
class ToolsConfig:
    approval: str = 'ask'
    # Operator switch between the configured safe policy and yolo (auto-approve all tools).
    yolo_switch: bool = True
    # Runtime switch state, relative to home; it overrides approval until switched back.
    approval_state_file: str = 'state/approval.json'
    # Tool classes (fnmatch patterns over tool names) and per-class or per-tool rules: allow | gate.
    # allow skips approval under ask and deny_risky; gate keeps the policy. A rule naming a tool
    # beats its class rule. Unlisted risky tools follow the policy. Unleashed skips approval for a tool that is on.
    approval_classes: dict = field(default_factory=lambda: {
        'memory-self': ['clock_heartbeat', 'clock_journal_write', '*write_layer', 'skill_write'],
        'acting': ['mac_act', 'desk_act', 'shell', 'write_file', 'edit_file', 'apply_patch', 'move_path',
                   'copy_path', 'delete_path', 'mkdir', 'process', 'remote_shell', 'remote_write_file',
                   'background_task', 'computer_mouse', 'computer_scroll', 'computer_type', 'computer_key',
                   'computer_focus', 'computer_click', 'computer_perform_secondary_action', 'computer_set_value',
                   'computer_select_text', 'computer_drag', 'computer_press_key', 'computer_type_text']})
    # Older configs may still list tools here. Every route offers the full registry; this list does not hide tools.
    direct_route_tools: list = field(default_factory=list)
    # Move a direct-route turn to the tools route when the model asks for a tool it was not offered.
    promote_direct_route: bool = True
    approval_rules: dict = field(default_factory=lambda: {'memory-self': 'allow', 'acting': 'gate'})
    clip_chars: int = 30000
    timeout: float = 30.0
    # Page sizes for workspace listing and search. The model may pass a smaller limit.
    list_limit: int = 200
    grep_limit: int = 40
    grep_scan: int = 2000
    # How many commits git_read log returns unless the call asks for fewer.
    git_log_limit: int = 20
    # Directory for shell and process output that does not fit in the tool result. Relative to the workspace.
    spill_dir: str = 'tool-output'
    output_tail_chars: int = 4000
    read_line_limit: int = 400
    glob_limit: int = 200
    read_bytes_limit: int = 1000000
    image_bytes_limit: int = 5000000
    fetch_max_bytes: int = 1000000
    fetch_timeout: float = 20.0
    shell_max_timeout: float = 600.0
    # Names the operator turned off. Empty offers every tool. The list applies
    # in every approval mode, including Unleashed: the next turn omits them and a
    # call is refused. Unleashed skips approval for tools that stay on.
    disabled: list = field(default_factory=list)


@dataclass
class AgentsConfig:
    # Compatibility fallback per provider kind; zero means unlimited.
    # A positive value is an operator ceiling over every spawn lane, in both modes.
    # Zero leaves a local lane to that model's max_concurrent_requests.
    max_concurrent: int = 0
    # Unlisted kinds are unlimited until locality or a written per-kind cap decides.
    concurrency_by_kind: dict = field(default_factory=dict)
    max_depth: int = 2
    enabled: bool = False
    token_budget: int = 0
    judge_tokens: int = 0
    wall_seconds: float = 0.0
    view_bytes: int = 16000
    runs_dir: str = 'agent-runs'
    planner_tokens: int = 0
    worker_backend: str = ''
    backend_policy: dict = field(default_factory=dict)
    allowed_scopes: list = field(default_factory=list)
    worktrees_enabled: bool = False
    commit_children: bool = False
    worktree_result_format: str = 'native'
    worktree_context_files: list = field(default_factory=list)
    worktree_context_bytes: int = 128000
    repo_root: str = ''
    worktrees_dir: str = 'agent-worktrees'
    git_command: str = 'git'
    worktree_timeout: float = 30.0
    validation_timeout: float = 120.0
    validation_commands: list = field(default_factory=list)


@dataclass
class ServerConfig:
    host: str = '127.0.0.1'
    port: int = 0
    max_hops: int = 4
    resume_turns: bool = True
    # Opt-in names a local HTTPS proxy (for example tailscale serve) presents as
    # Host/Origin. The core still binds loopback only. Empty means loopback names only.
    allowed_hosts: list = field(default_factory=list)
    # Shared key for requests that arrive through a proxy. Empty means
    # no auth at all, which is the desktop posture. See auth_mode for
    # whether the key is also required on direct loopback requests.
    auth_token: str = ''
    # 'loopback' (default): the key is required only when a request arrives through
    # an allowed_hosts proxy -- the one path a remote client can reach. Direct
    # loopback requests stay unauthenticated, so a local web page, the desktop
    # window and a curl script cannot be stranded by a wrong or missing key.
    # 'always': the key is required on every /api/ request, for a genuinely shared
    # machine. Choosing this means every local client must present the key.
    auth_mode: str = 'loopback'
    # True when a supervisor restarts the core after a clean exit (launchd KeepAlive);
    # enables the Restart core control.
    supervised: bool = False
    # Restart drain: running turns get this long to finish, then are interrupted and given the grace period.
    drain_seconds: float = 180.0
    interrupt_grace_seconds: float = 20.0
    # An idle restart waits this long for a running turn to go silent before
    # giving up and draining. It bounds SILENCE, not turn duration: measured on
    # this home, 110 of 663 turns ran longer than 30 minutes and 26 longer than
    # an hour, so a duration bound interrupts the work the wait exists to
    # protect. Only 8 of those turns were ever silent this long, and each was a
    # single model call that returned nothing for hours -- genuinely wedged.
    restart_stall_seconds: float = 1800.0
    # Characters of the replied-to message quoted into a reply turn.
    reply_quote_chars: int = 600
    # gzip/deflate JSON and static responses at least this large when the client accepts it; 0 turns it off.
    compress_min_bytes: int = 1024
    # Events replayed to a page that opens with no checkpoint. The transcript comes
    # from the messages endpoint, so this only has to cover the newest work; 0 replays all.
    event_replay_limit: int = 200


@dataclass
class DaemonConfig:
    max_frame_bytes: int = 8 * 1024 * 1024
    max_pending_requests: int = 32
    approval_timeout: float = 30.0
    event_poll_seconds: float = 0.2
    history_page_size: int = 200
    zoom_page_bytes: int = 16000
    mirror_retry_seconds: float = 30.0
    mirror_dir: str = 'mirror'


@dataclass
class SwarmConfig:
    enabled: bool = False
    tasks_file: str = 'tasks.jsonl'
    jobs_dir: str = 'jobs'
    # Workers one job record may list. None is unset: Safe mode allows 8, Unleashed does not
    # add a cap. A saved integer of at least 2 holds in both modes. Not a concurrency cap.
    max_job_tasks: int | None = None
    max_artifact_bytes: int = 65536
    # Opt-in admission hold for models listed in infrastructure.pressure_engines.
    # Off unless the operator turns it on. On applies in Safe mode and in Unleashed.
    pressure_hold_enabled: bool = False
    # Hold at or above this fraction of advertised slots, counting running, waiting and warming.
    pressure_soft_threshold: float = 0.75
    max_attempts: int = 3
    # None is unset. Safe mode then requires each worker's own backend and account,
    # so the job's evidence is independent. That is a guard, not a judge-versus-worker
    # rule. Unleashed mode drops it. A saved true or false holds in both modes.
    distinct_workers: bool | None = None
    lease_seconds: float = 600.0
    retry_base: float = 1.0
    retry_cap: float = 60.0
    circuit_failures: int = 3
    circuit_cooldown: float = 30.0
    gates_enabled: bool = False


@dataclass
class ChannelsConfig:
    enabled: bool = False
    journal_file: str = 'channels.jsonl'
    auto_deliver: bool = True
    auto_reply: bool = False
    max_hops: int = 4
    rate_limit: int = 20
    rate_window: float = 60.0
    rate_overrides: dict = field(default_factory=dict)
    max_text_bytes: int = 65536
    delivery_limit: int = 100
    needs_reply_threshold: float = 0.7
    block_cycles: bool = True


@dataclass
class BoardConfig:
    enabled: bool = False
    journal_file: str = 'board.jsonl'
    max_text_bytes: int = 8192
    max_artifacts: int = 32
    max_questions: int = 32
    judge_asks: bool = False
    # Off by default so an existing install does not suddenly
    # require a sign-off it has no one to perform. When on, a card may not move to
    # succeeded without an acceptance recorded by an identity the policy names.
    acceptance_required: bool = False
    # Identities allowed to accept. Empty with the gate on means any known identity may
    # accept -- that is the one-person install, and it is still recorded, not assumed.
    acceptance_by: list = field(default_factory=list)
    ask_human_threshold: float = 0.7


@dataclass
class IntakeConfig:
    """Bounds for gathering pull requests onto the board.

    The collector reads one named repository. These bounds are configuration rather than
    literals in tessera/intake.py, because how many pull requests to gather and how long to
    wait for the provider are install decisions, not facts about the code.
    """
    # How many open pull requests one gather reads when the caller does not say.
    limit: int = 20
    # Seconds to wait for the provider before naming a timeout as the failure.
    timeout_seconds: float = 60.0


@dataclass
class IdentityConfig:
    """Person records under the board's owner strings.

    Off by default so an existing install keeps behaving exactly as it does today:
    a card owner stays a free string, nothing is validated against a registry, and
    no identity journal is written. Turning it on makes ownership resolvable and
    makes the operator a real record instead of a config value.
    """
    enabled: bool = False
    journal_file: str = 'identities.jsonl'
    # Record the configured operator_name as a person on start, so the board's
    # folded owner resolves to an identity rather than an unknown string.
    record_operator: bool = True
    # Record the configured agent_name as a person on start, so the companion's own
    # projects resolve. With only the operator recorded, every project managed by the
    # companion shows as owned by nobody the moment records are enabled. The companion
    # is one identity across many sessions, so it is recorded as a person rather than a
    # session: a project manager must not stop resolving when the session that created
    # it is gone.
    record_companion: bool = True


@dataclass
class RemoteConfig:
    """A machine the agent may operate over SSH (remote_shell / remote_read_file / remote_write_file)."""
    name: str = ''
    enabled: bool = True
    description: str = ''
    host: str = ''
    port: int = 22
    user: str = ''
    # Private key path (absolute, ~, or relative to home). Empty uses the ssh client's defaults.
    identity_file: str = ''
    # Pinned host keys (absolute, ~, or relative to home). Empty uses the ssh client's defaults.
    known_hosts_file: str = ''
    strict_host_key_checking: str = 'yes'
    ssh_command: list = field(default_factory=lambda: ['ssh'])
    connect_timeout: int = 10
    timeout: float = 60.0
    # Optional GUI access shown on Integrations and checked by TCP connect.
    rdp_host: str = ''
    rdp_port: int = 0
    rdp_doc: str = ''
    # The Integrations RDP switch. Independent of `enabled` (SSH tools) and `desk`.
    rdp_enabled: bool = True
    # Desk tools (desk_look, desk_act, desk_status) for a remote graphical session.
    desk: bool = False
    desk_screenshot_command: str = ('f=$(mktemp --suffix=.png) && XDG_RUNTIME_DIR=/run/user/$(id -u) '
                                    'DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/$(id -u)/bus gnome-screenshot -f "$f" '
                                    '&& cat "$f"; rm -f "$f"')
    desk_input_command: str = 'sudo -n python3 -'


@dataclass
class SkillsConfig:
    """Agent Skills shelf. The catalog never enters the system prompt or tool schemas."""
    # Off leaves the files on disk and stops search and load in every approval
    # mode, including Unleashed. A session that already started with this off has
    # no skill tools until it is started again.
    enabled: bool = True
    # User shelf directory; empty means <home>/skills.
    shelf: str = ''
    # Project directories (absolute, or under ~) whose .tessera/skills and .agents/skills may override by name.
    trusted_projects: list = field(default_factory=list)
    project_dirs: list = field(default_factory=lambda: ['.tessera/skills', '.agents/skills'])
    note_bytes: int = 4096
    note_description_chars: int = 160
    search_limit: int = 20
    # Skill names the operator turned off. The files stay on disk. Search and
    # load skip them in every approval mode, including Unleashed.
    disabled: list = field(default_factory=list)
    # Extra skill directories, absolute paths. The user shelf and trusted project dirs stay.
    extra_dirs: list = field(default_factory=list)


@dataclass
class PromptConfig:
    """System prompt sections and the per-turn nudge. Empty strings turn a part off.
    {project} and {personal_project} are the configured AetherMind projects."""
    memory_guidance: str = (
        'AetherMind: your continuity store of experiences, decisions and corrections. It is a manual '
        'lookup tool, not injected context; nothing from it reaches you unless you call it. The memory_* '
        'tools reach it.\n'
        '- Stores: {project} (this harness){personal}. Pass project_id for others.\n'
        '- A missing summary tree is normal. When memory_tree is available, it falls back to recall '
        'and says so in one line. Do not request a tree build.\n'
        '- Before answering about past work, decisions, people, projects, the operator, or any name you do not '
        'recognize, call memory_recall with distinctive terms. Answer from the layers and say when '
        'nothing matched.\n'
        '- When something durable happens (a decision, correction or lock), write memory_note with '
        'that type. No log-style busy-work. A repeated failure is not a memory note. '
        'record_lesson holds the trigger, the failure and the check when that tool is available.')
    skill_guidance: str = ('Procedures live on the skill shelf; search before house-specific or repeated work; '
                           'load a skill before acting on a hit; a skill never restricts delegation.')
    turn_reminder: str = ('Memory check: if this touches past context, call memory_recall before answering; '
                          'if something durable happened, write a memory_note.')
    # tool name -> one line; a line is included only when that tool is registered for the session.
    delegation_guidance: dict = field(default_factory=lambda: {
        'spawn_agent': "spawn_agent starts a background agent with this session's tools unless tools names a shorter list. Its report returns to this chat.",
        'wait_agents': 'wait_agents blocks until background agents finish and returns their reports.',
        'background_task': 'background_task runs a long command and wakes you when it ends or stalls.',
        'send_to_session': 'send_to_session and handoff pass work to other sessions.',
        'board_create_card': 'board_* tools track work on the WIP board; wip_board reads it.',
        # Starting is a transition, not a decoration. Without this line nothing told a
        # session to make it, and the journal held two `running` records in its whole
        # history while cards sat queued through hours of real work: the board said
        # "queued" about cards that were actively being worked, so its status lied.
        'board_start_card': ('board_start_card is the transition from claimed to running; '
                             'call it when the work actually begins, with a short receipt, '
                             'so the board shows in-flight work instead of a card that still reads queued.'),
        # The exit half of the same transition. Nothing settles a card when a turn ends,
        # so a session that starts a card and then stops without moving it leaves the
        # card reading `running` with nobody on it: the same lie as a card being worked
        # that still reads queued, from the other side.
        'board_update_status': ('board_update_status is how a card leaves running: move it to '
                                'succeeded, blocked or queued before your turn ends. A card left '
                                'running after you stop reads as in-flight work nobody holds.'),
        'board_release_card': ('board_release_card releases a claim and returns a running card to '
                               'the open pool; use it when you are handing off rather than finishing.'),
        'board_accept_card': ('board_accept_card records that an identity accepts a card as work, '
                              'with the evidence it was accepted against. When the board requires '
                              'acceptance, a card cannot move to succeeded until this is recorded. '
                              'Acceptance is history: reopening the card keeps it.'),
        'board_gather_intake': ('board_gather_intake gathers open pull requests from ONE named '
                                'repository (owner/name) into intake cards. An intake card is not '
                                'work: nobody can claim it and it wakes no worker until it is triaged '
                                'and accepted.'),
        'board_triage_intake': ('board_triage_intake records what an intake reviewer found -- subject '
                                '(bug, fix, feature-addition, feature-expansion, other), compatibility '
                                'with this codebase, and a summary. The acceptance decision reads these.'),
        'board_accept_intake': ('board_accept_intake accepts a triaged intake card as work: it becomes '
                                'claimable, with who accepted and why kept as history.'),
        'board_ignore_intake': ('board_ignore_intake declines an intake card as work and keeps it on '
                                'the board as evidence with the reason. Nothing is deleted.'),
        'set_timer': 'set_timer is this session\'s own wake, separate from the system floor. It wakes you later so you can keep watching and working.',
        'record_lesson': 'record_lesson saves a trigger, a failure and a check. The harness wakes you with it later.',
        'lesson_bind': 'lesson_bind attaches a shelf skill to an open lesson.',
        'lesson_check': 'lesson_check records whether a lesson passed, or retires a lesson that should stop waking you.',
        'park_question': 'park_question leaves a standing question the harness will wake you with.'})
    delegation_intro: str = ('Delegation: use your own tools first; delegate work that genuinely benefits from a '
                             'separate worker. Give children compact goals and point them at files; verify '
                             'their not-found claims.')
    # A generated map of models, services and machines from this config (no hosts in code).
    body_map: bool = True
    # Free text about the wider architecture, appended after the map. Operator-written.
    architecture: str = ''


@dataclass
class AutonomyConfig:
    """Event-driven wakes (docs/AUTONOMY.md). Caps gate delivery; held events are never dropped."""
    enabled: bool = False
    state_file: str = 'autonomy/state.json'
    tasks_dir: str = 'autonomy/tasks'
    tick_seconds: float = 1.0
    min_wake_gap_seconds: float = 60.0
    max_wakes_per_hour: int = 0
    max_wakes_per_day: int = 0
    max_timers: int = 24
    min_timer_seconds: float = 30.0
    max_timer_seconds: float = 604800.0
    max_tasks: int = 4
    task_max_seconds: float = 3600.0
    task_stall_seconds: float = 900.0
    keep_finished_tasks: int = 50
    wake_output_chars: int = 1500
    presence_away_seconds: float = 300.0
    # Wakes never interrupt the operator. They count as engaged, and wakes wait, for this long after a message or steer,
    engaged_message_seconds: float = 300.0
    # after they last typed in the composer,
    engaged_typing_seconds: float = 120.0
    # and while their window is focused, until this long passes with no message or typing (0: focus alone never counts).
    engaged_focus_seconds: float = 900.0
    # A focused window stops counting once its heartbeat is older than this.
    presence_stale_seconds: float = 300.0
    # Wake kinds that still deliver while the operator is engaged: answers to their own actions, such as an approval outcome.
    engaged_wake_kinds: list = field(default_factory=lambda: ['approval'])
    infra_poll_seconds: float = 300.0
    # A watched `systems` endpoint is fetched at most this often when its record
    # declares no `poll_seconds` of its own. Reachability only: one request, no body
    # read. The per-record signal wins, so a slow service can be given its own pace.
    endpoint_poll_seconds: float = 60.0
    endpoint_timeout_seconds: float = 5.0
    approval_expire_seconds: float = 3600.0
    # A session lesson, question, or intention wakes that session.
    # Floor pressure wakes the autonomy session on its own gap. An empty session agenda does not call it.
    self_prompt_enabled: bool = True
    self_prompt_gap_seconds: float = 900.0
    # How many of a session's own board cards one wake may name. Zero stops board offers.
    board_offer_limit: int = 3
    standing_floor: bool = True
    infinity_slug: str = 'infinity'
    # Display name for the autonomy session, used in its title and in the steer
    # prefix other sessions see. Empty means 'Autonomy': the slug is an address,
    # not a name, and a fresh install should not inherit one install's label.
    floor_name: str = ''
    # The user's world view, human-written in Settings: what the autonomy session
    # lives in and cares about. Generic installs have none until configured.
    world_view: str = ''
    # Optional generic endpoint sources the floor may watch. Each entry names a
    # label, an http(s) URL, and a poll interval in seconds. Empty by default;
    # the floor treats entries as requiring configuration before use.
    watch_sources: list = field(default_factory=list)
    lesson_file: str = 'learning/lessons.json'
    lesson_pass_score: float = 0.5
    max_open_lessons: int = 12
    lesson_fail_limit: int = 2
    # How much trust the oversight lane has to act on what it measures. The
    # operator picks this in Settings; it is a trust dial, not a default.
    #   'findings' - name the reading, change nothing.
    #   'approve'  - turn a finding into a card the operator accepts or rejects
    #                before anything is applied (try before adopt). Default.
    #   'auto'     - apply a bounded change on its own, but only after an A/B
    #                measurement shows it is better. The trial (tessera/trial.py)
    #                runs a bounded number of turns per arm and keeps the change
    #                only on a win; a tie or a loss reverts it. A reading alone
    #                never adopts anything.
    oversight_policy: str = 'approve'
    # How often open A/B trials are re-read. Each pass costs one bounded read of
    # a session's events, so this is far slower than the tick: trials measure
    # turns, and a turn takes longer than a tick by orders of magnitude. Zero
    # re-reads on every pass, which is what tests use.
    trial_poll_seconds: float = 60.0


@dataclass
class CuaConfig:
    """Computer use: screenshots and input on this Mac and on remotes with desk = true."""
    mac_enabled: bool = False
    # Legacy mac_look helper. A separate app from the native computer-use helper so the two builds
    # do not replace each other.
    mac_helper_app: str = 'cua/TesseraMacLook.app'
    mac_bundle_id: str = 'app.tessera.maclook'
    # A real identity (for example 'Apple Development: Name (TEAM)') keeps macOS grants across rebuilds.
    swiftc: str = '/usr/bin/swiftc'
    codesign_identity: str = '-'
    screencapture: str = '/usr/sbin/screencapture'
    sips: str = '/usr/bin/sips'
    max_image_px: int = 1600
    attach_images: bool = True
    timeout: float = 30.0
    # auto picks macOS on Darwin and the Linux tools on Linux. fake records actions for tests.
    backend: str = 'auto'
    screenshot_scale: float = 1.0
    osascript: str = 'osascript'
    input_command: str = 'xdotool'
    window_command: str = 'wmctrl'
    screenshot_commands: list = field(default_factory=lambda: ['scrot', 'import', 'gnome-screenshot'])


@dataclass
class ComputerUseJevConfig:
    """Optional Jev element picker for native computer use.

    Off unless enabled. Host and model come from settings or the environment
    and stay empty until set. Enabling the picker without both is a
    configuration error.
    """

    enabled: bool = False
    base_url: str = ''
    model: str = ''
    api_key_env: str = 'TYPESAFE_API_KEY'
    credential_ref: str = ''
    timeout: float = 8.0
    min_confidence: float = 0.5
    max_state_chars: int = 24000
    long_context: bool = False


@dataclass
class ComputerUseConfig:
    """Host computer use.

    ``auto`` on macOS uses the native helper when ``provider`` is empty or ``native``,
    and an MCP server when ``provider`` is ``mcp:<name>``. ``auto`` on Linux keeps
    the local desktop tools even if an MCP provider is set. ``provider = "native"``
    selects the helper on any platform (the binary itself runs on macOS). ``list_apps``
    and ``get_app_state`` stay read-only; every other computer-use tool follows the
    approval policy. ``jev`` adds read-only find and check tools when it is enabled.
    """
    backend: str = 'auto'
    # "native", "mcp:<mcp_servers name>", or empty. Empty on macOS is native.
    provider: str = ''
    # Prefix on the provider's tool names. Empty registers them as the server named them.
    tool_prefix: str = 'computer_'
    # Cap for accessibility text returned with a screenshot, before the general tool clip.
    text_chars: int = 12000
    # App bundle or executable. Relative paths are under the Tessera home. A .app uses
    # Contents/MacOS/TesseraCUA. Build it with scripts/build-cua-helper.sh.
    helper: str = 'cua/TesseraCUA.app'
    # Seconds to wait for one helper response.
    timeout: float = 30.0
    jev: ComputerUseJevConfig = field(default_factory=ComputerUseJevConfig)


@dataclass
class McpServerConfig:
    """An MCP server whose tools sessions may use: an HTTP(S) url or a stdio argv."""
    name: str = ''
    enabled: bool = True
    description: str = ''
    url: str = ''
    command: list = field(default_factory=list)
    env: dict = field(default_factory=dict)
    cwd: str = ''
    token_env: str = ''
    timeout: float = 30.0
    list_ttl: float = 300.0
    # Empty include exposes every tool except excluded ones.
    include: list = field(default_factory=list)
    exclude: list = field(default_factory=list)
    # Tools that run without approval; every other tool follows the approval policy.
    safe_tools: list = field(default_factory=list)
    # Registered tool name prefix; empty uses '<name>__'.
    tool_prefix: str = ''
    # False keeps the server on Integrations (checks) without adding its tools to sessions.
    session_tools: bool = True


@dataclass
class HostConfig:
    name: str = ''
    enabled: bool = False
    address: str = ''
    kind: str = ''
    ssh_alias: str = ''
    agent_url: str = ''
    disk_path: str = ''
    timeout: float = 3.0
    probe_command: list = field(default_factory=list)
    ssh_command: list = field(default_factory=list)
    services: dict = field(default_factory=dict)
    # Operator display name shown on the board (empty shows "Host N").
    label: str = ''
    # kind = "prometheus": PromQL per board metric; "{instance}" is replaced by prometheus_instance.
    prometheus_instance: str = ''
    prometheus_up_query: str = ''
    prometheus_queries: dict = field(default_factory=dict)
    # GPU metric -> PromQL; one series per GPU, grouped by the gpu label.
    prometheus_gpu_queries: dict = field(default_factory=dict)


@dataclass
class SystemConfig:
    """One purpose-bearing system record, loaded from `systems.*`."""
    name: str = ''
    kind: str = ''
    label: str = ''
    role: str = ''
    address: str = ''
    auth: str = ''
    watch: bool = False
    signals: dict = field(default_factory=dict)
    notes: str = ''


@dataclass
class InfrastructureConfig:
    # Direct, read-only engine occupancy sources; [] disables sampling.
    # Empty by default. An engine list is a fact about a deployment, not about
    # the product, so it belongs in config.toml.
    pressure_engines: list = field(default_factory=list)
    # Read-only SparkDash bridge; authentication remains on the source host.
    sparkdash_command: list[str] = field(default_factory=list)
    sparkdash_url: str = ''
    enabled: bool = False
    snapshot_file: str = 'infrastructure.jsonl'
    stale_after: float = 300.0
    max_hosts: int = 100
    max_output_bytes: int = 65536
    # Existing Prometheus (for example the one behind Grafana) read over its HTTP query API.
    prometheus_url: str = ''
    prometheus_query_path: str = '/api/v1/query'
    # Seconds between background collections of every enabled host; 0 collects only on request.
    collect_interval: float = 0.0


@dataclass
class GrafanaConfig:
    enabled: bool = False
    base_url: str = ''
    token_env: str = ''
    credential_ref: str = ''
    timeout: float = 5.0
    max_response_bytes: int = 1048576
    list_limit: int = 100
    datasources_path: str = '/api/datasources'
    dashboards_path: str = '/api/search'
    proxy_path: str = '/api/datasources/proxy/uid/{uid}/{path}'
    embed_path: str = '/d-solo/{uid}/{slug}'
    allowed_proxy_paths: list = field(default_factory=list)


@dataclass
class CodexCatalogConfig:
    """Where the ChatGPT/Codex sign-in lists models. Both values are settings."""

    models_url: str = 'https://chatgpt.com/backend-api/codex/models'
    client_version: str = '99.0.0'


@dataclass
class SpeechServiceConfig:
    """One local speech service that speaks the OpenAI-compatible audio API.

    ``models`` is what the endpoint reported the last time it was checked, so the
    page offers names the server actually serves instead of asking for a guess.
    """
    base_url: str = ''
    model: str = ''
    models: list = field(default_factory=list)
    timeout: float = 10.0
    max_response_bytes: int = 4194304


@dataclass
class SpeechOutputConfig(SpeechServiceConfig):
    """Text-to-speech also takes the voice name to speak with."""
    voice: str = ''


@dataclass
class VoiceConfig:
    enabled: bool = False
    # Which pipe voice runs. Empty picks duplex when both pipes are configured,
    # because duplex is the proven one. A single configured pipe needs no choice.
    pipe: str = ''
    websocket_url: str = ''
    model: str = ''
    api_base_url: str = ''
    client_secret_path: str = ''
    api_key_env: str = ''
    credential_ref: str = ''
    client_protocol_prefix: str = ''
    model_query_param: str = 'model'
    max_session_seconds: int = 600
    secret_ttl_seconds: int = 60
    clock_skew_seconds: float = 5.0
    timeout: float = 5.0
    max_response_bytes: int = 65536
    max_event_bytes: int = 65536
    max_text_bytes: int = 16384
    max_events: int = 1000
    max_sessions: int = 4
    # Only leases that opt into client keepalive are eligible for idle reaping.
    # A client that never beats (the iOS app) keeps the deadline as its backstop.
    # 150s, not 45s: a browser throttles timers in a hidden tab to about once a
    # minute after five minutes, so a tighter window would reap a live web call
    # whose tab merely lost focus. Still far below the 600s deadline.
    idle_reap_seconds: int = 150
    audio_sample_rate: int = 24000
    audio_format: str = 'audio/pcm'
    turn_detection: dict = field(default_factory=lambda: {'type': 'server_vad'})
    session_options: dict = field(default_factory=dict)
    tools: list = field(default_factory=list)
    tools_mode: str = 'configured'
    # Voices a client may pick per start; session_options.voice stays the default.
    voices: list = field(default_factory=list)
    # A voice session of its own, not a picker over ordinary chats. It carries its
    # own name and its own reasoning posture, because a spoken turn is answered in
    # a sentence or two and a deep reasoning pass there is latency the speaker
    # hears. The spoken side answers; tool work is handed to the same session,
    # which is the restriction that made voice usable instead of a parrot.
    dedicated_slug: str = 'voice'
    dedicated_title: str = 'Voice'
    dedicated_reasoning: str = 'low'
    dedicated_thinking_budget: int = 0
    # Local speech: a speech-to-text service and a text-to-speech service. Both
    # are needed for the local pipe; one alone configures nothing.
    stt: SpeechServiceConfig = field(default_factory=SpeechServiceConfig)
    tts: SpeechOutputConfig = field(default_factory=SpeechOutputConfig)
    # Push-to-talk bounds. One utterance goes in, one reply comes out; a turn
    # that takes longer than this stops waiting instead of holding the request.
    local_reply_timeout: float = 120.0
    local_max_audio_bytes: int = 786432


@dataclass
class AtlasConfig:
    enabled: bool = False
    endpoints: list = field(default_factory=list)
    token_env: str = ''
    timeout: float = 8.0
    probe_timeout: float = 2.0
    probe_interval: float = 30.0
    allow_index_refresh: bool = False
    brief_tool: str = ''
    contracts_tool: str = ''
    read_tool: str = ''
    # Selected only when allow_index_refresh is true; never called otherwise.
    refresh_tool: str = ''
    read_uri_prefixes: list = field(default_factory=list)
    max_brief_bytes: int = 4000
    max_briefs_per_session: int = 3
    # Extra arguments merged into every brief call (the task is always set by Tessera).
    brief_arguments: dict = field(default_factory=dict)
    # Run the session-start brief in the background; it joins the turn state when it lands.
    start_brief_background: bool = False


@dataclass
class EmberConfig:
    enabled: bool = False
    helmet_endpoints: list = field(default_factory=list)
    companion_endpoints: list = field(default_factory=list)
    token_env: str = ''
    timeout: float = 8.0
    probe_timeout: float = 2.0
    probe_interval: float = 30.0
    surface: str = ''
    profile: str = ''
    host_label: str = ''
    # Operator-written companion instance; encounter is skipped while empty.
    instance_id: str = ''
    # Remote tool behind the resident ember_decision tool; empty keeps it unavailable.
    decision_tool: str = ''
    allow_transition: bool = False
    transition_authority_env: str = ''


@dataclass
class CompanionsConfig:
    atlas: AtlasConfig = field(default_factory=AtlasConfig)
    ember: EmberConfig = field(default_factory=EmberConfig)


@dataclass
class InspectorConfig:
    enabled: bool = False
    socket_path: str = 'run/inspector.sock'
    source_root: str = ''
    max_frame_bytes: int = 8388608
    allow_groups: list = field(default_factory=list)
    operator_approval_env: str = ''
    max_note_bytes: int = 8192
    max_pending_requests: int = 8


@dataclass
class StatusConfig:
    task_limit: int = 100
    max_file_bytes: int = 4194304
    max_total_bytes: int = 8388608
    max_line_bytes: int = 262144
    max_logs: int = 1000


@dataclass
class UiConfig:
    # The full-width warning while approval is yolo. The button label is separate.
    yolo_banner: bool = True
    # False labels that button Unleashed. True labels it F-it and shows the cartoon.
    easter_egg_f_it: bool = False
    # An error event older than this (or one recorded before events carried a time) opens no banner.
    error_banner_seconds: float = 600.0
    # Opening a session loads this many newest messages; older ones load on demand.
    history_page: int = 60
    # Opening a session shows this many newest view lines; older ones stay in the
    # tree and can still be zoomed by start+count. 0 shows the whole view.
    view_page_lines: int = 80
    # Tool/result bodies longer than this arrive clipped; expanding one fetches it whole.
    item_body_chars: int = 4000
    # Length of the one-line summary on a collapsed tool item.
    item_summary_chars: int = 120
    # Touch hold that opens a session row's action menu.
    long_press_ms: int = 500
    # How often an open page re-reads the Safe/Unleashed mode so every view follows a switch; 0 = status poll only.
    approval_poll_ms: int = 1000
    # system follows the OS. light and dark are explicit.
    theme: str = 'system'
    # cairn is the default art with the Tessera-CT wordmark.
    # gremlin is the F-it cartoon and is shown only while easter_egg_f_it is on.
    mark: str = 'cairn'
    skin: str = 'strata'
    accent_color: str = ''
    show_thoughts: bool = True
    # enter sends on Enter. modifier sends on Ctrl or Command + Enter.
    send_shortcut: str = 'enter'
    # How many newest config changes Settings lists. The log beside config.toml
    # keeps this many lines, and the page says how many of them it is showing.
    config_journal_entries: int = 40


@dataclass
class Config:
    def identity_files(self):
        """Configured identity paths in load order (backward compatible with one string)."""
        value = self.identity_file
        if isinstance(value, list):
            return list(value)
        return [value] if value else []

    def operator_label(self):
        """How prompts name the human operating this install.

        Empty config means nobody is named: the text says "the operator".
        A fresh install never inherits someone else's name.
        """
        return self.operator_name.strip() or 'the operator'

    def operator_label_sentence(self):
        """The operator label with its first character capitalized.

        Only the first character is raised: str.capitalize() would lowercase
        the rest of the name, so a two-word name would come back with its
        second word wrongly lowercased.
        """
        label = self.operator_label()
        return label[:1].upper() + label[1:]

    def floor_label(self):
        """Display name for the autonomy session. The slug is an address, not a name."""
        return self.autonomy.floor_name.strip() or 'Autonomy'

    home: Path = field(default_factory=lambda: Path(os.environ.get('TESSERA_HOME', '~/.tessera')).expanduser())
    cwd: Path = field(default_factory=Path.cwd)
    # Set by load_config when no config.toml was found at `home`. A missing file
    # and an intentionally default file were indistinguishable, so a typo'd
    # --home started a healthy-looking core on defaults with no diagnostic.
    config_missing: bool = False
    # Default checkout for new sessions. Empty follows cwd. A session may override it.
    workspace: str = ''
    agent_name: str = 'Tessera'
    # The human operating this install, as they want to be addressed. Empty is
    # the distributable default: prompts then say "the operator" instead of
    # naming someone. A configured name is only ever this install's own.
    operator_name: str = ''
    user_instructions: str = ''
    backends: dict = field(default_factory=dict)
    roles: dict = field(default_factory=dict)
    accounts: dict = field(default_factory=dict)
    credentials: dict = field(default_factory=dict)
    aethermind: AetherMindConfig = field(default_factory=AetherMindConfig)
    # Older keys a file or TESSERA_* overlay still carries. Settings shows them.
    # Nothing in the turn path reads them, and a value here cannot turn memory off.
    retired_settings: dict = field(default_factory=dict)
    jev: JevConfig = field(default_factory=JevConfig)
    compactor: CompactorConfig = field(default_factory=CompactorConfig)
    view: ViewConfig = field(default_factory=ViewConfig)
    tools: ToolsConfig = field(default_factory=ToolsConfig)
    hooks: list = field(default_factory=list)
    agents: AgentsConfig = field(default_factory=AgentsConfig)
    server: ServerConfig = field(default_factory=ServerConfig)
    daemon: DaemonConfig = field(default_factory=DaemonConfig)
    swarm: SwarmConfig = field(default_factory=SwarmConfig)
    status: StatusConfig = field(default_factory=StatusConfig)
    channels: ChannelsConfig = field(default_factory=ChannelsConfig)
    board: BoardConfig = field(default_factory=BoardConfig)
    intake: IntakeConfig = field(default_factory=IntakeConfig)
    identity: IdentityConfig = field(default_factory=IdentityConfig)
    hosts: dict = field(default_factory=dict)
    systems: dict = field(default_factory=dict)
    remotes: dict = field(default_factory=dict)
    mcp_servers: dict = field(default_factory=dict)
    infrastructure: InfrastructureConfig = field(default_factory=InfrastructureConfig)
    grafana: GrafanaConfig = field(default_factory=GrafanaConfig)
    voice: VoiceConfig = field(default_factory=VoiceConfig)
    codex: CodexCatalogConfig = field(default_factory=CodexCatalogConfig)
    companions: CompanionsConfig = field(default_factory=CompanionsConfig)
    inspector: InspectorConfig = field(default_factory=InspectorConfig)
    autonomy: AutonomyConfig = field(default_factory=AutonomyConfig)
    cua: CuaConfig = field(default_factory=CuaConfig)
    computer_use: ComputerUseConfig = field(default_factory=ComputerUseConfig)
    prompt: PromptConfig = field(default_factory=PromptConfig)
    skills: SkillsConfig = field(default_factory=SkillsConfig)
    ui: UiConfig = field(default_factory=UiConfig)
    # Fixed class name when [inspector] could not be loaded; the socket is skipped.
    inspector_error: str = ''
    # Operator identity file(s), for example SOUL.md and CORTEX.md. A single path string
    # or a list of paths loaded in order. Empty means none.
    identity_file: object = ''
    identity_max_bytes: int = 65536

    def validate(self):
        self.credentials = CredentialResolver(self.credentials).references
        for name, account in self.accounts.items():
            _validate_fields(account, 'account')
            if not name or account.name != name or account.provider_kind not in PROVIDER_KINDS:
                raise ValueError('Invalid account name or provider kind')
            if account.credential_ref and account.credential_ref not in self.credentials:
                raise ValueError('Account credential_ref is not configured')
            if account.provider_kind == 'cli_worker' and account.credential_ref:
                raise ValueError('CLI accounts use the official CLI login, not API credentials')
            for limits in (account.limits, account.budget):
                for value in limits.values():
                    if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
                        raise ValueError('Account limits and budget must be finite nonnegative numbers')
        for name in ('agent_name', 'operator_name', 'user_instructions'):
            if not isinstance(getattr(self, name), str):
                raise ValueError(f'{name} must be text')
        for section in ('aethermind', 'jev', 'view', 'tools', 'agents', 'server', 'daemon', 'compactor', 'swarm', 'status', 'channels', 'board', 'intake', 'identity', 'infrastructure', 'grafana', 'voice', 'autonomy', 'cua', 'computer_use', 'prompt', 'skills', 'ui'):
            value = getattr(self, section)
            _validate_fields(value, section)
        _validate_codex(self.codex)
        skills = self.skills
        if (skills.note_bytes < 128 or skills.note_description_chars < 16 or skills.search_limit < 1
                or not all(isinstance(item, str) and item for item in skills.trusted_projects + skills.project_dirs)):
            raise ValueError('Invalid skills settings')
        _name_list(self.tools.disabled, 'tools.disabled')
        _name_list(skills.disabled, 'skills.disabled')
        _skill_directories(skills.extra_dirs)
        if (self.daemon.max_frame_bytes < 1024 or self.daemon.max_pending_requests < 1
                or self.daemon.approval_timeout <= 0 or self.daemon.event_poll_seconds <= 0
                or self.daemon.history_page_size < 1 or self.daemon.zoom_page_bytes < 1
                or self.daemon.mirror_retry_seconds < 0 or not self.daemon.mirror_dir):
            raise ValueError('Invalid daemon bounds or mirror directory')
        autonomy = self.autonomy
        if (autonomy.tick_seconds <= 0 or autonomy.min_wake_gap_seconds < 0
                or autonomy.max_wakes_per_hour < 0 or autonomy.max_wakes_per_day < 0 or autonomy.max_timers < 0
                or autonomy.min_timer_seconds <= 0 or autonomy.max_timer_seconds < autonomy.min_timer_seconds
                or autonomy.max_tasks < 0 or autonomy.task_max_seconds <= 0 or autonomy.task_stall_seconds <= 0
                or autonomy.keep_finished_tasks < 0 or autonomy.wake_output_chars < 0
                or autonomy.presence_away_seconds < 0 or autonomy.engaged_message_seconds < 0
                or autonomy.engaged_typing_seconds < 0 or autonomy.engaged_focus_seconds < 0
                or autonomy.presence_stale_seconds <= 0 or autonomy.infra_poll_seconds <= 0
                or autonomy.endpoint_poll_seconds <= 0 or autonomy.endpoint_timeout_seconds <= 0
                or autonomy.approval_expire_seconds <= 0 or autonomy.self_prompt_gap_seconds < 0
                or autonomy.board_offer_limit < 0
                or not isinstance(autonomy.standing_floor, bool)
                or not isinstance(autonomy.infinity_slug, str) or not autonomy.infinity_slug
                or not isinstance(autonomy.world_view, str)
                or not isinstance(autonomy.watch_sources, list)
                or any(not isinstance(item, dict) for item in autonomy.watch_sources)
                or autonomy.max_open_lessons < 1 or autonomy.lesson_fail_limit < 1
                or not autonomy.lesson_file
                or not 0 <= autonomy.lesson_pass_score <= 1
                or autonomy.oversight_policy not in ('findings', 'approve', 'auto')
                or autonomy.trial_poll_seconds < 0):
            raise ValueError('Invalid autonomy limits')
        for source in self.autonomy.watch_sources:
            if set(source) != {'name', 'url', 'interval_seconds'}:
                raise ValueError('Invalid autonomy watch source')
            entry = source['name']
            poll = source['interval_seconds']
            if not isinstance(entry, str) or not entry:
                raise ValueError('Invalid autonomy watch source')
            if not isinstance(source['url'], str) or not source['url'].startswith(('http://', 'https://')):
                raise ValueError('Invalid autonomy watch source')
            if type(poll) is not int or poll <= 0:
                raise ValueError('Invalid autonomy watch source')
        if self.cua.max_image_px < 64 or self.cua.timeout <= 0 or self.cua.screenshot_scale <= 0:
            raise ValueError('Invalid cua limits')
        if self.cua.backend not in ('auto', 'fake', 'linux', 'mac'):
            raise ValueError('cua.backend must be auto, fake, linux, or mac')
        if not isinstance(self.cua.screenshot_commands, list) or not all(isinstance(item, str) and item for item in self.cua.screenshot_commands):
            raise ValueError('cua.screenshot_commands must name commands')
        use = self.computer_use
        if use.backend not in ('auto', 'fake', 'linux', 'mcp', 'native'):
            raise ValueError('computer_use.backend must be auto, fake, linux, mcp, or native')
        if not isinstance(use.tool_prefix, str) or type(use.text_chars) is not int or use.text_chars < 64:
            raise ValueError('computer_use.tool_prefix must be a string and text_chars at least 64')
        if not isinstance(use.helper, str) or not use.helper.strip():
            raise ValueError('computer_use.helper must be a path')
        if type(use.timeout) not in (int, float) or isinstance(use.timeout, bool) or use.timeout <= 0:
            raise ValueError('computer_use.timeout must be a positive number')
        if use.provider and use.provider != 'native' and not (use.provider.startswith('mcp:') and use.provider[4:]):
            raise ValueError('computer_use.provider must be empty, "native", or mcp:<server name>')
        if use.backend == 'mcp' and not use.provider.startswith('mcp:'):
            raise ValueError('computer_use.backend mcp needs provider = "mcp:<server name>"')
        if use.backend == 'native' and use.provider.startswith('mcp:'):
            raise ValueError('computer_use.backend native does not use an mcp provider')
        if use.provider.startswith('mcp:'):
            server_name = use.provider.split(':', 1)[1]
            server = self.mcp_servers.get(server_name)
            if server is None or not server.enabled:
                raise ValueError(f'computer_use.provider names unknown or disabled mcp_servers.{server_name}')
        _validate_computer_use_jev(use.jev)
        if use.jev.credential_ref and use.jev.credential_ref not in self.credentials:
            raise ValueError('computer_use.jev credential_ref is not configured')
        tool_ints = {
            'tools.output_tail_chars': self.tools.output_tail_chars,
            'tools.read_line_limit': self.tools.read_line_limit,
            'tools.glob_limit': self.tools.glob_limit,
            'tools.read_bytes_limit': self.tools.read_bytes_limit,
            'tools.image_bytes_limit': self.tools.image_bytes_limit,
            'tools.fetch_max_bytes': self.tools.fetch_max_bytes,
        }
        for name, value in tool_ints.items():
            if type(value) is not int or value <= 0:
                raise ValueError(f'{name} must be a positive integer')
        if self.tools.fetch_timeout <= 0 or self.tools.shell_max_timeout <= 0 or not self.tools.spill_dir or '\x00' in self.tools.spill_dir:
            raise ValueError('Invalid tool output or fetch limits')
        if not self.swarm.tasks_file or self.swarm.max_attempts <= 0 or self.swarm.lease_seconds <= 0:
            raise ValueError('Invalid swarm task path, attempts or lease')
        if not self.swarm.jobs_dir or self.swarm.max_artifact_bytes <= 0:
            raise ValueError('Invalid swarm job directory or bounds')
        tasks = self.swarm.max_job_tasks
        if tasks is not None and (type(tasks) is not int or isinstance(tasks, bool) or tasks < 2):
            raise ValueError('Invalid swarm job directory or bounds')
        if self.swarm.distinct_workers is not None and type(self.swarm.distinct_workers) is not bool:
            raise ValueError('swarm.distinct_workers must be true, false, or omitted')
        if (type(self.swarm.pressure_hold_enabled) is not bool
                or type(self.swarm.pressure_soft_threshold) not in (int, float)
                or not 0 < self.swarm.pressure_soft_threshold <= 1):
            raise ValueError('Invalid swarm pressure hold or soft threshold (0, 1]')
        from .infrastructure import validate_inventory
        from .grafana import validate_settings
        from .engine_pressure import validate_engines
        validate_engines(self.infrastructure.pressure_engines)
        validate_inventory(self)
        validate_settings(self.grafana, self.credentials)
        from .voice import validate_voice
        validate_voice(self.voice, self.credentials)
        board = self.board
        if not board.journal_file or not 0 <= board.ask_human_threshold <= 1:
            raise ValueError('Invalid board journal or ask-human threshold')
        for name in ('max_text_bytes', 'max_artifacts', 'max_questions'):
            if getattr(board, name) <= 0:
                raise ValueError('Board bounds must be positive integers')
        if not isinstance(board.acceptance_required, bool):
            raise ValueError('board.acceptance_required must be true or false')
        if not isinstance(board.acceptance_by, list) or any(
                not isinstance(item, str) or not item for item in board.acceptance_by):
            raise ValueError('board.acceptance_by must be a list of identity names')
        if type(self.intake.limit) is not int or self.intake.limit <= 0:
            raise ValueError('intake.limit must be a positive integer')
        if not isinstance(self.intake.timeout_seconds, (int, float)) or isinstance(self.intake.timeout_seconds, bool):
            raise ValueError('intake.timeout_seconds must be a number')
        if not math.isfinite(self.intake.timeout_seconds) or self.intake.timeout_seconds <= 0:
            raise ValueError('intake.timeout_seconds must be positive and finite')
        from .channels import ChannelStore
        channels = self.channels
        if (not channels.journal_file or channels.delivery_limit <= 0
                or not 0 <= channels.needs_reply_threshold <= 1):
            raise ValueError('Invalid channel journal, delivery bound or reply threshold')
        ChannelStore(channels.journal_file, max_hops=channels.max_hops, rate_limit=channels.rate_limit,
                     rate_window=channels.rate_window, max_text_bytes=channels.max_text_bytes,
                     rate_overrides=channels.rate_overrides)
        for item in fields(self.status):
            if getattr(self.status, item.name) <= 0:
                raise ValueError('Status bounds must be positive integers')
        policy = self.swarm
        if (policy.retry_base < 0 or policy.retry_cap < policy.retry_base
                or policy.circuit_failures <= 0 or policy.circuit_cooldown <= 0):
            raise ValueError('Invalid swarm retry or circuit settings')
        integers = {'view.node_bytes': self.view.node_bytes,
                    'view.high_bytes': self.view.high_bytes, 'view.low_bytes': self.view.low_bytes,
                    'tools.clip_chars': self.tools.clip_chars}
        for name, value in integers.items():
            if type(value) is not int or value <= 0:
                raise ValueError(f'{name} must be a positive integer')
        if self.tools.approval not in ('ask', 'never', 'deny_risky', 'yolo'):
            raise ValueError('tools.approval must be ask, never, deny_risky, or yolo')
        for key, rule in self.tools.approval_rules.items():
            if rule not in ('allow', 'gate'):
                raise ValueError(f'tools.approval_rules.{key} must be allow or gate')
        for key, patterns in self.tools.approval_classes.items():
            if not isinstance(patterns, list) or not all(isinstance(p, str) for p in patterns):
                raise ValueError(f'tools.approval_classes.{key} must be a list of tool name patterns')
        if not self.tools.approval_state_file:
            raise ValueError('tools.approval_state_file must be a path')
        if not 0 < self.view.low_bytes < self.view.high_bytes:
            raise ValueError('view budgets must satisfy 0 < low_bytes < high_bytes')
        if type(self.view.prompt_bytes) is not int or self.view.prompt_bytes <= 0:
            raise ValueError('view.prompt_bytes must be a positive integer')
        for value in (self.tools.timeout, self.aethermind.timeout, self.jev.timeout):
            if value <= 0:
                raise ValueError('timeouts must be positive')
        memory = self.aethermind
        if not isinstance(memory.endpoints, list):
            raise ValueError('aethermind.endpoints must be a list of HTTP(S) URLs')
        for endpoint in memory.endpoints + ([memory.url] if memory.url else []):
            if isinstance(endpoint, str) and endpoint.startswith('mcp:'):
                # A bundled or locally managed stdio server, resolved by name.
                if endpoint[4:] not in self.mcp_servers:
                    raise ValueError('aethermind endpoint names an unknown mcp_servers entry')
                continue
            if not isinstance(endpoint, str):
                raise ValueError('aethermind endpoints must be HTTP(S) URLs without credentials')
            parsed = urlsplit(endpoint)
            if parsed.scheme not in ('http', 'https') or not parsed.netloc or parsed.username is not None:
                raise ValueError('aethermind endpoints must be HTTP(S) URLs without credentials')
        if memory.probe_timeout <= 0 or memory.probe_interval < 0:
            raise ValueError('aethermind requires a positive probe_timeout and nonnegative probe_interval')
        if memory.source not in ('', 'embedded', 'external'):
            raise ValueError('aethermind.source must be embedded or external')
        if memory.personal_project and memory.personal_project == memory.project:
            raise ValueError('aethermind project and read-only personal_project must differ')
        if memory.note_project and memory.note_project == memory.personal_project and memory.personal_read_only:
            raise ValueError('aethermind note_project is the personal store; set personal_read_only = false')
        if memory.recall_scan < 1 or memory.recall_limit < 1:
            raise ValueError('aethermind recall_scan and recall_limit must be positive')
        if type(memory.inheritance_refresh_seconds) not in (int, float) or memory.inheritance_refresh_seconds < 0:
            raise ValueError('aethermind.inheritance_refresh_seconds must be 0 or more')
        if type(memory.engine_log_max_bytes) is not int or memory.engine_log_max_bytes < 1:
            raise ValueError('aethermind.engine_log_max_bytes must be a positive integer')
        if not isinstance(self.agents.concurrency_by_kind, dict):
            raise ValueError('agents.concurrency_by_kind must be a table')
        for kind, capacity in self.agents.concurrency_by_kind.items():
            if kind not in PROVIDER_KINDS or type(capacity) is not int or capacity < 0:
                raise ValueError('Agent concurrency caps must name provider kinds and integers of 0 (no limit) or more')
        if self.agents.max_depth < 0:
            raise ValueError('agents.max_depth cannot be negative')
        for name, server in self.mcp_servers.items():
            _validate_fields(server, 'mcp_server')
            lists_ok = all(isinstance(getattr(server, k), list) and all(isinstance(v, str) and v for v in getattr(server, k))
                           for k in ('command', 'include', 'exclude', 'safe_tools'))
            if (not name or server.name != name or not lists_ok or bool(server.url) == bool(server.command)
                    or not isinstance(server.env, dict) or any(not isinstance(k, str) or not isinstance(v, str) for k, v in server.env.items())
                    or server.timeout <= 0 or server.list_ttl < 0):
                raise ValueError(f'Invalid mcp_servers.{name}: needs exactly one of url or command, string lists and positive timeout')
            if server.url:
                _http_endpoints([server.url], f'mcp_servers.{name}.url')
        validate_companions(self.companions, self.mcp_servers)
        for name, remote in self.remotes.items():
            _validate_fields(remote, 'remote')
            if (not name or remote.name != name or not remote.host or not remote.user
                    or remote.host.startswith('-') or remote.user.startswith('-')
                    or any(c in remote.host + remote.user for c in '@ \t\n')
                    or not 0 < remote.port < 65536 or remote.connect_timeout <= 0 or remote.timeout <= 0
                    or remote.strict_host_key_checking not in ('yes', 'accept-new')
                    or not isinstance(remote.ssh_command, list) or not remote.ssh_command
                    or any(not isinstance(part, str) or not part for part in remote.ssh_command)):
                raise ValueError(f'Invalid remote {name}: needs host, user, port 1..65535, positive timeouts, '
                                 'strict_host_key_checking yes|accept-new and an ssh_command argv')
        files = self.identity_file if isinstance(self.identity_file, list) else [self.identity_file]
        if (any(not isinstance(item, str) for item in files)
                or (isinstance(self.identity_file, list) and any(not item for item in files))
                or type(self.identity_max_bytes) is not int or self.identity_max_bytes <= 0):
            raise ValueError('identity_file must be a path string or list of path strings with a positive identity_max_bytes')
        if (type(self.agents.max_concurrent) is not int or self.agents.max_concurrent < 0
                or self.agents.token_budget < 0
                or self.agents.judge_tokens < 0 or self.agents.wall_seconds < 0
                or self.agents.view_bytes < 0 or not self.agents.runs_dir):
            raise ValueError('Invalid daemon agent budgets or directory')
        if (self.agents.planner_tokens < 0 or self.agents.worktree_timeout <= 0
                or self.agents.validation_timeout <= 0 or not self.agents.worktrees_dir
                or not self.agents.git_command):
            raise ValueError('Invalid agent planning/worktree settings')
        if self.agents.worktree_result_format not in ('native', 'edits_json') or self.agents.worktree_context_bytes <= 0:
            raise ValueError('Invalid worktree result format or context bound')
        if any(not isinstance(p, str) or not p for p in self.agents.worktree_context_files):
            raise ValueError('Worktree context files must be relative text paths')
        if self.agents.worker_backend and self.agents.worker_backend not in self.backends:
            raise ValueError('Agent worker backend is not configured')
        for name, policy in self.agents.backend_policy.items():
            if name not in self.backends or not isinstance(policy, dict):
                raise ValueError('Agent backend policy must name a configured backend')
            if set(policy) - {'capability', 'cost_rank', 'description'}:
                raise ValueError('Unknown agent backend policy fields')
            if policy.get('capability') not in ('routine', 'standard', 'complex'):
                raise ValueError('Agent backend capability must be routine, standard or complex')
            rank = policy.get('cost_rank')
            if type(rank) not in (int, float) or not math.isfinite(rank) or rank < 0:
                raise ValueError('Agent backend cost rank must be finite and nonnegative')
            if not isinstance(policy.get('description', ''), str):
                raise ValueError('Agent backend policy description must be text')
        if self.agents.worktrees_enabled and (not self.agents.repo_root or not self.agents.allowed_scopes):
            raise ValueError('Agent worktrees require an explicit repository root and allowed scopes')
        if any(not isinstance(scope, str) or not scope.strip() for scope in self.agents.allowed_scopes):
            raise ValueError('Agent allowed scopes require nonempty text')
        if any(not isinstance(cmd, list) or not cmd or any(not isinstance(a, str) for a in cmd)
               for cmd in self.agents.validation_commands):
            raise ValueError('Agent validations require configured argv arrays')
        if self.agents.enabled and self.agents.wall_seconds >= self.swarm.lease_seconds:
            raise ValueError('Daemon agent wall budget must be shorter than the task lease')
        if self.jev.credential_ref and self.jev.credential_ref not in self.credentials:
            raise ValueError('Jev credential_ref is not configured')
        if (not 0 <= self.jev.gate_threshold <= 1 or not 0 <= self.jev.goal_threshold <= 1
                or self.jev.loop_window < 2 or self.jev.batch_items <= 0):
            raise ValueError('Invalid Jev gate thresholds, loop window or batch bound')
        if not 0 <= self.jev.risk_threshold <= 1:
            raise ValueError('jev.risk_threshold must be between zero and one')
        if (self.jev.cache_size <= 0 or not isinstance(self.jev.gated_tools, list)
                or any(not isinstance(name, str) for name in self.jev.gated_tools)):
            raise ValueError('Invalid Jev cache or gated_tools')
        if (self.compactor.workers <= 0 or self.compactor.retries < 0 or self.compactor.context_bytes < 0
                or not 0 <= self.compactor.judge_threshold <= 1):
            raise ValueError('Invalid compactor options')
        if type(self.aethermind.auto_recall) is not bool:
            raise ValueError('aethermind.auto_recall must be true or false')
        for settings in self.backends.values():
            _validate_fields(settings, 'backend')
            if type(settings.chat) is not bool:
                raise ValueError('backend chat flag must be true or false')
            if type(settings.enabled) is not bool:
                raise ValueError('backend enabled must be true or false')
            cap = settings.max_concurrent_requests
            if cap is not None and (type(cap) is not int or isinstance(cap, bool) or cap < 1):
                raise ValueError('backend max_concurrent_requests must be a positive integer or omitted')
            for label in ('throttle_requests_per_minute', 'throttle_tokens_per_minute'):
                pace = getattr(settings, label)
                if pace is not None and (type(pace) is not int or isinstance(pace, bool) or pace < 1):
                    raise ValueError(f'backend {label} must be a positive integer or omitted')
            if settings.provider_kind and settings.provider_kind not in PROVIDER_KINDS:
                raise ValueError('Unknown backend provider kind')
            if settings.credential_ref and settings.credential_ref not in self.credentials:
                raise ValueError('Backend credential_ref is not configured')
            if settings.api_key_env and (not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]{0,63}', settings.api_key_env)
                                         or flagged_env_value(settings.api_key_env)):
                raise ValueError('backend api_key_env must be an environment variable name, not a secret value')
            if settings.account:
                if settings.account not in self.accounts:
                    raise ValueError('Backend account is not configured')
                if settings.provider_kind or settings.credential_ref or settings.api_key_env:
                    raise ValueError('Account-bound backend must use account provider and credentials')
            kind = self.accounts[settings.account].provider_kind if settings.account else settings.provider_kind
            if kind == 'cli_worker':
                if settings.credential_ref or settings.api_key_env:
                    raise ValueError('CLI workers use the official CLI login, not API credentials')
                cli_settings(settings.extra.get('cli', {}))
            elif not settings.model or urlsplit(settings.base_url).scheme not in ('http', 'https') or not urlsplit(settings.base_url).netloc:
                raise ValueError(f'Backend {settings.name} requires an http(s) base_url and model')
        for role, name in self.roles.items():
            if name not in self.backends:
                raise ValueError(f'Role {role} names unknown backend {name}')
            if not self.backends[name].enabled:
                raise ValueError(f'Role {role} names a disabled backend {name}')
        if not isinstance(self.hooks, list):
            raise ValueError('hooks.shell must be a list')
        for hook in self.hooks:
            if not isinstance(hook, dict) or not isinstance(hook.get('command'), str) or not isinstance(hook.get('event'), str):
                raise ValueError('Each shell hook requires event and command strings')
        if not 0 <= self.server.port <= 65535 or self.server.max_hops < 0:
            raise ValueError('Invalid server port or hop limit')
        if not isinstance(self.ui.yolo_banner, bool):
            raise ValueError('ui.yolo_banner must be true or false')
        if not isinstance(self.ui.easter_egg_f_it, bool):
            raise ValueError('ui.easter_egg_f_it must be true or false')
        if self.ui.theme not in ('system', 'light', 'dark'):
            raise ValueError('ui.theme must be system, light, or dark')
        if self.ui.mark not in ('cairn', 'gremlin'):
            raise ValueError('ui.mark must be cairn or gremlin')
        if self.ui.skin not in ('strata', 'basalt', 'tide'):
            raise ValueError('ui.skin must be strata, basalt, or tide')
        if not isinstance(self.ui.accent_color, str) or (self.ui.accent_color and not re.fullmatch(r'#[0-9a-fA-F]{6}', self.ui.accent_color)):
            raise ValueError('ui.accent_color must be a six-digit hex color or empty for the skin default')
        if not isinstance(self.ui.show_thoughts, bool):
            raise ValueError('ui.show_thoughts must be true or false')
        if self.ui.send_shortcut not in ('enter', 'modifier'):
            raise ValueError('ui.send_shortcut must be enter or modifier')
        for name, low in (('history_page', 1), ('item_body_chars', 200), ('item_summary_chars', 20), ('long_press_ms', 100), ('approval_poll_ms', 0), ('config_journal_entries', 1)):
            value = getattr(self.ui, name)
            if type(value) is not int or value < low:
                raise ValueError(f'ui.{name} must be an integer >= {low}')
        if not isinstance(self.server.reply_quote_chars, int) or self.server.reply_quote_chars < 1:
            raise ValueError('server.reply_quote_chars must be a positive integer')
        if type(self.server.compress_min_bytes) is not int or self.server.compress_min_bytes < 0:
            raise ValueError('server.compress_min_bytes must be an integer >= 0')
        if not isinstance(self.server.allowed_hosts, list) or any(
                not isinstance(name, str) or not re.fullmatch(r'[A-Za-z0-9](?:[A-Za-z0-9.-]{0,251}[A-Za-z0-9])?\.?', name)
                for name in self.server.allowed_hosts):
            raise ValueError('server.allowed_hosts must be bare host names (no scheme, port, path or wildcard)')
        if not isinstance(self.server.auth_token, str) or any(c.isspace() for c in self.server.auth_token):
            raise ValueError('server.auth_token must be a string with no whitespace')
        if self.server.auth_mode not in ('loopback', 'always'):
            raise ValueError("server.auth_mode must be 'loopback' or 'always'")
        if self.server.auth_mode == 'always' and not self.server.auth_token:
            raise ValueError("server.auth_mode 'always' requires server.auth_token")
        return self

    def backend_for(self, role='main'):
        name = self.roles.get(role, self.roles.get('main'))
        if name is None and self.backends:
            name = next(iter(self.backends))
        if name not in self.backends:
            raise ValueError(f'No backend configured for {role}; run tessera init and edit config.toml')
        settings = self.backends[name]
        if not settings.enabled:
            raise ValueError(f'Backend {name} is disabled')
        extra = dict(settings.extra)
        kind = settings.provider_kind or 'openai_chat'
        reference = settings.credential_ref
        if settings.account:
            account = self.accounts[settings.account]
            kind = account.provider_kind
            reference = account.credential_ref
            extra['account'] = account.name
            extra['account_settings'] = asdict(account)
        extra.update(provider_kind=kind, credential_ref=reference, credential_sources=self.credentials,
                     oauth_home=str(self.home))
        from .endpoint_throttle import throttle_limits
        # JSON-safe snapshots. A job hashes this whole backend. The Safe-mode
        # default of one local request is derived from the approval switch, so
        # it must not be in the hash: switching to Unleashed would change it and
        # strand a queued job. A number stored on the model was set by hand
        # and stays. Live chats re-read the effective limit through bind_endpoint.
        requests_per_minute, tokens_per_minute = throttle_limits(self, name)
        extra['endpoint_limit'] = settings.max_concurrent_requests
        extra['endpoint_home'] = str(self.home)
        extra['endpoint_requests_per_minute'] = requests_per_minute
        extra['endpoint_tokens_per_minute'] = tokens_per_minute
        return replace(settings, extra=extra, provider_kind=kind, credential_ref=reference)


def _http_endpoints(values, label, servers=None):
    if not isinstance(values, list):
        raise ValueError(label + ' must be a list of HTTP(S) URLs')
    for endpoint in values:
        if servers is not None and isinstance(endpoint, str) and endpoint.startswith('mcp:'):
            if endpoint[4:] not in servers:
                raise ValueError(f'{label} names unknown mcp_servers entry {endpoint[4:]}')
            continue
        parsed = urlsplit(endpoint) if isinstance(endpoint, str) else None
        if parsed is None or parsed.scheme not in ('http', 'https') or not parsed.netloc or parsed.username is not None:
            raise ValueError(label + ' must be HTTP(S) URLs without credentials')


def validate_companions(companions, servers=None):
    servers = servers if servers is not None else {}
    if not isinstance(companions, CompanionsConfig):
        raise ValueError('Invalid companions configuration')
    atlas, ember = companions.atlas, companions.ember
    _validate_fields(atlas, 'companions.atlas')
    _validate_fields(ember, 'companions.ember')
    _http_endpoints(atlas.endpoints, 'companions.atlas.endpoints', servers)
    _http_endpoints(ember.helmet_endpoints, 'companions.ember.helmet_endpoints', servers)
    _http_endpoints(ember.companion_endpoints, 'companions.ember.companion_endpoints', servers)
    for settings in (atlas, ember):
        if settings.timeout <= 0 or settings.probe_timeout <= 0 or settings.probe_interval < 0:
            raise ValueError('Companion timeouts must be positive and probe_interval nonnegative')
    if (not isinstance(atlas.read_uri_prefixes, list) or any(not isinstance(v, str) or not v for v in atlas.read_uri_prefixes)
            or atlas.max_brief_bytes <= 0 or atlas.max_briefs_per_session < 0):
        raise ValueError('Invalid companion read prefixes or brief bounds')


def validate_inspector(settings):
    _validate_fields(settings, 'inspector')
    if (not settings.socket_path or settings.max_frame_bytes < 1024 or settings.max_note_bytes <= 0
            or settings.max_pending_requests <= 0 or not isinstance(settings.allow_groups, list)
            or any(type(g) is not int or g < 0 for g in settings.allow_groups)):
        raise ValueError('Invalid inspector socket path, bounds or group allowlist')
    return settings


def computer_use_jev_configuration_error(settings):
    """Name the host and model when a Jev call would otherwise have nothing to use."""
    missing = []
    if not str(getattr(settings, 'base_url', '') or '').strip():
        missing.append('computer_use.jev.base_url')
    if not str(getattr(settings, 'model', '') or '').strip():
        missing.append('computer_use.jev.model')
    if not missing:
        return ''
    return ('configuration error: ' + ' and '.join(missing)
            + ' must be set when computer_use.jev is enabled')


def _validate_computer_use_jev(settings):
    _validate_fields(settings, 'computer_use.jev')
    if settings.base_url.strip():
        parts = urlsplit(settings.base_url)
        if parts.scheme not in ('http', 'https') or not parts.hostname or parts.username or parts.password:
            raise ValueError('computer_use.jev.base_url must be an http(s) origin')
    if settings.enabled:
        message = computer_use_jev_configuration_error(settings)
        if message:
            raise ValueError(message)
    if not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', settings.api_key_env):
        raise ValueError('computer_use.jev.api_key_env must name an environment variable')
    if isinstance(settings.timeout, bool) or type(settings.timeout) not in (int, float) or settings.timeout <= 0:
        raise ValueError('computer_use.jev.timeout must be a positive number')
    if (isinstance(settings.min_confidence, bool) or type(settings.min_confidence) not in (int, float)
            or not math.isfinite(settings.min_confidence) or not 0 <= settings.min_confidence <= 1):
        raise ValueError('computer_use.jev.min_confidence must be from 0 to 1')
    if type(settings.max_state_chars) is not int or settings.max_state_chars < 64:
        raise ValueError('computer_use.jev.max_state_chars must be an integer of at least 64')


def _name_list(values, label):
    if not isinstance(values, list):
        raise ValueError(f'{label} must be a list of names')
    seen = set()
    for item in values:
        if not isinstance(item, str) or not item or '\x00' in item or len(item) > 128 or item in seen:
            raise ValueError(f'{label} must be a list of distinct names')
        seen.add(item)


def _skill_directories(values):
    if not isinstance(values, list):
        raise ValueError('skills.extra_dirs must be a list of absolute paths')
    seen = set()
    for item in values:
        if not isinstance(item, str) or not item.strip() or '\x00' in item or len(item) > 1024:
            raise ValueError('skills.extra_dirs must be a list of absolute paths')
        expanded = Path(expand_config_path(item)).expanduser()
        if not expanded.is_absolute():
            raise ValueError('skills.extra_dirs entries must be absolute paths')
        key = str(expanded)
        if key in seen:
            raise ValueError('skills.extra_dirs contains a duplicate path')
        seen.add(key)


def _validate_codex(catalog):
    """The catalog URL and client version are checked when config loads."""
    _validate_fields(catalog, 'codex')
    parsed = urlsplit(catalog.models_url or '')
    if parsed.scheme not in ('http', 'https') or not parsed.netloc or parsed.username is not None or parsed.password:
        raise ValueError('codex.models_url must be an http(s) URL without credentials')
    version = catalog.client_version or ''
    if not isinstance(version, str) or not version or any(char in version for char in ' \t\r\n&#'):
        raise ValueError('codex.client_version must be a version name')


def _validate_fields(settings, section):
    for item in fields(settings):
        value = getattr(settings, item.name)
        valid = True
        if item.type is int:
            valid = type(value) is int
        elif item.type is float:
            valid = type(value) in (int, float) and math.isfinite(value)
        elif item.type in (str, bool, dict):
            valid = isinstance(value, item.type)
        if not valid:
            raise ValueError(f'Invalid {section}.{item.name}: expected {item.type.__name__}')


def _note_retired(config, path, saved):
    config.migration_notes.append(
        '%s is retired (saved value %r). Nothing applies it. '
        'A model\'s cap is max_concurrent_requests.' % (path, saved))


def _blank_as_none(value):
    """An empty env or TOML value is omitted, not a number."""
    if value is None or value == '':
        return None
    return value


def _env_value(raw):
    try:
        return tomllib.loads('value = ' + raw)['value']
    except tomllib.TOMLDecodeError:
        return raw


def expand_config_path(value):
    """Expand a leading ~ and $VAR or ${VAR} when that variable is set.

    An unset variable is left as written so a missing home does not become a
    relative path.
    """
    if not isinstance(value, str) or not value:
        return value

    def replacer(match):
        name = match.group(1) or match.group(2)
        if name in os.environ:
            return os.environ[name]
        return match.group(0)

    return os.path.expanduser(re.sub(r'\$\{([A-Za-z_][A-Za-z0-9_]*)\}|\$([A-Za-z_][A-Za-z0-9_]*)', replacer, value))


def _expand_mcp_server(server):
    server.command = [expand_config_path(part) for part in server.command]
    if server.cwd:
        server.cwd = expand_config_path(server.cwd)
    return server


def load_config(home=None):
    home = Path(home or os.environ.get('TESSERA_HOME', '~/.tessera')).expanduser().resolve()
    path = home / 'config.toml'
    data = {}
    if path.exists():
        with path.open('rb') as stream:
            data = tomllib.load(stream)
    # The server has no authentication. A legacy TESSERA_SERVER_TOKEN is never a config override.
    credential_names = {AetherMindConfig.token_env, JevConfig.api_key_env, 'TESSERA_SERVER_TOKEN'}
    pending = [data]
    while pending:
        section = pending.pop()
        for key, value in section.items():
            if isinstance(value, dict):
                pending.append(value)
            elif key in ('token_env', 'api_key_env', 'env', 'transition_authority_env', 'operator_approval_env') and isinstance(value, str):
                credential_names.add(value)
    for key, raw in os.environ.items():
        if key.startswith('TESSERA_') and key.endswith(('TOKEN_ENV', 'API_KEY_ENV')):
            credential_names.add(str(_env_value(raw)))
    sections = ('inspector', 'aethermind', 'jev', 'view', 'tools', 'agents', 'roles', 'server', 'daemon', 'compactor', 'accounts', 'credentials', 'swarm', 'status', 'channels', 'board', 'hosts', 'systems', 'infrastructure', 'grafana', 'voice', 'autonomy', 'cua', 'computer_use', 'prompt', 'skills', 'codex')
    for key, raw in os.environ.items():
        if not key.startswith('TESSERA_') or key == 'TESSERA_HOME' or key in credential_names:
            continue
        name = key[8:].lower()
        if '__' in name:
            parts = name.split('__')
        else:
            parts = [name]
            for section in sections:
                if name.startswith(section + '_'):
                    parts = [section, name[len(section) + 1:]]
                    break
        target = data
        for part in parts[:-1]:
            target = target.setdefault(part, {})
        target[parts[-1]] = _env_value(raw)
    config = Config(home=home)
    config.config_missing = not path.exists()
    config.migration_notes = []
    for name in ('cwd', 'workspace', 'agent_name', 'operator_name', 'user_instructions',
                 'identity_file', 'identity_max_bytes'):
        if name in data:
            setattr(config, name, data[name])
    if not isinstance(config.cwd, (str, Path)):
        raise ValueError('cwd must be a path string')
    config.cwd = Path(config.cwd).expanduser().resolve()
    if not isinstance(config.workspace, str):
        raise ValueError('workspace must be a path string')
    if config.workspace:
        config.workspace = str(Path(config.workspace).expanduser().resolve())
    config.credentials = CredentialResolver(data.get('credentials', {})).references
    accounts = data.get('accounts', {})
    if not isinstance(accounts, dict):
        raise ValueError('accounts must be named tables')
    for name, values in accounts.items():
        try:
            values = dict(values)
            limits = values.get('limits')
            if isinstance(limits, dict):
                limits = dict(limits)
                for key in ('max_concurrent', 'initial_concurrency'):
                    if key in limits:
                        _note_retired(config, f'accounts.{name}.limits.{key}', limits.pop(key))
                values['limits'] = limits
            config.accounts[name] = AccountConfig(name=name, **values)
        except TypeError:
            raise ValueError('Unknown account options; credentials must use credential_ref') from None
    for name, values in data.get('backends', {}).items():
        if 'api_key' in values:
            raise ValueError('Backend credentials must use api_key_env, not api_key')
        extra = dict(values.get('extra', {}))
        for key, value in values.items():
            if key not in ('base_url', 'model', 'api_key_env', 'extra', 'provider_kind', 'credential_ref', 'account', 'chat',
                           'enabled', 'max_concurrent_requests',
                           'throttle_requests_per_minute', 'throttle_tokens_per_minute'):
                extra[key] = value
        config.backends[name] = BackendConfig(name, values.get('base_url', ''), values.get('model', ''),
                                             values.get('api_key_env', ''), extra,
                                             values.get('provider_kind', ''), values.get('credential_ref', ''),
                                             values.get('account', ''), values.get('chat', True),
                                             enabled=values.get('enabled', True),
                                             max_concurrent_requests=_blank_as_none(values.get('max_concurrent_requests', None)),
                                             throttle_requests_per_minute=_blank_as_none(values.get('throttle_requests_per_minute', None)),
                                             throttle_tokens_per_minute=_blank_as_none(values.get('throttle_tokens_per_minute', None)))
    hosts = data.get('hosts', {})
    if not isinstance(hosts, dict):
        raise ValueError('Hosts must be named tables')
    for name, values in hosts.items():
        try:
            config.hosts[name] = HostConfig(name=name, **values)
        except TypeError:
            raise ValueError('Unknown host options; use configured probe argv and references') from None
    # Purpose-bearing registry: what each system is FOR, and the thresholds the
    # floor should use for it. Validated here so a bad signal is refused at load
    # rather than silently ignored by the floor that reads it.
    systems = data.get('systems', {})
    if not isinstance(systems, dict):
        raise ValueError('Systems must be a table')
    config.systems, _ = load_systems({'systems': systems, 'credentials': data.get('credentials', {})})
    remotes = data.get('remotes', {})
    if not isinstance(remotes, dict):
        raise ValueError('remotes must be named tables')
    for name, values in remotes.items():
        try:
            config.remotes[name] = RemoteConfig(name=name, **values)
        except TypeError:
            raise ValueError('Unknown remote options; use host, port, user, identity_file, known_hosts_file') from None
    servers = data.get('mcp_servers', {})
    if not isinstance(servers, dict):
        raise ValueError('mcp_servers must be named tables')
    for name, values in servers.items():
        try:
            config.mcp_servers[name] = _expand_mcp_server(McpServerConfig(name=name, **values))
        except TypeError:
            raise ValueError('Unknown mcp_servers options; use url or command, env, include, exclude, safe_tools') from None
    config.roles = dict(data.get('roles', {}))
    if isinstance(data.get('server'), dict):
        # Removed with HTTP auth; older homes may still carry it. It has no effect.
        data['server'].pop('token_env', None)
    # Every section the settings page can write has to be readable here, or a save
    # reports "Saved" for a key this loader then discards. `identity` was missing from
    # this tuple while the page already offered `identity.enabled`, so turning identity
    # records on wrote the key and nothing read it: the toggle appeared to work and the
    # records stayed off until someone edited the file by hand and restarted.
    retired = {}
    for name, cls in (('aethermind', AetherMindConfig), ('jev', JevConfig), ('view', ViewConfig), ('tools', ToolsConfig), ('agents', AgentsConfig), ('server', ServerConfig), ('daemon', DaemonConfig), ('compactor', CompactorConfig), ('swarm', SwarmConfig), ('status', StatusConfig), ('channels', ChannelsConfig), ('board', BoardConfig), ('intake', IntakeConfig), ('identity', IdentityConfig), ('infrastructure', InfrastructureConfig), ('grafana', GrafanaConfig), ('voice', VoiceConfig), ('autonomy', AutonomyConfig), ('cua', CuaConfig), ('computer_use', ComputerUseConfig), ('prompt', PromptConfig), ('skills', SkillsConfig), ('ui', UiConfig), ('codex', CodexCatalogConfig)):
        values = dict(data.get(name, {}))
        if name == 'aethermind' and 'enabled' in values:
            # A stored key from an older file or TESSERA_AETHERMIND_ENABLED.
            # Accepted so the file still loads. Ignored so it cannot turn memory off.
            retired['aethermind.enabled'] = values.pop('enabled')
        if name == 'board':
            # Older homes may still set the removed display cap. Serve every card.
            values.pop('card_limit', None)
        if name == 'agents' and 'local_serving_slots' in values:
            saved = values.pop('local_serving_slots')
            config.migration_notes.append(
                'agents.local_serving_slots is retired (saved value %r). '
                'Local agent lanes use each model\'s max_concurrent_requests.' % (saved,))
        if name == 'swarm':
            for key in ('job_workers', 'max_concurrent', 'backend_limits', 'default_backend_limit',
                        'initial_concurrency', 'aimd_successes', 'aimd_add', 'aimd_factor'):
                if key in values:
                    _note_retired(config, 'swarm.' + key, values.pop(key))
        nested_jev = None
        nested_speech = None
        if name == 'computer_use':
            nested_jev = values.pop('jev', {})
            if not isinstance(nested_jev, dict):
                raise ValueError('computer_use.jev must be a table')
        if name == 'voice':
            nested_speech = {'stt': values.pop('stt', {}), 'tts': values.pop('tts', {})}
        allowed = set()
        for item in fields(cls):
            allowed.add(item.name)
        unknown = set(values) - allowed
        if unknown:
            raise ValueError(f'Unknown {name} options: {sorted(unknown)}')
        section = cls(**values)
        if nested_jev is not None:
            unknown_jev = set(nested_jev) - {item.name for item in fields(ComputerUseJevConfig)}
            if unknown_jev:
                raise ValueError(f'Unknown computer_use.jev options: {sorted(unknown_jev)}')
            section.jev = ComputerUseJevConfig(**nested_jev)
        if nested_speech is not None:
            speech_classes = {'stt': SpeechServiceConfig, 'tts': SpeechOutputConfig}
            for key, table in nested_speech.items():
                if not isinstance(table, dict):
                    raise ValueError(f'voice.{key} must be a table')
                speech_class = speech_classes[key]
                allowed_speech = {item.name for item in fields(speech_class)}
                unknown_speech = set(table) - allowed_speech
                if unknown_speech:
                    raise ValueError(f'Unknown voice.{key} options: {sorted(unknown_speech)}')
                setattr(section, key, speech_class(**table))
        setattr(config, name, section)
    config.retired_settings = retired
    config.computer_use.helper = expand_config_path(config.computer_use.helper)
    defaults = ToolsConfig()
    for key in ('approval_classes', 'approval_rules'):  # entries in config add to or override the defaults
        if isinstance(getattr(config.tools, key), dict):
            setattr(config.tools, key, {**getattr(defaults, key), **getattr(config.tools, key)})
    companions = data.get('companions', {})
    if not isinstance(companions, dict) or set(companions) - {'atlas', 'ember'}:
        raise ValueError('Unknown companions; only atlas and ember are configurable')
    for name, cls in (('atlas', AtlasConfig), ('ember', EmberConfig)):
        values = companions.get(name, {})
        unknown = set(values) - {item.name for item in fields(cls)} if isinstance(values, dict) else {'?'}
        if unknown:
            raise ValueError(f'Unknown companions.{name} options: {sorted(unknown)}')
        setattr(config.companions, name, cls(**values))
    try:
        values = data.get('inspector', {})
        config.inspector = validate_inspector(InspectorConfig(**values))
    except (TypeError, ValueError) as error:
        # Invalid inspector settings skip the socket; they never refuse daemon start.
        config.inspector = InspectorConfig(enabled=False)
        config.inspector_error = type(error).__name__
    hooks = data.get('hooks', {})
    config.hooks = hooks.get('shell', []) if isinstance(hooks, dict) else hooks
    for note in config.migration_notes:
        _log.warning('%s', note)
    # Bundled engine discovery is a signal, not a spawn.
    # Config load must stay pure (no processes, no ports). The launcher in
    # tessera/bundled_engine.py provisions and starts the packaged engine when
    # a session actually needs memory and nothing else is configured; it holds
    # the URL in a process-wide registry. Validation of mcp: references above
    # covers genuinely configured stdio servers.
    return config.validate()
