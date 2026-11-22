#!/usr/bin/env python3
"""#778 release gate: the Linux frozen executables must not bundle an
AVX2-only libpython.

Pre-Haswell (e.g. i5-3470) CPUs die with SIGILL when the libpython frozen
into the PyInstaller onefile executable was built for x86-64-v3. This gate
disassembles the libpython that ACTUALLY shipped inside dist/backend and
dist/yaah-server and fails the release job if any AVX2-only mnemonic
appears (the issue's repro: vinserti128, vperm2i128, vpgatherdd).

Linux PyInstaller --onefile executables are ELF + appended CArchive: the
shared libraries are zlib-compressed archive entries, so plain objdump on
the executable sees nothing. This script reads the CArchive cookie + TOC
directly (the documented PyInstaller format, see
PyInstaller/archive/readers.py), decompresses the libpython3*.so* entries
with zlib, objdump -D's the raw bytes and greps for the mnemonics.

Usage: check_avx2_gate.py EXECUTABLE [EXECUTABLE ...]
Missing paths are skipped (each release matrix cell builds only its own
artifact); if NONE of the paths exists, the gate fails.
"""
import os
import re
import struct
import subprocess
import sys
import tempfile
import zlib

AVX2_ONLY = ("vinserti128", "vperm2i128", "vpgatherdd")

# PyInstaller CArchive format (PyInstaller/archive/readers.py, PyInstaller 6.x)
COOKIE_MAGIC = b"MEI\014\013\012\013\016"
COOKIE_FORMAT = "!8sIIII64s"  # magic, pkg_length, toc_offset, toc_length, pyvers, pylib_name
COOKIE_LENGTH = struct.calcsize(COOKIE_FORMAT)
TOC_ENTRY_FORMAT = "!IIIIBc"  # entry_length, offset, length, uncompressed_length, compression, typecode
TOC_ENTRY_LENGTH = struct.calcsize(TOC_ENTRY_FORMAT)

TYPE_BINARY = b"b"  # shared libraries / binaries in the TOC (struct 'c' -> bytes)


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
        name_len = entry_length - TOC_ENTRY_LENGTH
        name_bytes = toc_data[cur + TOC_ENTRY_LENGTH:cur + entry_length]
        name = name_bytes.rstrip(b"\0").decode("utf-8")
        cur += entry_length
        if typecode != TYPE_BINARY or not re.search(name_glob, name):
            continue
        with open(exe, "rb") as fp:
            fp.seek(archive_start + entry_offset)
            data = fp.read(data_length)
        if compression:
            data = zlib.decompress(data)
        yield name, data


def check_executable(exe):
    """Fail with ::error if an AVX2-only mnemonic shows up in the bundled libpython."""
    pattern = re.compile(rb"\b(?:%s)\b" % b"|".join(m.encode() for m in AVX2_ONLY))
    libs = list(read_carchive_entry(exe, r"libpython3.*\.so"))
    if not libs:
        raise RuntimeError(f"{exe}: no libpython3*.so entry found in the CArchive")
    for name, data in libs:
        try:
            with tempfile.NamedTemporaryFile(suffix=".so", delete=False) as tmp:
                tmp.write(data)
                tmp_path = tmp.name
            dis = disassemble(tmp_path)
        finally:
            os.unlink(tmp_path)
        hits = sorted({m.decode() for m in pattern.findall(dis)})
        if hits:
            first_lines = [
                ln.decode("ascii", "replace").strip()
                for ln in dis.splitlines() if pattern.search(ln)
            ][:5]
            print("offending instructions:", file=sys.stderr)
            for ln in first_lines:
                print(f"  {ln}", file=sys.stderr)
            raise RuntimeError(
                f"{exe}: bundled {name} contains AVX2-only instruction(s) {hits} "
                "(pre-Haswell CPUs would SIGILL)"
            )
        print(f"{exe}: bundled {name}: {len(data)} bytes, no AVX2-only mnemonics")


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
