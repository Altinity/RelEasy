"""Tests for the stateless cherry-pick PR-body composition."""

from __future__ import annotations

import unittest

import releasy.stateless as st
from releasy.github_ops import PRInfo


CH_BODY = (
    "### Changelog category (leave one):\n\n- Bug Fix\n\n"
    "### Changelog entry (a user-readable short description of the changes "
    "that goes to CHANGELOG.md):\n\n"
    "Fixed a crash when reading Iceberg tables.\n\n"
    "### CI/CD Options\n#### Exclude tests:\n- [ ] Fast test\n"
)

TARGET_TEMPLATE = (
    "## Changelog category (leave one):\n- Bug Fix\n\n"
    "### CI/CD Options\n#### Exclude tests:\n"
    "- [ ] <!---ci_exclude_fast--> Fast test\n"
    "- [x] <!---ci_exclude_tsan--> All with TSAN\n"
)


def _pr(**kw):
    base = dict(
        number=12345, title="Fix S3 thing", body=CH_BODY, state="merged",
        merge_commit_sha="abc123", head_sha="abc123",
        url="https://github.com/ClickHouse/ClickHouse/pull/12345",
        repo_slug="ClickHouse/ClickHouse", author="alice",
    )
    base.update(kw)
    return PRInfo(**base)


class CiOptionsSectionTest(unittest.TestCase):
    def test_extracts_section_verbatim_from_template(self):
        section = st._ci_options_section(TARGET_TEMPLATE)
        self.assertTrue(section.startswith("### CI/CD Options"))
        self.assertIn("<!---ci_exclude_tsan--> All with TSAN", section)
        # The changelog heading above it is not swept in.
        self.assertNotIn("Changelog category", section)

    def test_falls_back_to_default_block(self):
        for missing in (None, "## No CI section here\n- [ ] whatever\n"):
            section = st._ci_options_section(missing)
            self.assertTrue(section.startswith("### CI/CD Options"))
            self.assertIn("ci_exclude_fast", section)


class ChangelogBlockTest(unittest.TestCase):
    def test_entry_gets_url_and_author_attribution(self):
        block = st._build_changelog_block_for_pr(_pr())
        self.assertIn("- Bug Fix", block)
        self.assertIn(
            "Fixed a crash when reading Iceberg tables "
            "(https://github.com/ClickHouse/ClickHouse/pull/12345 by @alice).",
            block,
        )

    def test_missing_author_falls_back_to_url_only(self):
        block = st._build_changelog_block_for_pr(_pr(author=None))
        self.assertIn(
            "(https://github.com/ClickHouse/ClickHouse/pull/12345).", block,
        )

    def test_no_changelog_metadata_returns_none(self):
        self.assertIsNone(st._build_changelog_block_for_pr(_pr(body="just words")))
        self.assertIsNone(st._build_changelog_block_for_pr(None))


class PrBodyTest(unittest.TestCase):
    def test_full_body_composition(self):
        ci = st._ci_options_section(TARGET_TEMPLATE)
        body = _pr().url
        out = st._pr_body(_pr().url, _pr(), ci)
        self.assertTrue(out.startswith(f"Cherry-picked from {body}."))
        self.assertIn("by @alice).", out)
        self.assertIn("### CI/CD Options", out)
        # The raw upstream body's own "- [ ] Fast test" (no HTML marker) is
        # not pasted in; only the target template's CI section is.
        self.assertIn("<!---ci_exclude_fast--> Fast test", out)

    def test_non_pr_source_has_provenance_and_ci_only(self):
        ci = st._ci_options_section(TARGET_TEMPLATE)
        out = st._pr_body("https://github.com/x/y/commit/deadbeef", None, ci)
        self.assertTrue(out.startswith("Cherry-picked from "))
        self.assertIn("### CI/CD Options", out)
        self.assertNotIn("Changelog", out)


if __name__ == "__main__":
    unittest.main()
