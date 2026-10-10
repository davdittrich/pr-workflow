#!/usr/bin/env python3
"""pr-workflow: wrap the `gh` CLI to read PR/check status for the current
branch and write a gate-readable PR.md report.

stdlib-only (N5: no dependency beyond the `gh`/`git` binaries and the
Python 3 standard library). Every `gh`/`git` invocation is an argv list
passed to `subprocess.run` with shell execution left at its (disabled)
default -- no `gh`/`git` command is ever assembled as a shell string
(T-14-02, mirrors markdown-linting/scripts/lint.py's T-13-01 discipline).
"""
import argparse
import json
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

# D-04: two distinct, differently-worded notices so the user can tell which
# fix applies. Consumed by gh_available(); final wording/fail-open branch
# expansion (ship:post no-open-PR notice, etc.) is plan 14-02 scope -- kept
# as two distinct constants here so that scope stays separable.
NOTICE_GH_ABSENT = "gh not found on PATH -- install: https://cli.github.com"
NOTICE_GH_UNAUTH = "gh not authenticated -- run: gh auth login"

# Third fail-open notice: `gh` is on PATH, but a live gh/git call raised a
# transient error (timeout, OS-level spawn failure, or unparseable JSON).
# Distinct from the two guard notices above -- neither "absent" nor
# "unauthenticated" is a truthful description of this failure. Covers both
# gh_available()'s own `gh auth status` call and every call after it.
NOTICE_GH_ERROR = "gh command failed unexpectedly -- PR status unavailable this run"

# PRW-03: ship:post warn-only notice for "no open PR exists for this
# branch". Never printed together with any of the three notices above --
# ship_post_notice() checks gh_available() first and returns early on
# failure there.
NOTICE_NO_OPEN_PR = "no open PR exists for this branch -- ship proceeding, nothing created"


class GhCommandError(RuntimeError):
    """A `gh`/`git` call exited non-zero for a reason that is not itself an
    `OSError`/`TimeoutExpired`/`json.JSONDecodeError` -- e.g. `gh pr list`
    rate-limited or a transient 5xx surfaced as a clean non-zero exit, or
    `git branch --show-current` failing in a corrupted/misbehaving repo.
    Deliberately distinct from the plain `RuntimeError` `check_buckets()`
    raises for a genuine `gh pr checks` tool failure (WR-01): that case must
    stay uncaught, so it must not share this fail-open-eligible type."""


def find_project_root(start):
    """Walk up from `start` to the nearest ancestor containing `.planning/`.

    Guards T-14-03: every path this script reads or writes is confined to
    this resolved root, never derived unchecked from artifact text. Copied
    verbatim from markdown-linting/scripts/lint.py -- capabilities are
    independent and must not import across capability boundaries.
    """
    current = start.resolve()
    for _ in range(10):
        if (current / ".planning").is_dir():
            return current
        if current.parent == current:
            break
        current = current.parent
    raise ValueError(f"could not locate a .planning/ ancestor above {start}")


def confined(root, *parts):
    """Join parts onto root and reject any resolved escape (T-14-03)."""
    candidate = root.joinpath(*parts).resolve()
    try:
        candidate.relative_to(root)
    except ValueError:
        raise ValueError(f"path escapes project root: {candidate} not under {root}")
    return candidate


def gh_available():
    """PRW-04 two-tier guard: `gh` absent from PATH, then (only if present)
    a plain, non-`--json` `gh auth status` invocation (RESEARCH Pitfall 5 --
    `--json` mode always exits 0 even when unauthenticated, so it cannot be
    used to detect the unauthenticated case).

    Never raises. `gh auth status` is a live network call (it validates the
    token against the API), so it can time out or fail to spawn just like
    any later `gh` call -- and both callers invoke this guard *before* their
    fail-open `try`, so a raise here would escape as an uncaught traceback.
    At `execute:wave:post` that means `onError: skip` leaves the PREVIOUS
    run's report (possibly `pr_gate_ok: true`) stale on disk, which is
    exactly the failure T-14-01 exists to prevent. Degrade to the same
    honest sentinel a post-guard blow-up produces instead."""
    if shutil.which("gh") is None:
        return False, NOTICE_GH_ABSENT
    try:
        auth = subprocess.run(["gh", "auth", "status"], capture_output=True, text=True, errors="replace", timeout=15)
    except (subprocess.TimeoutExpired, OSError):
        return False, NOTICE_GH_ERROR
    if auth.returncode != 0:
        return False, NOTICE_GH_UNAUTH
    return True, None


def current_branch():
    result = subprocess.run(
        ["git", "branch", "--show-current"], capture_output=True, text=True, errors="replace", timeout=10,
    )
    if result.returncode != 0:
        raise GhCommandError(f"git branch --show-current failed: {result.stderr}")
    return result.stdout.strip()


def find_open_pr(branch):
    """D-02: `gh pr list --head <branch> --state open` (server-side state
    filter) resolves cleanly to `[]` for "no open PR" -- unlike `gh pr view`,
    which succeeds for closed/merged PRs too (RESEARCH Pitfall 2).

    WR-01: any non-zero exit (rate-limit, transient 5xx, or a genuine tool
    failure) raises `GhCommandError` rather than plain `RuntimeError`, so
    callers can fold it into the same fail-open path as a `gh` timeout --
    from the caller's perspective a `gh pr list` failure is exactly as
    "gh blew up after the availability guard passed" as a timeout is."""
    result = subprocess.run(
        ["gh", "pr", "list", "--head", branch, "--state", "open", "--json", "number,url"],
        capture_output=True, text=True, errors="replace", timeout=30,
    )
    if result.returncode != 0:
        raise GhCommandError(f"gh pr list failed: {result.stderr}")
    return json.loads(result.stdout)


def check_buckets(pr_number):
    """Parse `bucket` values from `gh pr checks <n> --json bucket`, branching
    on returncode before parsing (RESEARCH Pitfall 3: `--json` mode's exit
    code carries no pass/fail/pending signal). A non-zero exit whose stderr
    names the documented zero-checks strings maps to an empty bucket set
    (D-01 "zero checks configured" case); any other non-zero exit is a
    genuine tool failure and must not be laundered into a status."""
    result = subprocess.run(
        ["gh", "pr", "checks", str(pr_number), "--json", "bucket"],
        capture_output=True, text=True, errors="replace", timeout=30,
    )
    if result.returncode != 0:
        stderr = result.stderr.lower()
        if "no checks reported" in stderr or "no commit found" in stderr:
            return set()
        raise RuntimeError(f"gh pr checks failed: {result.stderr}")
    return {c.get("bucket") for c in json.loads(result.stdout)}


def rollup_pr_status(buckets):
    """Pure function: set[str] of `gh pr checks --json bucket` values ->
    D-01's four-state precedence (failing > pending > passing), extended per
    RESEARCH Pitfall 6 for the `bucket` vocabulary's `skipping`/`cancel`
    values (not literal in D-01's own state list): `skipping` contributes to
    passing, `cancel` contributes to failing alongside `fail`. An empty set
    (zero checks configured on an otherwise-open PR) is `passing`, never
    `none` -- `none` is reserved for "no open PR exists" (PRW-03)."""
    if buckets - {"pass", "skipping", "fail", "cancel", "pending"}:
        return "unavailable"  # unknown/missing/empty bucket: unmeasured, never passing
    if buckets & {"fail", "cancel"}:
        return "failing"
    if "pending" in buckets:
        return "pending"
    return "passing"  # only "pass"/"skipping" remain, or the set is empty


def derive_gate_ok(pr_status):
    """RESEARCH Pitfall 1: the gate predicate targets this derived boolean,
    never the raw four-state `pr_status` (single-scalar `equals` cannot
    express OR)."""
    return pr_status in ("none", "passing")


def _write_report(out_path, phase_dir, generated_at, generated_from, pr_status,
                   pr_gate_ok, pr_number, open_pr_count, unavailable_reason=None):
    """Full-overwrite report writer, shape cloned from lint.py::_write_report
    -- one place that fully overwrites PR.md, so no path can leave a prior
    status in place (T-14-01)."""
    lines = [
        "---",
        f"phase: {phase_dir.name}",
        f"pr_status: {pr_status}",
        f"pr_gate_ok: {'true' if pr_gate_ok else 'false'}",
        f"pr_number: {pr_number if pr_number is not None else 'null'}",
        f"open_pr_count: {open_pr_count}",
    ]
    # WR-03: every free-text frontmatter value goes through json.dumps --
    # its C-string-style escaping is valid inside a YAML double-quoted
    # scalar and keeps live text from breaking out of the value.
    # `generated_from` embeds the live branch name, which git allows `"` in
    # (e.g. `feature"pwn`). `unavailable_reason` embeds `gh`/`git` stderr,
    # which is multi-line: an unquoted newline there ends the scalar and
    # lets the REST of the stderr land as sibling frontmatter keys --
    # including a forged `pr_gate_ok: true` that a last-wins YAML reader
    # would hand straight to the ship:pre gate.
    if unavailable_reason is not None:
        lines.append(f"unavailable_reason: {json.dumps(unavailable_reason)}")
    lines.append(f"generated_from: {json.dumps(generated_from)}")
    lines.append(f"generated_at: {generated_at}")
    lines.append("---\n")
    body = (
        f"# PR.md: {phase_dir.name}\n\n"
        "> Regenerated every step. Do not hand-edit.\n"
    )
    out_path.write_text("\n".join(lines) + "\n" + body, encoding="utf-8")


def verify_post(phase_dir_arg):
    """execute:wave:post dispatch: fully overwrite
    `{phase_dir}/{padded_phase}-PR.md` from a live `gh` read.

    PRW-04 fail-open paths -- all print exactly one notice and still fully
    overwrite the report with `pr_status: unavailable`, `pr_gate_ok: false`:
    `gh` absent or unauthenticated (`gh_available()` guard), or any live
    `gh`/`git` call -- the guard's own `gh auth status` included -- raising
    `subprocess.TimeoutExpired`/`OSError`/`json.JSONDecodeError`/
    `GhCommandError`.
    An unmeasured status can never satisfy
    the ship:pre gate (T-14-01) -- this is a **deliberate divergence** from
    beads/scripts/sync.py::regenerate_beads_md (which leaves a prior report
    untouched when `bd` is unavailable), mirroring markdown-linting's
    lint.py::verify_post docstring on the same point. A future editor must
    not "fix" this back to a leave-it-alone behavior.

    `check_buckets()` raises `RuntimeError` for a `gh pr checks` non-zero
    exit whose stderr matches neither documented zero-checks string
    (RESEARCH Pitfall 3) -- a genuine tool failure that is not the
    zero-checks case must not be laundered into a `pr_status` of `passing`
    or `failing`. It IS caught here, separately from the fail-open tuple
    above, and returns exit 1 (not 0): under this dispatch point's
    `onError: skip` contract, an UNCAUGHT exception does not fail the
    phase -- it only skips regenerating PR.md, leaving whatever the
    PREVIOUS successful run wrote (e.g. `pr_gate_ok: true`) stale on disk,
    which is exactly the "stale status satisfies the gate" failure T-14-01
    exists to prevent. Catching and still writing the `unavailable`
    sentinel keeps both properties: the failure is loud (nonzero exit,
    printed message) AND the artifact is never stale.
    """
    phase_dir = Path(phase_dir_arg).resolve()
    project_root = find_project_root(phase_dir)
    padded_phase = phase_dir.name.split("-", 1)[0]
    out_path = confined(project_root, phase_dir.relative_to(project_root), f"{padded_phase}-PR.md")
    generated_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    available, reason = gh_available()
    if not available:
        print(reason)
        _write_report(
            out_path, phase_dir, generated_at,
            generated_from=f"none ({reason})",
            pr_status="unavailable", pr_gate_ok=False,
            pr_number=None, open_pr_count=0,
            unavailable_reason=reason,
        )
        return 0

    try:
        branch = current_branch()
        prs = find_open_pr(branch)
        generated_from = f"gh pr list --head {branch} --state open --json number,url"

        if not prs:
            _write_report(
                out_path, phase_dir, generated_at, generated_from=generated_from,
                pr_status="none", pr_gate_ok=True,
                pr_number=None, open_pr_count=0,
            )
            print("PR.md regenerated: no open PR (pr_status=none)")
            return 0

        # D-02/RESEARCH A4: more than one open PR can target one head
        # branch (different base branches). Take the first entry and record
        # the count rather than silently collapsing it.
        open_pr_count = len(prs)
        pr_number = prs[0]["number"]
        checks_generated_from = generated_from + f"; gh pr checks {pr_number} --json bucket"
        buckets = check_buckets(pr_number)
        pr_status = rollup_pr_status(buckets)
        pr_gate_ok = derive_gate_ok(pr_status)
        generated_from = checks_generated_from
    except (subprocess.TimeoutExpired, OSError, json.JSONDecodeError, GhCommandError) as exc:
        # GhCommandError IS-A RuntimeError (see its class docstring) --
        # this clause must stay ordered BEFORE the bare `except RuntimeError`
        # below so a GhCommandError (fail-open eligible) is never caught by
        # the more general clause meant only for check_buckets()'s plain
        # RuntimeError (a genuine, non-fail-open tool failure).
        print(NOTICE_GH_ERROR)
        _write_report(
            out_path, phase_dir, generated_at,
            generated_from="none (gh command failed)",
            pr_status="unavailable", pr_gate_ok=False,
            pr_number=None, open_pr_count=0,
            unavailable_reason=f"{type(exc).__name__}: {exc}",
        )
        return 0
    except RuntimeError as exc:
        print(f"gh pr checks failed: {exc}")
        _write_report(
            out_path, phase_dir, generated_at,
            generated_from="none (gh pr checks failed)",
            pr_status="unavailable", pr_gate_ok=False,
            pr_number=None, open_pr_count=0,
            unavailable_reason=f"RuntimeError: {exc}",
        )
        return 1
    except Exception as exc:  # noqa: BLE001 -- see below
        # Backstop for the impossible-but-not-inexpressible: gh returning
        # JSON that parses yet lacks `number`/`bucket` (KeyError), or any
        # other unforeseen failure. Deliberately broad, for the same reason
        # the two clauses above exist: under `onError: skip` an UNCAUGHT
        # exception silently preserves the previous run's report, so "let it
        # crash" here means "let a stale pr_gate_ok: true satisfy ship:pre".
        # Loud (exit 1, printed) but never stale -- same trade as the
        # RuntimeError clause.
        print(f"pr status read failed unexpectedly: {type(exc).__name__}: {exc}")
        _write_report(
            out_path, phase_dir, generated_at,
            generated_from="none (unexpected failure)",
            pr_status="unavailable", pr_gate_ok=False,
            pr_number=None, open_pr_count=0,
            unavailable_reason=f"{type(exc).__name__}: {exc}",
        )
        return 1

    _write_report(
        out_path, phase_dir, generated_at, generated_from=generated_from,
        pr_status=pr_status, pr_gate_ok=pr_gate_ok,
        pr_number=pr_number, open_pr_count=open_pr_count,
    )
    print(f"PR.md regenerated: pr_status={pr_status}")
    return 0


def ship_post_notice(phase_dir_arg):
    """ship:post dispatch (PRW-03): print a warn-only notice when the
    current branch has no open PR. `phase_dir_arg` is accepted only for CLI
    signature parity with `verify-post` -- this function reads and writes
    no file (never `PR.md`, which could be stale for a PR merged or closed
    since the last `execute:wave:post` run -- RESEARCH Pitfall 2) and
    mutates no git/GitHub state. Returns 0 on every path; issues no `gh`
    subcommand beyond `gh_available()`'s `auth status` and `find_open_pr`'s
    `pr list`.

    CR-01: the two live calls are wrapped in the same fail-open except tuple
    as `verify_post()` -- a `gh pr list`/`git branch` blow-up after the
    availability guard already passed must not crash `ship:post`'s
    onError: "skip" dispatch with an uncaught traceback.
    """
    del phase_dir_arg  # unused: no file is read or written on this path
    available, reason = gh_available()
    if not available:
        print(reason)
        return 0

    try:
        branch = current_branch()
        prs = find_open_pr(branch)
    except (subprocess.TimeoutExpired, OSError, json.JSONDecodeError, GhCommandError):
        print(NOTICE_GH_ERROR)
        return 0
    if not prs:
        print(f"{NOTICE_NO_OPEN_PR} (branch: {branch})")
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(prog="pr_status.py")
    sub = parser.add_subparsers(dest="command", required=True)

    verify_p = sub.add_parser(
        "verify-post",
        help="Fully overwrite {phase_dir}/{padded_phase}-PR.md from a live gh PR/check status read",
    )
    verify_p.add_argument("phase_dir")

    ship_p = sub.add_parser(
        "ship-post-notice",
        help="Print a warn-only notice when the current branch has no open PR (writes nothing)",
    )
    ship_p.add_argument("phase_dir")

    args = parser.parse_args(argv)

    if args.command == "verify-post":
        return verify_post(args.phase_dir)
    if args.command == "ship-post-notice":
        return ship_post_notice(args.phase_dir)
    return 1


if __name__ == "__main__":
    sys.exit(main())
