import Foundation
import Accelerate

enum PCMCodec {
    /// Convert mono Float32 samples in [-1, 1] to little-endian PCM16 bytes.
    static func floatToPCM16LE(_ samples: [Float]) -> Data {
        var data = Data(count: samples.count * 2)
        data.withUnsafeMutableBytes { raw in
            let destination = raw.bindMemory(to: Int16.self)
            var index = 0
            while index < samples.count {
                let clipped = max(-1.0, min(1.0, samples[index]))
                let scaled = clipped * 32767.0
                destination[index] = Int16(scaled.rounded())
                index += 1
            }
        }
        return data
    }

    /// Decode base64 PCM16LE mono into Float32 samples.
    static func pcm16LEBase64ToFloat(_ encoded: String) throws -> [Float] {
        guard let data = Data(base64Encoded: encoded) else {
            throw PCMCodecError.invalidBase64
        }
        if data.isEmpty || data.count % 2 != 0 {
            throw PCMCodecError.invalidPCM
        }
        let sampleCount = data.count / 2
        var samples = [Float](repeating: 0, count: sampleCount)
        data.withUnsafeBytes { raw in
            let source = raw.bindMemory(to: Int16.self)
            var index = 0
            while index < sampleCount {
                samples[index] = Float(source[index]) / 32768.0
                index += 1
            }
        }
        return samples
    }

    static func base64PCM16LE(fromFloatSamples samples: [Float]) -> String {
        let data = floatToPCM16LE(samples)
        return data.base64EncodedString()
    }

    /// Linear resample mono float audio from sourceRate to targetRate.
    static func resample(samples: [Float], sourceRate: Double, targetRate: Double) -> [Float] {
        if samples.isEmpty {
            return []
        }
        if abs(sourceRate - targetRate) < 0.01 {
            return samples
        }
        if sourceRate <= 0 || targetRate <= 0 {
            return samples
        }

        let ratio = targetRate / sourceRate
        let outputCount = max(1, Int((Double(samples.count) * ratio).rounded()))
        var output = [Float](repeating: 0, count: outputCount)
        var index = 0
        while index < outputCount {
            let sourcePosition = Double(index) / ratio
            let leftIndex = Int(sourcePosition)
            let rightIndex = min(leftIndex + 1, samples.count - 1)
            let fraction = Float(sourcePosition - Double(leftIndex))
            let left = samples[max(0, min(leftIndex, samples.count - 1))]
            let right = samples[rightIndex]
            output[index] = left + (right - left) * fraction
            index += 1
        }
        return output
    }
}

enum PCMCodecError: Error {
    case invalidBase64
    case invalidPCM
}
