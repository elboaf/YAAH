// Regression tests for agent chat markdown rendering (src/markdown.tsx).
// The original bug: MessageBody only special-cased fenced code blocks, so
// headings, emphasis, lists, links and tables rendered as literal text.

import { describe, expect, it } from 'vitest'
import { render, screen, within } from '@testing-library/react'
import { AgentMarkdown } from './markdown'

describe('AgentMarkdown', () => {
  it('renders headings as heading elements, not literal # text', () => {
    render(<AgentMarkdown content={'# Title\n\n## Section'} />)
    expect(screen.getByRole('heading', { level: 1, name: 'Title' })).toBeInTheDocument()
    expect(screen.getByRole('heading', { level: 2, name: 'Section' })).toBeInTheDocument()
    expect(screen.queryByText(/# Title/)).not.toBeInTheDocument()
  })

  it('renders bold, italic and strikethrough', () => {
    render(<AgentMarkdown content={'**bold** *italic* ~~gone~~'} />)
    expect(screen.getByText('bold').closest('strong')).toBeInTheDocument()
    expect(screen.getByText('italic').closest('em')).toBeInTheDocument()
    expect(screen.getByText('gone').closest('del')).toBeInTheDocument()
  })

  it('renders inline code as a chip', () => {
    render(<AgentMarkdown content={'run `npm test` now'} />)
    const chip = screen.getByText('npm test')
    expect(chip).toBeInTheDocument()
    expect(chip.tagName).toBe('CODE')
  })

  it('renders unordered, ordered and task lists', () => {
    render(
      <AgentMarkdown
        content={'- alpha\n- beta\n\n1. one\n2. two\n\n- [x] done\n- [ ] todo'}
      />,
    )
    expect(screen.getByText('alpha')).toBeInTheDocument()
    expect(screen.getByText('beta')).toBeInTheDocument()
    expect(screen.getByText('one')).toBeInTheDocument()
    expect(screen.getByText('two')).toBeInTheDocument()
    // GFM task list: rendered as checkboxes, first checked
    const boxes = screen.getAllByRole('checkbox')
    expect(boxes).toHaveLength(2)
    expect(boxes[0]).toBeChecked()
    expect(boxes[1]).not.toBeChecked()
  })

  it('renders links with safe attributes', () => {
    render(<AgentMarkdown content={'see [docs](https://example.com/x)'} />)
    const link = screen.getByRole('link', { name: 'docs' })
    expect(link).toHaveAttribute('href', 'https://example.com/x')
    expect(link).toHaveAttribute('target', '_blank')
    expect(link).toHaveAttribute('rel', 'noreferrer')
  })

  it('neutralizes javascript: URLs', () => {
    render(<AgentMarkdown content={'[click](javascript:alert(1))'} />)
    // The link is dropped entirely: the label survives only as plain text.
    expect(screen.queryByRole('link')).not.toBeInTheDocument()
    expect(screen.getByText('click')).toBeInTheDocument()
  })

  it('renders GFM tables', () => {
    render(
      <AgentMarkdown
        content={'| a | b |\n| --- | --- |\n| 1 | 2 |'}
      />,
    )
    const table = screen.getByRole('table')
    expect(within(table).getByText('a')).toBeInTheDocument()
    expect(within(table).getByText('2')).toBeInTheDocument()
  })

  it('renders blockquotes and thematic breaks', () => {
    render(<AgentMarkdown content={'> quoted\n\n---'} />)
    expect(screen.getByText('quoted').closest('blockquote')).toBeInTheDocument()
    expect(document.querySelector('hr')).toBeInTheDocument()
  })

  it('renders fenced code blocks as the shared CodeBlock (lang + copy)', () => {
    render(<AgentMarkdown content={'```ts\nconst x = 1\n```'} />)
    expect(screen.getByText('ts')).toBeInTheDocument()
    expect(screen.getByText('copy')).toBeInTheDocument()
    // Text is split across highlight spans; match on the code line container.
    expect(screen.getByText((_, el) => el?.textContent === 'const x = 1')).toBeInTheDocument()
  })

  it('renders fenced blocks without a language tag', () => {
    render(<AgentMarkdown content={'```\nplain code\n```'} />)
    expect(screen.getByText('text')).toBeInTheDocument()
    expect(screen.getByText(/plain code/)).toBeInTheDocument()
  })

  it('survives an unterminated fence mid-stream without crashing', () => {
    render(<AgentMarkdown content={'```python\ndef f():\n  return 1'} />)
    expect(screen.getByText('python')).toBeInTheDocument()
    expect(screen.getByText((_, el) => el?.textContent === 'def f():')).toBeInTheDocument()
  })

  it('suppresses spoken briefing markup from historical and partial messages', () => {
    render(
      <>
        <AgentMarkdown content={'Visible answer. <say>private briefing</say>'} />
        <AgentMarkdown content={'Older row. <say>truncated briefing'} />
        <AgentMarkdown content={'Trailing partial. <sa'} />
      </>,
    )
    expect(screen.getByText(/Visible answer\./)).toBeInTheDocument()
    expect(screen.getByText(/Older row\./)).toBeInTheDocument()
    expect(screen.getByText(/Trailing partial\./)).toBeInTheDocument()
    expect(screen.queryByText(/private briefing|truncated briefing|<say|<sa/)).not.toBeInTheDocument()
  })

  it('renders a full agent-style message end to end', () => {
    const md = [
      '## Plan',
      '',
      'I will **fix** the `render` path:',
      '',
      '1. parse input',
      '2. verify output',
      '',
      '| step | status |',
      '| --- | --- |',
      '| parse | done |',
      '',
      '```bash',
      'npm test',
      '```',
      '',
      'See [docs](https://example.com).',
    ].join('\n')
    render(<AgentMarkdown content={md} />)
    expect(screen.getByRole('heading', { level: 2, name: 'Plan' })).toBeInTheDocument()
    expect(screen.getByText('fix').closest('strong')).toBeInTheDocument()
    expect(screen.getByText('render').tagName).toBe('CODE')
    expect(screen.getByText('parse input')).toBeInTheDocument()
    expect(screen.getByRole('table')).toBeInTheDocument()
    expect(screen.getByText('bash')).toBeInTheDocument()
    expect(screen.getByRole('link', { name: 'docs' })).toHaveAttribute('href', 'https://example.com')
  })
})
