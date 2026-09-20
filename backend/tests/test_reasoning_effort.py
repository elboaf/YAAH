"""Reasoning effort (#6): the payload knob in model_client._build_payload.

"" (Default) must not send the param at all — providers that hard-reject
unknown fields stay unaffected until the user opts in. low/medium/high send
reasoning_effort; anything else (garbage, wrong case, whitespace) is treated
as unset rather than forwarded.
"""
from backend.agent.model_client import _build_payload


def _cfg(effort=None):
    cfg = {
        "model": "m",
        "temperature": 0.2,
        "max_tokens": 0,
        "api_base": "http://x",
        "api_key": "",
        "providers": {"p": {}},
    }
    if effort is not None:
        cfg["reasoning_effort"] = effort
    return cfg


def test_default_sends_no_param():
    assert "reasoning_effort" not in _build_payload(_cfg(), None, False)
    assert "reasoning_effort" not in _build_payload(_cfg(""), None, False)


def test_levels_send_the_param():
    for level in ("low", "medium", "high"):
        payload = _build_payload(_cfg(level), None, False)
        assert payload["reasoning_effort"] == level


def test_case_and_whitespace_normalized():
    assert _build_payload(_cfg(" High "), None, False)["reasoning_effort"] == "high"
    assert _build_payload(_cfg("MEDIUM"), None, False)["reasoning_effort"] == "medium"


def test_unknown_values_treated_as_unset():
    for bad in ("max", "ultra", "0", "none"):
        assert "reasoning_effort" not in _build_payload(_cfg(bad), None, False)
