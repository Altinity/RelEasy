"""Unit tests for accounting every failed check in ``analyze-fails``.

Covers the three ways a failed CI check used to disappear from a run:
a report with no per-test leaf (only job steps), a ``target_url`` with
no machine-readable report at all, and a check whose failures the
cross-shard dedupe folded into another check's shard.

Fixtures mirror the artefact shapes of Altinity/ClickHouse#2195
(a stateless shard that died in ``Start ClickHouse Server``) and #2210
(``Regression release iceberg_2`` duplicating the aarch64 suite).

Stdlib unittest (no pytest dependency). Run:
    python3 -m unittest discover -s tests
"""
from __future__ import annotations

import unittest
from pathlib import Path
from unittest import mock

from releasy import ci_failures
from releasy.analyze_fails import (
    _category_runner_section,
    _group_failures_by_shard,
    _render_failure_block,
    _write_failed_tests_manifest,
)
from releasy.ci_failures import (
    CATEGORY_OTHER,
    FailedStatus,
    FailedTest,
    TestFlowsLocator,
    extract_failed_tests,
    job_level_failure,
)

SHARD = "Stateless tests (amd_asan, distributed plan, parallel, 2/4)"
REPORT_URL = "https://artifacts.example/json.html?PR=2195&sha=abc&name_0=PR"

# Older praktika revisions write the GitHub-status vocabulary on the
# job's step nodes; the tests themselves never appear when the job dies
# during setup.
STEPS_ONLY_REPORT = {
    "name": SHARD,
    "status": "failure",
    "info": "Failures: 1/3",
    "results": [
        {"name": "Install ClickHouse", "status": "success", "results": []},
        {
            "name": "Start ClickHouse Server",
            "status": "failure",
            "info": "Run command: [dmesg --clear] … server died",
            "results": [],
        },
        {"name": "Collect logs", "status": "success", "results": []},
    ],
}

# The modern shape: job → grouping node → per-test leaves.
TESTS_REPORT = {
    "name": SHARD,
    "status": "FAIL",
    "results": [
        {"name": "Install ClickHouse", "status": "OK", "results": []},
        {
            "name": "Tests",
            "status": "FAIL",
            "results": [
                {"name": "00001_select_one", "status": "FAIL", "info": "boom"},
                {"name": "00002_select_two", "status": "OK"},
            ],
        },
    ],
}


def _status(context: str = SHARD, **kw) -> FailedStatus:
    return FailedStatus(
        context=context,
        state=kw.pop("state", "failure"),
        target_url=kw.pop("target_url", REPORT_URL),
        description=kw.pop("description", "Failures: 1/3"),
        category=kw.pop("category", "stateless"),
        locator=kw.pop("locator", None),
    )


class StepFailuresAreJobLevel(unittest.TestCase):
    """A job that died in setup still yields its failing step."""

    def test_failing_step_is_extracted(self):
        found = extract_failed_tests(
            STEPS_ONLY_REPORT, category="stateless",
            shard_context=SHARD, target_url=REPORT_URL,
        )
        self.assertEqual([t.name for t in found], ["Start ClickHouse Server"])
        self.assertEqual(found[0].status, "FAILURE")

    def test_failing_step_is_marked_job_level(self):
        found = extract_failed_tests(
            STEPS_ONLY_REPORT, category="stateless",
            shard_context=SHARD, target_url=REPORT_URL,
        )
        self.assertTrue(found[0].job_level)

    def test_real_tests_are_not_job_level(self):
        found = extract_failed_tests(
            TESTS_REPORT, category="stateless",
            shard_context=SHARD, target_url=REPORT_URL,
        )
        self.assertEqual([t.name for t in found], ["00001_select_one"])
        self.assertFalse(found[0].job_level)

    def test_childless_failed_root_is_job_level(self):
        found = extract_failed_tests(
            {"name": SHARD, "status": "FAIL", "info": "runner OOM"},
            category="stateless", shard_context=SHARD,
            target_url=REPORT_URL,
        )
        self.assertEqual(len(found), 1)
        self.assertTrue(found[0].job_level)


class UnreadableChecksBecomeShards(unittest.TestCase):
    """A check with no machine-readable report is still investigated."""

    def test_record_carries_reason_description_and_url(self):
        ft = job_level_failure(
            _status(
                context="Grype Scan clickhouse-server",
                category=CATEGORY_OTHER,
                description="Completed with 3 high/critical vulnerabilities",
                target_url="https://artifacts.example/grype/results.html",
            ),
            "this check published no machine-readable report",
        )
        self.assertTrue(ft.job_level)
        self.assertEqual(ft.name, "Grype Scan clickhouse-server")
        self.assertEqual(ft.shard_context, "Grype Scan clickhouse-server")
        self.assertIn("no machine-readable report", ft.info_excerpt)
        self.assertIn("3 high/critical", ft.info_excerpt)
        self.assertIn("grype/results.html", ft.info_excerpt)

    def test_it_groups_into_a_shard_of_its_own(self):
        ft = job_level_failure(_status(), "died in setup")
        shards = _group_failures_by_shard([ft])
        self.assertEqual(len(shards), 1)
        self.assertEqual(shards[0][1], SHARD)


class JobLevelPromptRendering(unittest.TestCase):
    """No test list means no runner invocation, whatever the category."""

    def test_runner_section_forbids_a_bare_suite_run(self):
        section = _category_runner_section(
            "stateless", [], SHARD, Path("/work/ch"), REPORT_URL,
            job_level=True,
        )
        self.assertNotIn("clickhouse-test", section)
        self.assertIn("no test list", section.lower())
        self.assertIn(REPORT_URL, section)
        self.assertIn(SHARD, section)
        for placeholder in (
            "{tests_arg}", "{shard_context}", "{target_url}",
            "{failed_tests_file}",
        ):
            self.assertNotIn(placeholder, section)

    def test_block_says_it_is_not_a_test(self):
        ft = job_level_failure(_status(), "died in setup")
        block = _render_failure_block(ft, 1, {}, 2, "https://pr")
        self.assertIn("Job-level failure", block)
        self.assertIn("not a test", block)

    def test_manifest_never_lists_a_job_name(self):
        with _tmpdir() as repo:
            _write_failed_tests_manifest(
                repo, [job_level_failure(_status(), "died in setup")],
            )
            body = (repo / ".releasy" / "failed-tests.txt").read_text()
            self.assertEqual(body, "")

    def test_manifest_keeps_real_tests_of_a_mixed_shard(self):
        with _tmpdir() as repo:
            _write_failed_tests_manifest(repo, [
                job_level_failure(_status(), "died in setup"),
                FailedTest(
                    name="00001_select_one", status="FAIL",
                    category="stateless", shard_context=SHARD,
                    target_url=REPORT_URL,
                ),
            ])
            body = (repo / ".releasy" / "failed-tests.txt").read_text()
            self.assertEqual(body, "00001_select_one\n")


SHARD_A = "Regression aarch64 iceberg_2"
SHARD_B = "Regression release iceberg_2"


def _testflows_status(context: str) -> FailedStatus:
    return FailedStatus(
        context=context, state="failure",
        target_url=f"https://artifacts.example/{context}/report.html",
        description="", category="regression",
        locator=TestFlowsLocator(
            report_dir=f"https://artifacts.example/{context}",
        ),
    )


FAILS_LOG_A = (
    "✘ 1m [  Fail  ] /iceberg/feature/export/a\n"
    "    AssertionError\n"
    "✘ 1m [  Fail  ] /iceberg/feature/export/b\n"
    "    AssertionError\n"
)
# Same first scenario as A, plus one only this arch saw.
FAILS_LOG_B = (
    "✘ 1m [  Fail  ] /iceberg/feature/export/a\n"
    "    AssertionError\n"
    "✘ 1m [  Fail  ] /iceberg/feature/export/c\n"
    "    AssertionError\n"
)


class DedupeAccounting(unittest.TestCase):
    """A check absorbed by the cross-shard dedupe is still accounted for."""

    def _discover(self, logs: dict[str, str]):
        statuses = [_testflows_status(c) for c in logs]

        def _fetch_log(locator, **_kw):
            for ctx, text in logs.items():
                if locator.report_dir.endswith(ctx):
                    return text, None
            return None, "not found"

        with mock.patch.object(
            ci_failures, "fetch_failed_statuses",
            return_value=(statuses, None),
        ), mock.patch.object(
            ci_failures, "fetch_testflows_fails_log", _fetch_log,
        ):
            failures, err = ci_failures.discover_pr_failures(
                None, "https://github.com/o/r/pull/1",
                head_sha="deadbeef", head_ref="head", base_ref="base",
            )
        self.assertIsNone(err)
        return failures

    def test_fully_absorbed_check_is_reported(self):
        failures = self._discover({
            SHARD_A: FAILS_LOG_A, SHARD_B: FAILS_LOG_A,
        })
        self.assertEqual(
            {t.shard_context for t in failures.failed_tests}, {SHARD_A},
        )
        self.assertEqual(len(failures.covered_elsewhere), 1)
        note = failures.covered_elsewhere[0]
        self.assertTrue(note.startswith(f"{SHARD_B}:"))
        self.assertIn("all 2 failure(s)", note)
        self.assertIn(SHARD_A, note)

    def test_partially_absorbed_check_reports_both_halves(self):
        failures = self._discover({
            SHARD_A: FAILS_LOG_A, SHARD_B: FAILS_LOG_B,
        })
        self.assertEqual(
            {t.shard_context for t in failures.failed_tests},
            {SHARD_A, SHARD_B},
        )
        note = failures.covered_elsewhere[0]
        self.assertIn("1 of 2 failure(s)", note)
        self.assertIn("the remaining 1", note)

    def test_distinct_checks_produce_no_notes(self):
        failures = self._discover({
            SHARD_A: FAILS_LOG_A,
            SHARD_B: (
                "✘ 1m [  Fail  ] /iceberg/feature/export/z\n"
                "    AssertionError\n"
            ),
        })
        self.assertEqual(failures.covered_elsewhere, [])


class UndecomposableCheckPolicy(unittest.TestCase):
    """``job_level`` decides shard-or-warning for unreadable checks."""

    def _discover(self, *, job_level: bool):
        statuses = [_status(
            context="Grype Scan clickhouse-server",
            category=CATEGORY_OTHER,
            target_url="https://artifacts.example/grype/results.html",
        )]
        with mock.patch.object(
            ci_failures, "fetch_failed_statuses",
            return_value=(statuses, None),
        ):
            failures, err = ci_failures.discover_pr_failures(
                None, "https://github.com/o/r/pull/1",
                head_sha="deadbeef", head_ref="head", base_ref="base",
                job_level=job_level,
            )
        self.assertIsNone(err)
        return failures

    def test_default_hands_the_check_over_as_a_shard(self):
        failures = self._discover(job_level=True)
        self.assertEqual(len(failures.failed_tests), 1)
        self.assertTrue(failures.failed_tests[0].job_level)
        self.assertEqual(failures.skipped_status_warnings, [])

    def test_opt_out_reports_it_as_a_warning(self):
        failures = self._discover(job_level=False)
        self.assertEqual(failures.failed_tests, [])
        self.assertEqual(len(failures.skipped_status_warnings), 1)
        self.assertIn(
            "Grype Scan", failures.skipped_status_warnings[0],
        )


def _tmpdir():
    import tempfile
    from contextlib import contextmanager

    @contextmanager
    def _ctx():
        with tempfile.TemporaryDirectory() as d:
            yield Path(d)

    return _ctx()


if __name__ == "__main__":
    unittest.main()
