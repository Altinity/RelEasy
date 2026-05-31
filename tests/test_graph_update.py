"""Unit tests for the pure logic behind `releasy graph discover/update`.

Stdlib unittest (no pytest dependency). Run:
    python3 -m unittest discover -s tests
    python3 tests/test_graph_update.py
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import yaml

import releasy.dag_discovery as d
from releasy.config import (
    Config,
    OriginConfig,
    PRGroupConfig,
    PRSourcesConfig,
    SessionConfig,
)

URL = "https://github.com/o/r/pull/{}".format


def node(uid, *nums, deps=None, group=False, merged="2026-01-01T00:00:00+00:00"):
    urls = [URL(n) for n in nums]
    return d.DAGNode(
        unit_id=uid,
        is_user_group=group,
        pr_urls=urls,
        pr_titles=["t" + str(n) for n in nums],
        earliest_merged_at=merged,
        deps=deps or [],
        discovery_method="trial-clean",
    )


def report(nodes, *, excluded=None, **kw):
    r = d.DiscoveryReport(
        base_branch=kw.get("base_branch", "b"),
        target_sha="sha",
        generated_at="2026-05-31T00:00:00+00:00",
        candidate_unit_count=len(nodes),
        candidate_pr_count=sum(len(n.pr_urls) for n in nodes),
        skipped_already_in_target=[],
        nodes=nodes,
        components=[],
        singletons=[],
        excluded=excluded or [],
        **{k: v for k, v in kw.items() if k != "base_branch"},
    )
    r.components, r.singletons = d.recompute_components(r)
    return r


def fence(yaml_text):
    return "rationale here\n```yaml\n" + yaml_text + "\n```\n"


class ParseGraphSpec(unittest.TestCase):
    def test_valid(self):
        spec = d._parse_graph_spec(fence("units:\n  - id: u\n    prs: [%s]" % URL(1)))
        self.assertEqual(spec["units"][0]["id"], "u")

    def test_skips_leading_example_fence(self):
        txt = "```\nnot real\n```\n" + fence("units: [{id: u, prs: [%s]}]" % URL(1))
        self.assertEqual(d._parse_graph_spec(txt)["units"][0]["id"], "u")

    def test_last_valid_block_wins(self):
        txt = fence("units: [{id: old, prs: [%s]}]" % URL(9)) + fence(
            "units: [{id: new, prs: [%s]}]" % URL(8)
        )
        self.assertEqual(d._parse_graph_spec(txt)["units"][0]["id"], "new")

    def test_no_block(self):
        self.assertIsNone(d._parse_graph_spec("no fences"))

    def test_non_dict_block(self):
        self.assertIsNone(d._parse_graph_spec("```yaml\n- a\n- b\n```"))

    def test_missing_units_key(self):
        self.assertIsNone(d._parse_graph_spec("```yaml\nfoo: bar\n```"))


class HasCycle(unittest.TestCase):
    def test_cycle(self):
        self.assertTrue(d._has_cycle([node("x", 1, deps=["y"]), node("y", 2, deps=["x"])]))

    def test_acyclic(self):
        self.assertFalse(d._has_cycle([node("x", 1, deps=["y"]), node("y", 2)]))

    def test_self_loop(self):
        self.assertTrue(d._has_cycle([node("x", 1, deps=["x"])]))

    def test_diamond_is_acyclic(self):
        nodes = [
            node("a", 1, deps=["b", "c"]),
            node("b", 2, deps=["d"]),
            node("c", 3, deps=["d"]),
            node("d", 4),
        ]
        self.assertFalse(d._has_cycle(nodes))


class BuildReportFromSpec(unittest.TestCase):
    def test_new_pr_warned_and_kept(self):
        prior = report([node("u1", 1)])
        w = []
        new = d._build_report_from_spec(
            prior, {"units": [{"id": "u1", "prs": [URL(1)]}, {"id": "u2", "prs": [URL(2)]}]}, w
        )
        ids = {n.unit_id for n in new.nodes}
        self.assertEqual(ids, {"u1", "u2"})
        self.assertTrue(any("is new" in x for x in w))

    def test_prior_veto_persists_when_not_restated(self):
        prior = report([node("u1", 1)], excluded=[{"url": URL(100), "reason": "old"}])
        new = d._build_report_from_spec(prior, {"units": [{"id": "u1", "prs": [URL(1)]}]}, [])
        self.assertEqual({e["url"] for e in new.excluded}, {URL(100)})

    def test_new_veto_added(self):
        prior = report([node("u1", 1)])
        spec = {"units": [{"id": "u1", "prs": [URL(1)]}], "exclude": [{"url": URL(5), "reason": "x"}]}
        new = d._build_report_from_spec(prior, spec, [])
        self.assertEqual({e["url"] for e in new.excluded}, {URL(5)})

    def test_readd_unvetoes(self):
        prior = report([node("u1", 1)], excluded=[{"url": URL(100), "reason": "old"}])
        # PR 100 now appears as a live unit -> must drop from excluded.
        spec = {"units": [{"id": "u1", "prs": [URL(1)]}, {"id": "u100", "prs": [URL(100)]}]}
        new = d._build_report_from_spec(prior, spec, [])
        self.assertEqual(new.excluded, [])

    def test_non_list_exclude_guarded(self):
        prior = report([node("u1", 1)], excluded=[{"url": URL(100), "reason": "old"}])
        w = []
        new = d._build_report_from_spec(
            prior, {"units": [{"id": "u1", "prs": [URL(1)]}], "exclude": {"url": "x"}}, w
        )
        self.assertEqual({e["url"] for e in new.excluded}, {URL(100)})
        self.assertTrue(any("not a list" in x for x in w))

    def test_dangling_dep_dropped(self):
        prior = report([node("u1", 1)])
        w = []
        new = d._build_report_from_spec(
            prior, {"units": [{"id": "u1", "prs": [URL(1)], "depends_on": ["ghost"]}]}, w
        )
        self.assertEqual(new.nodes[0].deps, [])
        self.assertTrue(any("unknown" in x for x in w))

    def test_cycle_rejected(self):
        prior = report([node("u1", 1), node("u2", 2)])
        spec = {"units": [
            {"id": "u1", "prs": [URL(1)], "depends_on": ["u2"]},
            {"id": "u2", "prs": [URL(2)], "depends_on": ["u1"]},
        ]}
        self.assertIsNone(d._build_report_from_spec(prior, spec, []))

    def test_is_user_group_carried_from_prior(self):
        prior = report([node("G", 1, group=True)])
        new = d._build_report_from_spec(prior, {"units": [{"id": "G", "prs": [URL(1), URL(2)]}]}, [])
        self.assertTrue(new.nodes[0].is_user_group)

    def test_invalid_url_dropped(self):
        prior = report([node("u1", 1)])
        w = []
        new = d._build_report_from_spec(
            prior, {"units": [{"id": "u1", "prs": [URL(1), "not-a-url"]}]}, w
        )
        self.assertEqual(new.nodes[0].pr_urls, [URL(1)])
        self.assertTrue(any("unparseable" in x for x in w))

    def test_empty_spec_returns_none(self):
        self.assertIsNone(d._build_report_from_spec(report([node("u1", 1)]), {"units": []}, []))


class ResolveBaseBranch(unittest.TestCase):
    def _cfg(self, target=None):
        return Config(
            name="n", origin=OriginConfig(remote="git@github.com:o/r.git"),
            project="p", target_branch=target,
        )

    def test_target_branch(self):
        self.assertEqual(d._resolve_base_branch(self._cfg("antalya-26.4"), None), "antalya-26.4")

    def test_onto_with_target_set_returns_target(self):
        # base_branch_name returns target_branch when set, regardless of onto.
        self.assertEqual(d._resolve_base_branch(self._cfg("antalya-26.4"), "v1"), "antalya-26.4")

    def test_neither_raises(self):
        with self.assertRaises(ValueError):
            d._resolve_base_branch(self._cfg(None), None)


class ReconcileSessionUserGroups(unittest.TestCase):
    def _cfg_with_group(self, tmp):
        grp = PRGroupConfig(id="G", prs=[URL(1)])
        session = SessionConfig(
            pr_sources=PRSourcesConfig(groups=[grp]), session_path=tmp / "b.session.yaml",
        )
        cfg = Config(
            name="n", origin=OriginConfig(remote="git@github.com:o/r.git"),
            project="p", config_path=tmp / "config.yaml",
        )
        cfg.session = session
        return cfg, grp

    def test_edit_persisted_to_disk(self):
        tmp = Path(tempfile.mkdtemp())
        cfg, grp = self._cfg_with_group(tmp)
        rpt = report([node("G", 1, 2, deps=["H"], group=True), node("H", 3)])
        changed = d._reconcile_session_user_groups(cfg, rpt, [])
        self.assertEqual(changed, 1)
        self.assertEqual(grp.prs, [URL(1), URL(2)])
        self.assertEqual(grp.depends_on, ["H"])
        disk = yaml.safe_load((tmp / "b.session.yaml").read_text())
        g = disk["pr_sources"]["groups"][0]
        self.assertEqual(len(g["prs"]), 2)
        self.assertEqual(g["depends_on"], ["H"])

    def test_idempotent(self):
        tmp = Path(tempfile.mkdtemp())
        cfg, _ = self._cfg_with_group(tmp)
        rpt = report([node("G", 1, group=True)])
        self.assertEqual(d._reconcile_session_user_groups(cfg, rpt, []), 0)

    def test_missing_group_warns(self):
        tmp = Path(tempfile.mkdtemp())
        cfg, _ = self._cfg_with_group(tmp)
        rpt = report([node("GHOST", 9, group=True)])
        w = []
        self.assertEqual(d._reconcile_session_user_groups(cfg, rpt, w), 0)
        self.assertTrue(any("no matching session group" in x for x in w))


class IssueBodyAndRoundTrip(unittest.TestCase):
    def test_render_contains_key_parts(self):
        r = report([node("u1", 1, deps=["u2"]), node("u2", 2)], excluded=[{"url": URL(9), "reason": "v"}])
        body = d.render_graph_issue_body(r)
        self.assertIn("mermaid", body)
        self.assertIn(d._issue_marker("b"), body)
        self.assertIn("#9", body)
        self.assertIn("Excluded", body)

    def test_render_empty_graph_ok(self):
        d.render_graph_issue_body(report([]))  # must not raise

    def test_report_round_trip(self):
        tmp = Path(tempfile.mkdtemp())
        rp = tmp / "graph.b.yaml"
        r = report(
            [node("u1", 1)],
            excluded=[{"url": URL(9), "reason": "v"}],
            issue_number=77, issue_url="https://x/77",
            last_ingested_at="2026-05-30T12:00:00+00:00",
        )
        d._write_report(r, rp)
        back = d.load_report(rp)
        self.assertEqual(back.issue_number, 77)
        self.assertEqual(back.last_ingested_at, "2026-05-30T12:00:00+00:00")
        self.assertEqual(back.excluded, [{"url": URL(9), "reason": "v"}])


class OpenOrUpdateIssue(unittest.TestCase):
    """#12 — recreate when the tracked issue 404s; never recreate on transient."""

    def setUp(self):
        self._update, self._create = d.update_issue, d.create_issue

    def tearDown(self):
        d.update_issue, d.create_issue = self._update, self._create

    def _cfg(self):
        return Config(name="n", origin=OriginConfig(remote="git@github.com:o/r.git"), project="p")

    def test_update_success(self):
        d.update_issue = lambda *a, **k: True
        d.create_issue = lambda *a, **k: self.fail("must not create")
        r = report([node("u1", 1)], issue_number=5, issue_url="u")
        self.assertEqual(d.open_or_update_graph_issue(self._cfg(), r, title="t"), (5, "u"))

    def test_404_recreates(self):
        d.update_issue = lambda *a, **k: None  # 404
        d.create_issue = lambda *a, **k: (99, "newurl")
        r = report([node("u1", 1)], issue_number=5, issue_url="old")
        self.assertEqual(d.open_or_update_graph_issue(self._cfg(), r, title="t"), (99, "newurl"))
        self.assertEqual(r.issue_number, 99)

    def test_transient_failure_does_not_recreate(self):
        d.update_issue = lambda *a, **k: False  # transient
        d.create_issue = lambda *a, **k: self.fail("must not create on transient failure")
        r = report([node("u1", 1)], issue_number=5, issue_url="u")
        self.assertIsNone(d.open_or_update_graph_issue(self._cfg(), r, title="t"))
        self.assertEqual(r.issue_number, 5)  # number preserved for retry


if __name__ == "__main__":
    unittest.main()
