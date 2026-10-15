"""#403: reference-guided generation, structured LoRA activation, the
control_lora depth-control shorthand, and the depth-map preprocessor.

Transport-level tests reuse the _FakeClient machinery from
test_image_generation (monkeypatched over httpx.AsyncClient). Depth
preprocessor tests run the real numpy/Pillow fallback path (the neural
path is force-disabled by ensuring the ONNX session init fails).

Degradation policy (review round 2): a server missing a capability
degrades with a note; invalid input and a violated control_lora
contract are hard errors.
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
    """Capabilities payload advertising exactly these img_gen features."""
    return {**_CAPS, "features_by_mode": {"img_gen": {**feat}}}


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
    """Script the fake capabilities + swap in the fake transport."""
    _FakeClient.caps_payload = _caps_with(**feat)
    monkeypatch.setattr(httpx, "AsyncClient", _FakeClient)


def _run(tool, args, workspace):
    """execute_tool via asyncio.run (the sibling module's pattern)."""
    return asyncio.run(tools.execute_tool(tool, args, workspace=workspace))


def _last_body():
    """The most recent img_gen submit body."""
    return _FakeClient.job_bodies[-1]


def _ref_files(tmp_path, *names):
    """Write small real PNGs into the workspace; returns their paths."""
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
    """Happy path: a name-matched lora lands in the job body as the
    server's own path with the requested multiplier, and reference
    files are inlined as data-URLs in ref_images."""
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
    """Multiplier is clamped (0-2), never int-truncated: 99 -> 2.0 and
    -1 -> 0.0, not 2 and 0."""
    _caps_feat(configured, monkeypatch, lora=True, hires=True)
    r = _run(
        "generate_image",
        {"prompt": "x", "lora": [{"name": "Ghibli", "multiplier": 99}, {"name": "Ghibli", "multiplier": -1}]},
        str(tmp_path),
    )
    assert not r.get("error"), r
    mults = [e["multiplier"] for e in _last_body()["lora"]]
    assert mults == [2.0, 0.0]


def test_lora_unknown_name_degrades_with_note(configured, tmp_path, monkeypatch):
    """An unresolvable lora name degrades: note explains it, the
    generation runs WITHOUT the lora field (the server would 400 on a
    garbage path)."""
    _caps_feat(configured, monkeypatch, lora=True, hires=True)
    r = _run(
        "generate_image",
        {"prompt": "x", "lora": [{"name": "no_such_lora"}]},
        str(tmp_path),
    )
    assert not r.get("error"), r
    assert "not found on the server" in r.get("note", "")
    assert "skipped" in r.get("note", "")
    assert "lora" not in _last_body()
    assert _FakeClient.submit_calls == 1


def test_control_lora_shorthand_resolves_and_validates(configured, tmp_path, monkeypatch):
    """control_lora resolves by substring, activates at 1.0, and accepts
    the caller-supplied [depth, photo] order."""
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
    """One ref without depth_from violates the [depth, photo] contract:
    hard error, nothing submitted."""
    _caps_feat(configured, monkeypatch, ref_images=True, lora=True, hires=True)
    (depth,) = _ref_files(tmp_path, "depth.png")
    r = _run(
        "generate_image",
        {"prompt": "x", "control_lora": "refcontrol", "reference_images": [depth]},
        str(tmp_path),
    )
    assert "depth FIRST" in r.get("error", "")
    assert _FakeClient.submit_calls == 0


def test_control_lora_unknown_name_is_a_hard_error(configured, tmp_path, monkeypatch):
    """Unlike plain lora, an unresolved control_lora refuses the whole
    request: the shorthand exists to prevent silently-wrong depth
    control, and degrading to no-LoRA is exactly that."""
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


def test_control_lora_with_depth_from_sends_depth_then_photo(configured, tmp_path, monkeypatch):
    """depth_from with no reference_images: the computed map is ref[0]
    and the source image itself is ref[1] - the full [depth, photo]
    contract assembled from one input."""
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
    photo_url = "data:image/png;base64," + base64.b64encode(_real_png(32, 24)).decode()
    assert body["ref_images"] == [expected, photo_url]
    assert "depth" in r.get("note", "")


def test_control_lora_missing_lora_capability_is_a_hard_error(configured, tmp_path, monkeypatch):
    """Without the lora feature, control_lora cannot be honored - and
    unlike plain lora it must NOT degrade (silently skipping the depth
    LoRA would produce an unguided render with no explanation)."""
    _caps_feat(configured, monkeypatch, ref_images=True, hires=True)  # no lora
    depth, photo = _ref_files(tmp_path, "depth.png", "photo.png")
    r = _run(
        "generate_image",
        {"prompt": "x", "control_lora": "refcontrol", "reference_images": [depth, photo]},
        str(tmp_path),
    )
    assert "supports LoRA activation" in r.get("error", "")
    assert _FakeClient.submit_calls == 0


# ------------------------------------------------------------ references


def test_reference_images_degrade_with_note(configured, tmp_path, monkeypatch):
    """A server without the ref_images feature still gets its
    generation - minus the references - with a note saying so (issue
    #403's graceful-degradation requirement)."""
    _caps_feat(configured, monkeypatch, lora=True, hires=True)  # no ref_images
    (ref,) = _ref_files(tmp_path, "photo.png")
    r = _run("generate_image", {"prompt": "x", "reference_images": [ref]}, str(tmp_path))
    assert not r.get("error"), r
    assert "reference-guided generation skipped" in r.get("note", "")
    assert "ref_images" not in _last_body()
    assert _FakeClient.submit_calls == 1


def test_reference_images_degrade_on_legacy_capsless(configured, tmp_path, monkeypatch):
    """Legacy caps-less servers have no channel for references: degrade
    with a note, never fall back to the sync endpoint carrying refs."""
    _FakeClient.caps_payload = {}
    monkeypatch.setattr(httpx, "AsyncClient", _FakeClient)
    (ref,) = _ref_files(tmp_path, "photo.png")
    r = _run("generate_image", {"prompt": "x", "reference_images": [ref]}, str(tmp_path))
    assert not r.get("error"), r
    assert "reference-guided generation skipped" in r.get("note", "")
    assert _FakeClient.gen_calls == 1
    assert _FakeClient.submit_calls == 0


def test_ref_image_outside_workspace_refused(configured, tmp_path, monkeypatch):
    """Path guard: references must resolve inside the workspace/store
    (parity with upscale_image) - invalid input is a hard error."""
    _caps_feat(configured, monkeypatch, ref_images=True, lora=True, hires=True)
    r = _run(
        "generate_image",
        {"prompt": "x", "reference_images": ["../../windows/win.ini"]},
        str(tmp_path),
    )
    assert "reference image:" in r.get("error", "")
    assert _FakeClient.submit_calls == 0


def test_too_many_refs_is_a_hard_error(configured, tmp_path, monkeypatch):
    """>4 references cannot be partially honored: refuse."""
    _caps_feat(configured, monkeypatch, ref_images=True, lora=True, hires=True)
    refs = _ref_files(tmp_path, *(f"r{i}.png" for i in range(5)))
    r = _run("generate_image", {"prompt": "x", "reference_images": refs}, str(tmp_path))
    assert "at most 4" in r.get("error", "")
    assert _FakeClient.submit_calls == 0


def test_depth_from_without_ref_feature_skips_download(configured, tmp_path, monkeypatch):
    """No ref channel -> no point paying the one-time ~100MB model
    download: skip the depth work with a note."""
    _caps_feat(configured, monkeypatch, lora=True, hires=True)  # no ref_images

    async def boom(image_bytes):  # must never be reached
        raise AssertionError("compute_depth must not run")

    monkeypatch.setattr(depthmap, "compute_depth", boom)
    (photo,) = _ref_files(tmp_path, "photo.png")
    r = _run("generate_image", {"prompt": "x", "depth_from": photo}, str(tmp_path))
    assert not r.get("error"), r
    assert "depth map skipped" in r.get("note", "")


def test_refs_skip_hires_second_pass(configured, tmp_path, monkeypatch):
    """Reference renders take the ESRGAN chain instead of the hires
    second diffusion pass (re-diffusion drifts off-reference)."""
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


def test_schema_grows_only_when_configured(configured, tmp_path, monkeypatch):
    """The new params ride the existing generate_image schema (no new
    tool, so subagent exclusion and gating are untouched)."""
    _caps_feat(configured, monkeypatch, hires=True)
    schema = next(
        s for s in tools.get_schemas(workspace=str(tmp_path))
        if s["function"]["name"] == "generate_image"
    )
    props = schema["function"]["parameters"]["properties"]
    for p in ("reference_images", "lora", "control_lora", "depth_from"):
        assert p in props, p


# ------------------------------------------------------------ depthmap


def test_fallback_luminance_produces_png(configured, tmp_path, monkeypatch):
    """Fallback path yields a decodable PNG at the input's size, with a
    note that says luminance approximation happened."""
    _force_fallback(monkeypatch)
    png, note = asyncio.run(depthmap.compute_depth(_real_png(32, 16)))
    assert png is not None
    from PIL import Image
    import io

    img = Image.open(io.BytesIO(png))
    assert img.size == (32, 16)
    assert "luminance" in note


def test_compute_depth_never_raises_on_garbage(configured, monkeypatch):
    """Undecodable input -> (None, reason), never an exception."""
    _force_fallback(monkeypatch)
    png, note = asyncio.run(depthmap.compute_depth(b"not an image at all"))
    assert png is None
    assert "could not decode" in note


def test_depth_note_mentions_unavailable_reason(configured, monkeypatch):
    """The fallback note must point at the fix (install onnxruntime),
    not just report failure."""
    _force_fallback(monkeypatch)
    _, note = asyncio.run(depthmap.compute_depth(_real_png(8, 8)))
    assert "onnxruntime" in note
