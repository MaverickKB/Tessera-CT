import AVFoundation
import Foundation

/// Observes route/interruption/media-reset. Category is configured for CallKit;
/// CallKit owns setActive / didActivate / didDeactivate.
final class AudioSessionController: @unchecked Sendable {
    private var observers: [NSObjectProtocol] = []
    private var onInterrupt: ((Bool, Bool) -> Void)?
    private var onMediaReset: (() -> Void)?
    private var onRouteChange: (() -> Void)?
    private var configured = false

    /// - Parameters:
    ///   - onInterrupt: `(began, shouldResume)`. `shouldResume` only matters when `began == false`.
    func configure(
        preferSpeaker: Bool,
        onInterrupt: @escaping (Bool, Bool) -> Void,
        onMediaReset: @escaping () -> Void,
        onRouteChange: @escaping () -> Void
    ) throws {
        self.onInterrupt = onInterrupt
        self.onMediaReset = onMediaReset
        self.onRouteChange = onRouteChange

        // Category + route only. Do not setActive — CallKit elevates and activates the session.
        try CallKitCallController.configureAudioSession(preferSpeaker: preferSpeaker)
        configured = true
        installObserversIfNeeded()
    }

    func applyRoute(preferSpeaker: Bool) throws {
        try CallKitCallController.applyRoute(preferSpeaker: preferSpeaker)
    }

    /// Re-assert category/route after an interruption end when CallKit still owns the call.
    /// Does not call setActive; the system / CallKit must already have the session active.
    func reassertCategory(preferSpeaker: Bool) throws {
        try CallKitCallController.configureAudioSession(preferSpeaker: preferSpeaker)
        configured = true
    }

    func teardownObservers() {
        for observer in observers {
            NotificationCenter.default.removeObserver(observer)
        }
        observers.removeAll()
        onInterrupt = nil
        onMediaReset = nil
        onRouteChange = nil
        configured = false
        // Do not setActive(false) here. CallKit deactivates via didDeactivate when the call ends.
    }

    private func installObserversIfNeeded() {
        if observers.isEmpty == false {
            return
        }
        let session = AVAudioSession.sharedInstance()
        let center = NotificationCenter.default
        observers.append(center.addObserver(
            forName: AVAudioSession.interruptionNotification,
            object: session,
            queue: nil
        ) { [weak self] note in
            self?.handleInterruption(note)
        })
        observers.append(center.addObserver(
            forName: AVAudioSession.mediaServicesWereResetNotification,
            object: nil,
            queue: nil
        ) { [weak self] _ in
            self?.onMediaReset?()
        })
        observers.append(center.addObserver(
            forName: AVAudioSession.routeChangeNotification,
            object: session,
            queue: nil
        ) { [weak self] _ in
            self?.onRouteChange?()
        })
    }

    private func handleInterruption(_ note: Notification) {
        guard let info = note.userInfo else {
            return
        }
        guard let typeValue = info[AVAudioSessionInterruptionTypeKey] as? UInt else {
            return
        }
        guard let type = AVAudioSession.InterruptionType(rawValue: typeValue) else {
            return
        }
        if type == .began {
            onInterrupt?(true, false)
            return
        }
        if type == .ended {
            var shouldResume = false
            if let optionValue = info[AVAudioSessionInterruptionOptionKey] as? UInt {
                let options = AVAudioSession.InterruptionOptions(rawValue: optionValue)
                shouldResume = options.contains(.shouldResume)
            }
            onInterrupt?(false, shouldResume)
        }
    }
}
