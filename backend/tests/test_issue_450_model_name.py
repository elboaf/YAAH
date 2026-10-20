"""#450: the loaded checkpoint rides every image-gen result note.

The server's capabilities `model` field (stem > name sans suffix) is
surfaced verbatim in the generate_image / upscale_image result note -
with a coarse prompting cue when the stem matches a known family
(klein/flux1/flux2/sdxl/sd), the bare name otherwise. Absent on
caps-less servers: the note simply never appears (legacy behavior
unchanged). No extra network traffic on the happy path: the name is
plucked from the SAME /sdcpp/v1/capabilities call the harness already
caches.
"""
import asyncio
import base64

import httpx
import pytest

from backend.agent import config as cfgmod
from backend.agent import tools
from backend.tests.test_image_generation import _FakeClient, _png


@pytest.fixture
def configured(tmp_path, monkeypatch):
    """Isolated config + reset fakes - same contract as the parent
    module's fixture, defined here so the fixture name never
    collides with the parameter name in test signatures."""
    monkeypatch.setattr(cfgmod, "CONFIG_PATH", tmp_path / "config.json")
    _FakeClient.reset()
    tools._CAPS_CACHE.clear()
    cfgmod.save_config(
        {"image_generation": {"provider": "sdcpp", "api_base": "http://127.0.0.1:1234"}}
    )
    return cfgmod.load_config()


def _caps(model=None):
    caps = {
        "limits": {"min_width": 64, "min_height": 64, "max_width": 4096, "max_height": 4096},
        "defaults": {"width": 512, "height": 512},
        "upscale": True,
        "features_by_mode": {"img_gen": {"hires": True, "vae_tiling": True}},
    }
    if model is not None:
        caps["model"] = model
    return caps


# ------------------------------------------------- _loaded_model_name (AC2)


def test_model_name_prefers_stem():
    """stem > name: the file's own short name wins, verbatim."""
    caps = {"model": {"name": "flux-2-klein-9b-Q6_K.gguf", "stem": "flux-2-klein-9b-Q6_K"}}
    assert tools._loaded_model_name(caps) == "flux-2-klein-9b-Q6_K"


def test_model_name_falls_back_to_name_sans_suffix():
    """A name-only payload still yields a usable label (sans suffix)."""
    assert tools._loaded_model_name({"model": {"name": "sdxl-0.9.safetensors"}}) == "sdxl-0.9"


def test_model_name_absent_degrades_to_none():
    """Older builds omit `model`: None, exactly like the other fields."""
    assert tools._loaded_model_name({}) is None
    assert tools._loaded_model_name({"model": None}) is None
    assert tools._loaded_model_name({"model": "not-a-dict"}) is None
    assert tools._loaded_model_name({"model": {}}) is None


# ------------------------------------------------------- _model_note (AC3)


def test_model_note_klein_family():
    """The specific klein stem wins and names its discipline."""
    n = tools._model_note("flux-2-klein-9b-Q6_K")
    assert n == (
        "server running: flux-2-klein-9b-Q6_K - this is a flux2-klein "
        "checkpoint: guidance-distilled (cfg locked at 1), 4 steps is the "
        "design point, negative prompts are ignored - drive the prompt, "
        "not the knobs"
    )


def test_model_note_sdxl_family():
    n = tools._model_note("sdxl-turbo")
    assert n.startswith("server running: sdxl-turbo - ")
    assert "SDXL" in n and "negative prompts matter" in n


def test_model_note_flux1_family():
    n = tools._model_note("flux1-dev")
    assert n.startswith("server running: flux1-dev - ")
    assert "flux1" in n


def test_model_note_unknown_stem_bare_name():
    """Unknown stems are surfaced as-is, no family claim."""
    assert tools._model_note("qwen-image-2") == "server running: qwen-image-2"


def test_model_note_none_is_none():
    assert tools._model_note(None) is None
    assert tools._model_note("") is None


# --------------------------------------------------- generate_image (AC1)


def test_generate_image_note_carries_model_name(configured, tmp_path, monkeypatch):
    """Happy path: the loaded model rides the result note, no extra
    request (the name comes from the cached capabilities call)."""
    _FakeClient.caps_payload = _caps(
        {"name": "flux-2-klein-9b-Q6_K.gguf", "stem": "flux-2-klein-9b-Q6_K"}
    )
    monkeypatch.setattr(httpx, "AsyncClient", _FakeClient)
    result = asyncio.run(
        tools.execute_tool("generate_image", {"prompt": "x"}, workspace=str(tmp_path))
    )
    assert not result.get("error"), result
    note = result.get("note", "")
    assert "server running: flux-2-klein-9b-Q6_K" in note
    assert "flux2-klein" in note  # family cue
    # AC1: no extra request - exactly one capabilities poll
    assert _FakeClient.caps_calls == 1


def test_generate_image_note_degrades_without_model(configured, tmp_path, monkeypatch):
    """Server omits `model`: no note line, tool still succeeds (AC4)."""
    _FakeClient.caps_payload = _caps()
    monkeypatch.setattr(httpx, "AsyncClient", _FakeClient)
    result = asyncio.run(
        tools.execute_tool("generate_image", {"prompt": "x"}, workspace=str(tmp_path))
    )
    assert not result.get("error"), result
    assert "server running" not in result.get("note", "")


def test_generate_image_capsless_legacy_has_no_model_note(configured, tmp_path, monkeypatch):
    """Caps-less legacy passthrough: the note simply stays absent (AC4)."""
    # caps_payload empty -> capabilities returns {} -> b is None
    _FakeClient.caps_payload = {}
    monkeypatch.setattr(httpx, "AsyncClient", _FakeClient)
    result = asyncio.run(
        tools.execute_tool("generate_image", {"prompt": "x"}, workspace=str(tmp_path))
    )
    assert not result.get("error"), result
    assert "server running" not in result.get("note", "")


# ------------------------------------------------------- upscale_image (AC1)


def test_upscale_image_single_repeat_note_carries_model(configured, tmp_path, monkeypatch):
    _FakeClient.caps_payload = _caps({"stem": "sdxl-turbo"})
    _FakeClient.upscale_payload = {
        "images": [{"b64_json": base64.b64encode(_png(128, 128)).decode()}],
        "upscaler": "realesr-general", "scale": 2, "repeats": 1,
        "width": 128, "height": 128,
    }
    monkeypatch.setattr(httpx, "AsyncClient", _FakeClient)
    ws = tmp_path / "ws"
    (ws / "ai-images").mkdir(parents=True)
    (ws / "ai-images" / "src.png").write_bytes(_png(64, 64))
    result = asyncio.run(
        tools.execute_tool(
            "upscale_image", {"path": "ai-images/src.png", "repeats": 1},
            workspace=str(ws),
        )
    )
    assert not result.get("error"), result
    note = result.get("note", "")
    assert "server running: sdxl-turbo" in note
    assert "SDXL" in note


def test_upscale_image_capsless_has_no_model_note(configured, tmp_path, monkeypatch):
    _FakeClient.caps_payload = {}
    _FakeClient.upscale_payload = {
        "images": [{"b64_json": base64.b64encode(_png(128, 128)).decode()}],
        "upscaler": "realesr-general", "scale": 2, "repeats": 1,
        "width": 128, "height": 128,
    }
    monkeypatch.setattr(httpx, "AsyncClient", _FakeClient)
    ws = tmp_path / "ws"
    (ws / "ai-images").mkdir(parents=True)
    (ws / "ai-images" / "src.png").write_bytes(_png(64, 64))
    result = asyncio.run(
        tools.execute_tool(
            "upscale_image", {"path": "ai-images/src.png", "repeats": 1},
            workspace=str(ws),
        )
    )
    assert not result.get("error"), result
    assert "server running" not in result.get("note", "")
