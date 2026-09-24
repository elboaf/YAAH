"""Provider model catalogs can advertise per-model reasoning capabilities."""

from backend.agent.providers import _model_info


def test_uses_provider_advertised_effort_values():
    assert _model_info([
        {
            "id": "reasoner",
            "reasoning": {"supported_efforts": ["max", "low", "high", "low"]},
            "supported_parameters": ["reasoning_effort"],
        }
    ]) == [
        {
            "id": "reasoner",
            "reasoning_efforts": ["max", "low", "high"],
            "supports_reasoning": True,
        }
    ]


def test_capability_parameter_without_values_does_not_invent_levels():
    assert _model_info([
        {"id": "generic", "supported_parameters": ["reasoning_effort"]}
    ]) == [
        {"id": "generic", "reasoning_efforts": [], "supports_reasoning": True}
    ]


def test_unknown_or_non_reasoning_model_has_no_efforts():
    assert _model_info([
        {"id": "unknown"},
        {"id": "standard", "supported_parameters": ["temperature"]},
        None,
        {"name": "missing id"},
    ]) == [
        {"id": "unknown", "reasoning_efforts": [], "supports_reasoning": False},
        {"id": "standard", "reasoning_efforts": [], "supports_reasoning": False},
    ]
