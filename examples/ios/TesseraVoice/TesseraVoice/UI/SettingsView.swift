import SwiftUI

struct SettingsView: View {
    @EnvironmentObject private var settings: AppSettingsStore
    @EnvironmentObject private var model: AppModel
    @State private var saveMessage: String?

    var body: some View {
        Form {
            Section {
                TextField("Server URL", text: $settings.serverURLText)
                    .textInputAutocapitalization(.never)
                    .autocorrectionDisabled()
                    .keyboardType(.URL)
                    .disabled(model.canEditSettings == false)
                SecureField("Access token (optional)", text: $settings.accessTokenText)
                    .textInputAutocapitalization(.never)
                    .autocorrectionDisabled()
                    .disabled(model.canEditSettings == false)
                Button("Save token to Keychain") {
                    do {
                        try settings.saveAccessTokenFromField()
                        saveMessage = "Token saved (after first unlock)."
                    } catch {
                        saveMessage = "Could not save token."
                    }
                }
                .disabled(model.canEditSettings == false)
                if let saveMessage {
                    Text(saveMessage)
                        .font(.footnote)
                        .foregroundStyle(.secondary)
                }
            } header: {
                Text("Server")
            } footer: {
                Text("No server ships by default. Example shape only: \(ProductManifest.exampleServerURL). Optional token is stored in Keychain with after-first-unlock-this-device-only accessibility. Network errors never show the token.")
            }

            Section("Audio") {
                Toggle("Use phone speaker", isOn: $settings.preferSpeaker)
                    .disabled(model.canEditSettings == false)
                Text("Off by default. Connected AirPods or headphones take priority. A single AirPods press toggles microphone mute during a call; playback continues.")
                    .font(.footnote)
                    .foregroundStyle(.secondary)
            }

            Section("Product") {
                LabeledContent("Name", value: ProductManifest.productName)
                LabeledContent("Wordmark", value: ProductManifest.wordmark)
                LabeledContent("Version", value: ProductManifest.version)
                LabeledContent("Bundle ID", value: ProductManifest.bundleIdentifier)
                LabeledContent("Min iOS", value: ProductManifest.minimumIOSVersion)
            }

            Section("Honest limits") {
                Text("You start a session while unlocked. Screen lock does not stop an active call. This app does not promise incoming calls, cold-launch mic while locked, survival after force quit, or offline cognition.")
                    .font(.footnote)
                    .foregroundStyle(.secondary)
            }
        }
    }
}
