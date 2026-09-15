"""`pr_sources.on_hold`: PRs parked while they wait on something.

A hold is not a veto — the unit stays in the graph and keeps its edges,
`releasy run` just walks past it. Covers the three promises: the session
file round-trips holds, the run gate skips held units, and the graph issue
lists them under their own section instead of the working lists.
"""

from __future__ import annotations

import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path

import yaml

import releasy.dag_discovery as d
import releasy.pipeline as pl
import releasy.pr_membership as pm
from releasy.config import (
    Config,
    OriginConfig,
    PRGroupConfig,
    PRSourcesConfig,
    SessionConfig,
    load_session,
    save_session,
)
from releasy.github_ops import PRInfo
from releasy.state import FeatureState

from test_graph_update import URL, node, report

REF = lambda n: ("o", "r", n)  # noqa: E731 — canonical ref for URL(n)


def pr(num):
    return PRInfo(
        number=num, title=f"t{num}", body="", state="merged",
        merge_commit_sha=f"s{num}", head_sha="h", url=URL(num),
        repo_slug="o/r", merged_at=f"2026-01-{num:02d}T00:00:00+00:00",
    )


def unit(uid, *nums, group=False):
    return pl.FeatureUnit(
        feature_id=uid, prs=[pr(n) for n in nums], if_exists="skip",
        is_group=group, group_id=uid if group else None,
    )


def cfg_with(tmp, **ps_kwargs):
    cfg = Config(
        name="n", origin=OriginConfig(remote="git@github.com:o/r.git"),
        project="p", config_path=tmp / "config.yaml",
    )
    cfg.session = SessionConfig(
        pr_sources=PRSourcesConfig(**ps_kwargs),
        session_path=tmp / "b.session.yaml",
    )
    return cfg


class SessionRoundTrip(unittest.TestCase):
    """The hand-editable half: what you write in the session file is what
    comes back, in both the bare-URL and the {url, reason} form."""

    def _roundtrip(self, on_hold_yaml):
        tmp = Path(tempfile.mkdtemp())
        session_path = tmp / "b.session.yaml"
        session_path.write_text(yaml.dump({
            "features": [],
            "pr_sources": {"include_prs": [URL(1)], "on_hold": on_hold_yaml},
        }))
        cfg = Config(
            name="n", origin=OriginConfig(remote="git@github.com:o/r.git"),
            project="p", config_path=tmp / "config.yaml",
        )
        return load_session(cfg, session_path)

    def test_bare_url_entry(self):
        ps = self._roundtrip([URL(1)]).pr_sources
        self.assertEqual(ps.on_hold, [URL(1)])
        self.assertEqual(ps.on_hold_reasons, {})

    def test_dict_entry_keeps_reason(self):
        ps = self._roundtrip(
            [{"url": URL(1), "reason": "waiting for the follow-up"}],
        ).pr_sources
        self.assertEqual(ps.on_hold, [URL(1)])
        self.assertEqual(ps.on_hold_reasons[URL(1)], "waiting for the follow-up")

    def test_forms_mix(self):
        ps = self._roundtrip(
            [URL(1), {"url": URL(2), "reason": "why"}],
        ).pr_sources
        self.assertEqual(ps.on_hold, [URL(1), URL(2)])
        self.assertEqual(ps.on_hold_reasons, {URL(2): "why"})

    def test_unknown_key_rejected(self):
        with self.assertRaises(ValueError):
            self._roundtrip([{"url": URL(1), "ai_context": "nope"}])

    def test_save_preserves_both_forms(self):
        session = self._roundtrip([URL(1), {"url": URL(2), "reason": "why"}])
        save_session(session)
        disk = yaml.safe_load(session.session_path.read_text())
        self.assertEqual(
            disk["pr_sources"]["on_hold"],
            [URL(1), {"url": URL(2), "reason": "why"}],
        )

    def test_absent_key_means_no_holds(self):
        tmp = Path(tempfile.mkdtemp())
        session_path = tmp / "b.session.yaml"
        session_path.write_text(yaml.dump(
            {"features": [], "pr_sources": {"include_prs": [URL(1)]}},
        ))
        cfg = Config(
            name="n", origin=OriginConfig(remote="git@github.com:o/r.git"),
            project="p", config_path=tmp / "config.yaml",
        )
        self.assertEqual(load_session(cfg, session_path).pr_sources.on_hold, [])

    def test_hold_and_veto_on_one_pr_warns(self):
        session = self._roundtrip([URL(1)])
        self.assertEqual(session.load_warnings, [])
        tmp = Path(tempfile.mkdtemp())
        session_path = tmp / "b.session.yaml"
        session_path.write_text(yaml.dump({
            "features": [],
            "pr_sources": {"on_hold": [URL(1)], "exclude_prs": [URL(1)]},
        }))
        cfg = Config(
            name="n", origin=OriginConfig(remote="git@github.com:o/r.git"),
            project="p", config_path=tmp / "config.yaml",
        )
        warnings = load_session(cfg, session_path).load_warnings
        self.assertTrue(
            any("on_hold" in w and "exclude_prs" in w for w in warnings),
            warnings,
        )


class HoldMapping(unittest.TestCase):
    def test_canonical_match_ignores_url_spelling(self):
        tmp = Path(tempfile.mkdtemp())
        cfg = cfg_with(tmp, on_hold=["https://github.com/o/r.git/pull/1"])
        self.assertEqual(pl.hold_map(cfg), {REF(1): ""})

    def test_unparseable_entry_dropped(self):
        tmp = Path(tempfile.mkdtemp())
        cfg = cfg_with(tmp, on_hold=["not-a-url", URL(1)])
        self.assertEqual(pl.hold_map(cfg), {REF(1): ""})

    def test_any_held_member_holds_the_whole_group(self):
        # A group cherry-picks as one atomic unit — porting the rest
        # without the held PR would ship a broken subset.
        holds = {REF(2): "waiting"}
        self.assertEqual(
            pl._unit_hold_reason(unit("G", 1, 2, 3, group=True), holds),
            "waiting",
        )

    def test_several_holds_join_their_reasons(self):
        holds = {REF(1): "a", REF(2): "b"}
        self.assertEqual(
            pl._unit_hold_reason(unit("G", 1, 2, group=True), holds), "a; b",
        )

    def test_unheld_unit_is_none_not_empty(self):
        # "" is a real value — a hold with no recorded reason — so the
        # not-held sentinel has to be None.
        self.assertIsNone(pl._unit_hold_reason(unit("pr-1", 1), {REF(9): "x"}))
        self.assertEqual(pl._unit_hold_reason(unit("pr-1", 1), {REF(1): ""}), "")

    def test_marking_does_not_drop_units(self):
        # graph discover runs off the same list; a held unit must keep its
        # node (and its edges) in the graph.
        tmp = Path(tempfile.mkdtemp())
        cfg = cfg_with(tmp, on_hold=[URL(1)])
        units = [unit("pr-1", 1), unit("pr-2", 2)]
        pl._mark_held_units(cfg, units, "o/r")
        self.assertEqual([u.feature_id for u in units], ["pr-1", "pr-2"])
        self.assertEqual(units[0].hold_reason, "")
        self.assertIsNone(units[1].hold_reason)


class MembershipEdits(unittest.TestCase):
    def test_hold_then_release(self):
        tmp = Path(tempfile.mkdtemp())
        cfg = cfg_with(tmp, include_prs=[URL(1)])
        self.assertTrue(pm.hold_pr(cfg, URL(1), "waiting"))
        self.assertEqual(cfg.pr_sources.on_hold, [URL(1)])
        self.assertEqual(cfg.pr_sources.on_hold_reasons[URL(1)], "waiting")
        self.assertTrue(pm.unhold_pr(cfg, URL(1)))
        self.assertEqual(cfg.pr_sources.on_hold, [])
        self.assertEqual(cfg.pr_sources.on_hold_reasons, {})

    def test_hold_stays_out_of_include_and_exclude(self):
        tmp = Path(tempfile.mkdtemp())
        cfg = cfg_with(tmp, include_prs=[URL(1)])
        pm.hold_pr(cfg, URL(1), "waiting")
        self.assertEqual(cfg.pr_sources.include_prs, [URL(1)])
        self.assertEqual(cfg.pr_sources.exclude_prs, [])

    def test_re_hold_updates_the_reason(self):
        tmp = Path(tempfile.mkdtemp())
        cfg = cfg_with(tmp)
        pm.hold_pr(cfg, URL(1), "first")
        pm.hold_pr(cfg, URL(1), "second")
        self.assertEqual(cfg.pr_sources.on_hold, [URL(1)])
        self.assertEqual(cfg.pr_sources.on_hold_reasons[URL(1)], "second")

    def test_hold_refused_for_a_vetoed_pr(self):
        tmp = Path(tempfile.mkdtemp())
        cfg = cfg_with(tmp, exclude_prs=[URL(1)])
        self.assertFalse(pm.hold_pr(cfg, URL(1), "waiting"))
        self.assertEqual(cfg.pr_sources.on_hold, [])

    def test_release_of_an_unheld_pr_is_a_no_op(self):
        tmp = Path(tempfile.mkdtemp())
        cfg = cfg_with(tmp)
        self.assertTrue(pm.unhold_pr(cfg, URL(1)))

    def test_malformed_url_refused(self):
        tmp = Path(tempfile.mkdtemp())
        cfg = cfg_with(tmp)
        self.assertFalse(pm.hold_pr(cfg, "nope", ""))
        self.assertFalse(pm.unhold_pr(cfg, "nope"))

    def test_veto_clears_the_hold(self):
        tmp = Path(tempfile.mkdtemp())
        cfg = cfg_with(tmp, include_prs=[URL(1)], on_hold=[URL(1)])
        cfg.pr_sources.on_hold_reasons[URL(1)] = "waiting"
        saved = pm.load_state
        pm.load_state = lambda c: __import__(
            "releasy.state", fromlist=["PipelineState"],
        ).PipelineState()
        pm.save_state = lambda s, c: None
        try:
            self.assertTrue(pm.remove_pr(cfg, URL(1)))
        finally:
            pm.load_state = saved
        self.assertEqual(cfg.pr_sources.on_hold, [])
        self.assertEqual(cfg.pr_sources.on_hold_reasons, {})
        self.assertIn(URL(1), cfg.pr_sources.exclude_prs)


def body(nodes, progress=None, held=None):
    return d.render_graph_issue_body(report(nodes), progress or {}, held)


def hold_entry(out):
    """The first bullet under the On-hold heading."""
    section = out.split("### ⏸ On hold")[1]
    return next(l for l in section.splitlines() if l.startswith("- "))


class IssueRendering(unittest.TestCase):
    def test_own_section_with_the_reason(self):
        out = body([node("pr-1", 1)], held={REF(1): "waiting for follow-up"})
        self.assertIn("### ⏸ On hold — not being ported right now", out)
        self.assertIn("⏸ on hold: waiting for follow-up", out)

    def test_left_out_of_working_lists(self):
        out = body([node("pr-1", 1), node("pr-2", 2)], held={REF(1): "w"})
        standalone = out.split("### Standalone PRs")[1].split("###")[0]
        self.assertIn(URL(2), standalone)
        self.assertNotIn(URL(1), standalone)

    def test_group_leaves_the_groups_section(self):
        out = body([node("G", 1, 2, group=True)], held={REF(1): "w"})
        self.assertNotIn("### Groups (port together, in apply order)", out)
        self.assertIn("**`G`** · 2 PRs", out)
        self.assertIn(URL(2), out)

    def test_not_folded(self):
        # A hold is live work somebody expects back; Discarded / Excluded
        # are folded shut, this one is not.
        out = body([node("pr-1", 1)], held={REF(1): "w"})
        self.assertNotIn("<summary>⏸", out)

    def test_hold_with_no_reason_still_reads(self):
        entry = hold_entry(body([node("pr-1", 1)], held={REF(1): ""}))
        self.assertTrue(entry.endswith("— ⏸ on hold"), entry)

    def test_existing_port_pr_is_still_linked(self):
        fs = FeatureState(status="needs_review", rebase_pr_url=URL(9))
        out = body([node("pr-1", 1)], {"pr-1": fs}, {REF(1): "w"})
        self.assertIn("⏸ on hold: w · 🟡 in review", out)
        self.assertIn(URL(9), out)

    def test_not_started_is_not_repeated_on_the_entry(self):
        self.assertNotIn(
            "⬜ not started",
            hold_entry(body([node("pr-1", 1)], held={REF(1): "w"})),
        )

    def test_held_unit_counted_once_in_the_tally(self):
        # Not in both the not-started bucket and the on-hold one — the
        # breakdown has to add up to the unit count.
        out = body([node("pr-1", 1), node("pr-2", 2)], held={REF(1): "w"})
        summary = [l for l in out.splitlines() if l.startswith("**Progress:")][0]
        self.assertIn("⬜ not started: 1", summary)
        self.assertIn("⏸ on hold: 1", summary)

    def test_merged_unit_ignores_the_hold(self):
        # The port landed — the hold no longer decides anything about it.
        fs = FeatureState(status="merged", rebase_pr_url=URL(9))
        out = body([node("pr-1", 1)], {"pr-1": fs}, {REF(1): "w"})
        self.assertNotIn("### ⏸ On hold", out)

    def test_counted_in_the_progress_line_and_headline(self):
        out = body([node("pr-1", 1), node("pr-2", 2)], held={REF(1): "w"})
        self.assertIn("⏸ on hold: 1", out)
        self.assertIn("1 unit(s) are **on hold**", out)

    def test_no_holds_no_section(self):
        self.assertNotIn("### ⏸ On hold", body([node("pr-1", 1)]))


class SpecHolds(unittest.TestCase):
    """`graph update`: what a member comment can do to the hold list."""

    def test_missing_key_preserves_current_holds(self):
        # The model didn't mention holds — that must not release them.
        self.assertIsNone(d._parse_spec_holds({"units": []}, []))

    def test_empty_list_releases_everything(self):
        self.assertEqual(d._parse_spec_holds({"on_hold": []}, []), {})

    def test_entries_parse_in_both_forms(self):
        spec = {"on_hold": [URL(1), {"url": URL(2), "reason": "why"}]}
        self.assertEqual(
            d._parse_spec_holds(spec, []), {URL(1): "", URL(2): "why"},
        )

    def test_bad_url_skipped_with_a_warning(self):
        warnings = []
        spec = {"on_hold": ["nope", {"url": URL(1), "reason": "w"}]}
        self.assertEqual(d._parse_spec_holds(spec, warnings), {URL(1): "w"})
        self.assertEqual(len(warnings), 1)

    def test_non_list_preserves_current_holds(self):
        warnings = []
        self.assertIsNone(d._parse_spec_holds({"on_hold": "all of them"}, warnings))
        self.assertEqual(len(warnings), 1)

    def test_apply_holds_and_releases_against_the_session(self):
        tmp = Path(tempfile.mkdtemp())
        cfg = cfg_with(tmp, on_hold=[URL(1)])
        held, released, failures = d._apply_spec_holds(
            cfg, {URL(2): "new reason"},
        )
        self.assertEqual(held, [URL(2)])
        self.assertEqual(released, [URL(1)])
        self.assertEqual(failures, [])
        self.assertEqual(cfg.pr_sources.on_hold, [URL(2)])

    def test_unchanged_hold_is_not_reported_as_new(self):
        tmp = Path(tempfile.mkdtemp())
        cfg = cfg_with(tmp, on_hold=[URL(1)])
        cfg.pr_sources.on_hold_reasons[URL(1)] = "same"
        held, released, failures = d._apply_spec_holds(cfg, {URL(1): "same"})
        self.assertEqual((held, released, failures), ([], [], []))

    def test_reason_change_keeps_the_hold(self):
        tmp = Path(tempfile.mkdtemp())
        cfg = cfg_with(tmp, on_hold=[URL(1)])
        cfg.pr_sources.on_hold_reasons[URL(1)] = "old"
        held, released, _ = d._apply_spec_holds(cfg, {URL(1): "new"})
        self.assertEqual((held, released), ([], []))
        self.assertEqual(cfg.pr_sources.on_hold_reasons[URL(1)], "new")

    def test_vetoed_pr_reports_a_failure(self):
        tmp = Path(tempfile.mkdtemp())
        cfg = cfg_with(tmp, exclude_prs=[URL(1)])
        held, released, failures = d._apply_spec_holds(cfg, {URL(1): "w"})
        self.assertEqual(held, [])
        self.assertEqual(failures, ["hold #1"])

    def test_current_holds_reach_the_prompt(self):
        block = d._render_current_graph_block(
            report([node("pr-1", 1)]), {REF(1): "waiting"},
        )
        self.assertIn("Currently on hold:", block)
        self.assertIn(f"{URL(1)} — waiting", block)

    def test_prompt_block_omits_the_heading_with_no_holds(self):
        block = d._render_current_graph_block(report([node("pr-1", 1)]), {})
        self.assertNotIn("Currently on hold:", block)


class DependentsBlock(unittest.TestCase):
    """A held unit never reaches ``merged``, so the existing dep gate is
    what makes its dependents report as blocked — no extra bookkeeping."""

    def test_dependent_of_a_held_unit_is_unmet(self):
        from releasy.state import PipelineState

        dependent = unit("pr-3", 3)
        dependent.depends_on = ["pr-1"]
        # The held unit is skipped before any state is written, so the gate
        # sees no entry for it at all.
        self.assertEqual(pl._unmet_deps(dependent, PipelineState()), ["pr-1"])

    def test_dependent_of_a_merged_unit_is_not(self):
        from releasy.state import PipelineState

        dependent = unit("pr-3", 3)
        dependent.depends_on = ["pr-1"]
        state = PipelineState()
        state.features["pr-1"] = FeatureState(status="merged")
        self.assertEqual(pl._unmet_deps(dependent, state), [])


class CLIWiring(unittest.TestCase):
    """`releasy hold` / `unhold`: the arguments reach pr_membership and a
    refusal there becomes a non-zero exit."""

    def setUp(self):
        from click.testing import CliRunner
        import releasy.cli as cli_mod

        self.runner = CliRunner()
        self.cli = cli_mod.cli
        self.calls = []
        tmp = Path(tempfile.mkdtemp())
        self.cfg = cfg_with(tmp)

        @contextmanager
        def fake_locked_config(ctx, *, session="optional"):
            self.calls.append(("locked_config", session))
            yield self.cfg

        self._saved = cli_mod._locked_config
        cli_mod._locked_config = fake_locked_config
        self.cli_mod = cli_mod

    def tearDown(self):
        self.cli_mod._locked_config = self._saved

    def _stub(self, name, result):
        saved = getattr(pm, name)
        setattr(pm, name, lambda *a, **kw: (
            self.calls.append((name, a, kw)) or result
        ))
        self.addCleanup(setattr, pm, name, saved)

    def test_hold_passes_url_and_reason(self):
        self._stub("hold_pr", True)
        res = self.runner.invoke(self.cli, ["hold", URL(1), "--reason", "why"])
        self.assertEqual(res.exit_code, 0, res.output)
        self.assertIn(("hold_pr", (self.cfg, URL(1), "why"), {}), self.calls)

    def test_hold_without_reason_passes_empty_string(self):
        self._stub("hold_pr", True)
        res = self.runner.invoke(self.cli, ["hold", URL(1)])
        self.assertEqual(res.exit_code, 0, res.output)
        self.assertIn(("hold_pr", (self.cfg, URL(1), ""), {}), self.calls)

    def test_hold_needs_the_session(self):
        # The session file is what `on_hold` lives in — loading it is not
        # optional for this command.
        self._stub("hold_pr", True)
        self.runner.invoke(self.cli, ["hold", URL(1)])
        self.assertIn(("locked_config", "required"), self.calls)

    def test_hold_refusal_exits_nonzero(self):
        self._stub("hold_pr", False)
        res = self.runner.invoke(self.cli, ["hold", URL(1)])
        self.assertEqual(res.exit_code, 1)

    def test_unhold_passes_the_url(self):
        self._stub("unhold_pr", True)
        res = self.runner.invoke(self.cli, ["unhold", URL(1)])
        self.assertEqual(res.exit_code, 0, res.output)
        self.assertIn(("unhold_pr", (self.cfg, URL(1)), {}), self.calls)

    def test_unhold_refusal_exits_nonzero(self):
        self._stub("unhold_pr", False)
        res = self.runner.invoke(self.cli, ["unhold", URL(1)])
        self.assertEqual(res.exit_code, 1)

    def test_url_argument_is_required(self):
        for cmd in ("hold", "unhold"):
            self.assertEqual(self.runner.invoke(self.cli, [cmd]).exit_code, 2)


class GroupHoldEndToEnd(unittest.TestCase):
    def test_group_member_hold_marks_the_group_unit(self):
        tmp = Path(tempfile.mkdtemp())
        cfg = cfg_with(
            tmp,
            groups=[PRGroupConfig(id="G", prs=[URL(1), URL(2)])],
            on_hold=[URL(2)],
        )
        cfg.pr_sources.on_hold_reasons[URL(2)] = "waiting on #3"
        units = [unit("G", 1, 2, group=True)]
        pl._mark_held_units(cfg, units, "o/r")
        self.assertEqual(units[0].hold_reason, "waiting on #3")


if __name__ == "__main__":
    unittest.main()
