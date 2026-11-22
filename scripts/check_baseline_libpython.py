#!/usr/bin/env python3
"""#778 build-time self-check: the interpreter about to do the Linux sidecar
freeze must be the BASELINE (non-x86-64-v3) CPython.

Runs inside the workflow right after the pinned python-build-standalone
tarball is extracted, BEFORE its bin/ is prepended to PATH. `sys.executable`
is therefore the interpreter under test — no PATH resolution involved.

Three checks, any failure kills the job:

1. The interpreter's own libpython3*.so* contains no AVX2-only instructions.
   This is the definitive test: the same objdump + mnemonic scan the release
   gate runs on the frozen artifact, applied to the freeze interpreter.
2. The interpreter identifies as plain x86_64 (not an x86_64_v3 flavor
   build): sys.platform is linux, uname/machine is x86_64, and
   sys._base_executable (set) does not mention x86_64_v3.
3. Exact version match: interpreter_info.version matches the workflow-pinned
   PBS_VERSION, so a re-download of a different build cannot slip in.

Windows/macOS jobs never run this (Linux-only workflow steps).
"""
import glob
import os
import platform
import re
import subprocess
import sys
import sysconfig

# AVX2-only mnemonics (the issue's repro list). AVX (v1) mnemonics like
# vzeroupper/vmovdqu are NOT in the list: v1 is Sandy Bridge (2011), older
# than every CPU this fix targets.
AVX2_ONLY = ("vinserti128", "vperm2i128", "vpgatherdd")

PBS_VERSION = os.environ.get("PBS_VERSION", "")


def fail(msg):
    print(f"::error::{msg}", file=sys.stderr)
    sys.exit(1)


def disassemble(path):
    """objdump -D a file, return raw disassembly bytes (seam for tests)."""
    return subprocess.run(
        ["objdump", "-D", path],
        check=True, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
    ).stdout


def libpython_candidates():
    """Candidate libpython paths for THIS interpreter (its own installation)."""
    patterns = []
    for key in ("LDLIBRARY", "LIBDIR", "prefix"):
        val = sysconfig.get_config_var(key)
        if val:
            patterns.append(val)
    out = []
    for base in patterns:
        out.extend(sorted(glob.glob(os.path.join(base, "libpython3*.so*"))))
    if not out:
        # LDLIBRARY may be a bare filename; look under the install tree.
        prefix = sysconfig.get_config_var("prefix") or sys.exec_prefix
        out.extend(sorted(glob.glob(os.path.join(prefix, "lib", "libpython3*.so*"))))
    return sorted(set(os.path.realpath(p) for p in out))


def no_avx2(path):
    """True if objdump -D of path shows none of the AVX2-only mnemonics."""
    try:
        dis = disassemble(path)
    except FileNotFoundError:
        fail("objdump not found on PATH; cannot run the #778 baseline check")
    hits = [m for m in AVX2_ONLY if re.search(rb"\b%s\b" % m.encode(), dis)]
    if hits:
        fail(f"AVX2-only instruction(s) {hits} found in {path}")
    return len(dis)


def main():
    # 1) the interpreter's own libpython is baseline
    libs = libpython_candidates()
    if not libs:
        fail(
            "no libpython3*.so* found for this interpreter "
            f"(prefix={sysconfig.get_config_var('prefix')}); cannot verify baseline"
        )
    total = 0
    for lib in libs:
        print(f"scanning {lib}")
        total += no_avx2(lib)

    # 2) plain x86_64, not a v2/v3/v4 flavor build
    machine = platform.machine()
    if machine != "x86_64":
        fail(f"expected x86_64 machine, got {machine}")
    base_exec = getattr(sys, "_base_executable", "") or ""
    if "x86_64_v3" in base_exec or "x86_64_v2" in base_exec or "x86_64_v4" in base_exec:
        fail(f"flavor build detected via sys._base_executable: {base_exec}")

    # 3) exact pinned version
    if not PBS_VERSION:
        fail("PBS_VERSION env var missing; refusing to verify unpinned interpreter")
    expected = tuple(int(p) for p in PBS_VERSION.split("."))
    if sys.version_info[:3] != expected:
        fail(
            f"interpreter version {'.'.join(map(str, sys.version_info[:3]))} "
            f"!= pinned PBS_VERSION {PBS_VERSION}"
        )

    v = sys.version_info
    print(
        f"baseline check OK: {sys.executable} is CPython {v.major}.{v.minor}.{v.micro} "
        f"on plain x86_64; {len(libs)} libpython3*.so* scanned "
        f"({total} disassembly bytes), no AVX2-only mnemonics"
    )


if __name__ == "__main__":
    main()
