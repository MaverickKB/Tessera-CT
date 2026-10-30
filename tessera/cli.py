"""Harness CLI. Status and zoom do not need a configured model backend."""

import argparse
from dataclasses import replace
import json
import os
from pathlib import Path
import sys
import socket
from urllib.parse import urlsplit

from .config import PLACEHOLDER_PROJECTS, load_config
from .assets import asset_path


# Hosts the example config ships that can never answer. Kept as a literal list rather
# than a heuristic so the message names the exact line a person has to change.
PLACEHOLDER_HOSTS = ('models.example.com', 'api.example.com')
# PLACEHOLDER_PROJECTS comes from the loader, which is the one place that decides
# whether a project value is a store address or still the example's text.


def _healed_project_value(home):
    """The default project store start writes when the placeholder is in place."""
    return str(Path(home).expanduser() / 'projects' / 'default')


def _healed_personal_value(home):
    """The default personal store start writes when the placeholder is in place."""
    return str(Path(home).expanduser() / 'projects' / 'personal')


def _is_placeholder_project(value):
    """Whether a loaded project value still names the example's placeholder.

    The loader resolves every non-empty project to an absolute path, so the
    placeholder is recognized by its last component rather than by the whole
    string.
    """
    text = str(value or '').strip()
    if not text:
        return False
    return Path(text).name in PLACEHOLDER_PROJECTS


def _placeholder_memory_projects(config):
    """The aethermind keys start must replace with a real store.

    The example's placeholder names are one case. An empty project is the other:
    an empty name addresses no store at all, so the first session would open
    memoryless and stay that way. Both heal to a store under this home.
    """
    memory = config.aethermind
    keys = []
    project = (getattr(memory, 'project', '') or '').strip()
    if not project or _is_placeholder_project(project):
        keys.append(('project', _healed_project_value))
    if _is_placeholder_project(getattr(memory, 'personal_project', '')):
        keys.append(('personal_project', _healed_personal_value))
    return keys


def _placeholder_memory_project(config):
    """Name the example's memory project when it is still in place.

    An empty project is a legal configuration (no project store); only the
    example's placeholder names are called out, because a session that keeps
    one addresses a store that does not exist.
    """
    configured = (getattr(config.aethermind, 'project', '') or '').strip()
    if _is_placeholder_project(configured):
        return ('[aethermind] project is still the example placeholder '
                f'{Path(configured).name!r}, which resolves to {configured!r}. '
                'Set it to a real project path (for example a directory under your '
                'Tessera home) in config.toml or Settings before the first turn; '
                'the session addresses the store that name resolves to.')
    return None


def _unusable_backends(config, *, resolve_hosts=False):
    """Why this install cannot produce a turn, in the order a person should fix it.

    Never sends a model request or spends a token. With resolve_hosts enabled,
    the doctor also checks DNS; template creation stays offline. It reports
    failures a fresh install actually hits: a placeholder endpoint or a credential
    variable that is not set -- and says nothing about roles that resolve fine.
    """
    gaps = []
    roles = getattr(config, 'roles', {}) or {}
    checked = set()
    for role in ('main', 'fast', 'compactor'):
        name = roles.get(role)
        if not name or name in checked:
            continue
        checked.add(name)
        backend = (config.backends or {}).get(name)
        if backend is None:
            gaps.append(f'roles.{role} names backend {name!r}, which is not declared.')
            continue
        url = str(getattr(backend, 'base_url', '') or '')
        host = url.split('//', 1)[-1].split('/', 1)[0]
        if any(placeholder in host for placeholder in PLACEHOLDER_HOSTS):
            gaps.append(f'[backends.{name}] base_url is still the placeholder {url!r}; '
                        f'point it at the endpoint you will actually use.')
        if resolve_hosts and url:
            parsed = urlsplit(url)
            try:
                socket.getaddrinfo(parsed.hostname, parsed.port or (443 if parsed.scheme == 'https' else 80), type=socket.SOCK_STREAM)
            except (OSError, ValueError) as error:
                gaps.append(f'[backends.{name}] cannot resolve endpoint host {parsed.hostname!r}: {error}')
        env_name = getattr(backend, 'api_key_env', '') or ''
        credential = getattr(backend, 'credential_ref', '') or ''
        if env_name and not credential and not os.environ.get(env_name):
            gaps.append(f'[backends.{name}] needs {env_name} in the environment, '
                        f'and it is not set. Export it, or name a [credentials] table '
                        f'with credential_ref.')
    return gaps


def _missing_memory_engine(config):
    """Explain when this install has neither an external service nor a bundle."""
    memory = config.aethermind
    if memory.endpoints or memory.url:
        return None
    # Use the engine's own packaged/explicit path resolution so the doctor
    # reports the same missing-bundle condition as runtime startup.
    from .bundled_engine import _engine_root
    if _engine_root(config) is not None:
        return None
    from .engine_manifest import vendored_status
    status = vendored_status()
    if status == 'match':
        # The checkout holds the vendored bytes. The wheel build writes the
        # receipt that startup looks for; their presence is not a missing engine.
        return None
    if status == 'mismatch':
        return ('The vendored AetherMind engine does not match '
                'docs/packaging/aethermind-engine-manifest.json. '
                'A release wheel refuses to build until that tree matches. '
                'TESSERA_BUNDLE_SOURCE is an optional override of the same bytes, '
                'not a way to skip the check.')
    return ('This install has no memory engine bundled. Configure an AetherMind '
            'endpoint or install a release wheel, which vendors the embedded engine.')


def _unread_settings(config, approval):
    """Doctor mode's second half: a SET value nothing reads is a lie on the page.

    The consumer map (tessera/settings_consumers.py) names the reader of every
    field the Settings page serves, or says NO-READER. Only a config that
    actually sets a NO-READER field is a finding: a default value nobody
    changed is not the operator's mistake.
    """
    from . import settings_consumers
    mapping = settings_consumers.annotate_counts(settings_consumers.build_map(config, approval))
    findings = []
    for field_id in settings_consumers.unread_set_fields(mapping):
        # A record container with no config path is page furniture, not a set
        # value. A scalar NO-READER field is compared against the config's own
        # attributes, including nested paths.
        target = config
        known = True
        for part in field_id.split('.'):
            if not hasattr(target, part):
                known = False
                break
            target = getattr(target, part)
        if known:
            findings.append(f'[{field_id}] is set but no code reads it; '
                            'it changes nothing. Remove it or wire a consumer.')
    return findings


def _approve(name, arguments):
    if not sys.stdin.isatty():
        return False
    print(f'Approve {name}: {json.dumps(arguments, ensure_ascii=False)}', file=sys.stderr)
    try:
        return input('Allow? [y/N] ').strip().lower() in ('y', 'yes')
    except (EOFError, KeyboardInterrupt):
        return False


def _inspect(config, args):
    from .capture import Capture
    from .log import ChatLog
    from .tree import SummaryTree
    from .view import View
    root = config.home / 'chat'
    with ChatLog(root / 'log') as log:
        tree = SummaryTree(root, log, node_bytes=config.view.node_bytes)
        view = View(root, tree, high_bytes=config.view.high_bytes, low_bytes=config.view.low_bytes)
        if args.command == 'zoom':
            if '+' in args.id:
                start, count = args.id.split('+', 1)
                start, count = int(start), int(count)
            else:
                start, count = int(args.id), args.count
            print(view.zoom(start, count))
            return
        memory = config.aethermind
        capture = Capture(None, root / 'outbox', memory.project, memory.personal_project, memory.author)
        print(json.dumps({'backends': list(config.backends), 'roles': config.roles,
                          'messages': log.count(), 'view_bytes': len(view.render().encode('utf-8')),
                          'write_debt': len(capture.debt())}, indent=2))


def _start(config, args):
    """One command for a working app: create what is missing, then serve and open.

    init and seed are setup steps a person has to know about and run in order.
    This composes them: a missing config is created, an empty store is seeded,
    the OS launcher is written when asked, and then the core runs with the UI
    opened in a browser. The first turn of a fresh install happens in a window,
    not in a terminal.
    """
    from .bundled_engine import ensure_local_engine_bundle
    from . import seeds as seed_pack

    home = config.home
    target = home / 'config.toml'
    first_run = not target.exists()
    if first_run:
        from .assets import asset_path
        home.mkdir(parents=True, exist_ok=True)
        with target.open('x', encoding='utf-8') as stream:
            stream.write(asset_path('config.example.toml').read_text(encoding='utf-8'))
        print(f'Created {target}.')
        # The config main() loaded predates the file, so its backends and
        # memory project are defaults. Reload so the first core runs with
        # the settings the new file defines, not empty ones.
        config = load_config(home)
    # Self-heal the memory projects: a placeholder project names a store that
    # does not exist, and the session would address nothing. On first run the
    # fix is part of creation and stays quiet; later runs say it out loud so
    # the reset is visible and can be looked into.
    placeholders = _placeholder_memory_projects(config)
    if placeholders:
        edits = [{'path': ['aethermind', key], 'value': heal(home)} for key, heal in placeholders]
        from .config_editor import ConfigEditor
        ConfigEditor(target).apply(edits, label='start first run' if first_run else 'start self-heal')
        config = load_config(home)
        if not first_run:
            names = ', '.join(key for key, _ in placeholders)
            healed = ', '.join(str(edit['value']) for edit in edits)
            print(f'start: [aethermind] {names} named no usable store yet; '
                  f'reset to {healed}. A config backup was written beside config.toml.')
    ensure_local_engine_bundle(config)
    # Seed the store the session will address, before the core answers anything.
    # AetherMind never comes up empty: an install whose store cannot take the
    # pack refuses here, by name, instead of serving a session with no rules.
    try:
        result = seed_pack.ensure(config)
    except RuntimeError as error:
        store = seed_pack.store_path(config)
        print(f'tessera: the memory store {store} could not be seeded: {error}', file=sys.stderr)
        print('tessera: not serving, because an empty store would answer every '
              'memory read with nothing.', file=sys.stderr)
        print('tessera: ' + seed_pack.RECOVERY_HINT, file=sys.stderr)
        return 1
    if result.get('unavailable'):
        # Native Windows refuses every store write. Say so, in the sentence the
        # rest of the product uses, rather than starting quietly with no seeds.
        print('tessera: the seed pack was not written: ' + seed_pack.lock_refusal_sentence(),
              file=sys.stderr)
    elif result.get('layers_written') or result.get('skills_staged') or result.get('quarantined'):
        print(seed_pack.describe(result))
    if getattr(args, 'install_launcher', False):
        from .launcher import install as install_launcher
        written = install_launcher(home)
        print(f"Launcher written: {written['entry']}")
    from .server import create_server
    from . import updates
    origin = updates.install_report()
    if origin:
        print(f'Tessera from {origin["url"]} at {origin["commit"][:10]}', flush=True)
    # start owns its process, so an update restart respawns here rather than
    # waiting on a supervisor. SIGTERM drains and exits. An update restart
    # execs a fresh interpreter with the bound port on the command line,
    # because the new process reads config from disk and would otherwise pick
    # a new port; the page only reconnects to the origin it came from.
    if getattr(args, 'port', None) is not None:
        config.server.port = args.port
    config.server.port = config.server.port or 0
    first_iteration = [True]
    while True:
        server = create_server(config)
        server.self_supervised = True
        config.server.port = server.server_address[1]
        if first_iteration[0] and not getattr(args, 'no_browser', False):
            first_iteration[0] = False
            # The page must not open before the core answers. Probe readiness from
            # a thread against the bound address, open once. An update restart
            # execs a fresh interpreter, so later iterations do not reopen it.
            import threading
            import time
            import urllib.request
            import webbrowser
            url = server.url

            def _open_when_ready(url=url):
                deadline = time.monotonic() + 30
                while time.monotonic() < deadline:
                    try:
                        urllib.request.urlopen(url.rstrip('/') + '/api/status?shape=connection', timeout=1)
                        webbrowser.open(url)
                        return
                    except OSError:
                        time.sleep(0.2)

            threading.Thread(target=_open_when_ready, daemon=True).start()
        print(f'Serving Tessera at {server.url}', flush=True)
        import signal
        terminating = []

        def _terminate(*_args):
            terminating.append(True)
            server.restart()

        signal.signal(signal.SIGTERM, _terminate)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            server.server_close()
            server.manager.close()
            from .server import _release_embedded_engine
            _release_embedded_engine(config)
            return 0
        # serve_forever returns after a drain. A termination exits; an update
        # restart execs a fresh interpreter, because the already-imported
        # modules are the old installation's and would keep serving old code
        # behind the new static assets. execv replaces this process image with
        # a new Python at the same address, holding the listener gap shut.
        server.server_close()
        server.manager.close()
        from .server import _release_embedded_engine
        _release_embedded_engine(config)
        if terminating:
            return 0
        restart_argv = [sys.executable, '-m', 'tessera', '--home', str(home),
                        'start', '--no-browser',
                        '--port', str(server.server_address[1])]
        print('Restarting with the updated install…', flush=True)
        os.execv(sys.executable, restart_argv)
    return 0


def _named_chat(config, name):
    from .sessions import SessionManager
    with SessionManager(config, approve=_approve) as manager:
        selected = None
        for metadata in manager.list_sessions():
            if metadata['slug'] == name or metadata['title'] == name:
                selected = metadata
                break
        if selected is None:
            selected = manager.create(name)
        slug = selected['slug']
        session = manager.get(slug)
        print(f'Tessera session: {slug}. Type /quit or press Ctrl-D to exit.')
        while True:
            try:
                text = input('> ')
            except EOFError:
                break
            if text.strip() in ('/quit', '/exit'):
                break
            if not text.strip():
                continue
            position = session.log.count()
            turn_id = manager.submit(slug, text)
            manager.wait(slug)
            reply = ''
            reports = []
            for message in session.log.iter(position):
                if message.kind == 'agent':
                    reply = message.text
                elif message.kind == 'work':
                    reports.append(message.text)
            if reply:
                print(reply)
            for report in reports:
                print(report)
            outcome = manager.turn(slug, turn_id)
            if outcome['state'] == 'error':
                print(outcome['error'], file=sys.stderr)
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(prog='tessera', description='A small generic agent harness.')
    parser.add_argument('--home', type=Path, help='Config and chat directory (default: TESSERA_HOME or ~/.tessera)')
    commands = parser.add_subparsers(dest='command', required=True)
    init_parser = commands.add_parser('init', help='Create a placeholder configuration without overwriting')
    init_parser.add_argument('--check', action='store_true',
                             help='Report whether the current config can answer, without writing or calling out')
    start_parser = commands.add_parser('start',
                                       help='Set up what is missing, then run the desktop core and open the UI')
    start_parser.add_argument('--no-browser', action='store_true',
                              help='Start the core without opening the browser')
    start_parser.add_argument('--port', type=int,
                              help='Serve on this port; 0 selects a free port. An update restart passes the bound port so the page reconnects to the same origin.')
    start_parser.add_argument('--install-launcher', action='store_true',
                              help='Also write the OS launcher (app menu, start menu, Applications)')
    launcher_parser = commands.add_parser('install-launcher',
                                           help='Write the OS launcher for `tessera start` (app menu, start menu, Applications)')
    seeds = commands.add_parser('seed', help='Write the packaged seed pack into an AetherMind store')
    seeds.add_argument('--store', type=Path,
                       help='AetherMind project root to write into (default: the configured memory project)')
    seeds.add_argument('--skills-dir', type=Path,
                       help='Skill shelf to stage into (default: <home>/skills, or [skills] shelf)')
    seeds.add_argument('--dry-run', action='store_true',
                       help='Report what would be written without writing it')
    commands.add_parser('app-server', help='Run the headless NDJSON stdio daemon')
    chat = commands.add_parser('chat', help='Chat interactively')
    chat.add_argument('--session', help='Resume a named subject or create it if missing')
    for name, description in (
        ('serve', 'Run the loopback desktop core in the foreground'),
        ('desktop', 'Run an app-owned core with JSON readiness and stdin lifetime'),
    ):
        server = commands.add_parser(name, help=description)
        server.add_argument('--host', help='Override server.host; loopback only')
        server.add_argument('--port', type=int, help='Override server.port; 0 selects a free port')
    run = commands.add_parser('run', help='Run one turn, then wait for agent reports')
    run.add_argument('message')
    commands.add_parser('status', help='Show backends, view bytes, and pending capture count')
    job_create = commands.add_parser('swarm-create', help='Save an explicit bounded swarm job without executing it')
    job_create.add_argument('spec', type=Path, help='JSON object with objective and workers')
    job_create.add_argument('--session', required=True, help='Existing saved session slug')
    job_run = commands.add_parser('swarm-run', help='Run one bounded wave of an existing swarm job')
    job_run.add_argument('job_id')
    job_show = commands.add_parser('swarm-show', help='Inspect saved job counts without executing providers')
    job_show.add_argument('job_id')
    zoom = commands.add_parser('zoom', help='Expand a start/count range or start+count ID')
    zoom.add_argument('id')
    zoom.add_argument('count', type=int, nargs='?', default=1)
    from .inspector_client import add_parsers
    add_parsers(commands)
    args = parser.parse_args(argv)
    try:
        config = load_config(args.home)
        if args.command in ('inspect', 'inspect-mcp'):
            from .inspector_client import InspectorError, run_cli, serve_mcp
            if args.command == 'inspect-mcp':
                return serve_mcp(config, args.attach)
            try:
                print(json.dumps(run_cli(config, args), ensure_ascii=False, indent=2))
            except InspectorError as error:
                print(f'tessera inspect: {error} ({error.code})', file=sys.stderr)
                return 1
            return 0
        if args.command == 'app-server':
            from .app_server import run
            run(config)
            return 0
        if args.command.startswith('swarm-'):
            from .jobs import SwarmJobs
            if args.command == 'swarm-show':
                job = SwarmJobs(config).get(args.job_id)
            else:
                from .sessions import SessionManager
                job_config = replace(config, server=replace(config.server, resume_turns=False))
                with SessionManager(job_config) as manager:
                    if args.command == 'swarm-create':
                        spec = json.loads(args.spec.read_text(encoding='utf-8'))
                        if not isinstance(spec, dict) or set(spec) != {'objective', 'workers'}:
                            raise ValueError('Swarm spec requires objective and workers only')
                        job = manager.create_swarm_job(args.session, spec['objective'], spec['workers'])
                    else:
                        job = manager.run_swarm_job(args.job_id)
            print(json.dumps({'job_id': job['id'], 'status': job['status'], 'worker_count': len(job['workers']),
                              'artifact_count': len(job['artifacts']), 'merged': job['merged'] is not None}))
            return 0 if job['status'] != 'failed' else 1
        if args.command == 'init':
            config.home.mkdir(parents=True, exist_ok=True)
            target = config.home / 'config.toml'
            if getattr(args, 'check', False):
                # A doctor, not a writer: say what a turn would need and what is missing.
                if not target.exists():
                    print(f'No config at {target}. Run `tessera init` first.', file=sys.stderr)
                    return 1
                loaded = load_config(config.home)
                gaps = _unusable_backends(loaded, resolve_hosts=True)
                gaps.extend(_unread_settings(loaded, {'yolo': False}))
                # The same absence init names on creation is the one a person will
                # come back and look for. --check is where they look, so say it here
                # too, and say it as a gap so the exit code matches the report:
                # a config that resolves every backend yet has no memory is not a
                # complete install, and --check should not call it one.
                memory_gap = _missing_memory_engine(loaded)
                if memory_gap:
                    gaps.append(memory_gap)
                placeholder = _placeholder_memory_project(loaded)
                if placeholder:
                    gaps.append(placeholder)
                if not gaps:
                    print('Configuration can answer: every role resolves to a backend with a usable credential.')
                    return 0
                for line in gaps:
                    print(line, file=sys.stderr)
                return 1
            example = asset_path('config.example.toml').read_text(encoding='utf-8')
            with target.open('x', encoding='utf-8') as stream:
                stream.write(example)
            # Flush before the stderr block: stdout is block-buffered when piped, and a
            # person redirecting both streams should read "Created" first, then the gaps.
            print(f'Created {target}.', flush=True)
            # A source checkout has the engine bytes but no wheel receipt. A
            # home-local verified bundle makes the embedded engine start for a
            # normal install, not only for a release wheel.
            from .bundled_engine import ensure_local_engine_bundle
            ensure_local_engine_bundle(load_config(config.home))
            # Writing the file is not the same as having a working install. The example
            # ships a placeholder endpoint and an unset credential, and until now init
            # reported plain success on an install that could not produce a single turn:
            # the first failure appeared at use time, reading as a broken product rather
            # than a setup step. Say what is missing here, where the person is looking.
            gaps = _unusable_backends(load_config(config.home))
            if gaps:
                print('This install cannot answer yet. To make it work:', file=sys.stderr)
                for line in gaps:
                    print(f'  {line}', file=sys.stderr)
            memory_gap = _missing_memory_engine(load_config(config.home))
            if memory_gap:
                print(memory_gap, file=sys.stderr)
            placeholder = _placeholder_memory_project(load_config(config.home))
            if placeholder:
                print(placeholder, file=sys.stderr)
            return 0
        if args.command == 'seed':
            from . import seeds as seed_pack
            # The store and shelf resolve through the same helpers the boot
            # path uses, so this command cannot seed a sibling of the store a
            # session addresses. A store that does not exist yet is created,
            # because a fresh install is exactly the case it exists for.
            store = args.store if args.store is not None else seed_pack.store_path(config)
            shelf = args.skills_dir if args.skills_dir is not None else seed_pack.shelf_path(config)
            result = seed_pack.install(store, skills_dir=shelf, dry_run=args.dry_run)
            print(seed_pack.describe(result))
            return 0
        if args.command == 'start':
            return _start(config, args)
        if args.command == 'install-launcher':
            from .launcher import install as install_launcher
            written = install_launcher(config.home)
            print(f"Launcher written: {written['entry']}")
            if written.get('icon'):
                print(f"Icon: {written['icon']}")
            return 0
        if args.command in ('serve', 'desktop'):
            if args.host is not None:
                config.server.host = args.host
            if args.port is not None:
                config.server.port = args.port
            config.validate()
            if args.command == 'desktop':
                from .desktop import run
                run(config)
            else:
                # `serve` is the operator-typed path, so a missing config.toml is
                # named once on stderr. A missing file would otherwise look like a
                # healthy core on defaults: approval 'ask', no model, and an empty
                # sessions dir. `desktop` stays silent on purpose: its home comes
                # from the app, and its ready record plus an empty stderr are the
                # contract the desktop shell parses.
                if config.config_missing:
                    print(f'tessera: no config.toml in {config.home}; running on defaults. '
                          f'Check --home or run `tessera init` first.', file=sys.stderr, flush=True)
                from .server import serve
                serve(config)
            return 0
        if args.command == 'chat' and args.session:
            return _named_chat(config, args.session)
        if args.command in ('status', 'zoom'):
            _inspect(config, args)
            return 0
        from .bundled_engine import AetherMindUnavailable
        from .loop import Session
        with Session(config, approve=_approve) as session:
            if args.command == 'run':
                try:
                    print(session.run_turn(args.message))
                except AetherMindUnavailable as failure:
                    print('tessera: ' + str(failure), file=sys.stderr)
                    return 1
                session.agents.wait()
                for message in session.log.iter():
                    if message.kind == 'work' and message.meta.get('session_id', session.session_id) == session.session_id:
                        print(message.text)
                return 0
            print('Tessera chat. Type /quit or press Ctrl-D to exit.')
            position = session.log.count()
            while True:
                for message in session.log.iter(position):
                    if message.kind == 'work':
                        print(message.text)
                position = session.log.count()
                try:
                    text = input('> ')
                except EOFError:
                    break
                if text.strip() in ('/quit', '/exit'):
                    break
                if text.strip():
                    try:
                        print(session.run_turn(text))
                    except AetherMindUnavailable as failure:
                        print('tessera: ' + str(failure), file=sys.stderr)
                        return 1
        return 0
    except PermissionError as exc:
        print(f'tessera: {exc}', file=sys.stderr)
        return 1
    except (ValueError, OSError, RuntimeError, IndexError) as exc:
        print(f'tessera: {exc}', file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print('\ntessera: interrupted', file=sys.stderr)
        return 130


if __name__ == '__main__':
    raise SystemExit(main())
