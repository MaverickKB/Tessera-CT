import AVFoundation
import Foundation
import Speech

final class OnDeviceSpeechRecognizer: @unchecked Sendable {
    enum Output: Sendable {
        case partial(String)
        case finalizedSegment(String)
        case failed(String)
    }
    private let queue = DispatchQueue(label: "com.example.tessera.voice.speech")
    private var generation = UUID()
    private var started = false
    private var running = false
    private var analyzer: SpeechAnalyzer?
    private var inputBuilder: AsyncStream<AnalyzerInput>.Continuation?
    private var analyzerFormat: AVAudioFormat?
    private var sourceFormat: AVAudioFormat?
    private var audioConverter: AVAudioConverter?
    private var resultsTask: Task<Void, Never>?
    private var analyzeTask: Task<Void, Never>?
    private var onOutput: (@Sendable (Output) -> Void)?

    static func ensureReady() async -> SpeechReadiness {
        guard SpeechTranscriber.isAvailable else {
            return .unavailable("On-device speech unavailable on this device")
        }
        guard let locale = await SpeechTranscriber.supportedLocale(equivalentTo: Locale.current) else {
            return .unavailable("On-device speech has no locale for this device")
        }
        let probe = SpeechTranscriber(locale: locale, preset: .progressiveTranscription)
        let status = await AssetInventory.status(forModules: [probe])
        if status == .installed {
            return .ready
        }
        if status == .unsupported {
            return .unavailable("On-device speech assets unsupported")
        }
        do {
            if let request = try await AssetInventory.assetInstallationRequest(supporting: [probe]) {
                try await request.downloadAndInstall()
            }
            let after = await AssetInventory.status(forModules: [probe])
            if after == .installed {
                return .ready
            }
            return .unavailable("On-device speech assets not installed")
        } catch {
            return .unavailable("On-device speech install failed")
        }
    }

    func start(onOutput: @escaping @Sendable (Output) -> Void) async throws {
        let token: UUID? = queue.sync {
            if started { return nil }
            started = true
            return generation
        }
        guard let token else { return }
        guard SpeechTranscriber.isAvailable,
              let locale = await SpeechTranscriber.supportedLocale(equivalentTo: Locale.current) else {
            throw SpeechRecognizerError.unavailable
        }
        let transcriber = SpeechTranscriber(locale: locale, preset: .progressiveTranscription)
        guard await AssetInventory.status(forModules: [transcriber]) == .installed else {
            throw SpeechRecognizerError.assetsMissing
        }
        guard let format = await SpeechAnalyzer.bestAvailableAudioFormat(compatibleWith: [transcriber]) else {
            throw SpeechRecognizerError.formatUnavailable
        }
        let (stream, builder) = AsyncStream.makeStream(of: AnalyzerInput.self, bufferingPolicy: .bufferingOldest(24))
        let analyzer = SpeechAnalyzer(modules: [transcriber], options: SpeechAnalyzer.Options(priority: .userInitiated, modelRetention: .whileInUse))
        try await analyzer.prepareToAnalyze(in: format)
        let installed = queue.sync {
            guard generation == token, Task.isCancelled == false else { return false }
            self.analyzer = analyzer
            self.analyzerFormat = format
            self.inputBuilder = builder
            self.onOutput = onOutput
            self.running = true
            self.resultsTask = Task.detached { [weak self] in
                do {
                    for try await result in transcriber.results {
                        if Task.isCancelled { return }
                        let text = String(result.text.characters)
                        if text.isEmpty { continue }
                        let output: Output = result.isFinal ? .finalizedSegment(text) : .partial(text)
                        self?.emit(output, token: token)
                    }
                } catch {
                    if Task.isCancelled == false { self?.emit(.failed("Speech recognition failed"), token: token) }
                }
            }
            self.analyzeTask = Task.detached { [weak self] in
                do { _ = try await analyzer.analyzeSequence(stream) }
                catch {
                    if Task.isCancelled == false { self?.emit(.failed("Speech analysis failed"), token: token) }
                }
            }
            return true
        }
        if installed == false {
            builder.finish()
            await analyzer.cancelAndFinishNow()
            throw CancellationError()
        }
    }

    func appendCaptureBuffer(_ buffer: AVAudioPCMBuffer) {
        queue.sync {
            guard running, let format = analyzerFormat, let builder = inputBuilder else { return }
            do {
                let converted = try convert(buffer, to: format)
                if converted.frameLength == 0 { return }
                if case .dropped = builder.yield(AnalyzerInput(buffer: converted)) {
                    onOutput?(.failed("Speech input queue full"))
                }
            } catch {
                onOutput?(.failed("Speech input conversion failed"))
            }
        }
    }

    func stop() {
        queue.sync {
            generation = UUID()
            running = false
            onOutput = nil
            inputBuilder?.finish()
            inputBuilder = nil
            resultsTask?.cancel()
            analyzeTask?.cancel()
            resultsTask = nil
            analyzeTask = nil
            if let analyzer {
                Task.detached { await analyzer.cancelAndFinishNow() }
            }
            analyzer = nil
            analyzerFormat = nil
            sourceFormat = nil
            audioConverter = nil
        }
    }

    private func emit(_ output: Output, token: UUID) {
        queue.async {
            guard self.generation == token, self.running else { return }
            self.onOutput?(output)
        }
    }

    private func convert(_ source: AVAudioPCMBuffer, to format: AVAudioFormat) throws -> AVAudioPCMBuffer {
        if source.format.sampleRate == format.sampleRate,
           source.format.commonFormat == format.commonFormat,
           source.format.channelCount == format.channelCount {
            return source
        }
        if sourceFormat != source.format {
            guard let built = AVAudioConverter(from: source.format, to: format) else {
                throw SpeechRecognizerError.formatUnavailable
            }
            audioConverter = built
            sourceFormat = source.format
        }
        guard let converter = audioConverter else {
            throw SpeechRecognizerError.formatUnavailable
        }
        let ratio = format.sampleRate / max(source.format.sampleRate, 1)
        let capacity = AVAudioFrameCount(Double(source.frameLength) * ratio) + 32
        guard let outBuffer = AVAudioPCMBuffer(pcmFormat: format, frameCapacity: capacity) else {
            throw SpeechRecognizerError.formatUnavailable
        }
        var consumed = false
        let inputBlock: AVAudioConverterInputBlock = { _, outStatus in
            if consumed {
                outStatus.pointee = .noDataNow
                return nil
            }
            consumed = true
            outStatus.pointee = .haveData
            return source
        }
        var error: NSError?
        let status = converter.convert(to: outBuffer, error: &error, withInputFrom: inputBlock)
        if status == .error || error != nil {
            throw SpeechRecognizerError.formatUnavailable
        }
        return outBuffer
    }

}

enum SpeechRecognizerError: LocalizedError {
    case unavailable
    case assetsMissing
    case formatUnavailable

    var errorDescription: String? {
        switch self {
        case .unavailable:
            return "On-device speech unavailable"
        case .assetsMissing:
            return "On-device speech assets not ready"
        case .formatUnavailable:
            return "On-device speech format unavailable"
        }
    }
}
