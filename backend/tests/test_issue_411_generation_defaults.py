"""#411 (+ #419): model-appropriate generation defaults for generate_image.

Caller > configured generation_defaults > BUILT-IN (1024x1024, steps 4,
cfg 1.0 - #419 retired the omit-to-server-default tail: sampling is
never unset anymore). #433: cfg + distilled guidance are LOCKED at 1
(guidance-distilled klein) and cfg_scale is not a model-facing
parameter; the built-in layer injects distilled_guidance 1.0 whenever
txt_cfg sits at the klein operating point. The seed is NEVER pinned on
the omit path;
reseed=True asks the server for a fresh random draw (seed -1); the
result note carries the ground truth of what the server actually ran,
parsed from the PNG parameters chunk.
"""
import asyncio
import base64
import json
import struct
import zlib

import httpx
import pytest

from backend.agent import config as cfgmod
from backend.agent import tools
from backend.tests.test_image_generation import _FakeClient


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


def _chunk(ctype: bytes, data: bytes) -> bytes:
    return (
        struct.pack(">I", len(data))
        + ctype
        + data
        + struct.pack(">I", zlib.crc32(ctype + data) & 0xFFFFFFFF)
    )


def _png_with_params(w: int, h: int, text: str) -> bytes:
    """A structurally valid PNG with a tEXt parameters chunk (what sd.cpp
    embeds with the parameters it actually received)."""
    ihdr = struct.pack(">IIBBBBB", w, h, 8, 6, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + _chunk(b"IHDR", ihdr)
        + _chunk(b"IDAT", b"\x00\x01\x00\x00")
        + _chunk(b"tEXt", b"parameters\x00" + text.encode("utf-8"))
        + _chunk(b"IEND", b"")
    )


def _caps(defaults=(512, 512)):
    return {
        "limits": {
            "min_width": 64,
            "min_height": 64,
            "max_width": 4096,
            "max_height": 4096,
            "max_upscale_width": 8192,
            "max_upscale_height": 8192,
        },
        "defaults": {"width": defaults[0], "height": defaults[1]},
        "upscale": True,
        "features_by_mode": {"img_gen": {"hires": True, "vae_tiling": True}},
    }


def _gen(cfgfx, tmp_path, monkeypatch, **args):
    _FakeClient.caps_payload = _caps()
    monkeypatch.setattr(httpx, "AsyncClient", _FakeClient)
    args.setdefault("prompt", "a shark on a whale")
    result = asyncio.run(
        tools.execute_tool("generate_image", args, workspace=str(tmp_path))
    )
    return result


def _embedded_extra(legacy_body: dict) -> dict:
    """Parse the <sd_cpp_extra_args> JSON out of a legacy prompt."""
    prompt = legacy_body.get("prompt") or ""
    if "<sd_cpp_extra_args>" not in prompt:
        return {}
    blob = prompt.split("<sd_cpp_extra_args>", 1)[1]
    blob = blob.split("</sd_cpp_extra_args>", 1)[0]
    return json.loads(blob)


# ----------------------------------------------------------------- omit path


def test_full_omit_uses_built_in_defaults(configured, tmp_path, monkeypatch):
    """#419 + #421: a prompt-only call with no config rides the
    BUILT-IN layer - steps 4 + cfg 1.0 in sample_params, a NATIVE
    1024x1024 render (within limits; no hires detour), and never a
    pinned seed."""
    result = _gen(configured, tmp_path, monkeypatch)
    assert not result.get("error"), result
    body = _FakeClient.job_bodies[0]
    assert body["seed"] == -1  # #423: omit = fresh random draw
    assert body["sample_params"]["sample_steps"] == 4
    assert body["sample_params"]["guidance"]["txt_cfg"] == 1.0
    assert body["sample_params"]["guidance"]["distilled_guidance"] == 1.0
    assert body["width"] == 1024 and body["height"] == 1024
    assert "hires" not in body
    assert result["width"] == 1024 and result["height"] == 1024
    note = result.get("note") or ""
    assert "cfg_scale locked at 1" in note
    assert "steps 4 (built-in default)" in note
    assert "(configured default)" not in note


def test_omit_path_capsless_carries_built_ins_in_envelope(configured, tmp_path, monkeypatch):
    """#419: on a caps-less server the built-in steps/cfg ride the
    <sd_cpp_extra_args> envelope and the built-in size rides the legacy
    size string - the omit path is fully specified everywhere."""
    _FakeClient.caps_payload = {}
    monkeypatch.setattr(httpx, "AsyncClient", _FakeClient)
    result = asyncio.run(
        tools.execute_tool(
            "generate_image", {"prompt": "x"}, workspace=str(tmp_path)
        )
    )
    assert not result.get("error"), result
    _, body = _FakeClient.last
    extra = _embedded_extra(body)
    assert extra["sample_params"]["sample_steps"] == 4
    assert extra["sample_params"]["guidance"]["txt_cfg"] == 1.0
    assert extra["sample_params"]["guidance"]["distilled_guidance"] == 1.0
    assert extra["seed"] == -1  # #423: omit = fresh random draw
    assert body["size"] == "1024x1024"


# ---------------------------------------------------------------------- seed


def test_caller_seed_reaches_native_body(configured, tmp_path, monkeypatch):
    _gen(configured, tmp_path, monkeypatch, seed=7)
    assert _FakeClient.job_bodies[0]["seed"] == 7


def test_reseed_requests_fresh_random_draw(configured, tmp_path, monkeypatch):
    """reseed=True is the caller ASKING for randomization: seed -1 (the
    protocol's own randomizer), never a pinned value."""
    _gen(configured, tmp_path, monkeypatch, reseed=True)
    assert _FakeClient.job_bodies[0]["seed"] == -1


def test_reseed_true_never_pins_a_pinned_seed(configured, tmp_path, monkeypatch):
    _gen(configured, tmp_path, monkeypatch, reseed=True, seed=42)
    # seed wins over reseed when both arrive (explicit beats the ask)
    assert _FakeClient.job_bodies[0]["seed"] == 42


def test_reseed_capsless_rides_extra_args(configured, tmp_path, monkeypatch):
    _FakeClient.caps_payload = {}
    monkeypatch.setattr(httpx, "AsyncClient", _FakeClient)
    result = asyncio.run(
        tools.execute_tool(
            "generate_image", {"prompt": "x", "reseed": True},
            workspace=str(tmp_path),
        )
    )
    assert not result.get("error"), result
    _, body = _FakeClient.last
    assert _embedded_extra(body)["seed"] == -1


def test_reseed_capsless_yields_to_explicit_seed(configured, tmp_path, monkeypatch):
    """Same precedence as the native path when both inputs arrive."""
    _FakeClient.caps_payload = {}
    monkeypatch.setattr(httpx, "AsyncClient", _FakeClient)
    result = asyncio.run(
        tools.execute_tool(
            "generate_image", {"prompt": "x", "reseed": True, "seed": 42},
            workspace=str(tmp_path),
        )
    )
    assert not result.get("error"), result
    _, body = _FakeClient.last
    assert _embedded_extra(body)["seed"] == 42


# ------------------------------------------------------- steps / cfg native


def test_steps_native_body_sample_params(configured, tmp_path, monkeypatch):
    _gen(configured, tmp_path, monkeypatch, steps=15)
    sp = _FakeClient.job_bodies[0]["sample_params"]
    assert sp["sample_steps"] == 15
    # #419: the omit slot still fills from the built-in layer
    assert sp["guidance"]["txt_cfg"] == 1.0
    assert sp["guidance"]["distilled_guidance"] == 1.0


def test_cfg_scale_native_body_guidance(configured, tmp_path, monkeypatch):
    _gen(configured, tmp_path, monkeypatch, cfg_scale=1.0)
    sp = _FakeClient.job_bodies[0]["sample_params"]
    assert sp["guidance"]["txt_cfg"] == 1.0
    assert sp["guidance"]["distilled_guidance"] == 1.0  # #433: locked at 1
    # #419: the omit slot still fills from the built-in layer
    assert sp["sample_steps"] == 4


def test_steps_and_cfg_together(configured, tmp_path, monkeypatch):
    _gen(configured, tmp_path, monkeypatch, steps=15, cfg_scale=1.0)
    sp = _FakeClient.job_bodies[0]["sample_params"]
    assert sp["sample_steps"] == 15
    assert sp["guidance"]["txt_cfg"] == 1.0
    assert sp["guidance"]["distilled_guidance"] == 1.0


def test_native_endpoint_unavailable_falls_back_with_sampling_fields(
    configured, tmp_path, monkeypatch
):
    """The native-unavailable fallback embeds extra args from the body -
    steps/cfg MUST ride along (the hard-coded key-list regression)."""
    _FakeClient.native_ok = False
    result = _gen(configured, tmp_path, monkeypatch, steps=15, cfg_scale=1.0)
    assert not result.get("error"), result
    _, legacy = _FakeClient.last
    extra = _embedded_extra(legacy)
    assert extra["sample_params"]["sample_steps"] == 15
    assert extra["sample_params"]["guidance"]["txt_cfg"] == 1.0
    assert extra["seed"] == -1  # #423: omit = fresh random draw


# ---------------------------------------------------------- caps-less extras


def test_capsless_extra_args_envelope_steps_and_cfg(configured, tmp_path, monkeypatch):
    _FakeClient.caps_payload = {}
    monkeypatch.setattr(httpx, "AsyncClient", _FakeClient)
    result = asyncio.run(
        tools.execute_tool(
            "generate_image",
            {"prompt": "x", "steps": 15, "cfg_scale": 1.0},
            workspace=str(tmp_path),
        )
    )
    assert not result.get("error"), result
    _, body = _FakeClient.last
    extra = _embedded_extra(body)
    assert extra["sample_params"]["sample_steps"] == 15
    assert extra["sample_params"]["guidance"]["txt_cfg"] == 1.0


# ------------------------------------------------------------ config defaults


def test_config_defaults_apply_on_omit(configured, tmp_path, monkeypatch):
    cfgmod.save_config(
        {
            "image_generation": {
                "provider": "sdcpp",
                "api_base": "http://127.0.0.1:1234",
                "generation_defaults": {
                    "cfg_scale": 1.0,
                    "steps": 4,
                    "width": 1024,
                    "height": 1024,
                },
            }
        }
    )
    result = _gen(configured, tmp_path, monkeypatch)
    assert not result.get("error"), result
    body = _FakeClient.job_bodies[0]
    assert body["sample_params"]["sample_steps"] == 4
    assert body["sample_params"]["guidance"]["txt_cfg"] == 1.0
    # the configured 1024 size IS the native render (#421)
    assert body["width"] == 1024 and body["height"] == 1024
    assert "hires" not in body
    assert body["seed"] == -1  # #423: omit = fresh random draw
    note = result.get("note") or ""
    assert "configured default" in note
    assert "(built-in default)" not in note
    assert result["width"] == 1024 and result["height"] == 1024


def test_caller_beats_config_default(configured, tmp_path, monkeypatch):
    cfgmod.save_config(
        {
            "image_generation": {
                "provider": "sdcpp",
                "api_base": "http://127.0.0.1:1234",
                "generation_defaults": {"cfg_scale": 1.0, "steps": 4},
            }
        }
    )
    result = _gen(configured, tmp_path, monkeypatch, steps=15, width=512, height=512)
    assert not result.get("error"), result
    body = _FakeClient.job_bodies[0]
    assert body["sample_params"]["sample_steps"] == 15  # caller
    assert body["sample_params"]["guidance"]["txt_cfg"] == 1.0  # config
    assert body["width"] == 512 and body["height"] == 512  # caller


def test_config_defaults_reach_capsless_transport(configured, tmp_path, monkeypatch):
    cfgmod.save_config(
        {
            "image_generation": {
                "provider": "sdcpp",
                "api_base": "http://127.0.0.1:1234",
                "generation_defaults": {"steps": 4},
            }
        }
    )
    _FakeClient.caps_payload = {}
    monkeypatch.setattr(httpx, "AsyncClient", _FakeClient)
    result = asyncio.run(
        tools.execute_tool(
            "generate_image", {"prompt": "x"}, workspace=str(tmp_path)
        )
    )
    assert not result.get("error"), result
    _, body = _FakeClient.last
    assert _embedded_extra(body)["sample_params"]["sample_steps"] == 4


def test_config_rejects_nonpositive_and_fractional_counts(configured, tmp_path, monkeypatch):
    """0 / negative / fractional width, height, steps are config junk;
    cfg_scale stays a free finite float."""
    cfgmod.save_config(
        {
            "image_generation": {
                "provider": "sdcpp",
                "api_base": "http://127.0.0.1:1234",
                "generation_defaults": {
                    "width": 0,
                    "height": -64,
                    "steps": 4.5,
                    "cfg_scale": 1.5,
                },
            }
        }
    )
    result = _gen(configured, tmp_path, monkeypatch)
    assert not result.get("error"), result
    body = _FakeClient.job_bodies[0]
    sp = body.get("sample_params") or {}
    assert sp["sample_steps"] == 4           # 4.5 rejected -> built-in (#419)
    assert sp.get("guidance", {}).get("txt_cfg") == 1.5  # free float kept
    assert "distilled_guidance" not in (sp.get("guidance") or {})  # off-lock: no injection
    # 0 / -64 rejected -> the BUILT-IN 1024x1024 is the native render
    # (#421); junk keys never poison it
    assert body["width"] == 1024 and body["height"] == 1024
    assert "hires" not in body
    note = result.get("note") or ""
    assert "steps 4 (built-in default)" in note
    assert "steps (configured" not in note
    assert "width (configured" not in note
    assert "height (configured" not in note


def test_config_width_only_squares(configured, tmp_path, monkeypatch):
    """A configured width with no height keeps the pre-existing square
    semantics (native render from the full 1024x1024 intent)."""
    cfgmod.save_config(
        {
            "image_generation": {
                "provider": "sdcpp",
                "api_base": "http://127.0.0.1:1234",
                "generation_defaults": {"width": 1024},
            }
        }
    )
    result = _gen(configured, tmp_path, monkeypatch)
    assert not result.get("error"), result
    body = _FakeClient.job_bodies[0]
    # native render from the full 1024x1024 intent (#421), NOT a
    # 1024x512 half-render
    assert body["width"] == 1024 and body["height"] == 1024
    assert "hires" not in body


def test_junk_config_values_are_ignored(configured, tmp_path, monkeypatch):
    cfgmod.save_config(
        {
            "image_generation": {
                "provider": "sdcpp",
                "api_base": "http://127.0.0.1:1234",
                "generation_defaults": {"steps": "many", "cfg_scale": None, "width": True},
            }
        }
    )
    result = _gen(configured, tmp_path, monkeypatch)
    assert not result.get("error"), result
    body = _FakeClient.job_bodies[0]
    assert body["sample_params"]["sample_steps"] == 4  # built-in underlay
    assert "configured default" not in (result.get("note") or "")
    assert "(built-in default)" in (result.get("note") or "")


# -------------------------------------------------- ground-truth result note


def test_params_chunk_note_reaches_result(configured, tmp_path, monkeypatch):
    async def fake_wait(api_base, job_id, on_chunk=None, deadline_s=None):
        text = (
            "prompt\nSteps: 4\nCFG scale: 1.000000\nGuidance: 1.000000\n"
            "Seed: 28500\nSize: 1024x1024\n"
        )
        return base64.b64encode(_png_with_params(1024, 1024, text)).decode(), None

    monkeypatch.setattr(tools, "_img_gen_job_wait", fake_wait)
    result = _gen(configured, tmp_path, monkeypatch, steps=4, cfg_scale=1.0)
    assert not result.get("error"), result
    note = result.get("note") or ""
    assert "server ran:" in note
    assert "steps 4" in note
    assert "seed 28500" in note
    assert "cfg_scale 1.000000" in note
    assert "size 1024x1024" in note


def test_params_chunk_absent_stays_silent(configured, tmp_path, monkeypatch):
    """No chunk (e.g. the ESRGAN chain's re-encoded PNG) -> no note, no
    error: degrades silently."""
    result = _gen(configured, tmp_path, monkeypatch)
    assert not result.get("error"), result
    assert "server ran:" not in (result.get("note") or "")


def test_png_params_scans_past_foreign_text_chunks():
    """A tEXt chunk that is NOT the parameters keyword is skipped; the
    scan continues and finds a later parameters chunk."""
    png = (
        b"\x89PNG\r\n\x1a\n"
        + _chunk(b"IHDR", struct.pack(">IIBBBBB", 64, 64, 8, 6, 0, 0, 0))
        + _chunk(b"tEXt", b"Comment\x00generated by a tool")
        + _chunk(b"tEXt", b"parameters\x00Steps: 4, Seed: 9, Size: 64x64")
        + _chunk(b"IEND", b"")
    )
    assert tools._png_params_note(png) == "server ran: steps 4, seed 9, size 64x64"
    # a chunkless PNG (the plain test blob) and garbage both stay silent
    assert tools._png_params_note(b"\x89PNG\r\n\x1a\n" + b"\x00" * 30) == ""
    assert tools._png_params_note(b"") == ""


# -------------------------------------------------------------------- schema


def test_schema_documents_steps_cfg_reseed():
    props = tools.GENERATE_IMAGE_SCHEMA["function"]["parameters"]["properties"]
    assert "steps" in props and props["steps"]["type"] == "integer"
    # #433: cfg is LOCKED at 1 (guidance-distilled klein) - cfg_scale
    # is not a model-facing parameter at all
    assert "cfg_scale" not in props
    assert "reseed" in props and props["reseed"]["type"] == "boolean"
    steps_desc = props["steps"]["description"]
    assert "explicitly asks" in steps_desc and "distilled" in steps_desc
    assert "15-20" not in steps_desc
    assert props["seed"]["description"].startswith(
        "Seed for reproducible output."
    )
    desc = tools.GENERATE_IMAGE_SCHEMA["function"]["description"]
    assert "locked at 1" in desc and "EXPLICITLY asks for more" in desc


# ------------------------------------------------------------------ #419


def test_built_in_size_reaches_capsless_legacy_size(configured, tmp_path, monkeypatch):
    """#419 second transport leg: with NO capabilities reply, a caller
    size rides the legacy `size` string - built-ins already covered by
    test_omit_path_capsless_carries_built_ins_in_envelope above."""
    _FakeClient.caps_payload = {}
    monkeypatch.setattr(httpx, "AsyncClient", _FakeClient)
    result = asyncio.run(
        tools.execute_tool(
            "generate_image", {"prompt": "x", "width": 512}, workspace=str(tmp_path)
        )
    )
    assert not result.get("error"), result
    _, body = _FakeClient.last
    assert body["size"] == "512x512"


def test_caller_size_and_config_size_partial_mix(configured, tmp_path, monkeypatch):
    """Caller height 768 with no width anywhere: width squares to the
    caller height (pre-existing `height or width` semantics); no
    configured-default note fires for the squared width."""
    cfgmod.save_config(
        {
            "image_generation": {
                "provider": "sdcpp",
                "api_base": "http://127.0.0.1:1234",
                "generation_defaults": {"steps": 6},
            }
        }
    )
    result = _gen(configured, tmp_path, monkeypatch, height=768)
    assert not result.get("error"), result
    body = _FakeClient.job_bodies[0]
    # height-only intent squares to 768x768 (pre-existing semantics)
    # and renders natively (#421)
    assert body["width"] == 768 and body["height"] == 768
    assert "hires" not in body
    assert body["sample_params"]["sample_steps"] == 6
    note = result.get("note") or ""
    assert "steps 6 (configured default)" in note
    assert "size 1024x1024" not in note
    assert "width (configured" not in note


def test_mixed_config_and_built_in_notes(configured, tmp_path, monkeypatch):
    """Config overrides only steps; cfg_scale + size report their
    BUILT-IN source so the note tells the truth about every layer."""
    cfgmod.save_config(
        {
            "image_generation": {
                "provider": "sdcpp",
                "api_base": "http://127.0.0.1:1234",
                "generation_defaults": {"steps": 12},
            }
        }
    )
    result = _gen(configured, tmp_path, monkeypatch)
    assert not result.get("error"), result
    body = _FakeClient.job_bodies[0]
    assert body["sample_params"]["sample_steps"] == 12
    assert body["sample_params"]["guidance"]["txt_cfg"] == 1.0
    assert body["width"] == 1024 and body["height"] == 1024
    note = result.get("note") or ""
    assert "steps 12 (configured default)" in note
    assert "cfg_scale locked at 1" in note
    assert "size 1024x1024 (built-in default)" in note


def test_reseed_still_forces_fresh_draw_over_built_ins(configured, tmp_path, monkeypatch):
    """reseed=True stays a fresh-random request (seed -1) even though
    #419 pins everything else on the omit path."""
    result = _gen(configured, tmp_path, monkeypatch, reseed=True)
    assert not result.get("error"), result
    body = _FakeClient.job_bodies[0]
    assert body["seed"] == -1
    assert body["sample_params"]["sample_steps"] == 4


def test_nonpositive_config_width_falls_through_to_built_in(
    configured, tmp_path, monkeypatch
):
    """Junk/nonpositive config size keys are dropped and the BUILT-IN
    size underlay catches the omit (native render, #421)."""
    cfgmod.save_config(
        {
            "image_generation": {
                "provider": "sdcpp",
                "api_base": "http://127.0.0.1:1234",
                "generation_defaults": {"width": -64, "height": 0},
            }
        }
    )
    result = _gen(configured, tmp_path, monkeypatch)
    assert not result.get("error"), result
    body = _FakeClient.job_bodies[0]
    assert body["width"] == 1024 and body["height"] == 1024
    assert "hires" not in body
    assert "(built-in default)" in (result.get("note") or "")
