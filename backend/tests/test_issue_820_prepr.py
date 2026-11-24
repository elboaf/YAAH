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
    # No project config, uses global defaults
    config = prepr.load_verification_config(str(tmp_path))
    assert config.get("enabled", True)
    assert len(config.get("checks", [])) > 0


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
    assert "Failed checks" in formatted
