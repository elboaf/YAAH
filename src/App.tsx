export default function App() {
  return (
    <div className="flex h-screen w-screen bg-zinc-900 text-zinc-100">
      {/* Three-panel layout: left files+model, center chat, right bash+tools */}
      <aside className="w-1/4 min-w-[220px] border-r border-zinc-800 p-3">
        <h1 className="text-sm font-semibold text-zinc-400">AI Coding Agent</h1>
        <p className="mt-2 text-xs text-zinc-500">Left panel — files &amp; model</p>
      </aside>
      <main className="flex flex-1 flex-col items-center justify-center">
        <p className="text-zinc-500">Center panel — chat</p>
      </main>
      <aside className="w-1/4 min-w-[220px] border-l border-zinc-800 p-3">
        <p className="text-xs text-zinc-500">Right panel — bash &amp; tools</p>
      </aside>
    </div>
  )
}
