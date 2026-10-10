---
name: gsd-pr-workflow-report
description: "Report PR state via gh: at execute:wave:post regenerate the phase's PR.md from the branch's open PR and checks; at ship:post print a notice when the branch has no open PR"
allowed-tools:
  - Read
  - Bash
---

## Step 1 -- Enabled check

Run `gsd-tools config-get pr-workflow.enabled --default true` (it resolves the nested
`config["pr-workflow"]["enabled"]`). On output `false`, print
`pr-workflow reporting is disabled (pr-workflow.enabled); nothing regenerated.` and stop.

## Step 2 -- Pick the subcommand

SUB is `verify-post` when dispatched at `execute:wave:post`, `ship-post-notice` when dispatched at `ship:post`.

## Step 3 -- Run

Phase directory: `$ARGUMENTS` when given, else the `PHASE_DIR` of the phase the dispatching
workflow is running. When neither exists, print `pr-workflow: phase directory unknown` and stop.

```bash
PR_PY=""
for candidate in \
  "${CLAUDE_PROJECT_DIR:-}/.gsd/capabilities/pr-workflow/scripts/pr_status.py" \
  "${GSD_HOME:-$HOME}/.gsd/capabilities/pr-workflow/scripts/pr_status.py" \
  "${CLAUDE_PLUGIN_ROOT:-}/scripts/pr_status.py"
do
  if [ -f "$candidate" ]; then PR_PY="$candidate"; break; fi
done
if [ -z "$PR_PY" ]; then
  echo "pr-workflow: pr_status.py not found in project, global, or plugin roots" >&2
  exit 1
fi
python3 -I "$PR_PY" "$SUB" <phase directory>
```

Exit 0: print stdout verbatim, empty included (`ship-post-notice` prints nothing when an open PR
exists). Exit 1: print stdout and report the failure; exit 1 means the script could not write the
`unavailable` sentinel to PR.md.

## Rules

1. `verify-post` alone writes `PR.md`, into the phase directory where the ship:pre gate looks.
2. At `ship:post`, probe live and leave `PR.md` unread (it may describe a PR merged since).
3. This skill only reads PR state; it opens no PR.
