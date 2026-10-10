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
    ("gh", "auth", "status"): "auth",
    ("git", "branch", "--show-current"): "branch",
    ("gh", "pr", "list"): "list",
    ("gh", "pr", "checks"): "checks",
}


def _verify(phase_dir, fn="verify_post", which="/usr/bin/gh", auth="", branch="feature-x\n",
            pr_list=PR_LIST_ONE_OPEN, checks=None):
    """Run pr_status.<fn>(phase_dir) against one fake gh/git. Each stage value
    is a CompletedProcess, an exception instance (raised), or a str (stdout,
    rc 0). Returns (exit_code, captured stdout, argv list of every call)."""
    stages = {
        "auth": auth, "branch": branch, "list": pr_list,
        "checks": _fixture("checks_pass.json") if checks is None else checks,
    }
    calls = []

    def fake_run(argv, **kwargs):
        calls.append(argv)
        stage = _STAGE_ARGV.get(tuple(argv[:3]))
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
                pr_status.current_branch()


class TestVerifyPost(unittest.TestCase):
    """verify_post() end-to-end against a mocked subprocess.run -- one
    fake_run dispatcher per test, branching on the argv prefix so each test
    only asserts the calls its own scenario actually needs."""

    def test_no_open_pr_writes_none_and_gate_ok(self):
        with tempfile.TemporaryDirectory() as tmp:
            phase_dir = _write_phase_dir(Path(tmp))

            def fake_run(argv, **kwargs):
                if argv[:3] == ["gh", "auth", "status"]:
                    return _completed("", 0)
                if argv[:3] == ["git", "branch", "--show-current"]:
                    return _completed("main\n", 0)
                if argv[:3] == ["gh", "pr", "list"]:
                    return _completed(_fixture("pr_list_empty.json"), 0)
                raise AssertionError(f"unexpected call: {argv}")

            with mock.patch("shutil.which", return_value="/usr/bin/gh"):
                with mock.patch("subprocess.run", side_effect=fake_run):
                    exit_code = pr_status.verify_post(str(phase_dir))

            self.assertEqual(exit_code, 0)
            report_path = phase_dir / "14-PR.md"
            self.assertTrue(report_path.exists())
            text = report_path.read_text(encoding="utf-8")
            self.assertIn("pr_status", text)
            self.assertIn("pr_gate_ok", text)
            self.assertIn("generated_at", text)
            self.assertIn("generated_from", text)
            self.assertIn("pr_status: none", text)
            self.assertIn("pr_gate_ok: true", text)

    def test_open_pr_failing_checks_writes_failing_and_gate_not_ok(self):
        with tempfile.TemporaryDirectory() as tmp:
            phase_dir = _write_phase_dir(Path(tmp))

            def fake_run(argv, **kwargs):
                if argv[:3] == ["gh", "auth", "status"]:
                    return _completed("", 0)
                if argv[:3] == ["git", "branch", "--show-current"]:
                    return _completed("feature-x\n", 0)
                if argv[:3] == ["gh", "pr", "list"]:
                    return _completed(PR_LIST_ONE_OPEN, 0)
                if argv[:3] == ["gh", "pr", "checks"]:
                    return _completed(_fixture("checks_fail.json"), 0)
                raise AssertionError(f"unexpected call: {argv}")

            with mock.patch("shutil.which", return_value="/usr/bin/gh"):
                with mock.patch("subprocess.run", side_effect=fake_run):
                    exit_code = pr_status.verify_post(str(phase_dir))

            self.assertEqual(exit_code, 0)
            text = (phase_dir / "14-PR.md").read_text(encoding="utf-8")
            self.assertIn("pr_status: failing", text)
            self.assertIn("pr_gate_ok: false", text)

    def test_branch_with_embedded_quote_does_not_break_frontmatter(self):
        # WR-03: git allows `"` in ref names; generated_from must escape it
        # rather than terminating the quoted YAML scalar early.
        with tempfile.TemporaryDirectory() as tmp:
            phase_dir = _write_phase_dir(Path(tmp))

            def fake_run(argv, **kwargs):
                if argv[:3] == ["gh", "auth", "status"]:
                    return _completed("", 0)
                if argv[:3] == ["git", "branch", "--show-current"]:
                    return _completed('feature"pwn\n', 0)
                if argv[:3] == ["gh", "pr", "list"]:
                    return _completed(_fixture("pr_list_empty.json"), 0)
                raise AssertionError(f"unexpected call: {argv}")

            with mock.patch("shutil.which", return_value="/usr/bin/gh"):
                with mock.patch("subprocess.run", side_effect=fake_run):
                    exit_code = pr_status.verify_post(str(phase_dir))

            self.assertEqual(exit_code, 0)
            text = (phase_dir / "14-PR.md").read_text(encoding="utf-8")
            line = next(l for l in text.splitlines() if l.startswith("generated_from:"))
            self.assertEqual(
                line,
                'generated_from: "gh pr list --head=feature\\"pwn --state open --limit 100 --json number,url,isCrossRepository"',
            )
            # frontmatter block boundaries stay intact -- no unquoted
            # trailing text leaked onto the generated_from line.
            self.assertEqual(text.count("---"), 2)

    def test_rerun_overwrites_not_appends(self):
        with tempfile.TemporaryDirectory() as tmp:
            phase_dir = _write_phase_dir(Path(tmp))

            def fake_run(argv, **kwargs):
                if argv[:3] == ["gh", "auth", "status"]:
                    return _completed("", 0)
                if argv[:3] == ["git", "branch", "--show-current"]:
                    return _completed("main\n", 0)
                if argv[:3] == ["gh", "pr", "list"]:
                    return _completed(_fixture("pr_list_empty.json"), 0)
                raise AssertionError(f"unexpected call: {argv}")

            with mock.patch("shutil.which", return_value="/usr/bin/gh"):
                with mock.patch("subprocess.run", side_effect=fake_run):
                    pr_status.verify_post(str(phase_dir))
                    first_text = (phase_dir / "14-PR.md").read_text(encoding="utf-8")
                    pr_status.verify_post(str(phase_dir))
                    second_text = (phase_dir / "14-PR.md").read_text(encoding="utf-8")

            # generated_at is a live UTC timestamp: comparing it across two
            # runs would fail whenever they straddle a second boundary. Every
            # other byte must be identical -- that is what "overwrites, not
            # appends" means.
            self.assertEqual(len(first_text), len(second_text))
            self.assertEqual(first_text.count("---"), 2)
            self.assertEqual(second_text.count("---"), 2)
            self.assertEqual(_without_timestamp(first_text), _without_timestamp(second_text))


class TestOpenPrReport(unittest.TestCase):
    """The report's non-status fields (`pr_number`, `open_pr_count`) and the
    passing/pending/skipping rollups end-to-end, not just as pure-function
    assertions -- the gate reads what `_write_report` emits, not what
    `rollup_pr_status` returns."""

    def _run(self, checks_fixture, pr_list=PR_LIST_ONE_OPEN):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        phase_dir = _write_phase_dir(Path(tmp.name))

        def fake_run(argv, **kwargs):
            if argv[:3] == ["gh", "auth", "status"]:
                return _completed("", 0)
            if argv[:3] == ["git", "branch", "--show-current"]:
                return _completed("feature-x\n", 0)
            if argv[:3] == ["gh", "pr", "list"]:
                return _completed(pr_list, 0)
            if argv[:3] == ["gh", "pr", "checks"]:
                return _completed(_fixture(checks_fixture), 0)
            raise AssertionError(f"unexpected call: {argv}")

        with mock.patch("shutil.which", return_value="/usr/bin/gh"):
            with mock.patch("subprocess.run", side_effect=fake_run):
                exit_code = pr_status.verify_post(str(phase_dir))
        self.assertEqual(exit_code, 0)
        return _frontmatter((phase_dir / "14-PR.md").read_text(encoding="utf-8"))

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
        # D-02/RESEARCH A4: two open PRs can share one head branch (different
        # base branches). The count must be recorded, not silently collapsed.
        two_open = json.dumps([
            _pr(42),
            _pr(43),
        ])
        fields = self._run("checks_pass.json", pr_list=two_open)
        self.assertEqual(fields["pr_number"], "42")
        self.assertEqual(fields["open_pr_count"], "2")


class TestFailOpen(unittest.TestCase):
    """PRW-04: `gh` absent or unauthenticated, or a live `gh` call blowing up
    with a transient error, all degrade to exit 0, exactly one notice, and a
    sentinel report that fully overwrites any prior content -- mirrors
    markdown-linting's TestFailOpen shape (test_lint.py)."""

    def test_gh_absent_fail_open(self):
        with tempfile.TemporaryDirectory() as tmp:
            phase_dir = _write_phase_dir(Path(tmp))
            captured = io.StringIO()
            with mock.patch("shutil.which", return_value=None):
                with mock.patch(
                    "subprocess.run",
                    side_effect=AssertionError("gh must not be invoked when absent from PATH"),
                ) as mock_run:
                    with mock.patch("sys.stdout", captured):
                        exit_code = pr_status.verify_post(str(phase_dir))

            self.assertEqual(exit_code, 0)
            self.assertEqual(captured.getvalue().count(pr_status.NOTICE_GH_ABSENT), 1)
            self.assertEqual(mock_run.call_count, 0)
            text = (phase_dir / "14-PR.md").read_text(encoding="utf-8")
            self.assertIn("pr_status: unavailable", text)
            self.assertIn("pr_gate_ok: false", text)
            self.assertIn("unavailable_reason", text)

    def test_gh_unauthenticated_fail_open(self):
        with tempfile.TemporaryDirectory() as tmp:
            phase_dir = _write_phase_dir(Path(tmp))
            captured = io.StringIO()

            def fake_run(argv, **kwargs):
                if argv[:3] == ["gh", "auth", "status"]:
                    return _completed("", 1, stderr="not logged in")
                raise AssertionError(f"unexpected call: {argv}")

            with mock.patch("shutil.which", return_value="/usr/bin/gh"):
                with mock.patch("subprocess.run", side_effect=fake_run):
                    with mock.patch("sys.stdout", captured):
                        exit_code = pr_status.verify_post(str(phase_dir))

            self.assertEqual(exit_code, 0)
            output = captured.getvalue()
            self.assertEqual(output.count(pr_status.NOTICE_GH_UNAUTH), 1)
            self.assertNotIn(pr_status.NOTICE_GH_ABSENT, output)
            text = (phase_dir / "14-PR.md").read_text(encoding="utf-8")
            self.assertIn("pr_status: unavailable", text)
            self.assertIn("pr_gate_ok: false", text)

    def test_stale_passing_status_replaced_by_sentinel(self):
        with tempfile.TemporaryDirectory() as tmp:
            phase_dir = _write_phase_dir(Path(tmp))
            report_path = phase_dir / "14-PR.md"
            report_path.write_text(
                "---\npr_status: passing\npr_gate_ok: true\n---\n\nstale\n", encoding="utf-8"
            )
            with mock.patch("shutil.which", return_value=None):
                with mock.patch(
                    "subprocess.run",
                    side_effect=AssertionError("gh must not be invoked when absent from PATH"),
                ):
                    pr_status.verify_post(str(phase_dir))

            text = report_path.read_text(encoding="utf-8")
            self.assertIn("pr_status: unavailable", text)
            self.assertNotIn("pr_status: passing", text)
            self.assertNotIn("pr_gate_ok: true", text)

    def test_gh_pr_list_timeout_fail_open(self):
        with tempfile.TemporaryDirectory() as tmp:
            phase_dir = _write_phase_dir(Path(tmp))
            captured = io.StringIO()

            def fake_run(argv, **kwargs):
                if argv[:3] == ["gh", "auth", "status"]:
                    return _completed("", 0)
                if argv[:3] == ["git", "branch", "--show-current"]:
                    return _completed("main\n", 0)
                if argv[:3] == ["gh", "pr", "list"]:
                    raise subprocess.TimeoutExpired(cmd=argv, timeout=30)
                raise AssertionError(f"unexpected call: {argv}")

            with mock.patch("shutil.which", return_value="/usr/bin/gh"):
                with mock.patch("subprocess.run", side_effect=fake_run):
                    with mock.patch("sys.stdout", captured):
                        exit_code = pr_status.verify_post(str(phase_dir))

            self.assertEqual(exit_code, 0)
            self.assertEqual(captured.getvalue().count(pr_status.NOTICE_GH_ERROR), 1)
            text = (phase_dir / "14-PR.md").read_text(encoding="utf-8")
            self.assertIn("pr_status: unavailable", text)
            self.assertIn("pr_gate_ok: false", text)

    def test_gh_pr_list_nonzero_exit_fail_open(self):
        # WR-01: find_open_pr()'s GhCommandError on any gh pr list non-zero
        # exit (not just a TimeoutExpired) must also fail open, unlike
        # check_buckets()'s deliberately-uncaught plain RuntimeError.
        with tempfile.TemporaryDirectory() as tmp:
            phase_dir = _write_phase_dir(Path(tmp))
            captured = io.StringIO()

            def fake_run(argv, **kwargs):
                if argv[:3] == ["gh", "auth", "status"]:
                    return _completed("", 0)
                if argv[:3] == ["git", "branch", "--show-current"]:
                    return _completed("main\n", 0)
                if argv[:3] == ["gh", "pr", "list"]:
                    return _completed("", 1, stderr="HTTP 403: rate limited")
                raise AssertionError(f"unexpected call: {argv}")

            with mock.patch("shutil.which", return_value="/usr/bin/gh"):
                with mock.patch("subprocess.run", side_effect=fake_run):
                    with mock.patch("sys.stdout", captured):
                        exit_code = pr_status.verify_post(str(phase_dir))

            self.assertEqual(exit_code, 0)
            self.assertEqual(captured.getvalue().count(pr_status.NOTICE_GH_ERROR), 1)
            text = (phase_dir / "14-PR.md").read_text(encoding="utf-8")
            self.assertIn("pr_status: unavailable", text)
            self.assertIn("pr_gate_ok: false", text)

    def test_checks_zero_checks_stderr_is_passing(self):
        with tempfile.TemporaryDirectory() as tmp:
            phase_dir = _write_phase_dir(Path(tmp))

            def fake_run(argv, **kwargs):
                if argv[:3] == ["gh", "auth", "status"]:
                    return _completed("", 0)
                if argv[:3] == ["git", "branch", "--show-current"]:
                    return _completed("feature-x\n", 0)
                if argv[:3] == ["gh", "pr", "list"]:
                    return _completed(PR_LIST_ONE_OPEN, 0)
                if argv[:3] == ["gh", "pr", "checks"]:
                    return _completed("", 1, stderr="no checks reported on the 'feature-x' branch")
                raise AssertionError(f"unexpected call: {argv}")

            with mock.patch("shutil.which", return_value="/usr/bin/gh"):
                with mock.patch("subprocess.run", side_effect=fake_run):
                    exit_code = pr_status.verify_post(str(phase_dir))

            self.assertEqual(exit_code, 0)
            text = (phase_dir / "14-PR.md").read_text(encoding="utf-8")
            self.assertIn("pr_status: passing", text)
            self.assertIn("pr_gate_ok: true", text)

    def test_checks_unrelated_stderr_writes_sentinel(self):
        # check_buckets() still raises plain RuntimeError for a genuine
        # gh pr checks failure -- but verify_post now catches it rather
        # than letting it propagate uncaught. Under this dispatch point's
        # onError:skip contract, an uncaught exception would skip
        # regenerating PR.md entirely, leaving a stale prior-run status
        # (e.g. pr_gate_ok: true) sitting there, silently satisfying
        # ship:pre on unmeasured data. Loud (exit 1) but never stale.
        with tempfile.TemporaryDirectory() as tmp:
            phase_dir = _write_phase_dir(Path(tmp))
            report_path = phase_dir / "14-PR.md"
            report_path.write_text(
                "---\npr_status: passing\npr_gate_ok: true\n---\n\nstale\n", encoding="utf-8"
            )

            def fake_run(argv, **kwargs):
                if argv[:3] == ["gh", "auth", "status"]:
                    return _completed("", 0)
                if argv[:3] == ["git", "branch", "--show-current"]:
                    return _completed("feature-x\n", 0)
                if argv[:3] == ["gh", "pr", "list"]:
                    return _completed(PR_LIST_ONE_OPEN, 0)
                if argv[:3] == ["gh", "pr", "checks"]:
                    return _completed("", 1, stderr="thread panicked at checks.go:1:1")
                raise AssertionError(f"unexpected call: {argv}")

            captured = io.StringIO()
            with mock.patch("shutil.which", return_value="/usr/bin/gh"):
                with mock.patch("subprocess.run", side_effect=fake_run):
                    with mock.patch("sys.stdout", captured):
                        exit_code = pr_status.verify_post(str(phase_dir))

            self.assertEqual(exit_code, 0)
            self.assertIn("gh pr checks failed", captured.getvalue())
            text = report_path.read_text(encoding="utf-8")
            self.assertIn("pr_status: unavailable", text)
            self.assertIn("pr_gate_ok: false", text)
            self.assertIn('unavailable_reason: "RuntimeError', text)
            self.assertNotIn("pr_status: passing", text)
            self.assertNotIn("pr_gate_ok: true", text)

    def test_gh_command_error_still_fails_open_not_caught_by_runtime_error_clause(self):
        # GhCommandError IS-A RuntimeError. It must stay routed through the
        # fail-open tuple's except clause (exit 0), never the bare
        # `except RuntimeError` meant only for check_buckets()'s genuine
        # tool failure -- the two must not collapse into one behavior.
        with tempfile.TemporaryDirectory() as tmp:
            phase_dir = _write_phase_dir(Path(tmp))

            def fake_run(argv, **kwargs):
                if argv[:3] == ["gh", "auth", "status"]:
                    return _completed("", 0)
                if argv[:3] == ["git", "branch", "--show-current"]:
                    return _completed("main\n", 0)
                if argv[:3] == ["gh", "pr", "list"]:
                    return _completed("", 1, stderr="HTTP 403: rate limited")
                raise AssertionError(f"unexpected call: {argv}")

            captured = io.StringIO()
            with mock.patch("shutil.which", return_value="/usr/bin/gh"):
                with mock.patch("subprocess.run", side_effect=fake_run):
                    with mock.patch("sys.stdout", captured):
                        exit_code = pr_status.verify_post(str(phase_dir))

            self.assertEqual(exit_code, 0)
            self.assertEqual(captured.getvalue().count(pr_status.NOTICE_GH_ERROR), 1)
            self.assertNotIn("gh pr checks failed", captured.getvalue())


    def test_gh_stderr_cannot_forge_frontmatter_keys(self):
        # WR-03, second injection sink: unavailable_reason embeds gh stderr
        # verbatim, and stderr is multi-line. Unquoted, everything after the
        # first newline lands as sibling frontmatter keys -- so a stderr line
        # reading `pr_gate_ok: true` would be handed to the ship:pre gate by
        # a last-wins YAML reader, on a run that measured nothing.
        with tempfile.TemporaryDirectory() as tmp:
            phase_dir = _write_phase_dir(Path(tmp))
            captured = io.StringIO()

            def fake_run(argv, **kwargs):
                if argv[:3] == ["gh", "auth", "status"]:
                    return _completed("", 0)
                if argv[:3] == ["git", "branch", "--show-current"]:
                    return _completed("main\n", 0)
                if argv[:3] == ["gh", "pr", "list"]:
                    return _completed(
                        "", 1, stderr="boom\npr_gate_ok: true\npr_status: passing\n"
                    )
                raise AssertionError(f"unexpected call: {argv}")

            with mock.patch("shutil.which", return_value="/usr/bin/gh"):
                with mock.patch("subprocess.run", side_effect=fake_run):
                    with mock.patch("sys.stdout", captured):
                        exit_code = pr_status.verify_post(str(phase_dir))

            self.assertEqual(exit_code, 0)
            text = (phase_dir / "14-PR.md").read_text(encoding="utf-8")
            fields = _frontmatter(text)
            self.assertEqual(fields["pr_gate_ok"], "false")
            self.assertEqual(fields["pr_status"], "unavailable")
            self.assertEqual(text.count("---"), 2)
            # the stderr text is still reported, just as one escaped scalar
            self.assertIn("boom", fields["unavailable_reason"])
            self.assertNotIn("\n", fields["unavailable_reason"])

    def test_gh_auth_status_blowup_fails_open_and_overwrites_stale(self):
        # gh_available() runs BEFORE verify_post's fail-open try block, and
        # `gh auth status` is a live network call. If it could raise, the
        # traceback would escape, onError:skip would swallow it, and the
        # PREVIOUS run's pr_gate_ok: true would stay on disk unmeasured.
        with tempfile.TemporaryDirectory() as tmp:
            phase_dir = _write_phase_dir(Path(tmp))
            report_path = phase_dir / "14-PR.md"
            report_path.write_text(
                "---\npr_status: passing\npr_gate_ok: true\n---\n\nstale\n", encoding="utf-8"
            )
            captured = io.StringIO()

            def fake_run(argv, **kwargs):
                if argv[:3] == ["gh", "auth", "status"]:
                    raise subprocess.TimeoutExpired(cmd=argv, timeout=15)
                raise AssertionError(f"unexpected call: {argv}")

            with mock.patch("shutil.which", return_value="/usr/bin/gh"):
                with mock.patch("subprocess.run", side_effect=fake_run):
                    with mock.patch("sys.stdout", captured):
                        exit_code = pr_status.verify_post(str(phase_dir))

            self.assertEqual(exit_code, 0)
            self.assertEqual(captured.getvalue().count(pr_status.NOTICE_GH_ERROR), 1)
            fields = _frontmatter(report_path.read_text(encoding="utf-8"))
            self.assertEqual(fields["pr_status"], "unavailable")
            self.assertEqual(fields["pr_gate_ok"], "false")

    def test_malformed_pr_json_writes_sentinel(self):
        # gh JSON that parses but lacks `number` raises KeyError -- outside
        # the fail-open tuple and not a RuntimeError. Uncaught, it would skip
        # the regeneration and leave the stale report. Loud but never stale.
        with tempfile.TemporaryDirectory() as tmp:
            phase_dir = _write_phase_dir(Path(tmp))
            report_path = phase_dir / "14-PR.md"
            report_path.write_text(
                "---\npr_status: passing\npr_gate_ok: true\n---\n\nstale\n", encoding="utf-8"
            )
            captured = io.StringIO()

            def fake_run(argv, **kwargs):
                if argv[:3] == ["gh", "auth", "status"]:
                    return _completed("", 0)
                if argv[:3] == ["git", "branch", "--show-current"]:
                    return _completed("main\n", 0)
                if argv[:3] == ["gh", "pr", "list"]:
                    return _completed(json.dumps([{"url": "https://example.invalid/1"}]), 0)
                raise AssertionError(f"unexpected call: {argv}")

            with mock.patch("shutil.which", return_value="/usr/bin/gh"):
                with mock.patch("subprocess.run", side_effect=fake_run):
                    with mock.patch("sys.stdout", captured):
                        exit_code = pr_status.verify_post(str(phase_dir))

            self.assertEqual(exit_code, 0)
            fields = _frontmatter(report_path.read_text(encoding="utf-8"))
            self.assertEqual(fields["pr_status"], "unavailable")
            self.assertEqual(fields["pr_gate_ok"], "false")
            self.assertIn("KeyError", fields["unavailable_reason"])


class TestNoticeDistinctness(unittest.TestCase):
    """D-04: the user must be able to tell which fix applies -- the two
    notices must be distinct strings and neither a substring of the other."""

    def test_notices_are_distinct_and_not_substrings(self):
        a, b = pr_status.NOTICE_GH_ABSENT, pr_status.NOTICE_GH_UNAUTH
        self.assertNotEqual(a, b)
        self.assertNotIn(a, b)
        self.assertNotIn(b, a)


class TestShipPostNotice(unittest.TestCase):
    """PRW-03: `ship:post` warn-only notice when no open PR exists for the
    current branch. Writes no file, mutates no git/GitHub state, and never
    reads `PR.md` -- the probe is always live (RESEARCH Pitfall 2)."""

    def test_no_open_pr_prints_one_notice_naming_branch(self):
        with tempfile.TemporaryDirectory() as tmp:
            phase_dir = _write_phase_dir(Path(tmp))
            captured = io.StringIO()
            calls = []

            def fake_run(argv, **kwargs):
                calls.append(argv)
                if argv[:3] == ["gh", "auth", "status"]:
                    return _completed("", 0)
                if argv[:3] == ["git", "branch", "--show-current"]:
                    return _completed("feature-y\n", 0)
                if argv[:3] == ["gh", "pr", "list"]:
                    return _completed(_fixture("pr_list_empty.json"), 0)
                raise AssertionError(f"unexpected call: {argv}")

            with mock.patch("shutil.which", return_value="/usr/bin/gh"):
                with mock.patch("subprocess.run", side_effect=fake_run):
                    with mock.patch("sys.stdout", captured):
                        exit_code = pr_status.ship_post_notice(str(phase_dir))

            self.assertEqual(exit_code, 0)
            output = captured.getvalue()
            lines = [line for line in output.splitlines() if line.strip()]
            self.assertEqual(len(lines), 1)
            self.assertIn(pr_status.NOTICE_NO_OPEN_PR, output)
            self.assertIn("feature-y", output)
            for argv in calls:
                for forbidden in ("create", "merge", "comment", "edit", "review"):
                    self.assertNotIn(forbidden, argv)

    def test_open_pr_prints_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            phase_dir = _write_phase_dir(Path(tmp))
            captured = io.StringIO()

            def fake_run(argv, **kwargs):
                if argv[:3] == ["gh", "auth", "status"]:
                    return _completed("", 0)
                if argv[:3] == ["git", "branch", "--show-current"]:
                    return _completed("feature-y\n", 0)
                if argv[:3] == ["gh", "pr", "list"]:
                    return _completed(PR_LIST_ONE_OPEN, 0)
                raise AssertionError(f"unexpected call: {argv}")

            with mock.patch("shutil.which", return_value="/usr/bin/gh"):
                with mock.patch("subprocess.run", side_effect=fake_run):
                    with mock.patch("sys.stdout", captured):
                        exit_code = pr_status.ship_post_notice(str(phase_dir))

            self.assertEqual(exit_code, 0)
            self.assertEqual(captured.getvalue(), "")

    def test_gh_pr_list_failure_fail_open(self):
        # CR-01: a gh pr list blow-up after gh_available() already passed
        # must fail open (print NOTICE_GH_ERROR, return 0), not crash with
        # an uncaught RuntimeError/GhCommandError -- mirrors
        # TestFailOpen::test_gh_pr_list_timeout_fail_open.
        with tempfile.TemporaryDirectory() as tmp:
            phase_dir = _write_phase_dir(Path(tmp))
            captured = io.StringIO()

            def fake_run(argv, **kwargs):
                if argv[:3] == ["gh", "auth", "status"]:
                    return _completed("", 0)
                if argv[:3] == ["git", "branch", "--show-current"]:
                    return _completed("feature-y\n", 0)
                if argv[:3] == ["gh", "pr", "list"]:
                    raise subprocess.TimeoutExpired(cmd=argv, timeout=30)
                raise AssertionError(f"unexpected call: {argv}")

            with mock.patch("shutil.which", return_value="/usr/bin/gh"):
                with mock.patch("subprocess.run", side_effect=fake_run):
                    with mock.patch("sys.stdout", captured):
                        exit_code = pr_status.ship_post_notice(str(phase_dir))

            self.assertEqual(exit_code, 0)
            output = captured.getvalue()
            self.assertEqual(output.count(pr_status.NOTICE_GH_ERROR), 1)
            self.assertNotIn(pr_status.NOTICE_NO_OPEN_PR, output)

    def test_gh_absent_prints_prw04_notice_not_no_pr_notice(self):
        with tempfile.TemporaryDirectory() as tmp:
            phase_dir = _write_phase_dir(Path(tmp))
            captured = io.StringIO()
            with mock.patch("shutil.which", return_value=None):
                with mock.patch(
                    "subprocess.run",
                    side_effect=AssertionError("gh must not be invoked when absent from PATH"),
                ):
                    with mock.patch("sys.stdout", captured):
                        exit_code = pr_status.ship_post_notice(str(phase_dir))

            self.assertEqual(exit_code, 0)
            output = captured.getvalue()
            self.assertEqual(output.count(pr_status.NOTICE_GH_ABSENT), 1)
            self.assertNotIn(pr_status.NOTICE_NO_OPEN_PR, output)

    def test_gh_auth_status_blowup_fails_open(self):
        # Same guard, other caller: gh_available() is called outside this
        # function's try block too, so it must not raise here either.
        with tempfile.TemporaryDirectory() as tmp:
            phase_dir = _write_phase_dir(Path(tmp))
            captured = io.StringIO()

            def fake_run(argv, **kwargs):
                if argv[:3] == ["gh", "auth", "status"]:
                    raise OSError("gh vanished between which() and run()")
                raise AssertionError(f"unexpected call: {argv}")

            with mock.patch("shutil.which", return_value="/usr/bin/gh"):
                with mock.patch("subprocess.run", side_effect=fake_run):
                    with mock.patch("sys.stdout", captured):
                        exit_code = pr_status.ship_post_notice(str(phase_dir))

            self.assertEqual(exit_code, 0)
            output = captured.getvalue()
            self.assertEqual(output.count(pr_status.NOTICE_GH_ERROR), 1)
            self.assertNotIn(pr_status.NOTICE_NO_OPEN_PR, output)

    def test_never_reads_pr_md(self):
        with tempfile.TemporaryDirectory() as tmp:
            phase_dir = _write_phase_dir(Path(tmp))
            self.assertFalse((phase_dir / "14-PR.md").exists())

            def fake_run(argv, **kwargs):
                if argv[:3] == ["gh", "auth", "status"]:
                    return _completed("", 0)
                if argv[:3] == ["git", "branch", "--show-current"]:
                    return _completed("main\n", 0)
                if argv[:3] == ["gh", "pr", "list"]:
                    return _completed(_fixture("pr_list_empty.json"), 0)
                raise AssertionError(f"unexpected call: {argv}")

            with mock.patch("shutil.which", return_value="/usr/bin/gh"):
                with mock.patch("subprocess.run", side_effect=fake_run):
                    exit_code = pr_status.ship_post_notice(str(phase_dir))

            self.assertEqual(exit_code, 0)
            self.assertFalse((phase_dir / "14-PR.md").exists())


class TestNonUtf8Output(unittest.TestCase):
    """kilo.3: `text=True` without errors= made b'\\xff' from gh/git raise
    UnicodeDecodeError past every fail-open guard, keeping a stale report."""

    def _run_with_fake_bins(self, **stages):
        """Run verify_post against real fake `gh`/`git` executables on PATH so
        the real subprocess decode path is exercised. stages maps a stage
        (auth, branch, list, checks) to shell that emits non-UTF8 bytes."""
        scripts = {
            "gh": 'case "$1 $2" in\n'
                  f'  "auth status") {stages.get("auth", "exit 0")} ;;\n'
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
            "auth": bad_err, "branch": bad_err, "list": bad_err, "list-stdout": bad_out,
            "checks": bad_err,
        }.items():
            with self.subTest(stage=stage):
                key = "list" if stage == "list-stdout" else stage
                fields = self._run_with_fake_bins(**{key: body})
                self.assertEqual(fields["pr_status"], "unavailable")
                self.assertEqual(fields["pr_gate_ok"], "false")


STALE_PASS = "---\npr_status: passing\npr_gate_ok: true\n---\n\nstale\n"


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
