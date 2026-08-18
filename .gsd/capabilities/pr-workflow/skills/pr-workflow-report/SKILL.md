---
name: gsd-pr-workflow-report
description: "Regenerates PR.md from a live gh PR/check status read for the current branch (execute:wave:post lifecycle dispatch)"
allowed-tools:
  - Read
  - Bash
---

**STOP -- DO NOT READ THIS FILE. You are already reading it. This prompt was injected into your context by the command system. Using the Read tool on this file wastes tokens. Begin executing Step 1 immediately.**

## Step 1 -- Config Gate

Check whether the pr-workflow capability is enabled by reading `.planning/config.json` directly
with the Read tool.

1. Read `.planning/config.json` with the Read tool.
2. If the file exists, `config["pr-workflow"]` is present, and
   `config["pr-workflow"]["enabled"]` is explicitly the boolean `false`: display the disabled
   message below and **STOP**.
3. Otherwise -- the file is missing, `config["pr-workflow"]` is absent, or it is present with no
   `enabled` key -- fall through to the shipped default (`pr-workflow.enabled: true` in
   `capability.json`) and proceed to Step 2.

**Disabled message:**

```
GSD > PR WORKFLOW REPORT

PR workflow reporting is disabled (pr-workflow.enabled).
Nothing was regenerated; the loop proceeds normally.
```

This step is `onError: skip` at both dispatch points (`execute:wave:post`, `ship:post`) -- no
dispatch ever fails a phase.

## Step 1.5 -- Lifecycle-point branch

This skill is registered at **two** `capability.json` `steps[]` entries, both `ref.skill:
"pr-workflow-report"`: `execute:wave:post` and `ship:post`. Determine which point dispatched this
run and follow the matching branch -- do not collapse the two into one call.

**At `execute:wave:post`**: proceed to **Step 2** below.

**At `ship:post`**: proceed to **Step 2b** below, then stop -- do not also run Step 2's
`verify-post` dispatch from this point.

## Step 2 -- execute:wave:post: Regenerate PR.md

Run one Bash call passing only the phase directory:

```bash
python3 .gsd/capabilities/pr-workflow/scripts/pr_status.py verify-post <phase directory>
```

Print `pr_status.py`'s stdout summary verbatim (`PR.md regenerated: pr_status=<state>` or
`PR.md regenerated: no open PR (pr_status=none)`).

## Step 2b -- ship:post: warn-only no-open-PR notice

Run one Bash call passing only the phase directory:

```bash
python3 .gsd/capabilities/pr-workflow/scripts/pr_status.py ship-post-notice <phase directory>
```

Print its stdout verbatim, including the empty-output case (`ship_post_notice` prints nothing when
an open PR already exists for the current branch -- the notice is warn-only and conditional, not a
banner that always fires).

## Anti-Patterns

1. DO NOT write `PR.md` at a project-root path (`.planning/PR.md`) -- the generic gate evaluator
   only resolves artifacts inside `phaseDir` (14-RESEARCH.md Pitfall 4). `pr_status.py verify-post`
   already writes the correct phase-scoped path; this skill must never override that with a
   different destination.
2. DO NOT re-run the config gate's check more than once per invocation, and DO NOT call any other
   `pr_status.py` subcommand from either dispatch point beyond the one named in its own branch.
3. DO NOT create, open, or draft a PR from the `ship:post` dispatch point, and DO NOT read `PR.md`
   for the answer there -- a stale artifact could report a PR that has since been merged or closed
   (14-RESEARCH.md Pitfall 2). `ship_post_notice`'s probe is always live.
