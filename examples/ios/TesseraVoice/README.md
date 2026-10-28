# Tessera for iPhone

Native iPhone speech input and output for a selected Tessera session. The existing session runner owns reasoning, tools, memory, and continuity. The configured voice provider acts as the voice agent: it answers the caller directly and its audio is the speaker's voice. Provider credentials stay on the server.

## Runtime

1. Select a live session and start the CallKit call while unlocked.
2. Lease the provider socket through `POST /api/sessions/<slug>/voice/start`. The lease carries the session tools and the broker instruction; the provider speaks its answer on that socket.
3. Stream microphone audio with `input_audio_buffer.append`. While muted, or while our own playback is running, send silence of the same length so the transcriber cannot hear Tessera and hand it back as a user turn.
4. Play `response.output_audio.delta` (`response.audio.delta` on the older event name) as it arrives, and render `response.output_audio_transcript.*` as the assistant transcript.
5. Record completed turns by posting the filtered provider events to `/api/sessions/<slug>/voice/events`. The server runs the session turn, tool calls included, and returns socket frames for the provider. Do not also follow and speak a harness reply: in broker mode that answers twice.
6. If the provider creates a response that stays silent, re-ask once after 8s of silence. A healthy response can take 4.4s to its first audio byte, so the re-arm must wait rather than fire on a fixed 2s timer, and it must never fire for a turn that already spoke.
7. End cancels phone I/O. It does not interrupt, delete, or replace the harness session. Stop turn is a separate explicit harness control.

The existing CallKit activation, AirPods mute action, voice-processing audio graph, and headphone routing remain the audio owners. Muting discards the current analyzer generation and pending recognition fragments. Unmuting creates a fresh recognizer. Playback continues while the microphone is muted.

The call screen includes alternate typed input, steering, Stop turn, pending tool approvals, and session notes. Human questions use ordinary selected-session replies. Steering sends `source:"voice"`, without desk-presence updates. Approval cards use the existing autonomy approval endpoints. No approval mode or harness policy is changed.

## Resilience and diagnostics

SSE reconnects only the GET, using a durable per-endpoint/session Last-Event-ID cursor. Completed durable events are deduplicated. Live delta frames do not advance the durable cursor. Registration happens before submission, so a reply arriving before the POST acknowledgement still correlates. Unconfirmed submissions remain queued with their original identity and have an explicit retry control. Recognition input, pending submissions, pending speech, playback duration, and transcript history are bounded.

`Library/Caches/channel-state.json` contains the latest 40 metadata-only records. Records include run, phase, selected session, bridge/playback counters, response timing, mute, background, and protected-data state. The file contains no transcript, audio, endpoint credential, or provider secret.

## Configuration

Endpoint and selected-session preferences persist across relaunches. A refresh does not silently replace a configured session that is unavailable. Fresh installs contain no live endpoint. Optional server access tokens use device-only Keychain storage accessible after first unlock. Headphones or receiver are the default; speaker output is explicit.

## Build and checks

`TesseraVoice/Product/ProductManifest.swift` owns product identity and build defaults. The deployment target comes from ProductManifest. The Apple team and bundle-id prefix come from `Signing.local.xcconfig`, copied from `Signing.example.xcconfig`. SDK version and device OS are independent. See [docs/ios/DEVELOPER-MODE.md](../../../docs/ios/DEVELOPER-MODE.md).

```sh
xcodebuild -project TesseraVoice.xcodeproj -scheme TesseraVoice \
  -destination 'generic/platform=iOS' -derivedDataPath "$BUILD_SCRATCH" \
  CODE_SIGNING_ALLOWED=NO build
xcodebuild -project TesseraVoice.xcodeproj -scheme TesseraVoice \
  -destination "id=$DEVICE" -derivedDataPath "$DEVICE_BUILD_SCRATCH" \
  -allowProvisioningUpdates build
xcodebuild -project TesseraVoice.xcodeproj -scheme TesseraVoice \
  -destination 'platform=iOS Simulator,name=iPhone 17 Pro' \
  -derivedDataPath "$TEST_SCRATCH" -only-testing:TesseraVoiceTests \
  CODE_SIGNING_ALLOWED=NO test
```

The checked-in Xcode project already registers the sources. Regeneration is only needed when changing registration. Unit tests cover protocol identities, provenance exclusion, replay parsing, final-only endpointing, PCM helpers, and CallKit gates. They do not establish on-device SpeechAnalyzer readiness, audible playback, physical AirPods gestures, or locked-screen microphone privacy.

Physical acceptance requires a spoken request producing an attributable tool-using turn in the selected real session, audible provider playback of that reply, continued playback while muted, exclusion of muted speech, lock/unlock multi-turn operation, and microphone cleanup after End. The UI tests exercise the provisioned device without a test-only speech injection path.

## Workspace in the native app

The connect screen and the navigation title show the tree art with the
Tessera-CT wordmark. The embedded page uses the same header as the desktop
app. The iOS app icon stays the tree image. This example does not register
alternate app icons. In-app icon and logo choice is the Settings control on
the server page. This example does not draw the header trail. The embedded
workspace is the web page, so the tree and the gremlin crew are that page's
trail. The trail is hidden at the page's 1020px cutoff, which includes a
phone. No native iOS crew was added.

The main screen loads the current server UI, including chat, WIP/project planning,
infrastructure, integrations and settings. Connection opens device settings;
configure the private server's HTTPS Tailscale address for access away from LAN.
The app uses the device's existing Tailscale connection. It does not create a VPN
or change the server's route. Updated server UI assets load without an app rebuild.

Voice buttons enter the existing native CallKit coordinator. The embedded web
view never owns the microphone. The bottom call bar shows the active call's
session and provides mute/end independently of web navigation. Voice opens the
native call details; Back to workspace dismisses that screen without ending the
call. Reloading the workspace, browsing another session or a failed web load
preserves the native call. Ending a call and stopping a session turn remain
separate actions. Web file exports open the native share sheet.

The optional endpoint credential remains in device Keychain. The embedded page
receives an ephemeral same-origin fetch header; it is not placed in URLs or web
storage. External navigation cannot use the native message bridge.

Run `node --test Scripts/test_workspace_bridge.js` for deterministic bridge
checks without microphone access. `WorkspaceTests` adds native URL policy and
real WebKit click/navigation checks. The UI journey test takes an explicitly
selected `WORKSPACE_TEST_URL` on its test runner and performs read-only page
navigation. Existing physical call tests open the native Voice panel first.

Build 7 displays as Tessera on the paired iPhone. Its branding update retains the physically accepted build 6 voice and workspace behavior. Audible replies through
AirPods, unlocked and locked-screen conversation, one row per utterance, and
continued conversation plus spoken background-task completion after Back to
workspace were confirmed on the device. Muted playback, unmute recovery and End
microphone/call cleanup were confirmed too. AirPods hardware control gestures and off-LAN phone
acceptance remain separate checks. See [docs/ios/DEVELOPER-MODE.md](../../../docs/ios/DEVELOPER-MODE.md).


In session handoff mode, duplex voice dispatches substantial work to the selected
session through session_task and continues the conversation while it runs. Native
completion polling waits for speech and playback to become idle, then supplies
the actual session result to the voice provider. Speaking over a reply clears
playback; Stop turn explicitly cancels session work. Keep the server's current
voice mode until this client is installed and the handoff is activated together.
