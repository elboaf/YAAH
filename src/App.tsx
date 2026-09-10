import { ChatPanel, Sidebar } from './components'

export default function App() {
  return (
    <div className="flex h-screen w-screen bg-zinc-900 text-zinc-100">
      <Sidebar />
      <ChatPanel />
      <aside className="hidden w-1/4 min-w-[220px] border-l border-zinc-800 p-3 lg:block">
        <p className="text-xs text-zinc-500">Right panel — bash &amp; tools (coming soon)</p>
      </aside>
    </div>
  )
}
