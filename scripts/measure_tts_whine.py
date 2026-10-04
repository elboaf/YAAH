"""Measure the local-TTS whine (#298): steady tones at sr/5 and 2*sr/5.

The int8-quantized kokoro model emits tones at exactly sampleRate/5 and
2*sampleRate/5 (the iSTFT frame rate and its first harmonic; 4.8/9.6 kHz
at 24 kHz), +22..+32 dB above the p25 noise floor. This harness measures
a candidate model pack through the REAL production engine (agent.speak,
unmodified) so the #298 decision rule can be applied from its output:

    adopt the smallest-footprint variant meeting ALL of
    1. whine <= 6 dB over the p25 floor at sr/5 AND 2*sr/5 on every corpus
       utterance,
    2. RTF no worse than the int8 baseline measured in the same run,
    3. a 50-utterance soak with zero rail-pinned (constant-sample) chunks.

Usage (from the repo root):
    YAAH_TTS_MODEL_DIR=<model dir> python scripts/measure_tts_whine.py \
        <out_dir> [--threads N] [--reps K] [--soak N]

Per corpus utterance it writes utt_<i>.wav (16-bit mono) for listening
and one results.json holding every metric: tone dBFS in +/-30 Hz bands
at sr/5 and 2*sr/5, the p25 noise floor, the over-floor deltas, RTF
(median of --reps takes; audio metrics from the first take), peak level,
plus threads, the resolved model dir as printed by speak's
"[tts] kokoro ready in ... (<dir>)" line (cross-checked against
YAAH_TTS_MODEL_DIR) and the soak verdict.

The engine itself is never reconfigured here: the default path is
production-parity code, and the optional --threads sweep is done by this
script alone — a transient patch that forces num_threads on sherpa's
OfflineTtsModelConfig for one engine build. No env plumbing, no edits in
speak.py. Importing this module has no side effects; the tests patch
agent.speak and exercise only the pure DSP/reporting parts.
"""
import argparse
import contextlib
import functools
import io
import json
import os
import re
import statistics
import sys
import time
import wave
from contextlib import contextmanager
from pathlib import Path
from typing import NamedTuple
from unittest import mock

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "backend"))

CORPUS = [
    "Who's there?",
    "Now they knew what had been in the long, thin package he had brought "
    "with them.",
    "Tilde ell five thirty nine. I'll sync the branch, run the build, then "
    "report back with the results.",
]

# The rule's pass line: a tone more than 6 dB over the p25 floor is whine.
WHINE_BUDGET_DB = 6.0
# +/- Hz around each target frequency the FFT peak may sit in (the iSTFT
# line can park a hair off the exact bin; #298's reference used +/-30).
BAND_HZ = 30.0
# Soak utterances must stay short like real narration chunks.
SOAK_MAX_CHARS = 320

# speak.get_engine() announces the dir it loaded; the harness parses the
# same line back out of captured stdout so results.json records what the
# engine ACTUALLY used, not what the env var claims.
_READY_LINE = re.compile(r"\[tts\] kokoro ready in \d+(?:\.\d+)?s \((.+)\)")

_SOAK_NAMES = [
    "Dana", "Viktor", "Mei", "Sol", "Priya", "Owen", "Ade", "Lena",
    "Marcus", "Tove", "Ines", "Kofi",
]
_SOAK_VERBS = [
    "sync the branch", "run the build", "rerun the failing test",
    "trim the changelog", "bump the lockfile", "tag the release",
    "close the ticket", "revert the patch", "stage the hunk",
    "squash the commits", "rebase the branch", "sign the artifact",
]
_SOAK_OBJECTS = [
    "then report back with the results.",
    "and paste the tail of the log.",
    "before the standup starts.",
    "while the sandbox boots.",
    "after the merge window closes.",
    "so the ledger stays honest.",
]


class WhineMetrics(NamedTuple):
    """Spectral verdict for one chunk — the numbers the decision rule reads.

    A NamedTuple so reports can unpack the classic 5-tuple order AND read
    named fields; _asdict() feeds results.json."""

    tone_sr5_dbfs: float  # peak dBFS inside the +/-30 Hz band at sr/5
    tone_2sr5_dbfs: float  # same band around the first harmonic, 2*sr/5
    floor_dbfs: float  # 25th percentile of the spectrum (the noise floor)
    over_sr5_db: float  # tone_sr5_dbfs - floor_dbfs
    over_2sr5_db: float
    sample_rate: int
    noise_over_db: float  # largest off-tone band peak over floor: the
    # metric's noise baseline, for reading borderline over-floor numbers


def analyze(pcm, sample_rate: int) -> WhineMetrics:
    """Tone levels vs noise floor for one 16-bit chunk.

    Hann-windowed rFFT; dBFS normalized by len/2 (sinusoid-peak convention,
    matching #298's reference measurement); floor = 25th percentile of the
    spectrum. The whine is a steady line, so its bin towers over the
    broadband floor while barely moving the percentile.
    """
    x = np.asarray(pcm).astype(np.float64) / 32767.0
    x = x - x.mean()  # DC would leak a hump into every band
    spec = np.abs(np.fft.rfft(x * np.hanning(x.size)))
    db = 20.0 * np.log10(spec / (x.size / 2) + 1e-12)
    freqs = np.fft.rfftfreq(x.size, 1.0 / sample_rate)

    def band_peak(f: float) -> float:
        m = (freqs > f - BAND_HZ) & (freqs < f + BAND_HZ)
        return float(db[m].max()) if m.any() else float("-inf")

    floor = float(np.percentile(db, 25))
    t5 = band_peak(sample_rate / 5)
    t10 = band_peak(2 * sample_rate / 5)
    # Off-tone reference peaks: what band-max-vs-floor reads on ordinary
    # spectrum (max-of-noise-bins statistics, no parked line). Reading the
    # sr/5 numbers against these separates "the defect" from "the metric's
    # noise baseline" when a real measurement lands near the budget.
    refs = [band_peak(f) for f in (sample_rate / 7.3, sample_rate / 3.7, 0.77 * sample_rate / 2)]
    return WhineMetrics(
        t5,
        t10,
        floor,
        t5 - floor,
        t10 - floor,
        sample_rate,
        round(max(refs) - floor, 2),
    )


def is_rail_pinned(pcm) -> bool:
    """True when a chunk's samples are all one value (pinned to a rail):
    the int8 failure mode #298 calls out alongside the steady tone."""
    x = np.asarray(pcm)
    return bool(x.size > 0 and np.std(x.astype(np.float64)) == 0.0)


def median_rtf(rtfs) -> float:
    """--reps K takes: the median RTF (odd K -> the middle take)."""
    return float(statistics.median(rtfs))


def peak_dbfs(pcm) -> float:
    x = np.asarray(pcm).astype(np.float64) / 32767.0
    return float(20.0 * np.log10(np.max(np.abs(x)) + 1e-12))


def soak_utterances(n: int) -> list[str]:
    """n varied narration-shaped utterances (each <= 320 chars) by
    templating the corpus sentences with rotating names/numbers/phrases."""
    out: list[str] = []
    i = 0
    while len(out) < n:
        name = _SOAK_NAMES[i % len(_SOAK_NAMES)]
        verb = _SOAK_VERBS[i % len(_SOAK_VERBS)]
        obj = _SOAK_OBJECTS[(i // len(_SOAK_VERBS)) % len(_SOAK_OBJECTS)]
        num = 100 + i
        shapes = [
            f"Okay {name}, I'll {verb}, {obj}",
            f"{name} left {num} packages in the long, thin box by the door.",
            f"Who's there, {name}? It is package number {num}, queued for delivery.",
            f"Tilde ell five thirty {i % 10}. {name} will {verb}, {obj}",
        ]
        out.append(shapes[i % len(shapes)][:SOAK_MAX_CHARS])
        i += 1
    return out


def synthesize_take(text: str):
    """One production synthesis: (pcm16le bytes, sample_rate, rtf)."""
    from agent import speak

    t0 = time.perf_counter()
    raw, sr = speak.synthesize(text)
    wall = time.perf_counter() - t0
    return raw, sr, wall / (len(raw) / 2 / sr)


def measure_utterance(text: str, index: int, reps: int, out_dir: Path) -> dict:
    """Synthesize one corpus utterance --reps times: median RTF, first-take
    audio. Writes utt_<index>.wav for listening."""
    rtfs = []
    raw = sr = None
    for _ in range(max(1, reps)):
        raw, sr, rtf = synthesize_take(text)
        rtfs.append(rtf)
    pcm = np.frombuffer(raw, dtype=np.int16)
    m = analyze(pcm, sr)
    wav_path = out_dir / f"utt_{index}.wav"
    with wave.open(str(wav_path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(raw)
    return {
        "index": index,
        "text": text,
        "duration_s": round(pcm.size / sr, 3),
        "rtf": round(median_rtf(rtfs), 4),
        "rtf_takes": [round(r, 4) for r in rtfs],
        "peak_dbfs": round(peak_dbfs(pcm), 2),
        "wav": wav_path.name,
        **{k: round(v, 2) if isinstance(v, float) else v for k, v in m._asdict().items()},
    }


def run_soak(n: int) -> dict:
    """Criterion 3: n varied utterances; count rail-pinned chunks."""
    pinned = []
    rtfs = []
    for i, text in enumerate(soak_utterances(n)):
        raw, sr, rtf = synthesize_take(text)
        if is_rail_pinned(np.frombuffer(raw, dtype=np.int16)):
            pinned.append(i)
        rtfs.append(rtf)
    return {
        "chunks": n,
        "pinned_chunks": len(pinned),
        "pinned_indices": pinned,
        "rtf_median": round(median_rtf(rtfs), 4) if rtfs else None,
    }


def rule_verdict(report: dict) -> dict:
    """The #298 decision rule's local criteria, from one report's numbers.
    (Criterion 2 — RTF vs the int8 baseline — compares across reports, so
    the caller reads rtf_median directly.)"""
    worsts = [max(u["over_sr5_db"], u["over_2sr5_db"]) for u in report["utterances"]]
    worst = max(worsts) if worsts else None
    soak = report.get("soak_result")
    return {
        "whine_ok": worst is not None and worst <= WHINE_BUDGET_DB,
        "whine_worst_over_db": None if worst is None else round(worst, 2),
        "soak_ok": bool(soak) and soak["pinned_chunks"] == 0,
        "rtf_median": report.get("rtf_median"),
        "whine_budget_db": WHINE_BUDGET_DB,
    }


def build_report(
    *,
    model_dir: str,
    resolved_dir: str,
    threads: int,
    reps: int,
    soak: int,
    utts,
    rtf_median: float,
    soak_result,
) -> dict:
    """Everything results.json carries, per variant x threads. Utterance
    records flatten their WhineMetrics (the harness may hand them over
    nested under 'metrics')."""
    records = []
    for u in utts:
        rec = dict(u)
        m = rec.pop("metrics", None)
        if m is not None:
            rec.update(m._asdict() if hasattr(m, "_asdict") else dict(m))
        records.append(rec)
    return {
        "model_dir": model_dir,
        "resolved_dir": resolved_dir,
        "threads": threads,
        "reps": reps,
        "soak": soak,
        "utterances": records,
        "rtf_median": rtf_median,
        "soak_result": soak_result,
    }


def resolved_matches_env(resolved_dir, env_value) -> bool:
    """The resolved dir must BE the env-pinned one; a mismatch means the
    wrong pack was measured and every number in the report is suspect."""
    if not env_value:
        return True  # nothing pinned: the default candidates were free to win
    return Path(resolved_dir).resolve() == Path(env_value).resolve()


@contextmanager
def forced_threads(n: int):
    """Run the block with sherpa's OfflineTtsModelConfig forced to
    num_threads=n. Production parity is untouched: speak.py is never
    edited and its _NUM_THREADS constant stays authoritative unless a
    sweep explicitly enters this block. Pair with `speak._engine = None`
    so get_engine() actually rebuilds under the forced config."""
    import sherpa_onnx as sherpa

    def forced_config(cls):
        @functools.wraps(cls, assigned=("__name__", "__qualname__", "__doc__"))
        def factory(*args, **kwargs):
            kwargs["num_threads"] = n
            return cls(*args, **kwargs)

        return factory

    with mock.patch.object(
        sherpa, "OfflineTtsModelConfig", forced_config(sherpa.OfflineTtsModelConfig)
    ):
        yield


@contextmanager
def _capture_ready_line():
    """Capture stdout around an engine build so the harness can parse the
    model dir out of speak's readiness line (the ground truth of what the
    engine actually loaded, not what the env var claims)."""
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        yield buf


def run(argv=None) -> dict:
    """The real measurement run: warm engine, per-utterance takes, optional
    thread sweep and soak; writes results.json into out_dir and returns it."""
    from agent import speak

    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("out_dir")
    ap.add_argument("--threads", type=int, default=0, help="force num_threads (0 = production 4)")
    ap.add_argument("--reps", type=int, default=3, help="takes per utterance; RTF = median")
    ap.add_argument("--soak", type=int, default=0, help="run an N-utterance rail-pin soak")
    args = ap.parse_args(argv)

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    env_dir = os.environ.get("YAAH_TTS_MODEL_DIR")
    model_dir = speak.find_model_dir()

    if args.threads:
        speak._engine = None
        with forced_threads(args.threads), _capture_ready_line() as buf:
            engine = speak.get_engine()
        threads = args.threads
    else:
        with _capture_ready_line() as buf:
            engine = speak.get_engine()
        threads = 4
    if engine is None:
        raise SystemExit("no TTS model resolved — set YAAH_TTS_MODEL_DIR to a model pack")
    matches = _READY_LINE.findall(buf.getvalue())
    if not matches:
        raise SystemExit("engine loaded but no '[tts] kokoro ready in ... (<dir>)' line")
    resolved_dir = matches[-1]

    utts = [measure_utterance(text, i, args.reps, out_dir) for i, text in enumerate(CORPUS)]
    report = build_report(
        model_dir=str(model_dir),
        resolved_dir=resolved_dir,
        threads=threads,
        reps=args.reps,
        soak=args.soak,
        utts=utts,
        rtf_median=round(median_rtf([u["rtf"] for u in utts]), 4),
        soak_result=run_soak(args.soak) if args.soak else None,
    )
    report["rule_verdict"] = rule_verdict(report)
    report["resolved_matches_env"] = resolved_matches_env(resolved_dir, env_dir)
    if not report["resolved_matches_env"]:
        print(
            f"WARNING: resolved model dir {resolved_dir} != YAAH_TTS_MODEL_DIR "
            f"{env_dir} — the wrong pack may have been measured",
            file=sys.stderr,
        )
    (out_dir / "results.json").write_text(json.dumps(report, indent=2))
    print(json.dumps(report, indent=2))
    return report


def main() -> None:
    run()


if __name__ == "__main__":
    main()
