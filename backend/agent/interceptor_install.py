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
import json
import ctypes
import os
import subprocess
from pathlib import Path

from backend.agent import interceptor
from backend.agent.gitproc import CREATE_NO_WINDOW as _CREATE_NO_WINDOW


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
        # curl.exe is a console binary spawned from a windowed app: without
        # the flag a console window flashes during the download (#777).
        creationflags=_CREATE_NO_WINDOW,
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
        # The Inno Setup installer is a console-flashed child of a windowed
        # app; CREATE_NO_WINDOW suppresses the flash (#777). POSIX: no-op (0).
        creationflags=_CREATE_NO_WINDOW,
    )


# --- junctions -----------------------------------------------------------

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
# The I/O system reparse tag for a directory junction (a mount point).
# Other reparse points -- for example cloud-synchronized placeholders from
# OneDrive/Files On-Demand -- share _JUNCTION_ATTRIBUTE but carry a different
# tag, so the tag is required to avoid mistaking them for a junction.
_IO_REPARSE_TAG_MOUNT_POINT = 0xA0000003


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
    if not (attr != 0xFFFFFFFF and bool(attr & _JUNCTION_ATTRIBUTE)):
        return False
    # The attribute is shared by other reparse points (cloud placeholders,
    # etc.), so require the mount-point reparse tag too. Only a junction has
    # IO_REPARSE_TAG_MOUNT_POINT. lstat does not follow the entry, so a
    # dangling junction still yields the tag; st_reparse_tag is Windows-only,
    # and a non-Windows host never reaches here (kernel32 is absent).
    try:
        return os.lstat(str(path)).st_reparse_tag == _IO_REPARSE_TAG_MOUNT_POINT
    except OSError:
        return False


def _is_link_entry(path: Path) -> bool:
    """True when path is a junction OR a (directory) symlink -- i.e. any
    entry YAAH can legitimately create as a link to an Interceptor pack.

    A junction is the Windows-only link type the install flow lays down; a
    directory symlink is an equivalent repairable link (is_dir() follows it).
    A plain real directory is NOT a link: it may be the user's own folder or
    (as in #775) YAAH's own bundled skill, and it is never treated as a
    healthy Interceptor pack link. This mirrors the manager's
    ``_link_is_link`` in interceptor.py so both surfaces agree on what counts
    as "linked" vs "occupied" for the same slot.
    """
    return _is_junction(path) or path.is_symlink()


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
        # powershell.exe is a console child of a windowed app; suppress the
        # flash at each of the three junction creations (#777). POSIX: no-op.
        creationflags=_CREATE_NO_WINDOW,
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

    Idempotency: if the done-marker and the CLI are both present, nothing
    is re-downloaded and the installer is not re-run -- but the junctions
    are still walked by the repair-only pass, so a junction a user (or an
    Interceptor update) deleted or re-pointed is repaired rather than
    silently left dangling: the second click repairs/reports, never
    corrupts.
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

    # Idempotency: a fully-successful prior install is not re-run -- no
    # download, no installer -- but the junctions ARE walked by the
    # repair-only pass: a slot a user deleted or re-pointed since the last
    # install gets repaired here, so a second click repairs/reports rather
    # than corrupting. When the packs root is gone (CLI resolves via PATH
    # but the install dir was wiped) there is nothing to repair against:
    # report the current state instead of failing the re-click.
    if _marker_done() and _cli_installed():
        try:
            junction_result = _create_junctions()
            # Re-record so remove_links() can vouch for repaired/recreated
            # links even after the ownership manifest itself was lost.
            _record_live_link_ownership(junction_result)
            note = "already installed; junctions verified/repaired, nothing re-run"
        except RuntimeError as exc:
            junction_result = _junctions_result()
            note = f"already installed; nothing re-run (junctions not repaired: {exc})"
        return {
            "installed": True,
            "did_install": False,
            "junctions": junction_result["junctions"],
            "occupied": junction_result.get("occupied", {}),
            "marker": True,
            "note": note,
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
    # directory (a user's own folder, or YAAH's own bundled 'research' skill
    # in #775) is never replaced; a dangling or mis-pointed junction is
    # removed and recreated. The result is {"junctions": {...},
    # "occupied": {...}}: only actual junctions/symlinks go in `junctions`
    # (so `install()` never advertises a junction target that doesn't
    # exist); preserved real directories go in `occupied` instead.
    junction_result = _create_junctions()
    junctions = junction_result["junctions"]
    # Record ownership so remove_links() can later distinguish these junctions
    # from foreign entries that merely share a slot name. Only live links are
    # claimed -- a slot preserved as the user's real directory stays unclaimed.
    _record_live_link_ownership(junction_result)

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
    """name -> absolute target pack dir for each of router/browser/research.

    Delegates to interceptor._link_slot_paths() (the manager's own slot map)
    so the two modules share one definition of where the slots live.
    """
    return interceptor._link_slot_paths()


def _remove_dir_link(path: Path) -> None:
    """Remove a junction or directory symlink reliably.

    os.rmdir handles Windows directory links (junctions and directory
    symlinks); os.unlink is the POSIX path, where os.rmdir refuses a
    symlink. Raises OSError only when both fail (locked/in-use entry).
    """
    try:
        os.rmdir(str(path))
    except OSError:
        os.unlink(str(path))


def _create_junctions() -> dict:
    """Create/repair the three skill junctions. Returns a dict with two
    keys: ``junctions`` (name -> resolved target for each junction/symlink
    YAAH verified or created) and ``occupied`` (name -> resolved path for
    each slot holding a *real* directory that is not a link -- the user's
    own folder, or YAAH's own bundled 'research' skill in #775).

    Link slot name (router/browser/research) is paired with its matching
    installed pack directory name (interceptor/interceptor-browser/
    interceptor-research) by shared order in interceptor.LINK_SLOTS and
    interceptor._INTERCEPTOR_PACK_NAMES. A junction or directory symlink
    already pointing at the correct pack is left untouched (no delete +
    recreate churn on re-run); a mis-pointed or dangling one is replaced;
    a pre-existing *real* directory is never replaced (it is reported as
    an occupied slot, NOT as a junction target and never counted as
    linked).
    """
    root = _installed_packs_root()
    if root is None:
        raise RuntimeError(
            "interceptor skills root not found; nothing to junction to"
        )
    targets: dict[str, str] = {}
    occupied: dict[str, str] = {}
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
        expected = str(target_dir.resolve())
        link_path = _link_slot_targets()[name]
        if link_path.is_dir() and not _is_link_entry(link_path):
            # A pre-existing *real* directory at the slot (the user's own
            # folder, or YAAH's own bundled 'research' skill in #775) is never
            # replaced. It is NOT a junction, so it must NOT be advertised in
            # the junctions output (that would claim a junction target that
            # does not exist) nor counted as linked. Record it separately as
            # an occupied slot so callers can surface it honestly. Directory
            # symlinks are NOT this case: is_dir() follows them, they are
            # repairable links, so they fall through below.
            occupied[name] = str(link_path.resolve())
            continue
        # Skip delete+recreate churn when the link already points at the
        # expected pack (the common every-rerun case).
        if _is_link_entry(link_path) and _owns_entry(link_path, expected):
            targets[name] = expected
            continue
        # Remove a recognized junction or symlink so we can recreate it
        # pointing at the correct pack. A regular file (or any other entry)
        # is left in place and reported as a conflict rather than deleted.
        old_target: str | None = None
        if _is_link_entry(link_path):
            try:
                # The raw stored target (os.readlink works for junctions and
                # directory symlinks) so a failed replacement can restore it.
                old_target = os.readlink(str(link_path))
            except OSError:
                old_target = None
            try:
                _remove_dir_link(link_path)
            except OSError:
                raise RuntimeError(
                    f"could not remove existing link at slot {link_path}; "
                    "leaving it in place rather than replacing it"
                ) from None
        elif os.path.lexists(str(link_path)):
            # A real entry that is neither junction nor symlink: preserve it.
            raise RuntimeError(f"conflict at skill slot {link_path}")
        # Ensure the parent dir exists so _create_junction never runs against
        # a missing parent, which would otherwise fail the whole install.
        link_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            _create_junction(expected, link_path)
        except RuntimeError:
            # A failed replacement must not leave a previously-working slot
            # empty: restore the link exactly as it was (worst case still
            # mis-pointed, never missing), then surface the original error.
            if old_target and not os.path.lexists(str(link_path)):
                _create_junction(old_target, link_path)
            raise
        targets[name] = expected
    return {"junctions": targets, "occupied": occupied}


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
# its type alone. A missing/corrupt manifest is treated as "no record":
# YAAH cannot prove a link is its own, so nothing is removed -- the ticket
# says "unlinks exactly what YAAH created -- nothing else", and a link of
# unknown ownership is not provably YAAH's.
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
        json.dumps(
            {name: target for name, target in targets.items()}, indent=2
        )
        + "\n",
        encoding="utf-8",
    )


def _record_live_link_ownership(junction_result: dict) -> None:
    """Record ownership for slots whose CURRENT entry is a live link.

    junction_result is the dict from _create_junctions: its `junctions`
    maps slot -> resolved target for actual junction/symlink links (only
    those are links), and `occupied` lists preserved real directories that
    are NOT links and stay unclaimed -- so remove_links() vouches only for
    links and can never delete the user's real directory.
    """
    slot_paths = _link_slot_targets()
    live = {
        name: target
        for name, target in junction_result.get("junctions", {}).items()
        if _is_junction(slot_paths[name]) or slot_paths[name].is_symlink()
    }
    _record_junction_ownership(live)


def _read_junction_ownership() -> dict:
    """The name -> target recorded by the last successful install, or {}."""
    manifest = _ownership_manifest_path()
    if not manifest.is_file():
        return {}
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


def _junctions_result() -> dict:
    """The {"junctions": {...}, "occupied": {}} view of the current on-disk
    junctions. Used as the fallback when the repair-only pass raises (e.g.
    a pack dir is gone mid-re-run): advertise only the junctions that are
    actually live, and report every preserved real directory as occupied so
    the returned dict never claims a junction target that does not exist.
    """
    junctions: dict[str, str] = {}
    occupied: dict[str, str] = {}
    for name, link_path in _link_slot_targets().items():
        if _is_junction(link_path):
            try:
                junctions[name] = str(link_path.resolve())
            except OSError:
                junctions[name] = None
        elif link_path.is_symlink():
            junctions[name] = None
        elif link_path.is_dir():
            # A real directory that is not a link: preserved, never a junction.
            occupied[name] = str(link_path.resolve())
    return {"junctions": junctions, "occupied": occupied}


def remove_links() -> dict:
    """Delete only the junctions YAAH created, leaving everything else.

    Ownership is decided by the manifest install() wrote, never by the
    entry's type alone: a link is removed only when the ownership record
    exists AND the entry still points at the recorded target. With no
    ownership record (or a corrupt one) nothing is removed: the ticket
    says "unlinks exactly what YAAH created -- nothing else", and without
    the record YAAH cannot prove a link is its own -- a foreign junction
    that merely shares a slot name is not YAAH's to delete. A real
    directory at a slot is never removed in any case.

    Returns {"removed": [paths], "skipped": {slot: reason}} so a caller
    can surface exactly what was left in place and why.
    """
    removed: list[str] = []
    skipped: dict[str, str] = {}
    ownership = _read_junction_ownership()
    for name, link_path in _link_slot_targets().items():
        is_link = _is_junction(link_path) or link_path.is_symlink()
        if not is_link:
            # A real directory (or an absent entry): never YAAH's to delete,
            # whatever the ownership record says.
            skipped[name] = "not a link (real directory or absent)"
            continue
        target = ownership.get(name)
        if target is None:
            skipped[name] = "no ownership record for this slot"
            continue
        if not _owns_entry(link_path, target):
            # Re-pointed or otherwise no longer YAAH's: leave it alone.
            skipped[name] = "link exists but points elsewhere than recorded"
            continue
        try:
            _remove_dir_link(link_path)
            removed.append(str(link_path))
        except OSError:
            skipped[name] = "delete failed (entry is locked or in use)"
    return {"removed": removed, "skipped": skipped}
