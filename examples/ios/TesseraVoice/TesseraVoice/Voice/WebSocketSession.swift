import Foundation

/// Pure enqueue policy shared by WebSocketSession and unit tests.
enum WebSocketOutboundPolicy {
    static func canEnqueue(messageBytes: Int, queuedBytes: Int, maxBufferedBytes: Int) -> Bool {
        if messageBytes <= 0 {
            return false
        }
        if messageBytes > maxBufferedBytes {
            return false
        }
        if queuedBytes < 0 || maxBufferedBytes < 0 {
            return false
        }
        return queuedBytes + messageBytes <= maxBufferedBytes
    }
}

/// Serial outbound WebSocket writer with a bounded in-flight byte window,
/// event_id dedupe, and a ping/receive activity watchdog.
final class WebSocketSession: @unchecked Sendable {
    private struct OutboundMessage {
        let text: String
        let byteCount: Int
    }

    private let task: URLSessionWebSocketTask
    private let queue = DispatchQueue(label: "com.example.tessera.voice.websocket")
    private let maxBufferedBytes: Int
    private let pingWatchdogSeconds: TimeInterval
    private let pingIntervalSeconds: TimeInterval

    private var sentIds = Set<String>()
    private var closed = false
    private var onEvent: (([String: Any]) -> Void)?
    private var onFail: ((String) -> Void)?
    private var outbound: [OutboundMessage] = []
    private var queuedBytes = 0
    private var sendInFlight = false
    private var lastActivity = Date()
    private var watchdogWorkItem: DispatchWorkItem?
    private var receiveArmed = false

    init(
        task: URLSessionWebSocketTask,
        maxBufferedBytes: Int = ProductManifest.defaultMaxWebSocketBufferedBytes,
        pingWatchdogSeconds: TimeInterval = ProductManifest.defaultPingWatchdogSeconds,
        pingIntervalSeconds: TimeInterval = 15
    ) {
        self.task = task
        self.maxBufferedBytes = maxBufferedBytes
        self.pingWatchdogSeconds = pingWatchdogSeconds
        self.pingIntervalSeconds = pingIntervalSeconds
    }

    func start(onEvent: @escaping ([String: Any]) -> Void, onFail: @escaping (String) -> Void) {
        queue.async {
            if self.closed {
                return
            }
            self.onEvent = onEvent
            self.onFail = onFail
            self.lastActivity = Date()
            self.task.resume()
            if self.receiveArmed == false {
                self.receiveArmed = true
                self.receiveLoop()
            }
            self.armWatchdogLocked()
        }
    }

    func sendJSONObject(_ object: [String: Any]) {
        queue.async {
            if self.closed {
                return
            }
            if let eventId = object["event_id"] as? String {
                if self.sentIds.contains(eventId) {
                    return
                }
                self.sentIds.insert(eventId)
                if self.sentIds.count > 4_096 {
                    self.sentIds.removeAll(keepingCapacity: true)
                }
            }
            do {
                let data = try JSONSerialization.data(withJSONObject: object, options: [])
                guard let text = String(data: data, encoding: .utf8) else {
                    self.failLocked("Voice disconnected")
                    return
                }
                self.enqueueLocked(text)
            } catch {
                self.failLocked("Voice disconnected")
            }
        }
    }

    func sendJSONValue(_ value: JSONValue) {
        if let object = value.objectValue {
            var plain: [String: Any] = [:]
            for (key, nested) in object {
                plain[key] = nested.toAny()
            }
            sendJSONObject(plain)
            return
        }
        queue.async {
            self.failLocked("Voice disconnected")
        }
    }

    func close() {
        queue.async {
            self.shutdownLocked(sendFail: nil)
        }
    }

    // MARK: - Writer

    private func enqueueLocked(_ text: String) {
        let byteCount = text.utf8.count
        if WebSocketOutboundPolicy.canEnqueue(
            messageBytes: byteCount,
            queuedBytes: queuedBytes,
            maxBufferedBytes: maxBufferedBytes
        ) == false {
            failLocked("Audio send queue full")
            return
        }
        outbound.append(OutboundMessage(text: text, byteCount: byteCount))
        queuedBytes += byteCount
        pumpLocked()
    }

    private func pumpLocked() {
        if closed {
            return
        }
        while outbound.isEmpty == false {
            let next = outbound.removeFirst()
            sendInFlight = true
            let startedAt = Date()
            // URLSession owns wire order. Completion latency must not serialize capture.
            task.send(.string(next.text)) { [weak self] error in
                self?.queue.async {
                    guard let self, self.closed == false else { return }
                    self.queuedBytes = max(0, self.queuedBytes - next.byteCount)
                    self.sendInFlight = self.queuedBytes > 0
                    if error != nil {
                        self.failLocked("Voice disconnected")
                        return
                    }
                    let elapsed = Date().timeIntervalSince(startedAt)
                    if elapsed > 0.5 {
                        NSLog("Voice send completion delayed: %.2f seconds; buffered=%d", elapsed, self.queuedBytes)
                    }
                    self.noteActivityLocked()
                }
            }
        }
    }

    // MARK: - Receive / ping

    private func receiveLoop() {
        task.receive { [weak self] result in
            guard let self else { return }
            self.queue.async {
                if self.closed {
                    return
                }
                switch result {
                case .failure:
                    self.failLocked("Connection closed")
                case .success(let message):
                    self.noteActivityLocked()
                    switch message {
                    case .string(let text):
                        self.handleTextLocked(text)
                        self.receiveLoop()
                    case .data:
                        self.failLocked("Unreadable event from provider")
                    @unknown default:
                        self.failLocked("Unreadable event from provider")
                    }
                }
            }
        }
    }

    private func handleTextLocked(_ text: String) {
        if text.count > maxBufferedBytes {
            failLocked("Unreadable event from provider")
            return
        }
        guard let data = text.data(using: .utf8) else {
            failLocked("Unreadable event from provider")
            return
        }
        guard let object = try? JSONSerialization.jsonObject(with: data) as? [String: Any] else {
            failLocked("Unreadable event from provider")
            return
        }
        let callback = onEvent
        // Leave the queue before client work so a slow handler cannot stall the writer.
        if let callback {
            DispatchQueue.main.async {
                callback(object)
            }
        }
    }

    private func armWatchdogLocked() {
        watchdogWorkItem?.cancel()
        let interval = min(max(5.0, pingIntervalSeconds), max(5.0, pingWatchdogSeconds / 3))
        let work = DispatchWorkItem { [weak self] in
            self?.watchdogTickLocked()
        }
        watchdogWorkItem = work
        queue.asyncAfter(deadline: .now() + interval, execute: work)
    }

    private func watchdogTickLocked() {
        if closed {
            return
        }
        let silence = Date().timeIntervalSince(lastActivity)
        if silence >= pingWatchdogSeconds {
            failLocked("No transport activity for 45 seconds")
            return
        }
        if silence >= pingIntervalSeconds && sendInFlight == false {
            task.sendPing { [weak self] error in
                self?.queue.async {
                    guard let self, self.closed == false else { return }
                    if error != nil {
                        self.failLocked("Transport ping failed")
                        return
                    }
                    self.noteActivityLocked()
                }
            }
        }
        armWatchdogLocked()
    }

    private func noteActivityLocked() {
        lastActivity = Date()
    }

    private func failLocked(_ detail: String) {
        if closed {
            return
        }
        NSLog("Voice transport stopped: %@; queued=%d; inFlight=%@; activityAge=%.2f", detail, queuedBytes, String(sendInFlight), Date().timeIntervalSince(lastActivity))
        let callback = onFail
        shutdownLocked(sendFail: nil)
        if let callback {
            DispatchQueue.global(qos: .userInitiated).async {
                callback(detail)
            }
        }
    }

    private func shutdownLocked(sendFail: String?) {
        if closed {
            return
        }
        closed = true
        watchdogWorkItem?.cancel()
        watchdogWorkItem = nil
        outbound.removeAll(keepingCapacity: false)
        queuedBytes = 0
        sendInFlight = false
        onEvent = nil
        let failCallback = onFail
        onFail = nil
        task.cancel(with: .goingAway, reason: nil)
        if let sendFail, let failCallback {
            DispatchQueue.global(qos: .userInitiated).async {
                failCallback(sendFail)
            }
        }
    }
}
