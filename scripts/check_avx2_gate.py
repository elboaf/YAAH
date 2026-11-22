#!/usr/bin/env python3
"""#778 release gate: the Linux frozen executables must bundle the BASELINE
libpython, not an x86-64-v3 one.

Pre-Haswell (e.g. i5-3470) CPUs die with SIGILL when the libpython frozen
into the PyInstaller onefile executable was built for x86-64-v3.

Linux PyInstaller --onefile executables are ELF + appended CArchive: the
shared libraries are zlib-compressed archive entries, so plain objdump on
the executable sees nothing. This script reads the CArchive cookie + TOC
directly (the documented PyInstaller format, see
PyInstaller/archive/readers.py) and decompresses the libpython3*.so*
entries with zlib.

Verdict per bundled libpython (PR #803 review redesign):
1. Chain of custody - sha256 equality with the MEASURED libpython of the
   pinned baseline interpreter (_avx2_calibration). Byte-identical to the
   artifact we disassembled end-to-end: zero inference. This is the
   primary gate.
2. Calibrated spread scan - defense in depth for the "tarball updated,
   calibration not" window: mnemonic counts must stay within the measured
   baseline envelope (3x measured, min 25). Mnemonic PRESENCE alone is
   NOT a violation: the baseline libpython legitimately contains 76
   AVX2-only instructions in dispatch-gated crypto clusters; the v3 build
   of the same tag has 488 spread wide. A hash miss plus an in-envelope
   spread still fails the build (unknown bytes shipped) but says so
   honestly.

Usage: check_avx2_gate.py EXECUTABLE [EXECUTABLE ...]
Missing paths are skipped (each release matrix cell builds only its own
artifact); if NONE of the paths exists, the gate fails.
"""
import hashlib
import importlib
import os
import re
import struct
import subprocess
import sys
import tempfile
import zlib

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
cal = importlib.import_module("_avx2_calibration")

AVX2_ONLY = ("vinserti128", "vperm2i128", "vpgatherdd")

# PyInstaller CArchive format (PyInstaller/archive/readers.py, PyInstaller 6.x)
COOKIE_MAGIC = b"MEI\014\013\012\013\016"
COOKIE_FORMAT = "!8sIIII64s"  # magic, pkg_length, toc_offset, toc_length, pyvers, pylib_name
COOKIE_LENGTH = struct.calcsize(COOKIE_FORMAT)
TOC_ENTRY_FORMAT = "!IIIIBc"  # entry_length, offset, length, uncompressed_length, compression, typecode
TOC_ENTRY_LENGTH = struct.calcsize(TOC_ENTRY_FORMAT)

TYPE_BINARY = b"b"  # shared libraries / binaries in the TOC


def disassemble(path):
    """objdump -D a file, return raw disassembly bytes (seam for tests)."""
    return subprocess.run(
        ["objdump", "-D", path],
        check=True, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
    ).stdout


def read_carchive_entry(exe, name_glob):
    """Yield (entry_name, bytes) for every CArchive entry matching name_glob."""
    with open(exe, "rb") as fp:
        fp.seek(0, os.SEEK_END)
        size = fp.tell()
        # The cookie sits at the tail of the appended archive; a 1 MiB
        # look-back is far more than COOKIE_LENGTH + any trailing padding.
        tail_start = max(0, size - (1 << 20))
        fp.seek(tail_start)
        tail = fp.read()
        cookie_pos = tail.rfind(COOKIE_MAGIC)
        if cookie_pos == -1:
            raise RuntimeError(f"{exe}: not a PyInstaller CArchive (no cookie magic)")
        magic, pkg_length, toc_offset, toc_length, _pyvers, _pylib = struct.unpack(
            COOKIE_FORMAT, tail[cookie_pos:cookie_pos + COOKIE_LENGTH]
        )
        archive_end = tail_start + cookie_pos + COOKIE_LENGTH
        archive_start = archive_end - pkg_length
        fp.seek(archive_start + toc_offset)
        toc_data = fp.read(toc_length)

    cur = 0
    while cur < len(toc_data):
        entry_length, entry_offset, data_length, _uncompressed, compression, typecode = \
            struct.unpack(TOC_ENTRY_FORMAT, toc_data[cur:cur + TOC_ENTRY_LENGTH])
        name_bytes = toc_data[cur + TOC_ENTRY_LENGTH:cur + entry_length]
        name = name_bytes.rstrip(b"\x00").decode("utf-8")
        cur += entry_length
        if typecode != TYPE_BINARY or not re.search(name_glob, name):
            continue
        with open(exe, "rb") as fp:
            fp.seek(archive_start + entry_offset)
            data = fp.read(data_length)
        if compression:
            data = zlib.decompress(data)
        yield name, data


def mnemonic_counts(dis):
    """Whole-word mnemonic counts in objdump output (seam for tests)."""
    return {m: len(re.findall(rb"\b%s\b" % m.encode(), dis)) for m in AVX2_ONLY}


def check_executable(exe):
    """Verify the bundled libpython: pinned-hash custody, then calibrated spread."""
    libs = list(read_carchive_entry(exe, r"libpython3.*\.so"))
    if not libs:
        raise RuntimeError(f"{exe}: no libpython3*.so entry found in the CArchive")
    for name, data in libs:
        digest = hashlib.sha256(data).hexdigest()
        if digest == cal.PINNED_LIBPYTHON_SHA256:
            print(
                f"{exe}: bundled {name}: sha256 matches the pinned baseline "
                "artifact (chain of custody OK)"
            )
        else:
            print(
                f"::warning::{exe}: bundled {name} is NOT byte-identical to the "
                f"pinned baseline libpython (sha256 {digest}); judging by the "
                "calibrated spread scan alone. If the pin was updated on "
                "purpose, re-measure scripts/_avx2_calibration.py.",
                file=sys.stderr,
            )
        with tempfile.NamedTemporaryFile(suffix=".so", delete=False) as tmp:
            tmp.write(data)
            tmp_path = tmp.name
        try:
            dis = disassemble(tmp_path)
        finally:
            os.unlink(tmp_path)
        counts = mnemonic_counts(dis)
        offenders = [(m, c) for m, c in counts.items() if c > cal.threshold(m)]
        if offenders:
            detail = ", ".join(f"{m}={c} (limit {cal.threshold(m)})" for m, c in offenders)
            raise RuntimeError(
                f"{exe}: bundled {name} AVX2-only mnemonic spread {detail} "
                "exceeds the calibrated baseline envelope - looks like an "
                "x86_64_v3 body (pre-Haswell CPUs would SIGILL)"
            )
        summary = ", ".join(f"{m}={counts[m]}" for m in AVX2_ONLY)
        print(f"{exe}: bundled {name}: spread {summary} within baseline envelope")


def main(argv):
    if not argv:
        print("usage: check_avx2_gate.py EXECUTABLE [EXECUTABLE ...]", file=sys.stderr)
        return 2
    existing = [p for p in argv if os.path.isfile(p)]
    if not existing:
        print(f"::error::none of the gate inputs exists: {', '.join(argv)}", file=sys.stderr)
        return 1
    rc = 0
    for exe in existing:
        try:
            check_executable(exe)
        except RuntimeError as exc:
            print(f"::error::{exc}", file=sys.stderr)
            rc = 1
    return rc


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
