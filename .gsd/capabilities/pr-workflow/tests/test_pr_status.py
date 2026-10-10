"""Tests for .gsd/capabilities/pr-workflow/scripts/pr_status.py.

Stdlib unittest only (N5, mirrors markdown-linting/tests/test_lint.py's R-01
finding): the suite must pass under `python3 -m unittest discover` with no
third-party test runner. pr_status.py's parent directory is put on sys.path
at module import so no package __init__.py and no install step is needed --
mirrors test_lint.py's own sys.path.insert technique.
"""
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import pr_status  # noqa: E402

FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures"

def _pr(number, cross=False):
    return {"number": number, "url": f"https://github.com/x/y/pull/{number}", "isCrossRepository": cross}


PR_LIST_ONE_OPEN = json.dumps([_pr(42)])


def _write_phase_dir(tmp_path, phase_dir_name="14-pr-workflow-capability-dogfood"):
    """Lay out a minimal .planning/phases/<phase_dir_name>/ tree under
    tmp_path so find_project_root resolves -- mirrors test_lint.py's
    _write_phase_dir, scoped to just what verify_post needs."""
    phase_dir = tmp_path / ".planning" / "phases" / phase_dir_name
    phase_dir.mkdir(parents=True)
    return phase_dir


def _completed(stdout, returncode=0, stderr=""):
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr=stderr)


def _fixture(name):
    return (FIXTURES_DIR / name).read_text(encoding="utf-8")


_STAGE_ARGV = {
    ("gh", "pr", "list"): "list",
    ("gh", "pr", "checks"): "checks",
}


def _verify(phase_dir, fn="verify_post", which="/usr/bin/gh", branch="feature-x\n",
            pr_list=PR_LIST_ONE_OPEN, checks=None):
    """Run pr_status.<fn>(phase_dir) against one fake gh/git. Each stage value
    is a CompletedProcess, an exception instance (raised), or a str (stdout,
    rc 0). Returns (exit_code, captured stdout, argv list of every call)."""
    stages = {
        "branch": branch, "list": pr_list,
        "checks": _fixture("checks_pass.json") if checks is None else checks,
    }
    calls = []

    def fake_run(argv, **kwargs):
        calls.append(argv)
        stage = "branch" if argv[0] == "git" else _STAGE_ARGV.get(tuple(argv[:3]))
        if stage is None:
            raise AssertionError(f"unexpected call: {argv}")
        value = stages[stage]
        if isinstance(value, BaseException):
            raise value
        return _completed(value) if isinstance(value, str) else value

    captured = io.StringIO()
    with mock.patch("shutil.which", return_value=which):
        with mock.patch.object(pr_status.subprocess, "run", side_effect=fake_run):
            with mock.patch("sys.stdout", captured):
                exit_code = getattr(pr_status, fn)(str(phase_dir))
    return exit_code, captured.getvalue(), calls


def _report(phase_dir):
    return _frontmatter((phase_dir / "14-PR.md").read_text(encoding="utf-8"))


def _without_timestamp(text):
    """Drop the live `generated_at:` line so two runs are byte-comparable."""
    return "\n".join(l for l in text.splitlines() if not l.startswith("generated_at:"))


def _frontmatter(text):
    """Parse the report's frontmatter into a dict the way a last-wins YAML
    reader would -- later duplicate keys overwrite earlier ones. That is the
    lens the ship:pre gate sees, so it is the lens injection must be tested
    through."""
    lines = text.splitlines()
    assert lines[0] == "---", "report must open with a frontmatter fence"
    end = lines.index("---", 1)
    fields = {}
    for line in lines[1:end]:
        if ": " in line:
            key, value = line.split(": ", 1)
            fields[key] = value
    return fields


class TestRollupPrecedence(unittest.TestCase):
    """D-01 precedence (failing > pending > passing) plus Pitfall 6's
    skipping/cancel bucket mapping, as pure-function assertions with no I/O."""

    def test_all_pass_is_passing(self):
        self.assertEqual(pr_status.rollup_pr_status({"pass"}), "passing")

    def test_pass_and_skipping_is_passing(self):
        self.assertEqual(pr_status.rollup_pr_status({"pass", "skipping"}), "passing")

    def test_pass_and_pending_is_pending(self):
        self.assertEqual(pr_status.rollup_pr_status({"pass", "pending"}), "pending")

    def test_pass_pending_fail_precedence_is_failing(self):
        self.assertEqual(pr_status.rollup_pr_status({"pass", "pending", "fail"}), "failing")

    def test_cancel_is_failing(self):
        self.assertEqual(pr_status.rollup_pr_status({"cancel"}), "failing")

    def test_unknown_missing_or_empty_bucket_is_unavailable(self):
        # kilo.4: only the documented vocabulary may roll up; anything else
        # is unmeasured and must never reach pr_gate_ok:true.
        for buckets in ({"weird"}, {"pass", "weird"}, {None}, {""}):
            with self.subTest(buckets=buckets):
                self.assertEqual(pr_status.rollup_pr_status(buckets), "unavailable")

    def test_empty_set_is_passing(self):
        # D-01: zero checks configured on an otherwise-open PR is passing,
        # never none -- none is reserved for "no open PR exists".
        self.assertEqual(pr_status.rollup_pr_status(set()), "passing")


class TestDeriveGateOk(unittest.TestCase):
    def test_none_and_passing_are_gate_ok(self):
        self.assertTrue(pr_status.derive_gate_ok("none"))
        self.assertTrue(pr_status.derive_gate_ok("passing"))

    def test_pending_failing_unavailable_are_not_gate_ok(self):
        self.assertFalse(pr_status.derive_gate_ok("pending"))
        self.assertFalse(pr_status.derive_gate_ok("failing"))
        self.assertFalse(pr_status.derive_gate_ok("unavailable"))


class TestCurrentBranch(unittest.TestCase):
    """WR-02: `current_branch()` must not silently mask a `git` failure as
    an empty branch string -- it raises `GhCommandError` on non-zero exit."""

    def test_raises_on_nonzero_exit(self):
        with mock.patch(
            "subprocess.run",
            return_value=_completed("", 1, stderr="fatal: not a git repository"),
        ):
            with self.assertRaises(pr_status.GhCommandError):
                pr_status.current_branch(Path("."))


GENERATED_AT_RE = r"(?m)^generated_at: \d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$"
STALE_PASS = "---\npr_status: passing\npr_gate_ok: true\n---\n\nstale\n"


class TestVerifyPost(unittest.TestCase):
    """verify_post() end-to-end against one faked gh/git (see _verify)."""

    def _run(self, **stages):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        phase_dir = _write_phase_dir(Path(tmp.name))
        exit_code, output, _ = _verify(phase_dir, **stages)
        return phase_dir, exit_code, output

    def test_no_open_pr_writes_none_and_gate_ok(self):
        phase_dir, exit_code, _ = self._run(pr_list=_fixture("pr_list_empty.json"))
        self.assertEqual(exit_code, 0)
        text = (phase_dir / "14-PR.md").read_text(encoding="utf-8")
        fields = _frontmatter(text)
        self.assertEqual(fields["pr_status"], "none")
        self.assertEqual(fields["pr_gate_ok"], "true")
        self.assertIn("generated_from", fields)
        self.assertRegex(text, GENERATED_AT_RE)

    def test_open_pr_failing_checks_writes_failing_and_gate_not_ok(self):
        phase_dir, exit_code, _ = self._run(checks=_fixture("checks_fail.json"))
        self.assertEqual(exit_code, 0)
        fields = _report(phase_dir)
        self.assertEqual(fields["pr_status"], "failing")
        self.assertEqual(fields["pr_gate_ok"], "false")

    def test_branch_with_embedded_quote_does_not_break_frontmatter(self):
        # WR-03: git allows `"` in branch names; generated_from must escape it
        # instead of terminating the quoted YAML scalar early.
        phase_dir, exit_code, _ = self._run(
            branch='feature"pwn\n', pr_list=_fixture("pr_list_empty.json"))
        self.assertEqual(exit_code, 0)
        text = (phase_dir / "14-PR.md").read_text(encoding="utf-8")
        line = next(l for l in text.splitlines() if l.startswith("generated_from:"))
        self.assertEqual(
            line,
            'generated_from: "gh pr list --head=feature\\"pwn --state open --limit 100'
            ' --json number,url,isCrossRepository"',
        )
        # frontmatter block boundaries intact -- no unquoted newline leaked
        self.assertEqual(text.count("---"), 2)

    def test_rerun_overwrites_not_appends(self):
        with tempfile.TemporaryDirectory() as tmp:
            phase_dir = _write_phase_dir(Path(tmp))
            texts = []
            for _ in range(2):
                _verify(phase_dir, pr_list=_fixture("pr_list_empty.json"))
                texts.append((phase_dir / "14-PR.md").read_text(encoding="utf-8"))
            # generated_at is live: comparing it across runs would fail
            # whenever they straddle a second boundary. Every other byte must
            # be identical -- that is what "overwrites, not appends" means.
            self.assertEqual([t.count("---") for t in texts], [2, 2])
            self.assertEqual(_without_timestamp(texts[0]), _without_timestamp(texts[1]))


class TestOpenPrReport(unittest.TestCase):
    """The report's non-status fields (`pr_number`, `open_pr_count`) and the
    passing/pending/skipping rollups end-to-end -- the gate reads what
    `_write_report` emits, not what `rollup_pr_status` returns."""

    def _run(self, checks_fixture, pr_list=PR_LIST_ONE_OPEN):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        phase_dir = _write_phase_dir(Path(tmp.name))
        exit_code, _, _ = _verify(phase_dir, pr_list=pr_list, checks=_fixture(checks_fixture))
        self.assertEqual(exit_code, 0)
        return _report(phase_dir)

    def test_all_checks_pass_reports_number_and_count(self):
        fields = self._run("checks_pass.json")
        self.assertEqual(fields["pr_status"], "passing")
        self.assertEqual(fields["pr_gate_ok"], "true")
        self.assertEqual(fields["pr_number"], "42")
        self.assertEqual(fields["open_pr_count"], "1")

    def test_pending_checks_are_not_gate_ok(self):
        fields = self._run("checks_pending.json")
        self.assertEqual(fields["pr_status"], "pending")
        self.assertEqual(fields["pr_gate_ok"], "false")

    def test_skipping_checks_are_gate_ok(self):
        fields = self._run("checks_skipping.json")
        self.assertEqual(fields["pr_status"], "passing")
        self.assertEqual(fields["pr_gate_ok"], "true")

    def test_check_with_unknown_missing_or_empty_bucket_is_not_gate_ok(self):
        for checks in ('[{"bucket": "weird"}]', "[{}]", '[{"bucket": ""}]'):
            with self.subTest(checks=checks):
                with tempfile.TemporaryDirectory() as tmp:
                    phase_dir = _write_phase_dir(Path(tmp))
                    _verify(phase_dir, checks=checks)
                    fields = _report(phase_dir)
                    self.assertEqual(fields["pr_status"], "unavailable")
                    self.assertEqual(fields["pr_gate_ok"], "false")

    def test_phase_dir_name_cannot_forge_frontmatter_keys(self):
        # kilo.7: `phase:` embedded the directory name unquoted, so a newline
        # in it planted a key ahead of the real pr_gate_ok.
        with tempfile.TemporaryDirectory() as tmp:
            phase_dir = _write_phase_dir(Path(tmp), "02-a\npr_gate_ok: true")
            _verify(phase_dir, checks=_fixture("checks_fail.json"))
            text = (phase_dir / "02-PR.md").read_text(encoding="utf-8")
            frontmatter = text.split("---\n")[1]
            self.assertEqual([l for l in frontmatter.splitlines() if l.startswith("pr_gate_ok:")], ["pr_gate_ok: false"])
            self.assertEqual(_frontmatter(text)["pr_gate_ok"], "false")

    def test_two_open_prs_record_count_and_take_first(self):
        # D-02/RESEARCH A4: two open PRs on one head must not collapse silently.
        fields = self._run("checks_pass.json", pr_list=json.dumps([_pr(42), _pr(43)]))
        self.assertEqual(fields["pr_number"], "42")
        self.assertEqual(fields["open_pr_count"], "2")


class TestFailOpen(unittest.TestCase):
    """PRW-04: `gh` absent or unauthenticated, or a live `gh` call blowing up
    with a transient error, all degrade to exit 0, exactly one notice, and a
    sentinel report that fully overwrites any prior content -- mirrors
    markdown-linting's TestFailOpen shape (test_lint.py)."""

    def _run(self, stale=False, **kwargs):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        phase_dir = _write_phase_dir(Path(tmp.name))
        if stale:
            (phase_dir / "14-PR.md").write_text(STALE_PASS, encoding="utf-8")
        exit_code, output, calls = _verify(phase_dir, **kwargs)
        self.assertEqual(exit_code, 0)
        return _report(phase_dir), output, calls

    def assertSentinel(self, fields):
        self.assertEqual(fields["pr_status"], "unavailable")
        self.assertEqual(fields["pr_gate_ok"], "false")

    def test_gh_absent_fail_open(self):
        fields, output, calls = self._run(which=None)
        self.assertEqual(output.count(pr_status.NOTICE_GH_ABSENT), 1)
        self.assertEqual(calls, [])
        self.assertSentinel(fields)
        self.assertNotEqual(fields["unavailable_reason"], "")

    def test_gh_unauthenticated_fail_open(self):
        # kilo.15: no separate `gh auth status` network call; unauthenticated
        # surfaces as a failing `gh pr list` and degrades to the sentinel.
        failing = _completed("", 1, stderr="To get started with GitHub CLI, please run: gh auth login")
        fields, output, calls = self._run(pr_list=failing)
        self.assertEqual(output.count(pr_status.NOTICE_GH_ERROR), 1)
        self.assertSentinel(fields)
        self.assertNotIn(["gh", "auth", "status"], [c[:3] for c in calls])

    def test_stale_passing_status_replaced_by_sentinel(self):
        fields, _, _ = self._run(stale=True, which=None)
        self.assertSentinel(fields)

    def test_gh_pr_list_timeout_fail_open(self):
        fields, output, _ = self._run(pr_list=subprocess.TimeoutExpired(cmd="gh", timeout=30))
        self.assertEqual(output.count(pr_status.NOTICE_GH_ERROR), 1)
        self.assertSentinel(fields)

    def test_gh_pr_list_nonzero_exit_fail_open(self):
        # WR-01: a GhCommandError (non-zero exit, not a TimeoutExpired) must
        # fail open (exit 0), never as the `gh pr checks` RuntimeError clause
        # -- two different failures must not collapse into one behavior.
        fields, output, _ = self._run(pr_list=_completed("", 1, stderr="HTTP 403: rate limited"))
        self.assertEqual(output.count(pr_status.NOTICE_GH_ERROR), 1)
        self.assertNotIn("gh pr checks failed", output)
        self.assertSentinel(fields)

    def test_checks_zero_checks_stderr_is_passing(self):
        fields, _, _ = self._run(
            checks=_completed("", 1, stderr="no checks reported on 'feature-x' branch"))
        self.assertEqual(fields["pr_status"], "passing")
        self.assertEqual(fields["pr_gate_ok"], "true")

    def test_checks_unrelated_stderr_writes_sentinel(self):
        # check_buckets() raises plain RuntimeError for a genuine gh pr
        # checks failure; verify_post must catch it, since under onError:skip
        # an uncaught exception would leave the prior (possibly
        # pr_gate_ok: true) report in place. Loud but never stale.
        fields, output, _ = self._run(
            stale=True, checks=_completed("", 1, stderr="HTTP 500 from checks.go:1:1"))
        self.assertIn("gh pr checks failed", output)
        self.assertSentinel(fields)
        self.assertIn('RuntimeError', fields["unavailable_reason"])

    def test_gh_stderr_cannot_forge_frontmatter_keys(self):
        # WR-03, second injection sink: unavailable_reason embeds multi-line
        # gh stderr. Unquoted, everything after the first newline lands as
        # sibling frontmatter keys -- a stderr line reading `pr_gate_ok: true`
        # would reach the ship:pre gate via a last-wins YAML reader.
        evil = _completed("", 1, stderr="boom\npr_gate_ok: true\npr_status: passing\n")
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        phase_dir = _write_phase_dir(Path(tmp.name))
        _verify(phase_dir, pr_list=evil)
        text = (phase_dir / "14-PR.md").read_text(encoding="utf-8")
        self.assertSentinel(_frontmatter(text))
        self.assertEqual(text.count("---"), 2)
        self.assertIn("boom", _frontmatter(text)["unavailable_reason"])

    def test_malformed_pr_json_writes_sentinel(self):
        # gh JSON that parses but lacks `number` raises KeyError, outside the
        # fail-open tuple; the outer backstop must still write the sentinel.
        fields, _, _ = self._run(stale=True, pr_list=json.dumps([{"url": "https://example.invalid/1", "isCrossRepository": False}]))
        self.assertSentinel(fields)
        self.assertIn("KeyError", fields["unavailable_reason"])


class TestShipPostNotice(unittest.TestCase):
    """PRW-03: `ship:post` warn-only notice when no open PR exists for the
    current branch. Writes no file, mutates no git/GitHub state, and never
    reads `PR.md` -- the probe is always live (RESEARCH Pitfall 2)."""

    def _run(self, **kwargs):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        phase_dir = _write_phase_dir(Path(tmp.name))
        exit_code, output, calls = _verify(phase_dir, fn="ship_post_notice", **kwargs)
        self.assertEqual(exit_code, 0)
        return output, calls

    def test_no_open_pr_prints_one_notice_naming_branch(self):
        output, calls = self._run(branch="feature-y\n", pr_list=_fixture("pr_list_empty.json"))
        lines = [line for line in output.splitlines() if line.strip()]
        self.assertEqual(len(lines), 1)
        self.assertIn(pr_status.NOTICE_NO_OPEN_PR, output)
        self.assertIn("feature-y", output)
        for argv in calls:
            for forbidden in ("create", "merge", "comment", "edit", "review"):
                self.assertNotIn(forbidden, argv)

    def test_open_pr_prints_nothing(self):
        output, _ = self._run(branch="feature-y\n")
        self.assertEqual(output, "")

    def test_gh_pr_list_failure_fail_open(self):
        # CR-01: a gh blow-up after the guard must print NOTICE_GH_ERROR and
        # exit 0, not crash ship:post (cf. TestFailOpen timeout test).
        output, _ = self._run(
            branch="feature-y\n", pr_list=subprocess.TimeoutExpired(cmd="gh", timeout=30))
        self.assertEqual(output.count(pr_status.NOTICE_GH_ERROR), 1)
        self.assertNotIn(pr_status.NOTICE_NO_OPEN_PR, output)

    def test_gh_absent_prints_prw04_notice_not_no_pr_notice(self):
        output, calls = self._run(which=None)
        self.assertEqual(output.count(pr_status.NOTICE_GH_ABSENT), 1)
        self.assertNotIn(pr_status.NOTICE_NO_OPEN_PR, output)
        self.assertEqual(calls, [])

    def test_never_reads_pr_md(self):
        # A stale PR.md claiming an open PR must not influence the notice:
        # the probe is live, and the file is left untouched.
        with tempfile.TemporaryDirectory() as tmp:
            phase_dir = _write_phase_dir(Path(tmp))
            report = phase_dir / "14-PR.md"
            report.write_text(STALE_PASS + "pr_number: 42\n", encoding="utf-8")
            exit_code, output, _ = _verify(
                phase_dir, fn="ship_post_notice", pr_list=_fixture("pr_list_empty.json"))
            self.assertEqual(exit_code, 0)
            self.assertIn(pr_status.NOTICE_NO_OPEN_PR, output)
            self.assertEqual(report.read_text(encoding="utf-8"), STALE_PASS + "pr_number: 42\n")


class TestNonUtf8Output(unittest.TestCase):
    """kilo.3: `text=True` without errors= made b'\\xff' from gh/git raise
    UnicodeDecodeError past every fail-open guard, keeping a stale report."""

    def _run_with_fake_bins(self, **stages):
        """Run verify_post against real fake `gh`/`git` executables on PATH so
        the real subprocess decode path is exercised. stages maps a stage
        (branch, list, checks) to shell that emits non-UTF8 bytes."""
        scripts = {
            "gh": 'case "$1 $2" in\n'
                  f'  "pr list") {stages.get("list", "echo [{{\\\"number\\\":1}}]")} ;;\n'
                  f'  "pr checks") {stages.get("checks", "echo [{{\\\"bucket\\\":\\\"pass\\\"}}]")} ;;\n'
                  "esac\n",
            "git": f'{stages.get("branch", "echo feature-x")}\n',
        }
        with tempfile.TemporaryDirectory() as tmp:
            bindir = Path(tmp) / "bin"
            bindir.mkdir()
            for name, body in scripts.items():
                (bindir / name).write_text("#!/bin/sh\n" + body, encoding="utf-8")
                (bindir / name).chmod(0o755)
            phase_dir = _write_phase_dir(Path(tmp))
            report = phase_dir / "14-PR.md"
            report.write_text("---\npr_status: passing\npr_gate_ok: true\n---\n", encoding="utf-8")
            with mock.patch.dict(os.environ, {"PATH": f"{bindir}:{os.environ['PATH']}"}):
                with mock.patch("sys.stdout", io.StringIO()):
                    pr_status.verify_post(str(phase_dir))
            return _frontmatter(report.read_text(encoding="utf-8"))

    def test_non_utf8_bytes_at_every_stage_still_write_sentinel(self):
        bad_err = "printf '\\377' >&2; exit 1"
        bad_out = "printf '\\377'"
        for stage, body in {
            "branch": bad_err, "list": bad_err, "list-stdout": bad_out,
            "checks": bad_err,
        }.items():
            with self.subTest(stage=stage):
                key = "list" if stage == "list-stdout" else stage
                fields = self._run_with_fake_bins(**{key: body})
                self.assertEqual(fields["pr_status"], "unavailable")
                self.assertEqual(fields["pr_gate_ok"], "false")
                # decoded with errors=replace, not rescued by the backstop
                self.assertNotIn("UnicodeDecodeError", fields.get("unavailable_reason", ""))


class TestFindOpenPr(unittest.TestCase):
    """kilo.6: PR lookup must be exact (--head=<b>, owner-qualified, paged
    past gh's default 30) and fail closed on an unusable branch or JSON."""

    def _verify(self, **stages):
        with tempfile.TemporaryDirectory() as tmp:
            phase_dir = _write_phase_dir(Path(tmp))
            _, _, calls = _verify(phase_dir, **stages)
            return _report(phase_dir), calls

    def test_detached_head_is_unavailable_without_querying_gh(self):
        fields, calls = self._verify(branch="\n")
        self.assertEqual(fields["pr_status"], "unavailable")
        self.assertEqual(fields["pr_gate_ok"], "false")
        self.assertNotIn(["gh", "pr", "list"], [c[:3] for c in calls])

    def test_pr_list_argv_is_exact_and_unpaged_limit_raised(self):
        _, calls = self._verify()
        argv = next(c for c in calls if c[:3] == ["gh", "pr", "list"])
        self.assertIn("--head=feature-x", argv)
        self.assertEqual(argv[argv.index("--limit") + 1], "100")

    def test_fork_pr_with_same_branch_name_is_ignored(self):
        fields, _ = self._verify(pr_list=json.dumps([_pr(7, cross=True), _pr(42)]))
        self.assertEqual(fields["pr_number"], "42")
        self.assertEqual(fields["open_pr_count"], "1")

    def test_only_fork_pr_means_no_open_pr(self):
        fields, _ = self._verify(pr_list=json.dumps([_pr(7, cross=True)]))
        self.assertEqual(fields["pr_status"], "none")

    def test_non_list_json_is_unavailable(self):
        for body in ("null", "{}", '"x"'):
            with self.subTest(body=body):
                fields, _ = self._verify(pr_list=body)
                self.assertEqual(fields["pr_status"], "unavailable")
                self.assertEqual(fields["pr_gate_ok"], "false")


class TestProjectRootGit(unittest.TestCase):
    """kilo.17: the branch is read with `git -C <project root>`, never from
    whatever directory the dispatcher happened to be in."""

    def test_git_branch_runs_in_project_root_not_cwd(self):
        with tempfile.TemporaryDirectory() as tmp:
            phase_dir = _write_phase_dir(Path(tmp))
            _, _, calls = _verify(phase_dir)
            git = next(c for c in calls if c[0] == "git")
            self.assertEqual(git, ["git", "-C", str(Path(tmp).resolve()), "branch", "--show-current"])


class TestMainDispatch(unittest.TestCase):
    def test_verify_post_routes_phase_dir_and_returns_its_code(self):
        with mock.patch.object(pr_status, "verify_post", return_value=7) as vp:
            self.assertEqual(pr_status.main(["verify-post", "some/dir"]), 7)
        vp.assert_called_once_with("some/dir")

    def test_ship_post_notice_routes_phase_dir(self):
        with mock.patch.object(pr_status, "ship_post_notice", return_value=0) as sp:
            self.assertEqual(pr_status.main(["ship-post-notice", "some/dir"]), 0)
        sp.assert_called_once_with("some/dir")

    def test_bad_or_missing_subcommand_exits_2(self):
        for argv in (["bogus", "x"], [], ["verify-post"]):
            with self.subTest(argv=argv):
                with mock.patch("sys.stderr", io.StringIO()):
                    with self.assertRaises(SystemExit) as cm:
                        pr_status.main(argv)
                self.assertEqual(cm.exception.code, 2)


class TestUncoveredBranches(unittest.TestCase):
    def _sentinel(self, **stages):
        with tempfile.TemporaryDirectory() as tmp:
            phase_dir = _write_phase_dir(Path(tmp))
            exit_code, output, _ = _verify(phase_dir, **stages)
            return exit_code, output, _report(phase_dir)

    def test_no_commit_found_stderr_means_zero_checks_passing(self):
        _, _, fields = self._sentinel(checks=_completed("", 1, stderr="no commit found for ref"))
        self.assertEqual(fields["pr_status"], "passing")
        self.assertEqual(fields["pr_gate_ok"], "true")

    def test_cancelled_check_is_failing_end_to_end(self):
        _, _, fields = self._sentinel(checks=_fixture("checks_cancel.json"))
        self.assertEqual(fields["pr_status"], "failing")
        self.assertEqual(fields["pr_gate_ok"], "false")

    def test_live_call_failures_write_sentinel_and_exit_0(self):
        failures = {
            "checks timeout": {"checks": subprocess.TimeoutExpired(cmd="gh", timeout=30)},
            "checks oserror": {"checks": OSError("spawn failed")},
            "checks invalid json": {"checks": "not json"},
            "list invalid json": {"pr_list": "not json"},
            "branch oserror": {"branch": OSError("git vanished")},
            "branch timeout": {"branch": subprocess.TimeoutExpired(cmd="git", timeout=10)},
        }
        for name, stages in failures.items():
            with self.subTest(name):
                exit_code, _, fields = self._sentinel(**stages)
                self.assertEqual(exit_code, 0)
                self.assertEqual(fields["pr_status"], "unavailable")
                self.assertEqual(fields["pr_gate_ok"], "false")

    def test_ship_post_notice_branch_oserror_fails_open(self):
        with tempfile.TemporaryDirectory() as tmp:
            phase_dir = _write_phase_dir(Path(tmp))
            exit_code, output, _ = _verify(
                phase_dir, fn="ship_post_notice", branch=OSError("git vanished"))
            self.assertEqual(exit_code, 0)
            self.assertEqual(output.count(pr_status.NOTICE_GH_ERROR), 1)


class TestStaleNeverSurvives(unittest.TestCase):
    """kilo.5: no exception path may leave a prior pr_gate_ok:true behind.
    Every unavailable outcome exits 0 (README/SKILL: fail-open, onError:skip)."""

    def test_no_planning_dir_still_writes_sentinel(self):
        with tempfile.TemporaryDirectory() as tmp:
            phase_dir = Path(tmp) / "14-x"
            phase_dir.mkdir()
            (phase_dir / "14-PR.md").write_text(STALE_PASS, encoding="utf-8")
            exit_code, _, _ = _verify(phase_dir)
            self.assertEqual(exit_code, 0)
            self.assertEqual(_report(phase_dir)["pr_gate_ok"], "false")

    def test_write_failure_removes_stale_report_and_exits_1(self):
        with tempfile.TemporaryDirectory() as tmp:
            phase_dir = _write_phase_dir(Path(tmp))
            (phase_dir / "14-PR.md").write_text(STALE_PASS, encoding="utf-8")
            with mock.patch.object(pr_status.os, "replace", side_effect=OSError("ro")):
                exit_code, _, _ = _verify(phase_dir)
            self.assertEqual(exit_code, 1)
            self.assertFalse((phase_dir / "14-PR.md").exists())

    def test_torn_write_never_reaches_report_path(self):
        real = Path.write_text

        def torn(self_, data, *a, **kw):
            real(self_, data[: len(data) // 2], *a, **kw)
            raise OSError("killed mid-write")

        with tempfile.TemporaryDirectory() as tmp:
            phase_dir = _write_phase_dir(Path(tmp))
            (phase_dir / "14-PR.md").write_text(STALE_PASS, encoding="utf-8")
            with mock.patch.object(Path, "write_text", torn):
                _verify(phase_dir)
            report = phase_dir / "14-PR.md"
            text = report.read_text(encoding="utf-8") if report.exists() else ""
            self.assertNotIn("pr_gate_ok: true", text)
            self.assertIn(text.count("---"), (0, 2))


class TestConfined(unittest.TestCase):
    """T-14-03: confined() must reject any resolved escape from the
    project root -- copied verbatim from lint.py, re-tested independently
    here per this capability's own threat register entry."""

    def test_confined_raises_for_escape(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "root"
            root.mkdir()
            with self.assertRaises(ValueError):
                pr_status.confined(root, "..", "..", "etc", "passwd")


if __name__ == "__main__":
    unittest.main()
