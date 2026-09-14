// Entry for the pipeline replay: reads a message from JSON on stdin, runs the
// REAL frontend proseForSpeech + splitSentences, prints chunk analysis.
import { proseForSpeech, splitSentences } from '../src/speech'

let input = ''
process.stdin.on('data', (d) => (input += d))
process.stdin.on('end', () => {
  const { text } = JSON.parse(input)
  const prose = proseForSpeech(text)
  const chunks = splitSentences(prose)
  const lost = text.length - text.replace(/```[\s\S]*?```/g, '').length // rough
  console.log(JSON.stringify({ sourceLen: text.length, proseLen: prose.length, chunks }, null, 1))
})
