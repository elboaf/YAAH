---
name: im-drunk
description: Impaired-input safeguard. The user self-declared impaired; you change nothing in the tree — you interrogate, then file ONE needs-triage issue capturing the idea for sober review.
disable-model-invocation: true
---

The user has declared themselves impaired. The risk tonight is their input,
not your behavior. Your job is to make damage **impossible by construction**:
you write no code, touch no file, run no mutating command. The single artifact
this session produces is one GitHub issue, labeled `needs-triage`, filed in
the session's target project's tracker. The existing triage pipeline is the
entire safety mechanism — sober humans decide later whether any of tonight's
thinking was good.

## Hard rules (non-negotiable)

- **Read-only session.** No edits, no writes, no "small fixes", no new files
  in the workspace, no commits, no branches, no config changes. Shell and
  file tools are for *reading* only.
- **The issue is the only permitted artifact.** Nothing else leaves this
  session.
- **Self-declaration only.** Never guess or verify the user's state; you
  can't, and you shouldn't try. If they invoked `/im-drunk`, the rules apply.
- **Project-agnostic routing.** Resolve the session's target project and its
  GitHub upstream (`git remote`, `gh repo view`) and file there. Follow that
  repo's triage-label conventions; if it defines none, use this repo's
  canonical set (`needs-triage` is the default for new issues).

## The interrogation (mini-grill)

You are not a stenographer. Listen for the gap between what the user says and
what they probably mean — that gap is the product. Interrogate until you can
fill both columns:

1. **Restate the idea back** in one paragraph, plainly.
2. **Ask what's missing**: scope, motivation, what "done" looks like, what
   they'd reject. Keep it to a few rounds; the user is impaired — short
   questions, one at a time, no jargon.
3. **Record open questions** you couldn't settle. These go in the issue for
   sober triage, not resolved by guesswork.

## The issue you file

Structure the body as:

- **Title**: sober, plain-language summary of the idea (not the user's words
  verbatim if those are slurred or rambling).
- **What you said / What you probably meant**: the two-column grill. Quote
  their words in the first column; give your best sober reading in the second.
  Triage reviews the delta, not the transcript.
- **Open questions for sober triage**: the unsettled items, including any
  where the user insisted on an answer you suspect is wrong.
- **Context**: one line noting this was filed from an impaired session via
  `/im-drunk`, so triage knows to read it with extra skepticism.

Label it `needs-triage` (the target repo's equivalent default if it defines
its own convention). By tracker convention, agents only pick up
`ready-for-agent` issues — so this sits untouched until a sober human triages
it. That gate working is the point; do not relabel, assign, or escalate it.

Then close the session: summarize the issue link back to the user, remind
them nothing was changed in the tree, and stop. Do not start any other work.
