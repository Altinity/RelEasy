from __future__ import annotations

import unittest

import releasy.dag_discovery as d
from releasy.github_ops import STATUS_MAP, STATUS_OPTIONS
from releasy.pipeline import _recreate_opt_in, terminal_statuses
from releasy.state import STATUS_DISPLAY_ORDER, FeatureState
from releasy.status import STATUS_HEADINGS, STATUS_ICONS

from test_graph_update import URL, node, report


def reverted_fs(reason="reverted by #2217", pr="https://github.com/o/r/pull/9"):
    return FeatureState(
        status="reverted", rebase_pr_url=pr, skip_reason=reason,
    )


class TestRevertedRendering(unittest.TestCase):
    def body(self, nodes, progress):
        return d.render_graph_issue_body(report(nodes), progress)

    def test_own_section_with_reason_port_pr_and_headline(self):
        body = self.body([node("pr-1", 1)], {"pr-1": reverted_fs()})
        self.assertIn("### ↩ Reverted — do NOT re-port", body)
        self.assertNotIn("🗑 <b>Discarded</b>", body)
        self.assertIn("↩ reverted (do not re-port) · reverted by #2217", body)
        self.assertIn("https://github.com/o/r/pull/9", body)
        self.assertIn("do not port them again", body)

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
    def test_terminal_statuses_follow_the_opt_ins(self):
        always = {"merged", "skipped", "superseded"}
        for kw, extra in (
            ({}, {"closed", "reverted"}),
            ({"closed": True}, {"reverted"}),
            ({"reverted": True}, {"closed"}),
            ({"closed": True, "reverted": True}, set()),
        ):
            with self.subTest(**kw):
                self.assertEqual(terminal_statuses(_Config(**kw)), always | extra)

    def test_opt_in_recreates_on_a_renumbered_branch(self):
        self.assertIsNone(_recreate_opt_in(_Config(), "reverted"))
        self.assertIsNone(_recreate_opt_in(_Config(reverted=True), "merged"))
        self.assertIsNone(_recreate_opt_in(_Config(reverted=True), None))
        why, flag = _recreate_opt_in(_Config(reverted=True), "reverted")
        self.assertEqual(flag, "recreate_reverted_prs")
        self.assertIn("reverted", why.lower())

    def test_status_vocabulary_is_complete(self):
        self.assertIn("reverted", STATUS_ICONS)
        self.assertIn("reverted", STATUS_HEADINGS)
        self.assertIn("reverted", STATUS_DISPLAY_ORDER)
        self.assertIn("reverted", d._PROGRESS_MARKER)
        self.assertIn("reverted", d._PROGRESS_SUMMARY_ORDER)
        self.assertEqual(STATUS_MAP["reverted"], "Reverted")
        self.assertIn("Reverted", STATUS_OPTIONS)


if __name__ == "__main__":
    unittest.main()
