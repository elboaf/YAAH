"""#411: model-appropriate generation defaults for generate_image.

Caller > configured generation_defaults > omit (server defaults). The
seed is NEVER pinned on the omit path; reseed=True asks the server for a
fresh random draw (seed -1); the result note carries the ground truth of
what the server actually ran, parsed from the PNG parameters chunk.
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


def test_full_omit_native_sends_no_sampling_fields(configured, tmp_path, monkeypatch):
    """Byte-identical to pre-#411 behavior: prompt-only call, no config ->
    no seed, no sample_params, no steps anywhere in the job body."""
    result = _gen(configured, tmp_path, monkeypatch)
    assert not result.get("error"), result
    body = _FakeClient.job_bodies[0]
    assert "seed" not in body
    assert "sample_params" not in body
    assert "steps" not in body
    assert "cfg_scale" not in body
    assert "server ran:" not in (result.get("note") or "")


def test_omit_path_capsless_has_no_extra_args_block(configured, tmp_path, monkeypatch):
    _FakeClient.caps_payload = {}
    monkeypatch.setattr(httpx, "AsyncClient", _FakeClient)
    result = asyncio.run(
        tools.execute_tool(
            "generate_image", {"prompt": "x"}, workspace=str(tmp_path)
        )
    )
    assert not result.get("error"), result
    _, body = _FakeClient.last
    assert "<sd_cpp_extra_args>" not in (body.get("prompt") or "")


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
    assert "guidance" not in sp


def test_cfg_scale_native_body_guidance(configured, tmp_path, monkeypatch):
    _gen(configured, tmp_path, monkeypatch, cfg_scale=1.0)
    sp = _FakeClient.job_bodies[0]["sample_params"]
    assert sp["guidance"]["txt_cfg"] == 1.0
    assert "sample_steps" not in sp


def test_steps_and_cfg_together(configured, tmp_path, monkeypatch):
    _gen(configured, tmp_path, monkeypatch, steps=15, cfg_scale=1.0)
    sp = _FakeClient.job_bodies[0]["sample_params"]
    assert sp["sample_steps"] == 15
    assert sp["guidance"]["txt_cfg"] == 1.0


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
    # the render budget stays conservative (the server's 512 default);
    # the configured 1024 size is reached via the hires fix, in-band
    assert body["width"] == 512 and body["height"] == 512
    assert body["hires"]["target_width"] == 1024
    assert body["hires"]["target_height"] == 1024
    assert "seed" not in body
    note = result.get("note") or ""
    assert "configured default" in note
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
    assert "sample_steps" not in sp          # 4.5 rejected
    assert sp.get("guidance", {}).get("txt_cfg") == 1.5  # free float kept
    assert "width" not in body and "height" not in body  # 0 / -64 rejected
    note = result.get("note") or ""
    assert "steps (configured" not in note
    assert "width (configured" not in note
    assert "height (configured" not in note


def test_config_width_only_squares(configured, tmp_path, monkeypatch):
    """A configured width with no height keeps the pre-existing square
    semantics (render budget from the full 1024x1024 intent)."""
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
    # render budget from the full 1024x1024 intent (512 render + hires
    # to 1024), NOT a 1024x512 half-budget
    assert body["hires"]["target_width"] == 1024
    assert body["hires"]["target_height"] == 1024


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
    assert "sample_params" not in body
    assert "configured default" not in (result.get("note") or "")


# -------------------------------------------------- ground-truth result note


def test_params_chunk_note_reaches_result(configured, tmp_path, monkeypatch):
    async def fake_wait(api_base, job_id):
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
    assert "cfg_scale" in props and props["cfg_scale"]["type"] == "number"
    assert "reseed" in props and props["reseed"]["type"] == "boolean"
    steps_desc = props["steps"]["description"]
    assert "15-20" in steps_desc and "draft" in steps_desc
    assert props["seed"]["description"].startswith(
        "Seed for reproducible output."
    )
    desc = tools.GENERATE_IMAGE_SCHEMA["function"]["description"]
    assert "fast-draft" in desc and "15-20" in desc
