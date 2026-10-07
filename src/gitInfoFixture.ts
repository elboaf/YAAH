import type { GitInfo } from './api'

/** The default GitInfo the status-strip chip tests mount with — one
 *  factory, many consumers (#350 review: the copy-per-test-file fixture
 *  was edit-amplifying; this follows the attachmentFixture.ts precedent). */
export function baseGitInfo(overrides: Partial<GitInfo> = {}): GitInfo {
  return {
    branch: 'master',
    upstream: null,
    local_hash: 'abc1234',
    remote_hash: null,
    worktree_hash: null,
    worktree_ahead: 0,
    ahead: 0,
    behind: 0,
    added: 0,
    deleted: 0,
    dirty: false,
    untracked: 0,
    changed: 0,
    ...overrides,
  }
}
