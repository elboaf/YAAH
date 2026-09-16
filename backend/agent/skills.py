"""Skill loading: SKILL.md files under ~/.yaah/skills/.

A skill is a directory containing a SKILL.md with YAML frontmatter
(``name``, ``description``, optional ``disable-model-invocation``) and a
markdown body of instructions. Skills are global — available to every
workspace — and are scanned once at startup, then only on explicit refresh.

Two invocation paths:
  - the user types /s <name> (or attaches a chip): the body is injected
    into the system prompt for that turn only;
  - the model calls the load_skill tool: same injection, mid-turn.

Skills flagged ``disable-model-invocation: true`` are hidden from the
model's index (so it can never auto-trigger them) but stay fully
invocable by the user.
"""
import os
import re
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path

# YAAH_SKILLS_PATH lets the test suite redirect this (default is the
# user's real ~/.yaah/skills).
SKILLS_DIR = Path(
    os.environ.get("YAAH_SKILLS_PATH") or Path.home() / ".yaah" / "skills"
)

# Frontmatter is bounded by --- markers at the very start of the file.
_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*(?:\n|$)", re.DOTALL)

MAX_SKILL_BODY_CHARS = 60_000
MAX_SKILLS = 200


@dataclass
class Skill:
    name: str
    description: str
    body: str
    path: str
    disable_model_invocation: bool = False


# name -> Skill; rebuilt by scan_skills(), read by everything else.
_skills: dict[str, Skill] = {}
_scanned = False


SAMPLE_SKILL_MD = """---
name: example
description: Sample skill showing the format — edit or delete me.
disable-model-invocation: true
---

This file demonstrates the skill format. A skill is a folder under
{dir} containing a SKILL.md like this one.

- The YAML frontmatter gives the skill a `name` and a `description`
  (the description is what the model sees when deciding to load it).
- `disable-model-invocation: true` keeps the model from auto-loading
  the skill; remove it to let the model load it on its own.
- Everything below the frontmatter is the instruction body, injected
  into the system prompt when the skill is invoked.

Users invoke skills by typing /<name> in the chat; the model loads
them itself with the load_skill tool when the task matches.
"""


def bundled_source_dir() -> Path | None:
    """Directory holding the skills shipped with the app
    (backend/bundled_skills in the repo; <exe>/_up_/backend/bundled_skills
    in the installed layout — Tauri turns the `../` resource glob prefix
    into a literal `_up_` dir next to the exe, mirroring whisper)."""
    candidates: list[Path] = []
    if getattr(sys, "frozen", False):
        exe = Path(sys.executable).parent
        candidates += [exe / "_up_" / "backend" / "bundled_skills",
                       exe / "bundled_skills",
                       Path.cwd() / "backend" / "bundled_skills"]
    candidates.append(Path(__file__).parent.parent / "bundled_skills")  # repo
    for c in candidates:
        if c.is_dir():
            return c
    return None


def ensure_dir() -> bool:
    """Create SKILLS_DIR when it doesn't exist and seed the example skill
    plus every bundled skill shipped under backend/bundled_skills, so a
    fresh install has somewhere to put skills and works out of the box.
    Seeding never overwrites an existing SKILL.md, so user edits survive
    upgrades. Returns True if the directory was just created."""
    just_created = False
    if not SKILLS_DIR.is_dir():
        try:
            SKILLS_DIR.mkdir(parents=True, exist_ok=True)
        except OSError:
            return False
        just_created = True

    seeds: dict[str, Path] = {}
    src = bundled_source_dir()
    if src is not None:
        try:
            seeds.update(
                {d.name: d for d in sorted(src.iterdir()) if d.is_dir()}
            )
        except OSError:
            pass
    if just_created:
        try:
            sample = SKILLS_DIR / "example"
            sample.mkdir(exist_ok=True)
            (sample / "SKILL.md").write_text(
                SAMPLE_SKILL_MD.format(dir=SKILLS_DIR), encoding="utf-8"
            )
        except OSError:
            pass  # dir exists, that's the part that matters

    for name, folder in seeds.items():
        try:
            target = SKILLS_DIR / name
            if (target / "SKILL.md").exists():
                continue
            target.mkdir(exist_ok=True)
            shutil.copytree(folder, target, dirs_exist_ok=True)
        except OSError:
            pass  # best-effort seeding; never block startup
    return just_created


def parse_skill_md(path: Path) -> Skill | None:
    """Parse one SKILL.md. Returns None when the file is not a valid skill
    (missing/blank name) so a broken file never breaks the whole scan."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None

    m = _FRONTMATTER_RE.match(text)
    meta: dict = {}
    body = text
    if m:
        try:
            loaded = _yaml_load(m.group(1))
        except Exception:  # noqa: BLE001 — bad YAML: skill is unusable
            return None
        if isinstance(loaded, dict):
            meta = loaded
        body = text[m.end():]

    name = str(meta.get("name") or "").strip()
    if not name:
        return None
    description = str(meta.get("description") or "").strip()
    body = body.strip()[:MAX_SKILL_BODY_CHARS]
    return Skill(
        name=name,
        description=description,
        body=body,
        path=str(path),
        disable_model_invocation=bool(meta.get("disable-model-invocation")),
    )


def _yaml_load(raw: str) -> object:
    """yaml.safe_load, with a flat 'key: value' fallback so a missing yaml
    package degrades instead of making every skill unparsable."""
    try:
        import yaml

        return yaml.safe_load(raw)
    except ImportError:
        out: dict = {}
        for line in raw.splitlines():
            if ":" in line and not line.strip().startswith("#"):
                k, _, v = line.partition(":")
                out[k.strip()] = v.strip().strip("'\"")
        return out


def scan_skills() -> dict[str, Skill]:
    """Rescan SKILLS_DIR and replace the cache. Idempotent; never raises."""
    global _skills, _scanned
    found: dict[str, Skill] = {}
    if SKILLS_DIR.is_dir():
        try:
            entries = sorted(SKILLS_DIR.iterdir())
        except OSError:
            entries = []
        for child in entries[:MAX_SKILLS * 4]:
            if len(found) >= MAX_SKILLS:
                break
            if not child.is_dir():
                continue
            skill_md = child / "SKILL.md"
            if not skill_md.is_file():
                continue
            skill = parse_skill_md(skill_md)
            if skill is not None and skill.name not in found:
                found[skill.name] = skill
    _skills = found
    _scanned = True
    return found


def ensure_scanned() -> dict[str, Skill]:
    """Scan on first use (startup calls this explicitly too)."""
    if not _scanned:
        scan_skills()
    return _skills


def list_skills() -> list[dict]:
    """All skills for the UI: name, description, manual-only flag, path."""
    ensure_scanned()
    return [
        {
            "name": s.name,
            "description": s.description,
            "disable_model_invocation": s.disable_model_invocation,
            "path": s.path,
        }
        for s in sorted(_skills.values(), key=lambda s: s.name.lower())
    ]


def get_skill(name: str) -> Skill | None:
    ensure_scanned()
    return _skills.get(name)


def model_invocable() -> list[Skill]:
    """Skills the model may auto-trigger (no disable-model-invocation)."""
    ensure_scanned()
    return [s for s in _skills.values() if not s.disable_model_invocation]


def index_for_prompt() -> str:
    """The <available_skills> block for the system prompt: name + one-line
    description of every model-invocable skill, plus load_skill usage.
    Empty string when there are none (no prompt noise)."""
    skills = model_invocable()
    if not skills:
        return ""
    lines = [
        "Skills available (load with the load_skill tool when relevant):",
    ]
    for s in sorted(skills, key=lambda s: s.name.lower()):
        desc = " ".join(s.description.split())
        lines.append(f"- {s.name}: {desc}")
    lines.append(
        "Call load_skill with a skill's name to load its full instructions "
        "into this conversation before acting on a matching task. Do not "
        "guess at a skill's contents."
    )
    return "\n".join(lines)


def bodies_for_prompt(names: list[str]) -> str:
    """Formatted instruction bodies for explicitly invoked skills (/s or
    chips). Unknown names are reported so the user sees the typo."""
    parts: list[str] = []
    for name in names:
        s = get_skill(name)
        if s is None:
            parts.append(f"# Skill not found: {name}")
            continue
        parts.append(
            f"# Skill: {s.name}\n\n"
            f"The skill's folder (any supporting files it references live "
            f"here) is: {Path(s.path).parent}\n\n{s.body}"
        )
    return "\n\n---\n\n".join(parts)


def load_skill_into_messages(args: dict, loaded_skills: list[str], messages: list) -> dict:
    """Handle a load_skill tool call: append the skill's body to the system
    prompt message so the rest of the turn follows it. Returns the tool
    result dict. Never raises.

    Shared by the parent loop and the sub-agent runner (a sub-agent loads
    skills into its OWN context the same way the parent does)."""
    name = str(args.get("name") or "").strip()
    skill = get_skill(name)
    if skill is None:
        available = ", ".join(s.name for s in model_invocable()) or "none available"
        return {
            "error": f"Unknown skill: {name}",
            "available": available,
        }
    if skill.name in loaded_skills:
        return {
            "loaded": skill.name,
            "note": "already loaded this turn",
        }
    loaded_skills.append(skill.name)
    # messages[0] is the system prompt; extend it in place so every later
    # model call in this turn sees the skill's instructions. The folder
    # path lets the model read the skill's own supporting files.
    if messages and messages[0].get("role") == "system":
        messages[0]["content"] = (
            f"{messages[0]['content']}\n\n---\n\n"
            f"# Loaded skill: {skill.name}\n\n"
            f"The skill's folder (any supporting files it references live "
            f"here) is: {Path(skill.path).parent}\n\n{skill.body}"
        )
    return {
        "loaded": skill.name,
        "description": skill.description,
        "folder": str(Path(skill.path).parent),
    }


def refresh() -> list[dict]:
    """Force a rescan (Settings / UI refresh button)."""
    scan_skills()
    return list_skills()
