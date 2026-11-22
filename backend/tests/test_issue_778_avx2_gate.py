"""#778 gate tests (redesigned after the PR #803 review).

The verdict logic these tests encode:
- Mnemonic PRESENCE is not a violation: the pinned BASELINE libpython
  legitimately contains 76 AVX2-only instructions in dispatch-gated
  crypto clusters (measured). What fails a build: libpython bytes that
  are neither the pinned artifact NOR within the calibrated baseline
  spread envelope.
- The unpinned-refusal check reads PBS_VERSION at CALL time: CI exports
  it at the job level, so a module-level constant would make the runtime
  monkeypatch a no-op (that exact bug shipped as the DID-NOT-RAISE CI
  failure of the first design).

Synthetic CArchive executables are built here byte-by-byte (cookie +
TOC + zlib blob) so read_carchive_entry is exercised against the real
PyInstaller layout without shipping a real 100 MB executable in the repo.
"""
import hashlib
import importlib.util
import struct
import sys
import zlib
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
sys.path.insert(0, str(SCRIPTS))

cal = importlib.import_module("_avx2_calibration")
gate = importlib.import_module("check_avx2_gate")

BASELINE_SPEC = importlib.util.spec_from_file_location(
    "baseline", SCRIPTS / "check_baseline_libpython.py"
)
baseline = importlib.util.module_from_spec(BASELINE_SPEC)
BASELINE_SPEC.loader.exec_module(baseline)


# ---------- calibration table ----------

def test_thresholds_are_calibrated_to_the_measured_baseline():
    # measured on the pinned artifact: 60 / 16 / 0
    assert cal.threshold("vinserti128") == 180
    assert cal.threshold("vperm2i128") == 48
    # never fewer than 25 even when the baseline measured 0
    assert cal.threshold("vpgatherdd") == 25


def test_pinned_libpython_hash_is_a_well_formed_sha256():
    assert len(cal.PINNED_LIBPYTHON_SHA256) == 64
    int(cal.PINNED_LIBPYTHON_SHA256, 16)  # must be hex


# ---------- synthetic CArchive executables ----------

def build_carchive_exe(path, entries):
    """Write a minimal PyInstaller onefile layout: [blob][toc][cookie].

    entries: list of (name, raw_bytes, compressed: bool)
    """
    toc = b""
    blob = b""
    for name, raw, compressed in entries:
        data = zlib.compress(raw) if compressed else raw
        entry_offset = len(blob)
        payload = struct.pack(
            gate.TOC_ENTRY_FORMAT,
            gate.TOC_ENTRY_LENGTH + len(name) + 1,
            entry_offset,
            len(data),
            len(raw),
            1 if compressed else 0,
            b"b",
        ) + name.encode() + b"\x00"
        toc += payload
        blob += data
    cookie = struct.pack(
        gate.COOKIE_FORMAT,
        gate.COOKIE_MAGIC,
        len(blob) + len(toc) + gate.COOKIE_LENGTH,
        len(blob),  # toc_offset
        len(toc),  # toc_length
        312,
        b"python3.12.so" + b"\x00" * 50,
    )
    path.write_bytes(blob + toc + cookie)


def libpython_entry(payload, compressed=True):
    return ("libpython3.12.so", payload, compressed)


# ---------- gate: verdicts ----------

def test_gate_passes_on_pinned_hash_custody(tmp_path, monkeypatch):
    payload = b"exact bytes of the pinned artifact"
    monkeypatch.setattr(cal, "PINNED_LIBPYTHON_SHA256", hashlib.sha256(payload).hexdigest())
    monkeypatch.setattr(
        gate, "disassemble",
        lambda p: b"   vmovdqu xmm0, [rsi]\n   vzeroupper\n",
    )
    exe = tmp_path / "backend"
    build_carchive_exe(exe, [libpython_entry(payload)])
    gate.check_executable(str(exe))  # must not raise


def test_gate_fails_when_spread_exceeds_the_calibrated_envelope(tmp_path, monkeypatch):
    payload = b"rebuilt with a v3 toolchain"
    # hash miss AND spread over the limit: the v3-shaped body
    lines = "   vinserti128 ymm0, ymm1, ymm2, 0x0\n" * 181
    monkeypatch.setattr(cal, "PINNED_LIBPYTHON_SHA256", hashlib.sha256(payload).hexdigest())
    monkeypatch.setattr(gate, "disassemble", lambda p: lines.encode())
    exe = tmp_path / "backend"
    build_carchive_exe(exe, [libpython_entry(payload)])
    with pytest.raises(RuntimeError, match="exceeds the calibrated baseline envelope"):
        gate.check_executable(str(exe))


def test_gate_hash_miss_with_in_envelope_spread_warns_but_does_not_fail(tmp_path, monkeypatch, capsys):
    # arbitrary bytes: cannot match the REAL pinned constant, so the
    # warning path fires without patching the calibration table
    payload = b"tarball updated, calibration not"
    lines = "   vinserti128 ymm0, ymm1, ymm2, 0x0\n" * 76
    monkeypatch.setattr(gate, "disassemble", lambda p: lines.encode())
    exe = tmp_path / "backend"
    build_carchive_exe(exe, [libpython_entry(payload)])
    gate.check_executable(str(exe))  # warning, not a failure
    err = capsys.readouterr().err
    assert "NOT byte-identical" in err
    assert "::warning::" in err


def test_gate_counts_whole_words_only(tmp_path, monkeypatch):
    # a substring hit must not count: "vinserti128something" is not the mnemonic
    dis = b"  vinserti128suffix 0\n  vinserti128 ymm0, ymm1, ymm2, 0x0\n"
    counts = gate.mnemonic_counts(dis)
    assert counts["vinserti128"] == 1


def test_gate_main_fails_when_no_input_exists(capsys):
    assert gate.main(["/nope/missing"]) == 1


def test_gate_main_reports_every_failing_executable(tmp_path, monkeypatch):
    payload = b"x"
    lines = "   vpgatherdd ymm0, [ymm1+ymm2], ymm3\n" * 26
    monkeypatch.setattr(cal, "PINNED_LIBPYTHON_SHA256", hashlib.sha256(payload).hexdigest())
    monkeypatch.setattr(gate, "disassemble", lambda p: lines.encode())
    exes = []
    for name in ("backend", "yaah-server"):
        exe = tmp_path / name
        build_carchive_exe(exe, [libpython_entry(payload)])
        exes.append(str(exe))
    assert gate.main(exes) == 1  # both listed paths exist, both fail


def test_gate_without_libpython_entry_fails(tmp_path):
    exe = tmp_path / "backend"
    build_carchive_exe(exe, [("other.so", b"not libpython", True)])
    with pytest.raises(RuntimeError, match="no libpython3"):
        gate.check_executable(str(exe))


# ---------- baseline self-check ----------

def _patch_happy_path(monkeypatch, dis=b"   nop\n"):
    monkeypatch.setattr(baseline, "libpython_candidates", lambda: ["/fake/libpython3.12.so"])
    monkeypatch.setattr(baseline.cal, "sha256_file", lambda p: cal.PINNED_LIBPYTHON_SHA256)
    monkeypatch.setattr(baseline, "_disassemble", lambda p: dis)
    monkeypatch.setattr(baseline.platform, "machine", lambda: "x86_64")
    # host-independent accept path: pretend to BE the pinned interpreter
    from collections import namedtuple

    VI = namedtuple("version_info", ["major", "minor", "micro", "releaselevel", "serial"])
    monkeypatch.setattr(baseline.sys, "version_info", VI(3, 12, 16, "final", 0))


def test_baseline_rejects_unpinned_under_ci_conditions(monkeypatch):
    # The exact CI failure of the first design: env exported at job level,
    # test clears it at runtime. Call-time read must still refuse.
    monkeypatch.setenv("PBS_VERSION", "3.12.16")  # CI's export
    _patch_happy_path(monkeypatch)
    monkeypatch.setenv("PBS_VERSION", "")  # the test's runtime monkeypatch
    with pytest.raises(SystemExit):
        baseline.main()


def test_baseline_accepts_the_pinned_artifact_shape(monkeypatch, capsys):
    monkeypatch.setenv("PBS_VERSION", "3.12.16")
    # in-envelope spread: exactly the measured baseline numbers
    dis = (
        "   vinserti128 ymm0, ymm1, ymm2, 0x0\n" * 60
        + "   vperm2i128 ymm0, ymm1, ymm2, 0x0\n" * 16
    )
    _patch_happy_path(monkeypatch, dis.encode())
    baseline.main()  # must not raise
    out = capsys.readouterr().out
    assert "chain of custody OK" in out
    assert "within baseline envelope" in out


def test_baseline_rejects_a_v3_shaped_body(monkeypatch):
    monkeypatch.setenv("PBS_VERSION", "3.12.16")
    dis = "   vinserti128 ymm0, ymm1, ymm2, 0x0\n" * 488
    _patch_happy_path(monkeypatch, dis.encode())
    with pytest.raises(SystemExit):
        baseline.main()


def test_baseline_rejects_hash_mismatch(monkeypatch):
    monkeypatch.setenv("PBS_VERSION", "3.12.16")
    _patch_happy_path(monkeypatch)
    monkeypatch.setattr(baseline.cal, "sha256_file", lambda p: "0" * 64)
    with pytest.raises(SystemExit):
        baseline.main()


def test_baseline_rejects_flavor_build(monkeypatch):
    monkeypatch.setenv("PBS_VERSION", "3.12.16")
    _patch_happy_path(monkeypatch)
    monkeypatch.setattr(
        baseline.sys, "_base_executable", "/x86_64_v3-unknown-linux-gnu/install/python",
        raising=False,
    )
    with pytest.raises(SystemExit):
        baseline.main()


def test_baseline_rejects_version_drift(monkeypatch):
    monkeypatch.setenv("PBS_VERSION", "3.12.16")
    _patch_happy_path(monkeypatch)
    from collections import namedtuple

    VI = namedtuple("version_info", ["major", "minor", "micro", "releaselevel", "serial"])
    monkeypatch.setattr(baseline.sys, "version_info", VI(3, 12, 15, "final", 0))
    with pytest.raises(SystemExit):
        baseline.main()


def test_baseline_missing_libpython_fails(monkeypatch):
    monkeypatch.setenv("PBS_VERSION", "3.12.16")
    monkeypatch.setattr(baseline, "libpython_candidates", lambda: [])
    with pytest.raises(SystemExit):
        baseline.main()


def test_baseline_counts_whole_words_only(monkeypatch):
    dis = b"  vinserti128suffix 0\n  vinserti128 ymm0, ymm1, ymm2, 0x0\n"
    assert baseline.count_mnemonics(dis, "vinserti128") == 1
