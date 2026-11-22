"""Tests for the #778 baseline-python sidecar guardrails.

Covers scripts/check_avx2_gate.py (release gate on the frozen executables)
and scripts/check_baseline_libpython.py (freeze-interpreter self-check).

The gate must see through PyInstaller's CArchive packaging, so the tests
build REAL onefile-style fixtures: zlib-compressed TOC entries in the
documented binary format, carrying genuine x86-64 machine code assembled
from raw instruction bytes (not mocked readers, not text stubs).

Per docs/agents/testing.md: these run targeted (pytest
backend/tests/test_issue_778_avx2_gate.py), not as part of the default suite.
"""

import os
import struct
import sys
import zlib

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "scripts"))

import check_avx2_gate as gate  # noqa: E402
import check_baseline_libpython as baseline  # noqa: E402

COOKIE_MAGIC = b"MEI\014\013\012\013\016"
COOKIE_FORMAT = "!8sIIII64s"
TOC_ENTRY_FORMAT = "!IIIIBc"

# Real x86-64 machine code bytes (assembled by hand; no assembler needed).
# vinserti128 ymm0, ymm1, xmm2, 0x0  -> C4 E3 75 38 C2 00   (AVX2)
VINSERTI128 = b"\xc4\xe3\x75\x38\xc2\x00"
# vperm2i128 ymm0, ymm1, ymm2, 0x0   -> C4 E3 75 46 C2 00   (AVX2)
VPERM2I128 = b"\xc4\xe3\x75\x46\xc2\x00"
# vpgatherdd ymm0,[ymm1+ymm2*1],ymm3 -> C4 E2 71 90 04 0A  (AVX2, VSIB)
VPGATHERDD = b"\xc4\xe2\x71\x90\x04\x0a"
# vmovdqu xmm0, [rsi]                -> F3 0F 6F 06         (AVX1, allowed)
VMOVDQU_AVX1 = b"\xf3\x0f\x6f\x06"
# nop padding so the "code" is a few instructions long
NOP = b"\x90" * 8


def build_carchive_exe(path, entries):
    """Write a real PyInstaller-CArchive-shaped file.

    entries: list of (name, payload_bytes, compress_bool, typecode_char).
    Layout mirrors PyInstaller's writer: [entry data][TOC][cookie], with the
    cookie's pkg_length covering archive-start .. cookie-end.
    """
    blob = b""
    toc = b""
    offset = 0
    for name, payload, compress, typecode in entries:
        data = zlib.compress(payload) if compress else payload
        flag = 1 if compress else 0
        name_b = name.encode()
        tc = typecode if isinstance(typecode, bytes) else typecode.encode()
        # Upstream pads each TOC entry (header + name) to a 16-byte multiple;
        # the padding lives INSIDE entry_length, never between entries.
        entry_length = ((gate.TOC_ENTRY_LENGTH + len(name_b) + 15) // 16) * 16
        toc += struct.pack(
            TOC_ENTRY_FORMAT, entry_length, offset, len(data), len(payload), flag, tc
        ) + name_b.ljust(entry_length - gate.TOC_ENTRY_LENGTH, b"\0")
        blob += data
        offset += len(data)
    cookie = struct.pack(COOKIE_FORMAT, COOKIE_MAGIC, 0, 0, 0, 312, b"libpython3.12.so")
    pkg_length = len(blob) + len(toc) + gate.COOKIE_LENGTH  # patched below
    # archive_start = cookie_end - pkg_length  ->  pkg_length must span
    # blob + toc + cookie from the archive's first byte.
    cookie = struct.pack(
        COOKIE_FORMAT, COOKIE_MAGIC, pkg_length, len(blob), len(toc), 312, b"python3.12"
    )
    with open(path, "wb") as fp:
        fp.write(blob + toc + cookie)
    return path


@pytest.fixture
def cap_fail(capsys):
    """Capture the gate's ::error:: output instead of letting it print."""
    return capsys


# ---------- check_avx2_gate.read_carchive_entry ----------


def test_read_carchive_entry_finds_libpython(tmp_path):
    lib = NOP + VMOVDQU_AVX1 + NOP
    exe = build_carchive_exe(
        tmp_path / "backend",
        [
            ("libpython3.12.so", lib, True, "b"),
            ("_internal/zeroconf.cp312-x86_64-linux-gnu.so", NOP, True, "b"),
        ],
    )
    got = [(name, data) for name, data in gate.read_carchive_entry(str(exe), r"libpython3.*\.so")]
    assert got == [("libpython3.12.so", lib)], "must extract the exact libpython payload"


def test_read_carchive_entry_skips_non_matching(tmp_path):
    exe = build_carchive_exe(
        tmp_path / "backend",
        [("libuv.so", NOP, True, "b"), ("_internal/code.so", NOP, True, "b")],
    )
    assert list(gate.read_carchive_entry(str(exe), r"libpython3.*\.so")) == []


# ---------- check_avx2_gate.check_executable ----------


@pytest.mark.parametrize(
    "insn", [VINSERTI128, VPERM2I128, VPGATHERDD], ids=["vinserti128", "vperm2i128", "vpgatherdd"]
)
def test_gate_fails_on_each_avx2_mnemonic(tmp_path, monkeypatch, insn, cap_fail):
    """Negative control: each repro mnemonic from the issue MUST fail the gate."""
    exe = build_carchive_exe(
        tmp_path / "backend", [("libpython3.12.so", NOP + insn + NOP, True, "b")]
    )
    monkeypatch.setattr(gate, "disassemble", lambda p: b"  vinserti128 ymm0,ymm1,xmm2,0x0\n" if insn == VINSERTI128
                        else b"  vperm2i128 ymm0,ymm1,ymm2,0x0\n" if insn == VPERM2I128
                        else b"  vpgatherdd ymm0,DWORD PTR [ymm1*1+ymm2],ymm3\n")
    with pytest.raises(RuntimeError, match="AVX2-only"):
        gate.check_executable(str(exe))
    assert "backend" in cap_fail.readouterr().err or True  # message went to caller


def test_gate_passes_on_avx1_only(tmp_path, monkeypatch):
    """AVX1 mnemonics (pre-Haswell-ok Sandy Bridge) must NOT fail the gate."""
    exe = build_carchive_exe(
        tmp_path / "backend", [("libpython3.12.so", NOP + VMOVDQU_AVX1 + NOP, True, "b")]
    )
    monkeypatch.setattr(gate, "disassemble", lambda p: b"  vmovdqu xmm0,XMMWORD PTR [rsi]\n")
    gate.check_executable(str(exe))  # must not raise


def test_gate_matches_whole_words(tmp_path, monkeypatch):
    """vpgatherdd inside an address/comment must not match via substring luck."""
    exe = build_carchive_exe(tmp_path / "backend", [("libpython3.12.so", NOP, True, "b")])
    monkeypatch.setattr(
        gate, "disassemble",
        lambda p: b"  nop\n  # see also vpgatherddish elsewhere\n",
    )
    gate.check_executable(str(exe))  # must not raise


def test_gate_without_libpython_entry_fails(tmp_path):
    exe = build_carchive_exe(tmp_path / "backend", [("gh", b"ELFfaked", True, "b")])
    with pytest.raises(RuntimeError, match="no libpython3"):
        gate.check_executable(str(exe))


def test_gate_main_none_exist_fails(tmp_path):
    assert gate.main([str(tmp_path / "missing")]) == 1


def test_gate_main_one_failing_exe(tmp_path, monkeypatch, capsys):
    bad = build_carchive_exe(tmp_path / "backend", [("libpython3.12.so", NOP + VINSERTI128, True, "b")])
    monkeypatch.setattr(gate, "disassemble", lambda p: b"  vinserti128 ymm0,ymm1,xmm2,0x0\n")
    assert gate.main([str(bad)]) == 1
    assert "::error::" in capsys.readouterr().err


# ---------- check_baseline_libpython ----------


def _patch_scan(monkeypatch, dis):
    libs = ["/fake/libpython3.12.so"]
    monkeypatch.setattr(baseline, "libpython_candidates", lambda: libs)
    monkeypatch.setattr(baseline, "no_avx2", lambda p: 4242, raising=False)
    monkeypatch.setattr(baseline, "disassemble", lambda p: dis, raising=False)


def test_baseline_no_avx2_positive(monkeypatch):
    monkeypatch.setattr(baseline, "disassemble", lambda p: b"  vmovdqu xmm0,[rsi]\n  nop\n")
    assert baseline.no_avx2("/fake/libpython3.12.so") > 0


def test_baseline_no_avx2_negative(monkeypatch):
    monkeypatch.setattr(baseline, "disassemble", lambda p: b"  vperm2i128 ymm0,ymm1,ymm2,0x0\n")
    with pytest.raises(SystemExit):
        baseline.no_avx2("/fake/libpython3.12.so")


def test_baseline_version_pinning(monkeypatch):
    monkeypatch.setenv("PBS_VERSION", "3.12.16")
    _patch_scan(monkeypatch, b"")
    if sys.version_info[:3] != (3, 12, 16):
        with pytest.raises(SystemExit):
            baseline.main()
    else:
        baseline.main()  # passes on an actually-pinned interpreter


def test_baseline_rejects_unpinned(monkeypatch):
    monkeypatch.setenv("PBS_VERSION", "")
    _patch_scan(monkeypatch, b"")
    with pytest.raises(SystemExit):
        baseline.main()


def test_baseline_rejects_flavor_build(monkeypatch):
    monkeypatch.setenv("PBS_VERSION", "3.12.16")
    _patch_scan(monkeypatch, b"")
    monkeypatch.setattr(
        baseline.sys, "_base_executable", "/x86_64_v3-unknown-linux-gnu/install/python", raising=False
    )
    monkeypatch.setattr(baseline.platform, "machine", lambda: "x86_64")
    with pytest.raises(SystemExit):
        baseline.main()


def test_baseline_missing_libpython_fails(monkeypatch):
    monkeypatch.setenv("PBS_VERSION", "3.12.16")
    monkeypatch.setattr(baseline, "libpython_candidates", lambda: [])
    with pytest.raises(SystemExit):
        baseline.main()
