"""#355: the fossil probe - one-command index-provenance verdict for the
primary's staged index (ADR-0016 section 5; the #353 nine-handoff
diagnosis class).

Behavioral tests in temp-repo fixtures (the #354 suite's idioms):

- clean index (== HEAD tree) => ``clean``
- staged-only-in-a-ref content (a landing fossil: committed to a wip/
  branch, then the primary reset but the index re-staged) =>
  ``fossil-candidate`` with blob evidence naming the containing ref
- a staged blob that exists in NO ref => ``live-wip`` with the
  unreachable blob evidence
- staged deletion of a tracked file: fossil when the removal is
  committed somewhere, live-wip otherwise
- the probe is READ-ONLY: the real index is byte-identical after the
  probe, and the verdict is JSON-serializable
- CLI: ``python -m backend.agent.landing <workspace>`` prints the
  verdict as JSON (the one-command agent invocation)
- not-a-repo => ``error`` verdict (data, not absence - never a crash)
"""
import asyncio
import json
import subprocess
import sys

from backend.agent import landing


def _git(cwd, *args, check=True, input=None):
    proc = subprocess.run(
        ["git", *args],
        cwd=cwd,
        capture_output=True,
        text=not isinstance(input, bytes),
        timeout=30,
        check=False,
        input=input,
    )
    if check:
        assert proc.returncode == 0, f"git {args}: {proc.stderr}"
    out = proc.stdout.strip()
    return out.decode("utf-8") if isinstance(out, bytes) else out


def _repo_with_commit(tmp_path, name="repo"):
    repo = tmp_path / name
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "master")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")
    (repo / "hello.txt").write_text("hi\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "first")
    return repo


def _probe(repo):
    return asyncio.run(landing.fossil_probe(str(repo)))


def _raw_index(repo):
    idx = _git(repo, "rev-parse", "--git-path", "index")
    if not idx.replace("\\", "/").startswith(str(repo).replace("\\", "/")):
        idx = str(repo) + "/" + idx
    with open(idx, "rb") as f:
        return f.read()


def test_clean_index(tmp_path):
    repo = _repo_with_commit(tmp_path)
    v = _probe(repo)
    assert v["verdict"] == "clean"
    assert v["index_tree"] == _git(repo, "rev-parse", "HEAD^{tree}")


def test_fossil_candidate_blob_committed_on_a_wip_branch(tmp_path):
    repo = _repo_with_commit(tmp_path)
    # The fossil shape: the content WAS committed to a branch (the #354
    # wip sweep's refs/heads/wip/...), then the primary was reset --hard
    # but the staged index still holds the content.
    _git(repo, "checkout", "-qb", "wip/fossil-1")
    (repo / "hello.txt").write_text("fossil content\n", encoding="utf-8")
    _git(repo, "commit", "-qam", "wip: the content that would be swept")
    _git(repo, "checkout", "-q", "master")
    (repo / "hello.txt").write_text("fossil content\n", encoding="utf-8")
    _git(repo, "add", "hello.txt")

    v = _probe(repo)
    assert v["verdict"] == "fossil-candidate"
    assert any("wip/fossil-1" in ev for ev in v["blob_evidence"])
    assert v["index_tree"] != v["head_tree"]


def test_live_wip_blob_exists_nowhere(tmp_path):
    repo = _repo_with_commit(tmp_path)
    # A blob typed only into the index: no branch, no tag, no ref carries
    # it (the never-lose-it class).
    (repo / "hello.txt").write_text("live wip content\n", encoding="utf-8")
    _git(repo, "add", "hello.txt")

    v = _probe(repo)
    assert v["verdict"] == "live-wip"
    assert any("hello.txt" in ev for ev in v["unreachable_blobs"])


def test_staged_deletion_fossil_when_committed_elsewhere(tmp_path):
    repo = _repo_with_commit(tmp_path)
    _git(repo, "checkout", "-qb", "wip/deleter")
    _git(repo, "rm", "-q", "hello.txt")
    _git(repo, "commit", "-qm", "wip: delete hello")
    _git(repo, "checkout", "-q", "master")
    _git(repo, "rm", "-q", "--cached", "hello.txt")

    v = _probe(repo)
    assert v["verdict"] == "fossil-candidate"
    assert any("wip/deleter" in ev for ev in v["blob_evidence"])


def test_staged_deletion_live_wip_when_committed_nowhere(tmp_path):
    repo = _repo_with_commit(tmp_path)
    _git(repo, "rm", "-q", "--cached", "hello.txt")

    v = _probe(repo)
    assert v["verdict"] == "live-wip"


def _ls_files(repo):
    return _git(repo, "ls-files", "-s")


def test_probe_is_read_only_and_serializable(tmp_path):
    repo = _repo_with_commit(tmp_path)
    (repo / "hello.txt").write_text("live wip content\n", encoding="utf-8")
    _git(repo, "add", "hello.txt")
    before = _ls_files(repo)

    v = _probe(repo)

    # The staged CONTENT is untouched (raw index bytes may differ: git
    # refreshes the stat cache when it reads the index).
    assert _ls_files(repo) == before
    assert _git(repo, "status", "--porcelain") != ""  # staged state intact
    json.loads(json.dumps(v))  # any chat can consume the verdict


def test_cli_one_command_invocation(tmp_path):
    repo = _repo_with_commit(tmp_path)
    (repo / "hello.txt").write_text("live wip content\n", encoding="utf-8")
    _git(repo, "add", "hello.txt")

    proc = subprocess.run(
        [sys.executable, "-m", "backend.agent.landing", str(repo)],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
        cwd=".",
    )
    assert proc.returncode == 0, proc.stderr
    v = json.loads(proc.stdout)
    assert v["verdict"] == "live-wip"


def test_not_a_repo_is_an_error_verdict(tmp_path):
    empty = tmp_path / "empty"
    empty.mkdir()
    v = _probe(empty)
    assert v["verdict"] == "error"
    assert v["evidence"]  # the git failure is carried, not swallowed