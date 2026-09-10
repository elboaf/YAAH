import { ActivityPanel, ChatPanel, Sidebar } from './components'

export default function App() {
  return (
    <div className="flex h-screen w-screen bg-zinc-900 text-zinc-100">
      <Sidebar />
      <ChatPanel />
      <ActivityPanel />
    </div>
  )
}
