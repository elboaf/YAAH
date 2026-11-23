"""Provider preset schema (the /api/providers contract the Settings UI
renders as "+ chip" buttons). Every preset must carry the four base keys;
ninfer additionally ships the cooled #799 post-thinking default so
preset-born blocks start sane on machines that never saw a hand-edited
config.json (runtime backstop: model_client.NINFER_DEFAULT_POST_THINKING).
"""
from backend.agent import providers

BASE_KEYS = {"api_base", "model", "needs_api_key", "supports_tools"}


def test_every_preset_carries_the_base_schema():
    for name, preset in providers.PRESETS.items():
        assert BASE_KEYS <= set(preset), f"{name}: missing {BASE_KEYS - set(preset)}"
        assert preset["needs_api_key"] in (True, False)
        assert preset["supports_tools"] in (True, False, None)


def test_ninfer_preset_exists_and_ships_the_cooled_default():
    p = providers.PRESETS["ninfer"]
    assert p["api_base"] == "http://localhost:8084/v1"
    assert p["needs_api_key"] is False
    assert p["post_thinking"] == {"temperature": 0.2}


def test_only_ninfer_carries_sampling_knobs():
    # The knob is engine-specific; other presets must stay knob-free so
    # strict OpenAI-compatible servers never see an unknown field.
    for name, preset in providers.PRESETS.items():
        if name != "ninfer":
            assert "post_thinking" not in preset, name
