import Foundation

/// Single source of product identity and tunables.
/// Templates, project settings, and runtime defaults read from here only.
enum ProductManifest {
    static let productName = "Tessera"
    static let displayName = "Tessera"
    /// Product wordmark beside the tree art. Adopters name their own companion.
    static let wordmark = "Tessera-CT"
    /// Signed bundle id. The prefix is PRODUCT_BUNDLE_IDENTIFIER_PREFIX in Signing.xcconfig.
    static var bundleIdentifier: String {
        Bundle.main.bundleIdentifier ?? "com.example.tessera.voice"
    }
    static let version = "0.1.1"
    static let buildNumber = "7"
    static let minimumIOSVersion = "17.0"

    /// Example only. The app ships with an empty server URL.
    static let exampleServerURL = "http://localhost:8741"

    static let defaultConnectTimeoutSeconds: TimeInterval = 20
    static let defaultMaxPlaybackSeconds: TimeInterval = 15
    static let defaultMaxPendingBridgeEvents = 32
    static let defaultMaxCaptureQueueChunks = 24
    static let defaultChunkFrames = 2048
    static let defaultMaxWebSocketBufferedBytes = 262_144
    static let defaultPingWatchdogSeconds: TimeInterval = 45
    static let fallbackAudioSampleRate = 24_000

    static let microphoneUsageDescription =
        "Tessera needs the microphone for an active voice session you start while unlocked."
    static let localNetworkUsageDescription =
        "Tessera reaches your private server on the local network or private VPN."
}
