# Tessera desktop (Tauri v2)

A native window (WKWebView on macOS, WebView2 on Windows, WebKitGTK on Linux)
around the existing vanilla UI. No frontend build, no Node at runtime.

1. The shell spawns `python -m tessera desktop --host <core_host> --port <core_port>`
   in the checkout, as a managed child with piped stdin/stdout/stderr.
2. It waits up to `startup_seconds` for the v1 ready record
   ([contract](../docs/DESKTOP_IPC.md)) and accepts only an `http://` loopback
   origin with a port, the owned child's PID and no extra fields.
3. The window loads the UI from that origin, so the core's Host/Origin fence
   passes. Navigation cannot leave that origin. The page gets no Tauri IPC
   (no capabilities, no global `__TAURI__`); it talks to the core over plain
   same-origin HTTP/SSE like any other client.
4. On exit the shell closes the core's stdin (graceful), sends SIGTERM after
   `shutdown_seconds`, and kills after `terminate_seconds`. If the shell itself
   dies, the closed pipe stops the core. If the core dies, the app exits.

### Keeping the core (app as supervisor)

With `keep_core = true` in `desktop.toml` the app stops being a parent and
becomes a supervisor instead:

- It spawns `python -m tessera --home <home> serve --host <h> --port <p>` on the
  `attach_url` port, detached: stdin is null, output goes to
  `<home>/logs/core.log`. Closing the window does not stop it.
- The next app run finds the address answering and attaches to the same core
  instead of starting a second one (one home, one core: the manager lock refuses
  a second core on the same home).
- If that core dies while the app is open, the app respawns it at the same
  address and backs off on repeated failure. The page's reconnect banner clears
  when it answers.
- So starting the app is also the way to bump a dead core, and a restart that
  drains into nothing no longer leaves the harness dark.

`keep_core` requires `attach_url` (a stable address is what a later run attaches
to) and a `home`. It is off by default.

The spawned core gets a PATH with `$HOME/.local/bin`, `/opt/homebrew/bin` and
`/usr/local/bin` in front of the app's own, because a window opened from Finder
inherits `/usr/bin:/bin:/usr/sbin:/sbin` and the agent CLIs (`codex`, `node`)
live in `~/.local/bin`. Override with `spawn_path`.

## Build and run

Requires Rust (rustup), Python 3.11+ with this checkout installed in `.venv`, and
the platform webview prerequisites from https://v2.tauri.app/start/prerequisites/.

```sh
cd desktop
cargo run --release              # development launch
cargo install tauri-cli --version "^2" --locked
cargo tauri build                # Tessera.app / .msi / .deb / .AppImage for the host OS
cargo test                       # config, ready-record and real core lifecycle tests
```

### Linux build that was run (Ubuntu 24.04)

The image's default Cargo was 1.83, which cannot build this Tauri crate.
rustup stable (`rustc 1.99.1`) was installed on the machine only. The repo
does not pin a toolchain. WebKitGTK 4.1 development packages, `patchelf`,
`libxdo-dev`, `libssl-dev`, `libayatana-appindicator3-dev`, `librsvg2-dev`,
and `pkg-config` were installed from the distro. The build that produced a
bundle was:

```sh
cargo +stable install tauri-cli --version "^2" --locked
cd desktop && cargo +stable tauri build --bundles deb
cargo +stable test
```

That command wrote `desktop/target/release/bundle/deb/Tessera_0.1.0_amd64.deb`.
An AppImage was not produced in that run; the bundler now builds `deb` and
`appimage` together.

`.github/workflows/desktop.yml` builds the shell for Linux (`deb`, `AppImage`)
and Windows (`msi`, `nsis`) on every change to it, so both platforms have an
installable artifact instead of a source tree nobody built.

Handing the core to a system service (launchd) stays macOS-only. Start and Stop
in the workspace are not: see the next section.

Sign macOS builds so permission grants (Microphone) survive rebuilds:
`APPLE_SIGNING_IDENTITY="Apple Development: Your Name (TEAMID)" cargo tauri build`,
or re-sign an installed copy with `codesign --force --deep --sign "<identity>" Tessera.app`.

`--smoke` opens the window, prints the ready line and exits after the page loads.
stdout carries one JSON line: `{"event":"tessera-desktop.window-ready","startup_ms":...}`.

## Settings

### Start and stop the server on Linux and Windows

The same two buttons work without a system service. In the default and
`keep_core` modes the app is already the core's supervisor, and
`src/supervisor.rs` exposes it to the page: **Start server** spawns or respawns
the core at the configured address, **Stop server** closes it and waits until
the address stops answering. The window stays open, so Start brings it back with
the same sessions and configuration.

`loaded` is false only when the app attached to a core it did not start, such as
one a systemd unit or a terminal owns. The page then hides the buttons rather
than offering controls that would refuse.

### Native server controls on macOS

Set `launch_agent` to the absolute path of the core's existing LaunchAgent plist,
set `attach_url` to that service's loopback address, and keep `keep_core = false`.
In this mode launchd owns the core. Opening the app starts the service if needed
and opens the existing workspace. Closing the app leaves the service alone.

The workspace Settings page contains **Start server** and **Stop server** beside
Restart core. The app menu's Settings command selects that same page.
Stop unloads the service, including KeepAlive, and waits for the server to stop.
The workspace stays open, so Start can start the service again without an HTTP
connection. Sessions and configuration are preserved; failures remain visible.

Only the configured workspace origin in the main window may invoke the two
native lifecycle commands. RunAtLoad may start the service at the next login.

Defaults, then `$TESSERA_DESKTOP_CONFIG` (or `<TESSERA_HOME|~/.tessera>/desktop.toml`),
then `TESSERA_DESKTOP_<KEY>` environment variables. Unknown keys are rejected.

| key | default | |
|---|---|---|
| `python` | `<core_root>/.venv/bin/python`, else `python3` | interpreter |
| `core_root` | the build-time checkout | where the core runs |
| `core_host` | `127.0.0.1` | loopback IPs only |
| `core_port` | `0` (OS picks a free port) | |
| `startup_seconds` / `shutdown_seconds` / `terminate_seconds` | 30 / 5 / 2 | |
| `width` / `height` / `min_width` / `min_height` | 1280 / 900 / 390 / 540 | |
| `title` | `Tessera` | window title |
| `attach_url` / `attach_seconds` | empty / 2 | live core to attach to, and how long to wait for it |
| `home` | `TESSERA_HOME`, else refuse to spawn | Tessera home for a spawned core |
| `keep_core` | `false` | leave the core running after the window closes (needs `attach_url`) |
| `spawn_path` | app PATH with `~/.local/bin`, `/opt/homebrew/bin`, `/usr/local/bin` first | PATH for a spawned core |

`TESSERA_HOME` is passed through to the core and selects its config and sessions.

## Known gaps

- Voice runs on the web UI's browser path (the renderer opens the provider socket
  with the core-minted short-lived lease). The Electron shell (removed) kept that socket in
  main; a Rust-side voice bridge is not ported yet. Packaged builds also need
  `NSMicrophoneUsageDescription` before microphone use on macOS.
- Bundles still run the Python core from a checkout (`core_root`); shipping a
  frozen Python sidecar inside the bundle is a later step. Builds are unsigned unless
  `APPLE_SIGNING_IDENTITY` is set.
