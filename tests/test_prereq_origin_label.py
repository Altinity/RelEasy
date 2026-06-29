"""Tests for the in-origin auto-prereq label gate.

Covers ``require_origin_prereq_label``: a discovered prerequisite that
lives in the origin repo (and whose triggering PR is also in origin)
must carry the configured selection labels, otherwise the dive aborts
and the user must label it or list it explicitly. Cross-repo prereqs
(forward-port / backport sources) are never gated.
"""

from __future__ import annotations

import unittest
from types import SimpleNamespace

from releasy.github_ops import PRInfo
from releasy.pipeline import (
    _matches_config_labels,
    _reject_unlabeled_origin_prereqs,
)

ORIGIN = "Altinity/ClickHouse"
FORK = "ClickHouse/ClickHouse"


def _cfg(by_labels, *, exclude_labels=None, require=True):
    """Minimal config double for the two helpers under test."""
    return SimpleNamespace(
        origin=SimpleNamespace(remote=f"https://github.com/{ORIGIN}.git"),
        pr_sources=SimpleNamespace(
            by_labels=[SimpleNamespace(labels=list(lbls)) for lbls in by_labels],
            exclude_labels=list(exclude_labels or []),
        ),
        ai_resolve=SimpleNamespace(
            auto_add_prerequisite_prs=SimpleNamespace(
                require_origin_prereq_label=require,
            ),
        ),
    )


def _pr(number, labels, repo=ORIGIN):
    return PRInfo(
        number=number,
        title="t",
        body="b",
        state="merged",
        merge_commit_sha="deadbeef",
        head_sha="cafe",
        url=f"https://github.com/{repo}/pull/{number}",
        repo_slug=repo,
        labels=list(labels),
    )


class MatchesConfigLabels(unittest.TestCase):
    def test_all_labels_of_entry_present(self):
        cfg = _cfg([["antalya-26.3", "port-antalya"]])
        self.assertTrue(
            _matches_config_labels(cfg, _pr(1, ["antalya-26.3", "port-antalya"]))
        )

    def test_missing_one_anded_label(self):
        cfg = _cfg([["antalya-26.3", "port-antalya"]])
        self.assertFalse(_matches_config_labels(cfg, _pr(1, ["antalya-26.3"])))

    def test_case_insensitive(self):
        cfg = _cfg([["Antalya-26.3"]])
        self.assertTrue(_matches_config_labels(cfg, _pr(1, ["antalya-26.3"])))

    def test_exclude_label_disqualifies(self):
        cfg = _cfg([["antalya-26.3"]], exclude_labels=["wontport"])
        self.assertFalse(
            _matches_config_labels(cfg, _pr(1, ["antalya-26.3", "wontport"]))
        )

    def test_matches_any_entry(self):
        cfg = _cfg([["a", "b"], ["solo"]])
        self.assertTrue(_matches_config_labels(cfg, _pr(1, ["solo"])))

    def test_no_by_labels_never_matches(self):
        self.assertFalse(_matches_config_labels(_cfg([]), _pr(1, ["anything"])))


class RejectUnlabeledOriginPrereqs(unittest.TestCase):
    def setUp(self):
        self.cfg = _cfg([["antalya-26.3"]])
        self.trigger = _pr(100, ["antalya-26.3"])  # in origin

    def test_in_origin_unlabeled_rejected(self):
        out = _reject_unlabeled_origin_prereqs(
            self.cfg, self.trigger, [_pr(1, [])],
        )
        self.assertEqual([p.number for p in out], [1])

    def test_in_origin_labeled_allowed(self):
        out = _reject_unlabeled_origin_prereqs(
            self.cfg, self.trigger, [_pr(1, ["antalya-26.3"])],
        )
        self.assertEqual(out, [])

    def test_cross_repo_prereq_not_gated(self):
        out = _reject_unlabeled_origin_prereqs(
            self.cfg, self.trigger, [_pr(1, [], repo=FORK)],
        )
        self.assertEqual(out, [])

    def test_cross_repo_triggering_pr_disables_gate(self):
        trigger = _pr(100, [], repo=FORK)
        out = _reject_unlabeled_origin_prereqs(
            self.cfg, trigger, [_pr(1, [])],
        )
        self.assertEqual(out, [])

    def test_gate_disabled_via_config(self):
        cfg = _cfg([["antalya-26.3"]], require=False)
        out = _reject_unlabeled_origin_prereqs(cfg, self.trigger, [_pr(1, [])])
        self.assertEqual(out, [])

    def test_no_by_labels_is_noop(self):
        cfg = _cfg([])
        out = _reject_unlabeled_origin_prereqs(cfg, self.trigger, [_pr(1, [])])
        self.assertEqual(out, [])

    def test_mixed_only_unlabeled_in_origin_rejected(self):
        out = _reject_unlabeled_origin_prereqs(
            self.cfg,
            self.trigger,
            [
                _pr(1, ["antalya-26.3"]),       # labeled, origin → keep
                _pr(2, []),                      # unlabeled, origin → reject
                _pr(3, [], repo=FORK),           # cross-repo → keep
            ],
        )
        self.assertEqual([p.number for p in out], [2])


if __name__ == "__main__":
    unittest.main()
