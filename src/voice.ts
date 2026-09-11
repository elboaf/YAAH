/** Mic capture for voice dictation: records mono audio in the webview and
 *  hands the composer a 16 kHz 16-bit WAV Blob ready for /api/transcribe.
 *
 *  The whisper.cpp CLI wants 16 kHz PCM WAV, so we resample in the graph
 *  (AudioContext at 16 kHz) instead of shipping a conversion step. Records
 *  in 4-second chunk buffers via ScriptProcessorNode — deprecated API, but
 *  it works identically in every Chromium webview and needs no worklet
 *  bundling. */

const TARGET_RATE = 16000

export class VoiceRecorder {
  private ctx: AudioContext | null = null
  private stream: MediaStream | null = null
  private processor: ScriptProcessorNode | null = null
  private source: MediaStreamAudioSourceNode | null = null
  private mute: GainNode | null = null
  private chunks: Float32Array[] = []
  private captureRate = TARGET_RATE

  async start(): Promise<void> {
    this.stream = await navigator.mediaDevices.getUserMedia({
      audio: { echoCancellation: true, noiseSuppression: true },
    })
    // Some webviews ignore the rate hint; encodeWav downsamples manually if so.
    this.ctx = new AudioContext({ sampleRate: TARGET_RATE })
    this.captureRate = this.ctx.sampleRate
    this.source = this.ctx.createMediaStreamSource(this.stream)
    this.processor = this.ctx.createScriptProcessor(4096, 1, 1)
    this.chunks = []
    this.processor.onaudioprocess = (e) => {
      this.chunks.push(new Float32Array(e.inputBuffer.getChannelData(0)))
    }
    this.mute = this.ctx.createGain()
    this.mute.gain.value = 0 // keep the graph pulling without monitoring
    this.source.connect(this.processor)
    this.processor.connect(this.mute)
    this.mute.connect(this.ctx.destination)
  }

  /** Stop capture and encode everything recorded so far. */
  async stop(): Promise<Blob> {
    try {
      if (this.processor) this.processor.onaudioprocess = null
      this.source?.disconnect()
      this.processor?.disconnect()
      this.mute?.disconnect()
      this.stream?.getTracks().forEach((t) => t.stop())
      await this.ctx?.close()
    } finally {
      this.ctx = null
      this.stream = null
      this.processor = null
      this.source = null
      this.mute = null
    }
    const pcm = mergeChunks(this.chunks)
    this.chunks = []
    return encodeWav(pcm, this.captureRate)
  }
}

function mergeChunks(chunks: Float32Array[]): Float32Array {
  const total = chunks.reduce((n, c) => n + c.length, 0)
  const out = new Float32Array(total)
  let off = 0
  for (const c of chunks) {
    out.set(c, off)
    off += c.length
  }
  return out
}

/** Float32 [-1,1] → 16-bit PCM mono WAV, downsampling by averaging when the
 *  capture rate exceeds the 16 kHz the engines expect. */
function encodeWav(samples: Float32Array, captureRate: number): Blob {
  let data = samples
  let rate = captureRate
  if (captureRate > TARGET_RATE) {
    const ratio = captureRate / TARGET_RATE
    const n = Math.floor(samples.length / ratio)
    const down = new Float32Array(n)
    for (let i = 0; i < n; i++) {
      const start = Math.floor(i * ratio)
      const end = Math.min(Math.floor((i + 1) * ratio), samples.length)
      let sum = 0
      for (let j = start; j < end; j++) sum += samples[j]
      down[i] = end > start ? sum / (end - start) : 0
    }
    data = down
    rate = TARGET_RATE
  }
  const buffer = new ArrayBuffer(44 + data.length * 2)
  const view = new DataView(buffer)
  const ascii = (off: number, s: string) => {
    for (let i = 0; i < s.length; i++) view.setUint8(off + i, s.charCodeAt(i))
  }
  ascii(0, 'RIFF')
  view.setUint32(4, 36 + data.length * 2, true)
  ascii(8, 'WAVE')
  ascii(12, 'fmt ')
  view.setUint32(16, 16, true)
  view.setUint16(20, 1, true) // PCM
  view.setUint16(22, 1, true) // mono
  view.setUint32(24, rate, true)
  view.setUint32(28, rate * 2, true)
  view.setUint16(32, 2, true)
  view.setUint16(34, 16, true)
  ascii(36, 'data')
  view.setUint32(40, data.length * 2, true)
  let off = 44
  for (let i = 0; i < data.length; i++, off += 2) {
    const s = Math.max(-1, Math.min(1, data[i]))
    view.setInt16(off, s < 0 ? s * 0x8000 : s * 0x7fff, true)
  }
  return new Blob([buffer], { type: 'audio/wav' })
}
