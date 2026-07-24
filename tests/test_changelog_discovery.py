"""Tests for the PR-based release-changelog discovery."""

from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path

import releasy.changelog as cl
from releasy.git_ops import first_parent_pr_numbers, pr_number_from_subject
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
        # Single range qualifier — two comparison qualifiers on one field make
        # GitHub Search silently drop the date filter (returns everything).
        q = build_merged_base_query(
            "o/r", "b", merged_from="2025-01-01T00:00:00+00:00",
            merged_to="2025-06-01T00:00:00+00:00",
        )
        self.assertIn(
            "merged:2025-01-01T00:00:00+00:00..2025-06-01T00:00:00+00:00", q,
        )
        # Exactly one merged: term (guards against a revert to the dual form).
        self.assertEqual(q.count("merged:"), 1)

    def test_window_lower_only(self):
        q = build_merged_base_query("o/r", "b", merged_from="2025-01-01")
        self.assertIn("merged:>=2025-01-01", q)

    def test_window_upper_only(self):
        q = build_merged_base_query("o/r", "b", merged_to="2025-06-01")
        self.assertIn("merged:<=2025-06-01", q)

    def test_exclude_labels_quoted(self):
        q = build_merged_base_query(
            "o/r", "b", exclude_labels=["forwardport", "forward port"],
        )
        self.assertIn('-label:"forwardport"', q)
        self.assertIn('-label:"forward port"', q)


class PrNumberFromSubject(unittest.TestCase):
    def test_merge_commit(self):
        self.assertEqual(
            pr_number_from_subject(
                "Merge pull request #2011 from Altinity/backports/24.8.14/99119"
            ),
            2011,
        )

    def test_squash_commit_trailing_paren(self):
        self.assertEqual(
            pr_number_from_subject("Fix a nasty crash in the parser (#1234)"),
            1234,
        )

    def test_merge_branch_not_matched(self):
        # A target-branch merge is not a PR reference.
        self.assertIsNone(
            pr_number_from_subject(
                "Merge branch 'customizations/24.8.14' into backports/24.8/79147"
            )
        )

    def test_direct_push_not_matched(self):
        self.assertIsNone(pr_number_from_subject("Bump version to 24.8.14.10547"))

    def test_mid_subject_hash_not_matched(self):
        # Only a *trailing* (#N) counts — an inline "#N" mention doesn't.
        self.assertIsNone(
            pr_number_from_subject("Backport #93016 to 24.8 Altinity Stable")
        )

    def test_merge_prefix_wins_over_trailing_paren(self):
        # The merge-commit number is the PR; a trailing (#M) doesn't override it.
        self.assertEqual(
            pr_number_from_subject("Merge pull request #10 from x/y (#20)"), 10,
        )

    def test_revert_squash_matched(self):
        # A reverted-via-PR squash subject is a real merged PR.
        self.assertEqual(
            pr_number_from_subject('Revert "Broken change" (#77)'), 77,
        )


class FirstParentPrNumbers(unittest.TestCase):
    """Integration test over a real temp git repo (first-parent + dedup + sort)."""

    def _git(self, *args):
        return subprocess.run(
            ["git", *args], cwd=self.repo, check=True,
            capture_output=True, text=True, env=self.env,
        ).stdout.strip()

    def _commit(self, subject):
        # -c commit.gpgsign=false works on every git; GIT_CONFIG_* isolation
        # (below) needs 2.32+, so keep both.
        self._git("-c", "commit.gpgsign=false", "commit", "--allow-empty", "-m", subject)
        return self._git("rev-parse", "HEAD")

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)  # runs even if setUp raises
        self.repo = Path(self._tmp.name)
        # Isolate from the host: drop git's repo-redirecting vars and pin
        # config to /dev/null so global gpgsign / hooks can't interfere.
        self.env = {
            k: v for k, v in os.environ.items()
            if k not in {
                "GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_COMMON_DIR",
            }
        }
        self.env.update({
            "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
            "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t",
            "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_SYSTEM": os.devnull,
            "HOME": str(self.repo),
        })
        # No -b/--initial-branch (needs git 2.28); read the default branch back.
        self._git("init", "-q")
        self.main = self._git("symbolic-ref", "--short", "HEAD")
        self.base = self._commit("base")

    def test_first_parent_only_dedup_sorted(self):
        # A side branch whose inner commit references #999 — must be excluded
        # because it lives on second-parent history.
        self._git("checkout", "-q", "-b", "feature")
        self._commit("inner work (#999)")
        self._git("checkout", "-q", self.main)
        self._git(
            "merge", "--no-ff", "-m", "Merge pull request #30 from feature",
            "feature",
        )
        self._commit("Direct push, no PR")
        self._commit("A squash-merged change (#10)")
        to = self._commit("Merge branch 'main' of origin")  # not a PR

        nums = first_parent_pr_numbers(self.repo, self.base, to)
        # #999 excluded (second-parent), non-PR commits ignored, ascending.
        self.assertEqual(nums, [10, 30])

    def test_empty_range(self):
        self.assertEqual(first_parent_pr_numbers(self.repo, self.base, self.base), [])


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


class InlineBundleSplit(unittest.TestCase):
    """A manual port PR whose one entry section inlines ≥2 attributed backports."""

    _CH = "https://github.com/ClickHouse/ClickHouse/pull"

    def _entries(self, entry, category="Bug Fix"):
        pr = _pr(2045, body=_body(category, entry),
                 title="Backport #93016 to 24.8 Altinity Stable")
        pr.url = "https://github.com/o/r/pull/2045"
        return cl._entries_for_pr(None, pr, "o/r", {})

    def test_two_inlined_entries_split(self):
        entry = (
            f"Fix sparse column mutation error.\n"
            f"({self._CH}/93016 by @avogar)\n"
            f"Rebuild projection on alter modify of its PK column.\n"
            f"({self._CH}/75720 by @avogar)"
        )
        es = self._entries(entry)
        self.assertEqual(len(es), 2)
        self.assertEqual(
            [e.description for e in es],
            ["Fix sparse column mutation error.",
             "Rebuild projection on alter modify of its PK column."],
        )
        self.assertEqual([e.upstream_prs[0].number for e in es], [93016, 75720])
        # Both bullets share the PR's single category and point via one PR.
        self.assertTrue(all(e.section == cl.SECTION_BUG_FIXES for e in es))
        self.assertEqual(
            cl._render_entry(es[0]),
            f"* Fix sparse column mutation error. "
            f"({self._CH}/93016 by @avogar via https://github.com/o/r/pull/2045)",
        )
        self.assertEqual(
            cl._render_entry(es[1]),
            f"* Rebuild projection on alter modify of its PK column. "
            f"({self._CH}/75720 by @avogar via https://github.com/o/r/pull/2045)",
        )

    def test_single_inlined_entry_not_split(self):
        # One attribution paren → normal single-entry path, not the splitter.
        es = self._entries(f"Fix one thing. ({self._CH}/93016 by @avogar)")
        self.assertEqual(len(es), 1)
        self.assertEqual(es[0].description, "Fix one thing.")
        self.assertEqual(es[0].upstream_prs[0].number, 93016)

    def test_benign_parens_do_not_split(self):
        # Parentheticals that name no upstream PR are not entry boundaries.
        es = self._entries("Fix A (descending order). Fix B (edge case).")
        self.assertEqual(len(es), 1)
        self.assertEqual(
            es[0].description, "Fix A (descending order). Fix B (edge case).")

    def test_not_for_changelog_bundle_dropped(self):
        entry = (
            f"Fix A.\n({self._CH}/93016 by @avogar)\n"
            f"Fix B.\n({self._CH}/75720 by @avogar)"
        )
        es = self._entries(entry, category="Not for Changelog")
        self.assertEqual(es, [])


class SplitInlineEntries(unittest.TestCase):
    _CH = "https://github.com/ClickHouse/ClickHouse/pull"

    def test_none_when_fewer_than_two(self):
        self.assertIsNone(
            cl._split_inline_entries(f"Fix. ({self._CH}/1 by @a)", "o/r"))
        self.assertIsNone(cl._split_inline_entries("no parens here", "o/r"))

    def test_crlf_section(self):
        # Real PR bodies use CRLF; the split must survive it.
        section = (
            f"Fix A.\r\n({self._CH}/1 by @a)\r\n"
            f"Fix B.\r\n({self._CH}/2 by @b)"
        )
        chunks = cl._split_inline_entries(section, "o/r")
        self.assertEqual(len(chunks), 2)
        self.assertEqual(chunks[0][0], "Fix A.")
        self.assertEqual(chunks[0][1], [("ClickHouse/ClickHouse", 1, "a")])
        self.assertEqual(chunks[1][1], [("ClickHouse/ClickHouse", 2, "b")])

    def test_dangling_attribution_joins_previous(self):
        # A back-to-back attribution with no description of its own attaches to
        # the preceding entry rather than becoming a bullet with empty text.
        section = (
            f"Fix A. ({self._CH}/1 by @a) ({self._CH}/2 by @b) "
            f"Fix B. ({self._CH}/3 by @c)"
        )
        chunks = cl._split_inline_entries(section, "o/r")
        self.assertEqual(len(chunks), 2)
        self.assertEqual(chunks[0][0], "Fix A.")
        self.assertEqual(
            chunks[0][1],
            [("ClickHouse/ClickHouse", 1, "a"), ("ClickHouse/ClickHouse", 2, "b")],
        )
        self.assertEqual(chunks[1][0], "Fix B.")
        self.assertEqual(chunks[1][1], [("ClickHouse/ClickHouse", 3, "c")])


if __name__ == "__main__":
    unittest.main()
