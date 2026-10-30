import SwiftUI

struct RootView: View {
    @EnvironmentObject private var model: AppModel
    @EnvironmentObject private var voice: VoiceCoordinator
    @EnvironmentObject private var settings: AppSettingsStore
    @StateObject private var workspace = WorkspaceState()
    @State private var connection: WorkspaceConnection?
    @State private var connectionID = UUID()
    @State private var showSettings = false
    @State private var showCall = false

    var body: some View {
        NavigationStack {
            ZStack {
                if let connection {
                    WorkspaceView(connection: connection, state: workspace)
                        .id(connectionID)
                } else {
                    ContentUnavailableView {
                        VStack(spacing: 8) {
                            Image("BrandMark")
                                .resizable()
                                .frame(width: 64, height: 64)
                                .clipShape(RoundedRectangle(cornerRadius: 14))
                                .accessibilityHidden(true)
                            Text(ProductManifest.wordmark)
                                .font(.title3.weight(.semibold))
                            Label("Connect to Tessera", systemImage: "network")
                        }
                    } description: {
                        Text("Set your private server address in Connection. Use its Tailscale HTTPS address when away from home.")
                    } actions: {
                        Button("Connection") { showSettings = true }
                    }
                }
                if let error = workspace.error {
                    VStack(spacing: 12) {
                        Text(error).multilineTextAlignment(.center)
                        Button("Retry workspace") { workspace.reload() }
                            .buttonStyle(.borderedProminent)
                    }
                    .padding()
                    .background(.regularMaterial, in: RoundedRectangle(cornerRadius: 16))
                    .padding()
                }
            }
            .navigationTitle(model.agentName.isEmpty ? ProductManifest.displayName : model.agentName)
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .principal) {
                    VStack(spacing: 0) {
                        Text(model.agentName.isEmpty ? ProductManifest.displayName : model.agentName)
                            .font(.headline)
                        Text(ProductManifest.wordmark)
                            .font(.caption2)
                            .foregroundStyle(.secondary)
                    }
                    .accessibilityElement(children: .combine)
                }
                ToolbarItem(placement: .topBarLeading) {
                    Button("Connection", systemImage: "network") { showSettings = true }
                        .disabled(voice.isRunning)
                }
                ToolbarItemGroup(placement: .topBarTrailing) {
                    Menu {
                        Button("Chat") { workspace.openPage("chat") }
                        Button("Work in progress") { workspace.openPage("wip") }
                        Button("Infrastructure") { workspace.openPage("infrastructure") }
                        Button("Voice") { workspace.openPage("voice") }
                        Button("Integrations") { workspace.openPage("integrations") }
                        Button("Settings") { workspace.openPage("settings") }
                    } label: {
                        Label("Pages", systemImage: "square.grid.2x2")
                    }
                    .disabled(connection == nil || voice.isRunning)
                    Button("Reload workspace", systemImage: "arrow.clockwise") { workspace.reload() }
                        .disabled(connection == nil)
                }
            }
            .safeAreaInset(edge: .bottom, spacing: 0) { callBar }
            .sheet(isPresented: $showSettings) {
                NavigationStack {
                    SettingsView(onSaved: {
                        connect()
                        showSettings = false
                    })
                    .navigationTitle("Connection")
                }
            }
            .sheet(isPresented: $showCall) {
                NavigationStack {
                    CallView()
                        .navigationTitle("Native voice")
                        .toolbar {
                            ToolbarItem(placement: .confirmationAction) {
                                Button("Back to workspace") { showCall = false }
                            }
                        }
                }
            }
            .sheet(item: $workspace.download) { item in
                WorkspaceShareView(url: item.url)
            }
            .task { connect() }
        }
    }

    private var callBar: some View {
        HStack(spacing: 12) {
            Button { showCall = true } label: {
                VStack(alignment: .leading, spacing: 2) {
                    Label(voice.isRunning ? voice.status.label : "Voice", systemImage: "waveform")
                    if let slug = voice.activeSessionSlug, voice.isRunning {
                        Text(slug).font(.caption).lineLimit(1)
                    }
                }
                .frame(maxWidth: .infinity, alignment: .leading)
            }
            if voice.isRunning {
                Button(voice.isMuted ? "Unmute" : "Mute") { voice.setMuted(!voice.isMuted) }
                Button("End", role: .destructive) { voice.stop() }
            }
        }
        .buttonStyle(.bordered)
        .padding(.horizontal)
        .padding(.vertical, 8)
        .background(.bar)
    }

    private func connect() {
        do {
            let next = WorkspaceConnection(url: try settings.resolvedServerURL(), token: settings.accessToken())
            if connection != next {
                connection = next
                connectionID = UUID()
            }
            workspace.error = nil
            Task { await model.refresh() }
        } catch {
            connection = nil
            workspace.error = settings.serverURLText.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
                ? nil : error.localizedDescription
        }
    }
}
