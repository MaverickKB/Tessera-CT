# Developer Mode for the example iPhone app

The example project is [`examples/ios/TesseraVoice`](../../examples/ios/TesseraVoice).
On the home screen it is named Tessera. It is a client of a Tessera server you
run. The project does not name an Apple team, a personal bundle-id prefix, or
a server host.

Deploy notes are in [`examples/ios/DEPLOY.md`](../../examples/ios/DEPLOY.md).
What the app does is in
[`examples/ios/TesseraVoice/README.md`](../../examples/ios/TesseraVoice/README.md).

## 1. Enable Developer Mode on the iPhone

This applies to iOS 16 and later.

1. Open Settings, then Privacy & Security.
2. Scroll to Developer Mode. If the row is missing, connect the phone to a Mac
   with Xcode once, unlock the phone, and look again.
3. Turn Developer Mode on. The phone restarts.
4. After the restart, confirm the alert that asks you to enable Developer Mode.

## 2. Pair the phone with Xcode

1. Install Xcode and open it once so it can install device support.
2. Connect the iPhone with a cable. Unlock the phone.
3. If the phone asks to trust this computer, confirm it and enter the passcode.
4. In Xcode, open Window, then Devices and Simulators. Wait until the phone
   finishes pairing and is available as a run destination.
5. Settings, then Developer, appears on the phone after a successful pair.

## 3. Choose your own signing team

Signing comes from xcconfig, not from a team id stored in the project.

1. From `examples/ios/TesseraVoice`, copy `Signing.example.xcconfig` to
   `Signing.local.xcconfig`. The local file is gitignored. Do not commit it.
2. Set `DEVELOPMENT_TEAM` to your Apple Developer Team ID. In Xcode that is
   Settings, then Accounts, then your team. It is the 10-character team id,
   not your Apple ID email.
3. Set `PRODUCT_BUNDLE_IDENTIFIER_PREFIX` to a prefix you control. The example
   value is `com.example`, which produces these product ids:
   - `com.example.tessera.voice` for the app
   - `com.example.tessera.voice.tests` for unit tests
   - `com.example.tessera.voice.uitests` for UI tests
4. Open `TesseraVoice.xcodeproj`. Each build configuration reads
   `Signing.xcconfig`, which includes the example file and then your local
   file when that file exists. Local values replace the example. Signing &
   Capabilities should show your team, with Automatically manage signing on.
5. Select the TesseraVoice scheme and your phone as the run destination.

`Signing.example.xcconfig` leaves `DEVELOPMENT_TEAM` empty, so a device build
needs the local file. A simulator or a compile check can pass
`CODE_SIGNING_ALLOWED=NO` and does not need a team.

## 4. Trust the developer app on the phone

1. In Xcode, run the TesseraVoice scheme so the app installs on the phone.
2. If the phone reports an untrusted developer, open Settings, then General,
   then VPN & Device Management.
3. Under Developer App, select the team you just installed and tap Trust.
   Confirm the prompt.
4. Open the app from the home screen.

## 5. Point the app at your Tessera server

The server field starts empty. The project does not store a host or a port
for your machine.

1. Start Tessera so the phone can open it. Bind the server on an address that
   exists on your LAN, not only on loopback, and use the port you configured.
2. On the phone, open the app's settings and set Server URL to
   `http://<lan-address>:<port>` or `https://<name-you-control>:<port>`.
   Use the address and port of the server you started. A name you control on
   a private VPN is the same kind of setting. Do not commit the URL.
3. The example allows local-network HTTP through `NSAllowsLocalNetworking` in
   the app Info.plist. A public HTTPS URL does not need that exception. Plain
   HTTP to a name that is not local networking is blocked by App Transport
   Security; use HTTPS or a local address.
4. If iOS asks for Local Network permission, allow it. The usage text says the
   app reaches your server on the local network or a private VPN.
5. An optional access token stays in the device Keychain. Enter it in the
   app. Do not put it in the project or the URL.
6. Select the Tessera session the call should use, then start the call while
   the phone is unlocked.

The phone has to be able to reach that address. The project never records it.

## A compile check without the phone

From `examples/ios/TesseraVoice`, with `BUILD_SCRATCH` set to a writable
directory:

```sh
xcodebuild -project TesseraVoice.xcodeproj -scheme TesseraVoice \
  -destination 'generic/platform=iOS' -derivedDataPath "$BUILD_SCRATCH" \
  CODE_SIGNING_ALLOWED=NO build
```

That shows the project compiles. It does not show microphone, CallKit, or a
paired phone.
