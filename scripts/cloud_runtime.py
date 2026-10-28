#!/usr/bin/env python3
"""Throwaway offline Tessera core for a cloud-agent VM.

Creates a home only when needed, never deletes sessions, and never uses
~/.tessera. Model traffic stays on loopback and uses tests/fake_backend.py.
"""

import argparse
import fcntl
import ipaddress
import json
import os
import signal
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
HEALTH_BODY = b"tessera-offline-model"


def loopback(host):
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def positive_port(raw, label):
    try:
        value = int(raw)
    except (TypeError, ValueError):
        raise SystemExit(f"{label} must be an integer port.") from None
    if not 0 < value <= 65535:
        raise SystemExit(f"{label} must be a TCP port from 1 to 65535.")
    return value


def truthy(raw):
    return str(raw).strip().lower() not in {"", "0", "false", "no", "off"}


def cloud_home():
    raw = os.environ.get("TESSERA_CLOUD_HOME")
    if raw:
        home = Path(raw).expanduser().resolve()
    else:
        base = Path(os.environ.get("TMPDIR", "/tmp")).expanduser().resolve()
        home = base / "tessera-cloud-home"
    forbidden = Path("~/.tessera").expanduser().resolve()
    if home == forbidden:
        raise SystemExit(
            "Refusing to use the default Tessera home (~/.tessera). "
            "Set TESSERA_CLOUD_HOME to a throwaway directory on this VM."
        )
    if home == ROOT or ROOT in home.parents:
        raise SystemExit("TESSERA_CLOUD_HOME must be outside the checkout.")
    return home


def python():
    candidate = ROOT / ".venv" / "bin" / "python"
    if not candidate.is_file():
        raise SystemExit("Missing .venv. Run: bash scripts/cloud-agent-install.sh")
    return str(candidate)


def state_dir(home):
    path = home / "run"
    path.mkdir(parents=True, exist_ok=True)
    return path


def ensure_config(home):
    home.mkdir(parents=True, exist_ok=True)
    os.chmod(home, 0o700)
    target = home / "config.toml"
    if target.exists():
        return
    source = ROOT / "demo" / "fake_config.toml"
    target.write_bytes(source.read_bytes())
    os.chmod(target, 0o600)


def read_pid(path):
    try:
        return int(path.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None


def alive(pid):
    if not pid:
        return False
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def cmdline(pid):
    try:
        raw = Path(f"/proc/{pid}/cmdline").read_bytes()
    except OSError:
        return ""
    return raw.replace(b"\x00", b" ").decode("utf-8", "replace")


def owned(pid, marker):
    return alive(pid) and marker in cmdline(pid)


def write_pid(path, pid):
    path.write_text(str(pid) + "\n", encoding="utf-8")


def http_get(url, timeout=2):
    request = urllib.request.Request(url, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as error:
        return error.code, error.read()
    except (urllib.error.URLError, TimeoutError, OSError):
        return None, b""


def model_up(host, port):
    status, body = http_get(origin(host, port) + "/health")
    return status == 200 and body == HEALTH_BODY


def core_up(host, port):
    status, body = http_get(origin(host, port) + "/api/status")
    if status != 200:
        return False
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return False
    return isinstance(payload, dict) and "ui_build" in payload and "aethermind" in payload


def origin(host, port):
    if ":" in host and not host.startswith("["):
        return f"http://[{host}]:{port}"
    return f"http://{host}:{port}"


def tail(path, limit=40):
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return ""
    return "\n".join(lines[-limit:])


def child_env(home, host, port, model_url, cwd, offline):
    env = {key: value for key, value in os.environ.items() if not key.startswith("TESSERA_")}
    env.update({
        "TESSERA_HOME": str(home),
        "TESSERA_CWD": str(cwd),
        "TESSERA_SERVER_HOST": host,
        "TESSERA_SERVER_PORT": str(port),
    })
    if offline:
        model = os.environ.get("TESSERA_FAKE_MODEL", "scripted-local-fake")
        env.update({
            "TESSERA_ROLES_MAIN": "fake",
            "TESSERA_ROLES_FAST": "fake",
            "TESSERA_ROLES_COMPACTOR": "fake",
            "TESSERA_BACKENDS__FAKE__BASE_URL": model_url,
            "TESSERA_BACKENDS__FAKE__MODEL": model,
            "TESSERA_BACKENDS__FAKE__TIMEOUT": "30",
            "TESSERA_BACKENDS__FAKE__RETRIES": "0",
            "TESSERA_BACKENDS__FAKE__STREAM": "false",
            # Memory has no off switch. This image has no build receipt, so a turn
            # refuses until an engine is actually available.
            "TESSERA_JEV_ENABLED": "false",
            "TESSERA_VOICE_ENABLED": "false",
            "TESSERA_SWARM_ENABLED": "false",
        })
    return env


def spawn(argv, env, log_path, cwd):
    log = open(log_path, "ab", buffering=0)
    try:
        proc = subprocess.Popen(
            argv,
            cwd=str(cwd),
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    finally:
        log.close()
    return proc


def exclusive(home):
    handle = open(state_dir(home) / "lock", "a", encoding="utf-8")
    fcntl.flock(handle, fcntl.LOCK_EX)
    return handle


def stop_pid(path, marker, sig):
    pid = read_pid(path)
    if pid is None:
        return
    if not owned(pid, marker):
        if not alive(pid):
            path.unlink(missing_ok=True)
        return
    os.kill(pid, sig)
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline and alive(pid):
        time.sleep(0.1)
    if alive(pid):
        os.kill(pid, signal.SIGKILL)
        time.sleep(0.2)
    path.unlink(missing_ok=True)


def settings_from_env():
    host = os.environ.get("TESSERA_BIND_HOST", "127.0.0.1")
    if not loopback(host):
        raise SystemExit("TESSERA_BIND_HOST must be a loopback address. The core refuses anything else.")
    port = positive_port(os.environ.get("TESSERA_PORT", "8740"), "TESSERA_PORT")
    fake_port = positive_port(os.environ.get("TESSERA_FAKE_MODEL_PORT", "8761"), "TESSERA_FAKE_MODEL_PORT")
    if fake_port == port:
        raise SystemExit("TESSERA_FAKE_MODEL_PORT must differ from TESSERA_PORT.")
    home = cloud_home()
    cwd = Path(os.environ.get("TESSERA_CLOUD_CWD", str(ROOT))).expanduser().resolve()
    offline = truthy(os.environ.get("TESSERA_CLOUD_OFFLINE", "1"))
    return host, port, fake_port, home, cwd, offline


def publish(home, host, port):
    url = origin(host, port)
    state = state_dir(home)
    (state / "url").write_text(url + "\n", encoding="utf-8")
    print(f"Tessera cloud core ready at {url}")
    print(f"home: {home}")
    print(f"url file: {state / 'url'}")
    return url


def start(foreground):
    host, port, fake_port, home, cwd, offline = settings_from_env()
    lock = exclusive(home)
    try:
        return _start(foreground, host, port, fake_port, home, cwd, offline, lock)
    finally:
        if not foreground:
            lock.close()


def _start(foreground, host, port, fake_port, home, cwd, offline, lock):
    ensure_config(home)
    state = state_dir(home)
    interpreter = python()
    model_url = origin(host, fake_port) + "/v1"
    core_ready = core_up(host, port)
    model_ready = (not offline) or model_up(host, fake_port)

    if offline and not model_ready:
        if owned(read_pid(state / "model.pid"), "offline_model.py"):
            raise SystemExit("Recorded offline model is alive but /health did not answer. Run stop, then start.")
        proc = spawn(
            [interpreter, str(ROOT / "scripts" / "offline_model.py"), "--host", host, "--port", str(fake_port)],
            os.environ.copy(),
            state / "model.log",
            ROOT,
        )
        write_pid(state / "model.pid", proc.pid)
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline and not model_up(host, fake_port):
            if proc.poll() is not None:
                print(tail(state / "model.log"), flush=True)
                raise SystemExit("Offline model exited before it became ready.")
            time.sleep(0.1)
        if not model_up(host, fake_port):
            print(tail(state / "model.log"), flush=True)
            raise SystemExit("Offline model did not become ready.")

    if not core_ready:
        if owned(read_pid(state / "core.pid"), "tessera"):
            raise SystemExit("Recorded core is alive but /api/status did not answer. Run stop, then start.")
        env = child_env(home, host, port, model_url, cwd, offline)
        proc = spawn(
            [interpreter, "-m", "tessera", "--home", str(home), "serve", "--host", host, "--port", str(port)],
            env,
            state / "core.log",
            ROOT,
        )
        write_pid(state / "core.pid", proc.pid)
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline and not core_up(host, port):
            if proc.poll() is not None:
                print(tail(state / "core.log"), flush=True)
                raise SystemExit("Tessera core exited before /api/status answered.")
            time.sleep(0.1)
        if not core_up(host, port):
            print(tail(state / "core.log"), flush=True)
            raise SystemExit("Tessera core did not become ready.")

    publish(home, host, port)
    lock.close()
    if not offline:
        print("Offline defaults are off. This script did not start the fake model or inject backend overrides.")
    if not foreground:
        return 0
    pid = read_pid(state / "core.pid")
    if not owned(pid, "tessera"):
        print("Core is already running; this process will not take over its lifetime.")
        return 0

    def shutdown(signum, frame):
        stop_pid(state / "core.pid", "tessera", signal.SIGINT)
        stop_pid(state / "model.pid", "offline_model.py", signal.SIGTERM)
        raise SystemExit(0)

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)
    while owned(pid, "tessera"):
        time.sleep(1)
    return 0


def stop():
    home = cloud_home()
    state = home / "run"
    if not state.is_dir():
        print(f"No cloud core state at {home}. Session files were not touched.")
        return 0
    lock = exclusive(home)
    try:
        # SIGINT exits the core without the restart drain used by the Mac supervisor.
        stop_pid(state / "core.pid", "tessera", signal.SIGINT)
        stop_pid(state / "model.pid", "offline_model.py", signal.SIGTERM)
        print(f"Stopped cloud core processes for {home}. Session files were left in place.")
        return 0
    finally:
        lock.close()


def status():
    host, port, fake_port, home, cwd, offline = settings_from_env()
    url = origin(host, port)
    ready = core_up(host, port)
    print(f"url: {url}")
    print(f"home: {home}")
    print(f"cwd: {cwd}")
    print(f"offline: {str(offline).lower()}")
    print(f"core: {'up' if ready else 'down'}")
    if offline:
        print(f"fake model: {'up' if model_up(host, fake_port) else 'down'}")
    if ready:
        publish(home, host, port)
        return 0
    return 1


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", nargs="?", choices=("start", "stop", "status"), default="start")
    parser.add_argument("--foreground", action="store_true", help="Stay attached to the core until it exits")
    args = parser.parse_args(argv)
    if args.command == "stop":
        return stop()
    if args.command == "status":
        return status()
    return start(args.foreground)


if __name__ == "__main__":
    raise SystemExit(main())
