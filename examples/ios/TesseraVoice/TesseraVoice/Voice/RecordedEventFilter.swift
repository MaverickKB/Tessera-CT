import Foundation

/// Filters provider events exactly like web/voice.js recordedEvent.
enum RecordedEventFilter {
    // The provider is the voice agent, so its output transcript is the agent's
    // real spoken reply and belongs in the session log. A transcribe-only lease
    // made that transcript an echo of the speaker; the broker design restores it.
    private static let supported: Set<String> = [
        "input_audio_buffer.committed",
        "conversation.item.input_audio_transcription.completed",
        "response.output_audio_transcript.done",
        "response.audio_transcript.done",
        "response.function_call_arguments.done",
        "response.done"
    ]

    static func recordedEvent(from event: [String: Any]) -> [String: Any]? {
        guard let type = event["type"] as? String else {
            return nil
        }
        if supported.contains(type) == false {
            return nil
        }

        var safe: [String: Any] = ["type": type]
        // The provider renamed this event family; the server accepts either name.
        if type == "response.audio_transcript.done" {
            safe["type"] = "response.output_audio_transcript.done"
        }

        let stringKeys = ["event_id", "item_id", "response_id", "call_id", "name", "arguments", "transcript"]
        for key in stringKeys {
            if let value = event[key] as? String {
                safe[key] = value
            }
        }

        let intKeys = ["content_index", "output_index"]
        for key in intKeys {
            if let value = event[key] as? Int, value >= 0 {
                safe[key] = value
            } else if let number = event[key] as? NSNumber {
                let intValue = number.intValue
                if intValue >= 0, Double(intValue) == number.doubleValue {
                    safe[key] = intValue
                }
            }
        }

        if type == "response.done" {
            guard let response = event["response"] as? [String: Any] else {
                return safe
            }
            guard let responseId = response["id"] as? String else {
                return safe
            }
            var safeResponse: [String: Any] = ["id": responseId]
            if let status = response["status"] as? String {
                let allowed = ["completed", "cancelled", "failed", "incomplete"]
                if allowed.contains(status) {
                    safeResponse["status"] = status
                }
            }
            if let output = response["output"] as? [Any] {
                var safeOutput: [[String: Any]] = []
                for item in output {
                    guard let object = item as? [String: Any] else {
                        continue
                    }
                    if object["type"] as? String == "function_call", let callId = object["call_id"] as? String {
                        safeOutput.append(["type": "function_call", "call_id": callId])
                    }
                }
                safeResponse["output"] = safeOutput
            }
            safe["response"] = safeResponse
        }

        return safe
    }

    static func transcriptUpdate(from event: [String: Any]) -> (role: String, text: String, isFinal: Bool)? {
        guard let type = event["type"] as? String else {
            return nil
        }
        // The provider is the voice agent, so its output transcript is the reply.
        if type == "conversation.item.input_audio_transcription.completed" {
            let text = event["transcript"] as? String ?? ""
            return ("user", text, true)
        }
        if type == "response.output_audio_transcript.delta" || type == "response.audio_transcript.delta" {
            let text = event["delta"] as? String ?? ""
            return ("assistant", text, false)
        }
        if type == "response.output_audio_transcript.done" || type == "response.audio_transcript.done" {
            let text = event["transcript"] as? String ?? ""
            return ("assistant", text, true)
        }
        return nil
    }
}
