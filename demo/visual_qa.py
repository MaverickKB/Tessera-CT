"""Capture four Chrome screenshots of a synthetic, offline session."""

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'tests'))
from fake_backend import FakeBackend
from tessera.config import Config
from tessera.server import create_server


def main():
    chrome = os.environ['TESSERA_CHROME_BINARY']
    with tempfile.TemporaryDirectory(prefix='visual-', dir=ROOT / 'demo') as directory:
        config = Config(home=Path(directory))
        config.server.port = 0
        config.jev.enabled = False
        server = create_server(config, backend_factory=lambda *args: FakeBackend([]))
        session = server.manager.create('Visual QA')
        log = server.manager.get(session['slug']).log
        log.append('user', 'Read notes.txt and tell me which delivery is ready for review.')
        log.append('agent', 'I will read the local note and check the delivery status.')
        log.append('tool', json.dumps({'name': 'read_file', 'arguments': {'path': 'notes.txt'}}))
        log.append('echo', 'The garden inventory report is ready for review. The equipment list still needs an update.')
        log.append('work', '[reader] Inventory is ready; equipment needs an update.')
        log.append('agent', 'The garden inventory is ready. Equipment remains pending.')
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            for size, dimensions in [('desktop', '1440,900'), ('mobile', '390,844')]:
                for theme in ('light', 'dark'):
                    output = ROOT / 'demo' / f'ui-{size}-{theme}.png'
                    output.unlink(missing_ok=True)
                    with tempfile.TemporaryDirectory(dir=directory) as profile:
                        args = [chrome, '--headless', '--no-first-run', '--no-default-browser-check',
                                '--user-data-dir=' + profile, '--window-size=' + dimensions,
                                '--force-device-scale-factor=1', '--timeout=10000',
                                '--disable-background-networking', '--disable-component-update',
                                '--no-sandbox', '--disable-gpu',
                                '--remote-debugging-port=0',
                                '--screenshot=' + str(output)]
                        if theme == 'dark':
                            args.append('--force-dark-mode')
                        if size == 'mobile':
                            args = [arg for arg in args if not arg.startswith(('--screenshot=', '--timeout='))]
                        args.append(server.url + '/#session=' + session['slug'])
                        # Some headless macOS builds write successfully but do not exit.
                        # Watch the artifact, then clean up only this temporary process group.
                        process = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                                   start_new_session=True)
                        try:
                            deadline = time.monotonic() + 25
                            active = Path(profile) / 'DevToolsActivePort'
                            while not active.exists() and time.monotonic() < deadline:
                                time.sleep(.1)
                            if size == 'mobile':
                                port = active.read_text().splitlines()[0]
                                target = None
                                while target is None and time.monotonic() < deadline:
                                    with urlopen(f'http://localhost:{port}/json/list', timeout=5) as response:
                                        targets = json.load(response)
                                    target = next((t for t in targets if t['type'] == 'page'), None)
                                    if target is None:
                                        time.sleep(.1)
                                if target is None:
                                    raise RuntimeError('Chrome did not open a page')
                                # Chrome enforces a desktop window minimum. Override the
                                # actual CSS viewport before its CLI screenshot timeout.
                                script = '''const ws = new WebSocket(process.argv[1]);
ws.onopen = () => ws.send(JSON.stringify({id:1, method:'Emulation.setDeviceMetricsOverride',
params:{width:390,height:844,deviceScaleFactor:1,mobile:false}}));
ws.onmessage = e => {
 const r=JSON.parse(e.data);
 if(r.error) {console.error(r.error);process.exit(1);}
 if(r.id===1) setTimeout(()=>ws.send(JSON.stringify({id:2,method:'Runtime.evaluate',
 params:{expression:'JSON.stringify({width:innerWidth,scroll:document.documentElement.scrollWidth})'}})),1000);
 if(r.id===2) {console.log(r.result.result.value); ws.send(JSON.stringify({id:3,method:'Page.captureScreenshot',params:{format:'png'}}));}
 if(r.id===3) {require('fs').writeFileSync(process.argv[2],Buffer.from(r.result.data,'base64'));ws.close();}
};
ws.onerror = e => {console.error(e.message);process.exit(1);};
setTimeout(()=>process.exit(1),5000).unref();'''
                                result = subprocess.run(['node', '-e', script, target['webSocketDebuggerUrl'], str(output)],
                                               check=True, capture_output=True, timeout=7)
                                print(result.stdout.decode().strip())
                            while not output.exists() and time.monotonic() < deadline:
                                time.sleep(.1)
                            if not output.exists():
                                raise RuntimeError('Chrome did not produce a screenshot')
                        finally:
                            import signal
                            try:
                                os.killpg(process.pid, signal.SIGTERM)
                            except ProcessLookupError:
                                pass
                            process.wait(timeout=5)
                    print(output.name)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=10)


if __name__ == '__main__':
    main()
