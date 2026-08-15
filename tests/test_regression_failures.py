"""Unit tests for TestFlows (regression suite) failure discovery.

Covers the pieces that used to make ``analyze-fails`` skip regression
checks entirely: status-context classification, TestFlows report-URL
parsing, ``fails.log.txt`` parsing (leaf-only, XFail-muted), and the
per-category prompt sections.

Fixtures are trimmed verbatim from the artefacts of
Altinity/ClickHouse#2210 (``Regression aarch64 swarms`` and
``Regression aarch64 iceberg_2``).

Stdlib unittest (no pytest dependency). Run:
    python3 -m unittest discover -s tests
"""
from __future__ import annotations

import unittest
from pathlib import Path

from releasy.analyze_fails import (
    _category_prior_section,
    _category_runner_section,
    _tests_arg,
)
from releasy.ci_failures import (
    CATEGORY_ORDER,
    CATEGORY_OTHER,
    ArtifactLocator,
    TestFlowsLocator,
    category_from_name,
    extract_regression_failures,
    locator_from_target_url,
    parse_testflows_fails_log,
)

S3 = "https://altinity-build-artifacts.s3.amazonaws.com"
SWARMS_REPORT = (
    f"{S3}/REFs/2210/merge/c1efccca5eb72115b0fd347002e24d426e018d21"
    "/regression/aarch64/with_analyzer/zookeeper/without_thread_fuzzer"
    "/swarms/report.html"
)
PRAKTIKA_TARGET = (
    f"{S3}/json.html?PR=2210&sha=c1efccc&name_0=PR"
    "&name_1=Stateless%20tests%20%28amd_debug%2C%20sequential%29"
)

# The detail section lists leaves *and* every enclosing node; only the
# top-level node carries the traceback.
SWARMS_FAILS_LOG = """\
✘ 1m 53s    [  Fail  ] /swarms/feature/node failure/check restart swarm node
    AssertionError
✘ 11m 34s   [  Fail  ] /swarms/feature/node failure
    AssertionError
✘ 27s 739ms [  Fail  ] /swarms/feature/task rescheduling/rescheduling with bucket granularity
    AssertionError
✘ 27s 740ms [  Fail  ] /swarms/feature/task rescheduling
    AssertionError
✘ 26m 16s   [  Fail  ] /swarms/feature
    AssertionError
✘ 29m 50s   [  Fail  ] /swarms
    AssertionError
    Traceback (most recent call last):
      File "swarms/tests/node_failure.py", line 79, in run_long_query
        result = node.query(
      File "helpers/cluster.py", line 1264, in query
        assert r.exitcode == exitcode, error(r.output)
    AssertionError: Oops! Assertion failed

    The following assertion was not satisfied
      assert r.exitcode == exitcode, error(r.output)

Failing

✘ [ Fail ] '/swarms/feature/node failure/check restart swarm node' (1m 53s)
✘ [ Fail ] '/swarms/feature/node failure' (11m 34s)
✘ [ Fail ] '/swarms/feature/task rescheduling/rescheduling with bucket granularity' (27s 739ms)
✘ [ Fail ] '/swarms/feature/task rescheduling' (27s 740ms)
✘ [ Fail ] '/swarms/feature' (26m 16s)
✘ [ Fail ] '/swarms' (29m 50s)

Debugging

Rerun the first failing test by executing your test program with the '--only' option.
--only '/swarms/feature/node failure/check restart swarm node/*'

Total time 29m 50s
"""

# Iceberg mixes expected failures (annotated with an upstream issue and
# re-listed under ``Known``) with real ones.
ICEBERG_FAILS_LOG = """\
✘ 2s 330ms  [ XFail  ] /iceberg/iceberg engine/glue catalog/predicate push down/issue with decimal column
    https://github.com/ClickHouse/ClickHouse/issues/80200
✘ 1s 239ms  [  Fail  ] /iceberg/export partition/no catalog/catalogs/drop with purge
    AssertionError

Known

✘ [ XFail ] '/iceberg/iceberg engine/glue catalog/predicate push down/issue with decimal column' (2s 330ms)

Failing

✘ [ Fail ] '/iceberg/export partition/no catalog/catalogs/drop with purge' (1s 239ms)
"""


class CategoryClassification(unittest.TestCase):
    """Regression contexts get their own category; nothing falls through."""

    def test_regression_contexts(self):
        for ctx in (
            "Regression aarch64 swarms",
            "Regression release s3_export_part",
            "Regression aarch64 iceberg_2",
        ):
            self.assertEqual(category_from_name(ctx), "regression", ctx)

    def test_known_categories_unchanged(self):
        self.assertEqual(category_from_name("Fast test"), "fasttest")
        self.assertEqual(
            category_from_name("Stateless tests (amd_debug, sequential)"),
            "stateless",
        )
        self.assertEqual(
            category_from_name("Integration tests (arm_binary)"),
            "integration",
        )
        self.assertEqual(
            category_from_name("Quick functional tests"), "quick_functional",
        )

    def test_unknown_context_is_processed_not_dropped(self):
        for ctx in ("Stress test (amd_debug)", "AST fuzzer", "Build (arm)"):
            self.assertEqual(category_from_name(ctx), CATEGORY_OTHER, ctx)

    def test_every_category_has_an_order(self):
        for cat in (
            "fasttest", "quick_functional", "stateless", "integration",
            "regression", CATEGORY_OTHER,
        ):
            self.assertIn(cat, CATEGORY_ORDER)

    def test_regression_sorts_after_the_in_repo_suites(self):
        self.assertGreater(
            CATEGORY_ORDER["regression"], CATEGORY_ORDER["stateless"],
        )


class LocatorDispatch(unittest.TestCase):
    """A target_url resolves to whichever report kind it points at."""

    def test_testflows_report(self):
        loc = locator_from_target_url(SWARMS_REPORT)
        self.assertIsInstance(loc, TestFlowsLocator)
        self.assertTrue(loc.fails_log_url().endswith("/swarms/fails.log.txt"))
        self.assertEqual(loc.report_url(), SWARMS_REPORT)

    def test_praktika_report_still_wins(self):
        loc = locator_from_target_url(PRAKTIKA_TARGET)
        self.assertIsInstance(loc, ArtifactLocator)

    def test_job_log_has_no_locator(self):
        self.assertIsNone(locator_from_target_url(
            "https://github.com/Altinity/ClickHouse/actions/runs/1/job/2",
        ))
        self.assertIsNone(locator_from_target_url(""))


class FailsLogParsing(unittest.TestCase):
    """``fails.log.txt`` folds both of its listings into one entry per path."""

    def test_every_node_is_parsed_once(self):
        entries = parse_testflows_fails_log(SWARMS_FAILS_LOG)
        self.assertEqual(len(entries), 6)
        self.assertIn("/swarms/feature/node failure", entries)

    def test_detail_block_is_captured_and_dedented(self):
        entries = parse_testflows_fails_log(SWARMS_FAILS_LOG)
        root = entries["/swarms"]
        self.assertTrue(root.detail.startswith("AssertionError"))
        self.assertIn("Traceback (most recent call last):", root.detail)
        # The trailing "Failing" heading is not part of the block.
        self.assertNotIn("Failing", root.detail)

    def test_summary_only_entry_keeps_its_status(self):
        entries = parse_testflows_fails_log(
            "Failing\n\n✘ [ Error ] '/suite/only summarised' (1s)\n",
        )
        self.assertEqual(entries["/suite/only summarised"].status, "ERROR")


class LeafExtraction(unittest.TestCase):
    """Only the deepest failing scenarios become work items."""

    def _extract(self, log: str):
        return extract_regression_failures(
            log,
            category="regression",
            shard_context="Regression aarch64 swarms",
            target_url=SWARMS_REPORT,
        )

    def test_ancestors_are_dropped(self):
        names = [t.name for t in self._extract(SWARMS_FAILS_LOG)]
        self.assertEqual(names, [
            "/swarms/feature/node failure/check restart swarm node",
            "/swarms/feature/task rescheduling/"
            "rescheduling with bucket granularity",
        ])

    def test_thin_leaf_borrows_the_ancestor_traceback(self):
        leaf = self._extract(SWARMS_FAILS_LOG)[0]
        self.assertIn("AssertionError", leaf.info_excerpt)
        self.assertIn("Traceback (most recent call last):", leaf.info_excerpt)
        self.assertIn("'/swarms'", leaf.info_excerpt)

    def test_shared_ancestor_traceback_is_attached_once(self):
        first, second = self._extract(SWARMS_FAILS_LOG)
        # One enclosing node can span hundreds of leaves; repeating its
        # single traceback on each would mislead and swamp the prompt.
        self.assertIn("Traceback (most recent call last):", first.info_excerpt)
        self.assertNotIn(
            "Traceback (most recent call last):", second.info_excerpt,
        )
        self.assertIn("first test under it", second.info_excerpt)

    def test_shard_metadata_is_carried(self):
        leaf = self._extract(SWARMS_FAILS_LOG)[0]
        self.assertEqual(leaf.category, "regression")
        self.assertEqual(leaf.shard_context, "Regression aarch64 swarms")
        self.assertEqual(leaf.target_url, SWARMS_REPORT)
        self.assertEqual(leaf.status, "FAIL")

    def test_expected_failures_are_muted(self):
        names = [t.name for t in self._extract(ICEBERG_FAILS_LOG)]
        self.assertEqual(
            names,
            ["/iceberg/export partition/no catalog/catalogs/drop with purge"],
        )

    def test_empty_log_yields_nothing(self):
        self.assertEqual(self._extract(""), [])


class PromptSections(unittest.TestCase):
    """Regression shards get a reproduction recipe, not a placeholder."""

    REPO = Path("/work/ch")
    TESTS = [
        "/swarms/feature/node failure/check restart swarm node",
        "/swarms/feature/task rescheduling/rescheduling with bucket granularity",
    ]

    def test_only_patterns_are_quoted_and_globbed(self):
        arg = _tests_arg("regression", self.TESTS)
        self.assertIn(
            "'/swarms/feature/node failure/check restart swarm node/*'", arg,
        )

    def test_other_categories_keep_bare_names(self):
        self.assertEqual(
            _tests_arg("stateless", ["00001_select_1"]), "00001_select_1",
        )

    def test_regression_section_is_actionable(self):
        section = _category_runner_section(
            "regression", self.TESTS, "Regression aarch64 swarms", self.REPO,
            SWARMS_REPORT,
        )
        self.assertIn("clickhouse-regression", section)
        self.assertIn("--only", section)
        self.assertIn("/work/ch/build/programs/clickhouse", section)
        self.assertIn("Regression aarch64 swarms", section)
        self.assertIn(SWARMS_REPORT.removesuffix("/report.html"), section)
        self.assertIn("nice-new-fails.log.txt", section)
        for placeholder in ("{repo_dir}", "{tests_arg}", "{report_dir}"):
            self.assertNotIn(placeholder, section)

    def test_long_regression_list_gets_the_array_recipe(self):
        section = _category_runner_section(
            "regression", [f"/s3/minio/case {i}" for i in range(60)],
            "Regression release s3_export_part", self.REPO,
        )
        self.assertIn(".releasy/failed-tests.txt", section)
        self.assertIn("mapfile", section)

    def test_unknown_category_falls_back_to_the_generic_hint(self):
        section = _category_runner_section(
            CATEGORY_OTHER, ["some_case"], "Stress test (amd_debug)", self.REPO,
        )
        self.assertIn("ci/defs/job_configs.py", section)
        self.assertIn("Stress test (amd_debug)", section)
        self.assertNotIn("{tests_arg}", section)

    def test_new_categories_have_triage_priors(self):
        for cat in ("regression", "quick_functional", CATEGORY_OTHER):
            self.assertNotIn(
                "no category-specific prior", _category_prior_section(cat),
            )


if __name__ == "__main__":
    unittest.main()
