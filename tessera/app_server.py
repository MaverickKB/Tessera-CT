"""Explicit stdio daemon entrypoint. No HTTP server or listener is imported."""

import argparse
from contextlib import redirect_stdout
from pathlib import Path
import signal
import sys

from .config import load_config
from .daemon import Daemon
from .rpc import serve


def _terminate(signum, frame):
    raise KeyboardInterrupt


def run(config):
    wire = sys.stdout.buffer
    previous = signal.signal(signal.SIGTERM, _terminate)
    try:
        # Only RPC owns the original stdout; incidental core prints are diagnostics.
        with redirect_stdout(sys.stderr):
            serve(Daemon(config), output=wire)
    finally:
        signal.signal(signal.SIGTERM, previous)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--home', type=Path)
    args = parser.parse_args(argv)
    try:
        run(load_config(args.home))
        return 0
    except KeyboardInterrupt:
        return 0
    except Exception as error:
        print('Daemon startup failed (' + type(error).__name__ + ')', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
