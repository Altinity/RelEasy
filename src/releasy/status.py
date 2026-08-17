"""Shared display constants for pipeline status output.

Kept as a tiny module of its own so ``pipeline.print_status`` and any
future status renderer (`releasy list`, project-board sync, …) can share
the same icon / heading vocabulary without depending on the old
STATUS.md generator (which was removed when state moved out of the
user's repo).
"""

from __future__ import annotations


STATUS_ICONS: dict[str, str] = {
    "needs_review": "\U0001f535 needs-review",
    "branch_created": "\U0001f7e1 branch-created",
    "conflict": "\U0001f534 conflict",
    "build_failed": "\U0001f6a7 build-failed",
    "skipped": "\u23ed skipped",
    "merged": "\u2705 merged",
    "blocked": "\u23f8 blocked",
    "closed": "\u26d4 closed",
    "superseded": "\u267b superseded",
    "reverted": "\u21a9 reverted",
}

STATUS_HEADINGS: dict[str, str] = {
    "needs_review": "Needs Review",
    "branch_created": "Branch Created \u2014 PR not opened yet",
    "conflict": "Conflict \u2014 unresolved (manual fix required)",
    "build_failed": "Build Failed \u2014 resolved locally; build/tests retried next run",
    "skipped": "Skipped",
    "merged": "Merged \u2014 landed on target branch",
    "blocked": "Blocked \u2014 waiting on depends_on units",
    "closed": "Closed \u2014 rebase PR closed without merging",
    "superseded": "Superseded \u2014 another PR already cherry-picks the source",
    "reverted": "Reverted \u2014 port merged, then reverted on target (never re-ported)",
}
