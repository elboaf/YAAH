"""Interceptor manager -- host-grade browser control for YAAH.

This is the single deep seam the Interceptor-in-YAAH project (spec #630,
ticket t1-manager) hangs off. Ticket 1 of 8: **status and the pinned
manifest only** -- there is no install logic here (t2-install is the
next ticket).

It can answer, at any moment:

  - Is Interceptor installed, and at what version?
  - Are YAAH's three skill links (router, browser, research) present?
  - Is the browser extension reachable?

Detection method (documented here on purpose -- never a silent registry
guess). Interceptor is a signed out-of-process CLI that runs a local
daemon; a YAAH process never installs or starts it. Everything here is
stdlib, dependency-light, and fails closed: a missing binary, a failing
command, or an unreadable manifest reads as a status string ("not
installed" / "extension missing" / "not linked"), never an exception
escaping.

Skill links are three *directory names* under ~/.yaah/skills. Ticket 5
junctions them to Interceptor's installed packs (router, browser,
research -- the names YAAH advertises; the packs themselves live under
Interceptor's skills root). If Interceptor is uninstalled externally the
junctions dangle and the links read as "not linked" cleanly. The scan
follows at most one level into each link (a symlink/junction) so a
broken link is reported, not followed into an error -- same care the
skills loader takes.

Extension state comes from the CLI's own JSON diagnosis command
(`diagnose extension --json`, a post-failure snapshot the CLI ships:
``{"problems": [...], "blocked": [...]}`` -- both empty means the
extension loads in every profile). A missing CLI or a failing command
reads as "extension missing", never an exception.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path

from backend.agent import gitproc

# YAAH_INTERCEPTOR_CLI lets the test suite point at a fake CLI (a
# throwaway script) the same way YAAH_SKILLS_PATH redirects the skills
# dir and YAAH_DB_PATH redirects the database. Default resolves to the
# documented install dir, then PATH.
INTERCEPTOR_CLI_ENV = "YAAH_INTERCEPTOR_CLI"

# The three skill-link names YAAH creates post-install and advertises.
# The installed packs are named differently (interceptor, interceptor-
# browser, interceptor-research); the link *name* is what the skills
# loader keys off, so the mapping name -> directory lives here.
LINK_SLOTS: tuple[str, ...] = ("router", "browser", "research")

# The packs the junctions point at, under the installed Interceptor skills
# root. Kept in the same order as LINK_SLOTS so status can pair them.
_INTERCEPTOR_PACK_NAMES: tuple[str, ...] = (
    "interceptor",
    "interceptor-browser",
    "interceptor-research",
)

# `~/.yaah/skills` -- the same SKILLS_DIR the skills loader uses. The
# manifest's SKILLS_PATH override is honored so tests never touch the
# real skills dir.
SKILLS_DIR = Path(
    os.environ.get("YAAH_SKILLS_PATH") or Path.home() / ".yaah" / "skills"
)


def _skills_dir() -> Path:
    """Runtime skills dir: honors an env override set after import
    (defensive) and falls back to the module-load SKILLS_DIR."""
    override = os.environ.get("YAAH_SKILLS_PATH")
    return Path(override) if override else SKILLS_DIR

# Candidate install locations for the Interceptor binary. The browser-only
# Inno Setup installer (per-user, documented) drops interceptor.exe here;
# a user's own binary on PATH is the fallback. Documented, not guessed.
# (No other candidates: %LOCALAPPDATA%\Cua\cua-driver belongs to a different
# product -- Hermes Agent's Cua driver -- and must never read as Interceptor.)
_INSTALL_DIR_CANDIDATES: tuple[str, ...] = (
    os.path.expandvars(r"%LOCALAPPDATA%\Programs\Interceptor"),
)

# The pinned installer manifest this manager reads as its source of truth
# for what it would install (asset URL + SHA-256 + upstream version +
# license note). Committed beside the module in the repo; shipped as a
# Tauri resource so the installed layout finds it at <exe>/_up_/backend/
# agent/ (tauri.conf.json turns the `../` glob prefix into a literal
# `_up_` dir next to the exe). Resolved through the packaged-layout scan
# at read time (see read_pinned_manifest); this beside-module anchor is
# the repo-checkout default. Read fresh on every status call so a
# manifest bump lands without a code change.
MANIFEST_PATH = Path(__file__).resolve().parent / "interceptor_manifest.json"

# The CLI's JSON extension-diagnosis command (post-failure snapshot). The
# CLI ships both a text and a --json form; this reads the machine-readable
# one. Both empty means the extension loads in every browser profile.
EXTENSION_DIAGNOSIS_ARGS = ["diagnose", "extension", "--json"]

# `interceptor 2.1.6 (...)` -- capture the dotted version, ignore the git
# sha / date that follow on the same line. The CLI prints the bare version
# right after the word "interceptor" (no leading "v").
_VERSION_RE = re.compile(r"\b(\d+\.\d+\.\d+|\d+\.\d+|\d+)\b")

# `--version` stdout is "interceptor 2.1.6 (e14ad6a-dirty, 2026-10-31)".
def _parse_version(stdout: str) -> str | None:
    m = _VERSION_RE.search(stdout or "")
    return m.group(1) if m else None


def _find_interceptor_cli() -> str | None:
    """Locate a directly-spawnable interceptor executable.

    Order (documented, never a silent registry guess):
      1. YAAH_INTERCEPTOR_CLI override (test seam). Set it to a real path
         to use a specific CLI; set it to a path that does not exist to
         force "not found" -- an explicit override is authoritative and
         takes precedence over PATH so a test can hide a real CLI.
      2. a direct file at the documented install-dir candidates;
      3. PATH via shutil.which.

    Returns the path, or None when nothing is present.
    """
    override = os.environ.get(INTERCEPTOR_CLI_ENV)
    if override is not None:
        override_path = Path(override)
        return str(override_path) if override_path.is_file() else None
    for candidate_dir in _INSTALL_DIR_CANDIDATES:
        exe = Path(candidate_dir) / "interceptor.exe"
        if exe.is_file():
            return str(exe)
    found = shutil.which("interceptor")
    if found:
        return found
    # Windows: a .cmd shim cannot be CreateProcess'd directly, so probe the
    # common portable-git location the toolkit uses, mirroring gitproc.
    for candidate in (
        Path.home() / "Desktop" / "toolkit" / "bin" / "interceptor.cmd",
    ):
        if candidate.is_file():
            return str(candidate)
    return None


def _run_cli(argv: list[str], timeout: float = 15.0) -> subprocess.CompletedProcess:
    """Run the Interceptor CLI, returning the CompletedProcess.

    Fails closed: a missing binary or a spawn/read error yields a
    CompletedProcess with returncode != 0 and empty stdout, so callers
    read "not installed" / "extension missing" rather than raising.
    """
    cli = _find_interceptor_cli()
    if cli is None:
        return subprocess.CompletedProcess(argv, 127, stdout="", stderr="cli not found")
    try:
        return subprocess.run(
            [cli, *argv],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return subprocess.CompletedProcess(argv, 126, stdout="", stderr="spawn failed")


def _link_slot_paths() -> dict[str, Path]:
    """name -> path for each link slot, under the configured SKILLS_DIR."""
    base = _skills_dir()
    return {name: base / name for name in LINK_SLOTS}


def _link_is_real_dir(path: Path) -> bool:
    """True when the link resolves to a real directory (not a dangling
    junction/symlink, not a missing entry). At most one level of indirection
    is followed, so a broken link is reported, never chased into an error.
    """
    try:
        return path.is_dir()  # follows one level: real dir or live junction
    except OSError:
        return False


def installed_state() -> str:
    """'installed' when the CLI is found, else 'not installed'."""
    return "installed" if _find_interceptor_cli() is not None else "not installed"


def _install_done() -> bool:
    """True when the install flow's done-marker is present.

    Reads the marker contract t2-install owns
    (interceptor_install._marker_done) through a late import so this module
    never imports the install flow at load time. Any error -- including a
    platform-only import failing on a CI runner -- reads as "not done":
    this helper gates a PATH convenience, never a status fact, so it fails
    closed.
    """
    try:
        from backend.agent import interceptor_install

        return bool(interceptor_install._marker_done())
    except Exception:  # noqa: BLE001 -- fail closed, see docstring
        return False


def path_prepend_dir() -> str | None:
    """The directory tool-process spawns should prepend to PATH, or None.

    This is ticket t4-path (#634)'s key: right after YAAH installs
    Interceptor, agent tool shells can already run `interceptor` without
    restarting YAAH -- every process the backend launches carries the
    install dir at the front of its PATH, keyed off the install state.

    Gated on the done-marker (a full, successful install -- a partial one
    must not advertise a command it did not lay down) *and* the documented
    install dir actually holding a CLI (the marker survives an external
    uninstall; a PATH entry pointing at nothing must not be advertised).

    The dir is returned even when it does not expand (a non-Windows dev
    machine, where %LOCALAPPDATA% stays literal): it is additive to PATH
    and resolves to nothing there, which is harmless.

    None when Interceptor is absent -- the caller's environment must stay
    byte-identical to the pre-feature one (zero effect when absent).
    """
    if not _install_done():
        return None
    for candidate_dir in _INSTALL_DIR_CANDIDATES:
        if (Path(candidate_dir) / "interceptor.exe").is_file():
            return candidate_dir
    return None


def installed_version() -> str | None:
    """The CLI's dotted version, or None when not installed.

    Runs `interceptor --version`; a failing command or an unparseable
    output returns None (still reported as 'installed', since the binary
    is present -- the version is best-effort metadata).
    """
    cli = _find_interceptor_cli()
    if cli is None:
        return None
    result = _run_cli(["--version"])
    # Fail closed: a nonzero return code means the version command did not
    # succeed, so return None even if stdout happens to look like a version
    # string. Installation detection stays independent of this check.
    if result.returncode != 0:
        return None
    version = _parse_version(result.stdout)
    return version


def skill_links_state() -> dict[str, str]:
    """Per-link state for each of router/browser/research.

    'linked' when the slot resolves to a real directory, else 'not
    linked'. A dangling junction (Interceptor uninstalled externally) is
    'not linked' here, never an exception -- same as an absent entry.
    """
    return {
        name: ("linked" if _link_is_real_dir(path) else "not linked")
        for name, path in _link_slot_paths().items()
    }


def skill_links_summary(states: dict[str, str] | None = None) -> str:
    """Compact 'x/total' summary (linked slots over total slots).

    Reads the live link states when no mapping is passed, but accepts one so
    callers (status()) derive the summary from the exact same scan they
    returned in a single read -- keeping the two consistent.
    """
    if states is None:
        states = skill_links_state()
    linked = sum(1 for s in states.values() if s == "linked")
    return f"{linked}/{len(states)}"


def extension_state() -> str:
    """'ready' when the extension loads in every profile, else 'extension
    missing'.

    Comes from the CLI's own JSON diagnosis. A missing CLI, a failing
    command, a non-JSON/empty body, or a diagnosis that reports problems
    all read as 'extension missing' -- never an exception escaping.
    """
    result = _run_cli(EXTENSION_DIAGNOSIS_ARGS)
    if result.returncode != 0 or not result.stdout.strip():
        return "extension missing"
    try:
        data = json.loads(result.stdout)
    except (ValueError, TypeError):
        return "extension missing"
    # Fail closed: data must be a dict whose problems and blocked arrays are
    # both present and empty. Missing fields, null, []-shaped or non-list
    # values read as "extension missing", never evidence of readiness.
    if not isinstance(data, dict):
        return "extension missing"
    problems = data.get("problems")
    blocked = data.get("blocked")
    if not isinstance(problems, list) or not isinstance(blocked, list):
        return "extension missing"
    if problems or blocked:
        return "extension missing"
    return "ready"


def extension_dir() -> str | None:
    """The unpacked browser-extension folder Chrome must load, or None.

    Ticket t6-guided-panel (#636): the guided panel needs the exact folder
    path for Chrome's "Load unpacked" step. The browser-only Inno Setup
    install lays the extension down beside the CLI (``<install
    dir>\\extension``, a manifest-keyed unpacked extension -- its ID
    derives from the manifest key, so YAAH never copies or rewrites it).
    The dir is reported when it holds a loadable manifest
    (``extension\\manifest.json``); anything else (Interceptor absent,
    external uninstall, a stray file) reads as None -- the panel shows no
    path rather than a wrong one. Same documented candidates the CLI
    lookup and path_prepend_dir() use; pure filesystem, never a
    subprocess.
    """
    for candidate_dir in _INSTALL_DIR_CANDIDATES:
        ext = Path(candidate_dir) / "extension" / "manifest.json"
        if ext.is_file():
            return str(Path(candidate_dir) / "extension")
    return None


def read_pinned_manifest() -> dict | None:
    """The committed pinned-installer manifest, or None when absent/invalid.

    Carries asset URL + SHA-256 + upstream version + Elastic License 2.0
    note. Read fresh on every status call so a manifest change lands
    without a code change.

    Discovery mirrors skills.bundled_source_dir: the repo checkout keeps
    the beside-module anchor; the installed app ships the manifest as a
    Tauri resource at <exe>/_up_/backend/agent/ (the frozen one-file
    extraction dir carries no data files), so the packaged-layout scan
    must run first or the Install button can never verify a download.
    First existing candidate wins; none exist -> None (fail closed).
    """
    candidates = gitproc.bundle_dirs("agent", "interceptor_manifest.json")
    candidates.append(MANIFEST_PATH)  # repo checkout: beside the module
    for candidate in candidates:
        try:
            is_file = candidate.is_file()
        except OSError:
            # A denied/unreadable parent dir must not escape as an
            # exception -- fail closed, same as every probe here.
            continue
        if is_file:
            return _read_manifest_file(candidate)
    return None


def _read_manifest_file(path: Path) -> dict | None:
    if not path.is_file():
        return None
    try:
        raw = path.read_text(encoding="utf-8")
        data = json.loads(raw)
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    return data


def status() -> dict:
    """The manager's single status contract -- the vocabulary every other
    surface (API routes, settings UI, agent skill) speaks:

      installed            'installed' | 'not installed'
      version              dotted version string, or None
      skill_links          {name: 'linked'|'not linked'}
      skill_links_summary  'x/total'
      extension            'ready' | 'extension missing'
      cli                the resolved CLI path, or None
    """
    version = installed_version()
    link_states = skill_links_state()
    return {
        "installed": installed_state(),
        "version": version,
        "skill_links": link_states,
        "skill_links_summary": skill_links_summary(link_states),
        "extension": extension_state(),
        "cli": _find_interceptor_cli(),
    }
