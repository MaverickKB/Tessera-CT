import AVFoundation
import CallKit
import Foundation

/// Events the voice coordinator must handle for one outgoing CallKit call.
@MainActor
protocol CallKitCallControllerDelegate: AnyObject {
    func callKitStartAccepted(callUUID: UUID)
    func callKitStartRejected(callUUID: UUID, detail: String)
    func callKitEndRequested(callUUID: UUID)
    func callKitMuteRequested(callUUID: UUID, muted: Bool)
    func callKitAudioSessionDidActivate(callUUID: UUID)
    func callKitAudioSessionDidDeactivate(callUUID: UUID)
    func callKitProviderDidReset()
}

/// Owns CXProvider + CXCallController for one outgoing Tessera voice call.
/// CallKit owns audio-session activation; this type only configures and reports.
final class CallKitCallController: NSObject {
    private let provider: CXProvider
    private let controller = CXCallController()
    private let providerQueue = DispatchQueue(label: "com.example.tessera.voice.callkit")

    weak var delegate: CallKitCallControllerDelegate?

    private(set) var activeCallUUID: UUID?
    private var endingCallUUIDs = Set<UUID>()

    override init() {
        let configuration = CXProviderConfiguration()
        configuration.supportsVideo = false
        configuration.maximumCallGroups = 1
        configuration.maximumCallsPerCallGroup = 1
        configuration.supportedHandleTypes = [.generic]
        configuration.includesCallsInRecents = false
        provider = CXProvider(configuration: configuration)
        super.init()
        provider.setDelegate(self, queue: providerQueue)
    }

    /// Request an outgoing call. Returns the call UUID immediately so perform-start
    /// can match before the async transaction completion runs.
    /// - Parameter completion: nil error means the transaction was accepted by the system.
    func requestStart(handleValue: String, completion: @escaping (String?) -> Void) -> UUID? {
        if activeCallUUID != nil {
            completion("A call is already active")
            return nil
        }

        let callUUID = UUID()
        activeCallUUID = callUUID
        let handle = CXHandle(type: .generic, value: handleValue)
        let action = CXStartCallAction(call: callUUID, handle: handle)
        action.isVideo = false
        let transaction = CXTransaction(action: action)
        controller.request(transaction) { [weak self] error in
            if let error {
                if self?.activeCallUUID == callUUID {
                    self?.activeCallUUID = nil
                }
                let failure = error as NSError
                NSLog("System call start rejected: %@ code=%d", failure.domain, failure.code)
                completion("System call rejected (\(failure.code))")
                return
            }
            completion(nil)
        }
        return callUUID
    }

    /// Request mute/unmute so AirPods and in-app mute share one CallKit action path.
    func requestMute(_ muted: Bool, callUUID: UUID, completion: ((String?) -> Void)? = nil) {
        guard activeCallUUID == callUUID else {
            completion?("No matching CallKit call")
            return
        }
        let action = CXSetMutedCallAction(call: callUUID, muted: muted)
        let transaction = CXTransaction(action: action)
        controller.request(transaction) { error in
            completion?(error?.localizedDescription)
        }
    }

    /// Request a normal hang-up through CallKit.
    func requestEnd(callUUID: UUID, completion: ((String?) -> Void)? = nil) {
        guard activeCallUUID == callUUID else {
            completion?("No matching CallKit call")
            return
        }
        if endingCallUUIDs.contains(callUUID) {
            completion?(nil)
            return
        }
        endingCallUUIDs.insert(callUUID)
        let action = CXEndCallAction(call: callUUID)
        let transaction = CXTransaction(action: action)
        controller.request(transaction) { [weak self] error in
            if let error {
                self?.endingCallUUIDs.remove(callUUID)
                completion?(error.localizedDescription)
                return
            }
            completion?(nil)
        }
    }

    func reportConnecting(callUUID: UUID, at date: Date = Date()) {
        provider.reportOutgoingCall(with: callUUID, startedConnectingAt: date)
    }

    func reportConnected(callUUID: UUID, at date: Date = Date()) {
        provider.reportOutgoingCall(with: callUUID, connectedAt: date)
    }

    /// End the system call without going through CXEndCallAction (protocol failure, reset).
    func reportEnded(callUUID: UUID, reason: CXCallEndedReason) {
        provider.reportCall(with: callUUID, endedAt: Date(), reason: reason)
        if activeCallUUID == callUUID {
            activeCallUUID = nil
        }
        endingCallUUIDs.remove(callUUID)
    }

    func clearActiveCall(callUUID: UUID?) {
        if let callUUID, activeCallUUID == callUUID {
            activeCallUUID = nil
        }
        if let callUUID {
            endingCallUUIDs.remove(callUUID)
        }
    }

    /// Category/mode only. CallKit activates the session later via didActivate.
    static func configureAudioSession(preferSpeaker: Bool) throws {
        let session = AVAudioSession.sharedInstance()
        try session.setCategory(
            .playAndRecord,
            mode: .voiceChat,
            options: [.allowBluetoothHFP]
        )
        _ = preferSpeaker
    }

    static func applyRoute(preferSpeaker: Bool) throws {
        let session = AVAudioSession.sharedInstance()
        var hasHeadphones = false
        for output in session.currentRoute.outputs {
            switch output.portType {
            case .bluetoothHFP, .bluetoothA2DP, .bluetoothLE, .headphones, .usbAudio:
                hasHeadphones = true
            default:
                break
            }
        }
        if hasHeadphones {
            try session.overrideOutputAudioPort(.none)
        } else if preferSpeaker {
            try session.overrideOutputAudioPort(.speaker)
        } else {
            try session.overrideOutputAudioPort(.none)
        }
    }
}

extension CallKitCallController: CXProviderDelegate {
    func providerDidReset(_ provider: CXProvider) {
        activeCallUUID = nil
        endingCallUUIDs.removeAll()
        Task { @MainActor in
            self.delegate?.callKitProviderDidReset()
        }
    }

    func provider(_ provider: CXProvider, perform action: CXStartCallAction) {
        let callUUID = action.callUUID
        do {
            // Prefer-speaker is applied again by the coordinator with the live setting.
            try CallKitCallController.configureAudioSession(preferSpeaker: false)
            action.fulfill()
            Task { @MainActor in
                self.delegate?.callKitStartAccepted(callUUID: callUUID)
            }
        } catch {
            action.fail()
            activeCallUUID = nil
            Task { @MainActor in
                self.delegate?.callKitStartRejected(
                    callUUID: callUUID,
                    detail: "Audio session configure failed"
                )
            }
        }
    }

    func provider(_ provider: CXProvider, perform action: CXEndCallAction) {
        let callUUID = action.callUUID
        action.fulfill()
        if activeCallUUID == callUUID {
            activeCallUUID = nil
        }
        endingCallUUIDs.remove(callUUID)
        Task { @MainActor in
            self.delegate?.callKitEndRequested(callUUID: callUUID)
        }
    }

    func provider(_ provider: CXProvider, perform action: CXSetMutedCallAction) {
        let callUUID = action.callUUID
        let muted = action.isMuted
        // Fulfill after the coordinator applies mute so CallKit state matches mic state.
        Task { @MainActor in
            self.delegate?.callKitMuteRequested(callUUID: callUUID, muted: muted)
            NSLog("Native call mic mute changed: %@", String(muted))
            action.fulfill()
        }
    }

    func provider(_ provider: CXProvider, perform action: CXSetHeldCallAction) {
        // Outgoing Tessera voice does not support hold; fail closed so the system does not claim hold.
        action.fail()
    }

    func provider(_ provider: CXProvider, didActivate audioSession: AVAudioSession) {
        let callUUID = activeCallUUID
        Task { @MainActor in
            guard let callUUID else { return }
            self.delegate?.callKitAudioSessionDidActivate(callUUID: callUUID)
        }
    }

    func provider(_ provider: CXProvider, didDeactivate audioSession: AVAudioSession) {
        let callUUID = activeCallUUID
        Task { @MainActor in
            guard let callUUID else { return }
            self.delegate?.callKitAudioSessionDidDeactivate(callUUID: callUUID)
        }
    }
}
