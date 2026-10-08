"""Per-project pipeline state files (``state_root()/<name>.state.yaml``).

Each file records its owning ``config_path`` to detect name collisions between configs.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

import yaml

from releasy.config import Config, PortMode, state_file_path

BranchStatus = Literal[
    "needs_review",
    "branch_created",
    "conflict",
    # Resolved on a local branch (no PR) but build/tests failed; resumed next run.
    "build_failed",
    "skipped",
    "merged",
    "blocked",
    # Terminal: rebase PR closed without merging (``pr_policy.recreate_closed_prs`` re-ports).
    "closed",
    # Terminal: another PR on the same base already cherry-picked the source PR(s).
    "superseded",
    # Terminal: merged then reverted on target; set only by ``releasy mark-reverted``
    # (``pr_policy.recreate_reverted_prs`` re-ports).
    "reverted",
]
PipelinePhase = Literal["init", "ports_done"]


# Why a port stopped short of a mergeable PR.
StallKind = Literal[
    # A prerequisite is queued in another unit whose PR has not merged yet.
    "waiting_for_merge",
    # A prerequisite PR is not ported anywhere.
    "missing_prereq",
    # An attempt cap was hit (auto-continue / build-resume).
    "retries_exhausted",
    "unresolvable",
    # The auto-prereq dive hit its depth cap, a cycle, or a fetch failure.
    "prereq_search_exhausted",
    # AI resolution is off, or the backend died.
    "resolver_unavailable",
    "build_unfixed",
]

# Stalls re-running cannot fix: the awaited thing lives outside the unit.
# ``retries_exhausted`` is absent because caps are re-read from config each run.
BLOCKING_STALL_KINDS: frozenset[str] = frozenset(
    {"waiting_for_merge", "missing_prereq"}
)

# Dead-end stalls retried up to ``ai_resolve.max_dead_end_attempts`` runs.
CAPPED_STALL_KINDS: frozenset[str] = frozenset(
    {"unresolvable", "prereq_search_exhausted"}
)

# ``{targets}`` is filled from waiting_on_*.
_STALL_LABEL: dict[str, str] = {
    "waiting_for_merge": "waiting for {targets} to merge",
    "missing_prereq": "missing prereq {targets}",
    "retries_exhausted": "retries exhausted",
    "unresolvable": "resolver gave up",
    "prereq_search_exhausted": "prereq search exhausted",
    "resolver_unavailable": "resolver unavailable",
    "build_unfixed": "build still failing",
}

_STALL_PR_NUMBER_RE = re.compile(r"/pull/(\d+)")


def _pr_ref(url: str) -> str:
    m = _STALL_PR_NUMBER_RE.search(url or "")
    return f"#{m.group(1)}" if m else (url or "?")


@dataclass
class StallReason:
    kind: StallKind
    detail: str = ""
    # Feature IDs whose port PR must merge before a retry is useful.
    waiting_on_units: list[str] = field(default_factory=list)
    # Source PR URLs that must land.
    waiting_on_prs: list[str] = field(default_factory=list)
    # First-recorded time (ISO-8601 UTC) and consecutive runs in this stall.
    since: str | None = None
    runs: int = 1

    def targets(self) -> str:
        """What this stall waits on; units take precedence over PRs."""
        bits = (
            [f"`{u}`" for u in self.waiting_on_units] if self.waiting_on_units
            else [_pr_ref(u) for u in self.waiting_on_prs]
        )
        return ", ".join(bits) or "an external change"

    def summary(self, *, max_detail: int = 90) -> str:
        label = _STALL_LABEL.get(self.kind, self.kind)
        if "{targets}" in label:
            label = label.format(targets=self.targets())
        detail = " ".join((self.detail or "").split())
        if len(detail) > max_detail:
            detail = detail[: max_detail - 1].rstrip() + "…"
        return f"{label}: {detail}" if detail else label

    def same_wait_as(self, other: "StallReason") -> bool:
        """True when ``other`` is the same stall, not just the same kind."""
        return (
            self.kind == other.kind
            and self.waiting_on_units == other.waiting_on_units
            and self.waiting_on_prs == other.waiting_on_prs
        )

    def to_dict(self) -> dict:
        out: dict = {"kind": self.kind}
        if self.detail:
            out["detail"] = self.detail
        if self.waiting_on_units:
            out["waiting_on_units"] = list(self.waiting_on_units)
        if self.waiting_on_prs:
            out["waiting_on_prs"] = list(self.waiting_on_prs)
        if self.since:
            out["since"] = self.since
        if self.runs != 1:
            out["runs"] = self.runs
        return out

    @classmethod
    def from_dict(cls, raw: object) -> "StallReason | None":
        """Parse a serialized stall; ``None`` for anything unusable."""
        if not isinstance(raw, dict):
            return None
        kind = raw.get("kind")
        if not kind or not isinstance(kind, str):
            return None
        return cls(
            kind=kind,  # type: ignore[arg-type]
            detail=str(raw.get("detail") or ""),
            waiting_on_units=[str(x) for x in (raw.get("waiting_on_units") or [])],
            waiting_on_prs=[str(x) for x in (raw.get("waiting_on_prs") or [])],
            since=raw.get("since"),
            runs=int(raw.get("runs", 1) or 1),
        )


def clear_conflict_markers(fs: FeatureState) -> None:
    """Clear conflict bookkeeping and the stall once an entry's work is finished."""
    fs.conflict_files = []
    fs.failed_step_index = None
    fs.partial_pr_count = None
    fs.stall = None


def make_stall(
    kind: StallKind,
    *,
    detail: str = "",
    waiting_on_units: list[str] | None = None,
    waiting_on_prs: list[str] | None = None,
    prior: "FeatureState | None" = None,
) -> StallReason:
    """Build a :class:`StallReason`; repeating ``prior``'s stall keeps ``since``, bumps ``runs``."""
    stall = StallReason(
        kind=kind,
        detail=detail,
        waiting_on_units=list(waiting_on_units or []),
        waiting_on_prs=list(waiting_on_prs or []),
        since=datetime.now(timezone.utc).isoformat(),
    )
    old = prior.stall if prior is not None else None
    if old is not None and old.same_wait_as(stall):
        stall.since = old.since or stall.since
        stall.runs = old.runs + 1
    return stall


# Display order of status groups, highest-attention first.
STATUS_DISPLAY_ORDER: tuple[str, ...] = (
    "conflict",
    "build_failed",
    "blocked",
    "branch_created",
    "needs_review",
    "skipped",
    "closed",
    "superseded",
    "reverted",
    "merged",
)


_CONFIG_PATH_HISTORY_MAX = 8


class OwnershipCollisionError(Exception):
    """Raised when a state file is owned by a different config than the one loaded."""

    def __init__(
        self,
        name: str,
        state_path: Path,
        loaded_config: Path,
        stored_config: Path,
    ) -> None:
        self.name = name
        self.state_path = state_path
        self.loaded_config = loaded_config
        self.stored_config = stored_config
        super().__init__(
            f"Project name {name!r} is already tracked at "
            f"{stored_config}, but you ran releasy with config "
            f"{loaded_config}. Either pick a different 'name:' in the "
            f"new config, delete the old config, or run "
            f"`releasy adopt` to rebind state to the new config."
        )


@dataclass
class FeatureState:
    status: BranchStatus = "needs_review"
    branch_name: str | None = None
    base_commit: str | None = None
    conflict_files: list[str] = field(default_factory=list)
    # For groups pr_url/pr_number/pr_title hold the first PR; pr_numbers/pr_urls
    # hold every PR in cherry-pick order.
    pr_url: str | None = None
    pr_number: int | None = None
    pr_title: str | None = None
    pr_body: str | None = None
    pr_numbers: list[int] = field(default_factory=list)
    pr_urls: list[str] = field(default_factory=list)
    # Source PRs the unit's PRs say they cherry-picked (from their bodies).
    contained_pr_urls: list[str] = field(default_factory=list)
    # Login of the (first) source PR's author.
    pr_author: str | None = None
    rebase_pr_url: str | None = None  # auto-created PR targeting base branch
    ai_resolved: bool = False
    ai_iterations: int | None = None
    # Cumulative Claude cost across all resolves; ``None`` when unknown.
    ai_cost_usd: float | None = None
    # Set iff the verifier returned NEEDS_ATTENTION; drives verify_label.
    verify_needs_attention: bool = False
    # Prevents re-posting the findings comment on re-runs.
    verify_comment_posted: bool = False
    # Port mode frozen at unit-build time.
    mode: PortMode | None = None
    # For partially-applied groups: 0-based index of the cherry-pick step that
    # failed conflict resolution, and how many earlier picks were committed.
    failed_step_index: int | None = None
    partial_pr_count: int | None = None
    # Auto-resumes of a partially-applied group (``pr_policy.max_partial_continue_attempts``).
    partial_continue_attempts: int = 0
    build_attempts: int = 0  # build-fix attempts spent in the last verify pass
    verify_resume_attempts: int = 0  # cross-run resumes (cap: max_verify_resume_attempts)
    last_verify_error: str | None = None
    # Origin URL of a pushed ``build_failed`` branch (it has no PR).
    branch_url: str | None = None
    # Latest PRs Claude reported as missing prerequisites, and its reason.
    missing_prereq_prs: list[str] = field(default_factory=list)
    missing_prereq_note: str | None = None
    # PRs prepended by prereq auto-recovery, in cherry-pick order.
    dynamic_prereq_urls: list[str] = field(default_factory=list)
    prereq_discovery_depth: int = 0
    # One ``{at_depth, triggering_pr, discovered, reason}`` per dive, newest last.
    prereq_trail: list[dict] = field(default_factory=list)
    # A dive aborted on ``max_prereq_depth`` or a cycle.
    prereq_recovery_exhausted: bool = False
    # Units/config entries already porting a discovered prereq: ``{prereq_url,
    # queued_in, queued_in_pr_url, carried, queued_status}``.
    queued_prereq_units: list[dict] = field(default_factory=list)
    # Last successful ``refresh --address-review``; the next pass's implicit --since.
    last_review_addressed_at: str | None = None
    # Unit IDs a ``blocked`` entry waits on.
    blocked_by: list[str] = field(default_factory=list)
    # ``config.merged_label`` post-merge bookkeeping done.
    merged_label_applied: bool = False
    # Why a terminal status (skipped/closed/superseded/reverted) was set.
    skip_reason: str | None = None
    stall: StallReason | None = None
    # Why the port no longer matches its unit's membership; ``run`` re-ports it.
    outdated: str | None = None


@dataclass
class PipelineState:
    started_at: str | None = None
    onto: str | None = None
    phase: PipelinePhase = "init"
    base_branch: str | None = None
    features: dict[str, FeatureState] = field(default_factory=dict)
    # Filled by load_state / save_state.
    config_path: str | None = None
    config_path_history: list[str] = field(default_factory=list)

    def set_started(self, onto: str) -> None:
        self.started_at = datetime.now(timezone.utc).isoformat()
        self.onto = onto


def _parse_features(raw_features: dict) -> dict[str, FeatureState]:
    features: dict[str, FeatureState] = {}
    for fid, fraw in (raw_features or {}).items():
        features[fid] = FeatureState(
            status=fraw.get("status", "needs_review"),
            branch_name=fraw.get("branch_name"),
            base_commit=fraw.get("base_commit"),
            conflict_files=fraw.get("conflict_files", []) or [],
            pr_url=fraw.get("pr_url"),
            pr_number=fraw.get("pr_number"),
            pr_title=fraw.get("pr_title"),
            pr_body=fraw.get("pr_body"),
            pr_numbers=fraw.get("pr_numbers", []) or [],
            pr_urls=fraw.get("pr_urls", []) or [],
            contained_pr_urls=fraw.get("contained_pr_urls", []) or [],
            pr_author=fraw.get("pr_author"),
            rebase_pr_url=fraw.get("rebase_pr_url"),
            ai_resolved=fraw.get("ai_resolved", False),
            ai_iterations=fraw.get("ai_iterations"),
            ai_cost_usd=fraw.get("ai_cost_usd"),
            verify_needs_attention=bool(
                fraw.get("verify_needs_attention", False)
            ),
            verify_comment_posted=bool(
                fraw.get("verify_comment_posted", False)
            ),
            mode=fraw.get("mode"),
            failed_step_index=fraw.get("failed_step_index"),
            partial_pr_count=fraw.get("partial_pr_count"),
            partial_continue_attempts=int(
                fraw.get("partial_continue_attempts", 0) or 0
            ),
            build_attempts=int(fraw.get("build_attempts", 0) or 0),
            verify_resume_attempts=int(
                fraw.get("verify_resume_attempts", 0) or 0
            ),
            last_verify_error=fraw.get("last_verify_error"),
            branch_url=fraw.get("branch_url"),
            missing_prereq_prs=fraw.get("missing_prereq_prs", []) or [],
            missing_prereq_note=fraw.get("missing_prereq_note"),
            dynamic_prereq_urls=fraw.get("dynamic_prereq_urls", []) or [],
            prereq_discovery_depth=int(fraw.get("prereq_discovery_depth", 0) or 0),
            prereq_trail=list(fraw.get("prereq_trail", []) or []),
            prereq_recovery_exhausted=bool(
                fraw.get("prereq_recovery_exhausted", False)
            ),
            queued_prereq_units=list(fraw.get("queued_prereq_units", []) or []),
            last_review_addressed_at=fraw.get("last_review_addressed_at"),
            blocked_by=list(fraw.get("blocked_by", []) or []),
            merged_label_applied=bool(fraw.get("merged_label_applied", False)),
            skip_reason=fraw.get("skip_reason"),
            stall=StallReason.from_dict(fraw.get("stall")),
            outdated=fraw.get("outdated"),
        )
    return features


def _read_raw_state(path: Path) -> dict:
    if not path.exists():
        return {}
    with open(path) as f:
        raw = yaml.safe_load(f) or {}
    if not isinstance(raw, dict):
        return {}
    return raw


def load_state(config: Config) -> PipelineState:
    """Load ``config``'s project state; empty when the file doesn't exist."""
    state_path = state_file_path(config.name)
    raw = _read_raw_state(state_path)

    run = raw.get("last_run") if isinstance(raw.get("last_run"), dict) else {}
    features = _parse_features(run.get("features", {}) or {})

    phase = run.get("phase", "init")
    if phase not in ("init", "ports_done"):
        phase = "init"

    return PipelineState(
        started_at=run.get("started_at"),
        onto=run.get("onto"),
        phase=phase,
        base_branch=run.get("base_branch"),
        features=features,
        config_path=raw.get("config_path"),
        config_path_history=list(raw.get("config_path_history", []) or []),
    )


def save_state(state: PipelineState, config: Config) -> None:
    """Persist ``state``, rebinding ``config_path`` and recording the old one in history."""
    state_path = state_file_path(config.name)
    state_path.parent.mkdir(parents=True, exist_ok=True)

    current_cfg = str(config.config_path.resolve())
    history = list(state.config_path_history or [])
    if state.config_path and state.config_path != current_cfg:
        if state.config_path not in history:
            history.append(state.config_path)
        history = history[-_CONFIG_PATH_HISTORY_MAX:]
    state.config_path = current_cfg
    state.config_path_history = history

    features_data = {}
    for fid, fs in state.features.items():
        entry: dict = {"status": fs.status}
        if fs.branch_name:
            entry["branch_name"] = fs.branch_name
        if fs.base_commit:
            entry["base_commit"] = fs.base_commit
        if fs.conflict_files:
            entry["conflict_files"] = fs.conflict_files
        if fs.pr_url:
            entry["pr_url"] = fs.pr_url
        if fs.pr_number:
            entry["pr_number"] = fs.pr_number
        if fs.pr_title:
            entry["pr_title"] = fs.pr_title
        if fs.pr_body:
            entry["pr_body"] = fs.pr_body
        if fs.pr_numbers and len(fs.pr_numbers) > 1:
            entry["pr_numbers"] = fs.pr_numbers
        if fs.pr_urls and len(fs.pr_urls) > 1:
            entry["pr_urls"] = fs.pr_urls
        if fs.contained_pr_urls:
            entry["contained_pr_urls"] = fs.contained_pr_urls
        if fs.pr_author:
            entry["pr_author"] = fs.pr_author
        if fs.rebase_pr_url:
            entry["rebase_pr_url"] = fs.rebase_pr_url
        if fs.ai_resolved:
            entry["ai_resolved"] = True
        if fs.ai_iterations is not None:
            entry["ai_iterations"] = fs.ai_iterations
        if fs.ai_cost_usd is not None:
            entry["ai_cost_usd"] = float(fs.ai_cost_usd)
        if fs.verify_needs_attention:
            entry["verify_needs_attention"] = True
        if fs.verify_comment_posted:
            entry["verify_comment_posted"] = True
        if fs.mode:
            entry["mode"] = fs.mode
        if fs.failed_step_index is not None:
            entry["failed_step_index"] = fs.failed_step_index
        if fs.partial_pr_count is not None:
            entry["partial_pr_count"] = fs.partial_pr_count
        if fs.partial_continue_attempts:
            entry["partial_continue_attempts"] = fs.partial_continue_attempts
        if fs.build_attempts:
            entry["build_attempts"] = fs.build_attempts
        if fs.verify_resume_attempts:
            entry["verify_resume_attempts"] = fs.verify_resume_attempts
        if fs.last_verify_error:
            entry["last_verify_error"] = fs.last_verify_error
        if fs.branch_url:
            entry["branch_url"] = fs.branch_url
        if fs.missing_prereq_prs:
            entry["missing_prereq_prs"] = fs.missing_prereq_prs
        if fs.missing_prereq_note:
            entry["missing_prereq_note"] = fs.missing_prereq_note
        if fs.dynamic_prereq_urls:
            entry["dynamic_prereq_urls"] = fs.dynamic_prereq_urls
        if fs.prereq_discovery_depth:
            entry["prereq_discovery_depth"] = fs.prereq_discovery_depth
        if fs.prereq_trail:
            entry["prereq_trail"] = fs.prereq_trail
        if fs.prereq_recovery_exhausted:
            entry["prereq_recovery_exhausted"] = True
        if fs.queued_prereq_units:
            entry["queued_prereq_units"] = fs.queued_prereq_units
        if fs.last_review_addressed_at:
            entry["last_review_addressed_at"] = fs.last_review_addressed_at
        if fs.blocked_by:
            entry["blocked_by"] = list(fs.blocked_by)
        if fs.merged_label_applied:
            entry["merged_label_applied"] = True
        if fs.skip_reason:
            entry["skip_reason"] = fs.skip_reason
        if fs.stall is not None:
            entry["stall"] = fs.stall.to_dict()
        if fs.outdated:
            entry["outdated"] = fs.outdated
        features_data[fid] = entry

    data: dict = {
        "name": config.name,
        "config_path": current_cfg,
    }
    if history:
        data["config_path_history"] = history
    data["last_run"] = {
        "started_at": state.started_at,
        "onto": state.onto,
        "phase": state.phase,
        "base_branch": state.base_branch,
        "features": features_data,
    }

    with open(state_path, "w") as f:
        yaml.dump(data, f, default_flow_style=False, sort_keys=False)


def verify_ownership(config: Config) -> None:
    """Raise :class:`OwnershipCollisionError` if state belongs to a different config."""
    state_path = state_file_path(config.name)
    raw = _read_raw_state(state_path)
    stored = raw.get("config_path")
    if not stored:
        return
    loaded_resolved = config.config_path.resolve()
    try:
        stored_resolved = Path(stored).resolve()
    except (OSError, RuntimeError):
        return
    if stored_resolved == loaded_resolved:
        return
    raise OwnershipCollisionError(
        name=config.name,
        state_path=state_path,
        loaded_config=loaded_resolved,
        stored_config=stored_resolved,
    )


def adopt_ownership(config: Config) -> tuple[Path | None, Path]:
    """Rebind the state file to the current config; return ``(previous_config, state_path)``."""
    state_path = state_file_path(config.name)
    state = load_state(config)
    previous: Path | None = None
    if state.config_path:
        try:
            prev = Path(state.config_path).resolve()
        except (OSError, RuntimeError):
            prev = None
        if prev and prev != config.config_path.resolve():
            previous = prev
    save_state(state, config)
    return previous, state_path


def find_feature_by_pr_url(
    state: PipelineState, pr_url: str,
) -> tuple[str, FeatureState] | None:
    """First tracked feature whose source or rebase URL matches ``pr_url``."""
    matches = find_features_by_pr_url(state, pr_url)
    return matches[0] if matches else None


def find_features_by_pr_url(
    state: PipelineState, pr_url: str,
) -> list[tuple[str, FeatureState]]:
    """Every tracked feature whose source or rebase URL matches ``pr_url``."""
    # Deferred: github_ops imports this module.
    from releasy.github_ops import parse_pr_url

    target = parse_pr_url(pr_url)
    if target is None:
        return []
    return [
        (fid, fs)
        for fid, fs in state.features.items()
        if any(
            url and parse_pr_url(url) == target
            for url in (fs.rebase_pr_url, fs.pr_url, *fs.pr_urls)
        )
    ]


# In-flight statuses an ``outdated`` mark can rebuild.
OUTDATABLE_STATUSES: frozenset[str] = frozenset({
    "needs_review", "branch_created", "conflict", "build_failed", "blocked",
})


def mark_outdated(fs: FeatureState, reason: str) -> bool:
    """Mark an in-flight port outdated. True when the mark was set."""
    if fs.status not in OUTDATABLE_STATUSES or fs.outdated:
        return False
    fs.outdated = reason
    return True


def find_merged_feature_for_prs(
    state: PipelineState,
    pr_urls: list[str],
    *,
    exclude_feature_id: str | None = None,
) -> tuple[str, FeatureState] | None:
    """A ``merged`` feature, other than ``exclude_feature_id``, sourcing all ``pr_urls``."""
    from releasy.github_ops import parse_pr_url

    wanted = {parse_pr_url(u) for u in pr_urls}
    if not wanted or None in wanted:
        return None
    for fid, fs in state.features.items():
        if fid == exclude_feature_id or fs.status != "merged":
            continue
        sources = {
            parse_pr_url(u) for u in (fs.pr_urls or [fs.pr_url]) if u
        }
        if wanted <= sources:
            return (fid, fs)
    return None
