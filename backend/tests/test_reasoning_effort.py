"""Reasoning effort payload behavior for provider-advertised values."""
from backend.agent.model_client import _build_payload


def _cfg(effort=None):
    cfg = {
        "model": "m",
        "api_base": "http://x",
        "api_key": "",
        "providers": {"p": {}},
    }
    if effort is not None:
        cfg["reasoning_effort"] = effort
    return cfg


def test_model_generation_controls_are_left_to_provider_defaults():
    payload = _build_payload(_cfg(), None, False)
    assert "temperature" not in payload
    assert "max_tokens" not in payload

    # Legacy values can remain in an existing config file, but are ignored.
    legacy_cfg = {**_cfg(), "temperature": 0.9, "max_tokens": 100}
    payload = _build_payload(legacy_cfg, None, False)
    assert "temperature" not in payload
    assert "max_tokens" not in payload


def test_default_sends_no_param():
    assert "reasoning_effort" not in _build_payload(_cfg(), None, False)
    assert "reasoning_effort" not in _build_payload(_cfg(""), None, False)


def test_provider_advertised_levels_send_the_param():
    for level in ("low", "medium", "high", "max"):
        payload = _build_payload(_cfg(level), None, False)
        assert payload["reasoning_effort"] == level


def test_effort_value_normalized_for_provider():
    assert _build_payload(_cfg(" Max "), None, False)["reasoning_effort"] == "max"


def test_unbounded_values_are_not_forwarded():
    assert "reasoning_effort" not in _build_payload(_cfg("x" * 65), None, False)
