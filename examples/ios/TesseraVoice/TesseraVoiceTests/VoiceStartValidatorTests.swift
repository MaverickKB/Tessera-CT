import XCTest
@testable import TesseraVoice

final class VoiceStartValidatorTests: XCTestCase {
    func testAcceptsValidStartPayload() throws {
        let now = Date(timeIntervalSince1970: 1_700_000_000)
        let response = makeResponse(
            expiresAt: now.timeIntervalSince1970 + 60,
            sessionExpiresAt: now.timeIntervalSince1970 + 300,
            rate: 24_000
        )
        let validated = try VoiceStartValidator.validate(response, now: now)
        XCTAssertEqual(validated.voiceId, "abc123")
        XCTAssertEqual(validated.audioSampleRate, 24_000)
        XCTAssertEqual(validated.instructions, "hello instructions")
    }

    func testRejectsExpiredLease() {
        let now = Date(timeIntervalSince1970: 1_700_000_000)
        let response = makeResponse(
            expiresAt: now.timeIntervalSince1970 - 1,
            sessionExpiresAt: now.timeIntervalSince1970 + 300,
            rate: 24_000
        )
        XCTAssertThrowsError(try VoiceStartValidator.validate(response, now: now))
    }

    func testRejectsHTTPStyleWebsocket() {
        let now = Date(timeIntervalSince1970: 1_700_000_000)
        var response = makeResponse(
            expiresAt: now.timeIntervalSince1970 + 60,
            sessionExpiresAt: now.timeIntervalSince1970 + 300,
            rate: 24_000
        )
        response = VoiceStartResponse(
            voice_id: response.voice_id,
            websocket_url: "https://example.invalid/ws",
            protocols: response.protocols,
            expires_at: response.expires_at,
            session_expires_at: response.session_expires_at,
            max_session_seconds: response.max_session_seconds,
            audio_sample_rate: response.audio_sample_rate,
            audio_format: response.audio_format,
            session_update: response.session_update
        )
        XCTAssertThrowsError(try VoiceStartValidator.validate(response, now: now))
    }

    private func makeResponse(expiresAt: Double, sessionExpiresAt: Double, rate: Double) -> VoiceStartResponse {
        let session: [String: JSONValue] = [
            "instructions": .string("hello instructions"),
            "turn_detection": .object(["type": .string("server_vad")]),
            "audio": .object([
                "input": .object([
                    "format": .object([
                        "type": .string("audio/pcm"),
                        "rate": .number(rate)
                    ]),
                    "transport": .string("json")
                ]),
                "output": .object([
                    "format": .object([
                        "type": .string("audio/pcm"),
                        "rate": .number(rate)
                    ]),
                    "transport": .string("json")
                ])
            ])
        ]
        let sessionUpdate: JSONValue = .object([
            "type": .string("session.update"),
            "session": .object(session)
        ])
        return VoiceStartResponse(
            voice_id: "abc123",
            websocket_url: "wss://example.invalid/v1/realtime",
            protocols: ["realtime_v1_token"],
            expires_at: expiresAt,
            session_expires_at: sessionExpiresAt,
            max_session_seconds: 600,
            audio_sample_rate: rate,
            audio_format: "audio/pcm",
            session_update: sessionUpdate
        )
    }
}
