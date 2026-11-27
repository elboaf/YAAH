"""Compaction policy (issue #852).

Pure policy unit: the effective-window budget, the measured-token trigger,
the circuit breaker, and the keep-budget invariant. These are the four
decisions the model-call loop acts on; the tests assert the external
behaviour of each decision, not the internals.
"""
import pytest

from backend.agent.compaction import (
    CircuitBreaker,
    CompactionPolicy,
    compaction_trigger,
    effective_context_window,
    keep_budget,
    max_output_tokens,
    should_compact,
)


class TestEffectiveWindow:
    def test_reserves_output_and_margin(self):
        # 128k window, 4k reserve, 512 margin -> 123,488 effective budget.
        assert effective_context_window(128_000) == 128_000 - 4_096 - 512

    def test_non_positive_window_is_zero(self):
        assert effective_context_window(0) == 0
        assert effective_context_window(-100) == 0

    def test_window_smaller_than_reserve_is_clamped(self):
        # A tiny window with a large reserve still leaves a non-empty budget.
        assert effective_context_window(100) == 0

    def test_custom_reserve_and_margin(self):
        assert effective_context_window(10_000, output_reserve=1_000, output_safety_margin=500) == 8_500


class TestTrigger:
    def test_fires_below_effective_window(self):
        eff = effective_context_window(128_000)
        assert compaction_trigger(eff) == eff - 4_096

    def test_non_positive_effective_window(self):
        assert compaction_trigger(0) == 0

    def test_should_compact_boundary(self):
        trigger = 100_000
        assert should_compact(100_001, trigger) is True
        assert should_compact(100_000, trigger) is False
        assert should_compact(99_999, trigger) is False


class TestKeepBudget:
    def test_keep_is_fraction_of_trigger(self):
        # default keep_fraction = 0.6
        assert keep_budget(100_000) == 60_000

    def test_absolute_trigger_folds_to_its_own_keep(self):
        # The load-bearing invariant (ADR-0004 #588 fault C): the keep can
        # never exceed the trigger it is derived from.
        assert keep_budget(100_000) <= 100_000
        assert keep_budget(10) <= 10

    def test_keep_never_exceeds_effective_window(self):
        # A huge keep fraction against a small window clamps to the window.
        assert keep_budget(50_000, keep_fraction=0.99, effective_window=10_000) == 10_000

    def test_non_positive_trigger(self):
        assert keep_budget(0) == 0

    def test_invalid_keep_fraction_falls_back(self):
        # keep_fraction outside (0,1] falls back to the default 0.6.
        assert keep_budget(100_000, keep_fraction=0.0) == 60_000
        assert keep_budget(100_000, keep_fraction=2.0) == 60_000


class TestMaxOutput:
    def test_capped_at_effective_minus_buffer(self):
        eff = effective_context_window(128_000)  # 123,392
        # baseline 120k is larger than effective - buffer (119,296), so it
        # caps to effective - buffer.
        assert max_output_tokens(eff, baseline_max_output=120_000) == eff - 4_096
        # baseline 8k is smaller than effective - buffer, so the baseline wins.
        assert max_output_tokens(eff, baseline_max_output=8_000) == 8_000

    def test_baseline_when_smaller_than_effective(self):
        # baseline 1k is smaller than effective - buffer, so it wins.
        assert max_output_tokens(128_000, baseline_max_output=1_000) == 1_000

    def test_non_positive_effective_window(self):
        assert max_output_tokens(0, baseline_max_output=8_000) == 0


class TestCircuitBreaker:
    def test_opens_after_min_failures(self):
        cb = CircuitBreaker(min_failures_to_open=3)
        assert cb.is_open is False
        cb.record_failure()
        cb.record_failure()
        assert cb.is_open is False
        cb.record_failure()
        assert cb.is_open is True

    def test_success_resets_counter(self):
        cb = CircuitBreaker(min_failures_to_open=2)
        cb.record_failure()
        cb.record_failure()
        assert cb.is_open is True
        cb.record_success()
        assert cb.is_open is False
        assert cb.consecutive_failures == 0

    def test_default_min_failures(self):
        cb = CircuitBreaker()
        assert cb.min_failures_to_open == 3
        for _ in range(2):
            cb.record_failure()
        assert cb.is_open is False

    def test_min_failures_below_one_is_clamped(self):
        cb = CircuitBreaker(min_failures_to_open=0)
        assert cb.min_failures_to_open == 3
        cb.record_success()  # never opens on the first failure

    def test_policy_defaults(self):
        p = CompactionPolicy.defaults()
        assert p.output_reserve_tokens == 4_096
        assert p.output_safety_margin_tokens == 512
        assert p.buffer_tokens == 4_096
        assert p.keep_fraction == 0.6
