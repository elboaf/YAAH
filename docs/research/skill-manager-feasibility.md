# Skill manager — feasibility research & phased design (#57)

Research from issue #57 (audited 2026-09-21); landed here as the durable record. The Phase 3 curated ledger seed ships as `backend/bundled_skills/skills-ledger.json` (loaded by `backend/agent/skills_ledger.py`).

### The idea

A **skill manager** in YAAH: one place to see installed skills, **enable/disable** them, **install** new ones from a GitHub repo, **update** them, and **remove** them — instead of hand-copying folders into `~/.yaah/skills/`.

### The honest question this issue answers

YAAH is a bespoke agent/harness. Most skills on GitHub ship installers aimed at Claude Code / Codex / Cursor, and "requirements + compatibility" sounds like a per-skill investigation that's onerous for both the user and YAAH's codebase. **Is a skill manager that can successfully install skills from existing GitHub repos actually possible?**

---

## TL;DR — yes, for most of the ecosystem, and cheaper than it looks

I audited 10 popular skill repos (file trees, installers, frontmatter, skill bodies — method at the bottom). The finding that changes the problem:

**The ecosystem has converged on the exact format YAAH already implements.** Anthropic's agent-skills format was released openly ([agentskills.io/specification](https://agentskills.io/specification), [agentskills/agentskills](https://github.com/agentskills/agentskills), ~25k stars): *a skill is a folder with a `SKILL.md` (YAML frontmatter: `name`, `description`, optional `license`/`compatibility`/`metadata`/`allowed-tools`) plus optional `scripts/`, `references/`, `assets/`.* That is precisely YAAH's contract in `backend/agent/skills.py` — including `disable-model-invocation`, which YAAH already parses. YAAH's parser **ignores unknown frontmatter keys**, so it is forward-compatible with every optional field.

Consequences:

- **"Installing" a skill = getting the right folder into `~/.yaah/skills/`.** The per-repo installers (plugin marketplaces, `curl | bash`, npm CLIs) are *packaging for other harnesses' plugin conventions* — not runtime requirements of the skill. We don't need to run them.
- **The "one installer, N harnesses" model is proven.** [`vercel-labs/skills`](https://github.com/vercel-labs/skills) (`npx skills add owner/repo`, ~32k stars) installs skills into 75+ agents and does exactly this: locate `SKILL.md` folders in the source repo, copy/symlink them into each agent's skills directory. No per-repo installer is invoked.
- **Scorecard for the 10 audited repos: 6 fully drop-in, 3 work with caveats, 1 fundamentally incompatible** (subagent-orchestration architecture).

The onerous part — per-skill compatibility investigation — can't be *eliminated*, but it can be *mostly automated into install-time signals* plus a small curated ledger (Phase 2/3 below). The manager's job is to tell the user quickly what a skill assumes, not to make everything work.

---

## What YAAH's skill system is today (the contract we're extending)

From `backend/agent/skills.py`:

- A skill is a folder under `~/.yaah/skills/` containing `SKILL.md` with YAML frontmatter (`name`, `description`, optional `disable-model-invocation`) and a markdown body.
- Two invocation paths: user types `/s <name>` (body injected into the system prompt for that turn), or the model calls `load_skill` mid-turn. The skill's **folder path is passed to the model**, so `references/` + `scripts/` + `assets/` progressive disclosure already works.
- `MAX_SKILL_BODY_CHARS = 60_000` truncation, `MAX_SKILLS = 200`, startup seeding of bundled skills + an example, `YAAH_SKILLS_PATH` test hook, rescan on refresh.
- Parser reads only `name`/`description`/`disable-model-invocation` and **silently ignores everything else** → tolerates `license`, `compatibility`, `metadata`, `allowed-tools`, `risk`, `source`, `argument-hint`, etc. from any repo.

What it lacks: enable/disable, remove, update, install, provenance, reload-without-restart, and any awareness of where a skill came from.

---

## The 10 repos audited

| Repo | What the skill actually is | Frontmatter vs. spec | Official install route | YAAH verdict |
|---|---|---|---|---|
| [obra/superpowers](https://github.com/obra/superpowers) | 15 markdown skills (methodology: TDD, debugging, plans…) | `name`+`description` only — strict | Per-harness plugin marketplaces (Claude, Codex, Cursor, Gemini, Pi, …) | **A — drop-in** (caveats below) |
| [DietrichGebert/ponytail](https://github.com/DietrichGebert/ponytail) | 6 markdown-only skills (lazy-senior-dev style) | `name`, `description`, `argument-hint`, `license` | Plugin cmds; docs list *"generic agents: copy `skills/*/SKILL.md`"* as a supported route | **A — drop-in** |
| [ayghri/i-have-adhd](https://github.com/ayghri/i-have-adhd) | 1 pure-prose output-style skill | `name`, `description`, **`disable-model-invocation: true`**, `license`, `metadata` | Claude/Codex plugins, `npx skills add`, manual copy | **A — drop-in** |
| [JuliusBrussee/caveman](https://github.com/JuliusBrussee/caveman) | 1 pure-prose style skill (+ 9 wrapper skills that drive a proxy CLI) | `name`+`description` only | `npx skills add -g`, plugin, `install.sh` | **A — drop-in** (install the one skill, skip the wrappers) |
| [K-Dense-AI/scientific-agent-skills](https://github.com/K-Dense-AI/scientific-agent-skills) (~46k stars) | 166 skills: SKILL.md + `references/` + Python `scripts/` + `assets/` | Strict closed set: `name`, `description`, `license`, `compatibility`, `allowed-tools`, `metadata` | `npx skills add`, `gh skill install`, plugin pkg, plain copy | **A — drop-in** (scripts need a Python env at *runtime*) |
| [cathrynlavery/diagram-design](https://github.com/cathrynlavery/diagram-design) (~42k stars) | 1 skill: SKILL.md + ~30 `references/` + stdlib-Python `scripts/` + HTML `assets/` | `name`, `description`, `license`, `metadata.version` | Plugin marketplaces, `pi install`, **manual symlink documented as a route** | **A — drop-in** |
| [Graphify-Labs/graphify](https://github.com/Graphify-Labs/graphify) | Markdown playbook that drives a Python CLI (`uv tool install graphifyy`) | `name`+`description` only | `uv/pipx install` + `graphify install --platform <host>` (20+ hosts) | **B — works with caveats** |
| [mvanhorn/last30days-skill](https://github.com/mvanhorn/last30days-skill) | 1 skill backed by a 184 KB Python engine; **SKILL.md body is ~258 KB** | Full spread: `version`, `argument-hint`, `allowed-tools`, `user-invocable`, `metadata.openclaw` (env vars, bins) | `npx skills add`, Claude plugin, `.skill`/`.mcpb` artifacts | **B — works with caveats** |
| [sickn33/agentic-awesome-skills](https://github.com/sickn33/agentic-awesome-skills) | **Registry of 2,406 curated skills** + Node 22 CLI + MCP control plane | `name`, `description` + extra keys (`risk`, `source`, `date_added`) — informational | `npx agentic-awesome-skills --claude/--codex/--path …` | **B as a source / A per skill** |
| [Egonex-AI/Understand-Anything](https://github.com/Egonex-AI/Understand-Anything) | 9 skills that orchestrate **10 subagent definitions** + a pnpm/TypeScript core + local dashboard | `name`, `description`, `argument-hint` | Claude plugin marketplace or per-host symlink installer | **C — not compatible as designed** |

### Per-repo notes that matter for YAAH

- **superpowers** explicitly documents a porting guide whose thesis is: *"Superpowers is the same content everywhere. What changes per harness is the thin layer that delivers that content to the model."* The Claude-Code-only part is a `SessionStart` **hook** that injects the `using-superpowers` bootstrap skill; in YAAH the equivalent is simply "the model can `load_skill` it from its description" — or a session-start auto-load toggle (see Gaps). Two skills assume subagent dispatch but ship written inline fallbacks. TDD/git skills assume YAAH's shell tool — which YAAH has.
- **ponytail / i-have-adhd / caveman** are pure prose. Always-on activation is delivered via *hooks* we don't have — the skill still works when invoked (`/s ponytail`, `/s i-have-adhd`, or "say 'caveman ultra'"). i-have-adhd's frontmatter uses `disable-model-invocation: true` — **exactly YAAH's own field**, honored today.
- **graphify** works if the user installs its CLI; the skill body's subagent-orchestration step (Step 3) would mislead YAAH's model (upstream has a sequential fallback for agents without subagents). Its `references/` sidecars work — YAAH already hands the model the folder path.
- **last30days**: installs fine, but the 258 KB body blows past YAAH's 60k-char cap → **silently truncated instruction contract**; the manager must warn. Frontmatter `allowed-tools: Bash, Read, Write, AskUserQuestion, WebSearch` documents exactly which harness tools it assumes — YAAH has shell/read/write/ask_user/web search, so the mapping is good, but API keys (15+ optional env vars) are the user's job.
- **agentic-awesome-skills** is the strongest evidence curation-at-scale is a solved problem elsewhere (2,406 skills, per-skill `risk` taxonomy, per-target compatibility JSON). Full-repo installs are known to exhaust context on other hosts — subsets only. Good *source* for YAAH's browse tab; nothing to integrate at runtime.
- **Understand-Anything** is the honest counterexample: the skills are orchestration scripts for a subagent architecture (up to 7 concurrent subagents), a pnpm-built core, `$CLAUDE_PLUGIN_ROOT` resolution, and a local dashboard server. No install path makes that work in YAAH; using it would be a port, not an install. **A skill manager needs to be able to say "no" convincingly.**

---

## Compatibility taxonomy (what "compatible" honestly means)

- **A — drop-in:** copy the folder, it works. (6/10)
- **B — works with caveats:** the container installs; the body assumes something YAAH must have or the user must provide. (3/10)
- **C — harness-coupled:** needs a runtime feature YAAH doesn't have (subagents, plugin host, build step). (1/10)

The YAAH gaps that cause B/C, in order of impact:

1. **No always-on / bootstrap injection.** Several ecosystems skills expect a `SessionStart`-hook equivalent. Cheap fix inside the manager: a per-skill **"load at session start"** toggle (inject body into the system prompt like an invoked skill, every turn). Converts the always-on tier of ponytail/superpowers/i-have-adhd from "manual invocation" to "fully working" with ~zero new backend concepts.
2. **No subagents.** Nothing to do short of building subagents (out of scope); the manager should *detect and flag* (`subagent`, `Task tool`, `agents/*.md` in repo) instead of failing mysteriously later.
3. **Context budget.** Descriptions are cheap (YAAH's `<available_skills>` index is name+one-liner — fine even at 166 skills), bodies load on invocation. Risks: mega-bodies (last30days) and users bulk-installing hundreds. Manager mitigations: body-size warning vs. the 60k cap, per-skill enable/disable, and maybe a soft cap on total enabled skills.
4. **Runtime environment.** Scripts need Python/Node/uv/API keys. YAAH has shell + file tools, so `scripts/*.py` are runnable *if the env exists*. Manager should **surface, not provision**: render `compatibility` field text, `metadata.openclaw.requires.env`/`bins`, and the scripts inventory at install time.
5. **Harness-specific dead ends in bodies.** `$CLAUDE_PLUGIN_ROOT`, `~/.claude/plugins/cache`, TOML slash commands — usually harmless (the model just won't find the referenced thing), occasionally misleading. Static heuristics can flag these lines in the install panel.

---

## Proposed shape (phased)

### Phase 1 — local lifecycle (no network; the drawer in #9 grows up)

- **Enable/disable** per skill: registry flag; `list_skills()` / `model_invocable()` / the `/s` menu respect it. Disabled ≠ deleted.
- **Remove**: delete folder (confirm dialog; show what's inside first).
- **Reload without restart** (`scan_skills()` already exists — expose it).
- **Provenance**: per-skill `~/.yaah/skills/<name>/.yaah-skill.json` (or a central `~/.yaah/skills/.registry.json`) recording `installed_from`, `ref`, `commit`, `subpath`, `installed_at`, `enabled`, `load_at_session_start`. Hand-made folders simply have no provenance file — the manager shows them as "local".
- UI: Settings → Skills (the #9 drawer, expanded): Installed tab with state, source, update button.

### Phase 2 — install from URL (the core feature)

- Accept `owner/repo`, a full GitHub URL, or a direct URL to a skill subfolder (same source grammar as `npx skills add`).
- **Discovery**: GitHub API tree (or codeload zip) → find every `*/SKILL.md`; multi-skill repos get a checklist UI (superpowers = 15 checkboxes, K-Dense = 166, AAS = curated search first).
- **Install = download zip → extract selected folders → copy into `~/.yaah/skills/<name>/`**. Public repos need no auth; name-collision prompt; enforce spec rule that `name` must match the folder name (warn otherwise). Nothing executes at install time.
- **Compatibility panel per skill** — the automated version of the per-skill investigation this issue worried about:
  - *Format check*: parses? name/description present? body size vs. cap?
  - *Declarative signals*: `compatibility` text, `license`, `metadata` (env vars, bins), `allowed-tools`, `disable-model-invocation`.
  - *Static heuristics*: scripts inventory (languages), repo-level harness adapters (`.claude-plugin/`, `hooks/`, `commands/` → "ships Claude-Code-specific extras — the skill folder doesn't need them"), body greps (`$CLAUDE_PLUGIN_ROOT`, `subagent`/`Task`, `~/.claude`).
  - *Rendered verdict*: ✅ ready / ⚠️ works with caveats (listed) / ⛔ likely incompatible (why). Heuristics only — the curated ledger below overrides for known repos.
- **Update**: refetch the recorded `ref`/commit → diff → apply. Never clobber local edits (mirror `ensure_dir()`'s never-overwrite seed rule; offer "keep mine / take theirs"). Pin to commit by default; floating tag opt-in.

### Phase 3 — discovery (small, curated, no registry dependency)

- A **curated ledger** in the YAAH repo (JSON): audited repos with verdict + notes. This research is the seed — the 10 repos above become the first entries, and #55/#56 resolve as ledger entries + pack installs rather than hand-bundling.
- Optional pointers to external catalogs ([skills.sh](https://skills.sh) / vercel-labs' discovery, AAS's 2,406-skill catalog, agentskills.io client showcase) — link-outs, not integrations.

### Explicit non-goals (the honest scope cuts)

- **Never run third-party install scripts** (`curl | bash`, `plugin install` CLIs). We install folders, not plugins.
- **No plugin-manifest/marketplace/hook compatibility** (`.claude-plugin/`, hooks runtime, slash-command TOML). That's other harnesses' delivery layer; YAAH has its own.
- **No subagent emulation.** C-class stays C-class; the manager says so up front.
- **No runtime provisioning** of Python/Node/API keys — surface requirements, let the user (or the agent, in-workspace) handle it.

---

## Security & trust notes

- A skill is **prompt content** (injection surface) and may contain **scripts the agent can later execute** via shell. So: install never executes anything; the panel shows a file inventory with sizes (adopt caps like `npx skills`' 10 MB download / 25 MB extracted); per-skill enable is the blast-radius control; `disable-model-invocation` stays honored; consider a "review before first enable" step for skills that ship scripts.
- Updates pin to commits by default so a repo can't silently change what gets injected into prompts.

## Relation to existing issues

- **#9** skill drawer → Phase 1 UI.
- **#55** (superpowers bundle) / **#56** (ponytail bundle) → supersede as "install from URL / pack" + ledger entries; the hooks-gap they worry about is addressed by the session-start auto-load toggle.
- **#24** bundle GitHub CLI → not required for Phase 2 (plain HTTPS + codeload covers public repos), but `gh` would add private-repo auth and API quota headroom.

## What would honestly be required (summary)

1. **Installer** (small): URL → tree listing → zip download → copy folder. Ecosystem convergence makes this the easy part — no per-repo installers, ever.
2. **Lifecycle bookkeeping** (small): enable/disable/remove/update + provenance file + rescan endpoint.
3. **Compatibility signals + ledger** (medium, never 100%): format checks, frontmatter rendering, static heuristics, curated verdicts for popular repos. This is where the "onerous per-skill investigation" lands — automated into a panel, with honest ⛔s.
4. **Optional harness additions** if we want more B→A conversions: session-start auto-load toggle (cheap, high value); task/todo tracking (nice-to-have). Subagents are the only big rock, and nothing audited makes them mandatory to skip.

---

*Research method: file trees + READMEs + installer scripts + SKILL.md frontmatter/bodies fetched at each repo's default branch (2026-09-21) via the GitHub API and raw file fetches; spec from [agentskills.io/specification](https://agentskills.io/specification); `npx skills` behavior from [vercel-labs/skills](https://github.com/vercel-labs/skills). Star counts approximate at time of writing.*

