import pytest
import json
import os
from pathlib import Path
from backend.agent import prepr


def test_looks_like_push_detects_git_push():
    assert prepr.looks_like_push("git push origin main")
    assert prepr.looks_like_push("git push")


def test_looks_like_push_detects_gh_pr():
    assert prepr.looks_like_push("gh pr create --title test")
    assert prepr.looks_like_push("gh pr merge 123")


def test_looks_like_push_rejects_normal_commands():
    assert not prepr.looks_like_push("ls -la")
    assert not prepr.looks_like_push("git status")
    assert not prepr.looks_like_push("git log")


def test_load_verification_config_defaults(tmp_path):
    # No project config: enabled by default, checks list empty — check
    # selection is marker-gated per workspace (prepr.default_checks),
    # not a hardcoded list that fails in every other ecosystem.
    config = prepr.load_verification_config(str(tmp_path))
    assert config.get("enabled", True)
    assert config.get("checks", []) == []


def test_load_verification_config_project_override(tmp_path):
    # Create project config that overrides
    yaah_dir = tmp_path / ".yaah"
    yaah_dir.mkdir()
    verify_config = {
        "enabled": False,
        "checks": []
    }
    (yaah_dir / "verify.json").write_text(json.dumps(verify_config))

    config = prepr.load_verification_config(str(tmp_path))
    assert config["enabled"] is False
    assert config["checks"] == []


def test_verify_before_push_disabled(tmp_path):
    # Disable via project config
    yaah_dir = tmp_path / ".yaah"
    yaah_dir.mkdir()
    (yaah_dir / "verify.json").write_text(json.dumps({"enabled": False}))

    result = prepr.verify_before_push("git push", str(tmp_path))
    assert result["ran"] is False
    assert result["reason"] == "disabled"


def test_format_for_agent_disabled():
    result = {"ran": False, "reason": "disabled", "checks": []}
    formatted = prepr.format_for_agent(result)
    assert "skipped" in formatted


def test_format_for_agent_all_passed():
    result = {
        "ran": True,
        "all_passed": True,
        "checks": [
            {"name": "lint", "passed": True, "duration_ms": 100},
            {"name": "typecheck", "passed": True, "duration_ms": 200},
        ],
        "failed_checks": [],
    }
    formatted = prepr.format_for_agent(result)
    assert "All checks passed" in formatted
    assert "Safe to push" in formatted


def test_format_for_agent_failed():
    result = {
        "ran": True,
        "all_passed": False,
        "checks": [
            {"name": "lint", "passed": True, "duration_ms": 100},
            {"name": "typecheck", "passed": False, "output": "Type error on line 5", "duration_ms": 200},
        ],
        "failed_checks": ["typecheck"],
    }
    formatted = prepr.format_for_agent(result)
    assert "typecheck" in formatted


def test_default_checks_node_workspace_with_tsconfig(tmp_path):
    # package.json + tsconfig.json -> lint (if eslint config) + typecheck
    (tmp_path / "package.json").write_text("{}")
    (tmp_path / "tsconfig.json").write_text("{}")
    (tmp_path / "eslint.config.js").write_text("")
    checks = prepr.default_checks(str(tmp_path))
    names = [c["name"] for c in checks]
    assert names == ["lint", "typecheck"]
    lint = checks[0]
    assert "eslint" in lint["command"]


def test_default_checks_node_workspace_no_lint_config(tmp_path):
    # package.json + tsconfig but no eslint/oxlint config: no lint check,
    # typecheck still applies (tsc needs no lint config).
    (tmp_path / "package.json").write_text("{}")
    (tmp_path / "tsconfig.json").write_text("{}")
    checks = prepr.default_checks(str(tmp_path))
    assert [c["name"] for c in checks] == ["typecheck"]


def test_default_checks_empty_workspace_is_empty(tmp_path):
    # A workspace declaring nothing gets no checks — the gate must not
    # assume src/ + eslint exist.
    assert prepr.default_checks(str(tmp_path)) == []


def test_verify_before_push_empty_workspace_passes_visibly(tmp_path):
    # Out-of-the-box behavior in a brand-new workspace: gate runs, reports
    # why there is nothing to check, and does NOT fail the push command.
    result = prepr.verify_before_push("git push", str(tmp_path))
    assert result["ran"] is True
    assert result["all_passed"] is True
    assert "no recognized project markers" in result["checks"][0]["output"]


def test_default_checks_python_workspace(tmp_path):
    (tmp_path / "pyproject.toml").write_text("")
    checks = prepr.default_checks(str(tmp_path))
    assert [c["name"] for c in checks] == ["lint-python"]


def test_default_checks_rust_and_go(tmp_path):
    (tmp_path / "Cargo.toml").write_text("")
    assert [c["name"] for c in prepr.default_checks(str(tmp_path))] == ["check-rust"]
    (tmp_path / "go.mod").write_text("")
    assert [c["name"] for c in prepr.default_checks(str(tmp_path))] == [
        "check-rust", "vet-go",
    ]


def test_this_repo_gets_lint_and_typecheck():
    # The YAAH repo itself: node markers exist, so the gate must produce
    # real checks (not a silent pass) — this is the regression guard for
    # the bug where every interception ran a lint command that could
    # never pass (eslint was never installed here).
    repo = str(Path(__file__).resolve().parents[2])
    checks = prepr.default_checks(repo)
    names = [c["name"] for c in checks]
    assert "typecheck" in names
