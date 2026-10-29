import React from 'react'

// #564: any uncaught render throw used to unmount the entire React tree —
// the "uniform grey window" (index.html body is bg-zinc-900, an empty #root
// shows as exactly that). This boundary at the app root converts the whole
// class into a captured error with a working reload button.
export class ErrorBoundary extends React.Component<
  { children: React.ReactNode },
  { error: Error | null }
> {
  state = { error: null as Error | null }

  static getDerivedStateFromError(error: Error) {
    return { error }
  }

  componentDidCatch(error: Error, info: React.ErrorInfo) {
    // Console, so a repro from the devtools console keeps the full stack.
    console.error('[ErrorBoundary]', error, info.componentStack)
  }

  render() {
    if (this.state.error) {
      return (
        <div className="flex h-screen w-screen flex-col items-center justify-center gap-4 bg-zinc-900 p-6 text-center">
          <h1 className="font-mono text-sm uppercase tracking-widest text-red-400">
            the frontend hit an error it could not render
          </h1>
          <pre className="max-w-[80ch] whitespace-pre-wrap break-words text-left font-mono text-xs text-zinc-300">
            {this.state.error.message}
            {'\n\n'}
            {this.state.error.stack ?? ''}
          </pre>
          <button
            className="rounded bg-orange-600 px-4 py-2 text-sm text-white hover:bg-orange-500"
            onClick={() => window.location.reload()}
          >
            Reload
          </button>
        </div>
      )
    }
    return this.props.children
  }
}
