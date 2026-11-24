"""Issue #838: the step-budget error named a non-existent Settings surface.

The old message pointed at "Settings -> General -> Max steps", but Settings has
no General tab and no global Max steps field (only per-model rows in the
Providers editor). The corrected message names only surfaces that exist: the
per-model max steps in Settings -> Providers, and config.json `max_steps`.
"""
from backend.agent.loop import _step_budget_message


def test_budget_message_names_only_real_surfaces():
    msg = _step_budget_message(200)
    # The old, broken pointer is gone.
    assert "General" not in msg
    # It names the per-model Providers surface that actually exists.
    assert "Providers" in msg
    # It names config.json `max_steps` as the escape hatch, with 0 = unlimited.
    assert "max_steps" in msg
    assert "unlimited" in msg


def test_budget_message_carries_the_number():
    assert _step_budget_message(50).startswith("Step budget (50)")
