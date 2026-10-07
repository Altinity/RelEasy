"""A group whose port PR is open picks up a member added later.

Stdlib unittest (no pytest dependency). Run:
    python3 -m unittest discover -s tests
"""
from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path

import releasy.pipeline as p
from releasy.github_ops import PRInfo


def _pr(n: int) -> PRInfo:
    return PRInfo(
        number=n, title=f"PR {n}", body="", state="merged",
        merge_commit_sha=None, head_sha="",
        url=f"https://github.com/acme/repo/pull/{n}", repo_slug="acme/repo",
    )


class GroupBranchMissingMembers(unittest.TestCase):

    def _git(self, *args):
        return subprocess.run(
            ["git", *args], cwd=self.repo, check=True,
            capture_output=True, text=True, env=self.env,
        ).stdout.strip()

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.repo = Path(self._tmp.name)
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
        self._git("init", "-q")
        self._git("-c", "commit.gpgsign=false", "commit", "--allow-empty",
                  "-q", "-m", "base")
        self.base = self._git("rev-parse", "HEAD")
        # Port branch carrying PR 1 only.
        self._git("checkout", "-q", "-b", "port")
        self._git("-c", "commit.gpgsign=false", "commit", "--allow-empty",
                  "-q", "-m",
                  f"port PR 1\n\nSource-PR: {_pr(1).url}")

    def _unit(self, *nums: int) -> p.FeatureUnit:
        return p.FeatureUnit(
            feature_id="auto-grp-1", prs=[_pr(n) for n in nums],
            if_exists="recreate", is_group=True, group_id="auto-grp-1",
        )

    def test_new_member_is_missing(self):
        self.assertTrue(p._group_branch_missing_members(
            self.repo, self.base, "port", self._unit(1, 2), "acme/repo",
        ))

    def test_unchanged_group_is_not_missing(self):
        self.assertFalse(p._group_branch_missing_members(
            self.repo, self.base, "port", self._unit(1), "acme/repo",
        ))


if __name__ == "__main__":
    unittest.main()
