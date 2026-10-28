import XCTest
@testable import TesseraVoice

final class CallAudioGateTests: XCTestCase {
    func testDrainedPlaybackReopensCaptureDespiteDurationRounding() {
        let residual = (0.1 + 0.2) - 0.1 - 0.2
        XCTAssertGreaterThan(residual, 0, "The prior duration gate could remain busy forever")
        var playback = VoicePlaybackState()
        playback.schedule(duration: 0.1)
        playback.schedule(duration: 0.2)
        let generation = playback.generation
        playback.complete(duration: 0.1, generation: generation)
        XCTAssertTrue(playback.isPlaying)
        playback.complete(duration: 0.2, generation: generation)
        XCTAssertFalse(playback.isPlaying)
        XCTAssertEqual(playback.seconds, 0)
    }

    func testClearedPlaybackIgnoresLateCompletionFromOldBuffers() {
        var playback = VoicePlaybackState()
        playback.schedule(duration: 0.5)
        let previous = playback.generation
        playback.clear()
        playback.schedule(duration: 0.3)
        playback.complete(duration: 0.5, generation: previous)
        XCTAssertTrue(playback.isPlaying)
        XCTAssertEqual(playback.pendingBuffers, 1)
        XCTAssertEqual(playback.seconds, 0.3)
        playback.complete(duration: 0.3, generation: playback.generation)
        XCTAssertFalse(playback.isPlaying)
    }

    func testEngineWaitsForCallKitActivationAndLease() {
        XCTAssertFalse(CallAudioGate.shouldStartEngine(callKitAudioActive: false, hasValidatedLease: true))
        XCTAssertFalse(CallAudioGate.shouldStartEngine(callKitAudioActive: true, hasValidatedLease: false))
        XCTAssertTrue(CallAudioGate.shouldStartEngine(callKitAudioActive: true, hasValidatedLease: true))
    }

    func testCaptureArmsOnlyWhenProtocolAndAudioAreReady() {
        XCTAssertFalse(CallAudioGate.shouldArmCapture(protocolReady: false, callKitAudioActive: true, engineStarted: true))
        XCTAssertFalse(CallAudioGate.shouldArmCapture(protocolReady: true, callKitAudioActive: false, engineStarted: true))
        XCTAssertFalse(CallAudioGate.shouldArmCapture(protocolReady: true, callKitAudioActive: true, engineStarted: false))
        XCTAssertTrue(CallAudioGate.shouldArmCapture(protocolReady: true, callKitAudioActive: true, engineStarted: true))
    }

    func testConnectedReportedOnceAtProtocolReadiness() {
        XCTAssertFalse(CallAudioGate.shouldReportConnected(protocolReady: false, alreadyReported: false))
        XCTAssertTrue(CallAudioGate.shouldReportConnected(protocolReady: true, alreadyReported: false))
        XCTAssertFalse(CallAudioGate.shouldReportConnected(protocolReady: true, alreadyReported: true))
    }
}
