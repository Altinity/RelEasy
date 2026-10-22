from __future__ import annotations

import unittest
from unittest import mock

import releasy.project_backport as pb
from releasy.github_ops import PRInfo


def _pr(num):
    return PRInfo(
        number=num, title="Crash in IN", body="", state="merged",
        merge_commit_sha="abc123", head_sha="",
        url=f"https://github.com/ClickHouse/ClickHouse/pull/{num}",
        repo_slug="ClickHouse/ClickHouse", labels=[], author="alice",
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

    def test_no_partial_match(self):
        self.assertFalse(pb._port_versions_includes("24.80", "24.8"))
        self.assertFalse(pb._port_versions_includes("24.8.14", "24.8"))

    def test_empty_and_none(self):
        self.assertFalse(pb._port_versions_includes(None, "24.8"))
        self.assertFalse(pb._port_versions_includes("", "24.8"))


class BranchName(unittest.TestCase):
    def test_sanitised(self):
        self.assertEqual(pb._backport_branch("24.8/foo", 5), "backport/24.8-foo/5")

    def test_sanitize_component(self):
        self.assertEqual(pb._sanitize_ref_component("24.8"), "24.8")
        self.assertEqual(pb._sanitize_ref_component("a b/c"), "a-b-c")


class ItemQualifies(unittest.TestCase):
    def test_missing_field_rejected(self):
        it = _item(port_versions=None)
        self.assertFalse(pb._item_qualifies(it, "24.8", pb.UPSTREAM_SLUG))


class RunDryRun(unittest.TestCase):
    def _run(self):
        UP = pb.UPSTREAM_SLUG
        items = [
            _item(repo=UP, number=100, port_versions="24.8"),
            _item(repo=UP, number=101, port_versions="24.8"),
            _item(repo=UP, number=102, port_versions="24.8"),
            _item(repo="Altinity/ClickHouse", number=900, port_versions="24.8"),
            _item(typename="DraftIssue", number=0, port_versions="24.8"),
            _item(repo=UP, number=103, port_versions="25.3"),
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
        self.assertEqual(set(by_num), {100, 101, 102})
        self.assertEqual(by_num[100].status, "would_create")
        self.assertEqual(by_num[101].status, "skipped")
        self.assertEqual(by_num[102].status, "skipped")
        self.assertIsNone(res.fatal)
        self.assertFalse(res.had_failures)
        self.assertEqual([o.upstream_number for o in res.outcomes], [102, 101, 100])


class RunFatal(unittest.TestCase):
    def test_missing_port_versions_field_is_fatal(self):
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
