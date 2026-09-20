/**
 * Notification chimes (#29): a short "run finished" chime and a distinct
 * "question needs an answer" chime, synthesized with WebAudio — no audio
 * assets, no new dependency. Both are sub-second, moderate volume, and
 * fire at most once per event (the callers own the once-per-event guards;
 * this module only owns "can we play at all").
 *
 * Autoplay policy: the AudioContext is created lazily on the first chime
 * attempt. If the browser suspended it (no user gesture yet in this
 * session), the chime is skipped silently — a missed chime is better than
 * a console error storm. The first user interaction (click/keydown) is
 * hooked once to resume a suspended context so later chimes work.
 */

let ctx: AudioContext | null = null
let interactionHooked = false

function getCtx(): AudioContext | null {
  if (typeof window === 'undefined') return null
  const AC = window.AudioContext ?? (window as any).webkitAudioContext
  if (!AC) return null
  if (!ctx) {
    ctx = new AC()
    if (!interactionHooked) {
      interactionHooked = true
      const resume = () => {
        if (ctx && ctx.state === 'suspended') void ctx.resume()
      }
      window.addEventListener('pointerdown', resume, { passive: true })
      window.addEventListener('keydown', resume, { passive: true })
    }
  }
  return ctx
}

/** Two-tone "run finished" chime: C6 -> E6, ~0.45s, soft attack/decay. */
export function playRunFinished(): void {
  const c = getCtx()
  if (!c) return
  if (c.state === 'suspended') {
    void c.resume().catch(() => {})
    if (c.state === 'suspended') return
  }
  const t = c.currentTime
  const master = c.createGain()
  master.gain.value = 0.12
  master.connect(c.destination)
  const tone = (freq: number, start: number, dur: number) => {
    const osc = c.createOscillator()
    const gain = c.createGain()
    osc.type = 'sine'
    osc.frequency.value = freq
    gain.gain.setValueAtTime(0, t + start)
    gain.gain.linearRampToValueAtTime(1, t + start + 0.02)
    gain.gain.exponentialRampToValueAtTime(0.001, t + start + dur)
    osc.connect(gain)
    gain.connect(master)
    osc.start(t + start)
    osc.stop(t + start + dur + 0.05)
  }
  tone(1046.5, 0, 0.35) // C6
  tone(1318.5, 0.12, 0.35) // E6
}

/** Two-tone "question pending" chime: E6 -> C6 (inverted), slightly longer. */
export function playQuestionPending(): void {
  const c = getCtx()
  if (!c) return
  if (c.state === 'suspended') {
    void c.resume().catch(() => {})
    if (c.state === 'suspended') return
  }
  const t = c.currentTime
  const master = c.createGain()
  master.gain.value = 0.12
  master.connect(c.destination)
  const tone = (freq: number, start: number, dur: number) => {
    const osc = c.createOscillator()
    const gain = c.createGain()
    osc.type = 'sine'
    osc.frequency.value = freq
    gain.gain.setValueAtTime(0, t + start)
    gain.gain.linearRampToValueAtTime(1, t + start + 0.02)
    gain.gain.exponentialRampToValueAtTime(0.001, t + start + dur)
    osc.connect(gain)
    gain.connect(master)
    osc.start(t + start)
    osc.stop(t + start + dur + 0.05)
  }
  tone(1318.5, 0, 0.4) // E6
  tone(1046.5, 0.16, 0.4) // C6
}
