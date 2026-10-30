import SwiftUI

struct SettingsView: View {
    @EnvironmentObject private var settings: AppSettingsStore
    @EnvironmentObject private var model: AppModel
    var onSaved: () -> Void = {}
    @Environment(\.dismiss) private var dismiss
    @State private var serverURL = ""
    @State private var token = ""
    @State private var preferSpeaker = false
    @State private var message: String?
    @State private var testing = false
    @State private var testSucceeded = false

    var body: some View {
        Form {
            Section {
                TextField("Server URL", text: $serverURL)
                    .textInputAutocapitalization(.never)
                    .autocorrectionDisabled()
                    .keyboardType(.URL)
                    .disabled(!model.canEditSettings || testing)
                SecureField("Access token (optional)", text: $token)
                    .textInputAutocapitalization(.never)
                    .autocorrectionDisabled()
                    .disabled(!model.canEditSettings || testing)
                Button(testing ? "Testing…" : "Test connection") {
                    Task { await testConnection() }
                }
                .disabled(!model.canEditSettings || testing)
                if let message {
                    Text(message)
                        .font(.footnote)
                        .foregroundStyle(testSucceeded ? Color.secondary : Color.red)
                        .accessibilityLabel(message)
                }

            } header: {
                Text("Server")
            } footer: {
                Text("Use the address of your running Tessera server, for example \(ProductManifest.exampleServerURL). On this phone, localhost refers to the phone, not your computer. For remote access, use your server’s private HTTPS address. Enter a token only if that server requires one. Save and connect stores the token securely on this device.")
            }

            Section("Audio") {
                Toggle("Use phone speaker", isOn: $preferSpeaker)
                    .disabled(!model.canEditSettings || testing)
                Text("Off by default. Connected AirPods or headphones take priority. A single AirPods press toggles microphone mute during a call; playback continues.")
                    .font(.footnote)
                    .foregroundStyle(.secondary)
            }

            Section {
                DisclosureGroup("About Tessera") {
                    LabeledContent("Name", value: ProductManifest.productName)
                    LabeledContent("Wordmark", value: ProductManifest.wordmark)
                    LabeledContent("Version", value: ProductManifest.version)
                    LabeledContent("Bundle ID", value: ProductManifest.bundleIdentifier)
                    LabeledContent("Min iOS", value: ProductManifest.minimumIOSVersion)
                }
            }

            Section("Honest limits") {
                Text("You start a session while unlocked. Screen lock does not stop an active call. This app does not promise incoming calls, cold-launch mic while locked, survival after force quit, or offline cognition.")
                    .font(.footnote)
                    .foregroundStyle(.secondary)
            }
        }
        .onAppear {
            serverURL = settings.serverURLText
            token = settings.accessTokenText
            preferSpeaker = settings.preferSpeaker
        }
        .onChange(of: serverURL) { _, _ in message = nil; testSucceeded = false }
        .onChange(of: token) { _, _ in message = nil; testSucceeded = false }
        .toolbar {
            ToolbarItem(placement: .cancellationAction) {
                Button("Cancel") { dismiss() }.disabled(testing)
            }
            ToolbarItem(placement: .confirmationAction) {
                Button("Save and connect") {
                    do {
                        try settings.saveConnection(serverURL: serverURL, token: token)
                        settings.preferSpeaker = preferSpeaker
                        onSaved()
                    } catch {
                        testSucceeded = false
                        message = error is SettingsError ? error.localizedDescription
                            : "Could not save the token securely. Your saved connection has not changed."
                    }
                }
                .disabled(!model.canEditSettings || testing)
            }
        }
        .interactiveDismissDisabled(testing)
    }

    @MainActor
    private func testConnection() async {
        testing = true
        testSucceeded = false
        message = nil
        defer { testing = false }
        do {
            let url = try AppSettingsStore.validateServerURL(serverURL)
            let value = token.trimmingCharacters(in: .whitespacesAndNewlines)
            let configuration = URLSessionConfiguration.ephemeral
            configuration.timeoutIntervalForRequest = 10
            configuration.timeoutIntervalForResource = 15
            let session = URLSession(configuration: configuration)
            defer { session.invalidateAndCancel() }
            _ = try await TesseraClient(session: session).fetchStatus(baseURL: url, accessToken: value.isEmpty ? nil : value)
            testSucceeded = true
            message = "Connected to Tessera. Save and connect to keep these settings."
        } catch {
            message = error is SettingsError ? error.localizedDescription
                : "Could not connect. Check that Tessera is running, this phone can reach its address, and the access token is correct."
        }
    }
}
