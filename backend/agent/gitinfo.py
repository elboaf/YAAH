"""Git workspace info for the UI (current branch, dirty state, commit
positions), kept out of the tool layer on purpose: this is app state, not a
model tool.

Cheap by design — stat() the .git/HEAD file and only spawn git when it
changed. The chat panel re-polls the endpoint every couple of seconds, so a
terminal `git checkout` reflects in the UI without any push channel, and a
non-repo workspace costs one failed stat per poll.
"""
import asyncio
import os
import re
import time
from pathlib import Path

from backend.agent import gitexec
from backend.agent.remote import parse_ns

# (resolved workspace root) -> (mtime_ns captured at last read, branch text;
# remote entries carry captured-at 0.0 and are gated by _branch_times)
_cache: dict[str, tuple[float, str | None]] = {}

# #333: remote roots have no HEAD to stat, so their branch-cache entries
# are time-gated instead of mtime-gated.
_branch_times: dict[str, float] = {}
_BRANCH_TTL = 2.0  # seconds; matches the UI poll cadence

# (resolved workspace root) -> (monotonic time captured, info dict). One git
# spawn burst per TTL per workspace no matter how many pollers ask — the
# status strip's readouts (branch, divergence, line counts) must describe the
# same instant, so they are computed together and cached together.
_info_cache: dict[str, tuple[float, dict]] = {}
_INFO_TTL = 2.0  # seconds; matches the UI poll cadence
_GIT_TIMEOUT = 5.0

# #350: a 7-char git hash line, for filtering gateway output that arrives
# stderr-merged (warnings must never poison a hash slot).
_HASH_RE = re.compile(r"[0-9a-f]{7,40}")


def _head_path(root: Path) -> Path | None:
    """The file whose mtime tracks branch switches."""
    git = root / ".git"
    if git.is_file():
        # Worktrees/submodules: .git is a stub file "gitdir: <path>".
        try:
            text = git.read_text(encoding="utf-8", errors="replace").strip()
        except OSError:
            return None
        if text.startswith("gitdir:"):
            target = text.split(":", 1)[1].strip()
            p = Path(target)
            if not p.is_absolute():
                p = root / target
            return p / "HEAD"
        return None
    if git.is_dir():
        return git / "HEAD"
    return None


def is_git_repo(root: Path | str) -> bool:
    """True when the directory looks like a git repo or worktree checkout
    (a .git dir or stub file). Pure stat — no spawn. #302: the staleness
    checks guard with this, because list_local_branches returns [] for a
    non-repo (git fails), which must not read as 'branch deleted'."""
    return _head_path(Path(root)) is not None


async def current_git_branch(root: Path | str) -> str | None:
    """Current branch name, or None when the workspace is not a git repo
    (detached HEADs report the short SHA).

    #333: routed through the git gateway. Local workspaces keep the
    HEAD-mtime fast path; remote workspaces ask the host through the
    channel (TTL-cached below — a remote poll costs a round-trip)."""
    root = Path(root)
    ws = str(root)
    remote = parse_ns(ws) is not None

    head = None if remote else _head_path(root)
    if not remote and head is None:
        return None
    mtime = 0
    if head is not None:
        try:
            mtime = head.stat().st_mtime_ns
        except OSError:
            return None

    key = ws
    cached = _cache.get(key)
    if cached:
        if not remote and cached[0] == mtime and cached[1] is not None:
            # Negative entries are never trusted (#331): a None cached
            # while HEAD was unborn (or by the pre-#331 build) would
            # otherwise outlive the first commit - HEAD's mtime does not
            # change when it is born, so there is no mtime signal to
            # invalidate it.
            return cached[1]
        if remote and time.monotonic() - _branch_times.get(key, 0.0) < _BRANCH_TTL:
            return cached[1]

    branch: str | None = None
    if remote:
        # #333: the gateway merges stderr at the source (the host's shell
        # redirects 2>&1), so the ambiguity-poisoning case is guarded
        # structurally: a branch name is a single line — trust the output
        # only when git left nothing else (warnings) behind.
        res = await gitexec.run_git(ws, "rev-parse", "--abbrev-ref", "HEAD")
        if res is not None:
            rc, out = res
            if rc == 0 and out and "\n" not in out:
                branch = out
    else:
        # _run_git (which passes --no-optional-locks, issue #279). stderr is
        # discarded for this lookup only: a workspace with a local branch named
        # HEAD makes rev-parse emit an ambiguity warning on stderr while still
        # exiting 0, and merged output would poison the branch value.
        rc, out = await _run_git(
            root, "rev-parse", "--abbrev-ref", "HEAD", merge_stderr=False
        )
        if rc == 0:
            branch = out or None
        else:
            # #331: an unborn HEAD (fresh `git init`, no commits yet) fails
            # rev-parse - that is a repo with a branch, not a non-repo. The
            # symref read below reports it, so the workspace is recognized
            # from the moment git init runs, not from the first commit.
            branch = await head_branch(root)

    # Cache keyed on the observed mtime (local) or capture time (remote):
    # when HEAD changes, the mtime mismatch forces a re-read.
    if remote:
        _cache[key] = (0.0, branch)
        _branch_times[key] = time.monotonic()
    else:
        _cache[key] = (mtime, branch)
    return branch


async def head_branch(root: Path | str) -> str | None:
    """The branch name HEAD points at - born or unborn (#331) - or None
    when it cannot be read (no repo, detached HEAD, corrupt .git).

    `rev-parse --abbrev-ref HEAD` needs HEAD to resolve; `branch
    --show-current` just reads the symref git records for exactly the
    unborn state (and returns empty for a detached HEAD: no name)."""
    rc, out = await _run_git(
        Path(root), "branch", "--show-current", merge_stderr=False
    )
    if rc != 0:
        return None
    return out or None


# ------------------------------------------------------------- ui readout

def _run_git(root: Path, *args: str, merge_stderr: bool = True):
    """The local executor. The subprocess implementation lives once, in
    the gateway (gitexec._run_git_local); this wrapper keeps this
    module's signature — including merge_stderr=False, the branch-lookup
    ambiguity guard — for every existing caller and test."""
    return gitexec._run_git_local(str(root), *args, merge_stderr=merge_stderr)


def _invalidate_branch_cache(root: Path | str) -> None:
    """Drop the HEAD-mtime cache entry so the next branch poll re-reads."""
    _cache.pop(str(Path(root)), None)


def _pop_info_entries(root: Path) -> None:
    """Drop every info-cache entry for this workspace (#350). Keys are
    root-prefixed with per-chat qualifiers (chat_root, selected branch),
    so a plain pop would miss them; exact legacy keys are included."""
    prefix = str(root)
    for k in [k for k in _info_cache if k == prefix or k.startswith(prefix + "|")]:
        _info_cache.pop(k, None)


async def _tip(root: Path, ref: str) -> str | None:
    """7-char short hash of `ref`, or None when it does not resolve."""
    rc, out = await _run_git(root, "rev-parse", "--short=7", ref)
    return out.strip() if rc == 0 and out.strip() else None


async def git_workspace_info(
    root: Path | str,
    *,
    chat_root: Path | str | None = None,
    branch: str | None = None,
) -> dict | None:
    """Everything the status strip's git readouts need, in one git burst.

    Returns None when the workspace is not a git repo. Shape:
      branch            current branch name (detached HEAD -> short SHA)
      local_hash        7-char short hash of the branch TIP (#350) - stable
                        across checkouts/detachments of the read tree
      remote_hash       7-char hash of the branch's upstream ref, None =
                        no upstream; resolved from the branch, so a
                        detached read tree no longer hides it (#350)
      worktree_hash     HEAD of the chat's own worktree (chat_root), None
                        when the chat has no tree - omitted, never
                        zero-filled (#350; local chats only)
      worktree_ahead          commits the chat tree has that the primary tree
                        lacks (diverged counts as ahead; unrelated
                        histories -> 0) (#350)
      upstream          upstream ref name, None = none
      ahead, behind     commit counts vs upstream (0/0 when no upstream)
      added, deleted    net diff lines vs HEAD (tracked changes only)
      dirty             worktree has any change (incl. untracked files)
      untracked         count of untracked files (tooltip detail)

    `branch` names the chat's selected branch (the endpoint passes the
    stored pick); the dirty/line counts read `chat_root` when the chat's
    own worktree exists - the #277 substitution, now explicit here so the
    hash trio and the dirtiness describe the same instant. TTL-cached: the
    strip's readouts must describe one instant, and the UI polls every
    ~2s, so the cache lives 2s.
    """
    root = Path(root)
    key = f"{root}|{chat_root or ''}|{branch or ''}"
    remote = parse_ns(str(root)) is not None
    now = time.monotonic()
    cached = _info_cache.get(key)
    if cached and now - cached[0] < _INFO_TTL:
        return cached[1]

    # #333: an unreachable remote host is an explicit state, never a silent
    # None — the strip renders "host offline" instead of vanishing. Local
    # non-repos keep today's None (not-a-repo is data, not unreachability).
    if remote:
        res = await gitexec.run_git(key, "status", "-sb", "--porcelain")
        if res is None:
            info = {"offline": True}
            _info_cache[key] = (now, info)
            return info
        rc, out = res
        if rc != 0:
            # Git ran and refused (not a repo, repository corrupt): report
            # the refusal — a fabricated readout would be worse than
            # absence, and unreachability would be the wrong state.
            info = {"offline": True, "error": out or "git failed"}
            _info_cache[key] = (now, info)
            return info
        info = _info_from_status(out)
        if info["branch"] is None:
            # Detached HEAD: porcelain cannot name it. The endpoint passes
            # the chat's selected branch (#350) - fall back to the old
            # short-SHA read only when no name is known (unborn HEAD fails
            # rc!=0 and stays None, same as local).
            info["branch"] = branch
            if not info["branch"]:
                res2 = await gitexec.run_git(key, "rev-parse", "--short=7", "HEAD")
                if res2 is not None and res2[0] == 0:
                    info["branch"] = res2[1] or None
        # #350: porcelain cannot express hashes - one extra rev-parse
        # burst fills both. Both refs are already host-resolved (branch
        # and upstream names come from the status header). `--short`
        # refuses multiple revisions ("Needed a single revision"), so
        # full hashes come back and are sliced here; the shape filter is
        # belt-and-braces because the channel merges stderr at the source.
        if info["branch"]:
            refs = [info["branch"]]
            if info["upstream"]:
                refs.append(info["upstream"])
            rc_h, out_h = await gitexec.run_git(key, "rev-parse", *refs)
            if rc_h == 0 and out_h:
                hashes = [
                    line.strip()[:7]
                    for line in out_h.splitlines()
                    if _HASH_RE.fullmatch(line.strip())
                ]
                info["local_hash"] = hashes[0] if hashes else None
                info["remote_hash"] = hashes[1] if len(hashes) > 1 else None
        _info_cache[key] = (now, info)
        return info

    # #350: which tree the dirty/line counts read. When the chat has its
    # own worktree, they describe THAT tree - the substitution #277 made
    # at the endpoint, now explicit here so the hash trio and the counts
    # describe the same instant.
    read_root = root
    if chat_root is not None and Path(chat_root).exists():
        read_root = Path(chat_root)

    tree_branch = await current_git_branch(read_root)  # the counts' tree
    if tree_branch is None:
        _info_cache[key] = (now, None)
        return None
    # The hashes resolve from the chat's SELECTED branch (stored pick,
    # else the read tree's branch) - never from the read tree's HEAD, so
    # a detached chat tree (the #277 geometry) cannot blind the upstream
    # readout the way `@{upstream}`-from-HEAD did. `branch` itself keeps
    # the #277 contract: it names the read tree (detached -> short SHA).
    sel = branch or tree_branch
    # A stale stored pick (branch deleted since) must not blank the
    # readout (#350 review): absence is data only for an unborn repo,
    # so an unresolvable pick falls back to the read tree's branch.
    if sel and await _tip(root, sel) is None:
        sel = tree_branch

    # Branch tip first: its resolution doubles as the has-commits check
    # (an unborn branch resolves to nothing, and a worktree of the same
    # repository cannot have commits either).
    local_hash = await _tip(root, sel) if sel else None
    has_commits = local_hash is not None

    upstream: str | None = None
    if has_commits:
        rc, out = await _run_git(
            root, "rev-parse", "--abbrev-ref", "--symbolic-full-name",
            f"{sel}@{{upstream}}",
        )
        if rc == 0:
            upstream = out.strip() or None
    ahead = behind = 0
    if upstream and has_commits:
        # rev-list --left-right --count gives ahead/behind in one call.
        rc, out = await _run_git(
            root, "rev-list", "--left-right", "--count",
            f"{upstream}...{sel}",
        )
        if rc == 0 and out:
            parts = out.split()
            if len(parts) == 2:
                behind, ahead = int(parts[0]), int(parts[1])

    remote_hash = await _tip(root, upstream) if upstream else None

    worktree_hash: str | None = None
    worktree_ahead = 0
    if read_root != root and has_commits:
        worktree_hash = await _tip(read_root, "HEAD")
        if worktree_hash:
            # Commits the chat tree has that the primary tree lacks.
            # A related history (merge base exists) counts with two dots -
            # diverged trees read their own commits, per the #350 color
            # rule "ahead = any commits primary lacks". Unrelated
            # histories have no merge base and read 0: never claim a
            # direction the histories cannot back.
            rc_mb, _ = await _run_git(
                root, "merge-base", "HEAD", worktree_hash
            )
            if rc_mb == 0:
                rc, out = await _run_git(
                    root, "rev-list", "--count", f"HEAD..{worktree_hash}"
                )
                if rc == 0 and out.strip().isdigit():
                    worktree_ahead = int(out.strip())

    added = deleted = 0
    untracked = 0
    if has_commits:
        rc, out = await _run_git(read_root, "diff", "--numstat", "HEAD")
        if rc == 0:
            for line in out.splitlines():
                if not line.strip():
                    continue
                # _run_git merges stderr in, so warning lines (e.g. CRLF
                # notices) land here tab-less; they are not numstat rows.
                parts = line.split("\t", 2)
                if len(parts) != 3:
                    continue
                a, d, _p = parts
                # Binary files report "-" for both counts; ignore them.
                added += int(a) if a.isdigit() else 0
                deleted += int(d) if d.isdigit() else 0
    rc, out = await _run_git(read_root, "ls-files", "--others", "--exclude-standard")
    if rc == 0:
        untracked = sum(1 for line in out.splitlines() if line.strip())

    rc, out = await _run_git(read_root, "status", "--porcelain")
    dirty = rc == 0 and bool(out.strip())
    changed = sum(1 for line in out.splitlines() if line.strip()) if rc == 0 else 0

    info = {
        "branch": tree_branch,
        "upstream": upstream,
        "local_hash": local_hash,
        "remote_hash": remote_hash,
        "worktree_hash": worktree_hash,
        "worktree_ahead": worktree_ahead,
        "ahead": ahead,
        "behind": behind,
        "added": added,
        "deleted": deleted,
        "dirty": dirty,
        "untracked": untracked,
        "changed": changed,
    }
    _info_cache[key] = (now, info)
    return info


def _info_from_status(out: str) -> dict:
    """#333: the remote readout, from one `git status -sb --porcelain`
    burst (cross-dialect safe, one round-trip). Shape matches the local
    info dict; details git cannot express in porcelain stay 0/None rather
    than wrong. Only porcelain lines are read — the channel's shell merges
    stderr into the output, so any warning line must never poison the
    counts (porcelain v1 entries are "XY PATH" / "? PATH" / "## ...")."""
    branch = None
    upstream: str | None = None
    ahead = behind = 0
    lines = out.splitlines()
    body = [
        line for line in lines
        if line.startswith("?? ")
        or (len(line) >= 3 and line[2] == " " and line[:2] != "##")
    ]
    head = next(
        (line for line in lines if line.startswith("##")), None
    )
    if head is not None:
        # The head line's position is not assumed: the channel's shell
        # merges stderr, so warnings can precede it in the stream.
        head = head[2:].strip()
        # Forms: "branch...upstream [ahead N, behind M]", "branch...upstream",
        # "branch" (no upstream), "HEAD (no branch)" — detached, which the
        # local path reports as the short SHA; porcelain cannot, so the
        # branch reads None there (the chip falls back to the stored pin).
        marker = ""
        if "..." in head:
            b, _, rest = head.partition("...")
            branch = b.strip() or None
            rest, _, trail = rest.partition(" [")
            upstream = rest.strip() or None
            if trail:
                marker = trail.rsplit("]", 1)[0]
        else:
            # "No commits yet on <branch>" — unborn HEAD (git cannot use
            # "..." form there); the branch is still nameable.
            if head.startswith("No commits yet on "):
                branch = head[len("No commits yet on "):] or None
            else:
                branch = None if head.startswith("HEAD") else (head or None)
        for part in marker.split(","):
            part = part.strip()
            if part.startswith("ahead"):
                ahead = _int_or_zero(part[5:])
            elif part.startswith("behind"):
                behind = _int_or_zero(part[6:])
    dirty = bool(body)
    return {
        "branch": branch,
        "upstream": upstream,
        "local_hash": None,
        "remote_hash": None,
        # #350: a local-only concept - the host-side chat-tree
        # substitution (#334) will fill it later; shape parity today.
        "worktree_hash": None,
        "worktree_ahead": 0,
        "ahead": ahead,
        "behind": behind,
        "added": 0,
        "deleted": 0,
        "dirty": dirty,
        "untracked": sum(1 for line in body if line.startswith("?? ")),
        "changed": sum(1 for line in body if not line.startswith("?? ")),
    }


def _int_or_zero(text: str) -> int:
    try:
        return int(text.strip())
    except ValueError:
        return 0


_branch_list_cache: dict[str, tuple[float, list[str]]] = {}
_BRANCH_LIST_TTL = 2.0  # seconds; matches the info-readout cache cadence


async def list_local_branches(root: Path | str) -> list[str]:
    """Local branch names for the chip's dropdown (current branch included;
    sorted by git's default ordering). Empty when not a repo; for an
    unborn HEAD (fresh `git init`, #331) the single symref name HEAD
    points at is reported, so the workspace reads as the repo it is.

    #302: TTL-cached — the chip's staleness read rides this list every
    ~2s poll, and a per-poll spawn would break the git-branch endpoint's
    cheap-by-design contract. Callers that must see fresh state after a
    branch write go through invalidate_git_caches, which drops it."""
    root = Path(root)
    key = str(root)
    now = time.monotonic()
    cached = _branch_list_cache.get(key)
    if cached and now - cached[0] < _BRANCH_LIST_TTL:
        return cached[1]
    res = await gitexec.run_git(root, "branch", "--format=%(refname:short)")
    if res is None:
        return []
    rc, out = res
    if rc != 0:
        return []
    branches = [line.strip() for line in out.splitlines() if line.strip()]
    if not branches:
        # #331: an unborn HEAD lists no branches, but the repo does have
        # one - the symref name HEAD points at. Report it so the #314
        # picker and the #302 staleness guard treat the repo as healthy.
        unborn = await head_branch(root)
        if unborn:
            branches = [unborn]
    _branch_list_cache[key] = (now, branches)
    return branches


def invalidate_git_caches(root: Path | str) -> None:
    """Force the next info/branch poll to re-read from git (after a UI-driven
    checkout, commit, push, pull ...)."""
    root = Path(root)
    _invalidate_branch_cache(root)
    _branch_times.pop(str(root), None)  # #333: remote branch-TTL bookkeeping
    _pop_info_entries(root)
    _branch_list_cache.pop(str(root), None)  # #302: staleness reads re-read
