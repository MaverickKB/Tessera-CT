import Foundation

/// Buffer ownership defines whether playback is busy. Duration is only a queue bound.
struct VoicePlaybackState {
    private(set) var pendingBuffers = 0
    private(set) var seconds: TimeInterval = 0
    private(set) var generation = 0

    var isPlaying: Bool { pendingBuffers > 0 }

    mutating func schedule(duration: TimeInterval) {
        pendingBuffers += 1
        seconds += duration
    }

    mutating func complete(duration: TimeInterval, generation: Int) {
        guard generation == self.generation else { return }
        pendingBuffers = max(0, pendingBuffers - 1)
        if pendingBuffers == 0 {
            seconds = 0
        } else {
            seconds = max(0, seconds - duration)
        }
    }

    mutating func clear() {
        generation += 1
        pendingBuffers = 0
        seconds = 0
    }
}

/// Pure gates for CallKit audio timing. Kept free of UIKit/CallKit so unit tests can lock the rules.
enum CallAudioGate {
    /// Engine IO may start only after CallKit activates the audio session and a validated lease exists.
    static func shouldStartEngine(callKitAudioActive: Bool, hasValidatedLease: Bool) -> Bool {
        callKitAudioActive && hasValidatedLease
    }

    /// Mic tap arms only when protocol session.updated is ready and the engine is live under CallKit audio.
    static func shouldArmCapture(protocolReady: Bool, callKitAudioActive: Bool, engineStarted: Bool) -> Bool {
        protocolReady && callKitAudioActive && engineStarted
    }

    /// Connecting is reported when the system start action is accepted and protocol connect begins.
    /// Connected is reported only after matching session.updated (lease/socket readiness).
    static func shouldReportConnected(protocolReady: Bool, alreadyReported: Bool) -> Bool {
        protocolReady && alreadyReported == false
    }
}
