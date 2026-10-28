/* Mono PCM16 capture. Audio rates and chunk size come from the voice session. */
class TesseraMicrophone extends AudioWorkletProcessor {
  constructor(options) {
    super();
    const settings = options.processorOptions;
    if (!Number.isFinite(settings.targetRate) || settings.targetRate <= 0 ||
        !Number.isSafeInteger(settings.chunkFrames) || settings.chunkFrames <= 0) {
      throw new Error("Invalid audio settings");
    }
    this.ratio = sampleRate / settings.targetRate;
    this.chunkFrames = settings.chunkFrames;
    this.position = 1;
    this.previous = 0;
    this.offset = 0;
    this.buffer = new ArrayBuffer(this.chunkFrames * 2);
    this.view = new DataView(this.buffer);
    this.active = true;
    this.port.onmessage = () => { this.active = false; };
  }

  process(inputs) {
    if (!this.active) return false;
    const input = inputs[0] && inputs[0][0];
    if (!input) return true;
    for (const sample of input) {
      while (this.position <= 1) {
        const interpolated = this.previous + (sample - this.previous) * this.position;
        const clamped = Math.max(-1, Math.min(1, interpolated));
        const integer = Math.round(clamped * (clamped < 0 ? 32768 : 32767));
        this.view.setInt16(this.offset * 2, integer, true);
        this.offset += 1;
        if (this.offset === this.chunkFrames) {
          this.port.postMessage(this.buffer, [this.buffer]);
          this.buffer = new ArrayBuffer(this.chunkFrames * 2);
          this.view = new DataView(this.buffer);
          this.offset = 0;
        }
        this.position += this.ratio;
      }
      this.position -= 1;
      this.previous = sample;
    }
    return true;
  }
}

registerProcessor("tessera-microphone", TesseraMicrophone);
