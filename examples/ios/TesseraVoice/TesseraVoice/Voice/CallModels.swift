import Foundation

enum CallPhase: String, Equatable {
    case idle
    case starting
    case connecting
    case listening
    case speaking
    case busy
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

    init(id: UUID = UUID(), role: String, text: String, isFinal: Bool) {
        self.id = id
        self.role = role
        self.text = text
        self.isFinal = isFinal
    }
}

enum SpeechReadiness: Equatable {
    case unknown
    case checking
    case installing
    case ready
    case unavailable(String)

    var isReady: Bool {
        if case .ready = self {
            return true
        }
        return false
    }

    var label: String {
        switch self {
        case .unknown:
            return "Speech unknown"
        case .checking:
            return "Checking on-device speech"
        case .installing:
            return "Installing on-device speech"
        case .ready:
            return "On-device speech ready"
        case .unavailable(let detail):
            return detail
        }
    }
}

final class ChannelDiagnostics {
    struct Record: Codable {
        var timestamp = Date()
        let event: String
        let run: String
        let phase: String
        let session: String?
        let recognition: Int
        let submitted: Int
        let agentReceived: Int
        let played: Int
        let turnID: String?
        let messageIndex: Int?
        let offset: Int?
        let bytes: Int?
        let pendingSubmissions: Int
        let pendingSpeech: Int
        let muted: Bool
        let protectedData: Bool
        let background: Bool

        enum CodingKeys: String, CodingKey {
            case timestamp, event, run, phase, session, recognition, submitted, agentReceived, played
            case turnID = "turn_id"
            case messageIndex = "message_i"
            case offset, bytes, pendingSubmissions, pendingSpeech, muted, protectedData, background
        }
    }
    private var records: [Record] = []
    private let queue = DispatchQueue(label: "com.example.tessera.voice.diagnostics")

    func record(_ record: Record) {
        records.append(record)
        if records.count > 40 { records.removeFirst() }
        guard let directory = FileManager.default.urls(for: .cachesDirectory, in: .userDomainMask).first,
              let data = try? JSONEncoder().encode(records) else { return }
        queue.async {
            try? data.write(to: directory.appendingPathComponent("channel-state.json"), options: [.atomic, .completeFileProtectionUntilFirstUserAuthentication])
        }
    }
}
