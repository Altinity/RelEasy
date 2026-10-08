from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

import releasy.dag_discovery as d
import releasy.pipeline as pl
from releasy.config import PRGroupConfig
from releasy.state import FeatureState, PipelineState

from test_graph_update import URL, node, report
from test_on_hold import cfg_with


def group_state(status="build_failed", **kw):
    return PipelineState(features={
        "grp": FeatureState(
            status=status, pr_url=URL(1),
            pr_urls=[URL(1), URL(2), URL(3)], **kw,
        ),
    })


class MarkOutdatedUnits(unittest.TestCase):
    def mark(self, state, nodes):
        cfg = cfg_with(Path(tempfile.mkdtemp()))
        saved = d.load_state, d.save_state
        d.load_state = lambda c: state
        d.save_state = lambda s, c: None
        try:
            return d.mark_outdated_units(cfg, report(nodes))
        finally:
            d.load_state, d.save_state = saved

    def test_lost_member_marks_port(self):
        state = group_state()
        self.assertEqual(self.mark(state, [node("grp", 1, 2)]), ["grp"])
        self.assertEqual(state.features["grp"].outdated, "#3 left the unit")

    def test_same_members_not_marked(self):
        state = group_state()
        self.assertEqual(self.mark(state, [node("grp", 1, 2, 3)]), [])
        self.assertIsNone(state.features["grp"].outdated)

    def test_auto_prereq_is_not_a_lost_member(self):
        state = group_state(dynamic_prereq_urls=[URL(3)])
        self.assertEqual(self.mark(state, [node("grp", 1, 2)]), [])

    def test_merged_port_not_marked(self):
        state = group_state(status="merged")
        self.assertEqual(self.mark(state, [node("grp", 1, 2)]), [])
        self.assertIsNone(state.features["grp"].outdated)

    def test_issue_shows_the_mark(self):
        fs = group_state().features["grp"]
        fs.outdated = "#3 left the unit"
        self.assertIn(
            "♻ outdated, re-ported on next run: #3 left the unit",
            d._progress_note(fs, 3),
        )


class AbsorbedSingleton(unittest.TestCase):
    def prune(self, pr_state="open", on_hold=(), pr_numbers=()):
        cfg = cfg_with(
            Path(tempfile.mkdtemp()), on_hold=list(on_hold),
            groups=[PRGroupConfig(id="grp", prs=[URL(1), URL(2), URL(3)])],
        )
        state = PipelineState(features={
            "pr-2": FeatureState(
                status="needs_review", pr_url=URL(2), rebase_pr_url=URL(20),
                pr_numbers=list(pr_numbers),
            ),
        })
        closed = []
        self.unboarded = []
        saved = (
            pl.fetch_pr_by_url, pl.close_pull_request, pl.remove_pr_from_projects,
        )
        pl.fetch_pr_by_url = lambda c, u, include_closed=False: (
            SimpleNamespace(state=pr_state)
        )
        pl.close_pull_request = (
            lambda c, n, comment=None: closed.append((n, comment)) or True
        )
        pl.remove_pr_from_projects = (
            lambda c, n: self.unboarded.append(n) or True
        )
        try:
            self.assertTrue(pl._prune_superseded_singletons(cfg, state))
        finally:
            (
                pl.fetch_pr_by_url, pl.close_pull_request,
                pl.remove_pr_from_projects,
            ) = saved
        self.assertNotIn("pr-2", state.features)
        return closed

    def test_open_port_pr_closed(self):
        self.assertEqual(
            self.prune(),
            [(20, "Superseded: #2 is now ported as part of group `grp`.")],
        )
        self.assertEqual(self.unboarded, [20])

    def test_replaced_group_port_pr_closed(self):
        self.assertEqual(
            self.prune(pr_numbers=[2, 3]),
            [(20, "Superseded: #2 is now ported as part of group `grp`.")],
        )
        self.assertEqual(self.unboarded, [20])

    def test_closed_port_pr_removed_from_boards(self):
        self.assertEqual(self.prune(pr_state="closed"), [])
        self.assertEqual(self.unboarded, [20])

    def test_merged_port_pr_not_touched(self):
        self.assertEqual(self.prune(pr_state="merged"), [])
        self.assertEqual(self.unboarded, [])

    def test_held_group_leaves_pr_open(self):
        self.assertEqual(self.prune(on_hold=[URL(1)]), [])
        self.assertEqual(self.unboarded, [])


if __name__ == "__main__":
    unittest.main()
