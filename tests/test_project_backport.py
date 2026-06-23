"""Tests for the stateless GitHub-Project-driven backport command."""

from __future__ import annotations

import unittest

import releasy.project_backport as pb
from releasy.github_ops import PRInfo


def _pr(num=89367, *, title="Crash in IN", body="", author="alice", slug="ClickHouse/ClickHouse"):
    return PRInfo(
        number=num, title=title, body=body, state="merged",
        merge_commit_sha="abc123", head_sha="",
        url=f"https://github.com/ClickHouse/ClickHouse/pull/{num}",
        repo_slug=slug, labels=[], author=author,
    )


def _item(*, typename="PullRequest", repo="ClickHouse/ClickHouse",
          number=89367, port_versions="24.8"):
    fv = {}
    if port_versions is not None:
        fv["port versions"] = port_versions
    return {
        "item_id": "I_1",
        "content_typename": typename,
        "pr_number": number,
        "pr_url": f"https://github.com/{repo}/pull/{number}",
        "repo_slug": repo,
        "field_values": fv,
    }


class PortVersionsIncludes(unittest.TestCase):
    def test_text_list_match(self):
        self.assertTrue(pb._port_versions_includes("24.8, 25.3", "24.8"))
        self.assertTrue(pb._port_versions_includes("24.8, 25.3", "25.3"))

    def test_exact_match(self):
        self.assertTrue(pb._port_versions_includes("24.8", "24.8"))

    def test_no_partial_match(self):
        # Must not match a longer adjacent version.
        self.assertFalse(pb._port_versions_includes("24.80", "24.8"))
        self.assertFalse(pb._port_versions_includes("24.8.14", "24.8"))

    def test_empty_and_none(self):
        self.assertFalse(pb._port_versions_includes(None, "24.8"))
        self.assertFalse(pb._port_versions_includes("", "24.8"))


class BranchName(unittest.TestCase):
    def test_simple(self):
        self.assertEqual(pb._backport_branch("24.8", 81234), "backport/24.8/81234")

    def test_sanitised(self):
        self.assertEqual(pb._backport_branch("24.8/foo", 5), "backport/24.8-foo/5")

    def test_sanitize_component(self):
        self.assertEqual(pb._sanitize_ref_component("24.8"), "24.8")
        self.assertEqual(pb._sanitize_ref_component("a b/c"), "a-b-c")


class ItemQualifies(unittest.TestCase):
    def test_upstream_pr_matching_version(self):
        self.assertTrue(pb._item_qualifies(_item(), "24.8", pb.UPSTREAM_SLUG))

    def test_origin_pr_rejected(self):
        it = _item(repo="Altinity/ClickHouse")
        self.assertFalse(pb._item_qualifies(it, "24.8", pb.UPSTREAM_SLUG))

    def test_draft_issue_rejected(self):
        it = _item(typename="DraftIssue")
        self.assertFalse(pb._item_qualifies(it, "24.8", pb.UPSTREAM_SLUG))

    def test_wrong_version_rejected(self):
        it = _item(port_versions="25.3")
        self.assertFalse(pb._item_qualifies(it, "24.8", pb.UPSTREAM_SLUG))

    def test_missing_field_rejected(self):
        it = _item(port_versions=None)
        self.assertFalse(pb._item_qualifies(it, "24.8", pb.UPSTREAM_SLUG))


class PRTitle(unittest.TestCase):
    def test_format(self):
        self.assertEqual(
            pb._pr_title("24.8", _pr(89367, title="Crash in IN function")),
            "24.8 Backport of #89367 - Crash in IN function",
        )


class ChangelogBlock(unittest.TestCase):
    UPSTREAM_BODY = (
        "### Changelog category (leave one):\n"
        "- Bug Fix (user-visible misbehavior in an official stable release)\n\n"
        "### Changelog entry (a user-readable short description):\n"
        "Possible crash in IN function.\n"
    )

    def test_includes_category_entry_and_attribution(self):
        pr = _pr(89367, body=self.UPSTREAM_BODY, author="ilejn")
        block = pb._build_changelog_block_for_pr(pr)
        self.assertIn("### Changelog category (leave one):", block)
        self.assertIn("- Bug Fix (user-visible misbehavior", block)
        self.assertIn("### Changelog entry", block)
        # Entry text + attribution; trailing period folded before paren.
        self.assertIn(
            "Possible crash in IN function "
            "(https://github.com/ClickHouse/ClickHouse/pull/89367 by @ilejn).",
            block,
        )

    def test_none_when_no_changelog(self):
        self.assertIsNone(pb._build_changelog_block_for_pr(_pr(1, body="no sections here")))


class CiOptionsSection(unittest.TestCase):
    TEMPLATE = (
        "<!-- a comment -->\n"
        "### Changelog category (leave one):\n- ...\n\n"
        "### Changelog entry (...):\n...\n\n"
        "### CI/CD Options\n"
        "#### Exclude tests:\n"
        "- [ ] <!---ci_exclude_fast--> Fast test\n"
        "- [x] <!---ci_exclude_asan--> All with ASAN\n"
    )

    def test_extracts_section_from_template(self):
        section = pb._ci_options_section(self.TEMPLATE)
        self.assertTrue(section.startswith("### CI/CD Options"))
        self.assertIn("All with ASAN", section)
        self.assertNotIn("Changelog", section)

    def test_falls_back_to_default_when_missing(self):
        self.assertIn("### CI/CD Options", pb._ci_options_section(None))
        self.assertIn("### CI/CD Options", pb._ci_options_section("no ci section here"))

    def test_keeps_trailing_content_below_checkboxes(self):
        # "and everything below" — content after the last checkbox is kept.
        template = self.TEMPLATE + "\n#### Notes\nRun the thing.\n"
        section = pb._ci_options_section(template)
        self.assertIn("#### Notes", section)
        self.assertIn("Run the thing.", section)


class PrBody(unittest.TestCase):
    def test_combines_changelog_and_ci(self):
        body = pb._build_pr_body("CHANGELOG", "### CI/CD Options\n- [ ] x")
        self.assertIn("CHANGELOG", body)
        self.assertIn("### CI/CD Options", body)
        self.assertTrue(body.endswith("\n"))

    def test_changelog_optional(self):
        body = pb._build_pr_body(None, "### CI/CD Options")
        self.assertTrue(body.startswith("### CI/CD Options"))


class RunDryRun(unittest.TestCase):
    """End-to-end orchestration (filter / sort / idempotency / dry-run) with
    the GitHub layer mocked out."""

    def _run(self):
        from unittest import mock

        UP = pb.UPSTREAM_SLUG
        items = [
            _item(repo=UP, number=100, port_versions="24.8"),                 # would_create
            _item(repo=UP, number=101, port_versions="24.8"),                 # skip: exists
            _item(repo=UP, number=102, port_versions="24.8"),                 # skip: not merged
            _item(repo="Altinity/ClickHouse", number=900, port_versions="24.8"),  # filtered: origin
            _item(typename="DraftIssue", number=0, port_versions="24.8"),     # filtered: draft
            _item(repo=UP, number=103, port_versions="25.3"),                 # filtered: version
        ]
        unmerged = PRInfo(
            number=102, title="x", body="", state="open", merge_commit_sha=None,
            head_sha="", url="u", repo_slug=UP, author="a",
        )
        prs = {100: _pr(100), 101: _pr(101), 102: unmerged}

        def fake_existing(cfg, target, n, url=None):
            return "https://github.com/Altinity/ClickHouse/pull/777" if n == 101 else None

        with mock.patch.object(pb, "_parse_project_url", lambda u: ("Altinity", 26, True)), \
             mock.patch.object(pb, "_get_project_id", lambda o, n, org: "PROJ"), \
             mock.patch.object(
                 pb, "_find_field_node",
                 lambda pid, name: {"id": "F", "name": "Port Versions", "dataType": "TEXT"}), \
             mock.patch.object(pb, "list_project_items_for_backport", lambda pid: items), \
             mock.patch.object(pb, "fetch_pr_by_number",
                               lambda cfg, n, slug=None, include_closed=False: prs.get(int(n))), \
             mock.patch.object(pb, "find_latest_pr_for_branch", lambda cfg, b, base=None: None), \
             mock.patch.object(pb, "find_open_backport_pr", fake_existing):
            opts = pb.ProjectBackportOptions(
                project_url="https://github.com/orgs/Altinity/projects/26",
                version="24.8", target="customizations/24.8.14", dry_run=True,
            )
            return pb.run_project_backport(opts)

    def test_filters_sorts_and_classifies(self):
        res = self._run()
        by_num = {o.upstream_number: o for o in res.outcomes}
        # Origin PR / draft / wrong-version items are filtered out entirely.
        self.assertEqual(set(by_num), {100, 101, 102})
        self.assertEqual(by_num[100].status, "would_create")
        self.assertEqual(by_num[101].status, "skipped")  # existing backport
        self.assertEqual(by_num[102].status, "skipped")  # not merged
        self.assertIsNone(res.fatal)
        self.assertFalse(res.had_failures)
        # Newest-first ordering.
        self.assertEqual([o.upstream_number for o in res.outcomes], [102, 101, 100])


class RunFatal(unittest.TestCase):
    def test_missing_port_versions_field_is_fatal(self):
        from unittest import mock
        with mock.patch.object(pb, "_parse_project_url", lambda u: ("Altinity", 26, True)), \
             mock.patch.object(pb, "_get_project_id", lambda o, n, org: "PROJ"), \
             mock.patch.object(pb, "_find_field_node", lambda pid, name: None):
            res = pb.run_project_backport(pb.ProjectBackportOptions(
                project_url="https://github.com/orgs/Altinity/projects/26",
                version="24.8", target="b", dry_run=True,
            ))
        self.assertIsNotNone(res.fatal)
        self.assertIn("Port Versions", res.fatal)


if __name__ == "__main__":
    unittest.main()
