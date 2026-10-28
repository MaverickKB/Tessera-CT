import Foundation
import Combine

@MainActor
final class AppModel: ObservableObject {
    @Published var sessions: [SessionSummary] = []
    @Published var voices: [String] = []
    @Published var defaultVoice: String = ""
    @Published var agentName: String = ""
    @Published var voiceConfigured = false
    @Published var voiceEnabled = false
    @Published var refreshError: String?
    @Published var isRefreshing = false

    let settings: AppSettingsStore
    let voice: VoiceCoordinator
    private let client = TesseraClient()

    init(settings: AppSettingsStore, voice: VoiceCoordinator) {
        self.settings = settings
        self.voice = voice
    }

    var liveSessions: [SessionSummary] {
        var result: [SessionSummary] = []
        for session in sessions {
            if session.archived == false {
                result.append(session)
            }
        }
        return result
    }

    var canEditSettings: Bool {
        voice.isRunning == false
    }

    func refresh() async {
        if isRefreshing {
            return
        }
        isRefreshing = true
        defer { isRefreshing = false }

        do {
            let baseURL = try settings.resolvedServerURL()
            let token = settings.accessToken()
            let status = try await client.fetchStatus(baseURL: baseURL, accessToken: token)
            let listed = try await client.listSessions(baseURL: baseURL, accessToken: token)

            sessions = listed
            agentName = status.agent_name ?? ""
            voiceEnabled = status.voice?.enabled ?? false
            voiceConfigured = status.voice?.configured ?? false
            voices = status.voice?.voices ?? []
            defaultVoice = status.voice?.voice ?? ""
            refreshError = nil

            if settings.selectedSlug.isEmpty == false {
                let stillThere = liveSessions.contains { $0.slug == settings.selectedSlug }
                if stillThere == false {
                    refreshError = "The selected session is unavailable. Choose a session explicitly; the active call keeps its original session."
                }
            } else {
                settings.selectedSlug = liveSessions.first?.slug ?? ""
            }

            if settings.selectedVoice.isEmpty {
                if voices.contains(defaultVoice) {
                    settings.selectedVoice = defaultVoice
                } else if let first = voices.first {
                    settings.selectedVoice = first
                }
            }
        } catch {
            refreshError = error.localizedDescription
        }
    }

    func startCall() async {
        do {
            try settings.saveAccessTokenFromField()
            let baseURL = try settings.resolvedServerURL()
            let slug = settings.selectedSlug
            if slug.isEmpty {
                refreshError = "Pick a live session first."
                return
            }
            let voiceName = settings.selectedVoice.isEmpty ? nil : settings.selectedVoice
            await voice.start(
                baseURL: baseURL,
                accessToken: settings.accessToken(),
                slug: slug,
                voice: voiceName,
                preferSpeaker: settings.preferSpeaker
            )
        } catch {
            refreshError = error.localizedDescription
        }
    }
}
