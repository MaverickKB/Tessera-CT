import SwiftUI

@main
struct TesseraVoiceApp: App {
    @StateObject private var modelHolder = AppBootstrap()

    var body: some Scene {
        WindowGroup {
            RootView()
                .environmentObject(modelHolder.settings)
                .environmentObject(modelHolder.voice)
                .environmentObject(modelHolder.model)
        }
    }
}

@MainActor
final class AppBootstrap: ObservableObject {
    let settings: AppSettingsStore
    let voice: VoiceCoordinator
    let model: AppModel

    init() {
        let settings = AppSettingsStore()
        let voice = VoiceCoordinator()
        self.settings = settings
        self.voice = voice
        self.model = AppModel(settings: settings, voice: voice)
    }
}
