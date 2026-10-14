"""#403: reference-guided generation, structured LoRA activation, the
control_lora depth-control shorthand, and the depth-map preprocessor.

Transport-level tests reuse the _FakeClient machinery from
test_image_generation (monkeypatched over httpx.AsyncClient). Depth
preprocessor tests run the real numpy/Pillow fallback path (the neural
path is force-disabled by ensuring the ONNX session init fails).
"""
import asyncio
import base64
import struct
import zlib

import httpx
import pytest

from backend.agent import config as cfgmod
from backend.agent import depthmap, tools

from backend.tests.test_image_generation import _FakeClient, _png


def _real_png(w, h, rgb=(128, 128, 128)):
    """A genuinely decodable PNG (the _png() helper in the other test
    module is only IHDR-shaped and PIL rejects it)."""
    raw = b"".join(b"\x00" + bytes(rgb) * w for _ in range(h))

    def chunk(tag, data):
        return (
            struct.pack(">I", len(data))
            + tag
            + data
            + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
        )

    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(raw))
        + chunk(b"IEND", b"")
    )


_CAPS = {
    "limits": {
        "min_width": 64,
        "min_height": 64,
        "max_width": 4096,
        "max_height": 4096,
        "max_upscale_width": 8192,
        "max_upscale_height": 8192,
    },
    "defaults": {"width": 512, "height": 512},
    "upscale": True,
    "features_by_mode": {"img_gen": {"hires": True, "vae_tiling": True}},
    "loras": [
        {"name": "flux2_klein_9b_refcontrol_depth", "path": "flux2_klein_9b_refcontrol_depth.safetensors"},
        {"name": "Ghibli_style_Flux 9B", "path": "Ghibli_style_Flux 9B.safetensors"},
    ],
}


def _caps_with(**feat):
    caps = {**_CAPS, "features_by_mode": {"img_gen": {**feat}}}
    return caps


@pytest.fixture
def configured(tmp_path, monkeypatch):
    """Same contract as the sibling module's fixture: isolated config,
    reset fake, cleared caps cache."""
    monkeypatch.setattr(cfgmod, "CONFIG_PATH", tmp_path / "config.json")
    _FakeClient.reset()
    tools._CAPS_CACHE.clear()
    cfgmod.save_config(
        {"image_generation": {"provider": "sdcpp", "api_base": "http://127.0.0.1:1234"}}
    )
    return cfgmod.load_config()


def _caps_feat(configured, monkeypatch, **feat):
    """Advertise exactly these img_gen features + the refcontrol lora."""
    _FakeClient.caps_payload = _caps_with(**feat)
    monkeypatch.setattr(httpx, "AsyncClient", _FakeClient)


def _run(tool, args, workspace):
    return asyncio.run(tools.execute_tool(tool, args, workspace=workspace))


def _last_body():
    return _FakeClient.job_bodies[-1]


def _ref_files(tmp_path, *names):
    out = []
    for n in names:
        p = tmp_path / n
        p.write_bytes(_real_png(32, 24))
        out.append(str(p))
    return out


def _force_fallback(monkeypatch):
    """Disable the neural depth path deterministically (onnxruntime may
    or may not be installed on the test box)."""
    monkeypatch.setattr(depthmap, "_init_onnx_session", lambda: None)


# ----------------------------------------------------------------- lora


def test_lora_and_reference_images_reach_job_body(configured, tmp_path, monkeypatch):
    _caps_feat(configured, monkeypatch, ref_images=True, lora=True, hires=True)
    ref = _ref_files(tmp_path, "photo.png")[0]
    r = _run(
        "generate_image",
        {
            "prompt": "x",
            "lora": [{"name": "refcontrol_depth", "multiplier": 0.8}],
            "reference_images": [ref],
        },
        str(tmp_path),
    )
    assert not r.get("error"), r
    body = _last_body()
    assert body["lora"] == [
        {
            "path": "flux2_klein_9b_refcontrol_depth.safetensors",
            "multiplier": 0.8,
        }
    ]
    assert body["ref_images"][0].startswith("data:image/png;base64,")


def test_lora_multiplier_clamped(configured, tmp_path, monkeypatch):
    _caps_feat(configured, monkeypatch, lora=True, hires=True)
    r = _run(
        "generate_image",
        {"prompt": "x", "lora": [{"name": "Ghibli", "multiplier": 99}, {"name": "Ghibli", "multiplier": -1}]},
        str(tmp_path),
    )
    assert not r.get("error"), r
    mults = [e["multiplier"] for e in _last_body()["lora"]]
    assert mults == [2.0, 0.0]


def test_control_lora_shorthand_resolves_and_validates(configured, tmp_path, monkeypatch):
    _caps_feat(configured, monkeypatch, ref_images=True, lora=True, hires=True)
    depth, photo = _ref_files(tmp_path, "depth.png", "photo.png")
    r = _run(
        "generate_image",
        {"prompt": "refcontrol", "control_lora": "refcontrol", "reference_images": [depth, photo]},
        str(tmp_path),
    )
    assert not r.get("error"), r
    body = _last_body()
    assert body["lora"] == [
        {"path": "flux2_klein_9b_refcontrol_depth.safetensors", "multiplier": 1.0}
    ]
    assert len(body["ref_images"]) == 2


def test_control_lora_rejects_single_ref(configured, tmp_path, monkeypatch):
    _caps_feat(configured, monkeypatch, ref_images=True, lora=True, hires=True)
    (depth,) = _ref_files(tmp_path, "depth.png")
    r = _run(
        "generate_image",
        {"prompt": "x", "control_lora": "refcontrol", "reference_images": [depth]},
        str(tmp_path),
    )
    assert "depth FIRST" in r.get("error", "")
    assert _FakeClient.submit_calls == 0


def test_control_lora_unknown_name_friendly_error(configured, tmp_path, monkeypatch):
    _caps_feat(configured, monkeypatch, ref_images=True, lora=True, hires=True)
    depth, photo = _ref_files(tmp_path, "depth.png", "photo.png")
    r = _run(
        "generate_image",
        {"prompt": "x", "control_lora": "nonexistent", "reference_images": [depth, photo]},
        str(tmp_path),
    )
    assert "no server LoRA matches" in r.get("error", "")
    assert "flux2_klein_9b_refcontrol_depth" in r["error"]
    assert _FakeClient.submit_calls == 0


def test_control_lora_with_depth_from_computes_and_prepends(configured, tmp_path, monkeypatch):
    _caps_feat(configured, monkeypatch, ref_images=True, lora=True, hires=True)

    async def fake_depth(image_bytes):
        return b"FAKEPNG", "depth fake"

    monkeypatch.setattr(depthmap, "compute_depth", fake_depth)
    (photo,) = _ref_files(tmp_path, "photo.png")
    r = _run(
        "generate_image",
        {"prompt": "x", "control_lora": "refcontrol", "depth_from": photo},
        str(tmp_path),
    )
    assert not r.get("error"), r
    body = _last_body()
    expected = "data:image/png;base64," + base64.b64encode(b"FAKEPNG").decode()
    assert body["ref_images"] == [expected]
    assert "depth" in r.get("note", "")


def test_reference_images_refused_without_feature(configured, tmp_path, monkeypatch):
    _caps_feat(configured, monkeypatch, lora=True, hires=True)  # no ref_images
    (ref,) = _ref_files(tmp_path, "photo.png")
    r = _run("generate_image", {"prompt": "x", "reference_images": [ref]}, str(tmp_path))
    assert "does not support reference images" in r.get("error", "")
    assert _FakeClient.submit_calls == 0


def test_reference_images_refused_on_legacy_capsless(configured, tmp_path, monkeypatch):
    _FakeClient.caps_payload = {}
    monkeypatch.setattr(httpx, "AsyncClient", _FakeClient)
    (ref,) = _ref_files(tmp_path, "photo.png")
    r = _run("generate_image", {"prompt": "x", "reference_images": [ref]}, str(tmp_path))
    assert "did not report" in r.get("error", "")
    # the legacy sync path must NOT silently degrade a reference request
    assert _FakeClient.gen_calls == 0
    assert _FakeClient.submit_calls == 0


def test_ref_image_outside_workspace_refused(configured, tmp_path, monkeypatch):
    _caps_feat(configured, monkeypatch, ref_images=True, lora=True, hires=True)
    r = _run(
        "generate_image",
        {"prompt": "x", "reference_images": ["../../windows/win.ini"]},
        str(tmp_path),
    )
    assert "reference image:" in r.get("error", "")
    assert _FakeClient.submit_calls == 0


def test_refs_skip_hires_second_pass(configured, tmp_path, monkeypatch):
    _caps_feat(configured, monkeypatch, ref_images=True, lora=True, hires=True, vae_tiling=True)
    _FakeClient.upscale_payload = {
        "images": [{"b64_json": base64.b64encode(_png(1024, 1024)).decode()}],
        "upscaler": "RealESRGAN_x4plus",
        "scale": 2,
        "repeats": 2,
        "width": 1024,
        "height": 1024,
    }
    (ref,) = _ref_files(tmp_path, "photo.png")
    r = _run(
        "generate_image",
        {"prompt": "x", "reference_images": [ref], "width": 1024, "height": 1024},
        str(tmp_path),
    )
    assert not r.get("error"), r
    body = _last_body()
    assert "hires" not in body, "reference renders must skip the second diffusion pass"
    assert _FakeClient.upscale_calls >= 1
    assert "hires second diffusion pass is skipped" in r.get("note", "")


def test_plain_oversized_still_uses_hires(configured, tmp_path, monkeypatch):
    """The refs-only hires skip must not leak into plain generations."""
    _caps_feat(configured, monkeypatch, hires=True, vae_tiling=True)
    r = _run("generate_image", {"prompt": "x", "width": 1024, "height": 1024}, str(tmp_path))
    assert not r.get("error"), r
    assert "hires" in _last_body()


def test_steps_and_guidance_passthrough(configured, tmp_path, monkeypatch):
    _caps_feat(configured, monkeypatch, hires=True)
    r = _run(
        "generate_image",
        {"prompt": "x", "steps": 30, "guidance": 4.0},
        str(tmp_path),
    )
    assert not r.get("error"), r
    assert _last_body()["sample_params"] == {"sample_steps": 30, "guidance": {"txt_cfg": 4.0}}


def test_schema_grows_only_when_configured(configured, tmp_path, monkeypatch):
    """The new params ride the existing generate_image schema (no new
    tool, so subagent exclusion and gating are untouched)."""
    _caps_feat(configured, monkeypatch, hires=True)
    names = [s["function"]["name"] for s in tools.get_schemas(workspace=str(tmp_path))]
    assert "generate_image" in names
    schema = next(s for s in tools.get_schemas(workspace=str(tmp_path)) if s["function"]["name"] == "generate_image")
    props = schema["function"]["parameters"]["properties"]
    for p in ("reference_images", "lora", "control_lora", "depth_from", "steps", "guidance"):
        assert p in props, p


# ------------------------------------------------------------ depthmap


def test_fallback_luminance_produces_png(configured, tmp_path, monkeypatch):
    _force_fallback(monkeypatch)
    png, note = asyncio.run(depthmap.compute_depth(_real_png(32, 16)))
    assert png is not None
    from PIL import Image
    import io

    img = Image.open(io.BytesIO(png))
    assert img.size == (32, 16)
    assert "luminance" in note


def test_compute_depth_never_raises_on_garbage(configured, monkeypatch):
    _force_fallback(monkeypatch)
    png, note = asyncio.run(depthmap.compute_depth(b"not an image at all"))
    assert png is None
    assert "could not decode" in note


def test_depth_note_mentions_unavailable_reason(configured, monkeypatch):
    _force_fallback(monkeypatch)
    _, note = asyncio.run(depthmap.compute_depth(_real_png(8, 8)))
    assert "onnxruntime" in note
