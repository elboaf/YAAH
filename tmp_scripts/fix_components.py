import io
p = 'src/components.tsx'
s = io.open(p, encoding='utf8').read()
def cut(a, b):
    global s
    i = s.index(a)
    j = s.index(b, i) + len(b)
    s = s[:i] + s[j:]

s = s.replace("  getAgentBranchStatus,\n", "", 1)
s = s.replace("useError, useAgentBranch, useStatus", "useError, useStatus", 1)
s = s.replace(", type AgentBranchInfo } from './store'", " } from './store'", 1)

s = s.replace("""  const done = tc.result !== undefined
  // A refused merge-back is an error even though it's "just" a tool result:
  // the turn's work did NOT reach the main tree. Red keeps meaning failure.
  // Exceptions: zero_commits means the work was already merged mid-turn -
  // a benign no-op, not a failure. A no-session refusal (issue #103) is
  // the probe shape - "is there anything left to integrate?" after the
  // session drained; nothing exists to fail.
  const r = tc.result as
    | { merged?: unknown; zero_commits?: unknown; reason?: unknown }
    | undefined
  const mergeFailed =
    tc.name === 'git_merge_back' &&
    r?.merged === false &&
    r?.zero_commits !== true &&
    !/no session worktree is bound/i.test(String(r?.reason ?? ''))
  return (
    <span
      className={`inline-flex shrink-0 items-center gap-1.5 rounded px-1.5 py-0.5 font-mono text-[11px] ${
        mergeFailed
          ? 'bg-red-950/60 text-red-300'
          : done
""".replace(' - ', ' \u2014 ').replace(' - ', ' \u2014 ').replace(' - ', ' \u2014 ').replace(' - ', ' \u2014 ').replace("it's \"just\"", 'it\u2019s "just"'), """  const done = tc.result !== undefined
  return (
    <span
      className={`inline-flex shrink-0 items-center gap-1.5 rounded px-1.5 py-0.5 font-mono text-[11px] ${
        done
""")

cut("type GitActivityOperation = {", "\nfunction FileChangesSummary({ summary }: { summary: FileChangeSummary }) {")

old_mv = """export function MessageView({ msg, live }: { msg: ChatMessage; live?: boolean }) {
  // Persisted failure markers (backend writes role='system' when a turn
  // dies): a slim machine line, not a fake agent message. EXCEPTION \u2014 the
  // end-of-turn merge-back handshake (#58 decision 5) persists the same way
  // and must NOT read as an error: a successful merge renders as the same
  // neutral git_merge_back pill the live stream showed; red stays reserved
  // for actual failures (the refusal's reason lives in the expandable
  // detail, exactly like every other tool result).
    if (msg.role === 'system') {
    if (!msg.content) return null
    let merge: { worktree_merge?: Record<string, unknown> } | null = null
    let fileChanges: FileChangeSummary | undefined
    let gitActivity: GitActivitySummary | undefined
    let status: {
      worktree_status?: {
        branch?: string
        base_branch?: string
        worktree_id?: string
        commits?: number
        dirty?: boolean
        worktree?: string
      }
    } | null = null
    try {
      const parsed: unknown = JSON.parse(msg.content)
      if (parsed && typeof parsed === 'object' && 'worktree_merge' in (parsed as object)) {
        merge = parsed as { worktree_merge: Record<string, unknown> }
      } else if (
        parsed &&
        typeof parsed === 'object' &&
        'file_changes' in parsed &&
        parsed.file_changes &&
        typeof parsed.file_changes === 'object'
      ) {
        fileChanges = parsed.file_changes as FileChangeSummary
      } else if (
        parsed &&
        typeof parsed === 'object' &&
        'git_activity' in parsed &&
        parsed.git_activity &&
        typeof parsed.git_activity === 'object'
      ) {
        gitActivity = parsed.git_activity as GitActivitySummary
      } else if (
        parsed &&
        typeof parsed === 'object' &&
        'worktree_status' in (parsed as object)
      ) {
        status = parsed as {
          worktree_status: {
            branch?: string
            base_branch?: string
            worktree_id?: string
            commits?: number
            dirty?: boolean
            worktree?: string
          }
        }
      }
    } catch {
      // not JSON \u2014 a genuine failure marker
    }
    if (fileChanges || gitActivity) {
      return (
        <div className="space-y-1 pl-3">
          {fileChanges && <FileChangesSummary summary={fileChanges} />}
          {gitActivity && <GitActivitySummary summary={gitActivity} />}
        </div>
      )
    }
    if (status) {
      // Turn end itself never integrates work. If commits remain, say plainly
      // that the main workspace is still unchanged; the existing merge action
      // is recovery UI, not a request for users to manage branches routinely.
      const s = status.worktree_status ?? {}
      const count = s.commits ?? 0
      const bits: string[] = [
        count > 0
          ? `${count} committed change(s) are not yet integrated into the main workspace`
          : 'No committed changes are waiting to be integrated',
      ]
      if (count > 0 && s.base_branch) bits.push(`target branch ${s.base_branch}`)
      if (s.dirty) bits.push('additional uncommitted changes are not included')
      return (
        <div className="pl-3">
          <div className="font-mono text-[11px] text-amber-300/90">\u25c6 {bits.join(', ')}</div>
        </div>
      )
    }
    if (merge) {
      const r = merge.worktree_merge ?? {}
      const tc: ToolCall = {
        id: `merge-back-${msg.id}`,
        name: 'git_merge_back',
        result: r,
      }
      return (
        <div className="pl-3">
          <ToolCallRow tc={tc} />
        </div>
      )
    }
"""
new_mv = """export function MessageView({ msg, live }: { msg: ChatMessage; live?: boolean }) {
  // Persisted failure markers (backend writes role='system' when a turn
  // dies): a slim machine line, not a fake agent message.
    if (msg.role === 'system') {
    if (!msg.content) return null
    let fileChanges: FileChangeSummary | undefined
    try {
      const parsed: unknown = JSON.parse(msg.content)
      if (
        parsed &&
        typeof parsed === 'object' &&
        'file_changes' in parsed &&
        parsed.file_changes &&
        typeof parsed.file_changes === 'object'
      ) {
        fileChanges = parsed.file_changes as FileChangeSummary
      }
    } catch {
      // not JSON \u2014 a genuine failure marker
    }
    if (fileChanges) {
      return (
        <div className="space-y-1 pl-3">
          <FileChangesSummary summary={fileChanges} />
        </div>
      )
    }
"""
assert old_mv in s
s = s.replace(old_mv, new_mv, 1)

cut("  // Issue #126: revalidate every persisted pendingMerge warning against the", "  }, [convs])\n")
s = s.replace("  const agentBranchByConv = useAgent((s) => s.agentBranchByConv)\n", "", 1)

s = s.replace("""      pendingMerge={Boolean(agentBranchByConv[String(c.id)]?.pendingMerge)}
      pendingMergeBranch={agentBranchByConv[String(c.id)]?.branch}
      onDismissPendingMerge={
        agentBranchByConv[String(c.id)]?.pendingMerge
          ? () => useAgent.getState().dismissPendingMerge(String(c.id))
          : undefined
      }
""", "")
s = s.replace("""                  // Chat deletion releases the session server-side; the
                  // chip must not keep claiming the agent branch.
                  useAgent.getState().setAgentBranch(key, null)
""", "")

s = s.replace("""  finished,
  pendingMerge,
  pendingMergeBranch,
  onDismissPendingMerge,
""", """  finished,
""")
s = s.replace("""  /** Session branch still has pending work; this survives opening/switching chats. */
  pendingMerge?: boolean
  pendingMergeBranch?: string
  /** Issue #126: clears the unmerged-work warning for this conversation
   *  (display state only \u2014 no merge runs, no branch is deleted). */
  onDismissPendingMerge?: () => void
""", "")

s = s.replace("""        title={
          pendingMerge
            ? `${liveTitle ?? conv.title} \u2014 unmerged session work on ${pendingMergeBranch ?? 'agent branch'}`
            : liveTitle ?? conv.title
        }
        aria-label={
          pendingMerge
            ? `${liveTitle ?? conv.title}, unmerged session work on ${pendingMergeBranch ?? 'agent branch'}`
            : liveTitle ?? conv.title
        }
""", """        title={liveTitle ?? conv.title}
        aria-label={liveTitle ?? conv.title}
""")

old_badge = """        {/* Issue #25: one status slot left of the title, same footprint for
            every state so the row never shifts. Precedence: needs-you (orange,
            pulsing) > pending merge > finished (green bar / red pill) > working dots. */}
        {blocked ? (
          <span
            aria-hidden="true"
            className="run-bar run-bar-orange mr-1.5 shrink-0"
            title="Waiting for you \u2014 a question or approval is pausing this run"
          />
        ) : pendingMerge ? (
          <span
            className="group/badge relative mr-1.5 inline-flex h-3 w-3 shrink-0 items-center justify-center rounded-full border border-orange-500/70 font-mono text-[9px] leading-none text-orange-300"
            title={`Unmerged session work on ${pendingMergeBranch ?? 'agent branch'} \u2014 open chat for details`}
          >
            <span aria-hidden="true">!</span>
            {/* Issue #126: the warning must be clearable by hand \u2014 hover
                reveals an \u00d7 that dismisses it (display state only). */}
            {onDismissPendingMerge && (
              <button
                className="absolute -right-1.5 -top-1.5 hidden h-3 w-3 items-center justify-center rounded-full bg-zinc-700 font-sans text-[8px] leading-none text-zinc-200 hover:bg-zinc-600 group-hover/badge:flex"
                title="Dismiss \u2014 mark this work as handled (does not merge or delete the branch)"
                aria-label={`Dismiss unmerged-work warning for ${pendingMergeBranch ?? 'agent branch'}`}
                onClick={(e) => {
                  e.stopPropagation()
                  onDismissPendingMerge()
                }}
              >
                \u00d7
              </button>
            )}
          </span>
        ) : finished === 'error' ? (
"""
new_badge = """        {/* Issue #25: one status slot left of the title, same footprint for
            every state so the row never shifts. Precedence: needs-you (orange,
            pulsing) > finished (green bar / red pill) > working dots. */}
        {blocked ? (
          <span
            aria-hidden="true"
            className="run-bar run-bar-orange mr-1.5 shrink-0"
            title="Waiting for you \u2014 a question or approval is pausing this run"
          />
        ) : finished === 'error' ? (
"""
assert old_badge in s
s = s.replace(old_badge, new_badge, 1)

s = s.replace("""  onCommandDone,
  agentBranch,
}: {""", """  onCommandDone,
}: {""")
s = s.replace("""  onCommandDone: () => void
  agentBranch: AgentBranchInfo | null
}) {""", """  onCommandDone: () => void
}) {""")
old_iso = """  // Mid-run AND between-turns isolation (adr/0003 revised): the primary
  // selector continues to represent the primary tree, while this separate
  // indicator reports the conversation's agent checkout. Sub-agents are
  // excluded \u2014 the parent turn owns the binding.
  const showAgentBranch = Boolean(agentBranch)
  // An explicit git_merge_back landed the agent branch in the primary tree;
  // the status indicator remains visible and switches to a neutral merged state.
  const agentMerged = Boolean(agentBranch?.merged)
"""
assert old_iso in s
s = s.replace(old_iso, "", 1)
i = s.index("      {showAgentBranch && (")
j = s.index("      {/* checkout dropdown", i)
s = s[:i] + s[j:]

s = s.replace("  const agentBranch = useAgentBranch()\n", "", 1)
s = s.replace("""          onCommandDone={refreshGitInfo}
          agentBranch={agentBranch}
""", """          onCommandDone={refreshGitInfo}
""")
s = s.replace("    setAgentBranch,\n", "", 1)

cut("    } else if (ev.type === 'worktree_bound') {", "    } else if (ev.type === 'tool_progress') {")
old_mb = """      if (ev.name === 'git_merge_back') {
        // An explicit merge landed the agent branch in the primary tree:
        // keep its separate indicator visible, but switch it to neutral.
        const r = ev.result as { merged?: boolean } | undefined
        const cur = useAgent.getState().agentBranchByConv[bufKey]
        if (r?.merged && cur) {
          setAgentBranch(bufKey, { ...cur, merged: true, pendingMerge: false })
        }
      }
"""
assert old_mb in s
s = s.replace(old_mb, "", 1)

io.open(p, 'w', encoding='utf8', newline='').write(s)
print('done')