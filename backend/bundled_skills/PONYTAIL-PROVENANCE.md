# PONYTAIL-PROVENANCE.md

Vendored from [DietrichGebert/ponytail](https://github.com/DietrichGebert/ponytail)
(MIT license), decision recorded on issue #56: bundle as plain opt-in skills
(available via the /s menu and model-invoked by description — **not**
always-on; per-turn re-injection / drift prevention is out of scope until an
always-on injection mechanism exists, tracked via #57 Phase 2).

- Upstream commit: `e3ba2aa6f1e6f0bc4d69eb09c9f0d0a93af56156` (v4.10.0, 2026-09-14)
- Vendored paths: `skills/{ponytail,ponytail-audit,ponytail-debt,ponytail-gain,ponytail-help,ponytail-review}` → `backend/bundled_skills/<name>/SKILL.md`
- Files copied byte-for-byte; nothing upstream was modified.

Seeding follows the normal bundled-skill rules (`skills.py: ensure_dir()`):
never overwrites an existing user copy, so existing installs do not receive
updates automatically — refresh by replacing these folders at a new pinned
commit.

Note: `ponytail/SKILL.md` uses a YAML block scalar (`description: >`) for its
description. That parses correctly with PyYAML (in requirements.txt); under
the no-PyYAML flat fallback only the first line survives, which is why the
description's first line carries the core summary.
