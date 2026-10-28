import XCTest
@testable import TesseraVoice

/// Pure-policy checks for runtime bounds the live socket/audio stack relies on.
final class RuntimeContractTests: XCTestCase {
    func testWebSocketEnqueueRejectsWhenMessageExceedsCap() {
        let maxBytes = 1_024
        XCTAssertFalse(WebSocketOutboundPolicy.canEnqueue(messageBytes: maxBytes + 1, queuedBytes: 0, maxBufferedBytes: maxBytes))
        XCTAssertTrue(WebSocketOutboundPolicy.canEnqueue(messageBytes: maxBytes, queuedBytes: 0, maxBufferedBytes: maxBytes))
    }

    func testWebSocketEnqueueRejectsWhenQueueWouldExceedCap() {
        let maxBytes = 1_024
        XCTAssertFalse(WebSocketOutboundPolicy.canEnqueue(messageBytes: 200, queuedBytes: 900, maxBufferedBytes: maxBytes))
        XCTAssertTrue(WebSocketOutboundPolicy.canEnqueue(messageBytes: 124, queuedBytes: 900, maxBufferedBytes: maxBytes))
        XCTAssertFalse(WebSocketOutboundPolicy.canEnqueue(messageBytes: 0, queuedBytes: 0, maxBufferedBytes: maxBytes))
    }

    func testSleepCancellationMustNotContinue() async {
        let task = Task {
            do {
                try await Task.sleep(nanoseconds: 1_000_000_000)
                return "continued"
            } catch {
                return "cancelled"
            }
        }
        task.cancel()
        let result = await task.value
        XCTAssertEqual(result, "cancelled")
    }
}
