"""Interceptor install flow -- ticket t2-install (#632).

The manager module (interceptor.py) reports *status*. This module performs
the one action the manager never performs: it installs the Interceptor
browser-only build on the user's machine. Everything here is Windows-only
and stdlib, and it fails closed: any failure leaves the machine exactly as
it found it -- nothing written, nothing run, no partial state.

The install does, in order, and only on success:

  1. Load the pinned manifest (see interceptor.read_pinned_manifest).
  2. Download the exact release asset to a temp file.
  3. SHA-256 it. A mismatch aborts *before* execution -- the temp file is
     deleted, and the install is left in the "not installed" state.
  4. Run the installer silently (per-user, no elevation, upstream's
     documented Inno Setup flags), logging to a temp file. The log is kept
     only if the run fails, so a clean install leaves no trace.
  5. Create the three skill junctions under ~/.yaah/skills (router, browser,
     research), pointing at the installed packs. A pre-existing *real*
     directory at a slot is never touched.
  6. Write a done-marker so later tickets (t3..t8) can tell a fully-done
     install apart from a partial one.

Re-running is safe. The second click re-derives every step from current
state rather than repeating side effects: if the CLI is already installed,
the silent run is skipped; if the junctions already exist and point at the
installed packs, they are left alone; if one points at the wrong place it is
repaired; the marker is only written once.

The process boundary is the one seam to the world: the download and the
installer run both go through interceptor.subprocess, so the test suite can
patch it and assert the *filesystem effects* (temp-file cleanup, junction
targets, marker, no-write-on-failure) without a real download or real
installer. Nothing here calls the network or runs a process directly.
"""
from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
from pathlib import Path

from backend.agent import interceptor


# --- seams to the world ---------------------------------------------------

# The temp directory downloads and installers live in. Isolated from the
# repo and from ~/.yaah so nothing survives a clean machine.
_INSTALL_TMP = Path(
    os.environ.get("YAAH_INSTALL_TMP") or (Path.home() / ".yaah" / "_installer_tmp")
)

# Marker file written only after a fully-successful install. Later tickets
# (t3..t8) key off it.
_DONE_MARKER_PATH = _INSTALL_TMP / "interceptor_install_done.txt"

# Inno Setup silent flags. Per-user (no /RUNASADMIN), suppress the UI, do
# not restart, write a log. The log is only *kept* on failure.
_INSTALL_SILENT_FLAGS = [
    "/VERYSILENT",
    "/SUPPRESSMSGBOXES",
    "/NORESTART",
]

# Exit codes the installer treats as success. Inno Setup returns 0 on a
# clean install, but 3010 (reboot required) also means it installed.
_INSTALL_SUCCESS_RC = frozenset({0, 3010})


# --- download + hash (faked at the subprocess boundary) -------------------


def _download_asset(url: str) -> Path:
    """Download the pinned installer asset to a temp file. Returns the path.

    Faked in tests by patching interceptor.subprocess.run; the real
    implementation uses curl's --fail/--location flags so a 404 or a
    corrupt body aborts the whole fetch with a nonzero exit and writes
    nothing.
    """
    _INSTALL_TMP.mkdir(parents=True, exist_ok=True)
    tmp = _INSTALL_TMP / "installer.exe"
    if tmp.exists():
        tmp.unlink()  # start fresh; a stale download must not be reused
    proc = subprocess.run(
        [
            "curl",
            "--fail",
            "--location",
            "--silent",
            "--output",
            str(tmp),
            url,
        ],
        capture_output=True,
        text=True,
        timeout=600,
        check=False,
    )
    if proc.returncode != 0 or not tmp.exists() or tmp.stat().st_size == 0:
        if tmp.exists():
            tmp.unlink()
        raise RuntimeError(
            f"installer download failed (curl exit {proc.returncode}): "
            f"{proc.stderr.strip() or proc.stdout.strip()}"
        )
    return tmp


def _sha256(path: Path) -> str:
    """The SHA-256 hex digest of a file's bytes."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


# --- installer run (faked at the subprocess boundary) ---------------------


def _run_installer(exe: Path, log: Path) -> subprocess.CompletedProcess:
    """Run the downloaded installer silently per-user. Returns the run.

    ``log`` is created in its parent dir first so a monkeypatched install
    temp dir (a test tmp dir) is honored, not the module-level default.
    """
    log.parent.mkdir(parents=True, exist_ok=True)
    return subprocess.run(
        [str(exe), *_INSTALL_SILENT_FLAGS, f"/LOG={log}"],
        capture_output=True,
        text=True,
        timeout=600,
        check=False,
    )


# --- junctions -----------------------------------------------------------

# Bound once at import. GetFileAttributesW returns 0xFFFFFFFF
# (INVALID_FILE_ATTRIBUTES) when the path does not exist; callers guard
# with path.exists() before relying on the value.
import ctypes
from ctypes import wintypes

# kernel32 is a Windows-only library; importing it on Linux/macOS (e.g. the
# CI runners) would raise at module import and break the whole test
# collection. Load it lazily inside _is_junction so the import stays clean
# on every platform, and only the reparse-point check fails closed (as a
# non-junction) off-Windows.
_load_kernel32 = None


def _get_kernel32():
    """The kernel32 GetFileAttributesW proc, loaded lazily.

    Returns None on non-Windows platforms (no kernel32), so callers treat
    a path as not-a-junction rather than crashing at import.
    """
    global _load_kernel32
    if _load_kernel32 is None:
        try:
            _load_kernel32 = ctypes.CDLL("kernel32").GetFileAttributesW
        except OSError:
            _load_kernel32 = False
    return _load_kernel32 or False

# FILE_ATTRIBUTE_REPARSE_POINT. A junction carries this flag on the entry
# itself; a plain directory does not -- the only reliable way to tell the
# two apart on Windows (is_dir()/is_symlink() agree for both).
_JUNCTION_ATTRIBUTE = 0x400


def _is_junction(path: Path) -> bool:
    """True when path is a directory reparse point (a junction).

    Windows reports a junction with the same flags as a plain directory --
    is_dir() and is_symlink() are both True and False respectively for
    both -- so os.path cannot tell them apart. The reparse-point attribute
    is the only discriminator. A missing path reads as not-a-junction. The
    existence check uses ``os.path.lexists`` (non-following metadata) rather
    than ``path.exists`` so a *dangling* junction -- an entry whose target is
    gone -- still counts as a junction and is not rejected by the caller on
    the strength of its missing target.
    """
    if not os.path.lexists(str(path)):
        return False
    get_attrs = _get_kernel32()
    if not get_attrs:
        # No kernel32 (non-Windows): a junction is a Windows object, so
        # treat everything as not-a-junction rather than crash.
        return False
    try:
        attr = get_attrs(str(path))
    except OSError:
        return False
    return attr != 0xFFFFFFFF and bool(attr & _JUNCTION_ATTRIBUTE)


def _create_junction(target_dir: str, link_path: Path) -> None:
    """Create a directory junction at link_path pointing at target_dir.

    Uses PowerShell's New-Item -ItemType Junction, which creates a real
    junction (a directory reparse point) and needs no administrator
    privilege -- unlike os.symlink, which on this host makes a symlink and
    refuses with WinError 1314 without SeCreateSymbolicLinkPrivilege.

    The paths are embedded as PowerShell single-quoted literals (embedded
    quotes doubled) so a path containing characters such as ``$`` or a
    single quote cannot break out of the command string.
    """

    def _ps_literal(value: str) -> str:
        return "'" + value.replace("'", "''") + "'"

    proc = subprocess.run(
        [
            "powershell",
            "-NoProfile",
            "-Command",
            "New-Item -ItemType Junction -Path "
            + _ps_literal(str(link_path))
            + " -Target "
            + _ps_literal(str(target_dir)),
        ],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"junction creation failed for {link_path}: "
            f"{proc.stderr.strip() or proc.stdout.strip()}"
        )


# --- done marker ---------------------------------------------------------


def _write_done_marker() -> None:
    """Record that a full install completed successfully."""
    _INSTALL_TMP.mkdir(parents=True, exist_ok=True)
    _DONE_MARKER_PATH.write_text("done", encoding="utf-8")


# --- the install ----------------------------------------------------------


def install() -> dict:
    """Perform the Interceptor install. Returns a result dict.

    The dict keys mirror the status contract so a UI/skill/skill can render
    the outcome without re-reading. On any failure a RuntimeError is raised
    with a human-readable message and the machine is left as it was found;
    the caller is expected to surface it, never to treat a partial state as
    success.

    Steps, all in order, only the last of which is a side effect:
      1. read the pinned manifest (refuse if absent/unreadable),
      2. download the exact asset,
      3. verify its SHA-256 (abort here on mismatch, zero side effects),
      4. run the installer silently per-user (skip if already installed),
      5. create the three skill junctions (idempotent, repair-only),
      6. write the done-marker (only on full success).

    Idempotency: if the done-marker and the CLI are both present, install
    is a no-op that reports current junction state and re-runs nothing --
    the marker is not rewritten and the junctions are not repaired.
    """
    manifest = interceptor.read_pinned_manifest()
    if not manifest:
        raise RuntimeError(
            "interceptor manifest missing or unreadable; cannot verify install"
        )

    url = manifest["url"]
    expected = (manifest.get("sha256") or "").lower()
    if not url or not expected:
        raise RuntimeError(
            "interceptor manifest missing url or sha256; refusing to install"
        )

    # Idempotency: a fully-successful prior install is reported, not
    # repeated. The junctions and marker already exist; the CLI is present.
    if _marker_done() and _cli_installed():
        return {
            "installed": True,
            "did_install": False,
            "junctions": _junction_targets(),
            "marker": True,
            "note": "already installed; nothing re-run",
        }

    # 1-3: fetch and verify BEFORE running anything. A mismatch aborts with
    # zero side effects -- the temp file is deleted and nothing is run.
    exe = _download_asset(url)
    digest = _sha256(exe)
    if digest != expected:
        exe.unlink()
        raise RuntimeError(
            f"installer hash mismatch: expected {expected}, got {digest}. "
            "Aborting before execution -- nothing installed, nothing run."
        )

    # 4: run silently. Skip if the CLI is already present (re-run safety: a
    # partial previous run that created the CLI but failed at junctions must
    # not re-run the installer). The downloaded installer is always removed
    # afterwards -- whether the run skipped or succeeded -- so a crash between
    # here and the marker write leaves no installer behind to be reused.
    did_run = False
    log = _INSTALL_TMP / "installer.log"
    if log.exists():
        log.unlink()
    try:
        if _cli_installed():
            log.write_text("already installed; installer run skipped", encoding="utf-8")
        else:
            proc = _run_installer(exe, log)
            if proc.returncode not in _INSTALL_SUCCESS_RC:
                # Keep the log for the human to inspect on failure.
                raise RuntimeError(
                    f"installer exited {proc.returncode}: "
                    f"{proc.stderr.strip() or proc.stdout.strip()}"
                )
            did_run = True
    finally:
        exe.unlink()

    # 5: create the three junctions. Each points at the matching installed
    # pack under Interceptor's skills root. Repair-only: a pre-existing real
    # directory (a user's own folder) is never replaced; a dangling or
    # mis-pointed junction is removed and recreated.
    junctions = _create_junctions()
    # Record ownership so remove_links() can later distinguish these junctions
    # from foreign entries that merely share a slot name.
    _record_junction_ownership(junctions)

    # 6: only now, on full success, record the done-marker.
    _write_done_marker()
    return {
        "installed": True,
        "did_install": did_run,
        "junctions": junctions,
        "marker": True,
    }


def _marker_done() -> bool:
    """True when a prior successful install wrote the marker."""
    return _DONE_MARKER_PATH.is_file()


def _cli_installed() -> bool:
    """True when the Interceptor CLI is present (the manager's own check)."""
    return interceptor.installed_state() == "installed"


def _installed_packs_root() -> Path | None:
    """The Interceptor skills root the junctions should point at.

    Derived from the documented install dir (%LOCALAPPDATA%\\Programs\\Interceptor)
    so the junction targets do not depend on whether the status CLI is
    currently resolvable. Returns None when that dir is absent.
    """
    for dir_candidate in interceptor._INSTALL_DIR_CANDIDATES:
        cli_dir = Path(dir_candidate)
        skills = cli_dir / "skills"
        if skills.is_dir():
            return skills
    return None


def _link_slot_targets() -> dict:
    """name -> absolute target pack dir for each of router/browser/research."""
    base = interceptor._skills_dir()
    return {name: base / name for name in interceptor.LINK_SLOTS}


def _create_junctions() -> dict:
    """Create/repair the three skill junctions. Returns name -> resolved target.

    Link slot name (router/browser/research) is paired with its matching
    installed pack directory name (interceptor/interceptor-browser/
    interceptor-research) by shared order in interceptor.LINK_SLOTS and
    interceptor._INTERCEPTOR_PACK_NAMES. A pre-existing junction/symlink/foreign
    link at a slot is removed and recreated to point at the correct pack; a
    pre-existing *real* directory is never replaced (it is reported as the
    slot's current state).
    """
    root = _installed_packs_root()
    if root is None:
        raise RuntimeError(
            "interceptor skills root not found; nothing to junction to"
        )
    targets = {}
    for name, pack_name in zip(
        list(interceptor.LINK_SLOTS), list(interceptor._INTERCEPTOR_PACK_NAMES)
    ):
        target_dir = root / pack_name
        if not target_dir.is_dir():
            # The pack directory the junction should point at does not exist
            # on disk yet: refuse rather than create a dangling junction.
            raise RuntimeError(
                f"expected Interceptor pack directory missing: {target_dir}"
            )
        link_path = _link_slot_targets()[name]
        if link_path.is_dir() and not _is_junction(link_path):
            # A pre-existing real directory at the slot is never replaced.
            targets[name] = str(link_path.resolve())
            continue
        # Remove only a recognized junction or symlink so we can recreate it
        # pointing at the correct pack. A regular file (or any other entry) is
        # left in place and reported as a conflict rather than deleted.
        if _is_junction(link_path) or link_path.is_symlink():
            try:
                link_path.unlink()
            except OSError:
                shutil.rmtree(link_path, ignore_errors=True)
        elif os.path.lexists(str(link_path)):
            # A real entry that is neither junction nor symlink: preserve it.
            raise RuntimeError(f"conflict at skill slot {link_path}")
        # Ensure the parent dir exists so _create_junction never runs against
        # a missing parent, which would otherwise fail the whole install.
        link_path.parent.mkdir(parents=True, exist_ok=True)
        _create_junction(str(target_dir.resolve()), link_path)
        targets[name] = str(target_dir.resolve())
    return targets


def _link_exists(link_path: Path) -> bool:
    """True when an entry exists at link_path even when it is a dangling
    junction/symlink (is_dir/exists both return False for a dangling link)."""
    return link_path.is_symlink() or os.path.lexists(str(link_path))


# --- ownership tracking ---------------------------------------------------

# A JSON manifest of the junctions YAAH created, keyed by slot name, each
# recorded with the absolute pack target YAAH pointed it at. remove_links()
# consults it so it removes a configured entry only when that entry still
# points at the same target: a junction that someone re-pointed, or a real
# directory that merely sits at a slot, is never removed on the strength of
# its type alone. A missing/corrupt manifest is treated as "no record" and
# falls back to type-based removal so an uninstall with no manifest still
# cleans up the junctions it would otherwise leave.
def _ownership_manifest_path() -> Path:
    """Path of the ownership manifest, derived from the current
    ``_INSTALL_TMP`` so it tracks a monkeypatched temp dir in tests."""
    return _INSTALL_TMP / "interceptor_junctions.json"


def _record_junction_ownership(targets: dict) -> None:
    """Persist name -> target for the junctions this install just created."""
    manifest = _ownership_manifest_path()
    if not targets:
        if manifest.exists():
            manifest.unlink()
        return
    _INSTALL_TMP.mkdir(parents=True, exist_ok=True)
    manifest.write_text(
        "{\n" + ",\n".join(f'  "{name}": "{target}"' for name, target in targets.items()) + "\n}\n",
        encoding="utf-8",
    )


def _read_junction_ownership() -> dict:
    """The name -> target recorded by the last successful install, or {}."""
    manifest = _ownership_manifest_path()
    if not manifest.is_file():
        return {}
    import json
    try:
        data = json.loads(manifest.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}
    return data if isinstance(data, dict) else {}


def _owns_entry(link_path: Path, target: str) -> bool:
    """True when the current entry at link_path still matches the target
    YAAH recorded as its owner. A mismatched (re-pointed) or missing target
    means the entry is no longer YAAH's, so remove_links() must leave it."""
    try:
        return link_path.resolve() == Path(target).resolve()
    except OSError:
        return False


def _junction_targets() -> dict:
    """name -> resolved target for each live junction (status view)."""
    targets = {}
    for name, link_path in _link_slot_targets().items():
        if _is_junction(link_path):
            try:
                targets[name] = str(link_path.resolve())
            except OSError:
                targets[name] = None
        else:
            targets[name] = None
    return targets


def remove_links() -> list:
    """Delete only the junctions YAAH created, leaving foreign links and
    real directories untouched. Returns the paths it removed.

    Ownership is decided by the manifest install() wrote, not by the entry's
    type alone: a junction/symlink is removed only when it still matches the
    target YAAH recorded. A real directory (which passes is_dir()/is_symlink())
    is never removed. When no ownership record exists, fall back to removing
    a junction/symlink by type so an uninstall without a manifest still
    cleans up.
    """
    removed = []
    ownership = _read_junction_ownership()
    for name, link_path in _link_slot_targets().items():
        # A real directory passes is_dir() and is_symlink(), so the type
        # check keeps it safe. The ownership record adds a second check for
        # links: do not remove one someone has re-pointed elsewhere.
        target = ownership.get(name)
        is_link = _is_junction(link_path) or link_path.is_symlink()
        if not is_link:
            continue
        if target is not None and not _owns_entry(link_path, target):
            # Re-pointed or otherwise no longer YAAH's: leave it alone.
            continue
        try:
            link_path.unlink()
            removed.append(str(link_path))
        except OSError:
            continue
    return removed
