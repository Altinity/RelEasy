"""Tests for the PR-based release-changelog discovery."""

from __future__ import annotations

import unittest

import releasy.changelog as cl
from releasy.github_ops import PRInfo, build_merged_base_query


def _pr(num, *, body="", labels=None, title="t"):
    return PRInfo(
        number=num, title=title, body=body, state="merged",
        merge_commit_sha=None, head_sha="", url=f"https://github.com/o/r/pull/{num}",
        repo_slug="o/r", labels=labels or [], author="alice",
    )


def _body(category=None, entry=None):
    parts = []
    if category is not None:
        parts.append(f"## Changelog category\n{category}")
    if entry is not None:
        parts.append(f"## Changelog entry\n{entry}")
    return "\n\n".join(parts)


class BuildMergedBaseQuery(unittest.TestCase):
    def test_minimal(self):
        q = build_merged_base_query("o/r", "antalya-26.3")
        self.assertEqual(q, "repo:o/r is:pr is:merged base:antalya-26.3")

    def test_window_both_bounds(self):
        q = build_merged_base_query(
            "o/r", "b", merged_from="2025-01-01T00:00:00+00:00",
            merged_to="2025-06-01T00:00:00+00:00",
        )
        # Lower bound EXCLUSIVE (avoids double-counting the --from boundary).
        self.assertIn("merged:>2025-01-01T00:00:00+00:00", q)
        self.assertIn("merged:<=2025-06-01T00:00:00+00:00", q)

    def test_window_lower_only(self):
        q = build_merged_base_query("o/r", "b", merged_from="2025-01-01")
        self.assertIn("merged:>2025-01-01", q)

    def test_window_upper_only(self):
        q = build_merged_base_query("o/r", "b", merged_to="2025-06-01")
        self.assertIn("merged:<=2025-06-01", q)

    def test_exclude_labels_quoted(self):
        q = build_merged_base_query(
            "o/r", "b", exclude_labels=["forwardport", "forward port"],
        )
        self.assertIn('-label:"forwardport"', q)
        self.assertIn('-label:"forward port"', q)


class IsForwardPort(unittest.TestCase):
    def test_label_forwardport(self):
        self.assertTrue(cl._is_forward_port(_pr(1, labels=["forwardport"])))

    def test_label_hyphenated(self):
        self.assertTrue(cl._is_forward_port(_pr(1, labels=["forward-port"])))

    def test_title(self):
        self.assertTrue(cl._is_forward_port(_pr(1, title="Forward port of X")))

    def test_not_a_forward_port(self):
        self.assertFalse(cl._is_forward_port(_pr(1, labels=["bug"], title="Fix X")))


class EntriesForPr(unittest.TestCase):
    """``_entries_for_pr`` classifies/drops a PR (no upstream refs → config unused)."""

    def _entries(self, pr):
        return cl._entries_for_pr(None, pr, "o/r", {})

    def _one(self, pr):
        es = self._entries(pr)
        self.assertEqual(len(es), 1)
        return es[0]

    def test_new_feature(self):
        e = self._one(_pr(1, body=_body("New Feature", "Add cool thing.")))
        self.assertEqual(e.section, cl.SECTION_NEW_FEATURES)
        self.assertEqual(e.description, "Add cool thing.")

    def test_forward_port_dropped(self):
        pr = _pr(1, body=_body("New Feature", "x"), labels=["forwardport"])
        self.assertEqual(self._entries(pr), [])

    def test_not_for_changelog_dropped(self):
        pr = _pr(1, body=_body("Not for Changelog (changelog entry is not required)", "x"))
        self.assertEqual(self._entries(pr), [])

    def test_no_entry_dropped(self):
        self.assertEqual(self._entries(_pr(1, body=_body("New Feature", None))), [])

    def test_category_absent_folds_to_improvements(self):
        e = self._one(_pr(1, body=_body(None, "Tweak something.")))
        self.assertEqual(e.section, cl.SECTION_IMPROVEMENTS)


_UP_URL = "https://github.com/ClickHouse/ClickHouse/pull/101272"


def _upstream():
    return PRInfo(
        number=101272, title="t", body="", state="merged", merge_commit_sha=None,
        head_sha="", url=_UP_URL, repo_slug="ClickHouse/ClickHouse", author="nihalzp",
    )


class TrailingParenGroup(unittest.TestCase):
    def test_simple(self):
        self.assertEqual(cl._trailing_paren_group("x (a b)"), (2, "a b"))

    def test_nested_markdown_link(self):
        # The whole balanced group is returned, spanning the inner (url).
        start, inner = cl._trailing_paren_group("x ([#1](http://h/1) by @a)")
        self.assertEqual(inner, "[#1](http://h/1) by @a")

    def test_no_trailing_paren(self):
        self.assertIsNone(cl._trailing_paren_group("ends with text"))

    def test_mid_string_paren_not_matched(self):
        self.assertIsNone(cl._trailing_paren_group("Nullable(Tuple) for X"))


class StripRedundantParens(unittest.TestCase):
    def _strip(self, desc):
        return cl._strip_redundant_upstream_parens(desc, [_upstream()])

    def test_bare_url(self):
        self.assertEqual(self._strip(f"D ({_UP_URL} by @nihalzp)"), "D")

    def test_markdown_link(self):
        self.assertEqual(self._strip(f"D ([#101272]({_UP_URL}) by @nihalzp)"), "D")

    def test_shorthand(self):
        self.assertEqual(self._strip("D (ClickHouse/ClickHouse#101272 by @nihalzp)"), "D")

    def test_benign_paren_kept(self):
        self.assertEqual(self._strip("Fix sorting (descending order)"),
                         "Fix sorting (descending order)")

    def test_benign_number_collision_kept(self):
        # Trailing paren whose number equals the upstream PR's must NOT be
        # stripped on a bare '#N' match (only url / slug#N count).
        self.assertEqual(
            self._strip("Fix race (regression in #101272)"),
            "Fix race (regression in #101272)",
        )

    def test_no_upstream_no_strip(self):
        self.assertEqual(
            cl._strip_redundant_upstream_parens(f"D ({_UP_URL} by @x)", []),
            f"D ({_UP_URL} by @x)",
        )


class AttributionFromText(unittest.TestCase):
    def test_url_and_author(self):
        self.assertEqual(
            cl._attribution_from_text(f"{_UP_URL} by @nihalzp", "o/r"),
            [("ClickHouse/ClickHouse", 101272, "nihalzp")],
        )

    def test_shorthand(self):
        self.assertEqual(
            cl._attribution_from_text("ClickHouse/ClickHouse#101272 by @x", "o/r"),
            [("ClickHouse/ClickHouse", 101272, "x")],
        )

    def test_bare_hash_ignored(self):
        self.assertEqual(cl._attribution_from_text("see #1234", "o/r"), [])

    def test_origin_ref_excluded(self):
        self.assertEqual(
            cl._attribution_from_text("https://github.com/o/r/pull/5 by @x", "o/r"), [],
        )

    def test_two_refs_two_authors(self):
        # Each ref is credited to the author that follows it, not the first.
        inner = "ClickHouse/ClickHouse#101 by @alice, ClickHouse/ClickHouse#102 by @bob"
        self.assertEqual(
            cl._attribution_from_text(inner, "o/r"),
            [("ClickHouse/ClickHouse", 101, "alice"),
             ("ClickHouse/ClickHouse", 102, "bob")],
        )

    def test_requires_at_sign(self):
        # "by hand" (no @) must not be parsed as an author.
        self.assertEqual(
            cl._attribution_from_text("ClickHouse/ClickHouse#101 fixed by hand", "o/r"),
            [("ClickHouse/ClickHouse", 101, None)],
        )


class EntryRecovery(unittest.TestCase):
    """`_entries_for_pr` recovers the via-form when no Cherry-picked-from line."""

    def _one(self, pr):
        es = cl._entries_for_pr(None, pr, "o/r", {})
        self.assertEqual(len(es), 1)
        return es[0]

    def test_recovers_upstream_attribution(self):
        # No "Cherry-picked from" → recovered from the entry's own paren.
        pr = _pr(1, body=_body("New Feature", f"Support X ({_UP_URL} by @nihalzp)"))
        self.assertEqual(
            cl._render_entry(self._one(pr)),
            f"* Support X ({_UP_URL} by @nihalzp via https://github.com/o/r/pull/1)",
        )

    def test_benign_paren_not_recovered(self):
        pr = _pr(1, body=_body("New Feature", "Fix sorting (descending order)"))
        e = self._one(pr)
        self.assertEqual(e.upstream_prs, [])
        self.assertEqual(
            cl._render_entry(e),
            "* Fix sorting (descending order) (https://github.com/o/r/pull/1 by @alice)",
        )


class BundleSplit(unittest.TestCase):
    """A port PR bundling ≥2 upstream backports → one bullet per upstream PR."""

    def test_entry_from_upstream(self):
        altinity = _pr(1773, body="")
        u = PRInfo(
            number=101278, title="t",
            body=_body("Bug Fix", "Fix Logical error on Iceberg UPDATE"),
            state="merged", merge_commit_sha=None, head_sha="",
            url="https://github.com/ClickHouse/ClickHouse/pull/101278",
            repo_slug="ClickHouse/ClickHouse", author="Desel72",
        )
        e = cl._entry_from_upstream(altinity, u)
        self.assertEqual(e.section, cl.SECTION_BUG_FIXES)
        self.assertEqual(
            cl._render_entry(e),
            "* Fix Logical error on Iceberg UPDATE "
            "(https://github.com/ClickHouse/ClickHouse/pull/101278 by @Desel72 "
            "via https://github.com/o/r/pull/1773)",
        )

    def test_two_upstream_two_bullets(self):
        def up(num, entry):
            return PRInfo(
                number=num, title="t", body=_body("Bug Fix", entry), state="merged",
                merge_commit_sha=None, head_sha="",
                url=f"https://github.com/ClickHouse/ClickHouse/pull/{num}",
                repo_slug="ClickHouse/ClickHouse", author="Desel72",
            )
        altinity = _pr(1773, body="")
        cache = {
            ("clickhouse/clickhouse", 101278): up(101278, "Fix A"),
            ("clickhouse/clickhouse", 102337): up(102337, "Fix B"),
        }
        # Stub the cherry-picked-from extraction to return both refs.
        orig = cl._extract_upstream_refs
        cl._extract_upstream_refs = lambda body, slug: [
            ("ClickHouse/ClickHouse", 101278), ("ClickHouse/ClickHouse", 102337),
        ]
        try:
            es = cl._entries_for_pr(None, altinity, "o/r", cache)
        finally:
            cl._extract_upstream_refs = orig
        self.assertEqual(len(es), 2)
        self.assertEqual([e.description for e in es], ["Fix A", "Fix B"])
        self.assertTrue(all(
            e.pr.url == "https://github.com/o/r/pull/1773" for e in es
        ))
        self.assertEqual([e.upstream_prs[0].number for e in es], [101278, 102337])


if __name__ == "__main__":
    unittest.main()
