/* Local speech pipe: hold to record, release to send, then hear the reply.
   The server owns the speech-to-text and text-to-speech calls, so the page only
   captures PCM and plays back what comes home. */
(() => {
  "use strict";

  function encodeBase64(bytes) {
    let text = "";
    const step = 0x8000;
    for (let index = 0; index < bytes.length; index += step) {
      text += String.fromCharCode.apply(null, bytes.subarray(index, index + step));
    }
    return btoa(text);
  }

  function decodeBase64(text) {
    const raw = atob(text);
    const bytes = new Uint8Array(raw.length);
    for (let index = 0; index < raw.length; index += 1) bytes[index] = raw.charCodeAt(index);
    return bytes;
  }

  function joinChunks(chunks) {
    let total = 0;
    for (const chunk of chunks) total += chunk.byteLength;
    const bytes = new Uint8Array(total);
    let offset = 0;
    for (const chunk of chunks) {
      bytes.set(new Uint8Array(chunk), offset);
      offset += chunk.byteLength;
    }
    return bytes;
  }

  class TesseraLocalVoice {
    constructor(options) {
      this.options = options;
      this.run = null;
      this.starting = null;
      this.playback = null;
      this.phaseValue = "off";
      this.deviceId = "";
    }

    running() { return this.run !== null || this.starting !== null; }
    phase() { return this.phaseValue; }

    notice(label, detail, slug) { this.options.onState(label, detail, slug); }

    level() {
      const run = this.run;
      if (!run || !run.analyser) return 0;
      const data = new Float32Array(run.analyser.fftSize);
      run.analyser.getFloatTimeDomainData(data);
      let sum = 0;
      for (const value of data) sum += value * value;
      return Math.min(1, Math.sqrt(sum / data.length) * 4);
    }

    waveform(points) {
      const run = this.run;
      if (!run || !run.analyser) return null;
      const data = new Float32Array(run.analyser.fftSize);
      run.analyser.getFloatTimeDomainData(data);
      const step = Math.max(1, Math.floor(data.length / points));
      const out = [];
      for (let index = 0; index < points; index += 1) out.push(data[index * step] || 0);
      return out;
    }

    constraints() {
      const audio = {channelCount: 1, echoCancellation: true, noiseSuppression: true};
      if (this.deviceId) audio.deviceId = {exact: this.deviceId};
      return {audio};
    }

    async start(slug) {
      if (this.run || this.starting) return;
      this.starting = this.open(slug);
      try {
        await this.starting;
      } finally {
        this.starting = null;
      }
    }

    async open(slug) {
      if (window.isSecureContext === false) {
        this.notice("Secure connection required", "Needs HTTPS", slug);
        return;
      }
      if (typeof navigator === "undefined" || !navigator.mediaDevices || !navigator.mediaDevices.getUserMedia
          || !window.AudioContext || !window.AudioWorkletNode) {
        this.notice("Audio unavailable", "Browser lacks mic/audio support", slug);
        return;
      }
      this.notice("Starting", "Allow microphone", slug);
      let stream;
      try {
        stream = await navigator.mediaDevices.getUserMedia(this.constraints());
      } catch (_) {
        this.notice("Microphone blocked", "Permission denied", slug);
        return;
      }
      const context = new AudioContext();
      try {
        await context.audioWorklet.addModule(this.options.workletUrl);
      } catch (_) {
        for (const track of stream.getTracks()) track.stop();
        await context.close();
        this.notice("Audio unavailable", "Microphone processor did not load", slug);
        return;
      }
      const source = context.createMediaStreamSource(stream);
      const node = new AudioWorkletNode(context, "tessera-microphone", {
        processorOptions: {targetRate: this.options.targetRate, chunkFrames: this.options.chunkFrames},
      });
      const analyser = context.createAnalyser();
      const chunks = [];
      node.port.onmessage = (event) => chunks.push(event.data);
      source.connect(node);
      source.connect(analyser);
      this.run = {slug, stream, context, node, analyser, chunks};
      this.phaseValue = "listening";
      this.notice("Listening", "Release to send", slug);
    }

    async stop() {
      if (this.starting) {
        try { await this.starting; } catch (_) { /* the failure already spoke */ }
      }
      const run = this.run;
      if (!run) return null;
      this.run = null;
      run.node.port.postMessage("stop");
      for (const track of run.stream.getTracks()) track.stop();
      const pcm = joinChunks(run.chunks);
      await run.context.close();
      if (!pcm.length) {
        this.phaseValue = "off";
        this.notice("No speech", "Nothing recorded", run.slug);
        return null;
      }
      this.phaseValue = "thinking";
      this.notice("Thinking", "Waiting for the reply", run.slug);
      let body;
      try {
        body = await this.options.request(
          "/api/sessions/" + encodeURIComponent(run.slug) + "/voice/local", "POST",
          {audio: encodeBase64(pcm), rate: this.options.targetRate});
      } catch (error) {
        this.phaseValue = "off";
        this.notice("Voice error", error && error.message ? error.message : "Request failed", run.slug);
        return null;
      }
      await this.speak(body, run.slug);
      return body;
    }

    async speak(body, slug) {
      const bytes = decodeBase64(body.audio);
      const channels = body.channels === 2 ? 2 : 1;
      const frames = Math.floor(bytes.length / (2 * channels));
      if (!frames || !body.rate) {
        this.phaseValue = "off";
        this.notice("Voice error", "The reply had no audio", slug);
        return;
      }
      const context = new AudioContext();
      const buffer = context.createBuffer(channels, frames, body.rate);
      const view = new DataView(bytes.buffer, bytes.byteOffset, bytes.byteLength);
      for (let channel = 0; channel < channels; channel += 1) {
        const lane = new Float32Array(frames);
        for (let frame = 0; frame < frames; frame += 1) {
          lane[frame] = view.getInt16((frame * channels + channel) * 2, true) / 32768;
        }
        buffer.copyToChannel(lane, channel);
      }
      const source = context.createBufferSource();
      source.buffer = buffer;
      source.connect(context.destination);
      this.phaseValue = "speaking";
      this.notice("Speaking", "Reply", slug);
      source.onended = () => {
        context.close();
        if (this.playback && this.playback.source === source) this.playback = null;
        this.phaseValue = "off";
        this.notice("Ready", "Hold to talk", slug);
      };
      this.playback = {source, context};
      source.start();
    }

    async cancel() {
      if (this.starting) {
        try { await this.starting; } catch (_) { /* nothing to clean up */ }
      }
      if (this.run) {
        for (const track of this.run.stream.getTracks()) track.stop();
        await this.run.context.close();
        this.run = null;
      }
      if (this.playback) {
        try { this.playback.source.stop(); } catch (_) { /* already ended */ }
        this.playback.context.close();
        this.playback = null;
      }
      this.phaseValue = "off";
    }
  }

  window.TesseraLocalVoice = TesseraLocalVoice;
})();
