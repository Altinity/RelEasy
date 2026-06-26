"""Unit tests for the deterministic build/test split.

Covers the cleanly unit-testable pieces: the new ``build_failed`` status and
verify-counter persistence, the ``ai_resolve`` config knobs, and the pure
helpers in ``build_verify`` (test detection, runner-hint selection, marker
parsing).

Stdlib unittest (no pytest dependency). Run:
    python3 -m unittest discover -s tests
"""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

import releasy.build_verify as bv
from releasy.config import (
    AIResolveConfig,
    _default_test_file_globs,
    load_config,
    save_config,
)
from releasy.state import (
    STATUS_DISPLAY_ORDER,
    FeatureState,
    PipelineState,
    _parse_features,
    load_state,
    save_state,
)


class BuildFailedStatus(unittest.TestCase):
    """``build_failed`` is a known, displayable status."""

    def test_in_display_order(self):
        self.assertIn("build_failed", STATUS_DISPLAY_ORDER)

    def test_has_icon_and_heading(self):
        from releasy.status import STATUS_ICONS, STATUS_HEADINGS
        self.assertIn("build_failed", STATUS_ICONS)
        self.assertIn("build_failed", STATUS_HEADINGS)


class VerifyCounterPersistence(unittest.TestCase):
    """New FeatureState verify fields survive the state parse round-trip."""

    def test_defaults_when_absent(self):
        feats = _parse_features({"f1": {"status": "build_failed"}})
        self.assertEqual(feats["f1"].build_attempts, 0)
        self.assertEqual(feats["f1"].verify_resume_attempts, 0)
        self.assertIsNone(feats["f1"].last_verify_error)

    def test_explicit_values_parsed(self):
        feats = _parse_features({"f1": {
            "status": "build_failed",
            "build_attempts": 5,
            "verify_resume_attempts": 2,
            "last_verify_error": "build still failing",
        }})
        self.assertEqual(feats["f1"].build_attempts, 5)
        self.assertEqual(feats["f1"].verify_resume_attempts, 2)
        self.assertEqual(feats["f1"].last_verify_error, "build still failing")

    def test_null_counters_coerced_to_zero(self):
        feats = _parse_features({"f1": {
            "status": "build_failed",
            "build_attempts": None,
            "verify_resume_attempts": None,
        }})
        self.assertEqual(feats["f1"].build_attempts, 0)
        self.assertEqual(feats["f1"].verify_resume_attempts, 0)


class StateRoundTrip(unittest.TestCase):
    """save_state → load_state preserves build_failed + verify counters."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._prev = os.environ.get("RELEASY_STATE_DIR")
        os.environ["RELEASY_STATE_DIR"] = self._tmp.name

    def tearDown(self):
        if self._prev is None:
            os.environ.pop("RELEASY_STATE_DIR", None)
        else:
            os.environ["RELEASY_STATE_DIR"] = self._prev
        self._tmp.cleanup()

    def _config(self) -> "object":
        path = Path(self._tmp.name) / "config.yaml"
        path.write_text(
            "name: test-proj\nproject: testp\n"
            "origin:\n  remote: https://github.com/o/r.git\n",
            encoding="utf-8",
        )
        return load_config(path)

    def test_round_trip(self):
        cfg = self._config()
        st = PipelineState(base_branch="b")
        st.features["f1"] = FeatureState(
            status="build_failed", branch_name="feature/b/1",
            base_commit="deadbeef", build_attempts=5,
            verify_resume_attempts=1, last_verify_error="boom",
        )
        save_state(st, cfg)
        loaded = load_state(cfg)
        fs = loaded.features["f1"]
        self.assertEqual(fs.status, "build_failed")
        self.assertEqual(fs.build_attempts, 5)
        self.assertEqual(fs.verify_resume_attempts, 1)
        self.assertEqual(fs.last_verify_error, "boom")

    def test_zero_counters_not_written(self):
        # Lazy-write: zero/empty verify fields must not bloat the YAML.
        cfg = self._config()
        st = PipelineState(base_branch="b")
        st.features["f1"] = FeatureState(
            status="needs_review", branch_name="feature/b/1",
        )
        save_state(st, cfg)
        from releasy.config import state_file_path
        text = state_file_path(cfg.name).read_text(encoding="utf-8")
        self.assertNotIn("build_attempts", text)
        self.assertNotIn("verify_resume_attempts", text)
        self.assertNotIn("last_verify_error", text)


class AIResolveBuildKnobs(unittest.TestCase):
    """The deterministic-build config knobs: defaults, parse, round-trip."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._prev = os.environ.get("RELEASY_STATE_DIR")
        os.environ["RELEASY_STATE_DIR"] = self._tmp.name

    def tearDown(self):
        if self._prev is None:
            os.environ.pop("RELEASY_STATE_DIR", None)
        else:
            os.environ["RELEASY_STATE_DIR"] = self._prev
        self._tmp.cleanup()

    def _write(self, ai_block: str = "") -> Path:
        path = Path(self._tmp.name) / "config.yaml"
        path.write_text(
            "name: test-proj\nproject: testp\n"
            "origin:\n  remote: https://github.com/o/r.git\n" + ai_block,
            encoding="utf-8",
        )
        return path

    def test_defaults(self):
        c = AIResolveConfig()
        self.assertTrue(c.deterministic_build)
        self.assertEqual(c.max_build_attempts, 5)
        self.assertEqual(c.max_verify_resume_attempts, 2)
        self.assertEqual(c.build_log_tail_lines, 500)
        self.assertTrue(c.run_pr_tests)
        self.assertEqual(c.test_file_globs, _default_test_file_globs())

    def test_parse_overrides(self):
        cfg = load_config(self._write(
            "ai_resolve:\n"
            "  deterministic_build: false\n"
            "  max_build_attempts: 8\n"
            "  max_verify_resume_attempts: 0\n"
            "  run_pr_tests: false\n"
            "  test_file_globs:\n"
            "    - 'tests/foo/**'\n"
        ))
        ai = cfg.ai_resolve
        self.assertFalse(ai.deterministic_build)
        self.assertEqual(ai.max_build_attempts, 8)
        self.assertEqual(ai.max_verify_resume_attempts, 0)
        self.assertFalse(ai.run_pr_tests)
        self.assertEqual(ai.test_file_globs, ["tests/foo/**"])

    def test_save_config_round_trip(self):
        path = self._write()
        cfg = load_config(path)
        cfg.ai_resolve.deterministic_build = False
        cfg.ai_resolve.max_build_attempts = 7
        save_config(cfg, path)
        again = load_config(path)
        self.assertFalse(again.ai_resolve.deterministic_build)
        self.assertEqual(again.ai_resolve.max_build_attempts, 7)


class TestDetection(unittest.TestCase):
    """`_touched_test_files` / `_categorise` against the default globs."""

    def test_filters_to_test_paths(self):
        globs = _default_test_file_globs()
        changed = [
            "src/Storages/StorageX.cpp",
            "tests/queries/0_stateless/01_x.sql",
            "tests/queries/0_stateless/01_x.reference",
            "tests/integration/test_a/test.py",
            "src/Storages/tests/gtest_storage.cpp",
            "docs/whatever.md",
        ]
        touched = bv._touched_test_files(changed, globs)
        self.assertIn("tests/queries/0_stateless/01_x.sql", touched)
        self.assertIn("tests/integration/test_a/test.py", touched)
        self.assertIn("src/Storages/tests/gtest_storage.cpp", touched)
        self.assertNotIn("src/Storages/StorageX.cpp", touched)
        self.assertNotIn("docs/whatever.md", touched)

    def test_no_tests_touched(self):
        globs = _default_test_file_globs()
        self.assertEqual(bv._touched_test_files(["src/a.cpp"], globs), [])

    def test_categorise(self):
        cats = bv._categorise([
            "tests/queries/0_stateless/01_x.sql",
            "tests/integration/test_a/test.py",
        ])
        self.assertEqual(cats, {"stateless", "integration"})

    def test_runner_hints_nonempty_for_known(self):
        hints = bv._runner_hints(["tests/queries/0_stateless/01_x.sql"])
        self.assertIn("clickhouse-test", hints)

    def test_runner_hints_fallback_for_unknown(self):
        hints = bv._runner_hints(["src/Storages/tests/gtest_x.cpp"])
        self.assertIn("ci/jobs", hints)


class MarkerParsing(unittest.TestCase):
    """`_last_marker` finds the final verdict, tolerating backticks/order."""

    def test_fixed(self):
        self.assertEqual(
            bv._last_marker("did stuff\nFIXED", ("FIXED", "CANNOT FIX")),
            "FIXED",
        )

    def test_cannot_fix_with_reason(self):
        self.assertEqual(
            bv._last_marker(
                "tried\nCANNOT FIX: needs upstream PR", ("FIXED", "CANNOT FIX"),
            ),
            "CANNOT FIX: needs upstream PR",
        )

    def test_backticked_marker(self):
        self.assertEqual(
            bv._last_marker("ran\n`TESTS PASSED`", ("TESTS PASSED", "TESTS FAILED")),
            "TESTS PASSED",
        )

    def test_last_wins(self):
        # If a marker word appears mid-text and again at the end, take the end.
        self.assertEqual(
            bv._last_marker(
                "FIXED was my goal\n...\nCANNOT FIX: gave up",
                ("FIXED", "CANNOT FIX"),
            ),
            "CANNOT FIX: gave up",
        )

    def test_none_when_absent(self):
        self.assertIsNone(
            bv._last_marker("no verdict here", ("FIXED", "CANNOT FIX")),
        )


if __name__ == "__main__":
    unittest.main()
