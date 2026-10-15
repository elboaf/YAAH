"""Issue #398: LoRA support for image generation.

Header parser fixtures for every metadata shape seen in the wild (zero
metadata, triggered, empty-caption, ai-toolkit no-tag, foreign base),
request-builder asserts (structured `lora[]` + trigger injection, never a
`<lora:...>` tag), the hash-keyed bundled KB (hash-only lookup,
user>builtin precedence, contradiction surfacing), the settings-save scan
trigger (non-blocking), and the /api/loras + /api/loras/override
endpoints behind the /loras slash command.
"""
import asyncio
import base64
import json
import re
import struct
import time

import httpx
import pytest
from fastapi.testclient import TestClient

from backend.agent import config as cfgmod
from backend.agent import loras, tools


# --------------------------------------------------------------- helpers

def _st_file(tmp_path, name, metadata=None):
    """A minimal safetensors file: 8-byte LE u64 header length + JSON
    header (two dummy tensor entries + optional __metadata__). Bytes
    after the header are never read by the parser."""
    header = {
        "weight.a": {"dtype": "F32", "shape": [1], "data_offsets": [0, 4]},
        "weight.b": {"dtype": "F32", "shape": [1], "data_offsets": [4, 8]},
    }
    if metadata is not None:
        header["__metadata__"] = metadata
    raw = json.dumps(header).encode()
    p = tmp_path / name
    p.write_bytes(struct.pack("<Q", len(raw)) + raw + b"\0" * 8)
    return p


def _entry(path, name=None):
    return {"name": name or path, "path": path}


def _scan_with(files):
    return {"files": files}


PNG = base64.b64encode(b"\x89PNG-fake-image").decode()


class _FakeResp:
    status_code = 200
    text = '{"error": "fake-response"}'

    def json(self):
        return {"data": [{"b64_json": PNG}]}


class _FakeClient:
    """Records img_gen submit bodies - that is where `lora[]` must
    appear. Capabilities reply with a small honest server (no hires, no
    upscale) so the flow stays on the native job path."""
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
                "features_by_mode": {"img_gen": {"hires": False}},
                "upscale": False,
                "upscalers": [],
            }
        elif url.endswith("/sdcpp/v1/jobs/fake-job"):
            r.json = lambda: {
                "status": "completed",
                "result": {"images": [{"b64_json": PNG}]},
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
    """Isolated config with an sd-server configured (same contract as
    test_image_generation.py's fixture)."""
    monkeypatch.setattr(cfgmod, "CONFIG_PATH", tmp_path / "config.json")
    _FakeClient.reset()
    tools._CAPS_CACHE.clear()
    cfgmod.save_config(
        {"image_generation": {"provider": "sdcpp",
                              "api_base": "http://127.0.0.1:1234"}}
    )
    return cfgmod.load_config()


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(cfgmod, "CONFIG_PATH", tmp_path / "config.json")
    import backend.main as mainmod

    monkeypatch.setattr(mainmod, "load_config", cfgmod.load_config)
    monkeypatch.setattr(mainmod, "save_config", cfgmod.save_config)
    from backend.main import app

    with TestClient(app) as c:
        yield c


# ------------------------------------------------- classification fixtures

def test_classify_zero_metadata_is_unknown():
    assert loras.classify_metadata(None)["class"] == "unknown"
    assert loras.classify_metadata({})["class"] == "unknown"


def test_classify_triggered_strip_repeat_prefix():
    meta = {
        "ss_tag_frequency": {
            "1_elusarca_ancient_style": {"elusarca_ancient_style, photo": 12}
        },
        "ss_base_model_version": "flux2_klein_9b",
    }
    c = loras.classify_metadata(meta)
    assert c["class"] == "triggered"
    assert c["trigger"] == "elusarca_ancient_style"
    assert c["base_model"] == "flux2_klein_9b"
    assert "elusarca_ancient_style" in c["match"]


def test_classify_triggered_json_string_tag_frequency():
    """The REAL ai-toolkit shape (verified on the live catalog):
    ss_tag_frequency is a JSON STRING inside __metadata__."""
    meta = {
        "ss_tag_frequency": '{"1_elusarca_ancient_style": {"elusarca_ancient_style": 1}}',
        "ss_base_model_version": "flux2_klein_9b",
        "software": '{"name": "ai-toolkit"}',
    }
    c = loras.classify_metadata(meta)
    assert c["class"] == "triggered"
    assert c["trigger"] == "elusarca_ancient_style"


def test_classify_count1_signature_flagged_for_kb():
    """One dataset dir, every caption count 1: the consistency-V2 trap.
    Classified triggered but flagged so the scan hashes the file and the
    KB adjudicates."""
    meta = {
        "ss_tag_frequency": '{"1_consistency": {"consistency": 1}}',
        "ss_base_model_version": "flux2_klein_9b",
    }
    c = loras.classify_metadata(meta)
    assert c["class"] == "triggered"
    assert c["count1_signature"] is True
    # the PixelArt file (also count-1 in the wild) has the same shape
    c2 = loras.classify_metadata({
        "ss_tag_frequency": '{"1_Pixel Art, PixArFK": {"Pixel Art, PixArFK": 1}}',
    })
    assert c2["trigger"] == "Pixel Art, PixArFK"
    assert c2["count1_signature"] is True


def test_classify_multi_dir_not_count1():
    meta = {
        "ss_tag_frequency": '{"1_a": {"a": 3}, "1_b": {"b": 4}}',
    }
    assert loras.classify_metadata(meta)["count1_signature"] is False


def test_classify_empty_caption_signature_is_triggerless():
    c = loras.classify_metadata({"ss_tag_frequency": {"1_": {"": 1}}})
    assert c["class"] == "triggerless"
    assert c["trigger"] is None
    # and as the string shape some trainers emit
    c2 = loras.classify_metadata({"ss_tag_frequency": '{"1_": {"": 1}}'})
    assert c2["class"] == "triggerless"


def test_classify_ai_toolkit_no_tag_is_triggerless():
    c = loras.classify_metadata(
        {"ss_base_model_version": "flux2_klein_9b", "trainer": "ai-toolkit"}
    )
    assert c["class"] == "triggerless"


def test_classify_foreign_base_recorded_not_fatal():
    c = loras.classify_metadata(
        {"modelspec.architecture": "sd15/lora",
         "ss_tag_frequency": {"1_x": {"x": 3}}}
    )
    assert c["class"] == "triggered"
    assert c["base_model"] == "sd15/lora"


def test_parse_header_reads_metadata_only(tmp_path):
    p = _st_file(tmp_path, "a.safetensors",
                 {"ss_base_model_version": "flux2_klein_9b"})
    meta = loras.read_safetensors_metadata(p)
    assert meta["ss_base_model_version"] == "flux2_klein_9b"


def test_parse_header_garbage_and_truncated(tmp_path):
    bad = tmp_path / "bad.safetensors"
    bad.write_bytes(b"\x01\x02\x03\x04\x05\x06\x07\x08not json")
    assert loras.read_safetensors_metadata(bad) is None
    short = tmp_path / "short.safetensors"
    short.write_bytes(b"\x01\x02")
    assert loras.read_safetensors_metadata(short) is None
    assert loras.read_safetensors_metadata(tmp_path / "missing") is None


# ------------------------------------------------------------ scan + KB

def test_scan_unknown_file_hashed_and_kb_rescued(tmp_path, monkeypatch):
    """SHA256 is computed for an UNKNOWN file and the hash-tied KB entry
    reclassifies it (the ultra_real_v5_pro rescue path)."""
    p = _st_file(tmp_path, "ultra_real_v5_pro.safetensors")  # no metadata
    real = loras.sha256_file(p)
    monkeypatch.setattr(loras, "builtin_kb", lambda: {
        real: {"class": "img2img_helper", "trigger": None,
               "multiplier": {"t2i_fallback": [0.6, 0.8]}},
    })
    scan = loras.scan_loras(tmp_path, [_entry("ultra_real_v5_pro.safetensors")])
    info = scan["files"]["ultra_real_v5_pro.safetensors"]
    assert info["class"] == "img2img_helper"
    assert info["sha256"] == real
    assert info["multiplier"] == 0.6


def test_scan_skips_hash_for_classified_files(tmp_path, monkeypatch):
    """Hash only what needs hashing: a metadata-classified file is never
    hashed."""
    calls = []

    def _spy(path):
        calls.append(path)
        return "0" * 64

    monkeypatch.setattr(loras, "sha256_file", _spy)
    _st_file(tmp_path, "t.safetensors",
             {"ss_tag_frequency": {"1_tok": {"tok": 4}}})
    scan = loras.scan_loras(tmp_path, [_entry("t.safetensors")])
    assert scan["files"]["t.safetensors"]["class"] == "triggered"
    assert calls == []


def test_kb_lookup_requires_hash(monkeypatch):
    """A filename-only match is never sufficient to apply builtin
    knowledge - the hash is the identity."""
    monkeypatch.setattr(loras, "builtin_kb",
                        lambda: {"a" * 64: {"class": "img2img_helper"}})
    assert loras.kb_lookup("a" * 64)["class"] == "img2img_helper"
    assert loras.kb_lookup(None) is None
    assert loras.kb_lookup("b" * 64) is None


def test_user_override_wins_over_builtin(tmp_path, monkeypatch):
    digest = "c" * 64
    monkeypatch.setattr(loras, "builtin_kb",
                        lambda: {digest: {"class": "triggerless"}})
    monkeypatch.setattr(cfgmod, "CONFIG_PATH", tmp_path / "config.json")
    loras.save_override(digest, {"class": "skip", "notes": "user says skip"})
    lk = loras.kb_lookup(digest)
    assert lk["class"] == "skip"
    assert lk["source_of_truth"] == "user"


def test_contradiction_surfaced_not_silently_overwritten(tmp_path, monkeypatch):
    """KB says img2img_helper but the file's own metadata says triggered:
    the KB class applies AND the warning is surfaced for a human glance.
    The donated hash arrives the way production donates it: the /loras
    override flow stamps the SHA256 it computed into the persisted scan
    (a first scan never hashes metadata-classified files - hash only
    what needs hashing). A KB entry landing later then conflicts."""
    p = _st_file(tmp_path, "x.safetensors",
                 {"ss_tag_frequency": {"1_tok": {"tok": 4}}})
    real = loras.sha256_file(p)
    scan1 = loras.scan_loras(tmp_path, [_entry("x.safetensors")])
    assert scan1["files"]["x.safetensors"]["class"] == "triggered"
    assert "sha256" not in scan1["files"]["x.safetensors"]
    # the override-endpoint stamp (main.py does exactly this)
    scan1["files"]["x.safetensors"]["sha256"] = real
    monkeypatch.setattr(loras, "builtin_kb",
                        lambda: {real: {"class": "img2img_helper"}})
    scan2 = loras.scan_loras(tmp_path, [_entry("x.safetensors")], scan1)
    info = scan2["files"]["x.safetensors"]
    assert info["class"] == "img2img_helper"
    assert "warning" in info


def test_scan_count1_triggered_adjudicated_by_kb(tmp_path, monkeypatch):
    """A count-1 'triggered' file is hashed; a KB entry flips it to
    img2img_helper WITH a contradiction warning (consistency-V2 rescue)."""
    p = _st_file(tmp_path, "c.safetensors",
                 {"ss_tag_frequency": '{"1_consistency": {"consistency": 1}}',
                  "ss_base_model_version": "flux2_klein_9b"})
    real = loras.sha256_file(p)
    monkeypatch.setattr(loras, "builtin_kb",
                        lambda: {real: {"class": "img2img_helper",
                                        "multiplier": {"img2img": [1.6, 1.6]}}})
    scan = loras.scan_loras(tmp_path, [_entry("c.safetensors")])
    info = scan["files"]["c.safetensors"]
    assert info["class"] == "img2img_helper"
    assert info["multiplier"] == 1.6
    assert "warning" in info


def test_scan_path_escape_refused(tmp_path):
    scan = loras.scan_loras(tmp_path, [_entry("../evil.safetensors")])
    info = scan["files"]["../evil.safetensors"]
    assert info["class"] == "unknown"
    assert "sha256" not in info  # never hash a path outside the lora dir


def test_bundled_kb_ships_two_seed_entries():
    """The addendum's two researched entries ship hash-keyed."""
    kb = loras.builtin_kb()
    hashes = {k for k in kb if re.fullmatch(r"[0-9a-f]{64}", k)}
    assert ("22eeedfe80ffc35963fdd837bbe99d789eb2279a8296f9aa14e944e97abdc848"
            in hashes)
    assert ("61db2017ce420b97bd5ef11984e5a894c90003a6bbf0dc9473f8d7b9ebb3ff93"
            in hashes)


def test_scan_persists_per_api_base(tmp_path, monkeypatch):
    """Scans coexist per server; keying is by normalized api_base."""
    monkeypatch.setattr(cfgmod, "CONFIG_PATH", tmp_path / "config.json")
    loras.save_scan("http://127.0.0.1:4070/", _scan_with({"a": {"name": "a"}}))
    loras.save_scan("http://localhost:4070", _scan_with({"b": {"name": "b"}}))
    # same port, different host spelling: distinct servers, both kept
    assert loras.load_scan("http://127.0.0.1:4070")["files"] == {"a": {"name": "a"}}
    assert loras.load_scan("http://localhost:4070")["files"] == {"b": {"name": "b"}}
    loras.save_scan("http://127.0.0.1:4070", _scan_with({"z": {"name": "z"}}))
    assert loras.load_scan("http://127.0.0.1:4070/")["files"] == {"z": {"name": "z"}}


# ------------------------------------------------------------- auto-apply

def test_apply_triggered_injects_trigger_and_lora_field():
    scan = _scan_with({
        "style.safetensors": {
            "name": "style", "class": "triggered",
            "trigger": "elusarca_ancient_style", "multiplier": 0.8,
            "match": ["elusarca_ancient_style"],
        }
    })
    # the prompt names the trigger the way the agent is taught to (the
    # tool result note reports applied triggers, reinforcing their use)
    prompt = "an elusarca_ancient_style marble bust"
    body = loras.apply_to_body({"prompt": prompt}, scan, prompt)
    assert body["lora"][0]["path"] == "style.safetensors"
    assert body["lora"][0]["multiplier"] == 0.8
    assert body["lora"][0]["is_high_noise"] is False
    assert body["prompt"].startswith("elusarca_ancient_style, ")
    assert "<lora:" not in body["prompt"]


def test_apply_never_emits_lora_tag():
    """The server rejects <lora:...> by design; the structured field is
    the only channel."""
    scan = _scan_with({
        "style.safetensors": {
            "name": "style", "class": "triggered", "trigger": "pix art",
            "multiplier": 0.8, "match": ["pix art"],
        }
    })
    body = loras.apply_to_body({"prompt": "x"}, scan, "pix art castle")
    assert "<lora:" not in json.dumps(body)


def test_apply_no_match_leaves_body_untouched():
    scan = _scan_with({
        "style.safetensors": {
            "name": "style", "class": "triggerless", "trigger": None,
            "multiplier": 0.8, "match": [],
        }
    })
    out = loras.apply_to_body({"prompt": "a cat"}, scan, "a cat")
    assert "lora" not in out
    assert out["prompt"] == "a cat"


def test_apply_img2img_helper_keys_off_init_image():
    """img2img helpers ride along ONLY when an init image is present, and
    never touch the prompt (their steering is the instruction itself)."""
    scan = _scan_with({
        "u.safetensors": {
            "name": "ultra_real_v5_pro", "class": "img2img_helper",
            "trigger": None, "multiplier": 0.7, "match": [],
        }
    })
    t2i = {"prompt": "a cat"}
    assert "lora" not in loras.apply_to_body(t2i, scan, "a cat")
    i2i = {"prompt": "a cat", "init_image": "data:image/png;base64,xxx"}
    out = loras.apply_to_body(i2i, scan, "a cat")
    assert out["lora"][0]["path"] == "u.safetensors"
    assert out["lora"][0]["multiplier"] == 0.7
    assert out["prompt"] == "a cat"


def test_apply_triggered_without_trigger_is_skipped():
    """A triggered classification whose trigger was lost never guesses."""
    scan = _scan_with({
        "s.safetensors": {
            "name": "s", "class": "triggered", "trigger": None,
            "multiplier": 0.8, "match": [],
        }
    })
    assert "lora" not in loras.apply_to_body({"prompt": "x"}, scan, "x")


def test_select_lora_prefers_full_match():
    scan = _scan_with({
        "a.safetensors": {
            "name": "a", "class": "triggered", "trigger": "tok",
            "multiplier": 0.8, "match": ["tok", "second"],
        },
        "b.safetensors": {
            "name": "b", "class": "triggerless", "trigger": None,
            "multiplier": 0.8, "match": ["tok"],
        },
    })
    sel = loras.select_lora(scan, "tok and second here", False)
    assert sel["path"] == "a.safetensors"  # 2/2 beats 1/1


def test_generate_image_attaches_lora(configured, tmp_path, monkeypatch):
    """End-to-end: the tool consults the persisted scan and the native
    job body carries the structured lora[] field + injected trigger."""
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
    assert "error" not in result
    body = _FakeClient.job_bodies[-1]
    assert body["lora"][0]["path"] == "style.safetensors"
    assert body["prompt"].startswith("snarbo, ")
    assert "<lora:" not in body["prompt"]
    assert "lora applied: style" in result.get("note", "")


def test_generate_image_without_scan_is_unchanged(configured, tmp_path, monkeypatch):
    """No scan persisted: the request carries no lora field at all
    (degrade-to-noop; auto-apply must never break a working server)."""
    monkeypatch.setattr(httpx, "AsyncClient", _FakeClient)
    result = asyncio.run(tools.execute_tool(
        "generate_image", {"prompt": "plain castle"}, workspace=str(tmp_path)
    ))
    assert "error" not in result
    assert "lora" not in _FakeClient.job_bodies[-1]
    assert "<lora:" not in _FakeClient.job_bodies[-1]["prompt"]


def test_generate_image_img2img_helper_needs_init_image(configured, tmp_path, monkeypatch):
    """A KB-rescued img2img helper present in the scan must NOT ride
    along on a plain t2i request (no init image)."""
    cfgmod.save_config({
        "lora_scan": {
            "http://127.0.0.1:1234": _scan_with({
                "u.safetensors": {
                    "name": "ultra_real_v5_pro", "class": "img2img_helper",
                    "trigger": None, "multiplier": 0.7, "match": [],
                    "kb": {"class": "img2img_helper",
                           "img2img_prompt": "Make this photo high-quality."},
                }
            })
        }
    })
    monkeypatch.setattr(httpx, "AsyncClient", _FakeClient)
    result = asyncio.run(tools.execute_tool(
        "generate_image", {"prompt": "a cat"}, workspace=str(tmp_path)
    ))
    assert "error" not in result
    assert "lora" not in _FakeClient.job_bodies[-1]


# ------------------------------------------- settings save triggers scan

def test_config_put_triggers_nonblocking_scan(client, monkeypatch):
    """A PUT /api/config carrying image_generation schedules the scan and
    returns IMMEDIATELY (never blocks the save)."""
    import backend.main as mainmod

    calls = []

    async def _fake_scan(api_base):
        calls.append(api_base)
        return {"scanned_at": "now", "files": {}, "unknown_count": 0}

    monkeypatch.setattr(mainmod, "_run_lora_scan", _fake_scan)
    r = client.put(
        "/api/config",
        json={"image_generation": {"api_base": "http://127.0.0.1:4599"}},
    )
    assert r.status_code == 200
    deadline = time.monotonic() + 5
    while not calls and time.monotonic() < deadline:
        time.sleep(0.05)
    assert calls == ["http://127.0.0.1:4599"]


def test_config_put_without_image_block_does_not_scan(client, monkeypatch):
    import backend.main as mainmod

    calls = []

    async def _fake_scan(api_base):
        calls.append(api_base)
        return {}

    monkeypatch.setattr(mainmod, "_run_lora_scan", _fake_scan)
    client.put("/api/config", json={"ui_scale": 1.0})
    time.sleep(0.2)
    assert calls == []


def test_config_put_blank_unconfigure_does_not_scan(client, monkeypatch):
    """Clearing the endpoint is an UNconfigure - no scan for the empty
    base."""
    import backend.main as mainmod

    calls = []

    async def _fake_scan(api_base):
        calls.append(api_base)
        return {}

    monkeypatch.setattr(mainmod, "_run_lora_scan", _fake_scan)
    client.put("/api/config", json={"image_generation": {"api_base": ""}})
    time.sleep(0.2)
    assert calls == []


# ------------------------------------------------- /api/loras + overrides

def _seed_scan(api_base="http://127.0.0.1:4599"):
    cfgmod.save_config({
        "image_generation": {"api_base": api_base},
        "lora_scan": {
            api_base: {
                "scanned_at": "now",
                "files": {
                    "a.safetensors": {
                        "name": "a", "class": "triggered", "trigger": "tok",
                        "multiplier": 0.8, "match": ["tok"],
                    },
                    "b.safetensors": {
                        "name": "b", "class": "unknown", "trigger": None,
                        "multiplier": None, "match": [], "sha256": "e" * 64,
                    },
                },
                "unknown_count": 1,
            }
        },
    })


def test_loras_endpoint_lists_catalog(client):
    _seed_scan()
    r = client.get("/api/loras")
    assert r.status_code == 200
    data = r.json()
    assert data["count"] == 2
    assert data["unknown_count"] == 1
    assert data["notice"] and "/loras" in data["notice"]
    by_path = {f["path"]: f for f in data["loras"]}
    assert by_path["a.safetensors"]["class"] == "triggered"


def test_loras_endpoint_unconfigured(client):
    r = client.get("/api/loras")
    assert r.status_code == 200
    assert r.json()["loras"] == []
    assert r.json()["notice"] is None


def test_override_endpoint_persists_hash_keyed(client):
    _seed_scan()
    r = client.post(
        "/api/loras/override",
        json={"path": "b.safetensors", "class": "triggerless",
              "trigger": None, "multiplier": 0.8},
    )
    assert r.status_code == 200
    data = r.json()
    assert data["ok"] is True
    assert data["sha256"] == "e" * 64
    ov = cfgmod.load_config().get("lora_overrides") or {}
    assert ov["e" * 64]["class"] == "triggerless"


def test_override_endpoint_unknown_path_404(client):
    _seed_scan()
    r = client.post(
        "/api/loras/override",
        json={"path": "nope.safetensors", "class": "triggerless"},
    )
    assert r.status_code == 404


def test_override_endpoint_validates_class(client):
    _seed_scan()
    r = client.post(
        "/api/loras/override",
        json={"path": "b.safetensors", "class": "bogus"},
    )
    assert r.status_code == 422
