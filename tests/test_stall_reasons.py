"""Unit tests for stall reasons — why a port is parked, and when to retry.

Covers the state model (enum + specifics, serialisation, ageing), the
``_prereq_stall`` mapping from a prereq-dive exit reason, and the run gate
that skips a unit whose stall cannot clear on its own.

Stdlib unittest (no pytest dependency). Run:
    python3 -m unittest discover -s tests
"""
from __future__ import annotations

import unittest

import releasy.pipeline as p
from releasy.config import Config, OriginConfig
from releasy.state import (
    BLOCKING_STALL_KINDS,
    FeatureState,
    PipelineState,
    StallReason,
    _parse_features,
    make_stall,
)

URL = "https://github.com/o/r/pull/{}".format


def cfg(**kw) -> Config:
    return Config(
        name="n", origin=OriginConfig(remote="git@github.com:o/r.git"),
        project="p", **kw,
    )


class StallSummary(unittest.TestCase):
    """The generic kind plus its specifics, in one short line."""

    def test_units_win_over_the_prs_they_carry(self):
        # The unit ID is what the reader acts on; repeating its source PR
        # only makes the line longer.
        s = StallReason(
            kind="waiting_for_merge", waiting_on_units=["auto-grp-2"],
            waiting_on_prs=[URL(7)],
        )
        self.assertEqual(s.summary(), "waiting for `auto-grp-2` to merge")

    def test_prs_named_when_no_unit_ports_them(self):
        s = StallReason(kind="missing_prereq", waiting_on_prs=[URL(7), URL(8)])
        self.assertEqual(s.summary(), "missing prereq #7, #8")

    def test_waiting_without_targets_stays_readable(self):
        self.assertEqual(
            StallReason(kind="waiting_for_merge").summary(),
            "waiting for an external change to merge",
        )

    def test_detail_appended_after_the_label(self):
        s = StallReason(kind="retries_exhausted", detail="auto-continue 2/2")
        self.assertEqual(s.summary(), "retries exhausted: auto-continue 2/2")

    def test_long_detail_truncated(self):
        s = StallReason(kind="build_unfixed", detail="x" * 200)
        self.assertLessEqual(len(s.summary(max_detail=20)), len("build still failing: ") + 20)
        self.assertTrue(s.summary(max_detail=20).endswith("…"))

    def test_detail_whitespace_collapsed(self):
        s = StallReason(kind="build_unfixed", detail="ninja:\n  build stopped")
        self.assertEqual(s.summary(), "build still failing: ninja: build stopped")

    def test_unknown_kind_falls_back_to_the_raw_value(self):
        self.assertEqual(StallReason(kind="something_new").summary(), "something_new")


class StallSerialisation(unittest.TestCase):
    """A stall survives the state-file round trip; junk parses to None."""

    def test_round_trip(self):
        s = StallReason(
            kind="waiting_for_merge", detail="d", waiting_on_units=["u"],
            waiting_on_prs=[URL(1)], since="2026-08-07T00:00:00+00:00", runs=4,
        )
        back = StallReason.from_dict(s.to_dict())
        self.assertEqual(back, s)

    def test_defaults_omitted_from_the_dump(self):
        self.assertEqual(StallReason(kind="unresolvable").to_dict(),
                         {"kind": "unresolvable"})

    def test_parsed_off_a_feature_entry(self):
        feats = _parse_features({
            "f1": {"status": "conflict",
                   "stall": {"kind": "missing_prereq",
                             "waiting_on_prs": [URL(9)]}},
        })
        self.assertEqual(feats["f1"].stall.kind, "missing_prereq")
        self.assertEqual(feats["f1"].stall.waiting_on_prs, [URL(9)])

    def test_absent_stall_is_none(self):
        self.assertIsNone(_parse_features({"f1": {"status": "conflict"}})["f1"].stall)

    def test_unusable_stall_is_none(self):
        for raw in ({}, {"detail": "no kind"}, "conflict", [1], None):
            with self.subTest(raw=raw):
                self.assertIsNone(StallReason.from_dict(raw))


class MakeStallAgeing(unittest.TestCase):
    """Re-recording the same stall ages it; a different one starts over."""

    def test_first_record_starts_at_one_run(self):
        s = make_stall("unresolvable", prior=None)
        self.assertEqual(s.runs, 1)
        self.assertTrue(s.since)

    def test_same_wait_keeps_since_and_bumps_runs(self):
        old = StallReason(kind="waiting_for_merge", waiting_on_units=["u"],
                          since="2026-01-01T00:00:00+00:00", runs=2)
        s = make_stall("waiting_for_merge", waiting_on_units=["u"],
                       prior=FeatureState(stall=old))
        self.assertEqual(s.since, "2026-01-01T00:00:00+00:00")
        self.assertEqual(s.runs, 3)

    def test_detail_change_alone_still_counts_as_the_same_wait(self):
        old = StallReason(kind="build_unfixed", detail="err A",
                          since="2026-01-01T00:00:00+00:00", runs=1)
        s = make_stall("build_unfixed", detail="err B",
                       prior=FeatureState(stall=old))
        self.assertEqual(s.runs, 2)
        self.assertEqual(s.detail, "err B")

    def test_different_target_resets(self):
        old = StallReason(kind="waiting_for_merge", waiting_on_units=["u1"],
                          since="2026-01-01T00:00:00+00:00", runs=5)
        s = make_stall("waiting_for_merge", waiting_on_units=["u2"],
                       prior=FeatureState(stall=old))
        self.assertEqual(s.runs, 1)
        self.assertNotEqual(s.since, "2026-01-01T00:00:00+00:00")

    def test_different_kind_resets(self):
        old = StallReason(kind="unresolvable", runs=5)
        self.assertEqual(make_stall("build_unfixed",
                                    prior=FeatureState(stall=old)).runs, 1)


class StallStillBlocks(unittest.TestCase):
    """Which stalls hold a unit back, and what releases them."""

    def _state(self, **features) -> PipelineState:
        return PipelineState(features=features)

    def test_only_blocking_kinds_are_gated(self):
        self.assertEqual(BLOCKING_STALL_KINDS,
                         frozenset({"waiting_for_merge", "missing_prereq"}))
        for kind in ("unresolvable", "retries_exhausted", "build_unfixed",
                     "resolver_unavailable", "prereq_search_exhausted"):
            with self.subTest(kind=kind):
                self.assertFalse(p._stall_still_blocks(
                    cfg(), self._state(), StallReason(kind=kind)))

    def test_waiting_on_an_open_unit_blocks(self):
        state = self._state(dep=FeatureState(status="needs_review"))
        self.assertTrue(p._stall_still_blocks(
            cfg(), state,
            StallReason(kind="waiting_for_merge", waiting_on_units=["dep"])))

    def test_merged_dependency_releases(self):
        for status in ("merged", "superseded"):
            with self.subTest(status=status):
                state = self._state(dep=FeatureState(status=status))
                self.assertFalse(p._stall_still_blocks(
                    cfg(), state,
                    StallReason(kind="waiting_for_merge",
                                waiting_on_units=["dep"])))

    def test_any_merged_dependency_releases(self):
        state = self._state(a=FeatureState(status="merged"),
                            b=FeatureState(status="conflict"))
        self.assertFalse(p._stall_still_blocks(
            cfg(), state,
            StallReason(kind="waiting_for_merge", waiting_on_units=["a", "b"])))

    def test_dependency_gone_from_the_session_releases(self):
        # It may have been dropped or renamed — we can no longer tell, so
        # a retry beats waiting forever.
        self.assertFalse(p._stall_still_blocks(
            cfg(), self._state(),
            StallReason(kind="waiting_for_merge", waiting_on_units=["ghost"])))

    def test_waiting_with_no_target_releases(self):
        self.assertFalse(p._stall_still_blocks(
            cfg(), self._state(), StallReason(kind="waiting_for_merge")))

    def test_missing_prereq_blocks_while_nobody_ports_it(self):
        self.assertTrue(p._stall_still_blocks(
            cfg(), self._state(),
            StallReason(kind="missing_prereq", waiting_on_prs=[URL(9)])))

    def test_missing_prereq_releases_once_queued(self):
        state = self._state(other=FeatureState(status="needs_review",
                                               pr_url=URL(9)))
        self.assertFalse(p._stall_still_blocks(
            cfg(), state,
            StallReason(kind="missing_prereq", waiting_on_prs=[URL(9)])))

    def test_missing_prereq_releases_once_carried_by_a_combined_port(self):
        """The prereq is listed nowhere, but a unit's body says it brings it."""
        state = self._state(other=FeatureState(
            status="needs_review", pr_url=URL(1718),
            contained_pr_urls=[URL(9)],
        ))
        self.assertFalse(p._stall_still_blocks(
            cfg(), state,
            StallReason(kind="missing_prereq", waiting_on_prs=[URL(9)])))


class QueuedStall(unittest.TestCase):
    """Building the ``waiting_for_merge`` stall from queued-prereq hits."""

    def test_repeated_unit_named_once(self):
        # One combined port routinely carries several discovered prereqs.
        queued = [
            {"prereq_url": URL(1388), "queued_in": "grp", "carried": True},
            {"prereq_url": URL(1618), "queued_in": "grp", "carried": True},
        ]
        stall = p._queued_stall(queued, prior=None)
        self.assertEqual(stall.waiting_on_units, ["grp"])
        self.assertEqual(stall.waiting_on_prs, [URL(1388), URL(1618)])
        self.assertEqual(stall.summary(), "waiting for `grp` to merge")

    def test_distinct_units_kept_in_order(self):
        queued = [
            {"prereq_url": URL(1), "queued_in": "b"},
            {"prereq_url": URL(2), "queued_in": "a"},
        ]
        self.assertEqual(
            p._queued_stall(queued, prior=None).waiting_on_units, ["b", "a"],
        )


class SkipForStall(unittest.TestCase):
    """The run gate: skip, or drop a stall that has cleared."""

    def _blocked(self) -> FeatureState:
        return FeatureState(
            status="conflict",
            stall=StallReason(kind="waiting_for_merge",
                              waiting_on_units=["dep"]),
        )

    def _state(self, fs: FeatureState) -> PipelineState:
        return PipelineState(features={
            "u": fs, "dep": FeatureState(status="needs_review"),
        })

    def _call(self, config, state, fs) -> bool:
        config.dry_run = True  # no state file writes from the test
        return p._skip_for_stall(config, state, fs, "branch", "label")

    def test_skips_and_ages_the_stall(self):
        fs = self._blocked()
        self.assertTrue(self._call(cfg(), self._state(fs), fs))
        self.assertEqual(fs.stall.runs, 2)

    def test_no_stall_no_skip(self):
        fs = FeatureState(status="conflict")
        self.assertFalse(self._call(cfg(), self._state(fs), fs))

    def test_no_prior_state_no_skip(self):
        self.assertFalse(self._call(cfg(), PipelineState(), None))

    def test_cleared_stall_is_dropped_and_the_unit_runs(self):
        fs = self._blocked()
        state = PipelineState(features={
            "u": fs, "dep": FeatureState(status="merged"),
        })
        self.assertFalse(self._call(cfg(), state, fs))
        self.assertIsNone(fs.stall)

    def test_non_blocking_kind_never_skips(self):
        fs = FeatureState(status="conflict",
                          stall=StallReason(kind="unresolvable"))
        self.assertFalse(self._call(cfg(), self._state(fs), fs))
        self.assertIsNotNone(fs.stall)  # kept for display

    def test_ignore_stalls_flag_overrides(self):
        fs = self._blocked()
        config = cfg()
        config.ignore_stalls = True
        self.assertFalse(self._call(config, self._state(fs), fs))
        self.assertIsNotNone(fs.stall)  # forced, not cleared

    def test_config_opt_out_overrides(self):
        fs = self._blocked()
        config = cfg()
        config.pr_policy.honor_stall_reasons = False
        self.assertFalse(self._call(config, self._state(fs), fs))


class PrereqStallMapping(unittest.TestCase):
    """``_decide_prereq_dive`` exit reasons → stall kinds."""

    class _Auto:
        max_prereq_depth = 2

    def _map(self, exit_reason, discovered=(URL(9),), state=None, depth=1):
        unit = type("U", (), {"feature_id": "u"})()
        return p._prereq_stall(
            cfg(), state or PipelineState(), unit, exit_reason,
            list(discovered), self._Auto(), depth, prior=None,
        )

    def test_queued_elsewhere_waits_on_that_unit(self):
        s = self._map({
            "reason": "queued_elsewhere",
            "queued": [{"prereq_url": URL(9), "queued_in": "auto-grp-2"}],
        })
        self.assertEqual(s.kind, "waiting_for_merge")
        self.assertEqual(s.waiting_on_units, ["auto-grp-2"])
        self.assertEqual(s.waiting_on_prs, [URL(9)])

    def test_detection_only_is_a_missing_prereq(self):
        s = self._map({"reason": "detection_only"})
        self.assertEqual(s.kind, "missing_prereq")
        self.assertEqual(s.waiting_on_prs, [URL(9)])

    def test_detection_only_upgrades_to_waiting_when_queued(self):
        # The dive never ran, so the queued check never ran either.
        state = PipelineState(features={
            "other": FeatureState(status="needs_review", pr_url=URL(9)),
        })
        s = self._map({"reason": "detection_only"}, state=state)
        self.assertEqual(s.kind, "waiting_for_merge")
        self.assertEqual(s.waiting_on_units, ["other"])

    def test_depth_exhausted_names_the_depth(self):
        s = self._map({"reason": "depth_exhausted"}, depth=2)
        self.assertEqual(s.kind, "prereq_search_exhausted")
        self.assertEqual(s.detail, "depth 2/2")

    def test_cycle_and_fetch_failure_exhaust_the_search(self):
        for reason in ("cycle", "fetch_failed", "all_already_in_base"):
            with self.subTest(reason=reason):
                self.assertEqual(
                    self._map({"reason": reason}).kind,
                    "prereq_search_exhausted",
                )

    def test_fetch_failure_waits_on_the_urls_it_could_not_fetch(self):
        s = self._map({"reason": "fetch_failed", "failed_urls": [URL(11)]})
        self.assertEqual(s.waiting_on_prs, [URL(11)])

    def test_unlabeled_origin_prereq_is_a_missing_prereq(self):
        s = self._map({"reason": "unlabeled_origin_prereq",
                       "unlabeled_urls": [URL(12)]})
        self.assertEqual(s.kind, "missing_prereq")
        self.assertEqual(s.waiting_on_prs, [URL(12)])


class StallConfig(unittest.TestCase):
    """``pr_policy.honor_stall_reasons`` defaults on."""

    def test_default_on(self):
        self.assertTrue(cfg().pr_policy.honor_stall_reasons)

    def test_ignore_stalls_defaults_off(self):
        self.assertFalse(cfg().ignore_stalls)


if __name__ == "__main__":
    unittest.main()
