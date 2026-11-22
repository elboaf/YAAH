#!/usr/bin/env python3
"""#778 build-time self-check: the interpreter about to do the Linux sidecar
freeze must be the BASELINE (non-x86-64-v3) CPython.

Runs inside the workflow right after the pinned python-build-standalone
tarball is extracted, BEFORE its bin/ is prepended to PATH. `sys.executable`
is therefore the interpreter under test - no PATH resolution involved.

Calibrated gate, not a presence check (see scripts/_avx2_calibration.py):
the baseline libpython legitimately contains ~76 AVX2-only instructions in
runtime-dispatch-gated crypto clusters, so mnemonic presence proves nothing.
Checks, any failure kills the job:

1. Chain of custody: this interpreter's libpython3*.so* must hash to the
   pinned artifact's SHA-256. The bytes, not their disassembly, identify
   the build.
2. Spread envelope: per-mnemonic counts must sit at/below the calibrated
   thresholds (3x the measured baseline, floor 25). A v3 build of the same
   tag measured 488 hits; the baseline's own clusters sit at 60/16/0.
3. Plain x86_64, not a v2/v3/v4 flavor build (machine + sys._base_executable).
4. Exact version match against PBS_VERSION, read at CALL time (CI exports it
   at job level; a module-level constant would bake the pin in at import and
   make runtime monkeypatching a no-op).

Windows/macOS jobs never run this (Linux-only workflow steps).
"""
import glob
import os
import platform
import re
import subprocess
import sys
import sysconfig

import _avx2_calibration as cal


def _pbs_version():
    """The pinned version, read at CALL time (see docstring check 4)."""
    return os.environ.get("PBS_VERSION", "")


def fail(msg):
    print(f"::error::{msg}", file=sys.stderr)
    sys.exit(1)


def _disassemble(path):
    """objdump -D a file, return raw disassembly bytes (seam for tests)."""
    return subprocess.run(
        ["objdump", "-D", path],
        check=True, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
    ).stdout


def count_mnemonics(dis: bytes, mnemonic: str) -> int:
    """Whole-word occurrences of mnemonic in objdump output."""
    return len(re.findall(rb"\b%s\b" % re.escape(mnemonic.encode()), dis))


def libpython_candidates():
    """Candidate libpython paths for THIS interpreter (its own installation).

    PBS's sysconfig data carries BUILD-time paths (prefix=/install) that do
    not exist after extraction (CodeRabbit finding on PR #803), so the
    runtime layout (sys.prefix + /lib) is tried first.
    """
    out = cal.libpython_candidates_for(sys.prefix)
    if not out:
        out = cal.libpython_candidates_for(sys.exec_prefix)
    if not out:
        # Last resort: build-time sysconfig locations that still exist.
        for key in ("LDLIBRARY", "LIBDIR"):
            val = sysconfig.get_config_var(key)
            if val and os.path.isdir(val):
                out.extend(sorted(glob.glob(os.path.join(val, "libpython3.[0-9]*.so.[0-9]*"))))
    return sorted(set(os.path.realpath(p) for p in out))


def main():
    # 1) locate the interpreter's own libpython
    libs = libpython_candidates()
    if not libs:
        fail(
            "no libpython3*.so* found for this interpreter "
            f"(prefix={sys.prefix}); cannot verify baseline"
        )

    # 2) chain of custody: the bytes are the pinned artifact's bytes
    for lib in libs:
        digest = cal.sha256_file(lib)
        if digest != cal.PINNED_LIBPYTHON_SHA256:
            fail(
                f"{lib} SHA-256 {digest} does not match the pinned artifact "
                f"({cal.PINNED_LIBPYTHON_SHA256}); chain of custody broken"
            )
    print("chain of custody OK: libpython3* matches the pinned artifact SHA-256")

    # 3) plain x86_64, not a v2/v3/v4 flavor build
    machine = platform.machine()
    if machine != "x86_64":
        fail(f"expected x86_64 machine, got {machine}")
    base_exec = getattr(sys, "_base_executable", "") or ""
    if "x86_64_v3" in base_exec or "x86_64_v2" in base_exec or "x86_64_v4" in base_exec:
        fail(f"flavor build detected via sys._base_executable: {base_exec}")

    # 4) spread envelope: counts vs the calibrated thresholds
    dis = _disassemble(libs[0])
    counts = {m: count_mnemonics(dis, m) for m in cal.BASELINE_BASE}
    over = {m: (n, cal.threshold(m)) for m, n in counts.items() if n > cal.threshold(m)}
    if over:
        fail(
            "AVX2-only mnemonic spread over baseline envelope: "
            + ", ".join(f"{m}={n} > {lim}" for m, (n, lim) in over.items())
        )
    print(
        "within baseline envelope: "
        + ", ".join(f"{m}={n}/{cal.threshold(m)}" for m, n in counts.items())
    )

    # 5) exact pinned version, read at call time
    pinned = _pbs_version()
    if not pinned:
        fail("PBS_VERSION env var missing; refusing to verify unpinned interpreter")
    expected = tuple(int(p) for p in pinned.split("."))
    if sys.version_info[:3] != expected:
        fail(
            f"interpreter version {'.'.join(map(str, sys.version_info[:3]))} "
            f"!= pinned PBS_VERSION {pinned}"
        )

    v = sys.version_info
    print(
        f"baseline check OK: {sys.executable} is CPython {v.major}.{v.minor}.{v.micro} "
        f"on plain x86_64; {len(libs)} libpython3*.so* verified"
    )


if __name__ == "__main__":
    main()
