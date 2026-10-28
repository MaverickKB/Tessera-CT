import XCTest
@testable import TesseraVoice

final class RecordedEventFilterTests: XCTestCase {
    @MainActor
    func testProviderRevisionsReplaceOneLineAndIgnoreOlderReplay() {
        let voice = VoiceCoordinator()
        voice.applyTranscript(role: "user", text: "What's 12 plus", isFinal: true, itemID: "speech-1")
        let originalID = voice.transcript[0].id
        voice.applyTranscript(role: "assistant", text: "Checking", isFinal: false, itemID: "reply-1")
        voice.applyTranscript(role: "user", text: "What's 12 plus 13?", isFinal: true, itemID: "speech-1")
        voice.applyTranscript(role: "user", text: "What's 12 plus", isFinal: true, itemID: "speech-1")
        voice.applyTranscript(role: "user", text: "What's 12 plus 13?", isFinal: true, itemID: "speech-1")
        XCTAssertEqual(voice.transcript.count, 2)
        XCTAssertEqual(voice.transcript[0].id, originalID)
        XCTAssertEqual(voice.transcript[0].text, "What's 12 plus 13?")
        voice.applyTranscript(role: "assistant", text: "Twenty-five.", isFinal: true, itemID: "reply-1")
        voice.applyTranscript(role: "assistant", text: "late delta", isFinal: false, itemID: "reply-1")
        XCTAssertEqual(voice.transcript[1].text, "Twenty-five.")
        voice.applyTranscript(role: "user", text: "What's 12 plus 13?", isFinal: true, itemID: "speech-2")
        XCTAssertEqual(voice.transcript.count, 3, "A new utterance can repeat the same words")
    }

    func testPassesTheSpeakerTranscript() {
        let event: [String: Any] = [
            "type": "conversation.item.input_audio_transcription.completed",
            "item_id": "item-1",
            "transcript": "hello",
            "content_index": 0
        ]
        let recorded = RecordedEventFilter.recordedEvent(from: event)
        XCTAssertEqual(recorded?["type"] as? String, "conversation.item.input_audio_transcription.completed")
        XCTAssertEqual(recorded?["item_id"] as? String, "item-1")
        XCTAssertEqual(recorded?["transcript"] as? String, "hello")
    }

    func testRecordsTheProvidersSpokenReplyAndNormalizesLegacyName() {
        for type in ["response.output_audio_transcript.done", "response.audio_transcript.done"] {
            let recorded = RecordedEventFilter.recordedEvent(from: [
                "type": type,
                "item_id": "assistant-1",
                "transcript": "The requested task is complete."
            ])
            XCTAssertEqual(recorded?["type"] as? String, "response.output_audio_transcript.done")
            XCTAssertEqual(recorded?["item_id"] as? String, "assistant-1")
            XCTAssertEqual(recorded?["transcript"] as? String, "The requested task is complete.")
        }
    }

    func testDropsUnsupportedTypes() {
        let event: [String: Any] = ["type": "response.output_audio.delta", "delta": "qq"]
        XCTAssertNil(RecordedEventFilter.recordedEvent(from: event))
    }

    func testSanitizesResponseDone() {
        let event: [String: Any] = [
            "type": "response.done",
            "response": [
                "id": "resp_1",
                "status": "completed",
                "output": [
                    ["type": "function_call", "call_id": "call_1", "name": "secret_tool"],
                    ["type": "message"]
                ],
                "extra": "drop-me"
            ]
        ]
        let recorded = RecordedEventFilter.recordedEvent(from: event)
        let response = recorded?["response"] as? [String: Any]
        XCTAssertEqual(response?["id"] as? String, "resp_1")
        XCTAssertEqual(response?["status"] as? String, "completed")
        let output = response?["output"] as? [[String: Any]]
        XCTAssertEqual(output?.count, 1)
        XCTAssertEqual(output?.first?["call_id"] as? String, "call_1")
        XCTAssertNil(response?["extra"])
    }

    func testLiveTranscriptSeparatesSpeakerAndAssistant() {
        let speaker = RecordedEventFilter.transcriptUpdate(from: [
            "type": "conversation.item.input_audio_transcription.completed",
            "transcript": "Please inspect the task."
        ])
        XCTAssertEqual(speaker?.role, "user")
        XCTAssertEqual(speaker?.text, "Please inspect the task.")
        XCTAssertEqual(speaker?.isFinal, true)
        for type in ["response.output_audio_transcript.delta", "response.audio_transcript.delta"] {
            let assistant = RecordedEventFilter.transcriptUpdate(from: ["type": type, "delta": "I am inspecting it."])
            XCTAssertEqual(assistant?.role, "assistant")
            XCTAssertEqual(assistant?.text, "I am inspecting it.")
            XCTAssertEqual(assistant?.isFinal, false)
        }
        for type in ["response.output_audio_transcript.done", "response.audio_transcript.done"] {
            let assistant = RecordedEventFilter.transcriptUpdate(from: ["type": type, "transcript": "Inspection complete."])
            XCTAssertEqual(assistant?.role, "assistant")
            XCTAssertEqual(assistant?.isFinal, true)
        }
    }
}
