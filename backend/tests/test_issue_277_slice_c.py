"""Slice C of #277 (ADR-0010): pin-at-creation + sub-agent note injection.

The draft destination card's branch pick pre-stores selected_branch at
conversation creation (no primary-tree checkout anywhere in the flow),
and every spawned sub-agent prompt carries the parent's selector note -
delegation cannot silently drop branch context (amendment decision 5).
"""
import subprocess

import pytest
from fastapi.testclient import TestClient


def _git(cwd, *args):
    proc = subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, timeout=30
    )
    assert proc.returncode == 0, f"git {args}: {proc.stderr}"
    return proc.stdout.strip()


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


@pytest.fixture()
def client():
    from backend.main import app

    with TestClient(app) as c:
        yield c


def test_create_conversation_prestores_selected_branch(client, tmp_path):
    repo = _repo_with_commit(tmp_path)
    r = client.post(
        "/api/conversations",
        json={"title": "t", "workspace": str(repo), "selected_branch": "master"},
    )
    assert r.status_code == 200, r.text
    cid = r.json()["id"]
    row = client.get(f"/api/conversations/{cid}").json()
    assert row["selected_branch"] == "master"
    # Pin-at-creation means pin: no primary-tree checkout happened.
    _git(repo, "rev-parse", "--verify", "master")


def test_create_without_branch_leaves_selector_unset(client, tmp_path):
    repo = _repo_with_commit(tmp_path)
    r = client.post(
        "/api/conversations",
        json={"title": "t", "workspace": str(repo)},
    )
    cid = r.json()["id"]
    row = client.get(f"/api/conversations/{cid}").json()
    assert not row.get("selected_branch")


@pytest.mark.asyncio
async def test_sub_agent_prompt_carries_branch_note(tmp_path, monkeypatch):
    from backend.agent.subagents import _sub_agent_system_prompt, run_sub_agent
    from backend.agent.subagents import get_agent_def, list_agents

    names = [d["name"] for d in list_agents()]
    if not names:
        pytest.skip("no agent definitions available")
    defn = get_agent_def(names[0])
    assert defn is not None
    note = _sub_agent_system_prompt(
        defn, str(tmp_path),
        branch_note="# Branch selector: bigtest\n\ndetached variant",
    )
    assert "# Branch selector: bigtest" in note

    # run_sub_agent threads the note into the prompt (never raises).
    captured = {}

    async def fake_chat(messages, tools=None, stream=True, model="", effort=""):
        captured["system"] = messages[0]["content"]

        async def _stream():
            yield {"type": "content", "text": "ok"}
            yield {"type": "finish"}
        return _stream()

    from backend.agent import subagents as sub_mod

    monkeypatch.setattr(sub_mod.model_client, "chat", fake_chat)
    result = await run_sub_agent(
        defn, "do things", str(tmp_path), branch_note="# Branch selector: pin"
    )
    assert "# Branch selector: pin" in captured.get("system", "")
    assert result["status"] == "completed"
