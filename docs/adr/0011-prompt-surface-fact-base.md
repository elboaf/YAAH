# The prompt-surface fact base is the committed manifests, not a prose document

The prompt-surface review (2026-09-30 → 2026-10-05, spec #160) produced two
prose artifacts: `docs/research/prompt-surface-inventory.md` — a hand-pinned
map of every prompt section with `file:line` refs and byte sizes — and
`docs/research/prompt-surface-findings.md`, the closed review's findings
report, plus its plan. By 2026-10-06 all three were deleted (PR #318,
closing #300).

The inventory rotted faster than it could be read: wrong four days after
its baseline (`aeba2e7` — #300), breaking CI five days in, with a partial
in-flight "repair" (#306's `cffa3ba`) duplicating table rows. Its line
numbers re-derived what `ast`/`grep` produce on demand; its byte sizes
mirrored `backend/prompt_manifests/*.json` — artifacts that are
**regenerated from the real assembly entry points and drift-guarded
byte-identical against the code** (`test_prompt_manifest.py`), and
therefore cannot lie the way a hand-pinned document must.

The #239 drift guard existed to keep the inventory honest; #300 fixed its
blindness to refs that drifted onto a *neighboring* symbol. But a guard
that expensive exists only to protect its document. With the document
gone, CI no longer pays the re-pin tax on every prompt edit, and the
guard's subject no longer exists.

What survives, and where it lives now:

- **Measured facts** (per-combo sizes, section breakdowns, rendered bytes)
  → the committed manifests. Source of truth; compare against them, never
  against a doc.
- **Deliberate-duplication verdicts** ("this echo is intentional", "that
  finding was resolved by #174") → closed issues + release notes, as before.
- **Orientation** (how assembly works: one base builder + conditional
  fragments + one manifest harness) → GLOSSARY.md, "Prompt surface" section (file was named CONTEXT.md at the time of writing).
- **The one orphaned guard** (`remote manifests carry no stripped
  schemas`) → moved into `test_prompt_manifest.py`; it guarded manifests,
  not the doc.

## Considered Options

- Keep the inventory with the tightened guard (PR #318's first shape):
  rejected by the owner — once the review project closed, the doc's
  maintenance cost and demonstrated rot rate outweighed its value.
- Keep only the inventory, delete the findings report: rejected — same
  rot, same re-pin tax.
- Delete the docs and the guard, keep the manifests (chosen): facts keep
  a living home; knowledge that cannot rot stays in code and tests;
  knowledge that must be re-derived is re-derived on demand.
