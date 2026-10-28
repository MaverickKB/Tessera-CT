import Foundation

struct SessionTurnPipeline {
    struct SpeakItem: Equatable {
        let turnId: String
        let messageIndex: Int
        let text: String
    }
    enum Action: Equatable {
        case showUser(text: String, isFinal: Bool)
        case showAssistant(text: String, isFinal: Bool)
        case setBusy(String?)
        case enqueueSpeak(SpeakItem)
        case turnFailed(turnId: String, error: String)
    }
    private(set) var registeredTurns: [String] = []
    private var receivedIndexes: [Int] = []
    private var earlySteeringEvents: [(event: String, data: [String: Any])]?

    mutating func reset() {
        registeredTurns = []
        receivedIndexes = []
        earlySteeringEvents = nil
    }

    mutating func registerSubmit(turnId: String) {
        if registeredTurns.contains(turnId) { return }
        registeredTurns.append(turnId)
        if registeredTurns.count > 128 { registeredTurns.removeFirst() }
    }

    mutating func beginSteering() {
        earlySteeringEvents = []
    }

    mutating func finishSteering(turnId: String?) -> [Action] {
        let early = earlySteeringEvents ?? []
        earlySteeringEvents = nil
        guard let turnId else { return [] }
        registerSubmit(turnId: turnId)
        var actions: [Action] = []
        for record in early {
            if record.data["turn_id"] as? String == turnId {
                actions.append(contentsOf: handleSSE(event: record.event, data: record.data))
            }
        }
        return actions
    }

    mutating func handleSSE(event: String, data: [String: Any]) -> [Action] {
        guard let turn = data["turn_id"] as? String else { return [] }
        if registeredTurns.contains(turn) == false {
            if let early = earlySteeringEvents, early.count < 32, event != "delta" {
                earlySteeringEvents?.append((event, data))
            }
            return []
        }
        switch event {
        case "turn_start":
            return [.setBusy("Session working")]
        case "turn_end":
            if data["success"] as? Bool == false {
                return [.setBusy(nil), .turnFailed(turnId: turn, error: "Turn did not complete")]
            }
            return [.setBusy(nil)]
        case "error":
            return [.setBusy(nil), .turnFailed(turnId: turn, error: "Session reported an error")]
        case "message":
            guard let kind = data["kind"] as? String else { return [] }
            if let meta = data["meta"] as? [String: Any] {
                if meta["voice_transcript"] as? Bool == true || meta["provenance"] as? String == "client_reported" { return [] }
            }
            if kind == "tool" { return [.setBusy("Using tools")] }
            guard kind == "agent", let index = data["i"] as? Int, index >= 0,
                  let text = data["text"] as? String, text.isEmpty == false else { return [] }
            if receivedIndexes.contains(index) { return [] }
            receivedIndexes.append(index)
            if receivedIndexes.count > 512 { receivedIndexes.removeFirst() }
            return [.showAssistant(text: text, isFinal: true),
                    .enqueueSpeak(SpeakItem(turnId: turn, messageIndex: index, text: text))]
        default:
            return []
        }
    }

}

/// SSE frame parser. Keeps last event id for reconnect without inventing payloads.
struct SessionSSEParser {
    private var eventName = "message"
    private var dataLines: [String] = []
    private(set) var lastEventID: String?
    private var frameID: String?

    struct Frame: Equatable {
        let id: String?
        let event: String
        let data: String
    }

    mutating func push(line: String) -> Frame? {
        if line.hasPrefix(":") {
            return nil
        }
        if line.isEmpty {
            return emit()
        }
        if line.hasPrefix("id:") {
            let value = String(line.dropFirst(3)).trimmingCharacters(in: .whitespaces)
            frameID = value
            return nil
        }
        if line.hasPrefix("event:") {
            eventName = String(line.dropFirst(6)).trimmingCharacters(in: .whitespaces)
            if eventName.isEmpty {
                eventName = "message"
            }
            return nil
        }
        if line.hasPrefix("data:") {
            var value = String(line.dropFirst(5))
            if value.hasPrefix(" ") {
                value = String(value.dropFirst())
            }
            dataLines.append(value)
            return nil
        }
        return nil
    }

    mutating func finish() -> Frame? {
        emit()
    }

    private mutating func emit() -> Frame? {
        if dataLines.isEmpty {
            eventName = "message"
            frameID = nil
            return nil
        }
        let data = dataLines.joined(separator: "\n")
        let frame = Frame(id: frameID, event: eventName, data: data)
        if let frameID { lastEventID = frameID }
        frameID = nil
        dataLines.removeAll(keepingCapacity: true)
        eventName = "message"
        return frame
    }

    static func jsonObject(from data: String) -> [String: Any]? {
        guard let raw = data.data(using: .utf8) else {
            return nil
        }
        guard let object = try? JSONSerialization.jsonObject(with: raw) as? [String: Any] else {
            return nil
        }
        return object
    }
}

/// Endpoint policy for when a progressive transcript becomes one POST body.
enum SpeechEndpointPolicy {
    /// Finalized text with enough trailing silence becomes one utterance.
    static func shouldSubmit(
        finalizedText: String,
        partialText: String,
        silenceSeconds: TimeInterval,
        requiredSilence: TimeInterval
    ) -> String? {
        let candidate = finalizedText.trimmingCharacters(in: .whitespacesAndNewlines)
        if candidate.isEmpty {
            return nil
        }
        if partialText.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty == false {
            return nil
        }
        if silenceSeconds < requiredSilence {
            return nil
        }
        return candidate
    }

}

/// A short spoken stop is a control, not another session message.
enum SpokenStop {
    static func isStopCommand(_ text: String) -> Bool {
        let words = words(in: text)
        if words.isEmpty || words.count > 4 {
            return false
        }
        var sawStop = false
        for word in words {
            if word == "stop" || word == "cancel" || word == "quiet" {
                sawStop = true
                continue
            }
            if word == "it" || word == "now" || word == "talking" || word == "please" || word == "that" {
                continue
            }
            return false
        }
        return sawStop
    }

    private static func words(in text: String) -> [String] {
        var words: [String] = []
        let pieces = text.lowercased().split { character in
            character.isLetter == false
        }
        for piece in pieces {
            let word = String(piece)
            if word.isEmpty == false {
                words.append(word)
            }
        }
        return words
    }
}
