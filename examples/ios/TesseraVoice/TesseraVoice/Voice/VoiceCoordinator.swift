import AVFoundation
import CallKit
import Foundation

/// Owns one explicit voice call lifecycle, matching web/voice.js behavior on native audio.
/// CallKit owns the system call + audio activation; this type owns Tessera protocol/audio IO.
@MainActor
final class VoiceCoordinator: ObservableObject {
    @Published private(set) var status = CallStatus()
    @Published private(set) var isRunning = false
    @Published private(set) var isMuted = false
    @Published private(set) var transcript: [TranscriptLine] = []
    @Published private(set) var preferSpeaker = true
    @Published private(set) var routeSummary = "Default"

    private let client: TesseraClient
    private let callKit = CallKitCallController()

    private var runID = UUID()
    private var callUUID: UUID?
    private var audioSession = AudioSessionController()
    private var audio: VoiceAudioEngine?
    private var socket: WebSocketSession?
    private var urlSession: URLSession?
    private var validated: VoiceStartValidator.Validated?
    /// Lease id captured as soon as start returns, before validation, so invalid-start cleanup retires it.
    private var leaseVoiceId: String?
    private var baseURL: URL?
    private var accessToken: String?
    var activeSessionSlug: String? { slug }
    private var slug: String?
    private var selectedVoice: String?
    private var ready = false
    private var retired = false
    private var interrupted = false
    private var callKitAudioActive = false
    private var engineStarted = false
    private var connectedReported = false
    private var endingFromCallKit = false
    private var muteRequestInFlight = false
    private var bridgePending = 0
    private var bridgeChain: Task<Void, Never>?
    private var rearmTask: Task<Void, Never>?
    private var taskPoll: Task<Void, Never>?
    private var taskPollNeeded = false
    private var taskPollRevision = 0
    private var userSpeaking = false
    private var answering = false
    private var responseAudio = 0
    // Utterance identity. A single answered-this-turn flag describes no
    // particular utterance, so a resumed utterance can suppress its own
    // recovery and a pause can fire response.create before the speaker
    // finished. web/voice.js carries the same rule.
    private var utterance = 0
    private var responses: [String: Int] = [:]
    private var answered: Set<Int> = []
    private var responseAt: Date?
    private var connectTimer: Task<Void, Never>?
    private var deadlineTimer: Task<Void, Never>?
    private var connectTask: Task<Void, Never>?

    init(client: TesseraClient = TesseraClient()) {
        self.client = client
        callKit.delegate = self
    }

    func start(
        baseURL: URL,
        accessToken: String?,
        slug: String,
        voice: String?,
        preferSpeaker: Bool
    ) async {
        if isRunning {
            return
        }

        let localRun = UUID()
        runID = localRun
        isRunning = true
        isMuted = false
        ready = false
        retired = false
        interrupted = false
        callKitAudioActive = false
        engineStarted = false
        connectedReported = false
        endingFromCallKit = false
        answering = false
        userSpeaking = false
        taskPollNeeded = false
        responseAudio = 0
        utterance = 0
        responses = [:]
        answered = []
        responseAt = nil
        muteRequestInFlight = false
        transcript = []
        bridgePending = 0
        leaseVoiceId = nil
        validated = nil
        callUUID = nil
        audio = nil
        self.baseURL = baseURL
        self.accessToken = accessToken
        self.slug = slug
        self.selectedVoice = voice
        self.preferSpeaker = preferSpeaker

        setStatus(.starting, "Starting", "Allow microphone")

        let permission = await requestMicrophone()
        if permission == false {
            fail(run: localRun, label: "Microphone denied", detail: "Microphone denied", endReason: .failed)
            return
        }
        if runID != localRun { return }

        setStatus(.connecting, "Connecting", "Starting system call")

        let handleValue = slug.isEmpty ? ProductManifest.productName : slug
        let startError: String? = await withCheckedContinuation { continuation in
            let uuid = callKit.requestStart(handleValue: handleValue) { error in
                continuation.resume(returning: error)
            }
            // UUID is assigned before the transaction completion so perform-start can match.
            // requestStart always invokes completion (including the already-active path).
            if let uuid {
                self.callUUID = uuid
            }
        }
        if runID != localRun {
            if let uuid = callUUID {
                callKit.reportEnded(callUUID: uuid, reason: .failed)
                callUUID = nil
            }
            return
        }
        if let startError {
            callUUID = nil
            fail(
                run: localRun,
                label: "Call unavailable",
                detail: startError,
                endReason: nil
            )
            return
        }
        // Protocol connect starts when CXStartCallAction is accepted (callKitStartAccepted).
    }

    func stop(detail: String = "Stopped") {
        let localRun = runID
        guard isRunning else {
            setStatus(.idle, "Stopped", detail)
            return
        }
        if let callUUID, endingFromCallKit == false {
            // Route hang-up through CallKit so the system call UI and AirPods stay in sync.
            callKit.requestEnd(callUUID: callUUID) { [weak self] error in
                Task { @MainActor in
                    guard let self else { return }
                    if self.runID != localRun { return }
                    if let error {
                        // Fall back to local teardown if the transaction is rejected.
                        self.end(
                            run: localRun,
                            label: "Stopped",
                            detail: detail,
                            isFailure: false,
                            endReason: .remoteEnded
                        )
                        _ = error
                    }
                }
            }
            return
        }
        end(run: localRun, label: "Stopped", detail: detail, isFailure: false, endReason: .answeredElsewhere)
    }

    /// UI mute uses the same CXSetMutedCallAction path as AirPods.
    func setMuted(_ muted: Bool) {
        guard isRunning else { return }
        if isMuted == muted { return }
        guard let callUUID else {
            applyMutedState(muted)
            return
        }
        if muteRequestInFlight { return }
        muteRequestInFlight = true
        callKit.requestMute(muted, callUUID: callUUID) { [weak self] error in
            Task { @MainActor in
                guard let self else { return }
                self.muteRequestInFlight = false
                if let error {
                    self.setStatus(self.status.phase, self.status.label, "Mute request failed")
                    _ = error
                    return
                }
                // Actual mute applies when CallKit delivers CXSetMutedCallAction.
            }
        }
    }

    func setPreferSpeaker(_ value: Bool) {
        preferSpeaker = value
        do {
            try audioSession.applyRoute(preferSpeaker: value)
            refreshRouteSummary()
        } catch {
            setStatus(status.phase, status.label, "Route change failed")
        }
    }

    // MARK: - CallKit delegate hooks

    fileprivate func handleCallKitStartAccepted(callUUID: UUID) {
        guard isRunning, self.callUUID == nil || self.callUUID == callUUID else {
            callKit.reportEnded(callUUID: callUUID, reason: .failed)
            return
        }
        self.callUUID = callUUID
        let localRun = runID
        do {
            try audioSession.configure(
                preferSpeaker: preferSpeaker,
                onInterrupt: { [weak self] began, shouldResume in
                    Task { @MainActor in
                        self?.handleInterruption(began: began, shouldResume: shouldResume, run: localRun)
                    }
                },
                onMediaReset: { [weak self] in
                    Task { @MainActor in
                        self?.fail(run: localRun, label: "Audio reset", detail: "Media services reset", endReason: .failed)
                    }
                },
                onRouteChange: { [weak self] in
                    Task { @MainActor in
                        guard let self, self.runID == localRun else { return }
                        self.refreshRouteSummary()
                    }
                }
            )
            refreshRouteSummary()
        } catch {
            fail(run: localRun, label: "Audio unavailable", detail: "Audio session configure failed", endReason: .failed)
            return
        }

        callKit.reportConnecting(callUUID: callUUID)
        setStatus(.connecting, "Connecting", "")

        connectTask?.cancel()
        connectTask = Task { [weak self] in
            await self?.connectProtocol(run: localRun, callUUID: callUUID)
        }
    }

    fileprivate func handleCallKitStartRejected(callUUID: UUID, detail: String) {
        guard self.callUUID == callUUID || self.callUUID == nil else { return }
        fail(run: runID, label: "Call unavailable", detail: detail, endReason: nil)
    }

    fileprivate func handleCallKitEndRequested(callUUID: UUID) {
        guard self.callUUID == callUUID else { return }
        endingFromCallKit = true
        end(run: runID, label: "Stopped", detail: "Call ended", isFailure: false, endReason: nil)
    }

    fileprivate func handleCallKitMuteRequested(callUUID: UUID, muted: Bool) {
        guard self.callUUID == callUUID, isRunning else { return }
        applyMutedState(muted)
    }

    fileprivate func handleCallKitAudioActivated(callUUID: UUID) {
        guard self.callUUID == callUUID, isRunning else { return }
        callKitAudioActive = true
        interrupted = false
        try? audioSession.applyRoute(preferSpeaker: preferSpeaker)
        startEngineIfNeeded(run: runID)
        armCaptureIfReady(run: runID)
        refreshRouteSummary()
    }

    fileprivate func handleCallKitAudioDeactivated(callUUID: UUID) {
        guard self.callUUID == callUUID else { return }
        callKitAudioActive = false
        if isRunning {
            audio?.suspendForInterruption()
            engineStarted = false
            if interrupted == false {
                setStatus(.muted, "Audio paused", "System deactivated call audio")
            }
        }
    }

    fileprivate func handleCallKitProviderReset() {
        if isRunning {
            endingFromCallKit = true
            end(run: runID, label: "Call reset", detail: "System reset the call provider", isFailure: true, endReason: nil)
        }
        callUUID = nil
        callKitAudioActive = false
        engineStarted = false
    }

    // MARK: - Protocol + audio

    private func connectProtocol(run: UUID, callUUID: UUID) async {
        guard runID == run, let baseURL, let slug else { return }

        do {
            let start = try await client.startVoice(
                baseURL: baseURL,
                accessToken: accessToken,
                slug: slug,
                voice: selectedVoice
            )
            // Preserve lease id before validation so invalid payloads still retire cleanly.
            leaseVoiceId = start.voice_id

            if runID != run {
                retireLease(baseURL: baseURL, token: accessToken, slug: slug, voiceId: start.voice_id)
                return
            }

            let validated: VoiceStartValidator.Validated
            do {
                validated = try VoiceStartValidator.validate(start)
            } catch {
                fail(
                    run: run,
                    label: "Voice unavailable",
                    detail: (error as? LocalizedError)?.errorDescription ?? "Could not start",
                    endReason: .failed
                )
                return
            }
            self.validated = validated

            let sessionConfig = URLSessionConfiguration.default
            sessionConfig.waitsForConnectivity = false
            sessionConfig.networkServiceType = .responsiveAV
            sessionConfig.shouldUseExtendedBackgroundIdleMode = true
            let urlSession = URLSession(configuration: sessionConfig)
            self.urlSession = urlSession

            let task = urlSession.webSocketTask(
                with: validated.websocketURL,
                protocols: validated.protocols
            )
            let socket = WebSocketSession(task: task)
            self.socket = socket

            let remainingLease = validated.sessionExpiresAt.timeIntervalSinceNow
            let maxSeconds = validated.maxSessionSeconds
            let deadline = min(remainingLease, maxSeconds)

            connectTimer = Task { [weak self] in
                let timeout = ProductManifest.defaultConnectTimeoutSeconds
                do {
                    try await Task.sleep(nanoseconds: UInt64(timeout * 1_000_000_000))
                } catch {
                    return
                }
                if Task.isCancelled { return }
                await MainActor.run {
                    guard let self, self.runID == run, self.ready == false else { return }
                    self.fail(run: run, label: "Voice connection timed out", detail: "Connect timed out", endReason: .failed)
                }
            }
            deadlineTimer = Task { [weak self] in
                if deadline <= 0 { return }
                do {
                    try await Task.sleep(nanoseconds: UInt64(deadline * 1_000_000_000))
                } catch {
                    return
                }
                if Task.isCancelled { return }
                await MainActor.run {
                    guard let self, self.runID == run else { return }
                    self.fail(run: run, label: "Voice session ended", detail: "Time limit reached", endReason: .failed)
                }
            }

            socket.start(
                onEvent: { [weak self] event in
                    Task { @MainActor in
                        self?.handleEvent(event, run: run)
                    }
                },
                onFail: { [weak self] detail in
                    Task { @MainActor in
                        self?.fail(run: run, label: "Voice disconnected", detail: detail, endReason: .failed)
                    }
                }
            )

            if let object = validated.sessionUpdate.objectValue {
                var plain: [String: Any] = [:]
                for (key, nested) in object {
                    plain[key] = nested.toAny()
                }
                socket.sendJSONObject(plain)
            } else {
                fail(run: run, label: "Voice disconnected", detail: "Voice could not connect.", endReason: .failed)
                return
            }

            // Engine starts only after CallKit didActivateAudioSession.
            if callKitAudioActive {
                startEngineIfNeeded(run: run)
            }
        } catch let error as TesseraClientError {
            if case .http(let status, _) = error, status == 403 {
                fail(run: run, label: "Voice access required", detail: "Core could not start voice", endReason: .failed)
            } else {
                fail(run: run, label: "Voice unavailable", detail: error.localizedDescription, endReason: .failed)
            }
        } catch let error as VoiceProtocolError {
            fail(run: run, label: "Voice unavailable", detail: error.localizedDescription, endReason: .failed)
        } catch {
            fail(run: run, label: "Voice unavailable", detail: "Could not start", endReason: .failed)
        }
    }

    private func startEngineIfNeeded(run: UUID) {
        guard runID == run, isRunning else { return }
        if engineStarted, audio?.isRunning == true { return }
        guard CallAudioGate.shouldStartEngine(
            callKitAudioActive: callKitAudioActive,
            hasValidatedLease: validated != nil
        ) else {
            // Lease/socket not ready yet, or CallKit has not activated audio.
            return
        }
        guard let validated else { return }

        if let existing = audio, existing.isRunning {
            engineStarted = true
            return
        }

        audio?.stop()
        let engine = VoiceAudioEngine(sampleRate: validated.audioSampleRate)
        do {
            try engine.start { [weak self] detail in
                Task { @MainActor in
                    self?.fail(run: run, label: "Voice data unavailable", detail: detail, endReason: .failed)
                }
            }
        } catch {
            fail(run: run, label: "Microphone unavailable", detail: "Audio engine failed to start", endReason: .failed)
            return
        }
        audio = engine
        engineStarted = true
        engine.setMuted(isMuted)
    }

    private func armCaptureIfReady(run: UUID) {
        guard runID == run else { return }
        guard CallAudioGate.shouldArmCapture(
            protocolReady: ready,
            callKitAudioActive: callKitAudioActive,
            engineStarted: engineStarted
        ) else { return }
        guard let audio else {
            fail(run: run, label: "Microphone unavailable", detail: "No microphone", endReason: .failed)
            return
        }
        do {
            try audio.armCapture(
                onChunk: { [weak self] data in
                    Task { @MainActor in
                        self?.sendAudioChunk(data, run: run)
                    }
                },
                onFail: { [weak self] detail in
                    Task { @MainActor in
                        self?.fail(run: run, label: "Voice connection stalled", detail: detail, endReason: .failed)
                    }
                }
            )
            audio.setMuted(isMuted)
            if interrupted == false, isMuted == false {
                setStatus(.listening, "Listening", "")
            } else if isMuted {
                setStatus(.muted, "Muted", "Microphone off. Playback continues.")
            }
        } catch {
            fail(run: run, label: "Microphone unavailable", detail: "No microphone", endReason: .failed)
        }
    }

    private func applyMutedState(_ muted: Bool) {
        isMuted = muted
        audio?.setMuted(muted)
        if isRunning && interrupted == false && callKitAudioActive {
            if muted {
                setStatus(.muted, "Muted", "Microphone off. Playback continues.")
            } else if ready, audio?.isRunning == true {
                setStatus(.listening, "Listening", "")
            }
        }
    }

    private func handleEvent(_ event: [String: Any], run: UUID) {
        if runID != run {
            return
        }
        guard let type = event["type"] as? String else {
            fail(run: run, label: "Voice data unavailable", detail: "Unreadable event from provider", endReason: .failed)
            return
        }

        if type == "error" {
            var code = "error"
            if let errorObject = event["error"] as? [String: Any] {
                if let value = errorObject["type"] as? String, isSafeProviderCode(value) {
                    code = value
                } else if let value = errorObject["code"] as? String, isSafeProviderCode(value) {
                    code = value
                }
            }
            fail(run: run, label: "Voice error", detail: "Provider: \(code)", endReason: .failed)
            return
        }

        if type == "session.updated", ready == false {
            // Any accepted session.updated means the provider took the settings.
            // Requiring an exact instructions echo left the call stuck before
            // Listening: the provider normalises the text, so it never matched.
            // web/voice.js had the same gate and the same fix.
            ready = true
            connectTimer?.cancel()
            connectTimer = nil
            if let callUUID,
               CallAudioGate.shouldReportConnected(protocolReady: true, alreadyReported: connectedReported) {
                connectedReported = true
                callKit.reportConnected(callUUID: callUUID)
            }
            startEngineIfNeeded(run: run)
            armCaptureIfReady(run: run)
        }

        if ready == false {
            return
        }

        // Every response is tagged with the utterance it answers. Both defects
        // in the old single-flag version came from that identity being absent.
        func utteranceOf(_ id: String?) -> Int {
            guard let id, let owner = responses[id] else { return utterance }
            return owner
        }

        if type == "input_audio_buffer.speech_started" {
            userSpeaking = true
            utterance += 1
            // Resumed speech cancels the previous utterance's recovery. Without
            // this, a pause-then-resume fired response.create from the old timer
            // while the speaker was still talking.
            rearmTask?.cancel()
            rearmTask = nil
            audio?.clearPlayback()
            // Barge-in clears voice playback. Session work requires explicit Stop.
        }

        // The provider is the voice agent: it hears the speaker, calls Tessera's
        // tools through /voice/events, and speaks the answer itself. Cancelling the
        // response is what made the app a transcription-only chatbot. Its audio is
        // the reply, so play it as it streams.
        if type == "response.output_audio.delta" || type == "response.audio.delta" {
            if let delta = event["delta"] as? String {
                // A delta from the previous utterance's response says nothing
                // about this one. Marking it answered suppressed recovery for a
                // new utterance that had genuinely produced no audio.
                let owner = utteranceOf(event["response_id"] as? String)
                if owner == utterance {
                    responseAudio += delta.count
                    answered.insert(owner)
                }
                setStatus(.speaking, "Speaking", "")
                audio?.enqueueBase64PCM16(delta)
            }
        }

        // Server VAD creates a response when it thinks the turn ended, but that
        // response can come up hung: response.created with zero audio for the
        // rest of the call, while an explicit response.create answers.
        // Cancelling does not help ("no active response found"), so ask for a
        // new response when the turn is silent.
        if type == "response.created" {
            answering = true
            responseAudio = 0
            responseAt = Date()
            if let response = event["response"] as? [String: Any],
               let id = response["id"] as? String {
                responses[id] = utterance
            }
        }
        if type == "response.done" {
            answering = false
            if let response = event["response"] as? [String: Any],
               let id = response["id"] as? String {
                responses.removeValue(forKey: id)
            }
        }
        if type == "input_audio_buffer.speech_stopped" {
            userSpeaking = false
            rearmTask?.cancel()
            let asked = utterance
            rearmTask = Task { [weak self] in
                // Poll rather than fire once: a healthy response can take
                // several seconds to its first audio byte, and re-asking while
                // it is still starting answers the same question twice.
                while true {
                    try? await Task.sleep(nanoseconds: 2_000_000_000)
                    guard let self, Task.isCancelled == false else { return }
                    guard self.runID == run, self.ready, self.interrupted == false else { return }
                    // The speaker resumed while this timer waited: it belongs to
                    // a superseded utterance, so it must not answer.
                    if asked != self.utterance { return }
                    // This turn already spoke: a second response would answer twice.
                    if self.answered.contains(asked) { return }
                    if self.answering && self.responseAudio == 0,
                       let at = self.responseAt, Date().timeIntervalSince(at) < 8 { continue }
                    self.socket?.sendJSONObject(["type": "response.create"])
                    return
                }
            }
        }

        if let update = RecordedEventFilter.transcriptUpdate(from: event) {
            applyTranscript(role: update.role, text: update.text, isFinal: update.isFinal,
                            itemID: event["item_id"] as? String,
                            contentIndex: event["content_index"] as? Int ?? 0)
        }

        if let recorded = RecordedEventFilter.recordedEvent(from: event) {
            bridge(recorded, run: run)
        }
    }

    private func sendAudioChunk(_ data: Data, run: UUID) {
        if runID != run || ready == false || interrupted || callKitAudioActive == false {
            return
        }
        let payload: Data
        // Send silence while our own speech is still playing, so the transcriber
        // cannot hear Tessera and hand it back as a user turn.
        if isMuted || audio?.isPlayingBack == true {
            payload = Data(count: data.count)
        } else {
            payload = data
        }
        let encoded = payload.base64EncodedString()
        socket?.sendJSONObject([
            "type": "input_audio_buffer.append",
            "audio": encoded
        ])
    }

    private func bridge(_ event: [String: Any], run: UUID) {
        if runID != run {
            return
        }
        bridgePending += 1
        if bridgePending > ProductManifest.defaultMaxPendingBridgeEvents {
            fail(run: run, label: "Transcript sync stalled", detail: "Transcript sync stalled", endReason: .failed)
            return
        }

        let previous = bridgeChain
        bridgeChain = Task { [weak self] in
            _ = await previous?.value
            guard let self else { return }
            await self.sendBridgeEvent(event, run: run)
        }
    }

    private func sendBridgeEvent(_ event: [String: Any], run: UUID) async {
        defer {
            bridgePending = max(0, bridgePending - 1)
        }
        guard runID == run else { return }
        guard let baseURL, let slug, let validated else { return }

        do {
            let payload = JSONValue.from(event)
            let result = try await client.postEvents(
                baseURL: baseURL,
                accessToken: accessToken,
                slug: slug,
                voiceId: validated.voiceId,
                events: [payload]
            )
            if runID != run {
                return
            }
            if let send = result.send {
                for reply in send {
                    socket?.sendJSONValue(reply)
                }
            }
            if result.poll_after_ms != nil {
                taskPollNeeded = true
                taskPollRevision += 1
                startTaskPoll(run: run)
            }
        } catch {
            fail(run: run, label: "Transcript sync failed", detail: "Stopped: event not recorded", endReason: .failed)
        }
    }

    private var voiceIdle: Bool {
        return !userSpeaking && !answering && audio?.isPlayingBack != true && bridgePending == 0
    }

    private func startTaskPoll(run: UUID) {
        guard taskPoll == nil else { return }
        taskPoll = Task { [weak self] in
            guard let self else { return }
            defer {
                if self.runID == run { self.taskPoll = nil }
            }
            while self.runID == run && self.taskPollNeeded {
                do {
                    try await Task.sleep(nanoseconds: 1_000_000_000)
                    guard !Task.isCancelled, self.runID == run else { return }
                    if !self.voiceIdle { continue }
                    guard let baseURL = self.baseURL, let slug = self.slug,
                          let validated = self.validated else { return }
                    let revision = self.taskPollRevision
                    let result = try await self.client.postEvents(
                        baseURL: baseURL, accessToken: self.accessToken, slug: slug,
                        voiceId: validated.voiceId, events: [])
                    guard self.runID == run else { return }
                    // Speech can begin while HTTP is in flight. Retain the result
                    // until that exchange finishes, without interrupting its audio.
                    while !self.voiceIdle {
                        try await Task.sleep(nanoseconds: 200_000_000)
                        guard !Task.isCancelled, self.runID == run else { return }
                    }
                    for reply in result.send ?? [] {
                        self.socket?.sendJSONValue(reply)
                    }
                    if result.poll_after_ms != nil || revision == self.taskPollRevision {
                        self.taskPollNeeded = result.poll_after_ms != nil
                    }
                } catch is CancellationError {
                    return
                } catch {
                    self.fail(run: run, label: "Task sync failed",
                              detail: "Session result remains in chat", endReason: .failed)
                    return
                }
            }
        }
    }

    func applyTranscript(role: String, text: String, isFinal: Bool,
                         itemID: String? = nil, contentIndex: Int = 0) {
        if text.isEmpty && isFinal == false {
            return
        }
        if let itemID, itemID.isEmpty == false {
            let key = "\(role):\(itemID):\(contentIndex)"
            for index in transcript.indices {
                if transcript[index].providerKey != key {
                    continue
                }
                var line = transcript[index]
                if isFinal {
                    if text.isEmpty || line.seenFinalTexts.contains(text) {
                        return
                    }
                    line.text = text
                    line.isFinal = true
                    line.seenFinalTexts.insert(text)
                } else {
                    if line.isFinal {
                        return
                    }
                    line.text += text
                }
                transcript[index] = line
                return
            }
            if text.isEmpty {
                return
            }
            var line = TranscriptLine(role: role, text: text, isFinal: isFinal)
            line.providerKey = key
            if isFinal {
                line.seenFinalTexts.insert(text)
            }
            transcript.append(line)
            trimTranscript()
            return
        }
        if isFinal == false {
            if let last = transcript.last, last.role == role, last.isFinal == false {
                var lines = transcript
                lines[lines.count - 1].text += text
                transcript = lines
                return
            }
            transcript.append(TranscriptLine(role: role, text: text, isFinal: false))
            trimTranscript()
            return
        }

        if let lastIndex = transcript.indices.last,
           transcript[lastIndex].role == role,
           transcript[lastIndex].isFinal == false {
            var lines = transcript
            if text.isEmpty == false {
                lines[lastIndex].text = text
            }
            lines[lastIndex].isFinal = true
            transcript = lines
            trimTranscript()
            return
        }

        if text.isEmpty == false {
            transcript.append(TranscriptLine(role: role, text: text, isFinal: true))
            trimTranscript()
        }
    }

    private func trimTranscript() {
        let limit = 40
        if transcript.count > limit {
            transcript = Array(transcript.suffix(limit))
        }
    }

    private func handleInterruption(began: Bool, shouldResume: Bool, run: UUID) {
        if runID != run || isRunning == false {
            return
        }
        if began {
            interrupted = true
            audio?.suspendForInterruption()
            engineStarted = false
            setStatus(.muted, "Interrupted", "Audio interrupted")
            return
        }

        // Interruption ended: only resume if CallKit still has the audio session active.
        if shouldResume == false || callKitAudioActive == false {
            if callKitAudioActive == false {
                return
            }
            fail(run: run, label: "Audio interrupted", detail: "Audio session interrupted", endReason: .failed)
            return
        }
        do {
            try audioSession.reassertCategory(preferSpeaker: preferSpeaker)
            // Prefer resume of the existing engine; only rebuild if resume cannot run.
            if let audio {
                do {
                    try audio.resumeAfterInterruption()
                    engineStarted = audio.isRunning
                } catch {
                    audio.stop()
                    self.audio = nil
                    engineStarted = false
                    startEngineIfNeeded(run: run)
                }
            } else {
                startEngineIfNeeded(run: run)
            }
            guard let audio = self.audio, audio.isRunning else {
                fail(run: run, label: "Audio interrupted", detail: "Audio engine stopped", endReason: .failed)
                return
            }
            interrupted = false
            audio.setMuted(isMuted)
            armCaptureIfReady(run: run)
            if ready && isMuted == false {
                setStatus(.listening, "Listening", "")
            } else if isMuted {
                setStatus(.muted, "Muted", "Microphone off. Playback continues.")
            }
            refreshRouteSummary()
        } catch {
            fail(run: run, label: "Audio interrupted", detail: "Could not resume audio", endReason: .failed)
        }
    }

    private func fail(run: UUID, label: String, detail: String, endReason: CXCallEndedReason?) {
        end(run: run, label: label, detail: detail, isFailure: true, endReason: endReason)
    }

    private func end(
        run: UUID,
        label: String,
        detail: String,
        isFailure: Bool,
        endReason: CXCallEndedReason?
    ) {
        // Always ignore callbacks from a superseded run.
        if runID != run {
            return
        }
        if isRunning == false && validated == nil && leaseVoiceId == nil && callUUID == nil {
            setStatus(isFailure ? .error : .idle, label, detail)
            return
        }

        taskPoll?.cancel()
        taskPoll = nil
        taskPollNeeded = false
        connectTimer?.cancel()
        deadlineTimer?.cancel()
        connectTask?.cancel()
        connectTimer = nil
        deadlineTimer = nil
        connectTask = nil

        audio?.stop()
        audio = nil
        socket?.close()
        socket = nil
        urlSession?.invalidateAndCancel()
        urlSession = nil
        audioSession.teardownObservers()

        let endingUUID = callUUID
        if let endingUUID {
            if endingFromCallKit == false, let endReason {
                callKit.reportEnded(callUUID: endingUUID, reason: endReason)
            } else if endingFromCallKit == false, endReason == nil, isFailure {
                callKit.reportEnded(callUUID: endingUUID, reason: .failed)
            } else if endingFromCallKit == false, isFailure == false {
                // Local fallback when CXEndCallAction was not used.
                callKit.reportEnded(callUUID: endingUUID, reason: .remoteEnded)
            }
            callKit.clearActiveCall(callUUID: endingUUID)
        }

        let voiceId = validated?.voiceId ?? leaseVoiceId
        let stopBase = baseURL
        let stopSlug = slug
        let stopToken = accessToken
        retireLease(baseURL: stopBase, token: stopToken, slug: stopSlug, voiceId: voiceId)

        validated = nil
        leaseVoiceId = nil
        baseURL = nil
        accessToken = nil
        slug = nil
        selectedVoice = nil
        callUUID = nil
        ready = false
        interrupted = false
        callKitAudioActive = false
        engineStarted = false
        connectedReported = false
        endingFromCallKit = false
        muteRequestInFlight = false
        isRunning = false
        isMuted = false
        bridgePending = 0
        bridgeChain = nil
        // Invalidate run id last so in-flight MainActor hops still match until teardown finishes.
        runID = UUID()

        setStatus(isFailure ? .error : .idle, label, detail)
    }

    private func retireLease(baseURL: URL?, token: String?, slug: String?, voiceId: String?) {
        if retired {
            return
        }
        retired = true
        guard let baseURL, let slug, let voiceId, voiceId.isEmpty == false else {
            return
        }
        Task.detached { [client] in
            do {
                try await client.stopVoice(
                    baseURL: baseURL,
                    accessToken: token,
                    slug: slug,
                    voiceId: voiceId
                )
            } catch {
                // Lease expires server-side if stop is not confirmed.
            }
        }
    }

    private func setStatus(_ phase: CallPhase, _ label: String, _ detail: String) {
        status = CallStatus(phase: phase, label: label, detail: detail)
    }

    private func requestMicrophone() async -> Bool {
        await withCheckedContinuation { continuation in
            AVAudioApplication.requestRecordPermission { granted in
                continuation.resume(returning: granted)
            }
        }
    }

    private func refreshRouteSummary() {
        let outputs = AVAudioSession.sharedInstance().currentRoute.outputs
        if outputs.isEmpty {
            routeSummary = preferSpeaker ? "Speaker" : "Receiver"
            return
        }
        var names: [String] = []
        for output in outputs {
            names.append(output.portName)
        }
        routeSummary = names.joined(separator: ", ")
    }

    private func isSafeProviderCode(_ value: String) -> Bool {
        if value.count < 1 || value.count > 64 {
            return false
        }
        for character in value {
            let isAlphaNum = character.isLetter || character.isNumber
            if isAlphaNum || character == "_" || character == "." || character == "-" {
                continue
            }
            return false
        }
        return true
    }
}

extension VoiceCoordinator: CallKitCallControllerDelegate {
    func callKitStartAccepted(callUUID: UUID) {
        handleCallKitStartAccepted(callUUID: callUUID)
    }

    func callKitStartRejected(callUUID: UUID, detail: String) {
        handleCallKitStartRejected(callUUID: callUUID, detail: detail)
    }

    func callKitEndRequested(callUUID: UUID) {
        handleCallKitEndRequested(callUUID: callUUID)
    }

    func callKitMuteRequested(callUUID: UUID, muted: Bool) {
        handleCallKitMuteRequested(callUUID: callUUID, muted: muted)
    }

    func callKitAudioSessionDidActivate(callUUID: UUID) {
        handleCallKitAudioActivated(callUUID: callUUID)
    }

    func callKitAudioSessionDidDeactivate(callUUID: UUID) {
        handleCallKitAudioDeactivated(callUUID: callUUID)
    }

    func callKitProviderDidReset() {
        handleCallKitProviderReset()
    }
}
