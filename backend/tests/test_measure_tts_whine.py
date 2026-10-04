"""#298 measurement harness: the int8 iSTFT whine at sr/5 and 2*sr/5.

The real engine is not exercised here (no model in CI); these tests build
synthetic PCM (sine + noise floor, pure noise, a rail-pinned chunk) and
unit-test only the harness's pure DSP and reporting parts, mirroring how
test_speak.py stubs the model dir instead of loading kokoro.
"""
import importlib.util
import json
import statistics
from pathlib import Path

import numpy as np
import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "measure_tts_whine.py"
SR = 24000


def _load():
    """Import the harness fresh. Tests patch agent.speak.get_engine to raise,
    so a harness that touched the engine at import time would explode here —
    the script must stay side-effect-free until run()."""
    spec = importlib.util.spec_from_file_location("measure_tts_whine", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def harness(monkeypatch):
    """Load the harness with agent.speak booby-trapped on BOTH import paths
    (tests use backend.agent, the script uses agent): importing/exec_module
    must never touch the engine, and any test that accidentally triggers
    synthesis explodes instead of silently no-op'ing on a missing model."""
    import sys
    import types

    from backend.agent import speak

    def _boom(*a, **k):
        raise AssertionError("get_engine called — the harness touched the engine")

    monkeypatch.setattr(speak, "get_engine", _boom)
    fake_speak = types.ModuleType("agent.speak")
    fake_speak.get_engine = _boom
    fake_speak.synthesize = _boom
    fake_speak.find_model_dir = _boom
    fake_pkg = types.ModuleType("agent")
    fake_pkg.speak = fake_speak
    monkeypatch.setitem(sys.modules, "agent", fake_pkg)
    monkeypatch.setitem(sys.modules, "agent.speak", fake_speak)
    return _load()


# ---- synthetic PCM builders -------------------------------------------------

def _int16(x: np.ndarray) -> np.ndarray:
    return np.round(np.clip(x, -1.0, 1.0) * 32767).astype(np.int16)


def _whine_pcm(seconds=1.0, amp=0.05, noise=1e-4, seed=42):
    """The defect signature: steady tones at exactly sr/5 and 2*sr/5 over a
    low noise floor (issue #298: +22..+32 dB over floor on int8)."""
    rng = np.random.default_rng(seed)
    t = np.arange(int(seconds * SR)) / SR
    x = (
        amp * np.sin(2 * np.pi * (SR / 5) * t)
        + amp / 2.5 * np.sin(2 * np.pi * (2 * SR / 5) * t)
        + noise * rng.standard_normal(t.size)
    )
    return _int16(x)


def _clean_pcm(seconds=1.0, seed=7):
    """Speech-like: harmonic stack + noise, nothing parked at sr/5."""
    rng = np.random.default_rng(seed)
    t = np.arange(int(seconds * SR)) / SR
    x = sum(
        0.02 / (k + 1) * np.sin(2 * np.pi * 150 * (k + 1) * t) for k in range(20)
    ) + 1e-4 * rng.standard_normal(t.size)
    return _int16(x)


# ---- analyze(): the DSP the decision rule reads ----

def test_analyze_finds_whine_tones_well_over_floor(harness):
    t5, t10, floor, over5, over10, *_ = harness.analyze(_whine_pcm(), SR)
    assert over5 > 20.0, f"sr/5 tone should tower over the floor, got +{over5:.1f} dB"
    assert over10 > 20.0, f"2*sr/5 tone should tower over the floor, got +{over10:.1f} dB"
    assert -180 < floor < -60  # a finite dBFS floor, not a blown-up number
    assert t5 > floor and t10 > floor


def test_analyze_peaks_land_on_exact_tone_frequencies(harness):
    """The band is ±30 Hz; the reported peak must sit AT sr/5 and 2*sr/5,
    not at some neighbor bin — that is what pins the tone to the iSTFT."""
    x = _whine_pcm().astype(np.float64) / 32767.0
    spec = np.abs(np.fft.rfft(x * np.hanning(x.size)))
    freqs = np.fft.rfftfreq(x.size, 1.0 / SR)
    for target in (SR / 5, 2 * SR / 5):
        band = (freqs > target - 30) & (freqs < target + 30)
        assert freqs[band][np.argmax(spec[band])] == pytest.approx(target, abs=1.0)


def test_analyze_pure_noise_stays_far_below_the_whine_signature(harness):
    """Band-max vs p25 floor is a max-vs-percentile statistic, so even tone
    free noise lands a hair over 0 — but nowhere near the +22..+32 dB the
    int8 defect shows. Clean must sit in the noise-statistics band, far
    from the defect band."""
    _, _, _, over5, over10, *_ = harness.analyze(
        _int16(1e-4 * np.random.default_rng(3).standard_normal(SR)), SR
    )
    assert over5 < 18.0 and over10 < 18.0
    assert max(over5, over10) < 20.0  # clear of the measured int8 signature


def test_analyze_speech_without_whine_matches_the_noise_baseline(harness):
    """A harmonic-stack 'voice' with no parked tone must not look whiny:
    its over-floor numbers stay near the pure-noise baseline (both are
    band-max-of-noise statistics), a wide margin under the defect band."""
    noise_over = harness.analyze(
        _int16(1e-4 * np.random.default_rng(3).standard_normal(SR)), SR
    )[3]
    _, _, _, over5, over10, *_ = harness.analyze(_clean_pcm(), SR)
    assert max(over5, over10) < 18.0
    assert abs(over5 - noise_over) < 8.0  # speech adds no spectral line at sr/5


def test_analyze_band_tolerates_slightly_off_tone(harness):
    """A tone 20 Hz off sr/5 still counts (the ±30 Hz band exists because the
    iSTFT line can sit a hair off the exact bin)."""
    rng = np.random.default_rng(5)
    t = np.arange(SR) / SR
    x = 0.05 * np.sin(2 * np.pi * (SR / 5 + 20) * t) + 1e-4 * rng.standard_normal(SR)
    _, _, _, over5, _, *_ = harness.analyze(_int16(x), SR)
    assert over5 > 20.0


def test_analyze_short_chunk_does_not_crash(harness):
    t5, t10, floor, over5, over10, *_ = harness.analyze(
        _whine_pcm(seconds=0.1), SR
    )
    assert all(np.isfinite(v) for v in (t5, t10, floor, over5, over10))


# ---- rail pinning (decision-rule criterion 3) ----

def test_is_rail_pinned_flags_constant_chunks(harness):
    assert harness.is_rail_pinned(np.full(4800, 3276, dtype=np.int16))
    assert harness.is_rail_pinned(np.zeros(10, dtype=np.int16))
    assert not harness.is_rail_pinned(np.arange(10, dtype=np.int16))
    assert not harness.is_rail_pinned(np.zeros(0, dtype=np.int16))  # empty is not pinned


# ---- reporting contract ----

def test_analyze_returns_named_metrics(harness):
    m = harness.analyze(_whine_pcm(), SR)
    for name in (
        "tone_sr5_dbfs", "tone_2sr5_dbfs", "floor_dbfs",
        "over_sr5_db", "over_2sr5_db", "noise_over_db",
    ):
        assert hasattr(m, name), f"metric object missing {name}"
    assert m.sample_rate == SR


def test_build_report_json_has_every_required_field(harness, tmp_path):
    rep = harness.build_report(
        model_dir=r"C:\models\kokoro-int8-multi-lang-v1_0",
        resolved_dir=r"C:\models\kokoro-int8-multi-lang-v1_0",
        threads=4,
        reps=3,
        soak=0,
        utts=[
            {
                "index": 0,
                "text": "Who's there?",
                "duration_s": 0.74,
                "rtf": 1.57,
                "peak_dbfs": -3.2,
                "metrics": harness.analyze(_whine_pcm(), SR),
            }
        ],
        rtf_median=1.57,
        soak_result=None,
    )
    blob = json.dumps(rep)  # must serialize cleanly
    loaded = json.loads(blob)
    for key in (
        "model_dir", "resolved_dir", "threads", "reps", "soak",
        "utterances", "rtf_median",
    ):
        assert key in loaded, f"report missing {key}"
    u = loaded["utterances"][0]
    for key in (
        "index", "text", "duration_s", "rtf", "peak_dbfs",
        "tone_sr5_dbfs", "tone_2sr5_dbfs", "floor_dbfs",
        "over_sr5_db", "over_2sr5_db", "sample_rate",
    ):
        assert key in u, f"utterance record missing {key}"


def test_synth_take_records_median_and_first_take(harness):
    """--reps K: RTF is the median of K takes; audio metrics come from take 1."""
    rtf = harness.median_rtf([1.5, 1.0, 2.0])
    assert rtf == statistics.median([1.5, 1.0, 2.0]) == 1.5
    assert harness.median_rtf([0.9]) == 0.9


# ---- corpus + soak templates (fixed by the issue) ----

def test_corpus_is_exactly_the_issue_sentences(harness):
    assert harness.CORPUS == [
        "Who's there?",
        "Now they knew what had been in the long, thin package he had "
        "brought with them.",
        "Tilde ell five thirty nine. I'll sync the branch, run the build, "
        "then report back with the results.",
    ]


def test_soak_utterances_are_varied_bounded_and_fifty(harness):
    utts = harness.soak_utterances(50)
    assert len(utts) == 50
    assert len(set(utts)) >= 40  # templated variety, not 50 copies
    assert all(len(u) <= 320 for u in utts)
    assert all(u.strip() for u in utts)
