# pr-workflow

Wraps gh CLI to read PR/check status for the current branch; execute:wave:post writes a gate-readable PR.md report, ship:pre gates advisorily on it.

## What it does

Two lifecycle steps and one gate:

- **`execute:wave:post`** runs `.gsd/capabilities/pr-workflow/scripts/pr_status.py verify-post
  <phase_dir>`, which regenerates a phase-scoped `{phase_dir}/{padded_phase}-PR.md` report on every
  invocation from a live `gh` read of the current branch's open PR and its check status. The report
  is fully overwritten every run, never merged with a prior hand edit, so a stale status can never
  linger.
- **`ship:post`** runs the same reporter in warn-only mode and prints one notice when no open PR
  exists for the current branch. This step never reads or writes `PR.md` itself — its answer is
  always a fresh `gh pr list` re-probe, so the notice can never reflect a possibly-stale generated
  artifact.
- **`ship:pre`** is an advisory gate that reads `PR.md`'s derived `pr_gate_ok` boolean frontmatter
  field, not the raw four-state `pr_status` field — a single-scalar equality predicate cannot
  express the OR across the two passing states (`none`, `passing`), so the gate targets the derived
  boolean instead.

Two config keys, both in `capability.json`'s `config` block and overridable in a project's
`.planning/config.json`:

| Key | Default | Effect |
|-----|---------|--------|
| `pr-workflow.enabled` | `true` | Master toggle. `false` skips the `execute:wave:post` and `ship:post` steps entirely. |
| `pr-workflow.ship_gate` | `true` | Whether the advisory `ship:pre` gate is even registered. `false` disables the gate but leaves the reporting steps running. |

## Requirements

- **The `gh` CLI** — https://cli.github.com. A first-class prerequisite in its own right: without
  it on `PATH`, `execute:wave:post` writes `pr_status: unavailable`/`pr_gate_ok: false` and prints
  `gh not found on PATH -- install: https://cli.github.com`; it exits 0 and never crashes the step.
- **An authenticated `gh` session** (`gh auth login`) — a separate, first-class prerequisite. `gh`
  being installed but not authenticated is a distinct failure mode with its own notice, `gh not
  authenticated -- run: gh auth login`, and the same fail-open write: exit 0, `pr_status:
  unavailable`, `pr_gate_ok: false`. A stale status is never presented as current in either case.
- **Python 3**, standard library only — the capability adds no third-party Python dependency.
- **gsd-core >= 1.6.0** (`capability.json`'s `engines` floor).
- **`git`** on `PATH` — used to resolve the current branch.

## Install

```bash
claude plugin marketplace add davdittrich/gsd-beads
claude plugin install pr-workflow@gsd-beads -y
```

The marketplace stays hosted at `davdittrich/gsd-beads` even though this plugin now lives in its
own repository — the marketplace entry's source simply points here.

## Uninstall

```bash
claude plugin uninstall pr-workflow -y
```

## Caveats

- **The `ship:pre` gate is advisory and NEVER blocks.** `capability.json` declares `blocking: false`
  and `onError: "skip"` — a false `pr_gate_ok` prints a warning and the ship proceeds regardless.
  This is the opposite of a hard gate: install this capability expecting a nudge, not an enforcement
  mechanism.
- **This capability only ever reads through `gh`.** It opens, edits, and merges nothing — every
  `gh` invocation is a read (`gh auth status`, `gh pr list`, `gh pr checks`).
- **The SessionStart hook re-grants the capability bundle at user scope on every session start** and
  exits silently when the bundle is unchanged, so repeated installs are cheap and idempotent.
- **Marketplace installation copies the cloned repository into the installer's local plugin cache.**
  This is documented Claude Code behavior this repository does not control.

## License

MIT — see [LICENSE](LICENSE).

## gsd-core

`pr-workflow` is a capability for [gsd-core](https://github.com/open-gsd/gsd-core). See that
project for the base planning framework this capability extends.
