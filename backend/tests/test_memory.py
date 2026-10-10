"""Persistent memory (backend/agent/memory.py): per-workspace file
layout, the save/read/delete tool executors, index maintenance, and
prompt injection."""
import os

import pytest

from backend.agent import memory


@pytest.fixture(autouse=True)
def _mem_root(tmp_path, monkeypatch):
    monkeypatch.setenv("YAAH_MEMORY_PATH", str(tmp_path / "mem"))
    yield


WS = str(os.getcwd())  # any stable absolute path; no FS access needed


# ---- project key ------------------------------------------------------------

def test_project_key_is_stable_and_path_derived():
    a = memory.project_key(WS)
    assert a == memory.project_key(WS)
    assert len(a) == 16
    assert a != memory.project_key(str(WS + "-other"))


def test_project_key_case_insensitive_on_windows(monkeypatch):
    monkeypatch.setattr(memory, "_case_insensitive_fs", lambda: True)
    upper = memory.project_key("C:\\Proj")
    lower = memory.project_key("c:\\proj")
    assert upper == lower


def test_project_key_remote_namespaced_is_raw_string():
    local = memory.project_key("C:\\somewhere\\remote:h1:\\proj")
    remote = memory.project_key("remote:h1:\\proj")
    assert remote != local
    assert memory.project_key("remote:h1:\\proj") == memory.project_key(
        "remote:h1:\\proj"
    )


def test_project_key_default_workspace_uses_home():
    # "" and "." are the same Default pseudo-workspace -> home dir
    assert memory.project_key("") == memory.project_key(".")
    assert memory.project_key("") != memory.project_key("C:\\nowhere")


# ---- save / read / delete round-trip ----------------------------------------

def test_save_creates_file_and_index_line():
    r = memory.save_memory(WS, "prefers dark ui", "Prefers dark UI",
                           "User prefers dark themes", "user",
                           "The user prefers dark UI themes.")
    assert "error" not in r
    d = memory.memory_dir(WS)
    assert (d / "prefers-dark-ui.md").exists()
    idx = (d / "MEMORY.md").read_text(encoding="utf-8")
    assert "[Prefers dark UI](prefers-dark-ui.md) — User prefers dark themes" in idx


def test_read_returns_full_content():
    memory.save_memory(WS, "fact", "Fact", "A fact", "project", "body text here")
    r = memory.read_memory(WS, "fact")
    assert "body text here" in r["content"]
    assert r["name"] == "fact"


def test_update_replaces_index_line_instead_of_appending():
    memory.save_memory(WS, "fact", "Fact", "v1", "project", "v1")
    memory.save_memory(WS, "fact", "Fact", "v2", "project", "v2")
    idx = (memory.memory_dir(WS) / "MEMORY.md").read_text("utf-8")
    assert idx.count("](fact.md)") == 1
    assert "v2" in idx and "v1" not in idx


def test_delete_removes_file_and_index_line():
    memory.save_memory(WS, "gone", "Gone", "soon", "project", "x")
    memory.save_memory(WS, "kept", "Kept", "stays", "project", "y")
    assert "error" not in memory.delete_memory(WS, "gone")
    assert not (memory.memory_dir(WS) / "gone.md").exists()
    idx = (memory.memory_dir(WS) / "MEMORY.md").read_text("utf-8")
    assert "gone.md" not in idx and "kept.md" in idx
    assert "error" in memory.delete_memory(WS, "gone")  # double delete


def test_invalid_names_rejected():
    for bad in ("../escape", "a/b", "", "UPPER CASE!", "x" * 200, ".hidden"):
        assert "error" in memory.save_memory(WS, bad, "t", "d", "project", "c")
        assert "error" in memory.read_memory(WS, bad)
        assert "error" in memory.delete_memory(WS, bad)


def test_unknown_read_and_delete_error_cleanly():
    assert "error" in memory.read_memory(WS, "nope")
    assert "error" in memory.delete_memory(WS, "nope")


def test_type_falls_back_to_project_and_empty_content_rejected():
    r = memory.save_memory(WS, "t1", "T", "d", "bogus-type", "c")
    assert r["type"] == "project"
    assert "error" in memory.save_memory(WS, "t2", "T", "d", "user", "   ")


# ---- prompt injection -------------------------------------------------------

def test_index_for_prompt_empty_until_first_memory():
    assert memory.index_for_prompt(WS) == ""
    memory.save_memory(WS, "fact", "Fact", "A fact", "project", "detail")
    block = memory.index_for_prompt(WS)
    assert "Persistent memory" in block
    assert "fact.md" in block
    assert "memory_save" in block  # tool guidance included


def test_index_for_prompt_template_seeded_dir_is_silent():
    memory.ensure_dir(WS)
    assert memory.index_for_prompt(WS) == ""


def test_index_for_prompt_caps_long_index():
    for i in range(400):
        memory.save_memory(WS, f"m-{i}", f"M{i}", "x" * 200, "project", "c")
    block = memory.index_for_prompt(WS)
    assert len(block) < memory.MAX_INDEX_CHARS + 2000
    assert "…[truncated]" in block


def test_index_for_prompt_never_raises_on_corrupt_index(tmp_path):
    d = memory.ensure_dir(WS)
    (d / "MEMORY.md").write_bytes(b"\xff\xfe\x00broken")
    assert isinstance(memory.index_for_prompt(WS), str)


# ---- save-criteria charter (ADR-0009): memory models the owner ---------------

def test_criteria_prompts_owner_model_retention_test():
    """#228: the criteria must present the charter and the retention test."""
    text = memory._WHEN_TO_SAVE
    assert "model of the owner, not a log of actions" in text
    assert "retention test" in text
    assert "handed only the repo and the tracker, work differently" in text
    # the criteria actually ship inside the injected block once a memory exists
    memory.save_memory(WS, "pref-dark", "Dark", "prefers dark", "user", "c")
    assert "retention test" in memory.index_for_prompt(WS)


def test_criteria_no_longer_invite_project_state_saves():
    """#228: the old 'project:' save invitation must be gone entirely."""
    block = memory.index_for_prompt(WS)
    assert "ongoing work, goals, or constraints NOT derivable" not in block
    assert memory._WHEN_TO_SAVE.count("- project:") == 0
    assert "issue tracker owns this" in memory._WHEN_TO_SAVE
    assert "the repo owns this" in memory._WHEN_TO_SAVE


def test_criteria_name_user_feedback_reference_types():
    """#228: user and feedback stay; reference demoted to rare."""
    text = memory._WHEN_TO_SAVE
    assert "- user:" in text
    assert "- feedback:" in text
    assert "- reference (rare):" in text
    assert "Why:" in text and "How to apply:" in text


def test_criteria_hygiene_dedupe_and_delete():
    """#228: dedupe-before-save and delete-when-wrong survive the rewrite."""
    text = memory._WHEN_TO_SAVE
    assert "near-duplicate" in text
    assert "delete memories that turn out to be wrong" in text
    assert "under ~200 chars" in text


# ---- tool dispatch wiring ----------------------------------------------------

@pytest.mark.asyncio
async def test_executors_route_through_execute_tool():
    from backend.agent.tools import execute_tool, tool_risk

    assert tool_risk("memory_read") == "read"
    assert tool_risk("memory_save") == "mutating"
    assert tool_risk("memory_delete") == "mutating"

    r = await execute_tool("memory_save",
                           {"name": "wire", "title": "Wire", "description": "d",
                            "type": "project", "content": "c"}, WS)
    assert "error" not in r
    r = await execute_tool("memory_read", {"name": "wire"}, WS)
    assert "error" not in r
    r = await execute_tool("memory_delete", {"name": "wire"}, WS)
    assert "error" not in r


# ---- #346: the injected-root path --------------------------------------------

@pytest.fixture()
def _mem_on(tmp_path, monkeypatch):
    """Memory enabled (#169 opt-in): the injected-root tests assert file
    effects, so the toggle must be ON - the graceful disabled-info dict
    would satisfy an `error not in result` check while saving nothing."""
    monkeypatch.setenv("YAAH_CONFIG_PATH", str(tmp_path / "config.json"))
    import importlib

    from backend.agent import config

    saved = dict(vars(config))
    importlib.reload(config)
    config.save_config({"memory": {"enabled": True}})
    yield
    monkeypatch.delenv("YAAH_CONFIG_PATH")
    for key, value in saved.items():
        setattr(config, key, value)


@pytest.mark.asyncio
async def test_save_resolves_injected_memory_workspace(_mem_on):
    """A dispatched memory call carries the harness-injected canonical
    root and resolves its store from THAT, not from the call workspace
    (the post-rebind chat tree the run actually executes in). Exercised
    through execute_tool, the funnel the injection lives behind (#346)."""
    from backend.agent.tools import execute_tool

    chat_tree = WS + "-chat"  # never created on disk; only hashed
    r = await execute_tool("memory_save",
                           {"name": "injected", "title": "Injected",
                            "description": "d", "type": "project",
                            "content": "body"},
                           chat_tree, memory_workspace=WS)
    assert "error" not in r
    d = memory.memory_dir(WS)
    assert (d / "injected.md").exists()
    assert not (memory.memory_dir(chat_tree) / "injected.md").exists()
    # Index maintenance follows the same canonical root.
    assert "](injected.md)" in (d / "MEMORY.md").read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_execute_tool_injects_canonical_root_for_memory_tools(_mem_on):
    """The funnel injects the canonical key for every memory tool;
    other tools keep resolving from their own workspace argument."""
    from backend.agent.tools import execute_tool, memory_workspace_for

    chat_tree = WS + "-chat2"
    r = await execute_tool("memory_save",
                           {"name": "funnel", "title": "F", "description": "d",
                            "type": "project", "content": "c"},
                           chat_tree, memory_workspace=WS)
    assert "error" not in r
    assert (memory.memory_dir(WS) / "funnel.md").exists()
    # Remote namespaces ride through untouched (client-local memories).
    assert memory_workspace_for("remote:h1:C:\\proj") == "remote:h1:C:\\proj"
    # Default pseudo-workspace: None -> executor falls back to the call
    # workspace (the never-rebinds shape, same store either way).
    assert memory_workspace_for("") is None
    assert memory_workspace_for(".") is None


def test_schemas_registered_platform_neutral():
    from backend.agent.tools import SCHEMAS

    for n in ("memory_save", "memory_read", "memory_delete"):
        assert n in SCHEMAS


# ---- issue #192: injection cap consistency ----------------------------------

def _write_index(workspace, text):
    d = memory.ensure_dir(workspace)
    (d / "MEMORY.md").write_text(text, encoding="utf-8")


def test_rendered_block_has_heading_exactly_once():
    # A user edit that reintroduces the template heading must not produce
    # a duplicate "# Persistent memory" heading in the rendered block.
    _write_index(WS, "# Persistent memory\n\n- [Fact](fact.md) — a fact\n")
    block = memory.index_for_prompt(WS)
    assert block.count("# Persistent memory") == 1


def test_index_prioritizes_user_and_feedback_over_project():
    # Type-prioritized index (#228 fold-in): user/feedback pinned at the
    # top; over budget the oldest project entries drop off first.
    for i in range(3):
        memory.save_memory(WS, f"proj-{i}", f"P{i}", "p", "project", "c")
    memory.save_memory(WS, "user-fact", "U", "u", "user", "c")
    memory.save_memory(WS, "fb-fact", "F", "f", "feedback", "c")
    block = memory.index_for_prompt(WS)
    assert block.index("user-fact.md") < block.index("proj-0.md")
    assert block.index("fb-fact.md") < block.index("proj-0.md")


def test_index_drop_oldest_project_first_when_over_budget(monkeypatch):
    lines = []
    for i in range(60):
        slug = f"proj-{i:03d}"
        lines.append(f"- [P{i}]({slug}.md) — {'x' * 200}")
        d = memory.ensure_dir(WS)
        (d / f"{slug}.md").write_text(
            f"---\nname: {slug}\ndescription: \"p\"\nmetadata:\n  type: project\n---\n\n# P{i}\n",
            encoding="utf-8",
        )
    for slug, label in (("user-1", "U"), ("fb-1", "F")):
        lines.append(f"- [{label}]({slug}.md) — {'x' * 200}")
        t = "user" if slug.startswith("user") else "feedback"
        (memory.ensure_dir(WS) / f"{slug}.md").write_text(
            f"---\nname: {slug}\ndescription: \"{t}\"\nmetadata:\n  type: {t}\n---\n\n# {label}\n",
            encoding="utf-8",
        )
    _write_index(WS, "\n".join(lines))
    monkeypatch.setattr(memory, "MAX_INDEX_CHARS", 3000)
    block = memory.index_for_prompt(WS)
    assert "…[truncated]" in block
    assert "proj-000.md" not in block      # oldest project dropped first
    assert "proj-059.md" in block          # newest project kept
    assert "user-1.md" in block and "fb-1.md" in block  # pinned types kept


def test_entry_type_ignores_non_type_keys(monkeypatch):
    # CodeRabbit return trip #1 on PR #251: `default_type: user` (or any
    # other frontmatter key ending in "type") must NOT classify the entry
    # as pinned — only an exact `type:` key does.
    import tempfile

    with tempfile.TemporaryDirectory() as td:
        f = memory.ensure_dir(td) / "entry.md"
        f.write_text(
            "---\nname: e\ndescription: \"d\"\n"
            "type: user\n"          # top-level key must NOT classify...
            "metadata:\n"
            "  type: project\n      # ...the metadata type is authoritative\n---\n\n# e\n",
            encoding="utf-8",
        )
        assert memory._entry_type(f.parent, "- [E](entry.md) — d") == "project"


def test_index_partition_by_index_not_content(monkeypatch):
    # CodeRabbit return trip #1 on PR #251: an unpinned line whose text is
    # byte-identical to a pinned line must still render — partitioning is
    # by position, not content membership.
    _write_index(
        WS,
        "- [U](user-1.md) — user line\n"
        "- [U](user-1.md) — user line\n"
        "- [P](p1.md) — project line\n",
    )
    d = memory.ensure_dir(WS)
    (d / "user-1.md").write_text(
        "---\nname: user-1\ndescription: \"u\"\nmetadata:\n  type: user\n---\n",
        encoding="utf-8",
    )
    (d / "p1.md").write_text(
        "---\nname: p1\ndescription: \"p\"\nmetadata:\n  type: project\n---\n",
        encoding="utf-8",
    )
    block = memory.index_for_prompt(WS)
    assert block.count("user line") == 2  # duplicate unpinned copy survives
    assert "project line" in block


def test_index_pinned_alone_over_budget_still_caps(monkeypatch):
    # CodeRabbit return trip #1 on PR #251: when pinned (user/feedback)
    # entries alone exceed MAX_INDEX_CHARS, the cap must hold — oldest
    # pinned entries drop before the body exceeds the budget.
    lines = []
    for i in range(40):
        slug = f"user-{i:03d}"
        lines.append(f"- [U{i}]({slug}.md) — {'x' * 200}")
        (memory.ensure_dir(WS) / f"{slug}.md").write_text(
            f"---\nname: {slug}\ndescription: \"u\"\nmetadata:\n"
            f"  type: user\n---\n",
            encoding="utf-8",
        )
    _write_index(WS, "\n".join(lines))
    monkeypatch.setattr(memory, "MAX_INDEX_CHARS", 3000)
    block = memory.index_for_prompt(WS)
    entry_lines = [ln for ln in block.splitlines() if ".md)" in ln]
    assert sum(len(ln) + 1 for ln in entry_lines) <= 3000  # cap holds, pinned-only
    assert "user-000.md" not in block  # oldest pinned dropped first
    assert "user-039.md" in block      # newest pinned kept


def test_save_memory_caps_body_and_reports_truncation():
    long_body = "x" * (memory.MAX_MEMORY_BODY_CHARS + 5000)
    r = memory.save_memory(WS, "big", "Big", "d", "project", long_body)
    assert "error" not in r
    assert r.get("truncated") is True
    saved = (memory.memory_dir(WS) / "big.md").read_text(encoding="utf-8")
    assert len(saved) <= memory.MAX_MEMORY_BODY_CHARS + 2048
    short = memory.save_memory(WS, "small", "S", "d", "project", "tiny")
    assert "truncated" not in short


def test_read_memory_body_not_eaten_by_frontmatter_slack():
    # A long title eats into the +2048 slack under the old save path; the
    # body must survive intact up to MAX_MEMORY_BODY_CHARS on read.
    long_title = "T" * 1500
    body = "y" * (memory.MAX_MEMORY_BODY_CHARS - 2000)
    memory.save_memory(WS, "slack", long_title, "d", "project", body)
    r = memory.read_memory(WS, "slack")
    assert body in r["content"]


# ---- #367: search memory ---------------------------------------------------

def test_search_memory_finds_trimmed_entries():
    memory.save_memory(WS, "dark-ui", "Prefers dark UI", "theme choice",
                       "user", "User prefers dark interface.")
    memory.save_memory(WS, "unrelated", "Other", "d", "project", "Nothing here.")
    r = memory.search_memory(WS, "dark")
    assert r["count"] == 1
    m = r["matches"][0]
    assert m["name"] == "dark-ui"
    assert "dark" in m["snippet"].lower()


def test_search_memory_empty_query_errors():
    assert "error" in memory.search_memory(WS, "  ")


def test_search_memory_no_match_returns_zero():
    memory.save_memory(WS, "a", "A", "d", "project", "hello")
    assert memory.search_memory(WS, "zzz")["count"] == 0


def test_truncated_index_marker_points_at_memory_search(monkeypatch):
    memory.save_memory(WS, "a", "A", "d", "project", "x")
    memory.save_memory(WS, "b", "B", "d", "project", "y")
    monkeypatch.setattr(memory, "MAX_INDEX_CHARS", 10)
    block = memory.index_for_prompt(WS)
    assert "memory_search" in block
