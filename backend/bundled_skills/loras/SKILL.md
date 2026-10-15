---
name: loras
description: Inspect, verify, and complete the LoRA catalog of the configured image-generation (sd.cpp) server; document LoRAs the scanner could not classify.
disable-model-invocation: true
---

# /loras - LoRA catalog for image generation

You are helping the user manage the LoRAs installed on their configured
image-generation server (stable-diffusion.cpp). Everything you need is in
two backend endpoints:

- `GET /api/loras` - the full catalog: for each LoRA its name, path,
  classification (`triggered` / `triggerless` / `img2img_helper` /
  `unknown`), trigger word, multiplier, match tokens, and for rescanned
  files the KB entry or user override that classified it.
- `POST /api/loras/override` - persist what the user tells you, keyed to
  the file's SHA256 (survives renames and re-downloads):
  `{"path": "<path from GET /api/loras>", "class": "triggered|triggerless|img2img_helper|skip", "trigger": "<token or null>", "img2img_prompt": "<instruction or null>", "multiplier": 0.8, "notes": "..."}`

The response of the override call includes the sha256 used.

## Flow

1. Call `GET /api/loras`. If it errors or returns `{"loras": [], "error": ...}`,
   tell the user image generation is not configured (Settings) or the server
   is unreachable, and stop.
2. Render a compact table: name, class, trigger/multiplier, source
   (metadata / KB / user / unknown).
3. For every `unknown` entry, gather - in ONE question round, not one
   question per file - what the user knows about each file:
   - its trigger word (or: none - triggerless), or
   - that it is an img2img helper (and with what instruction prompt, if
     any), or
   - that it should be skipped entirely.
4. `POST /api/loras/override` once per documented file, then show the
   effective catalog again with the new classifications in place.

## Rules

- Never invent a trigger word. If the user does not know one, record the
  file as `triggerless` (weight-only) or `skip` - never guess.
- Contradiction warnings in the GET output (KB says X, file metadata says
  Y) are worth reading aloud: the file may have been replaced under a
  familiar name.
- This skill is user-invoked only; do not load it mid-turn unprompted.
