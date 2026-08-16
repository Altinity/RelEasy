"""Unit tests for the pre-change (baseline) comparison.

``analyze-fails`` reads the last CI run on the target branch that
predates the PR's diff and labels each failure pre-existing / new /
uncomparable. Covers baseline-commit selection (release branches are
mostly merge commits with no run at all), the per-test verdict, and
what the prompt says in each case.

Stdlib unittest (no pytest dependency). Run:
    python3 -m unittest discover -s tests
"""
from __future__ import annotations

import unittest
from unittest import mock

from releasy import ci_failures
from releasy.analyze_fails import _baseline_line, _baseline_section
from releasy.ci_failures import BaselineRun, FailedStatus, FailedTest

BASE = "antalya-26.6"
MERGE_BASE = "5f5903e8e3aaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
RAN = "7eba7cb7099c40561fc2973a6b06a493d56ab2b4"
HEAD = "c1efccca5eb72115b0fd347002e24d426e018d21"


def _test(name: str, category: str = "stateless") -> FailedTest:
    return FailedTest(
        name=name, status="FAIL", category=category,
        shard_context=f"{category} shard", target_url="https://report",
    )


def _run(**kw) -> BaselineRun:
    return BaselineRun(
        sha=kw.pop("sha", RAN),
        committed_at=kw.pop("committed_at", "2026-08-14T08:36:13Z"),
        checks_total=kw.pop("checks_total", 57),
        checks_failed=kw.pop("checks_failed", 11),
        failing=kw.pop("failing", {("stateless", "00001_old"): "shard A"}),
        categories_run=kw.pop("categories_run", {"stateless"}),
        skipped_newer=kw.pop("skipped_newer", 0),
    )


class BaselineCommitSelection(unittest.TestCase):
    """Merge commits carry no CI run; keep walking back until one does."""

    def _discover(self, commits, statuses_by_sha, **kw):
        def _list(owner, repo, sha, limit):
            return commits[:limit], None

        def _statuses(owner, repo, sha, *, failed_only=True):
            sts = statuses_by_sha.get(sha, [])
            if failed_only:
                sts = [s for s in sts if s.state in ("failure", "error")]
            return sts, None

        with mock.patch.object(
            ci_failures, "_list_commits", _list,
        ), mock.patch.object(
            ci_failures, "fetch_statuses", _statuses,
        ), mock.patch.object(
            ci_failures, "decompose_statuses", lambda sts, **_: ([
                FailedTest(
                    name="00001_old", status="FAIL", category="stateless",
                    shard_context=s.context, target_url="",
                ) for s in sts
            ], []),
        ):
            return ci_failures.baseline_run_before(
                "o", "r", MERGE_BASE, **kw,
            )

    def test_skips_commits_with_no_run(self):
        run, err = self._discover(
            commits=[
                (MERGE_BASE, "2026-08-14T08:39:32Z"),
                (RAN, "2026-08-14T08:36:13Z"),
            ],
            statuses_by_sha={RAN: [FailedStatus(
                context="Stateless tests (amd_debug, parallel)",
                state="failure", target_url="https://report",
                description="", category="stateless", locator=None,
            )]},
        )
        self.assertIsNone(err)
        self.assertEqual(run.sha, RAN)
        self.assertEqual(run.checks_failed, 1)

    def test_reports_when_nothing_in_range_ran_ci(self):
        run, err = self._discover(
            commits=[(MERGE_BASE, "2026-08-14T08:39:32Z")],
            statuses_by_sha={},
        )
        self.assertIsNone(run)
        self.assertIn("no CI run found", err)

    def test_never_uses_the_prs_own_run_as_its_baseline(self):
        run, err = self._discover(
            commits=[(HEAD, "now"), (RAN, "before")],
            statuses_by_sha={
                HEAD: [FailedStatus(
                    context="x", state="failure", target_url="",
                    description="", category="stateless", locator=None,
                )],
                RAN: [FailedStatus(
                    context="y", state="failure", target_url="",
                    description="", category="stateless", locator=None,
                )],
            },
            exclude_sha=HEAD,
        )
        self.assertIsNone(err)
        self.assertEqual(run.sha, RAN)


class CategoryAwareSelection(unittest.TestCase):
    """A run that never ran the failing check answers no question."""

    COMMITS = [
        ("newer", "2026-08-13T03:22:37Z"),
        ("older", "2026-08-12T19:43:54Z"),
    ]

    def _discover(self, coverage, **kw):
        def _list(owner, repo, sha, limit):
            return self.COMMITS[:limit], None

        def _statuses(owner, repo, sha, *, failed_only=True):
            return [
                FailedStatus(
                    context=f"{cat} check", state="failure",
                    target_url="", description="", category=cat,
                    locator=None,
                )
                for cat in coverage.get(sha, [])
            ], None

        with mock.patch.object(
            ci_failures, "_list_commits", _list,
        ), mock.patch.object(
            ci_failures, "fetch_statuses", _statuses,
        ), mock.patch.object(
            ci_failures, "decompose_statuses", lambda sts, **_: ([], []),
        ):
            return ci_failures.baseline_run_before("o", "r", "mb", **kw)

    def test_skips_a_newer_run_that_lacks_the_needed_check(self):
        run, err = self._discover(
            {"newer": ["stateless"], "older": ["stateless", "fasttest"]},
            require_categories=frozenset({"fasttest"}),
        )
        self.assertIsNone(err)
        self.assertEqual(run.sha, "older")
        self.assertEqual(run.skipped_newer, 1)

    def test_takes_the_newest_run_when_it_covers(self):
        run, err = self._discover(
            {"newer": ["stateless", "fasttest"], "older": ["fasttest"]},
            require_categories=frozenset({"fasttest"}),
        )
        self.assertEqual(run.sha, "newer")
        self.assertEqual(run.skipped_newer, 0)

    def test_falls_back_to_the_newest_when_nothing_covers(self):
        run, err = self._discover(
            {"newer": ["stateless"], "older": ["stateless"]},
            require_categories=frozenset({"regression"}),
        )
        self.assertIsNone(err)
        self.assertEqual(run.sha, "newer")
        # Reported as a plain baseline: its gaps show up per test as
        # "not covered" rather than as a skipped-newer caveat.
        self.assertEqual(run.skipped_newer, 0)
        self.assertEqual(
            run.verdict_for("regression", "/x"), "not covered",
        )


class PerTestVerdict(unittest.TestCase):
    """Absent from the failure list only means "passed" if the check ran."""

    def test_failing_at_baseline_is_pre_existing(self):
        self.assertEqual(
            _run().verdict_for("stateless", "00001_old"), "failed",
        )

    def test_absent_but_category_ran_is_passed(self):
        self.assertEqual(
            _run().verdict_for("stateless", "00002_new"), "passed",
        )

    def test_absent_and_category_never_ran_is_not_covered(self):
        self.assertEqual(
            _run().verdict_for("regression", "/swarms/x"), "not covered",
        )


class FailureBlockAnnotation(unittest.TestCase):

    def test_pre_existing_names_the_baseline_and_shard(self):
        line = _baseline_line(_test("00001_old"), _run())
        self.assertIn("pre-existing", line)
        self.assertIn(RAN[:10], line)
        self.assertIn("shard A", line)
        self.assertIn("did not break it", line.lower())

    def test_new_since_baseline_points_the_finger(self):
        line = _baseline_line(_test("00002_new"), _run())
        self.assertIn("new since baseline", line)
        self.assertIn("prime suspect", line)

    def test_uncovered_category_claims_nothing(self):
        line = _baseline_line(_test("/swarms/x", "regression"), _run())
        self.assertIn("says nothing", line)

    def test_no_baseline_says_so(self):
        self.assertIn("no pre-change run", _baseline_line(_test("t"), None))


class BaselineSectionRendering(unittest.TestCase):

    def test_all_pre_existing_shard_is_told_not_to_build(self):
        tests = [_test("00001_old")]
        section = _baseline_section(tests, _run(), BASE)
        self.assertIn("1 of 1 already failed there", section)
        self.assertIn("without building anything", section)
        self.assertIn("UNRELATED", section)

    def test_mixed_shard_is_pointed_at_the_new_failures(self):
        tests = [_test("00001_old"), _test("00002_new")]
        section = _baseline_section(tests, _run(), BASE)
        self.assertIn("1 of 2 already failed there", section)
        self.assertIn("1 did not fail there", section)
        self.assertIn("start with the new-since-baseline", section.lower())
        self.assertNotIn("without building anything", section)

    def test_uncomparable_tests_are_counted_separately(self):
        tests = [_test("/swarms/x", "regression")]
        section = _baseline_section(tests, _run(), BASE)
        self.assertIn("1 could not be compared", section)

    def test_older_baseline_carries_the_caveat(self):
        section = _baseline_section(
            [_test("00002_new")], _run(skipped_newer=2), BASE,
        )
        self.assertIn("2 newer run(s)", section)
        self.assertIn("confirm against the diff", section)

    def test_newest_baseline_has_no_caveat(self):
        section = _baseline_section([_test("00002_new")], _run(), BASE)
        self.assertNotIn("newer run(s)", section)

    def test_missing_baseline_explains_itself(self):
        section = _baseline_section(
            [_test("t")], None, BASE, "artefacts pruned",
        )
        self.assertIn(BASE, section)
        self.assertIn("artefacts pruned", section)
        self.assertIn("no CI run", section)


if __name__ == "__main__":
    unittest.main()
