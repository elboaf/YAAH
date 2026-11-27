"""Auto-compaction policy for long sessions (issue #852).

The policy is deliberately pure: it takes a conversation's *measured*
prompt size and the model's resolved context window and answers the four
questions the caller must act on:

1. How large is the *effective* input budget? The reply needs room, so the
   effective input window is the model window minus the output reserve and a
   safety margin -- not the full window.
2. Is the conversation over budget? Compaction fires against the measured
   `usage.prompt_tokens` (exact, provider-reported), not a guess.
3. Is the summarizer healthy? A circuit breaker opens after a few
   consecutive failures so a broken summarizer stops looping forever.
4. How far back may the cut go? The keep budget is a fraction of the
   effective trigger, and an absolute trigger must be able to fold down to
   its own keep (the ADR-0004 #588 fault C).

The policy never touches the database or the model. The model-call loop
(`loop.py`) owns the side effects: reading `context_tokens`, running the
summary call, writing the summary + watermark, and emitting the status chip.
This module only decides.

Design notes map directly onto the six ADR-0020 failure modes (the spec's
"ADR-0020 -> zcode mitigation map"):

- *Trigger misses under per-model context settings* -> the trigger is
  computed from the *effective* window using exact measured tokens, and it
  moves with the resolved model each turn (the caller resolves the window
  fresh and passes it in).
- *Summarizer cancellations and churn* -> a single non-streaming summary
  request; the circuit breaker stops repeated attempts after a few
  consecutive failures instead of churning.
- *Regressions in the cut/keep math* -> keep budget is a fraction of the
  effective trigger with the invariant that an absolute trigger must fold
  down to its own keep; the cut is always at a message boundary (rounds).
- *General robustness of the summary* -> graceful degradation is the
  caller's job (retry with progressive trimming); the policy just says how
  far the cut may move.
"""
from __future__ import annotations

from dataclasses import dataclass, field


# Default policy knobs. The caller may override any of these from
# per-model Settings; the defaults reserve a wide output budget and fire
# compaction well before the hard ceiling.
DEFAULT_OUTPUT_RESERVE_TOKENS = 4_096
DEFAULT_OUTPUT_SAFETY_MARGIN_TOKENS = 512
DEFAULT_BUFFER_TOKENS = 4_096
DEFAULT_MIN_FAILURES_TO_OPEN = 3
DEFAULT_KEEP_FRACTION = 0.6


@dataclass(frozen=True)
class CompactionPolicy:
    """The tunable knobs of the compaction policy.

    All token counts are ints; a non-positive value disables the
    corresponding part of the budget.
    """

    output_reserve_tokens: int = DEFAULT_OUTPUT_RESERVE_TOKENS
    output_safety_margin_tokens: int = DEFAULT_OUTPUT_SAFETY_MARGIN_TOKENS
    buffer_tokens: int = DEFAULT_BUFFER_TOKENS
    min_failures_to_open: int = DEFAULT_MIN_FAILURES_TO_OPEN
    keep_fraction: float = DEFAULT_KEEP_FRACTION

    @classmethod
    def defaults(cls) -> "CompactionPolicy":
        return cls()


def effective_context_window(
    window: int,
    output_reserve: int = DEFAULT_OUTPUT_RESERVE_TOKENS,
    output_safety_margin: int = DEFAULT_OUTPUT_SAFETY_MARGIN_TOKENS,
) -> int:
    """The real input budget for the prompt.

    The reply must have room, so the effective input window is the model
    window minus the output reserve and the safety margin. Never negative:
    a tiny window with a large reserve still leaves a non-empty budget.
    """
    if window <= 0:
        return 0
    reserved = max(0, output_reserve) + max(0, output_safety_margin)
    return max(0, window - reserved)


def compaction_trigger(
    effective_window: int,
    buffer_tokens: int = DEFAULT_BUFFER_TOKENS,
) -> int:
    """The token count above which compaction fires.

    Fires against the measured prompt size minus a buffer, so compaction
    engages before the reply is generated and the reply still fits. Never
    negative.
    """
    if effective_window <= 0:
        return 0
    return max(0, effective_window - max(0, buffer_tokens))


def should_compact(prompt_tokens: int, trigger: int) -> bool:
    """True when the measured prompt size exceeds the trigger."""
    return prompt_tokens > trigger


def keep_budget(
    trigger: int,
    keep_fraction: float = DEFAULT_KEEP_FRACTION,
    effective_window: int | None = None,
) -> int:
    """How far back the cut may move: the number of tokens worth of the
    most recent history to preserve verbatim.

    Expressed as a fraction of the trigger. The load-bearing invariant
    (ADR-0004 #588 fault C): an absolute trigger must be able to fold down
    to its own keep. If the trigger is smaller than the keep fraction would
    allow, clamp the keep so it never exceeds the trigger -- otherwise the
    "preserve verbatim" window would be larger than the whole prompt the
    compaction is meant to shrink.
    """
    if trigger <= 0:
        return 0
    if not (0.0 < keep_fraction <= 1.0):
        keep_fraction = DEFAULT_KEEP_FRACTION
    keep = int(trigger * keep_fraction)
    if effective_window is not None:
        # The keep can never exceed the effective window: there is nothing
        # newer to preserve beyond the whole budget.
        keep = min(keep, max(0, effective_window))
    return min(keep, trigger)


def max_output_tokens(
    effective_window: int,
    baseline_max_output: int,
    buffer_tokens: int = DEFAULT_BUFFER_TOKENS,
) -> int:
    """Cap the reply so it always fits after the compaction budget.

    `min(baseline_max_output, effective - buffer)`. The reply is bounded so
    the request cannot overflow just because the reply is long.
    """
    if effective_window <= 0:
        return 0
    return min(baseline_max_output, max(0, effective_window - max(0, buffer_tokens)))


@dataclass
class CircuitBreaker:
    """A simple open/close breaker for the summarizer.

    Opens after `min_failures_to_open` consecutive failures; a single
    success closes it again. While open, auto-compaction must not run so a
    broken summarizer cannot wedge the session.
    """

    min_failures_to_open: int = DEFAULT_MIN_FAILURES_TO_OPEN
    _consecutive_failures: int = field(default=0, init=False)

    def __post_init__(self) -> None:
        if self.min_failures_to_open < 1:
            self.min_failures_to_open = DEFAULT_MIN_FAILURES_TO_OPEN
        self._consecutive_failures = 0

    @property
    def consecutive_failures(self) -> int:
        return self._consecutive_failures

    @property
    def is_open(self) -> bool:
        return self._consecutive_failures >= self.min_failures_to_open

    def record_failure(self) -> None:
        self._consecutive_failures += 1

    def record_success(self) -> None:
        self._consecutive_failures = 0
