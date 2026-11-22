"""Issue #799: provider-block sampling knobs reach the wire.

The ninfer engine resolves omitted sampling fields to the model-registered
defaults, and its answer (post-thinking) phase inherits the thinking-phase
sampler unless the request carries the ``post_thinking`` object. A provider
block may therefore set a block-level ``temperature`` (number) and/or a
``post_thinking`` object; _build_payload forwards them verbatim for THAT
provider's calls and keeps today's documented no-send behavior everywhere
else. Knobs are read only from the resolved call's provider block — never
from top-level config (legacy values stay ignored, #411).
"""
from backend.agent.model_client import _build_payload


def _cfg(providers=None, provider="ninfer"):
    return {
        "model": "m",
        "api_base": "http://x",
        "api_key": "",
        "active_provider": provider,
        "providers": providers
        if providers is not None
        else {
            "ninfer": {
                "api_base": "http://192.168.1.170:8084/v1",
                "model": "qwen3.8-27b-turbofcfusion-mtp-q4km-vision",
                "post_thinking": {"temperature": 0.2},
            },
            "z.ai": {"api_base": "https://api.z.ai/api/v1", "model": "glm"},
        },
    }


def test_provider_block_post_thinking_object_is_sent():
    payload = _build_payload(_cfg(), None, False)
    assert payload["post_thinking"] == {"temperature": 0.2}


def test_provider_block_temperature_is_sent_for_that_provider_only():
    cfg = _cfg(
        providers={
            "ninfer": {
                "api_base": "http://192.168.1.170:8084/v1",
                "model": "m",
                "temperature": 0.7,
            },
            "z.ai": {"api_base": "https://api.z.ai/api/v1", "model": "glm"},
        }
    )
    assert _build_payload(cfg, None, False)["temperature"] == 0.7
    # The other provider on the same config keeps the no-send behavior.
    other = _build_payload(_cfg(cfg["providers"], provider="z.ai"), None, False)
    assert "temperature" not in other
    assert "post_thinking" not in other


def test_inactive_blocks_do_not_leak_into_the_payload():
    cfg = _cfg()  # z.ai block carries no knobs anyway; swap its block
    cfg["providers"]["z.ai"]["temperature"] = 0.9
    assert "temperature" not in _build_payload(cfg, None, False)


def test_default_providers_send_nothing():
    cfg = _cfg(providers={"ninfer": {"api_base": "http://x", "model": "m"}})
    payload = _build_payload(cfg, None, False)
    assert "temperature" not in payload
    assert "post_thinking" not in payload


def test_top_level_knobs_stay_ignored():
    # #411 contract, extended by #799: only the provider block's values
    # ride; block wins even when top-level config carries different ones.
    cfg = _cfg()
    cfg["temperature"] = 0.9
    cfg["post_thinking"] = {"temperature": 0.1}
    payload = _build_payload(cfg, None, False)
    assert "temperature" not in payload
    assert payload["post_thinking"] == {"temperature": 0.2}


def test_no_block_resolves_to_no_send():
    cfg = _cfg()
    cfg["active_provider"] = "missing"
    payload = _build_payload(cfg, None, False)
    assert "temperature" not in payload
    assert "post_thinking" not in payload


def test_per_chat_override_targets_the_override_provider():
    # chat() resolves a provider::model pin by swapping active_provider to
    # the targeted provider (_resolve_call_cfg), so the RESOLVED view of a
    # chat pinned to ninfer while z.ai is the ambient default is: active
    # ninfer. Its block's knob rides; the ambient provider's (lack of) knobs
    # must not leak in either direction.
    pinned = _cfg()
    pinned["active_provider"] = "ninfer"
    assert _build_payload(pinned, None, False)["post_thinking"] == {
        "temperature": 0.2
    }
    ambient = _cfg()
    ambient["active_provider"] = "z.ai"
    payload = _build_payload(ambient, None, False)
    assert "post_thinking" not in payload
    assert "temperature" not in payload


def test_streaming_payloads_carry_the_knobs_too():
    payload = _build_payload(_cfg(), None, True)
    assert payload["post_thinking"] == {"temperature": 0.2}
