/** Read-aloud playback: queue the backend's WAV chunks through Web Audio,
 *  plus the tiny zustand store the header toggle, per-message stop, and
 *  Composer hooks share.
 *
 *  Division of labor: the backend turns prose into WAV (Kokoro via
 *  sherpa-onnx); this module owns playback â€” chunk queue with one-chunk
 *  prefetch so long reads play gaplessly, replace semantics (a new utterance
 *  cuts the old one off mid-word), and hard stop. Markdown â†’ prose and
 *  sentence chunking mirror backend/agent/speak.py (the frontend has the raw
 *  markdown; shipping it to the server for string munging would be a
 *  roundtrip with no upside). */

import { create } from 'zustand'

import { ttsStop, ttsSynthesize } from './api'

// ---------------------------------------------------------------- text prep

// Emoji: espeak-ng (Kokoro's text front-end) looks emoji up in its
// dictionary and SPEAKS THEIR NAMES (~0.8-1.7s each, probe-verified), so
// they are stripped before synthesis. Same ranges as speak.py; â†’/â†
// (U+2190-21FF) deliberately kept â€” legitimate technical prose.
const EMOJI =
  /[\u{1F000}-\u{1FAFF}\u{2600}-\u{27BF}\u{2B00}-\u{2BFF}\u{FE00}-\u{FE0F}\u{200D}\u{20E3}]+/gu

/** Remove emoji and repair the spacing they leave behind. */
function stripEmoji(text: string): string {
  return text
    .replace(EMOJI, '')
    .replace(/\s+([,.!?;:])/g, '$1')
    .replace(/ {2,}/g, ' ')
    .trim()
}

/** Markdown â†’ speakable prose: code fences and indented blocks become
 *  pauses (dropped), tables drop, links keep their label, emphasis strips.
 *  Mirrors speak.prose_for_speech. */
export function proseForSpeech(md: string, maxChars = 4000): string {  if (!md) return ''
  let t = md
  t = t.replace(/```[\s\S]*?```/g, '\n\n')
  t = t.replace(/^(?:    |\t).*(?:\n|$)+/gm, '\n\n')
  t = t.replace(/^[ \t]*\|.*\|[ \t]*$/gm, '')
  t = t.replace(/!\[[^\]]*\]\([^)]*\)/g, '')
  t = t.replace(/\[([^\]]+)\]\([^)]*\)/g, '$1')
  t = t.replace(/`([^`]+)`/g, '$1')
  t = t.replace(/^#{1,6}\s+/gm, '')
  t = t.replace(/^\s*[-*+]\s+/gm, '')
  // Emphasis: pair delimiters only when not glued to word chars, so code
  // identifiers (tts_enabled, max_tokens) keep their underscores.
  t = t.replace(
    /\*\*([^*]+)\*\*|\*([^*]+)\*|__([^_]+)__|(?<![A-Za-z0-9_])_([^_]+)_(?![A-Za-z0-9_])/g,
    (_m, a, b, c, d) => a || b || c || d || '',
  )
  t = t.replace(/<[^>]+>/g, '')
  t = stripEmoji(t)
  t = t.replace(/[ \t]+/g, ' ')
  t = t.replace(/\n{3,}/g, '\n\n')
  t = t.trim()
  if (t.length <= 4000) return t
  const cut = t.slice(0, 4000)
  const last = Math.max(cut.lastIndexOf('. '), cut.lastIndexOf('! '), cut.lastIndexOf('? '))
  return (last > 2000 ? cut.slice(0, last + 1) : cut).trim()
}

/** Live-stream variant of proseForSpeech: a code fence still open in the
 *  stream (odd ``` count) is not speech-eligible yet \u2014 drop everything
 *  from the last fence opener on so the narrator never reads half a code
 *  block. At emission end the fence closes and proseForSpeech sees it. */
export function liveProse(md: string): string {
  if (!md) return ''
  const parts = md.split('```')
  if (parts.length % 2 === 0) md = parts.slice(0, -1).join('```')
  return proseForSpeech(md)
}
// ---------------------------------------------------------------- briefing
// Two-channel split (#66): the spoken line is a briefing, not a read-aloud.
// Mirrors speak.py's SAY_MAX_CHARS / spoken_line / heuristic_briefing â€” the
// cap lives in one place per side and both sides stay in sync.

export const SAY_MAX_CHARS = 400

const SENT_END = /([.!?]+["')\]]?)\s+/g

/** Last-resort sentence-boundary truncation at the briefing budget. */
function clip(text: string, maxChars = SAY_MAX_CHARS): string {
  if (text.length <= maxChars) return text
  const cut = text.slice(0, maxChars)
  let last = -1
  for (const m of cut.matchAll(/([.!?]+["')\]]?)\s+/g)) last = m.index + m[0].length
  return (last > maxChars / 2 ? cut.slice(0, last) : cut).trim()
}

/** Fallback briefing without a model call: first sentence of the first
 *  paragraph plus the final sentence, clipped to the budget. Mirrors
 *  speak.heuristic_briefing. */
export function heuristicBriefing(md: string, maxChars = SAY_MAX_CHARS): string {
  const prose = proseForSpeech(md, Number.MAX_SAFE_INTEGER)
  const paras = prose.split('\n').map((p) => p.trim()).filter(Boolean)
  if (!paras.length) return ''
  const first = paras[0]
  const openEnd = first.match(/^[^]*?([.!?]+["')\]]?)(\s+|$)/)
  const opening = (openEnd && openEnd.index !== undefined ? first.slice(0, openEnd.index + openEnd[1].length) : first).trim()
  const tail = paras[paras.length - 1]
  let closeEnd = -1
  let closeLen = 0
  for (const m of tail.matchAll(/([.!?]+["')\]]?)(\s+|$)/g)) {
    closeEnd = m.index
    closeLen = m[1].length
  }
  const closing = (closeEnd >= 0 ? tail.slice(0, closeEnd + closeLen) : tail).trim()
  if (closing && closing !== opening && opening.length + closing.length + 1 <= maxChars) {
    return `${opening} ${closing}`
  }
  return clip(opening.length >= closing.length ? opening : closing, maxChars)
}

/** The final TTS input for a turn: the model-emitted briefing when present,
 *  else the heuristic, else the old truncated verbatim prose. Mirrors
 *  speak.spoken_line â€” everything passes through proseForSpeech and the
 *  hard cap is enforced here. */
export function spokenLine(briefing: string | null | undefined, md: string): string {
  const prose = proseForSpeech(md, Number.MAX_SAFE_INTEGER)
  const source = (briefing ?? '').trim()
  if (source) {
    const line = proseForSpeech(source, Number.MAX_SAFE_INTEGER)
    if (line.length <= SAY_MAX_CHARS) return line
    return heuristicBriefing(line) || clip(line)
  }
  return heuristicBriefing(prose) || clip(prose)
}

const ABBREV = new Set([
  'e.g', 'i.e', 'etc', 'vs', 'cf', 'dr', 'mr', 'mrs', 'ms', 'prof', 'st',
  'sr', 'jr', 'fig', 'no', 'vol', 'ch', 'sec', 'approx', 'inc', 'ltd', 'co',
])

/** Prose â†’ synthesis chunks: sentences merged up to ~80 chars for natural
 *  prosody, hard-split at ~300 so first audio arrives fast. Mirrors
 *  speak.split_sentences. */
export function splitSentences(text: string, minLen = 80, maxLen = 300): string[] {
  const flat = text.replace(/[ \t]+/g, ' ').trim()
  if (!flat) return []
  const raw: string[] = []
  let start = 0
  const re = /([.!?]+["')\]]?)\s+/g
  let m: RegExpExecArray | null
  while ((m = re.exec(flat))) {
    const end = m.index + m[0].length
    const tail = flat.slice(end, end + 6)
    const nextWord = tail.match(/[A-Za-z]+/)
    const before = flat.slice(Math.max(0, m.index - 6), m.index + 1)
    const lastWord = before.match(/([A-Za-z.]+)$/)
    // A lowercase continuation ("e.g. config.json") or a known abbreviation
    // ("Dr.") does not end the sentence.
    if (nextWord && !nextWord[0][0].match(/[A-Z0-9]/) && nextWord[0].toLowerCase() !== 'i') continue
    if (lastWord && ABBREV.has(lastWord[1].replace(/\.$/, '').toLowerCase())) continue
    raw.push(flat.slice(start, end))
    start = end
    re.lastIndex = end
  }
  if (start < flat.length) raw.push(flat.slice(start))

  const out: string[] = []
  let buf = ''
  for (let s of raw) {
    if (buf && buf.length + s.length > maxLen) {
      out.push(buf.trim())
      buf = ''
    }
    while (s.length > maxLen) {
      let cut = s.lastIndexOf(' ', maxLen)
      if (cut < maxLen / 2) cut = maxLen
      out.push(s.slice(0, cut).trim())
      s = s.slice(cut)
    }
    buf += s
    if (buf.length >= minLen) {
      out.push(buf.trim())
      buf = ''
    }
  }
  if (buf.trim()) out.push(buf.trim())
  return out.filter(Boolean)
}

// ---------------------------------------------------------------- player

type Phase = { speaking: boolean; msgId: string | null }

/** Error shape from ttsSynthesize: `superseded` marks the benign 409 a
 *  replacement utterance causes (drop silently); `status` 409 otherwise
 *  means the model went missing. */
type SynthError = Error & { superseded?: boolean; status?: number }

class SpeechPlayer {
  private ctx: AudioContext | null = null
  private sources: AudioBufferSourceNode[] = []
  private generation = 0
  /** Monotonic utterance generation, sent with every chunk request: chunks
   *  of one utterance share it, so concurrent prefetch never supersedes a
   *  live chunk â€” only stop(floor) raises the backend's floor past it. */
  private utteranceId = 0
  private listeners = new Set<(p: Phase) => void>()
  private phase: Phase = { speaking: false, msgId: null }
  /** Generation of the utterance currently playing (0 = none). The store's
   *  hard-stop paths floor the backend at this so the stopped utterance's
   *  own in-flight chunks abort too. */
  private activeEpoch = 0

  subscribe(fn: (p: Phase) => void): () => void {
    this.listeners.add(fn)
    return () => this.listeners.delete(fn)
  }

  private set(speaking: boolean, msgId: string | null) {
    this.phase = { speaking, msgId }
    for (const fn of this.listeners) fn(this.phase)
  }

  get current(): Phase {
    return this.phase
  }

  private ensureCtx(): AudioContext {
    if (!this.ctx || this.ctx.state === 'closed') {
      this.ctx = new AudioContext()
    }
    if (this.ctx.state === 'suspended') void this.ctx.resume()
    return this.ctx
  }

  /** Fetch + decode one chunk. Public so the queue can prefetch the next
   *  chunk while the current one plays. */
  private async fetchBuffer(
    text: string,
    signal: AbortSignal | undefined,
    opts: { voice?: string; speed?: number } | undefined,
    epoch: number,
  ): Promise<AudioBuffer> {
    const blob = await ttsSynthesize(text, signal, { ...opts, epoch })
    const buf = await blob.arrayBuffer()
    return await this.ensureCtx().decodeAudioData(buf)
  }

  private play(buf: AudioBuffer, gen: number): Promise<void> {
    return new Promise((resolve) => {
      if (gen !== this.generation || !this.ctx) return resolve()
      const src = this.ctx.createBufferSource()
      src.buffer = buf
      src.connect(this.ctx.destination)
      src.onended = () => {
        this.sources = this.sources.filter((s) => s !== src)
        resolve()
      }
      this.sources.push(src)
      src.start()
    })
  }

  /** Create/resume the AudioContext inside a user gesture (the toggle
   *  click). Without this, Chromium may leave the context suspended and
   *  the first auto-spoken turn stays silent. */
  warm(): void {
    const ctx = this.ensureCtx()
    if (ctx.state === 'suspended') void ctx.resume()
  }

  /** Stop whatever is playing/queued right now (replace semantics). Pings
   *  the backend with `floor`: every utterance generation <= floor aborts
   *  at its next sentence boundary. Pass the CURRENT utterance's id to stop
   *  it, or (newId - 1) when replacing so only older ones die. */
  stop(nextFloor = 0): void {
    this.generation++
    const hadAudio = this.sources.length > 0
    for (const s of this.sources) {
      try {
        s.stop()
      } catch {
        /* already stopped */
      }
    }
    this.sources = []
    if (this.phase.speaking || hadAudio) ttsStop(nextFloor)
    if (this.phase.speaking) this.set(false, null)
  }

  /** Generation of the utterance currently playing (0 = none). */
  get currentEpoch(): number {
    return this.activeEpoch
  }

  /** Clear the active-utterance marker after a hard stop so a later stop
   *  never floors at a stale epoch. */
  clearActiveEpoch(): void {
    this.activeEpoch = 0
  }

  /** Speak a list of prose chunks in order, prefetching one chunk ahead so
   *  playback is gapless as long as synthesis keeps up (it does: per-chunk
   *  synthesis is faster than that chunk's playback). Resolves when the
   *  utterance finishes or is replaced/stopped. */
  async speak(
    msgId: string,
    chunks: string[],
    onError?: (e: SynthError, status?: number) => void,
    opts?: { voice?: string; speed?: number },
  ): Promise<void> {
    // Claim this utterance's generation first, then floor the backend at
    // everything OLDER (superseded(e) == e <= floor): previous utterance's
    // in-flight chunks abort; our own (id = floor + 1) survive.
    const epoch = ++this.utteranceId
    this.stop(epoch - 1)
    if (!chunks.length) return
    const gen = ++this.generation
    this.activeEpoch = epoch
    this.set(true, msgId)
    try {
      let prefetch: Promise<AudioBuffer> | null = this.fetchBuffer(
        chunks[0],
        undefined,
        opts,
        epoch,
      )
      for (let i = 0; i < chunks.length; i++) {
        const current = prefetch
        prefetch =
          i + 1 < chunks.length ? this.fetchBuffer(chunks[i + 1], undefined, opts, epoch) : null
        if (!current) break
        let buf: AudioBuffer
        try {
          buf = await current
        } catch (e) {
          const err = e as SynthError
          if (err.name === 'AbortError' || gen !== this.generation) return
          if (err.superseded) return // replaced mid-fetch: benign, stay silent
          onError?.(err, err.status)
          return
        }
        if (gen !== this.generation) return
        await this.play(buf, gen)
      }
    } finally {
      this.activeEpoch = 0
      if (gen === this.generation) this.set(false, null)
    }
  }

  /** Live narration: one appendable utterance per run. `begin` claims the
   *  epoch and starts the chunk loop on an empty queue; `append` feeds the
   *  NEXT completed sentence-chunk (chunks handed in one at a time, so the
   *  one-chunk prefetch stays intact); `end` (optional) stops feeding \u2014
   *  playback drains naturally. Appended chunks share the utterance's
   *  epoch so they never self-supersede; any speak()/stop() from elsewhere
   *  supersedes the whole stream (generation check in the queue loop). */
  beginStream(msgId: string, onError?: (e: SynthError, status?: number) => void) {
    const epoch = ++this.utteranceId
    this.stop(epoch - 1)
    const gen = ++this.generation
    this.activeEpoch = epoch
    this.set(true, msgId)
    let queue: Promise<void> = Promise.resolve()
    let done = false
    const loop = async (text: string) => {
      let buf: AudioBuffer
      try {
        buf = await this.fetchBuffer(text, undefined, undefined, epoch)
      } catch (e) {
        const err = e as SynthError
        if (err.name === 'AbortError' || gen !== this.generation) return
        if (err.superseded) return
        onError?.(err, err.status)
        return
      }
      if (gen !== this.generation) return
      await this.play(buf, gen)
    }
    return {
      append: (chunk: string) => {
        if (done || gen !== this.generation || !chunk.trim()) return
        queue = queue.then(() => loop(chunk))
      },
      end: () => {
        done = true
        void queue.then(() => {
          this.activeEpoch = 0
          if (gen === this.generation) this.set(false, null)
        })
      },
    }
  }
}

export const speechPlayer = new SpeechPlayer()

// ---------------------------------------------------------------- store

interface TtsState {
  /** Read-aloud enabled (persisted config voice.tts_enabled). */
  enabled: boolean
  /** Model downloaded and resolvable. */
  ready: boolean
  /** Live playback state (mirrored from the player). */
  speaking: boolean
  speakingMsgId: string | null
  /** Last playback error (shown as the toggle's tooltip; auto-clears). */
  error: string | null
  /** Hydrate from GET /api/tts/status. */
  syncFromServer: (s: { available: boolean; tts_enabled: boolean }) => void
  setEnabled: (on: boolean) => void
  setReady: (ready: boolean) => void
  setError: (e: string | null) => void
  /** Begin a live mid-run narration; returns the feed handle, or null when
   *  TTS is off/not ready (the narrate effect skips everything). */
  beginNarration: (msgId: string) => {
    append: (chunk: string) => void
    end: () => void
  } | null
  /** Speak an assistant message (replaces any current utterance). `briefing`
   *  is the backend's spoken line (#66) when one arrived; the markdown is
   *  the fallback path (heuristic briefing, then truncated verbatim). */
  speakMessage: (msgId: string, markdown: string, briefing?: string | null) => void
  /** Speak an ask_user question; options stay visual. */
  speakQuestion: (callId: string, question: string) => void
  stop: () => void
  /** One-off preview of a voice (Settings); ignores the enabled flag. */
  previewVoice: (voice: string, speed: number) => void
}
export const useTts = create<TtsState>((set, get) => {
  // Wire the player's phase into the store once.
  speechPlayer.subscribe(({ speaking, msgId }) => {
    set({ speaking, speakingMsgId: msgId })
  })
  /** Shared playback-failure path: a superseded 409 is benign (a newer
   *  utterance replaced this one â€” stay silent, no error UI); any other 409
   *  means the model went missing â€” flip ready so the UI offers the
   *  download again. */
  const fail = (e: SynthError, status?: number) => {
    if (e.superseded) return
    set({
      error: status === 409 ? 'voice model missing â€” enable it in Settings' : e.message,
      ...(status === 409 ? { ready: false } : {}),
    })
    setTimeout(() => set({ error: null }), 8000)
  }
  return {
    enabled: false,
    ready: false,
    speaking: false,
    speakingMsgId: null,
    error: null,
    syncFromServer: ({ available, tts_enabled }) =>
      set({ ready: available, enabled: tts_enabled }),
    setEnabled: (on) => {
      if (on) speechPlayer.warm()
      // Muting floors the backend at the playing utterance: its in-flight
      // chunks abort too (same path as the per-message stop).
      useTts.getState().stop()
      set({ enabled: on, error: null })
    },
    setReady: (ready) => set({ ready }),
    setError: (error) => set({ error }),
    speakMessage: (msgId, markdown, briefing) => {
      const { enabled, ready } = useTts.getState()
      if (!enabled || !ready) return
      const prose = spokenLine(briefing, markdown)
      const chunks = splitSentences(prose)
      if (!chunks.length) return
      void speechPlayer.speak(msgId, chunks, fail)
    },
    beginNarration: (msgId) => {
      const { enabled, ready } = useTts.getState()
      if (!enabled || !ready) return null
      return speechPlayer.beginStream(msgId, fail)
    },
    speakQuestion: (callId, question) => {
      const { enabled, ready } = useTts.getState()
      if (!enabled || !ready) return
      const chunks = splitSentences(proseForSpeech(question))
      if (!chunks.length) return
      void speechPlayer.speak(`q-${callId}`, chunks, fail)
    },
    stop: () => {
      // Hard stop (mute toggle, per-message stop): floor at the utterance
      // that is playing so its own in-flight chunks abort as well.
      speechPlayer.stop(speechPlayer.currentEpoch)
      speechPlayer.clearActiveEpoch()
    },
    previewVoice: (voice, speed) => {
      speechPlayer.warm()
      void speechPlayer.speak(
        'preview',
        ['Hello. This is how I will read responses to you.'],
        fail,
        { voice, speed },
      )
    },
  }
})
