// Preset catalog for the MCP add flow (issue #129): a form-filler, not a
// marketplace. Ships with YAAH — no network fetch, no registry. Each entry
// is verifiably correct (published package, real flags); a wrong preset is
// worse than no preset. Keep the list short and curated.

export interface McpPreset {
  id: string
  label: string
  kind: 'stdio' | 'remote'
  // stdio: command + args template; remote: URL template.
  command?: string
  args?: string[]
  url?: string
  transport?: 'sse'
  // Keys the user is likely to need, offered as empty editable fields.
  envKeys?: string[]
  headerKeys?: string[]
  // One-line trust summary shown with the preset.
  trust: string
}

export const MCP_PRESETS: McpPreset[] = [
  {
    id: 'filesystem',
    label: 'Filesystem',
    kind: 'stdio',
    command: 'npx',
    args: ['-y', '@modelcontextprotocol/server-filesystem', 'C:\\path\\to\\folder'],
    trust: 'Runs locally with your permissions — it can read and write the folder you allow.',
  },
  {
    id: 'fetch',
    label: 'Fetch (web pages)',
    kind: 'stdio',
    command: 'uvx',
    args: ['mcp-server-fetch'],
    trust: 'Runs locally; fetches web pages on request.',
  },
  {
    id: 'github',
    label: 'GitHub',
    kind: 'stdio',
    command: 'npx',
    args: ['-y', '@modelcontextprotocol/server-github'],
    envKeys: ['GITHUB_PERSONAL_ACCESS_TOKEN'],
    trust: 'Runs locally; talks to the GitHub API with your token.',
  },
  {
    id: 'memory',
    label: 'Memory (knowledge graph)',
    kind: 'stdio',
    command: 'npx',
    args: ['-y', '@modelcontextprotocol/server-memory'],
    trust: 'Runs locally; stores its graph in your user directory.',
  },
  {
    id: 'playwright',
    label: 'Playwright (browser)',
    kind: 'stdio',
    command: 'npx',
    args: ['-y', '@playwright/mcp@latest'],
    trust: 'Runs locally and drives a browser with your permissions.',
  },
  {
    id: 'custom',
    label: 'Custom…',
    kind: 'stdio',
    trust: 'Local commands run with your permissions; remote URLs receive the headers you configure.',
  },
]
