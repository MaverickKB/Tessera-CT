import XCTest
@testable import TesseraVoice

final class PCMCodecTests: XCTestCase {
    func testRoundTripPCM16() throws {
        let original: [Float] = [0, 0.5, -0.5, 1.0, -1.0]
        let encoded = PCMCodec.base64PCM16LE(fromFloatSamples: original)
        let decoded = try PCMCodec.pcm16LEBase64ToFloat(encoded)
        XCTAssertEqual(decoded.count, original.count)
        for index in 0..<original.count {
            XCTAssertEqual(decoded[index], original[index], accuracy: 0.01)
        }
    }

    func testRejectsOddByteLength() {
        // one byte after base64 decode is invalid PCM16
        let bad = Data([0x00]).base64EncodedString()
        XCTAssertThrowsError(try PCMCodec.pcm16LEBase64ToFloat(bad))
    }

    func testResampleLength() {
        let source = [Float](repeating: 0.25, count: 240)
        let output = PCMCodec.resample(samples: source, sourceRate: 24_000, targetRate: 48_000)
        XCTAssertEqual(output.count, 480)
    }
}
