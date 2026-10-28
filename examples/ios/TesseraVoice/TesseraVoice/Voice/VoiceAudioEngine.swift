import AVFoundation
import Foundation

/// Single AVAudioEngine for capture + playback so voice processing has an output reference.
/// Voice processing is enabled before the input format is inspected.
final class VoiceAudioEngine: @unchecked Sendable {
    private let engine = AVAudioEngine()
    private let player = AVAudioPlayerNode()
    private let sampleRate: Double
    private let chunkFrames: Int
    private let maxQueueChunks: Int
    private let maxPlaybackSeconds: TimeInterval
    private let maxPendingBuffers: Int
    private let queue = DispatchQueue(label: "com.example.tessera.voice.audio")
    private let captureSlots = DispatchSemaphore(value: ProductManifest.defaultMaxCaptureQueueChunks)

    private var converter: AVAudioConverter?
    private var targetFormat: AVAudioFormat?
    private var playerFormat: AVAudioFormat?
    private var pendingChunks: [Data] = []
    private var pcmCarry = Data()
    private var muted = false
    private var running = false
    private var captureArmed = false
    private var playerStarted = false
    private var playback = VoicePlaybackState()
    private var onChunk: ((Data) -> Void)?
    private var onFail: ((String) -> Void)?
    private var drainScheduled = false
    private var tapInstalled = false

    var isRunning: Bool {
        queue.sync { running && engine.isRunning }
    }

    /// True while scheduled speech is still audible. Capture sends silence then, so
    /// the transcriber does not hear Tessera and hand it back as a user turn.
    var isPlayingBack: Bool {
        queue.sync { playback.isPlaying }
    }

    init(
        sampleRate: Double,
        chunkFrames: Int = ProductManifest.defaultChunkFrames,
        maxQueueChunks: Int = ProductManifest.defaultMaxCaptureQueueChunks,
        maxPlaybackSeconds: TimeInterval = ProductManifest.defaultMaxPlaybackSeconds,
        maxPendingBuffers: Int = ProductManifest.defaultMaxPendingBridgeEvents
    ) {
        self.sampleRate = sampleRate
        self.chunkFrames = chunkFrames
        self.maxQueueChunks = maxQueueChunks
        self.maxPlaybackSeconds = maxPlaybackSeconds
        self.maxPendingBuffers = maxPendingBuffers
    }

    /// Prepares the shared engine, enables voice processing first, then starts IO.
    /// Does not install the mic tap until `armCapture` so session.updated still gates streaming.
    func start(onFail: @escaping (String) -> Void) throws {
        try queue.sync {
            self.onFail = onFail
            if running {
                return
            }

            engine.reset()
            if player.engine == nil {
                engine.attach(player)
            }

            guard let playFormat = AVAudioFormat(
                commonFormat: .pcmFormatFloat32,
                sampleRate: sampleRate,
                channels: 1,
                interleaved: false
            ) else {
                throw AudioEngineError.formatUnavailable
            }
            playerFormat = playFormat
            engine.connect(player, to: engine.mainMixerNode, format: playFormat)
            engine.mainMixerNode.outputVolume = 1.0

            let input = engine.inputNode
            // Echo cancellation needs the output graph as reference: enable VP before reading format.
            do {
                try input.setVoiceProcessingEnabled(true)
            } catch {
                throw AudioEngineError.voiceProcessingUnavailable
            }

            // After VP is enabled, the node output format is the processed route format.
            let routeFormat = input.outputFormat(forBus: 0)
            if routeFormat.sampleRate <= 0 || routeFormat.channelCount < 1 {
                throw AudioEngineError.formatUnavailable
            }

            guard let pcmFormat = AVAudioFormat(
                commonFormat: .pcmFormatInt16,
                sampleRate: sampleRate,
                channels: 1,
                interleaved: true
            ) else {
                throw AudioEngineError.formatUnavailable
            }
            guard let builtConverter = AVAudioConverter(from: routeFormat, to: pcmFormat) else {
                throw AudioEngineError.converterUnavailable
            }
            converter = builtConverter
            targetFormat = pcmFormat

            engine.prepare()
            do {
                try engine.start()
            } catch {
                teardownLocked(keepCallbacks: true)
                throw AudioEngineError.startFailed
            }
            player.play()
            playerStarted = true
            running = true
        }
    }

    func armCapture(onChunk: @escaping (Data) -> Void, onFail: @escaping (String) -> Void) throws {
        try queue.sync {
            self.onChunk = onChunk
            self.onFail = onFail
            guard running, engine.isRunning else {
                throw AudioEngineError.startFailed
            }
            if captureArmed {
                return
            }

            let input = engine.inputNode
            let routeFormat = input.outputFormat(forBus: 0)
            if routeFormat.sampleRate <= 0 || routeFormat.channelCount < 1 {
                throw AudioEngineError.formatUnavailable
            }
            guard let pcmFormat = targetFormat else {
                throw AudioEngineError.formatUnavailable
            }
            // Rebuild converter against the live processed route format after VP + engine start.
            guard let builtConverter = AVAudioConverter(from: routeFormat, to: pcmFormat) else {
                throw AudioEngineError.converterUnavailable
            }
            converter = builtConverter
            pcmCarry.removeAll(keepingCapacity: true)

            let framesPerBuffer = AVAudioFrameCount(max(chunkFrames, Int(routeFormat.sampleRate / 25)))
            input.installTap(onBus: 0, bufferSize: framesPerBuffer, format: routeFormat) { [weak self] buffer, _ in
                self?.handleCaptureBuffer(buffer)
            }
            tapInstalled = true
            captureArmed = true
            scheduleDrainLocked()
        }
    }

    func setMuted(_ value: Bool) {
        queue.async {
            self.muted = value
            self.pendingChunks.removeAll(keepingCapacity: true)
            self.pcmCarry.removeAll(keepingCapacity: true)
        }
    }

    func enqueueBase64PCM16(_ encoded: String) {
        queue.async {
            do {
                let samples = try PCMCodec.pcm16LEBase64ToFloat(encoded)
                self.enqueueSamplesLocked(samples)
            } catch {
                self.failLocked("Unreadable event from provider")
            }
        }
    }

    func clearPlayback() {
        queue.async {
            self.player.stop()
            if self.running && self.playerStarted {
                self.player.play()
            }
            self.playback.clear()
        }
    }

    /// Pause IO for an audio interruption without dropping the engine object.
    func suspendForInterruption() {
        queue.sync {
            if running == false {
                return
            }
            if tapInstalled {
                engine.inputNode.removeTap(onBus: 0)
                tapInstalled = false
            }
            player.stop()
            engine.pause()
            pendingChunks.removeAll(keepingCapacity: true)
            pcmCarry.removeAll(keepingCapacity: true)
            playback.clear()
        }
    }

    /// Rebuild/reactivate after interruption end. Throws if the engine cannot run again.
    /// Does not activate AVAudioSession — CallKit owns session activation.
    func resumeAfterInterruption() throws {
        try queue.sync {
            guard running else {
                throw AudioEngineError.startFailed
            }

            if engine.isRunning == false {
                do {
                    try engine.start()
                } catch {
                    throw AudioEngineError.startFailed
                }
            }
            if playerStarted {
                player.play()
            }
            if captureArmed && tapInstalled == false {
                let input = engine.inputNode
                let routeFormat = input.outputFormat(forBus: 0)
                guard let pcmFormat = targetFormat,
                      let builtConverter = AVAudioConverter(from: routeFormat, to: pcmFormat) else {
                    throw AudioEngineError.converterUnavailable
                }
                converter = builtConverter
                let framesPerBuffer = AVAudioFrameCount(max(chunkFrames, Int(routeFormat.sampleRate / 25)))
                input.installTap(onBus: 0, bufferSize: framesPerBuffer, format: routeFormat) { [weak self] buffer, _ in
                    self?.handleCaptureBuffer(buffer)
                }
                tapInstalled = true
                scheduleDrainLocked()
            }
            if engine.isRunning == false {
                throw AudioEngineError.startFailed
            }
        }
    }

    func stop() {
        queue.sync {
            teardownLocked(keepCallbacks: false)
        }
    }

    private func teardownLocked(keepCallbacks: Bool) {
        running = false
        captureArmed = false
        playerStarted = false
        pendingChunks.removeAll()
        pcmCarry.removeAll()
        playback.clear()
        if keepCallbacks == false {
            onChunk = nil
            onFail = nil
        }
        if tapInstalled {
            engine.inputNode.removeTap(onBus: 0)
            tapInstalled = false
        }
        player.stop()
        if engine.isRunning {
            engine.stop()
        }
        converter = nil
        do {
            try engine.inputNode.setVoiceProcessingEnabled(false)
        } catch {
            // Best-effort disable on teardown.
        }
    }

    private func handleCaptureBuffer(_ source: AVAudioPCMBuffer) {
        guard captureSlots.wait(timeout: .now()) == .success else {
            queue.async { self.failLocked("Audio capture queue full") }
            return
        }
        guard let buffer = AVAudioPCMBuffer(pcmFormat: source.format, frameCapacity: source.frameLength) else {
            captureSlots.signal()
            return
        }
        buffer.frameLength = source.frameLength
        let inputBuffers = UnsafeMutableAudioBufferListPointer(UnsafeMutablePointer(mutating: source.audioBufferList))
        let outputBuffers = UnsafeMutableAudioBufferListPointer(buffer.mutableAudioBufferList)
        for index in 0..<inputBuffers.count {
            if let input = inputBuffers[index].mData, let output = outputBuffers[index].mData {
                memcpy(output, input, Int(inputBuffers[index].mDataByteSize))
                outputBuffers[index].mDataByteSize = inputBuffers[index].mDataByteSize
            }
        }
        queue.async {
            defer { self.captureSlots.signal() }
            guard self.running, self.captureArmed, let converter = self.converter, let targetFormat = self.targetFormat else {
                return
            }
            let ratio = targetFormat.sampleRate / max(buffer.format.sampleRate, 1)
            let capacity = AVAudioFrameCount(Double(buffer.frameLength) * ratio) + 32
            guard let outBuffer = AVAudioPCMBuffer(pcmFormat: targetFormat, frameCapacity: capacity) else {
                return
            }

            var consumed = false
            let inputBlock: AVAudioConverterInputBlock = { _, outStatus in
                if consumed {
                    outStatus.pointee = .noDataNow
                    return nil
                }
                consumed = true
                outStatus.pointee = .haveData
                return buffer
            }

            var error: NSError?
            let status = converter.convert(to: outBuffer, error: &error, withInputFrom: inputBlock)
            if status == .error || error != nil {
                self.failLocked("Microphone unavailable")
                return
            }
            if outBuffer.frameLength == 0 {
                return
            }
            guard let channel = outBuffer.int16ChannelData?[0] else {
                return
            }
            let byteCount = Int(outBuffer.frameLength) * MemoryLayout<Int16>.size
            var payload = Data(bytes: channel, count: byteCount)
            if self.muted {
                payload = Data(count: payload.count)
            }

            self.pcmCarry.append(payload)
            let bytesPerChunk = self.chunkFrames * MemoryLayout<Int16>.size
            while self.pcmCarry.count >= bytesPerChunk {
                let chunk = self.pcmCarry.prefix(bytesPerChunk)
                self.pcmCarry.removeFirst(bytesPerChunk)
                if self.pendingChunks.count >= self.maxQueueChunks {
                    self.failLocked("Audio send failed")
                    return
                }
                self.pendingChunks.append(Data(chunk))
            }
            self.scheduleDrainLocked()
        }
    }

    private func scheduleDrainLocked() {
        if drainScheduled || running == false {
            return
        }
        drainScheduled = true
        queue.asyncAfter(deadline: .now() + 0.01) { [weak self] in
            self?.drainLocked()
        }
    }

    private func drainLocked() {
        drainScheduled = false
        guard running else { return }
        if pendingChunks.isEmpty == false {
            let chunk = pendingChunks.removeFirst()
            onChunk?(chunk)
        }
        if pendingChunks.isEmpty == false || captureArmed {
            scheduleDrainLocked()
        }
    }

    private func enqueueSamplesLocked(_ samples: [Float]) {
        if running == false || playerStarted == false {
            return
        }
        if samples.isEmpty {
            return
        }
        guard let format = playerFormat else {
            failLocked("Audio backlog")
            return
        }
        guard let buffer = AVAudioPCMBuffer(pcmFormat: format, frameCapacity: AVAudioFrameCount(samples.count)) else {
            failLocked("Audio backlog")
            return
        }
        buffer.frameLength = AVAudioFrameCount(samples.count)
        if let channel = buffer.floatChannelData?[0] {
            for index in 0..<samples.count {
                channel[index] = samples[index]
            }
        }

        let duration = Double(samples.count) / sampleRate
        if playback.pendingBuffers >= maxPendingBuffers || playback.seconds + duration > maxPlaybackSeconds {
            failLocked("Audio backlog")
            return
        }

        playback.schedule(duration: duration)
        let generation = playback.generation
        if player.isPlaying == false {
            player.play()
        }
        player.scheduleBuffer(buffer, completionHandler: { [weak self] in
            self?.queue.async {
                guard let self else { return }
                self.playback.complete(duration: duration, generation: generation)
            }
        })
    }

    private func failLocked(_ detail: String) {
        let callback = onFail
        onFail = nil
        callback?(detail)
    }
}

enum AudioEngineError: Error {
    case formatUnavailable
    case converterUnavailable
    case voiceProcessingUnavailable
    case startFailed
}
