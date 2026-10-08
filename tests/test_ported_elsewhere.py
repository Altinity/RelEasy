"""A PR already ported inside a merged group is not ported again.

Regression for a merged combined port whose node dropped out of the graph:
its members came back as singletons and `run` opened duplicate port PRs,
`graph update` re-added them, and vetoing one was refused because the
merged group "is atomic".
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import releasy.dag_discovery as d
import releasy.pipeline as pl
import releasy.pr_membership as pm
from releasy.state import FeatureState, PipelineState

from test_graph_update import URL, node, report
from test_on_hold import cfg_with, unit


def merged_group_state():
    """Merged group ``grp`` (#1, #2) plus a later singleton port of #2."""
    return PipelineState(features={
        "grp": FeatureState(
            status="merged", pr_url=URL(1),
            pr_urls=[URL(1), URL(2)], rebase_pr_url=URL(10),
        ),
        "pr-2": FeatureState(
            status="needs_review", pr_url=URL(2),
            rebase_pr_url=URL(11),
        ),
    })


class RunSkipsPortedElsewhere(unittest.TestCase):
    def test_singleton_of_merged_group_member_skipped(self):
        cfg = cfg_with(Path(tempfile.mkdtemp()))
        self.assertTrue(
            pl._skip_ported_elsewhere(cfg, merged_group_state(), unit("pr-2", 2)),
        )

    def test_pr_outside_the_group_not_skipped(self):
        cfg = cfg_with(Path(tempfile.mkdtemp()))
        self.assertFalse(
            pl._skip_ported_elsewhere(cfg, merged_group_state(), unit("pr-3", 3)),
        )

    def test_unmerged_group_does_not_count(self):
        cfg = cfg_with(Path(tempfile.mkdtemp()))
        state = merged_group_state()
        state.features["grp"].status = "needs_review"
        self.assertFalse(pl._skip_ported_elsewhere(cfg, state, unit("pr-2", 2)))


class GraphUpdateDoesNotReadd(unittest.TestCase):
    def test_pr_ported_by_merged_group_dropped(self):
        prior = report([node("u1", 5)])
        w = []
        new = d._build_report_from_spec(
            prior,
            {"units": [{"id": "u1", "prs": [URL(5)]},
                       {"id": "pr-2", "prs": [URL(2)]}]},
            w, state=merged_group_state(),
        )
        self.assertEqual({n.unit_id for n in new.nodes}, {"u1"})
        self.assertTrue(any("already ported by merged grp" in x for x in w))


class RemoveSkipsMergedGroup(unittest.TestCase):
    def test_veto_purges_singleton_keeps_merged_group(self):
        cfg = cfg_with(Path(tempfile.mkdtemp()), include_prs=[URL(2)])
        state = merged_group_state()
        saved = pm.load_state, pm.save_state
        pm.load_state = lambda c: state
        pm.save_state = lambda s, c: None
        try:
            self.assertTrue(pm.remove_pr(cfg, URL(2)))
        finally:
            pm.load_state, pm.save_state = saved
        self.assertEqual(set(state.features), {"grp"})
        self.assertNotIn(URL(2), cfg.pr_sources.include_prs)
        self.assertIn(URL(2), cfg.pr_sources.exclude_prs)

    def test_veto_marks_unmerged_group_outdated(self):
        cfg = cfg_with(Path(tempfile.mkdtemp()), include_prs=[URL(2)])
        state = merged_group_state()
        state.features["grp"].status = "build_failed"
        saved = pm.load_state, pm.save_state
        pm.load_state = lambda c: state
        pm.save_state = lambda s, c: None
        try:
            self.assertTrue(pm.remove_pr(cfg, URL(2)))
        finally:
            pm.load_state, pm.save_state = saved
        self.assertEqual(set(state.features), {"grp"})
        self.assertEqual(state.features["grp"].outdated, "#2 removed")
        self.assertIn(URL(2), cfg.pr_sources.exclude_prs)


if __name__ == "__main__":
    unittest.main()
