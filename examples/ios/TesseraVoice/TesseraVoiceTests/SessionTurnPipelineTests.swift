import XCTest
@testable import TesseraVoice

final class SessionTurnPipelineTests: XCTestCase {
    private let turn = UUID().uuidString.lowercased()

    func testAgentReplyBeforePostAcknowledgementCorrelatesWithRegisteredUUID() {
        var pipeline = SessionTurnPipeline()
        pipeline.registerSubmit(turnId: turn)
        let reply: [String: Any] = ["turn_id": turn, "i": 17, "kind": "agent", "text": "Tool completed.",
                                    "meta": ["request_id": "provider-specific-value"]]
        let actions = pipeline.handleSSE(event: "message", data: reply)
        XCTAssertEqual(actions, [.showAssistant(text: "Tool completed.", isFinal: true),
                                 .enqueueSpeak(.init(turnId: turn, messageIndex: 17, text: "Tool completed."))])
        XCTAssertEqual(pipeline.handleSSE(event: "message", data: reply), [])
    }

    func testCanonicalIdentityWinsAndUnregisteredTurnsStaySilent() {
        var pipeline = SessionTurnPipeline()
        pipeline.registerSubmit(turnId: turn)
        let foreign: [String: Any] = ["turn_id": "0faea5cf-d23b-44da-aeae-40c83fd9f38d", "i": 42,
                                      "kind": "agent", "text": "Another turn", "meta": ["request_id": turn]]
        XCTAssertEqual(pipeline.handleSSE(event: "message", data: foreign), [])
    }

    func testServerAssignedSteeringTurnCanReplyBeforeControlAcknowledgement() {
        var pipeline = SessionTurnPipeline()
        pipeline.beginSteering()
        let message: [String: Any] = ["turn_id": turn, "i": 31, "kind": "agent", "text": "Steer applied"]
        XCTAssertTrue(pipeline.handleSSE(event: "message", data: message).isEmpty)
        let actions = pipeline.finishSteering(turnId: turn)
        XCTAssertTrue(actions.contains(.enqueueSpeak(.init(turnId: turn, messageIndex: 31, text: "Steer applied"))))
        XCTAssertTrue(pipeline.handleSSE(event: "message", data: message).isEmpty)
    }

    func testToolAndReasoningAreNotSentToSpeech() {
        var pipeline = SessionTurnPipeline()
        pipeline.registerSubmit(turnId: turn)
        for kind in ["tool", "note", "echo", "work"] {
            let actions = pipeline.handleSSE(event: "message", data: ["turn_id": turn, "i": 8, "kind": kind, "text": "body"])
            for action in actions {
                if case .enqueueSpeak = action { XCTFail("Only completed agent messages are speech input") }
            }
        }
    }

    func testClientReportedProviderTranscriptsCannotMasqueradeAsRunnerMessages() {
        var pipeline = SessionTurnPipeline()
        pipeline.registerSubmit(turnId: turn)
        for meta: [String: Any] in [["voice_transcript": true], ["provenance": "client_reported"]] {
            let actions = pipeline.handleSSE(event: "message", data: ["turn_id": turn, "i": 17,
                         "kind": "agent", "text": "Provider transcript", "meta": meta])
            XCTAssertTrue(actions.isEmpty)
        }
    }

    func testResetInvalidatesPriorRunAndRegistrationIsBounded() {
        var pipeline = SessionTurnPipeline()
        for _ in 0..<200 { pipeline.registerSubmit(turnId: UUID().uuidString) }
        XCTAssertEqual(pipeline.registeredTurns.count, 128)
        pipeline.reset()
        XCTAssertTrue(pipeline.registeredTurns.isEmpty)
    }

    func testSSECursorCommitsOnlyCompleteFramesAndLiveDeltasHaveNoID() {
        var parser = SessionSSEParser()
        XCTAssertNil(parser.push(line: "id: 7"))
        XCTAssertNil(parser.lastEventID)
        _ = parser.push(line: "event: message")
        _ = parser.push(line: "data: {\"i\":17}")
        XCTAssertEqual(parser.push(line: "")?.id, "7")
        XCTAssertEqual(parser.lastEventID, "7")
        _ = parser.push(line: "event: delta")
        _ = parser.push(line: "data: {\"text\":\"live\"}")
        XCTAssertNil(parser.push(line: "")?.id)
        XCTAssertEqual(parser.lastEventID, "7")
    }

    func testPartialCannotSubmitThenRepeatAsFinalAndLongFinalIsNotTruncated() {
        XCTAssertNil(SpeechEndpointPolicy.shouldSubmit(finalizedText: "first", partialText: "unfinished",
                     silenceSeconds: 5, requiredSilence: 1))
        XCTAssertNil(SpeechEndpointPolicy.shouldSubmit(finalizedText: "", partialText: "only partial",
                     silenceSeconds: 5, requiredSilence: 1))
        let text = String(repeating: "long phrase ", count: 20).trimmingCharacters(in: .whitespaces)
        XCTAssertEqual(SpeechEndpointPolicy.shouldSubmit(finalizedText: text, partialText: "",
                       silenceSeconds: 5, requiredSilence: 1), text)
        XCTAssertTrue(SpokenStop.isStopCommand("stop"))
        XCTAssertTrue(SpokenStop.isStopCommand("Stop talking"))
        XCTAssertTrue(SpokenStop.isStopCommand("cancel that"))
        XCTAssertFalse(SpokenStop.isStopCommand("stop the kiosk and tell me why"))
        XCTAssertFalse(SpokenStop.isStopCommand("how many sessions"))
    }
}
