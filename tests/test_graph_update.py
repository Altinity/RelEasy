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


class CollapseComponentsToGroups(unittest.TestCase):
    """Discovery now collapses each component into one ordered group node."""

    def _nodes(self, *specs):
        # specs: (uid, pr_num, is_user_group)
        return {
            uid: d.DAGNode(uid, grp, [URL(num)], [f"t{num}"],
                           f"2026-01-{num:02d}T00:00:00+00:00", [], "trial-clean")
            for uid, num, grp in specs
        }

    def test_component_merges_in_topo_order(self):
        nodes = self._nodes(("u10", 10, False), ("u20", 20, False),
                            ("u30", 30, False), ("solo", 99, False))
        # comp.unit_ids is topo order (prereq first)
        comp = d.DAGComponent("wcc-1", ["u10", "u20", "u30"], ["u10"],
                              [("u20", "u10"), ("u30", "u20")])
        folded, kept = d._collapse_components_to_groups(nodes, [comp], [])
        self.assertEqual(folded, {"u10", "u20", "u30"})
        self.assertEqual(kept, [])               # pure-auto: nothing kept
        self.assertIn("solo", nodes)
        gids = [k for k in nodes if k.startswith("auto-grp")]
        self.assertEqual(len(gids), 1)
        g = nodes[gids[0]]
        self.assertEqual(gids[0], "auto-grp-u10")          # lead unit id (unique)
        self.assertEqual(g.pr_urls, [URL(10), URL(20), URL(30)])  # prereq first
        self.assertEqual(g.discovery_method, "grouped")
        self.assertEqual(g.deps, [])
        self.assertFalse(g.is_user_group)

    def test_single_auto_unit_not_grouped(self):
        nodes = self._nodes(("a", 1, False))
        comp = d.DAGComponent("wcc-1", ["a"], [], [])
        folded, kept = d._collapse_components_to_groups(nodes, [comp], [])
        self.assertEqual(folded, set())
        self.assertEqual(kept, [])
        self.assertIn("a", nodes)

    def test_mixed_component_kept_not_merged(self):
        # auto `a` depends on user group `ug` → keep component (edges), don't
        # merge, and `a` keeps its deps so the overlay can emit depends_on.
        nodes = self._nodes(("ug", 5, True), ("a", 7, False))
        nodes["a"].deps = ["ug"]
        comp = d.DAGComponent("wcc-1", ["a", "ug"], [], [("a", "ug")])
        w = []
        folded, kept = d._collapse_components_to_groups(nodes, [comp], w)
        self.assertEqual(folded, set())          # nothing merged
        self.assertEqual(kept, [comp])           # component kept for overlay
        self.assertIn("ug", nodes)
        self.assertIn("a", nodes)
        self.assertEqual(nodes["a"].deps, ["ug"])  # dependency preserved
        self.assertFalse(any(k.startswith("auto-grp") for k in nodes))

    def test_user_group_to_auto_dep_warned(self):
        # user group depends on an auto unit → can't auto-apply → warn.
        nodes = self._nodes(("ug", 5, True), ("a", 7, False))
        nodes["ug"].deps = ["a"]
        comp = d.DAGComponent("wcc-1", ["a", "ug"], [], [("ug", "a")])
        w = []
        folded, kept = d._collapse_components_to_groups(nodes, [comp], w)
        self.assertEqual(kept, [comp])
        self.assertTrue(any("user group" in x and "depends on" in x for x in w))

    def test_no_gid_collision_cross_repo(self):
        # Two pure-auto components whose lead PRs share a number but differ by
        # repo must NOT collide (old auto-grp-<min-number> scheme would).
        a = d.DAGNode("o-r-pr-100", False, ["https://github.com/o/r/pull/100"],
                      ["t"], "2026-01-01T00:00:00+00:00", [], "trial-clean")
        a2 = d.DAGNode("o-r-pr-101", False, ["https://github.com/o/r/pull/101"],
                       ["t"], "2026-01-02T00:00:00+00:00", [], "trial-clean")
        b = d.DAGNode("u-s-pr-100", False, ["https://github.com/u/s/pull/100"],
                      ["t"], "2026-01-03T00:00:00+00:00", [], "trial-clean")
        b2 = d.DAGNode("u-s-pr-102", False, ["https://github.com/u/s/pull/102"],
                       ["t"], "2026-01-04T00:00:00+00:00", [], "trial-clean")
        nodes = {n.unit_id: n for n in (a, a2, b, b2)}
        comps = [
            d.DAGComponent("wcc-1", ["o-r-pr-100", "o-r-pr-101"], [], [("o-r-pr-101", "o-r-pr-100")]),
            d.DAGComponent("wcc-2", ["u-s-pr-100", "u-s-pr-102"], [], [("u-s-pr-102", "u-s-pr-100")]),
        ]
        folded, kept = d._collapse_components_to_groups(nodes, comps, [])
        gids = sorted(k for k in nodes if k.startswith("auto-grp"))
        self.assertEqual(gids, ["auto-grp-o-r-pr-100", "auto-grp-u-s-pr-100"])
        self.assertEqual(len(gids), 2)           # two distinct groups, no clobber


class IsReusableUnit(unittest.TestCase):
    """Incremental discovery reuses only standalone, cached, unchanged units."""

    def _cu(self, num):
        from releasy.pipeline import FeatureUnit
        from releasy.github_ops import PRInfo
        pr = PRInfo(number=num, title=f"t{num}", body="", state="merged",
                    merge_commit_sha=f"s{num}", head_sha="h", url=URL(num),
                    repo_slug="o/r", merged_at="2026-01-01T00:00:00+00:00")
        return d._CandidateUnit(f"pr-{num}", False, [pr], pr.merged_at,
                                FeatureUnit(feature_id=f"pr-{num}", prs=[pr], if_exists="skip"))

    def _node(self, urls, *, deps=None, cached=True, shas=None):
        return d.DAGNode("pr-x", False, urls, ["t"] * len(urls),
                         "2026-01-01T00:00:00+00:00", deps or [], "trial-clean",
                         cached=cached, merge_shas=shas if shas is not None else ["s1"])

    def test_reusable_clean_standalone(self):
        self.assertTrue(d._is_reusable_unit(self._node([URL(1)], shas=["s1"]), self._cu(1)))

    def test_not_reusable_multi_pr(self):
        self.assertFalse(d._is_reusable_unit(self._node([URL(1), URL(2)]), self._cu(1)))

    def test_not_reusable_with_deps(self):
        self.assertFalse(d._is_reusable_unit(self._node([URL(1)], deps=["pr-9"]), self._cu(1)))

    def test_not_reusable_uncached(self):
        self.assertFalse(d._is_reusable_unit(self._node([URL(1)], cached=False), self._cu(1)))

    def test_not_reusable_url_changed(self):
        self.assertFalse(d._is_reusable_unit(self._node([URL(2)]), self._cu(1)))

    def test_not_reusable_sha_changed(self):
        # same URL, but the PR was re-merged (new merge SHA) → must NOT reuse.
        self.assertFalse(d._is_reusable_unit(self._node([URL(1)], shas=["OLD"]), self._cu(1)))

    def test_not_reusable_missing_shas(self):
        # prior report has no merge_shas (older format) → can't verify → no reuse.
        self.assertFalse(d._is_reusable_unit(self._node([URL(1)], shas=[]), self._cu(1)))


class BuildGroupCacheBranches(unittest.TestCase):
    """Group combined branches are built + cached (clean) or skipped (conflict)."""

    def setUp(self):
        from releasy.pipeline import FeatureUnit
        from releasy.github_ops import PRInfo
        self._save = (d._trial_pick_unit, d._release_cache_branch, d._ensure_member_commits)
        self._released = []
        d._release_cache_branch = (
            lambda scratch, ref, br, keep: self._released.append((br, keep))
        )
        d._ensure_member_commits = lambda scratch, prs, origin_slug: []  # all present
        self._FeatureUnit, self._PRInfo = FeatureUnit, PRInfo

    def tearDown(self):
        d._trial_pick_unit, d._release_cache_branch, d._ensure_member_commits = self._save

    def _setup(self, *, clean):
        from releasy.github_ops import PRInfo
        from releasy.pipeline import FeatureUnit
        prs = [
            PRInfo(number=n, title=f"t{n}", body="", state="merged",
                   merge_commit_sha=f"sha{n}", head_sha="h",
                   url=URL(n), repo_slug="o/r", merged_at=f"2026-01-0{n}T00:00:00+00:00")
            for n in (1, 2)
        ]
        by_id = {
            f"pr-{p.number}": d._CandidateUnit(
                f"pr-{p.number}", False, [p], p.merged_at,
                FeatureUnit(feature_id=f"pr-{p.number}", prs=[p], if_exists="skip"))
            for p in prs
        }
        grp = d.DAGNode("auto-grp-pr-1", False, [URL(1), URL(2)], ["t1", "t2"],
                        "2026-01-01T00:00:00+00:00", [], "grouped")
        nodes = {"auto-grp-pr-1": grp, "solo": node("solo", 9)}
        outcome = type("O", (), {"clean": clean, "conflict_files": [] if clean else ["f.cpp"],
                                 "conflicting_pr_idx": None})()
        picked = {}
        d._trial_pick_unit = lambda scratch, unit, ref, **k: (
            picked.update(prs=[p.url for p in unit.prs], is_group=k.get("is_group")) or outcome
        )
        return nodes, by_id, grp, picked

    def test_clean_group_cached(self):
        nodes, by_id, grp, picked = self._setup(clean=True)
        d._build_group_cache_branches(Path("/x"), "b", "ref", nodes, by_id, "o/r", [])
        self.assertTrue(grp.cached)
        self.assertEqual(picked["prs"], [URL(1), URL(2)])   # apply order preserved
        self.assertTrue(picked["is_group"])
        self.assertIn(("feature/b/auto-grp-pr-1", True), self._released)  # kept

    def test_conflicting_group_not_cached(self):
        nodes, by_id, grp, picked = self._setup(clean=False)
        w = []
        d._build_group_cache_branches(Path("/x"), "b", "ref", nodes, by_id, "o/r", w)
        self.assertFalse(grp.cached)
        self.assertIn(("feature/b/auto-grp-pr-1", False), self._released)  # dropped
        self.assertTrue(any("conflicts" in x and "1 file" in x for x in w))


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


class UpstreamPrereqRecursion(unittest.TestCase):
    """Phase B pure pieces: cross-repo detection + upstream pull-in."""

    def setUp(self):
        from releasy.config import OriginConfig, UpstreamConfig
        from releasy.pipeline import FeatureUnit
        from releasy.github_ops import PRInfo
        self._FeatureUnit, self._PRInfo = FeatureUnit, PRInfo
        self.cfg = Config(
            name="n", origin=OriginConfig(remote="git@github.com:Altinity/ClickHouse.git"),
            project="p", upstream=UpstreamConfig(remote="git@github.com:ClickHouse/ClickHouse.git"),
        )
        self._save = (d.fetch_pr_by_url, d.ensure_remote, d.run_git)

    def tearDown(self):
        d.fetch_pr_by_url, d.ensure_remote, d.run_git = self._save

    def _pr(self, slug, num, sha="deadbeef"):
        return self._PRInfo(
            number=num, title=f"PR {num}", body="", state="merged",
            merge_commit_sha=sha, head_sha="h", url=f"https://github.com/{slug}/pull/{num}",
            repo_slug=slug, merged_at="2026-01-01T00:00:00+00:00",
        )

    def _cu(self, slug, num):
        pr = self._pr(slug, num)
        fu = self._FeatureUnit(feature_id=f"pr-{num}", prs=[pr], if_exists="skip")
        return d._CandidateUnit(f"pr-{num}", False, [pr], "2026-01-01T00:00:00+00:00", fu)

    def test_is_cross_repo(self):
        origin = "Altinity/ClickHouse"
        self.assertTrue(d._is_cross_repo(self._cu("ClickHouse/ClickHouse", 1), origin))
        self.assertFalse(d._is_cross_repo(self._cu("Altinity/ClickHouse", 2), origin))
        self.assertFalse(d._is_cross_repo(self._cu("ClickHouse/ClickHouse", 3), None))

    def test_pull_upstream_prereq_registers_unit(self):
        pr = self._pr("ClickHouse/ClickHouse", 5000, sha="abc123")
        d.fetch_pr_by_url = lambda config, url, **k: pr
        d.ensure_remote = lambda *a, **k: True
        d.run_git = lambda *a, **k: type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})()
        by_id, url2u, sha2u = {}, {}, {}
        url = "https://github.com/ClickHouse/ClickHouse/pull/5000"
        cu = d._pull_upstream_prereq(self.cfg, Path("/x"), url, by_id, url2u, sha2u, [])
        self.assertIsNotNone(cu)
        self.assertEqual(cu.unit_id, "ClickHouse-ClickHouse-pr-5000")
        self.assertEqual(url2u[url], cu.unit_id)
        self.assertEqual(sha2u["abc123"], cu.unit_id)
        self.assertIn(cu.unit_id, by_id)
        # idempotent: second call returns the same registered unit
        self.assertIs(d._pull_upstream_prereq(self.cfg, Path("/x"), url, by_id, url2u, sha2u, []), cu)

    def test_pull_upstream_prereq_commit_unfetchable(self):
        pr = self._pr("ClickHouse/ClickHouse", 6000, sha="nope")
        d.fetch_pr_by_url = lambda config, url, **k: pr
        d.ensure_remote = lambda *a, **k: True
        # cat-file always fails → commit never available → None
        d.run_git = lambda *a, **k: type("R", (), {"returncode": 1, "stdout": "", "stderr": ""})()
        w = []
        cu = d._pull_upstream_prereq(
            self.cfg, Path("/x"), "https://github.com/ClickHouse/ClickHouse/pull/6000",
            {}, {}, {}, w,
        )
        self.assertIsNone(cu)
        self.assertTrue(any("not fetchable" in x for x in w))

    def test_pull_upstream_prereq_no_upstream(self):
        from releasy.config import OriginConfig
        cfg = Config(name="n", origin=OriginConfig(remote="git@github.com:o/r.git"), project="p")
        self.assertIsNone(d._pull_upstream_prereq(
            cfg, Path("/x"), "https://github.com/u/s/pull/1", {}, {}, {}, []))

    def test_fallback_surfaces_external_urls(self):
        # _AIFallbackResult carries out-of-set prereq URLs for the caller.
        r = d._AIFallbackResult(deps=[], resolved=False, method="ai-resolve",
                                external_prereq_urls=["https://github.com/u/s/pull/9"])
        self.assertEqual(r.external_prereq_urls, ["https://github.com/u/s/pull/9"])


class IssueBodyAndRoundTrip(unittest.TestCase):
    def test_render_contains_key_parts(self):
        # A multi-PR group + a standalone, plus an exclusion.
        grp = d.DAGNode("auto-grp-1", False, [URL(1), URL(2)], ["t1", "t2"],
                        "2026-01-01T00:00:00+00:00", [], "grouped")
        r = report([grp, node("solo", 9)], excluded=[{"url": URL(7), "reason": "v"}])
        body = d.render_graph_issue_body(r)
        self.assertNotIn("mermaid", body)            # DAG is gone
        self.assertIn(d._issue_marker("b"), body)
        self.assertIn("Groups (port together", body)
        self.assertIn("1. [#1]", body)               # numbered apply order
        self.assertIn("2. [#2]", body)
        self.assertIn("Standalone PRs", body)
        self.assertIn("Excluded", body)
        self.assertIn("#7", body)

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

    def test_merge_shas_round_trip(self):
        tmp = Path(tempfile.mkdtemp())
        rp = tmp / "graph.b.yaml"
        n = d.DAGNode("pr-1", False, [URL(1)], ["t"], "2026-01-01T00:00:00+00:00",
                      [], "trial-clean", cached=True, merge_shas=["deadbeef"])
        d._write_report(report([n]), rp)
        back = d.load_report(rp)
        self.assertEqual(back.nodes[0].merge_shas, ["deadbeef"])


class OpenOrUpdateIssue(unittest.TestCase):
    """#12 — recreate when the tracked issue 404s; never recreate on transient."""

    def setUp(self):
        self._update, self._create, self._ensure = (
            d.update_issue, d.create_issue, d.ensure_label,
        )
        d.ensure_label = lambda *a, **k: True  # offline

    def tearDown(self):
        d.update_issue, d.create_issue, d.ensure_label = (
            self._update, self._create, self._ensure,
        )

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

    def test_labels_include_releasy_and_target_branch(self):
        ensured, used = [], {}
        d.ensure_label = lambda config, name, *a, **k: ensured.append(name) or True
        d.create_issue = lambda config, t, b, *, labels=None: used.update(labels=labels) or (1, "u")
        r = report([node("u1", 1)], base_branch="antalya-26.4")  # default issue_labels=["releasy"]
        d.open_or_update_graph_issue(self._cfg(), r, title="t")
        self.assertEqual(used["labels"], ["releasy", "antalya-26.4"])
        self.assertEqual(set(ensured), {"releasy", "antalya-26.4"})


if __name__ == "__main__":
    unittest.main()
