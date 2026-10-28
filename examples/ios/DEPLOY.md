# Example: developer iOS app

`TesseraVoice` is the example iPhone client. It displays as **Tessera** on the
home screen. It is a voice and workspace client of a Tessera server you run.
The project ships no Apple team id, no personal bundle-id prefix, and no
server host.

Physical setup is in [docs/ios/DEVELOPER-MODE.md](../../docs/ios/DEVELOPER-MODE.md).
What the app does is in [TesseraVoice/README.md](TesseraVoice/README.md).

## Deploying it

1. Copy `TesseraVoice/Signing.example.xcconfig` to
   `TesseraVoice/Signing.local.xcconfig`. Set `DEVELOPMENT_TEAM` and
   `PRODUCT_BUNDLE_IDENTIFIER_PREFIX` there. The local file is gitignored.
2. Open `TesseraVoice/TesseraVoice.xcodeproj` in Xcode.
3. Select the TesseraVoice scheme and your iPhone.
4. Run. On the phone, trust the developer profile under Settings, General,
   VPN & Device Management.
5. In the app, set the server URL to your Tessera server. The field starts
   empty. Use an address the phone can reach on your LAN or private VPN.

The checked-in project does not include editor user state (`xcuserdata`).
