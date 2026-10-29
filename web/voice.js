/* Explicit browser voice lifecycle. Provider settings and access come from Tessera. */
(() => {
  "use strict";
  class TesseraVoice {
    constructor(options) {
      this.options = options;
      this.current = null;
      this.deviceId = "";
      this.muted = false;
      this.held = true;
    }

    // Mic gate: muted, push-to-talk not held, or Tessera's own speech still playing.
    // Sending silence during our own playback keeps the transcriber from hearing
    // Tessera speak and handing it back as a user turn.
    gated() {
      const run = this.current;
      if (this.muted || !this.held) return true;
      return Boolean(run && run.audio && run.playbackAt > run.audio.currentTime + 0.05);
    }
    setMuted(value) { this.muted = value === true; }
    setHeld(value) { this.held = value === true; }

    level() {
      const run = this.current;
      if (!run || !run.analyser) return 0;
      const data = new Float32Array(run.analyser.fftSize);
      run.analyser.getFloatTimeDomainData(data);
      let sum = 0;
      for (const value of data) sum += value * value;
      return this.gated() ? 0 : Math.min(1, Math.sqrt(sum / data.length) * 4);
    }

    waveform(points) {
      const run = this.current;
      if (!run || !run.analyser) return null;
      const data = new Float32Array(run.analyser.fftSize);
      run.analyser.getFloatTimeDomainData(data);
      const step = Math.max(1, Math.floor(data.length / points));
      const out = [];
      for (let index = 0; index < points; index += 1) out.push(this.gated() ? 0 : data[index * step] || 0);
      return out;
    }

    running() { return this.current !== null; }

    phase() {
      const run = this.current;
      if (!run) return "off";
      if (!run.ready) return "connecting";
      if (run.audio && run.playbackAt > run.audio.currentTime + 0.05) return "speaking";
      if (run.answering) return "responding";
      if (this.muted) return "muted";
      if (!this.held) return "waiting";
      return "listening";
    }

    notice(label, detail, slug = this.current?.slug) { this.options.onState(label, detail, slug); }

    async start(slug) {
      if (this.running()) return;
      if (window.isSecureContext === false) {
        this.notice("Secure connection required", "Needs HTTPS");
        return;
      }
      if (typeof navigator === "undefined" || !navigator.mediaDevices || !navigator.mediaDevices.getUserMedia || !window.AudioContext || !window.AudioWorkletNode || !window.WebSocket) {
        this.notice("Audio unavailable", "Browser lacks mic/audio support");
        return;
      }
      const run = {slug, sources: new Set(), sentIds: new Set(), queue: Promise.resolve(), pending: 0, playbackAt: 0, ready: false,
                   userSpeaking: false, taskPoll: null, taskPollNeeded: false, taskPollRevision: 0, answering: false, responseAudio: 0, responseAt: 0, rearm: null,
                   // Utterance and response identity. A single answeredThisTurn flag
                   // was wrong in both directions: a late audio delta from the previous
                   // response marked the new utterance answered (so it never recovered),
                   // and a recovery timer from the previous utterance fired after the
                   // speaker resumed (so it asked for a second response).
                   utterance: 0, responses: new Map(), answered: new Set()};
      this.current = run;
      this.notice("Starting", "Allow microphone");
      try {
        run.audio = new window.AudioContext();
        const microphone = navigator.mediaDevices.getUserMedia({audio: this.deviceId ? {channelCount: 1, deviceId: {exact: this.deviceId}} : {channelCount: 1}, video: false}).then(stream => {
          if (this.current !== run) {
            for (const track of stream.getTracks()) track.stop();
            return;
          }
          run.microphone = stream;
        });
        await Promise.all([run.audio.resume(), microphone]);
        if (this.current !== run) return;
        this.notice("Connecting", "");
        // Opt into client keepalive. The core never holds a provider connection,
        // so a silent client and a dead client look identical from there; the
        // beat is the only honest liveness signal. Opt-in keeps the iOS app
        // (which does not beat) on the deadline backstop, untouched.
        const settings = await this.options.start(slug, {keepalive: Boolean(this.options.beatSeconds > 0 && this.options.heartbeat)});
        run.settings = settings;
        if (this.current !== run) { this.retire(run); return; }
        this.validate(settings);
        if (this.options.beatSeconds > 0 && this.options.heartbeat) this.beat(run);
        await run.audio.audioWorklet.addModule(this.options.workletUrl);
        if (this.current !== run) return;
        run.capture = new window.AudioWorkletNode(run.audio, "tessera-microphone", {processorOptions: {
          targetRate: settings.audio_sample_rate, chunkFrames: this.options.chunkFrames,
        }});
        run.capture.port.onmessage = event => {
          if (this.current !== run || !run.ready) return;
          try {
            // A slow socket must not end the call. Drop this frame; the next one
            // carries fresh audio, and the provider's VAD only needs continuity.
            if (run.socket.bufferedAmount > this.options.maxBufferedBytes) return;
            const bytes = new Uint8Array(event.data);
            if (this.gated()) bytes.fill(0);
            let binary = "";
            for (const byte of bytes) binary += String.fromCharCode(byte);
            this.send(run, {type: "input_audio_buffer.append", audio: btoa(binary)});
          } catch (_) { /* A dropped frame is not a dead call. */ }
        };
        for (const track of run.microphone.getTracks()) track.onended = () => this.fail(run, "Microphone disconnected", "The microphone was disconnected. Voice stopped.");
        run.socket = this.options.createSocket ? this.options.createSocket(settings) : new window.WebSocket(settings.websocket_url, settings.protocols);
        // Default binaryType is "blob". A binary frame then arrives as a Blob, and
        // reading it is async. Provider events are JSON text. An array buffer is
        // synchronous, so a binary frame still fails the string check and stops the
        // call, instead of hanging on a Blob read.
        if (run.socket) run.socket.binaryType = "arraybuffer";
        const deadline = Math.min(settings.session_expires_at * 1000 - Date.now(), settings.max_session_seconds * 1000);
        run.deadlineTimer = setTimeout(() => this.fail(run, "Voice session ended", "Time limit reached"), deadline);
        run.connectTimer = setTimeout(() => this.fail(run, "Voice connection timed out", "Connect timed out"), this.options.connectSeconds * 1000);
        run.socket.onopen = () => {
          if (this.current !== run) return;
          try { this.send(run, settings.session_update); }
          catch (_) { this.fail(run, "Voice disconnected", "Voice could not connect."); }
        };
        run.socket.onmessage = event => this.receive(run, event);
        run.socket.onerror = () => this.fail(run, "Voice disconnected", "The voice connection failed.");
        run.socket.onclose = () => this.fail(run, "Voice disconnected", "Connection closed");
      } catch (error) {
        if (this.current !== run) return;
        let label = "Voice unavailable";
        let detail = "Could not start";
        if (error.name === "NotAllowedError" || error.name === "SecurityError") {
          label = "Microphone denied";
          detail = this.options.createSocket ? "Microphone denied (System Settings)" : "Microphone denied";
        } else if (error.name === "NotFoundError" || error.name === "NotReadableError") {
          label = "Microphone unavailable";
          detail = "No microphone";
        } else if (error.status === 403) {
          label = this.options.createSocket ? "Voice unavailable" : "Voice access required";
          detail = this.options.createSocket ? "Core could not start voice" : "Core could not start voice";
        } else if (error.status === 429) {
          // Our core answers 429 only for its own lease table (server.py maps
          // VoiceError('limit') to 429), never for a provider rate limit. Saying
          // "unavailable" here reads as a provider fault and cost a diagnosis:
          // the real state is that a slot is still held by an abandoned lease.
          label = "Voice slot held";
          detail = "Another voice session is still open";
        }
        this.fail(run, label, detail);
      }
    }

    validate(settings) {
      if (!settings || typeof settings !== "object" || typeof settings.voice_id !== "string" || !settings.voice_id ||
          (!this.options.createSocket && (!Array.isArray(settings.protocols) || !settings.protocols.length || !settings.protocols.every(value => typeof value === "string" && value))) ||
          !Number.isFinite(settings.audio_sample_rate) || settings.audio_sample_rate <= 0 ||
          !Number.isFinite(settings.expires_at) || settings.expires_at * 1000 <= Date.now() ||
          !Number.isFinite(settings.session_expires_at) || settings.session_expires_at * 1000 <= Date.now() ||
          !Number.isFinite(settings.max_session_seconds) || settings.max_session_seconds <= 0 ||
          settings.session_update?.type !== "session.update" || typeof settings.session_update.session?.instructions !== "string") throw new Error("Invalid voice access");
      if (!this.options.createSocket) {
        const address = new URL(settings.websocket_url);
        if (!["wss:", "ws:"].includes(address.protocol) || address.username || address.password || address.hash ||
            (window.location.protocol === "https:" && address.protocol !== "wss:")) throw new Error("Invalid voice transport");
      }
      const session = settings.session_update.session;
      const topLevelTurn = session.turn_detection;
      const nestedTurn = session.audio?.input?.turn_detection;
      if (topLevelTurn?.type !== "server_vad" && nestedTurn?.type !== "server_vad") throw new Error("Unsupported speech turn detection");
      for (const direction of ["input", "output"]) {
        const audio = session.audio?.[direction];
        if (audio?.format?.type !== "audio/pcm" || audio.format.rate !== settings.audio_sample_rate ||
            (audio.transport && audio.transport !== "json")) throw new Error("Unsupported audio format");
      }
    }

    send(run, event) {
      if (this.current !== run || run.socket.readyState !== window.WebSocket.OPEN) return;
      if (typeof event.event_id === "string") {
        if (run.sentIds.has(event.event_id)) return;
        run.sentIds.add(event.event_id);
      }
      run.socket.send(JSON.stringify(event));
    }

    receive(run, message) {
      if (this.current !== run) return;
      try {
        if (typeof message.data !== "string" || message.data.length > this.options.maxBufferedBytes) throw new Error("Invalid event");
        const event = JSON.parse(message.data);
        if (!event || typeof event !== "object") throw new Error("Invalid event");
        if (event.type === "error") { const code = [event.error?.type, event.error?.code].find(value => typeof value === "string" && /^[a-z0-9_.-]{1,64}$/i.test(value)); const why = code ? "Provider: " + code : "Provider error"; this.fail(run, "Voice error", why); return; }
        // The provider echoes the session it accepted. Requiring an exact
        // instructions match made readiness fragile: a normalised echo never
        // matched, so the page sat on "Connecting" until the timeout fired.
        if (event.type === "session.updated" && !run.ready) {
          run.ready = true;
          clearTimeout(run.taskPoll);
      clearTimeout(run.connectTimer);
          run.source = run.audio.createMediaStreamSource(run.microphone);
          run.source.connect(run.capture);
          if (typeof run.audio.createAnalyser === "function") {
            run.analyser = run.audio.createAnalyser();
            run.analyser.fftSize = 1024;
            run.source.connect(run.analyser);
          }
          run.capture.connect(run.audio.destination); // The worklet's output is silence.
          this.notice("Listening", "");
        }
        if (!run.ready) return;
        // One utterance counter, and every response tagged with the utterance it
        // answers. Both defects in the old single-flag version come from that
        // identity being missing, so it is carried explicitly.
        const utteranceOf = id => (id && run.responses.has(id)) ? run.responses.get(id) : run.utterance;
        if (event.type === "input_audio_buffer.speech_started") {
          run.userSpeaking = true;
          run.utterance += 1;
          // Resumed speech cancels the previous utterance's recovery. Without
          // this, a pause-then-resume fired response.create from the old timer.
          clearTimeout(run.rearm);
          run.rearm = null;
          this.clearPlayback(run);
          // Barge-in clears spoken playback. Only explicit Stop cancels session work.
        }
        // Server VAD creates a response when it thinks the turn ended, but that
        // response can come up hung: response.created with no audio for the
        // rest of the call, while an explicit response.create answers.
        // Cancelling does not help ("no active response found"), so the only
        // recovery is to ask for a new response. Give a healthy response 2s
        // to start speaking first.
        if (event.type === "response.created") {
          run.answering = true;
          run.responseAudio = 0;
          run.responseAt = Date.now();
          if (typeof event.response?.id === "string") run.responses.set(event.response.id, run.utterance);
        }
        if (event.type === "response.output_audio.delta" || event.type === "response.audio.delta") {
          const owner = utteranceOf(event.response_id);
          // A delta from the previous utterance's response says nothing about
          // this one. Marking it answered suppressed recovery for a new
          // utterance that had genuinely produced no audio.
          if (owner === run.utterance) {
            run.responseAudio += (event.delta || "").length;
            run.answered.add(owner);
          }
        }
        if (event.type === "response.done") {
          run.answering = false;
          if (typeof event.response?.id === "string") run.responses.delete(event.response.id);
        }
        if (event.type === "input_audio_buffer.speech_stopped") {
          run.userSpeaking = false;
          clearTimeout(run.rearm);
          const asked = run.utterance;
          const check = () => {
            if (this.current !== run || !run.ready) return;
            // The speaker resumed while this timer waited: it belongs to a
            // superseded utterance, so it must not answer.
            if (asked !== run.utterance) return;
            // This utterance already spoke: a second response would answer
            // twice. A short reply that completed inside the window lands here.
            if (run.answered.has(asked)) return;
            // A response that is live but still silent may just be starting.
            // A healthy one can take several seconds from response.created to
            // its first audio byte, so re-asking at 2s answers a question that
            // is already being answered. Give it up to 8s before deciding
            // it is the hung case, then ask explicitly.
            if (run.answering && run.responseAudio === 0 && Date.now() - run.responseAt < 8000) {
              run.rearm = setTimeout(check, 2000);
              return;
            }
            // Either nothing was ever created, or a response is live but has
            // produced no audio for long enough to be the hung case.
            run.rearm = null;
            this.send(run, {type: "response.create"});
          };
          run.rearm = setTimeout(check, 2000);
        }
        // The provider is the voice agent: it hears the speaker, calls Tessera's
        // tools through /voice/events, and speaks the answer. So its audio is the
        // reply, and cancelling it is what turned voice into a transcription-only
        // chatbot. Play it, stream it as it arrives.
        if (event.type === "response.output_audio.delta" || event.type === "response.audio.delta") this.play(run, event.delta);
        this.transcript(event);
        const recorded = this.recordedEvent(event);
        if (recorded) this.bridge(run, recorded);
      } catch (_) { this.fail(run, "Voice data unavailable", "Unreadable event from provider"); }
    }

    transcript(event) {
      const hook = this.options.onTranscript;
      if (!hook) return;
      const text = value => typeof value === "string" ? value : "";
      // The provider is the voice agent, so its output transcript is the reply.
      if (event.type === "conversation.item.input_audio_transcription.completed") hook("user", text(event.transcript), true);
      else if (event.type === "response.output_audio_transcript.delta" || event.type === "response.audio_transcript.delta") hook("assistant", text(event.delta), false);
      else if (event.type === "response.output_audio_transcript.done" || event.type === "response.audio_transcript.done") hook("assistant", text(event.transcript), true);
    }

    recordedEvent(event) {
      const supported = ["input_audio_buffer.committed", "conversation.item.input_audio_transcription.completed", "response.output_audio_transcript.done", "response.audio_transcript.done", "response.function_call_arguments.done", "response.done"];
      if (!supported.includes(event.type)) return null;
      const safe = {type: event.type};
      if (safe.type === "response.audio_transcript.done") safe.type = "response.output_audio_transcript.done";
      for (const key of ["event_id", "item_id", "response_id", "call_id", "name", "arguments", "transcript"]) {
        if (typeof event[key] === "string") safe[key] = event[key];
      }
      for (const key of ["content_index", "output_index"]) {
        if (Number.isSafeInteger(event[key]) && event[key] >= 0) safe[key] = event[key];
      }
      if (event.type === "response.done" && typeof event.response?.id === "string") {
        safe.response = {id: event.response.id};
        if (["completed", "cancelled", "failed", "incomplete"].includes(event.response.status)) safe.response.status = event.response.status;
        if (Array.isArray(event.response.output)) {
          safe.response.output = [];
          for (const item of event.response.output) {
            if (item?.type === "function_call" && typeof item.call_id === "string") safe.response.output.push({type: "function_call", call_id: item.call_id});
          }
        }
      }
      return safe;
    }

    bridge(run, event) {
      run.pending += 1;
      if (run.pending > this.options.maxPendingEvents) {
        this.fail(run, "Transcript sync stalled", "Transcript sync stalled");
        return;
      }
      run.queue = run.queue.then(async () => {
        const result = await this.options.events(run.slug, run.settings.voice_id, [event]);
        this.options.onMessages(run.slug, result.messages);
        if (this.current !== run) return;
        if (!Array.isArray(result.send)) throw new Error("Invalid voice reply");
        for (const reply of result.send) this.send(run, reply);
        if (result.poll_after_ms) {
          run.taskPollNeeded = true;
          run.taskPollRevision = (run.taskPollRevision || 0) + 1;
          this.pollTasks(run);
        }
      }).catch(() => this.fail(run, "Transcript sync failed", "Stopped: event not recorded")).finally(() => { run.pending -= 1; });
    }

    voiceIdle(run) {
      return !run.userSpeaking && !run.answering && run.sources.size === 0 && run.pending === 0;
    }

    // A cheap liveness beat, deliberately not events([]): an empty events call
    // drains pending task completions, so using it as a beat would mark a
    // spoken result delivered and drop the reply. A failed beat is not a dead
    // call — the deadline still backstops, so one lost beat does not stop voice.
    beat(run) {
      if (!this.options.heartbeat || run.beatTimer || this.current !== run) return;
      run.beatTimer = setTimeout(async () => {
        run.beatTimer = null;
        if (this.current !== run) return;
        try { await this.options.heartbeat(run.slug, run.settings.voice_id); }
        catch (_) { /* A missed beat is not a stopped call. */ }
        if (this.current === run) this.beat(run);
      }, this.options.beatSeconds * 1000);
    }

    pollTasks(run) {
      if (run.taskPoll || !run.taskPollNeeded || this.current !== run) return;
      run.taskPoll = setTimeout(async () => {
        try {
          if (this.current !== run) return;
          if (!this.voiceIdle(run)) return;
          const revision = run.taskPollRevision;
          const result = await this.options.events(run.slug, run.settings.voice_id, []);
          if (!Array.isArray(result.send)) throw new Error("Invalid task reply");
          // Keep completed results during a new spoken exchange. One timer owns
          // this pending delivery and never interrupts the user's conversation.
          run.taskReplies = result.send;
          if (result.poll_after_ms || revision === run.taskPollRevision) {
            run.taskPollNeeded = Boolean(result.poll_after_ms);
          }
        } catch {
          this.fail(run, "Task sync failed", "Session result remains in chat");
        } finally {
          run.taskPoll = null;
          this.deliverTasks(run);
        }
      }, 1000);
    }

    deliverTasks(run) {
      if (this.current !== run) return;
      if (run.taskReplies?.length && !this.voiceIdle(run)) {
        run.taskPoll = setTimeout(() => {
          run.taskPoll = null;
          this.deliverTasks(run);
        }, 200);
        return;
      }
      for (const reply of run.taskReplies || []) this.send(run, reply);
      run.taskReplies = [];
      this.pollTasks(run);
    }

    playEncoded(encoded) {
      const run = this.current;
      if (!run || !run.ready) return;
      this.play(run, encoded);
    }

    play(run, encoded) {
      if (typeof encoded !== "string") throw new Error("Invalid audio");
      const binary = atob(encoded);
      if (!binary.length || binary.length % 2) throw new Error("Invalid audio");
      const data = new DataView(new ArrayBuffer(binary.length));
      for (let index = 0; index < binary.length; index += 1) data.setUint8(index, binary.charCodeAt(index));
      const buffer = run.audio.createBuffer(1, binary.length / 2, run.settings.audio_sample_rate);
      const samples = buffer.getChannelData(0);
      for (let index = 0; index < samples.length; index += 1) samples[index] = data.getInt16(index * 2, true) / 32768;
      const start = Math.max(run.audio.currentTime, run.playbackAt);
      if (start + buffer.duration - run.audio.currentTime > this.options.maxPlaybackSeconds || run.sources.size >= this.options.maxPendingEvents) throw new Error("Audio backlog");
      const source = run.audio.createBufferSource();
      source.buffer = buffer;
      source.connect(run.audio.destination);
      run.sources.add(source);
      source.onended = () => { run.sources.delete(source); source.disconnect(); };
      source.start(start);
      run.playbackAt = start + buffer.duration;
    }

    clearPlayback(run) {
      for (const source of run.sources) { source.onended = null; source.stop(); source.disconnect(); }
      run.sources.clear();
      run.playbackAt = 0;
    }

    retire(run) {
      if (run.retired || typeof run.settings?.voice_id !== "string") return;
      run.retired = true;
      run.queue.finally(() => this.options.stop(run.slug, run.settings.voice_id)).catch(() => {
        if (!this.current) this.notice("Microphone off", "Stop not confirmed; lease will expire", run.slug);
      });
    }

    stop(detail = "Stopped") {
      const run = this.current;
      if (!run) return;
      this.current = null;
      clearTimeout(run.taskPoll);
      clearTimeout(run.beatTimer);
      run.beatTimer = null;
      clearTimeout(run.connectTimer);
      clearTimeout(run.deadlineTimer);
      if (run.microphone) for (const track of run.microphone.getTracks()) { track.onended = null; track.stop(); }
      if (run.source) run.source.disconnect();
      run.analyser = null;
      if (run.capture) { run.capture.port.onmessage = null; run.capture.port.postMessage({stop: true}); run.capture.disconnect(); }
      this.clearPlayback(run);
      if (run.audio) run.audio.close().catch(() => {});
      if (run.socket) {
        run.socket.onopen = run.socket.onmessage = run.socket.onerror = run.socket.onclose = null;
        run.socket.close();
      }
      this.retire(run);
      this.notice("Stopped", detail, run.slug);
    }

    fail(run, label, detail) {
      if (this.current !== run) return;
      this.stop();
      this.notice(label, detail, run.slug);
    }
  }
  window.TesseraVoice = TesseraVoice;
})();
