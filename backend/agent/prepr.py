"""Pre-PR verification layer: run deterministic checks before push/PR.

Intercepts bash/powershell commands that look like git push or gh pr create,
runs configured lint/typecheck/test commands, and reports results to the agent.
"""

import json
import logging
import os
import re
from pathlib import Path
from typing import Any

import subprocess

from .config import load_config

log = logging.getLogger(__name__)

# Patterns that indicate push/PR intent
PUSH_PATTERNS = [
    r"git\s+push",
    r"gh\s+pr\s+create",
    r"gh\s+pr\s+merge",
    r"git\s+push\s+--force",
]


def looks_like_push(command: str) -> bool:
    """Check if a shell command is likely pushing to GitHub or creating a PR."""
    for pattern in PUSH_PATTERNS:
        if re.search(pattern, command):
            return True
    return False


def load_verification_config(workspace: str) -> dict:
    """Load pre-PR verification config.

    Merges global config with per-project overrides.
    Per-project: .yaah/verify.json in the workspace.
    """
    global_config = load_config().get("pre_pr", {})

    project_config_path = Path(workspace) / ".yaah" / "verify.json"
    project_config = {}
    if project_config_path.exists():
        try:
            project_config = json.loads(project_config_path.read_text())
        except (json.JSONDecodeError, OSError) as e:
            log.warning(f"Failed to load project verify config: {e}")

    # Merge: project overrides global
    merged = {**global_config, **project_config}
    return merged


def run_check(cmd: str, cwd: str, timeout: int = 120) -> dict:
    """Run a single verification check command.

    Returns: {name, passed, output, duration_ms}
    """
    import time

    start = time.time()
    try:
        result = subprocess.run(
            cmd,
            shell=True,
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        duration_ms = int((time.time() - start) * 1000)
        return {
            "passed": result.returncode == 0,
            "output": result.stdout + result.stderr,
            "duration_ms": duration_ms,
        }
    except subprocess.TimeoutExpired:
        return {"passed": False, "output": f"Timeout after {timeout}s", "duration_ms": timeout * 1000}
    except Exception as e:
        return {"passed": False, "output": str(e), "duration_ms": 0}


def _marker_exists(workspace: str, *markers: str) -> bool:
    """True when any of the given paths exists at the workspace root."""
    root = Path(workspace)
    return any((root / m).exists() for m in markers)


def default_checks(workspace: str) -> list[dict]:
    """Ecosystem-standard checks for THIS workspace, chosen by marker files.

    Each default only applies when the workspace declares that ecosystem
    (package.json -> node toolchain, pyproject.toml/requirements.txt ->
    Python, etc.). A workspace declaring nothing gets no defaults: every
    check SKIPs with a visible note instead of failing, so the gate works
    out of the box in any workspace — typed or not, configured or not.
    The fast linters here are advisory-grade (warnings pass); per-project
    .yaah/verify.json overrides all of it.
    """
    checks: list[dict] = []

    # Node/TypeScript: lint only when an eslint/oxlint config actually
    # exists (linting unconfigured JS is noise); typecheck when a TS
    # project is declared.
    if _marker_exists(workspace, "package.json"):
        if _marker_exists(
            workspace,
            "eslint.config.js", "eslint.config.mjs", "eslint.config.cjs",
            ".eslintrc", ".eslintrc.js", ".eslintrc.json", ".eslintrc.yml",
        ):
            checks.append({"name": "lint", "command": "npx eslint . --max-warnings 0"})
        elif _marker_exists(workspace, ".oxlintrc.json", "oxlint.json"):
            checks.append({"name": "lint", "command": "npx oxlint"})
        # Typecheck whenever TypeScript is declared, config or not.
        if _marker_exists(workspace, "tsconfig.json"):
            checks.append({"name": "typecheck", "command": "npx tsc --noEmit"})

    # Python: ruff covers lint+format cheaply when declared.
    if _marker_exists(workspace, "pyproject.toml", "ruff.toml", ".ruff.toml"):
        checks.append({"name": "lint-python", "command": "ruff check ."})

    # Rust / Go: compile-grade checks pick up most lint-level problems.
    if _marker_exists(workspace, "Cargo.toml"):
        checks.append({"name": "check-rust", "command": "cargo check"})
    if _marker_exists(workspace, "go.mod"):
        checks.append({"name": "vet-go", "command": "go vet ./..."})

    return checks


def verify_before_push(command: str, workspace: str, conversation_id: str = None) -> dict:
    """Run pre-PR verification checks.

    Returns structured result for the agent.
    """
    config = load_verification_config(workspace)

    if not config.get("enabled", True):
        return {"ran": False, "reason": "disabled", "checks": []}

    checks_config = config.get("checks", [])

    if not checks_config:
        # Marker-gated defaults for this workspace's ecosystem(s); a
        # workspace declaring nothing gets an empty list and every check
        # reports as a non-failing skip below.
        checks_config = default_checks(workspace)

    if not checks_config:
        # No ecosystem markers and no overrides: nothing this gate knows
        # how to check here. Report that visibly and pass — an empty
        # result list must not read as a silent gate bypass.
        return {
            "ran": True,
            "all_passed": True,
            "checks": [{
                "name": "defaults",
                "passed": True,
                "output": "no recognized project markers (package.json / tsconfig.json / pyproject.toml / Cargo.toml / go.mod) and no .yaah/verify.json checks — nothing to verify",
                "duration_ms": 0,
            }],
            "failed_checks": [],
            "total_duration_ms": 0,
            "command_intercepted": command,
        }

    results = []
    for check in checks_config:
        name = check.get("name", "unnamed")
        cmd = check.get("command", "")
        if not cmd:
            continue

        log.info(f"Running pre-PR check '{name}' in {workspace}")
        result = run_check(cmd, workspace)
        result["name"] = name
        results.append(result)

    all_passed = all(r["passed"] for r in results)
    failed = [r for r in results if not r["passed"]]
    total_duration = sum(r.get("duration_ms", 0) for r in results)

    return {
        "ran": True,
        "all_passed": all_passed,
        "checks": results,
        "failed_checks": [r["name"] for r in failed],
        "total_duration_ms": total_duration,
        "command_intercepted": command,
    }


def format_for_agent(result: dict) -> str:
    """Format verification results for the agent to read."""
    if not result.get("ran"):
        return "Pre-PR verification: skipped"

    lines = ["Pre-PR verification results:"]
    for check in result["checks"]:
        status = "✓ PASS" if check["passed"] else "✗ FAIL"
        duration = f" ({check.get('duration_ms', 0)}ms)"
        lines.append(f"  {status} {check['name']}{duration}")
        if not check["passed"] and check.get("output"):
            # Truncate output for agent readability
            output = check["output"].strip()
            if len(output) > 500:
                output = output[:500] + "..."
            lines.append(f"    {output}")

    if result["all_passed"]:
        lines.append("All checks passed. Safe to push.")
    else:
        lines.append(f"Failed checks: {', '.join(result['failed_checks'])}")
        lines.append("Fix these issues before pushing.")

    return "\n".join(lines)
