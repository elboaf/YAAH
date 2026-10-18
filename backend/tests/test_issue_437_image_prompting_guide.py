"""#437: official FLUX.2 klein prompting guidance + mode:"edit".

The official BFL prompting guide (docs.bfl.ai/
guides/prompting_guide_flux2_klein) teaches two OPPOSITE prompt
disciplines: creation = novelist prose (front-load, lighting-first);
editing = the prompt is the transformation ONLY, references carry the
visual detail. This module pins: the mode parameter (default, enum,
edit-without-refs hard error), the byte-identical guide phrases on the
model-facing surfaces (schema description, prompt param, HELP_DOCS),
the wire-prompt edit rewrite, the per-render result-note coaching, and
the unadvertised edit_image executor (the future promotion seam).
"""

import asyncio
from pathlib import Path

import httpx
import pytest

from backend.agent import config as cfgmod
from backend.agent import tools

from backend.tests.test_image_generation import _FakeClient
from backend.tests.test_imagegen_403 import (
    _caps_feat,
    _ref_files,
    _run,
)


@pytest.fixture
def configured(tmp_path, monkeypatch):
    """Isolated config, reset fake, cleared caps cache (sibling pattern)."""
    monkeypatch.setattr(cfgmod, "CONFIG_PATH", tmp_path / "config.json")
    _FakeClient.reset()
    tools._CAPS_CACHE.clear()
    cfgmod.save_config(
        {"image_generation": {"provider": "sdcpp", "api_base": "http://127.0.0.1:1234"}}
    )
    return cfgmod.load_config()


# ------------------------------------------------------------ guide constants


def test_guide_phrases_are_byte_identical_to_the_official_guide():
    """The constants quote the official klein guide verbatim (spot-checks
    against the shipped guide's prose)."""
    c, e = tools.IMG_GUIDE_CREATION, tools.IMG_GUIDE_EDIT
    for phrase in [
        "Write flowing novelist prose, not keywords",
        "subject first, then setting, details, and lighting",
        "word order signals priority",
        "single greatest impact",
        "every detail serves the image",
        "Kodak Portra 400",
    ]:
        assert phrase in c, phrase
    for phrase in [
        "carry the visual details",
        "what should change or how elements should combine",
        "replace the bike with a rearing black horse",
        "change the season to winter",
        "image 1 to match the style of image 2",
        "improve the lighting",
        "make it more professional",
    ]:
        assert phrase in e, phrase


# ------------------------------------------------------------------- schema


def test_schema_mode_enum_and_default(configured):
    props = tools.GENERATE_IMAGE_SCHEMA["function"]["parameters"]["properties"]
    mode = props["mode"]
    assert mode["type"] == "string"
    assert mode["enum"] == ["generate", "edit"]
    assert '"generate" (default)' in mode["description"]


def test_schema_prompt_param_carries_both_disciplines():
    props = tools.GENERATE_IMAGE_SCHEMA["function"]["parameters"]["properties"]
    pd = props["prompt"]["description"]
    # creation half
    assert "novelist prose" in pd and "front-load" in pd
    assert "photographer" in pd
    # edit half
    assert "transformation" in pd and "references carry the visual" in pd
    assert "never re-describe" in pd
    # the old generic advice is gone
    assert "Describe subject, style, medium" not in pd


def test_schema_description_names_mode_and_keeps_433_pins():
    desc = tools.GENERATE_IMAGE_SCHEMA["function"]["description"]
    assert 'mode "generate"' in desc and 'mode "edit"' in desc
    # #433/#435 pins untouched
    assert "locked at 1" in desc and "distilled guidance" in desc
    assert "step-distilled design point" in desc
    # cfg_scale stays out of the model-facing schema
    props = tools.GENERATE_IMAGE_SCHEMA["function"]["parameters"]["properties"]
    assert "cfg_scale" not in props


def test_help_docs_carries_the_guide_and_fixed_hires_claim():
    doc = tools.HELP_DOCS["generate_image"]
    assert "TWO PROMPTING DISCIPLINES" in doc
    assert "front-load" in doc and "TRANSFORMATION only" in doc
    # pre-#436 drift fixed: refs ride hires with a drift note, never skipped
    assert "skipped for reference renders" not in doc
    assert "ride the hires fix with a drift note" in doc


# ---------------------------------------------------------------- executor


def test_edit_mode_without_refs_is_a_teaching_hard_error(configured, tmp_path):
    r = _run(
        "generate_image",
        {"prompt": "replace the bike with a horse", "mode": "edit"},
        workspace=str(tmp_path),
    )
    assert r.get("error")
    assert "reference_images" in r["error"] and "transformation" in r["error"]
    assert 'mode "generate"' in r["error"]
    # no request left the machine
    assert _FakeClient.gen_calls == 0 and _FakeClient.submit_calls == 0


def test_edit_mode_bad_value_refuses(configured, tmp_path):
    r = _run(
        "generate_image",
        {"prompt": "x", "mode": "inpaint"},
        workspace=str(tmp_path),
    )
    assert r.get("error") == tools.EDIT_MODE_BAD


def test_generate_mode_omitted_is_the_default(configured, tmp_path, monkeypatch):
    _caps_feat(configured, monkeypatch, hires=True, vae_tiling=True)
    r = _run("generate_image", {"prompt": "a lighthouse at dusk"}, workspace=str(tmp_path))
    assert not r.get("error"), r
    # no editor note on a plain generation
    assert "references carried this render" not in (r.get("note") or "")


# --------------------------------------------------------- edit render path


def test_edit_mode_renders_with_refs_and_carries_editor_note(
    configured, tmp_path, monkeypatch
):
    _caps_feat(configured, monkeypatch, hires=True, vae_tiling=True, ref_images=True)
    refs = _ref_files(tmp_path, "src.png")
    r = _run(
        "generate_image",
        {
            "prompt": "replace the bike with a rearing black horse",
            "mode": "edit",
            "reference_images": refs,
        },
        workspace=str(tmp_path),
    )
    assert not r.get("error"), r
    body = _FakeClient.job_bodies[-1]
    assert body.get("ref_images")
    assert "apply this edit; the references carry the visual" in body["prompt"]
    note = r.get("note") or ""
    assert "references carried this render" in note
    assert "rephrase the change" in note


def test_generate_with_refs_gets_the_distinction_note(
    configured, tmp_path, monkeypatch
):
    """Implicit edit (no mode, refs present): same coaching, no rewrite."""
    _caps_feat(configured, monkeypatch, hires=True, vae_tiling=True, ref_images=True)
    refs = _ref_files(tmp_path, "src2.png")
    r = _run(
        "generate_image",
        {"prompt": "a lighthouse at dusk", "reference_images": refs},
        workspace=str(tmp_path),
    )
    assert not r.get("error"), r
    body = _FakeClient.job_bodies[-1]
    # no rewrite injected in generate mode
    assert "apply this edit" not in body["prompt"]
    assert "references carried this render" in (r.get("note") or "")


def test_edit_mode_capsless_legacy_still_errors_before_request(
    configured, tmp_path, monkeypatch
):
    _FakeClient.caps_payload = {}
    monkeypatch.setattr(httpx, "AsyncClient", _FakeClient)
    r = _run(
        "generate_image",
        {"prompt": "make it winter", "mode": "edit"},
        workspace=str(tmp_path),
    )
    assert r.get("error") and "reference_images" in r["error"]
    assert _FakeClient.gen_calls == 0


def test_edit_mode_on_refless_server_refuses(configured, tmp_path, monkeypatch):
    """Round 2 (CodeRabbit): edit mode NEEDS the references - a server
    without the ref_images capability (or with no native job API) gets
    a hard error, NOT a silent plain generation from a
    transformation-only prompt. Generate mode keeps the #403 degrade
    ladder untouched."""
    _FakeClient.caps_payload = {}
    monkeypatch.setattr(httpx, "AsyncClient", _FakeClient)
    refs = _ref_files(tmp_path, "src3.png")
    r = _run(
        "generate_image",
        {
            "prompt": "make it winter",
            "mode": "edit",
            "reference_images": refs,
        },
        workspace=str(tmp_path),
    )
    err = r.get("error") or ""
    assert "mode" in err and "edit" in err and "ref_images" in err, r
    assert _FakeClient.gen_calls == 0 and _FakeClient.submit_calls == 0


def test_generate_mode_still_degrades_refs_on_capsless(
    configured, tmp_path, monkeypatch
):
    """The #403 ladder survives: generate mode (explicit or omitted)
    with refs on a capsless server degrades with the note and renders."""
    _FakeClient.caps_payload = {}
    monkeypatch.setattr(httpx, "AsyncClient", _FakeClient)
    refs = _ref_files(tmp_path, "src5.png")
    r = _run(
        "generate_image",
        {"prompt": "a lighthouse", "reference_images": refs},
        workspace=str(tmp_path),
    )
    assert not r.get("error"), r
    assert "reference-guided generation skipped" in r.get("note", "")
    assert _FakeClient.gen_calls == 1
    assert "references carried this render" not in (r.get("note") or "")


# --------------------------------------------------------------- edit_image


def test_edit_image_executor_forces_mode_edit(configured, tmp_path, monkeypatch):
    _caps_feat(configured, monkeypatch, hires=True, vae_tiling=True, ref_images=True)
    refs = _ref_files(tmp_path, "src4.png")
    r = _run(
        "edit_image",
        {"prompt": "age this portrait by 30 years", "reference_images": refs},
        workspace=str(tmp_path),
    )
    assert not r.get("error"), r
    body = _FakeClient.job_bodies[-1]
    assert body.get("ref_images")
    assert "apply this edit" in body["prompt"]
    assert "references carried this render" in (r.get("note") or "")


def test_edit_image_requires_refs(configured, tmp_path):
    r = _run(
        "edit_image",
        {"prompt": "x"},
        workspace=str(tmp_path),
    )
    assert r.get("error") and "reference_images" in r["error"]


# ------------------------------------------------------- subagent exclusion


def test_edit_image_excluded_from_subagents():
    from backend.agent import subagents

    assert "edit_image" in subagents._ALWAYS_EXCLUDED


# ------------------------------------------------- attachment annotations


def test_attachment_annotation_names_mode_edit():
    import inspect

    from backend.agent import loop

    src = inspect.getsource(loop._parts_with_images)
    assert 'mode \\"edit\\"' in src  # escaped-quote source form
    assert "reference_images) / " in src


def test_image_rejection_note_names_mode_edit():
    from backend.agent import model_client

    assert 'mode "edit"' in model_client.IMAGE_REJECTION_NOTE
    assert 'mode "edit"' in model_client.IMAGE_NOVISION_NOTE


# ---------------------------------------------------- depth contract intact


def test_depth_control_contract_still_hard_errors(configured, tmp_path, monkeypatch):
    """mode:"edit" must not weaken the control_lora [depth, photo]
    contract (#403)."""
    _caps_feat(
        configured, monkeypatch, hires=True, vae_tiling=True, ref_images=True, lora=True
    )
    refs = _ref_files(tmp_path, "depth.png", "photo.png")
    r = _run(
        "generate_image",
        {
            "prompt": "x",
            "mode": "edit",
            "reference_images": refs[:1],
            "control_lora": "flux2_klein_9b_refcontrol_depth",
        },
        workspace=str(tmp_path),
    )
    assert r.get("error") and "depth FIRST" in r["error"]