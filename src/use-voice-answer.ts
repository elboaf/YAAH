import { useEffect } from 'react'

// Shared push-to-talk answer routing for approval cards.
// Each card type has different answer vocabulary, but the event-parse/guard/subscribe
// shape is identical: subscribe to a window event, filter by callId and submitted state,
// then hand the raw answer text to a card-specific interpretation callback.
export function useVoiceAnswer(
  event: string,
  callId: string,
  submitting: boolean,
  onAnswer: (answer: string) => void
) {
  useEffect(() => {
    const onVoiceAnswer = (e: Event) => {
      const { callId: evtCallId, answer } = (e as CustomEvent<{ callId: string; answer: string }>).detail
      if (evtCallId !== callId || !answer || submitting) return
      onAnswer(answer)
    }
    window.addEventListener(event, onVoiceAnswer)
    return () => window.removeEventListener(event, onVoiceAnswer)
  }, [event, callId, submitting, onAnswer])
}
