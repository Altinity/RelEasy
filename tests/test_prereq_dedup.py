"""Regression tests for the auto-prereq queued-elsewhere guard.

Covers the case where a missing-prereq report names releasy's OWN
in-flight port PR (e.g. #1950, the target-branch port of #1687) rather
than the upstream source PR. The guard must recognise it as already
queued so the dive aborts instead of re-porting the branch into a
duplicate combined PR.
"""

from __future__ import annotations

import unittest
from types import SimpleNamespace

from releasy.pipeline import _find_already_queued_prereqs
from releasy.state import FeatureState


def _cfg(include_prs=None, groups=None):
    return SimpleNamespace(
        pr_sources=SimpleNamespace(
            include_prs=include_prs or [], groups=groups or [],
        )
    )


def _url(n):
    return f"https://github.com/Altinity/ClickHouse/pull/{n}"


class QueuedPrereqGuard(unittest.TestCase):
    def setUp(self):
        # Feature pr-1687 ported upstream #1687 → its port PR is #1950.
        self.state = SimpleNamespace(features={
            "pr-1687": FeatureState(
                pr_urls=[_url(1687)], rebase_pr_url=_url(1950),
            ),
        })

    def test_own_port_pr_recognized(self):
        """A prereq naming the port PR (#1950) is flagged as queued."""
        out = _find_already_queued_prereqs(
            _cfg(), self.state, [_url(1950)], exclude_feature_id="pr-1759",
        )
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["queued_in"], "pr-1687")
        self.assertEqual(out[0]["queued_in_pr_url"], _url(1950))

    def test_upstream_source_pr_still_recognized(self):
        """The original source-PR match path still works."""
        out = _find_already_queued_prereqs(
            _cfg(), self.state, [_url(1687)], exclude_feature_id="pr-1759",
        )
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["queued_in"], "pr-1687")

    def test_unrelated_pr_not_flagged(self):
        """A genuinely missing prereq is not a false positive."""
        out = _find_already_queued_prereqs(
            _cfg(), self.state, [_url(9999)], exclude_feature_id="pr-1759",
        )
        self.assertEqual(out, [])

    def test_excluded_feature_skipped(self):
        """A unit's own port PR isn't flagged as queued against itself."""
        out = _find_already_queued_prereqs(
            _cfg(), self.state, [_url(1950)], exclude_feature_id="pr-1687",
        )
        self.assertEqual(out, [])


if __name__ == "__main__":
    unittest.main()
