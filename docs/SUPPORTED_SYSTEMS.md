# Supported systems and development stubs

What works today, what is a development stub, and what is untested. This list is a
measured statement, not a wish: every entry below is grounded in evidence gathered
against a real running system, unless the entry says otherwise.

## Supported

| Surface | Status | Evidence |
| --- | --- | --- |
| Mac desktop shell (Tauri) | Supported | Running on macOS against a supervised core; restart, settings, board and chat exercised live. |
| Mac web page (loopback + Tailscale front door) | Supported | Settings render and save proven over CDP; board write routes verified pre/post reload; hardwire suite green. |
| Phone web page (browser) | Supported with known limits | Chrome phone emulation drives every page; chat Read fix verified in a real browser. A physical-device pass over CUA has not been granted. |
| Board and project cockpit (Mac + phone browser) | Supported | Card writes, notes, claims, filing, canvas import and the pressure page all exercised live. |
| Supervised core lifecycle (launchd) | Supported | restart-core.sh reload proven twice this day (drain, supervisor handback). |
| Ubuntu 24.04 cloud VM, wheel and desktop .deb | Recorded | Wheel install, UI provider save, one local-fake chat turn, one CLI worker, and one WebKitGTK window. Not a Debian 13 result and not a bundled AetherMind turn. |
| Windows under WSL | Supported as Linux | A WSL process is Linux, so embedded memory follows the Linux path. The recorded run for that path is the Ubuntu 24.04 cloud VM. This file does not record a separate WSL install. |

## Development stubs

| Surface | Status | Evidence |
| --- | --- | --- |
| TesseraCUA native interaction | Dev stub | Accessibility permission not granted on this Mac; page-driving falls back to CDP. |
| Voice duplex, web | Working under test | Session-scoped voice route and render states verified without opening a microphone. |
| TesseraVoice iPhone app | Supported on a physical iPhone | Ships here under examples/ios/TesseraVoice: the full calling app, voice-first with CallKit, replaced the earlier web fallback. |
| Voice duplex, physical iPhone | Supported | Same app: microphone, playback, mute, headphone routing and CallKit exercised on a physical device. |
| Linux cloud VM (development) | Dev environment | The repo's cloud-agent bootstrap runs there. |

## Untested

- Clean macOS VM install (no run has been performed).
- Native Windows is not yet supported for embedded memory. The vendored
  AetherMind engine reports `lock_backend` `unavailable` there: its only file
  lock is `fcntl`. Before a turn, Tessera reads that report and refuses with
  `embedded AetherMind needs file locking that native Windows lacks; run Tessera under WSL, or configure an external AetherMind`.
  Settings shows that sentence. No native Windows desktop or VM install has
  been run. Native Windows is a later update.
- A live systemd user-service start, a Windows service, and Windows
  Credential Manager (no run has been performed). Settings says what this
  host will not do.
- A memory turn on the recorded Ubuntu wheel. That wheel was built without
  the engine. Linux and macOS can record an embedded memory write. Native
  Windows cannot with the embedded engine: the turn is refused before the
  write. Under WSL the process is Linux, and that is the Linux path.

## Rules this document lives by

A row moves from one column to another only with a recorded run against the real
surface named in the row. A green test suite is not a supported-system claim.
This file is the requirement-7 matrix for the distributable alpha and updates
with each clean-system test batch.