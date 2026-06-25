"""Unit tests for auto-continuing a partially-applied group.

Covers the three pieces of the feature that are cleanly unit-testable:
the partial-group marker, the per-feature attempt counter's persistence,
and the ``pr_policy.max_partial_continue_attempts`` cap config.

Stdlib unittest (no pytest dependency). Run:
    python3 -m unittest discover -s tests
"""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

import releasy.pipeline as p
from releasy.config import load_config, save_config
from releasy.state import FeatureState, _parse_features


class IsPartialGroup(unittest.TestCase):
    """``_is_partial_group`` marks only the draft-PR (idx>0) conflict flavour."""

    def test_none(self):
        self.assertFalse(p._is_partial_group(None))

    def test_clean_unit_not_partial(self):
        self.assertFalse(p._is_partial_group(FeatureState(status="needs_review")))

    def test_conflict_first_pick_failed_not_partial(self):
        # idx==0 flavour: nothing kept, partial_pr_count == 0.
        self.assertFalse(
            p._is_partial_group(
                FeatureState(status="conflict", partial_pr_count=0)
            )
        )

    def test_conflict_no_partial_count_not_partial(self):
        # A singleton conflict never sets partial_pr_count.
        self.assertFalse(
            p._is_partial_group(
                FeatureState(status="conflict", partial_pr_count=None)
            )
        )

    def test_partial_group_is_partial(self):
        self.assertTrue(
            p._is_partial_group(
                FeatureState(status="conflict", partial_pr_count=2)
            )
        )

    def test_partial_count_set_but_status_resolved_not_partial(self):
        # Once healed to needs_review, stale partial_pr_count must not
        # re-trigger auto-continue (this is the no-infinite-loop guard).
        self.assertFalse(
            p._is_partial_group(
                FeatureState(status="needs_review", partial_pr_count=2)
            )
        )


class AttemptCounterPersistence(unittest.TestCase):
    """``partial_continue_attempts`` survives the state parse round-trip."""

    def test_default_zero_when_absent(self):
        feats = _parse_features({"f1": {"status": "conflict"}})
        self.assertEqual(feats["f1"].partial_continue_attempts, 0)

    def test_explicit_value_parsed(self):
        feats = _parse_features(
            {"f1": {"status": "conflict", "partial_continue_attempts": 3}}
        )
        self.assertEqual(feats["f1"].partial_continue_attempts, 3)

    def test_null_coerced_to_zero(self):
        feats = _parse_features(
            {"f1": {"status": "conflict", "partial_continue_attempts": None}}
        )
        self.assertEqual(feats["f1"].partial_continue_attempts, 0)


class MaxPartialContinueAttemptsConfig(unittest.TestCase):
    """``pr_policy.max_partial_continue_attempts`` default, disable, round-trip."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._prev_state_dir = os.environ.get("RELEASY_STATE_DIR")
        os.environ["RELEASY_STATE_DIR"] = self._tmp.name

    def tearDown(self):
        if self._prev_state_dir is None:
            os.environ.pop("RELEASY_STATE_DIR", None)
        else:
            os.environ["RELEASY_STATE_DIR"] = self._prev_state_dir
        self._tmp.cleanup()

    def _write_config(self, pr_policy_block: str = "") -> Path:
        path = Path(self._tmp.name) / "config.yaml"
        path.write_text(
            "name: test-proj\n"
            "project: testp\n"
            "origin:\n"
            "  remote: https://github.com/o/r.git\n"
            + pr_policy_block,
            encoding="utf-8",
        )
        return path

    def test_default_is_two(self):
        cfg = load_config(self._write_config())
        self.assertEqual(cfg.pr_policy.max_partial_continue_attempts, 2)

    def test_zero_disables(self):
        cfg = load_config(
            self._write_config(
                "pr_policy:\n  max_partial_continue_attempts: 0\n"
            )
        )
        self.assertEqual(cfg.pr_policy.max_partial_continue_attempts, 0)

    def test_round_trip(self):
        cfg = load_config(
            self._write_config(
                "pr_policy:\n  max_partial_continue_attempts: 5\n"
            )
        )
        out = Path(self._tmp.name) / "out.yaml"
        save_config(cfg, out)
        self.assertEqual(
            load_config(out).pr_policy.max_partial_continue_attempts, 5
        )


if __name__ == "__main__":
    unittest.main()
