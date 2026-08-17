"""`reverted`: a port that merged and was then taken back out of target.

Covers the two promises the status makes — the graph issue says so in a
section of its own, and nothing ever ports the unit again.
"""

from __future__ import annotations

import unittest

import releasy.dag_discovery as d
from releasy.state import FeatureState

from test_graph_update import URL, node, report


def reverted_fs(reason="reverted by #2217", pr="https://github.com/o/r/pull/9"):
    return FeatureState(
        status="reverted", rebase_pr_url=pr, skip_reason=reason,
    )


class TestRevertedRendering(unittest.TestCase):
    def body(self, nodes, progress):
        return d.render_graph_issue_body(report(nodes), progress)

    def test_own_section_not_discarded(self):
        body = self.body([node("pr-1", 1)], {"pr-1": reverted_fs()})
        self.assertIn("### ↩ Reverted — do NOT re-port", body)
        self.assertNotIn("🗑 <b>Discarded</b>", body)

    def test_section_states_the_reason_and_port_pr(self):
        body = self.body([node("pr-1", 1)], {"pr-1": reverted_fs()})
        self.assertIn("↩ reverted (do not re-port) · reverted by #2217", body)
        self.assertIn("https://github.com/o/r/pull/9", body)

    def test_left_out_of_working_lists(self):
        body = self.body(
            [node("pr-1", 1), node("pr-2", 2)],
            {"pr-1": reverted_fs()},
        )
        standalone = body.split("### Standalone PRs")[1].split("###")[0]
        self.assertIn(URL(2), standalone)
        self.assertNotIn(URL(1), standalone)

    def test_group_renders_its_member_prs(self):
        body = self.body(
            [node("grp", 1, 2, group=True)], {"grp": reverted_fs()},
        )
        self.assertIn("**`grp`** · 2 PRs", body)
        self.assertIn(URL(1), body)
        self.assertIn(URL(2), body)
        self.assertNotIn("<details open>", body)

    def test_not_counted_as_ported_but_still_tallied(self):
        body = self.body(
            [node("pr-1", 1), node("pr-2", 2)],
            {"pr-1": reverted_fs(), "pr-2": FeatureState(status="merged",
                                                         rebase_pr_url=URL(8))},
        )
        self.assertIn("**Progress: 1/2 unit(s) ported**", body)
        self.assertIn("↩ reverted (do not re-port): 1", body)

    def test_headline_warns(self):
        body = self.body([node("pr-1", 1)], {"pr-1": reverted_fs()})
        self.assertIn("do not port them again", body)

    def test_absent_when_nothing_reverted(self):
        body = self.body([node("pr-1", 1)], {})
        self.assertNotIn("Reverted", body)


class _PRPolicy:
    def __init__(self, closed=False, reverted=False):
        self.recreate_closed_prs = closed
        self.recreate_reverted_prs = reverted


class _Config:
    def __init__(self, **kw):
        self.pr_policy = _PRPolicy(**kw)


class TestRevertedIsTerminal(unittest.TestCase):
    def test_terminal_by_default(self):
        from releasy.pipeline import terminal_statuses
        self.assertIn("reverted", terminal_statuses(_Config()))

    def test_closed_opt_in_does_not_reach_it(self):
        """`recreate_closed_prs` re-enters `closed` only — never `reverted`."""
        from releasy.pipeline import terminal_statuses
        terminal = terminal_statuses(_Config(closed=True))
        self.assertNotIn("closed", terminal)
        self.assertIn("reverted", terminal)

    def test_its_own_opt_in_re_enters_it(self):
        from releasy.pipeline import terminal_statuses
        terminal = terminal_statuses(_Config(reverted=True))
        self.assertNotIn("reverted", terminal)
        self.assertIn("closed", terminal)

    def test_always_terminal_regardless_of_flags(self):
        from releasy.pipeline import terminal_statuses
        terminal = terminal_statuses(_Config(closed=True, reverted=True))
        self.assertEqual(terminal, {"merged", "skipped", "superseded"})

    def test_opt_in_recreates_on_a_renumbered_branch(self):
        from releasy.pipeline import _recreate_opt_in
        self.assertIsNone(_recreate_opt_in(_Config(), "reverted"))
        self.assertIsNone(_recreate_opt_in(_Config(reverted=True), "merged"))
        self.assertIsNone(_recreate_opt_in(_Config(reverted=True), None))
        why, flag = _recreate_opt_in(_Config(reverted=True), "reverted")
        self.assertEqual(flag, "recreate_reverted_prs")
        self.assertIn("reverted", why.lower())

    def test_flag_defaults_to_false(self):
        from releasy.config import PRPolicyConfig
        self.assertFalse(PRPolicyConfig().recreate_reverted_prs)

    def test_merge_sweeps_never_touch_it(self):
        """No sweep flips a reverted entry back to merged/closed/superseded."""
        import inspect

        import releasy.pipeline as p
        for fn in (
            p._refresh_all_merge_status_from_github,
            p._refresh_all_superseded_status_from_github,
        ):
            for line in inspect.getsource(fn).splitlines():
                if line.strip().startswith("refreshable"):
                    self.assertNotIn("reverted", line)

    def test_status_vocabulary_is_complete(self):
        from releasy.github_ops import STATUS_MAP, STATUS_OPTIONS
        from releasy.state import STATUS_DISPLAY_ORDER
        from releasy.status import STATUS_HEADINGS, STATUS_ICONS

        self.assertIn("reverted", STATUS_ICONS)
        self.assertIn("reverted", STATUS_HEADINGS)
        self.assertIn("reverted", STATUS_DISPLAY_ORDER)
        self.assertIn("reverted", d._PROGRESS_MARKER)
        self.assertIn("reverted", d._PROGRESS_SUMMARY_ORDER)
        self.assertEqual(STATUS_MAP["reverted"], "Reverted")
        self.assertIn("Reverted", STATUS_OPTIONS)


if __name__ == "__main__":
    unittest.main()
