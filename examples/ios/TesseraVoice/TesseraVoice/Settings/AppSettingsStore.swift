import Foundation
import Combine

/// Editable while idle. Never holds provider credentials.
@MainActor
final class AppSettingsStore: ObservableObject {
    @Published var serverURLText: String {
        didSet { defaults.set(serverURLText, forKey: Keys.serverURL) }
    }

    @Published var accessTokenText: String {
        didSet {
            // Persist on explicit save so partial typing is not written mid-edit.
        }
    }

    @Published var selectedSlug: String {
        didSet { defaults.set(selectedSlug, forKey: Keys.selectedSlug) }
    }

    @Published var selectedVoice: String {
        didSet { defaults.set(selectedVoice, forKey: Keys.selectedVoice) }
    }

    @Published var preferSpeaker: Bool {
        didSet { defaults.set(preferSpeaker, forKey: Keys.preferSpeaker) }
    }

    private let defaults: UserDefaults

    private enum Keys {
        static let serverURL = "settings.serverURL"
        static let selectedSlug = "settings.selectedSlug"
        static let selectedVoice = "settings.selectedVoice"
        static let preferSpeaker = "settings.preferSpeaker"
    }

    init(defaults: UserDefaults = .standard, loadToken: () -> String? = KeychainStore.loadToken) {
        self.defaults = defaults
        self.serverURLText = defaults.string(forKey: Keys.serverURL) ?? ""
        self.selectedSlug = defaults.string(forKey: Keys.selectedSlug) ?? ""
        self.selectedVoice = defaults.string(forKey: Keys.selectedVoice) ?? ""
        if defaults.object(forKey: Keys.preferSpeaker) == nil {
            self.preferSpeaker = false
        } else {
            self.preferSpeaker = defaults.bool(forKey: Keys.preferSpeaker)
        }
        self.accessTokenText = loadToken() ?? ""
        defaults.set(serverURLText, forKey: Keys.serverURL)
        defaults.set(selectedSlug, forKey: Keys.selectedSlug)
        defaults.set(selectedVoice, forKey: Keys.selectedVoice)
        defaults.set(preferSpeaker, forKey: Keys.preferSpeaker)
    }

    func saveAccessTokenFromField() throws {
        let trimmed = accessTokenText.trimmingCharacters(in: .whitespacesAndNewlines)
        try KeychainStore.saveToken(trimmed.isEmpty ? nil : trimmed)
        accessTokenText = trimmed
    }

    func saveConnection(serverURL: String, token: String,
                        storeToken: (String?) throws -> Void = KeychainStore.saveToken) throws {
        let url = try Self.validateServerURL(serverURL)
        let trimmedToken = token.trimmingCharacters(in: .whitespacesAndNewlines)
        // Validate and store securely before changing any saved connection setting.
        try storeToken(trimmedToken.isEmpty ? nil : trimmedToken)
        accessTokenText = trimmedToken
        serverURLText = url.absoluteString
    }

    func resolvedServerURL() throws -> URL {
        try Self.validateServerURL(serverURLText)
    }

    static func validateServerURL(_ text: String) throws -> URL {
        let trimmed = text.trimmingCharacters(in: .whitespacesAndNewlines)
        if trimmed.isEmpty {
            throw SettingsError.missingServerURL
        }
        guard let url = URL(string: trimmed) else {
            throw SettingsError.invalidServerURL
        }
        guard let scheme = url.scheme?.lowercased() else {
            throw SettingsError.invalidServerURL
        }
        if scheme != "http" && scheme != "https" {
            throw SettingsError.invalidServerURL
        }
        if url.host == nil || url.host?.isEmpty == true || url.user != nil || url.password != nil
            || url.query != nil || url.fragment != nil {
            throw SettingsError.invalidServerURL
        }
        return url
    }

    func accessToken() -> String? {
        let trimmed = accessTokenText.trimmingCharacters(in: .whitespacesAndNewlines)
        if trimmed.isEmpty {
            return nil
        }
        return trimmed
    }
}

enum SettingsError: LocalizedError {
    case missingServerURL
    case invalidServerURL

    var errorDescription: String? {
        switch self {
        case .missingServerURL:
            return "Enter your Tessera server address before connecting."
        case .invalidServerURL:
            return "Enter an http or https server address without a username, password, query, or fragment."
        }
    }
}
