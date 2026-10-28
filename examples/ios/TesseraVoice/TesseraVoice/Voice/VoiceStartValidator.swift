import Foundation

enum CallPhase: String, Equatable {
    case idle
    case starting
    case connecting
    case listening
    case speaking
    case muted
    case ending
    case error
}

struct CallStatus: Equatable {
    var phase: CallPhase = .idle
    var label: String = "Idle"
    var detail: String = "Configure server and pick a session."
}

struct TranscriptLine: Identifiable, Equatable {
    let id: UUID
    let role: String
    var text: String
    var isFinal: Bool
    var providerKey: String?
    var seenFinalTexts: Set<String> = []

    init(role: String, text: String, isFinal: Bool) {
        self.id = UUID()
        self.role = role
        self.text = text
        self.isFinal = isFinal
    }
}

enum VoiceStartValidator {
    struct Validated {
        let voiceId: String
        let websocketURL: URL
        let protocols: [String]
        let expiresAt: Date
        let sessionExpiresAt: Date
        let maxSessionSeconds: TimeInterval
        let audioSampleRate: Double
        let audioFormat: String
        let sessionUpdate: JSONValue
        let instructions: String
    }

    static func validate(_ response: VoiceStartResponse, now: Date = Date()) throws -> Validated {
        if response.voice_id.isEmpty {
            throw VoiceProtocolError.invalidVoiceAccess
        }
        if response.protocols.isEmpty {
            throw VoiceProtocolError.invalidVoiceAccess
        }
        for value in response.protocols {
            if value.isEmpty {
                throw VoiceProtocolError.invalidVoiceAccess
            }
        }
        if response.audio_sample_rate <= 0 {
            throw VoiceProtocolError.invalidVoiceAccess
        }
        if response.max_session_seconds <= 0 {
            throw VoiceProtocolError.invalidVoiceAccess
        }

        let expiresAt = Date(timeIntervalSince1970: response.expires_at)
        let sessionExpiresAt = Date(timeIntervalSince1970: response.session_expires_at)
        if expiresAt <= now || sessionExpiresAt <= now {
            throw VoiceProtocolError.invalidVoiceAccess
        }

        guard let sessionUpdate = optionalObject(response.session_update) else {
            throw VoiceProtocolError.invalidVoiceAccess
        }
        if sessionUpdate["type"]?.stringValue != "session.update" {
            throw VoiceProtocolError.invalidVoiceAccess
        }
        guard let session = sessionUpdate["session"]?.objectValue else {
            throw VoiceProtocolError.invalidVoiceAccess
        }
        guard let instructions = session["instructions"]?.stringValue else {
            throw VoiceProtocolError.invalidVoiceAccess
        }

        guard let websocketURL = URL(string: response.websocket_url) else {
            throw VoiceProtocolError.invalidTransport
        }
        guard let scheme = websocketURL.scheme?.lowercased() else {
            throw VoiceProtocolError.invalidTransport
        }
        if scheme != "ws" && scheme != "wss" {
            throw VoiceProtocolError.invalidTransport
        }
        if websocketURL.user != nil || websocketURL.password != nil {
            throw VoiceProtocolError.invalidTransport
        }
        if websocketURL.fragment != nil {
            throw VoiceProtocolError.invalidTransport
        }

        if let turn = session["turn_detection"]?.objectValue {
            if turn["type"]?.stringValue != "server_vad" {
                throw VoiceProtocolError.unsupportedTurnDetection
            }
        } else {
            throw VoiceProtocolError.unsupportedTurnDetection
        }

        for direction in ["input", "output"] {
            guard let audio = session["audio"]?.objectValue?[direction]?.objectValue else {
                throw VoiceProtocolError.unsupportedAudioFormat
            }
            guard let format = audio["format"]?.objectValue else {
                throw VoiceProtocolError.unsupportedAudioFormat
            }
            if format["type"]?.stringValue != "audio/pcm" {
                throw VoiceProtocolError.unsupportedAudioFormat
            }
            if format["rate"]?.numberValue != response.audio_sample_rate {
                throw VoiceProtocolError.unsupportedAudioFormat
            }
            if let transport = audio["transport"]?.stringValue, transport != "json" {
                throw VoiceProtocolError.unsupportedAudioFormat
            }
        }

        if response.audio_format != "audio/pcm" {
            // Server may still negotiate audio/pcm in session_update; keep both consistent when present.
            // Some servers report the same format string.
        }

        return Validated(
            voiceId: response.voice_id,
            websocketURL: websocketURL,
            protocols: response.protocols,
            expiresAt: expiresAt,
            sessionExpiresAt: sessionExpiresAt,
            maxSessionSeconds: response.max_session_seconds,
            audioSampleRate: response.audio_sample_rate,
            audioFormat: response.audio_format,
            sessionUpdate: response.session_update,
            instructions: instructions
        )
    }

    private static func optionalObject(_ value: JSONValue) -> [String: JSONValue]? {
        value.objectValue
    }
}

enum VoiceProtocolError: LocalizedError {
    case invalidVoiceAccess
    case invalidTransport
    case unsupportedTurnDetection
    case unsupportedAudioFormat
    case providerError(String)
    case connectTimeout
    case sessionDeadline
    case stalled
    case unreadableEvent
    case bridgeFailed
    case audioBacklog
    case cancelled

    var errorDescription: String? {
        switch self {
        case .invalidVoiceAccess:
            return "Invalid voice access"
        case .invalidTransport:
            return "Invalid voice transport"
        case .unsupportedTurnDetection:
            return "Unsupported speech turn detection"
        case .unsupportedAudioFormat:
            return "Unsupported audio format"
        case .providerError(let code):
            return "Provider: \(code)"
        case .connectTimeout:
            return "Connect timed out"
        case .sessionDeadline:
            return "Time limit reached"
        case .stalled:
            return "Audio send failed"
        case .unreadableEvent:
            return "Unreadable event from provider"
        case .bridgeFailed:
            return "Stopped: event not recorded"
        case .audioBacklog:
            return "Audio backlog"
        case .cancelled:
            return "Cancelled"
        }
    }
}
