"""Shared calibration data for the #778 baseline gates.

The numbers here are MEASURED on the pinned artifact, not invented:
python-build-standalone 20261110, cpython-3.12.16, plain
x86_64-unknown-linux-gnu (the baseline flavor), libpython3.12.so.1.0
SHA-256 ca66582b...e0c98. See the PR #803 thread for the full scan
method (Capstone over ELF executable sections, restart-on-invalid so
no byte range is silently skipped).

Two facts the gates are built around:
- Mnemonic PRESENCE proves nothing: the baseline libpython legitimately
  contains 76 AVX2-only instructions in 8 tight, runtime-dispatch-gated
  clusters (hand-written crypto ASM: SHA-256/ChaCha20/poly1305 style).
  The x86_64_v3 build of the SAME tag has 488 hits spread over 6.1 MB.
- What actually identifies a v3 build: ELF ISA requirement notes,
  compiler-v3 codegen density, and (definitively) the libpython bytes.
"""
import hashlib
import os

# python/lib/libpython3.12.so.1.0 inside the pinned install_only tarball.
PINNED_LIBPYTHON_SHA256 = (
    "ca66582be9be872ddb8cd984535ef1dd8c2141afa50361b34adbaab1bb2e0c98"
)

# Measured on the pinned baseline libpython (see module docstring).
BASELINE_BASE = {
    "vinserti128": 60,
    "vperm2i128": 16,
    "vpgatherdd": 0,
}

# The v3 build of the same tag measured 488 hits in 64 clusters spread
# over 6.1 MB. A rebuild of the SAME baseline source can wobble (toolchain
# noise), so the threshold is generous: 3x the measured baseline per
# mnemonic, but never fewer than 25 (vpgatherdd baseline is 0; a lone
# stray is noise, a routine is not).
def threshold(mnemonic: str) -> int:
    return max(25, 3 * BASELINE_BASE.get(mnemonic, 0))


def sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fp:
        for chunk in iter(lambda: fp.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def libpython_candidates_for(prefix: str):
    """Runtime libpython discovery for THIS interpreter.

    PBS's sysconfig data carries BUILD-time paths (prefix=/install): the
    Makefile/_sysconfigdata baked into the tarball point at a path that
    does not exist after extraction (CodeRabbit finding on PR #803). The
    runtime layout is sys.prefix + /lib, falling back to exec prefix.
    """
    import glob

    bases = [
        prefix,
        os.path.join(prefix, "lib"),
    ]
    # de-dup, keep order
    seen = set()
    out = []
    for base in bases:
        if base in seen:
            continue
        seen.add(base)
        out.extend(sorted(glob.glob(os.path.join(base, "libpython3*.so*"))))
    return sorted(set(out))
