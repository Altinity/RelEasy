from __future__ import annotations

import unittest
from types import SimpleNamespace

from releasy.github_ops import parse_cherry_picked_refs
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
        self.state = SimpleNamespace(features={
            "pr-1687": FeatureState(
                pr_urls=[_url(1687)], rebase_pr_url=_url(1950),
            ),
        })

    def test_own_port_pr_recognized(self):
        out = _find_already_queued_prereqs(
            _cfg(), self.state, [_url(1950)], exclude_feature_id="pr-1759",
        )
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["queued_in"], "pr-1687")
        self.assertEqual(out[0]["queued_in_pr_url"], _url(1950))

    def test_upstream_source_pr_still_recognized(self):
        out = _find_already_queued_prereqs(
            _cfg(), self.state, [_url(1687)], exclude_feature_id="pr-1759",
        )
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["queued_in"], "pr-1687")

    def test_unrelated_pr_not_flagged(self):
        out = _find_already_queued_prereqs(
            _cfg(), self.state, [_url(9999)], exclude_feature_id="pr-1759",
        )
        self.assertEqual(out, [])

    def test_excluded_feature_skipped(self):
        out = _find_already_queued_prereqs(
            _cfg(), self.state, [_url(1950)], exclude_feature_id="pr-1687",
        )
        self.assertEqual(out, [])


COMBINED_BODY = """\
### Changelog entry

Export parts and partitions (https://github.com/Altinity/ClickHouse/pull/9001 \
by @someone).

Combined port of 12 PR(s) (group `apassos-3`). Cherry-picked from #1388, \
#1405, #1618, ClickHouse/ClickHouse#100452.

- #1388 — Antalya 26.1 - Forward port of export part and partition
"""


class CombinedPortProvenance(unittest.TestCase):
    def setUp(self):
        self.state = SimpleNamespace(features={
            "auto-grp-pr-1718": FeatureState(
                pr_url=_url(1718),
                pr_urls=[_url(1718), _url(1646)],
                pr_body=COMBINED_BODY,
                contained_pr_urls=[_url(1388), _url(1405), _url(1618)],
                rebase_pr_url=_url(2146),
            ),
        })

    def test_carried_prereq_recognized(self):
        out = _find_already_queued_prereqs(
            _cfg(), self.state, [_url(1388)], exclude_feature_id="pr-1832",
        )
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["queued_in"], "auto-grp-pr-1718")
        self.assertEqual(out[0]["queued_in_pr_url"], _url(2146))
        self.assertTrue(out[0]["carried"])

    def test_legacy_state_falls_back_to_body(self):
        self.state.features["auto-grp-pr-1718"].contained_pr_urls = []
        out = _find_already_queued_prereqs(
            _cfg(), self.state, [_url(1618)], exclude_feature_id="pr-1832",
        )
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["queued_in"], "auto-grp-pr-1718")
        self.assertTrue(out[0]["carried"])

    def test_direct_claim_wins_over_carried(self):
        cfg = _cfg(groups=[SimpleNamespace(id="grp-x", prs=[_url(1388)])])
        out = _find_already_queued_prereqs(
            cfg, self.state, [_url(1388)], exclude_feature_id="pr-1832",
        )
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["queued_in"], "config:groups[grp-x]")
        self.assertFalse(out[0]["carried"])

    def test_excluded_feature_skipped(self):
        out = _find_already_queued_prereqs(
            _cfg(), self.state, [_url(1388)],
            exclude_feature_id="auto-grp-pr-1718",
        )
        self.assertEqual(out, [])


class CherryPickedRefParsing(unittest.TestCase):
    def test_combined_port_clause_mid_line(self):
        refs = parse_cherry_picked_refs(COMBINED_BODY, "Altinity/ClickHouse")
        self.assertEqual(refs, [
            ("Altinity", "ClickHouse", 1388),
            ("Altinity", "ClickHouse", 1405),
            ("Altinity", "ClickHouse", 1618),
            ("ClickHouse", "ClickHouse", 100452),
        ])

    def test_single_pr_clause_stops_at_end_of_line(self):
        body = (
            "Cherry-picked from ClickHouse/ClickHouse#100452.\n"
            "\n---\n\nOriginal body mentioning #999.\n"
        )
        self.assertEqual(
            parse_cherry_picked_refs(body, "Altinity/ClickHouse"),
            [("ClickHouse", "ClickHouse", 100452)],
        )

    def test_full_url_ref(self):
        self.assertEqual(
            parse_cherry_picked_refs(
                "Cherry-picked from "
                "https://github.com/ClickHouse/ClickHouse/pull/107960.",
                "Altinity/ClickHouse",
            ),
            [("ClickHouse", "ClickHouse", 107960)],
        )

    def test_bare_ref_needs_a_default_slug(self):
        body = "Cherry-picked from #1388, ClickHouse/ClickHouse#42."
        self.assertEqual(
            parse_cherry_picked_refs(body, None),
            [("ClickHouse", "ClickHouse", 42)],
        )

    def test_no_clause_and_empty_body(self):
        self.assertEqual(parse_cherry_picked_refs("see #123", "a/b"), [])
        self.assertEqual(parse_cherry_picked_refs(None, "a/b"), [])


if __name__ == "__main__":
    unittest.main()
