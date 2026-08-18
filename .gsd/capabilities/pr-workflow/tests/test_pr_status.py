"""Tests for .gsd/capabilities/pr-workflow/scripts/pr_status.py.

Stdlib unittest only (N5, mirrors markdown-linting/tests/test_lint.py's R-01
finding): the suite must pass under `python3 -m unittest discover` with no
third-party test runner. pr_status.py's parent directory is put on sys.path
at module import so no package __init__.py and no install step is needed --
mirrors test_lint.py's own sys.path.insert technique.
"""
import io
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import pr_status  # noqa: E402

FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures"

PR_LIST_ONE_OPEN = json.dumps([{"number": 42, "url": "https://github.com/x/y/pull/42"}])


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
                'generated_from: "gh pr list --head feature\\"pwn --state open --json number,url"',
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

            self.assertEqual(len(first_text), len(second_text))
            self.assertEqual(first_text.count("---"), 2)
            self.assertEqual(second_text.count("---"), 2)
            self.assertEqual(first_text, second_text)


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

    def test_checks_unrelated_stderr_raises(self):
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
                    return _completed("", 1, stderr="thread panicked at checks.go:1:1")
                raise AssertionError(f"unexpected call: {argv}")

            with mock.patch("shutil.which", return_value="/usr/bin/gh"):
                with mock.patch("subprocess.run", side_effect=fake_run):
                    with self.assertRaises(RuntimeError):
                        pr_status.verify_post(str(phase_dir))


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
