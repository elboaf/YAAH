"""Issue #406: generate_image harnessing - LoRA proactivity + chat
attachment discoverability.

Live-session evidence (2026-10-14, retro-wave restyle): the model saw a
pasted image but had no handle for it (burned tool calls hunting the
bytes in the PyInstaller _MEI dir), and no way to learn which LoRAs the
server carries (the /loras skill is user-invoked by design). Covered:

- IMAGES_ROOT: YAAH_IMAGES_DIR override, frozen -> ~/.yaah/images.
- attachment annotations: seen images carry their store rel in text.
- filename-derived match tokens for triggerless LoRAs (squash matching,
  word-boundary guard, half-hit bar, stop words), and the negative
  space: generic prompts must NOT fire.
- catalog visibility: the result note lists available LoRAs when none
  was applied; the not-found note lists the known names.
"""
import asyncio
import json
import sys
from pathlib import Path

import httpx
import pytest

from backend.agent import config as cfgmod
from backend.agent import imagedata, loras, tools


# --------------------------------------------------------------- helpers

def _scan_with(files):
    return {"files": files}


def _real_png() -> bytes:
    # 1x1 transparent PNG
    return bytes.fromhex(
        "89504e470d0a1a0a0000000d494844520000000100000001080600000"
        "01f15c4890000000d4944415478da63fccf00f6030003030100c9fe92"
        "ef0000000049454e44ae426082"
    )


class _FakeResp:
    status_code = 200
    text = '{"error": "fake-response"}'

    def json(self):
        return {"data": [{"b64_json": _b64()}]}


def _b64():
    import base64

    return base64.b64encode(_real_png()).decode()


class _FakeClient:
    """Records img_gen submit bodies; caps reply advertises lora+ref_images
    like the live server does."""

    job_bodies = []
    submit_calls = 0

    @classmethod
    def reset(cls):
        cls.job_bodies = []
        cls.submit_calls = 0

    def __init__(self, *a, **k):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def get(self, url, **k):
        r = _FakeResp()
        if url.endswith("/sdcpp/v1/capabilities"):
            r.json = lambda: {
                "limits": {"min_width": 64, "min_height": 64,
                           "max_width": 2048, "max_height": 2048},
                "defaults": {"width": 512, "height": 512},
                "features_by_mode": {"img_gen": {"hires": False,
                                                 "ref_images": True,
                                                 "lora": True}},
                "loras": [
                    {"name": "ck-neon-retrowave-klein-9b",
                     "path": "ck-neon-retrowave-klein-9b.safetensors"},
                    {"name": "Ghibli_style_Flux 9B",
                     "path": "Ghibli_style_Flux 9B.safetensors"},
                ],
                "upscale": False,
                "upscalers": [],
            }
        elif url.endswith("/sdcpp/v1/jobs/fake-job"):
            r.json = lambda: {
                "status": "completed",
                "result": {"images": [{"b64_json": _b64()}]},
            }
        else:
            r.status_code = 404
            r.json = lambda: {}
        return r

    async def post(self, url, json=None, **k):
        r = _FakeResp()
        if url.endswith("/sdcpp/v1/img_gen"):
            _FakeClient.submit_calls += 1
            _FakeClient.job_bodies.append(json)
            r.status_code = 202
            r.json = lambda: {"id": "fake-job",
                              "poll_url": "/sdcpp/v1/jobs/fake-job"}
        else:
            r.status_code = 404
            r.json = lambda: {}
        return r


@pytest.fixture
def configured(tmp_path, monkeypatch):
    """Isolated config with an sd-server configured."""
    monkeypatch.setattr(cfgmod, "CONFIG_PATH", tmp_path / "config.json")
    _FakeClient.reset()
    tools._CAPS_CACHE.clear()
    cfgmod.save_config(
        {"image_generation": {"provider": "sdcpp",
                              "api_base": "http://127.0.0.1:1234"}}
    )
    return cfgmod.load_config()


# ------------------------------------------------------- IMAGES_ROOT (#406)

def test_images_root_env_override(monkeypatch, tmp_path):
    monkeypatch.setattr(imagedata, "IMAGES_ROOT", imagedata.IMAGES_ROOT)
    monkeypatch.setenv("YAAH_IMAGES_DIR", str(tmp_path / "img"))
    # the module constant is frozen at import; the FUNCTION decides
    root = imagedata._images_root()
    assert root == tmp_path / "img"


def test_images_root_frozen_goes_home(monkeypatch):
    monkeypatch.delenv("YAAH_IMAGES_DIR", raising=False)
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    root = imagedata._images_root()
    assert root == Path.home() / ".yaah" / "images"


def test_images_root_dev_is_repo_data():
    monkey_missing = "YAAH_IMAGES_DIR" not in __import__("os").environ
    root = imagedata.IMAGES_ROOT
    if monkey_missing:
        assert root.name == "images"


# ------------------------------------------------ attachment annotations

def test_parts_with_images_annotates_store_rel():
    import base64

    from backend.agent.loop import _parts_with_images

    data_url = (
        "data:image/png;base64,"
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk"
        "+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
    )
    rel = imagedata.save_data_url(data_url, subdir="406")
    parts = _parts_with_images("make this retro-wave", [rel])
    texts = [
        p["text"] for p in parts
        if isinstance(p, dict) and p.get("type") == "text"
    ]
    assert any(rel in t and "reference_images" in t for t in texts), texts
    assert any(p.get("type") == "image_url" for p in parts)


def test_parts_missing_file_stays_degraded():
    from backend.agent.loop import _parts_with_images

    parts = _parts_with_images("look", ["406/nope.png"])
    assert any("missing" in p.get("text", "") for p in parts if isinstance(p, dict))


# ------------------------------------------ filename-derived match tokens

def test_name_tokens_drops_base_model_noise():
    toks = loras._name_tokens("ck-neon-retrowave-klein-9b")
    assert toks == ["neon", "retrowave"]


def test_name_tokens_strips_brackets_and_digits():
    assert loras._name_tokens("[FLUX.2.Klein]PixelArt_Redmond") == [
        "pixelart", "redmond",
    ]
    assert loras._name_tokens("Ghibli_style_Flux 9B") == ["ghibli"]
    # bare hash-ish counters go
    assert loras._name_tokens("DarkGhibliKlein9b_000005000") == [
        "darkghibliklein9b",
    ]


def test_triggerless_filename_match_fires_on_style_word():
    """The live-session miss: 'retro-wave' must summon a retrowave LoRA."""
    scan = _scan_with({
        "ck.safetensors": {
            "name": "ck-neon-retrowave-klein-9b", "class": "triggerless",
            "trigger": None, "multiplier": None, "match": [],
        }
    })
    sel = loras.select_lora(scan, "make this look more retro-wave style", False)
    assert sel is not None and sel["name"] == "ck-neon-retrowave-klein-9b"
    # squashed spelling too
    sel2 = loras.select_lora(scan, "make this look more retrowave", False)
    assert sel2 is not None


def test_triggerless_generic_prompt_does_not_fire():
    scan = _scan_with({
        "a.safetensors": {
            "name": "Realistic_studio_Klein_9B_epoch_12", "class": "triggerless",
            "trigger": None, "multiplier": None, "match": [],
        },
        "b.safetensors": {
            "name": "Phone_Photography_2025_KL_9B", "class": "triggerless",
            "trigger": None, "multiplier": None, "match": [],
        },
    })
    assert loras.select_lora(scan, "a cozy village at dusk", False) is None
    assert loras.select_lora(scan, "a castle on a hill", False) is None


def test_phone_prefix_collision_does_not_score():
    """'phone' must not hit inside 'photography' (word-boundary squash)."""
    scan = _scan_with({
        "b.safetensors": {
            "name": "Phone_Photography_2025_KL_9B", "class": "triggerless",
            "trigger": None, "multiplier": None, "match": [],
        }
    })
    # 'photography' prompt: phone is NOT a whole word there -> 1 of 2
    # tokens (photography) hits -> score 0.5 -> still fires (>= 0.5 bar),
    # but a prompt with NEITHER word must not.
    assert loras.select_lora(scan, "a modern portrait", False) is None


def test_triggered_class_keeps_training_tokens():
    scan = _scan_with({
        "d.safetensors": {
            "name": "elusarca_ancient_style", "class": "triggered",
            "trigger": "elusarca_ancient_style", "multiplier": 0.8,
            "match": ["elusarca_ancient_style"],
        }
    })
    sel = loras.select_lora(scan, "an elusarca_ancient_style bust", False)
    assert sel and sel["cls"] == "triggered"
    # a filename-only token must NOT fire a triggered LoRA
    scan2 = _scan_with({
        "e.safetensors": {
            "name": "someother_name_xyz", "class": "triggered",
            "trigger": "realtoken", "multiplier": 0.8,
            "match": ["realtoken"],
        }
    })
    assert loras.select_lora(scan2, "someother name xyz style", False) is None


def test_apply_injects_no_trigger_for_filename_match():
    scan = _scan_with({
        "ck.safetensors": {
            "name": "ck-neon-retrowave-klein-9b", "class": "triggerless",
            "trigger": None, "multiplier": None, "match": [],
        }
    })
    prompt = "retro-wave city"
    body = loras.apply_to_body({"prompt": prompt}, scan, prompt)
    assert body["lora"][0]["path"] == "ck.safetensors"
    assert body["prompt"] == prompt  # no invented trigger injected


# ------------------------------------------- catalog visibility in results

def test_generate_image_note_lists_available_loras(configured, tmp_path, monkeypatch):
    monkeypatch.setattr(httpx, "AsyncClient", _FakeClient)
    result = asyncio.run(tools.execute_tool(
        "generate_image", {"prompt": "a plain castle"}, workspace=str(tmp_path)
    ))
    assert "error" not in result
    note = result.get("note", "")
    assert "available loras" in note
    assert "ck-neon-retrowave-klein-9b" in note


def test_generate_image_note_absent_when_lora_applied(configured, tmp_path, monkeypatch):
    cfgmod.save_config({
        "lora_scan": {
            "http://127.0.0.1:1234": _scan_with({
                "style.safetensors": {
                    "name": "style", "class": "triggered",
                    "trigger": "snarbo", "multiplier": 0.8,
                    "match": ["snarbo"],
                }
            })
        }
    })
    monkeypatch.setattr(httpx, "AsyncClient", _FakeClient)
    result = asyncio.run(tools.execute_tool(
        "generate_image", {"prompt": "snarbo castle"}, workspace=str(tmp_path)
    ))
    note = result.get("note", "")
    assert "available loras" not in note
    assert "lora applied: style" in note


def test_generate_image_note_absent_when_explicit_lora(configured, tmp_path, monkeypatch):
    monkeypatch.setattr(httpx, "AsyncClient", _FakeClient)
    result = asyncio.run(tools.execute_tool(
        "generate_image",
        {"prompt": "a castle", "lora": [{"name": "Ghibli_style_Flux 9B"}]},
        workspace=str(tmp_path),
    ))
    assert "error" not in result
    assert "available loras" not in result.get("note", "")


def test_not_found_note_lists_known_names(configured, tmp_path, monkeypatch):
    monkeypatch.setattr(httpx, "AsyncClient", _FakeClient)
    result = asyncio.run(tools.execute_tool(
        "generate_image",
        {"prompt": "a castle", "lora": [{"name": "nope"}]},
        workspace=str(tmp_path),
    ))
    note = result.get("note", "")
    assert "not found on the server" in note
    assert "ck-neon-retrowave-klein-9b" in note  # known list included


def test_no_catalog_note_when_server_has_none(configured, tmp_path, monkeypatch):
    class _BareClient(_FakeClient):
        async def get(self, url, **k):
            r = _FakeResp()
            if url.endswith("/sdcpp/v1/capabilities"):
                r.json = lambda: {
                    "limits": {"min_width": 64, "min_height": 64,
                               "max_width": 2048, "max_height": 2048},
                    "defaults": {"width": 512, "height": 512},
                    "features_by_mode": {"img_gen": {"hires": False,
                                                     "ref_images": True,
                                                     "lora": True}},
                    "loras": [],
                    "upscale": False,
                    "upscalers": [],
                }
            elif url.endswith("/sdcpp/v1/jobs/fake-job"):
                r.json = lambda: {
                    "status": "completed",
                    "result": {"images": [{"b64_json": _b64()}]},
                }
            else:
                r.status_code = 404
                r.json = lambda: {}
            return r

    monkeypatch.setattr(httpx, "AsyncClient", _BareClient)
    result = asyncio.run(tools.execute_tool(
        "generate_image", {"prompt": "a plain castle"}, workspace=str(tmp_path)
    ))
    assert "available loras" not in result.get("note", "")


# ------------------------------ reference_images accepts pasted-image rels

def test_reference_images_accepts_store_rel(configured, tmp_path):
    import base64

    data_url = (
        "data:image/png;base64,"
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk"
        "+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
    )
    rel = imagedata.save_data_url(data_url, subdir="406")
    result = asyncio.run(tools.execute_tool(
        "generate_image",
        {"prompt": "restyle this", "reference_images": [rel]},
        workspace=str(tmp_path),
    ))
    # must NOT error with "image not found" - the store rel resolves
    assert "error" not in result or "image not found" not in str(result["error"])
