"""Pipeline state management — read/write per-project state files.

State no longer lives in the user's repo dir. Each project (identified
by ``Config.name``) gets its own state file under
``state_root() / "<name>.state.yaml"`` (XDG state location by default,
overridable via ``$RELEASY_STATE_DIR``).

The state file additionally carries the absolute ``config_path`` of the
config that owns it, so we can:

  * surface a friendly listing in ``releasy list``,
  * detect "wait, this state belongs to a different config.yaml"
    collisions when somebody copies a config without changing ``name:``.

Use :func:`verify_ownership` before mutating; use :func:`adopt_ownership`
to forcibly rebind state to the current config (the ``releasy adopt``
command).
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
    # Resolved on a LOCAL branch (no PR), but RelEasy's build/tests didn't
    # pass this run; the next ``releasy run`` resumes the fix-loop on it.
    "build_failed",
    "skipped",
    "merged",
    "blocked",
    # Terminal: rebase PR was closed without merging on GitHub. Detected by
    # the refresh / run merge-status sweep; treated like ``skipped`` for the
    # refresh loop (no work, no monitoring) but kept distinct in the status
    # output and the project board so the user can see WHY it's terminal.
    # ``pr_policy.recreate_closed_prs`` is the only thing that lets a
    # ``closed`` entry get a new chance — via a renumbered port branch.
    "closed",
    # Terminal: another PR (open or merged) targeting the same base branch
    # has already cherry-picked the entry's source PR(s) — there is no
    # remaining work to do. Detected by the supersede sweep, which walks
    # the target branch's recent history (for merged supersedes) and open
    # PRs (for in-flight supersedes) for ``(cherry picked from commit
    # <sha>)`` footers citing our source SHAs. Like ``closed``, this is
    # gated by ``pr_policy.detect_superseded``.
    "superseded",
]
PipelinePhase = Literal["init", "ports_done"]


# ---------------------------------------------------------------------------
# Stall reasons — why a port stopped short of a mergeable PR
# ---------------------------------------------------------------------------
#
# ``status`` says WHAT a unit is (conflict / build_failed / …); a stall says
# WHY it got stuck and whether re-running could possibly change that. The
# generic ``kind`` is what code branches on; ``detail`` and the ``waiting_on_*``
# lists carry the specifics a human needs to read.

StallKind = Literal[
    # A prerequisite is already queued in another releasy unit — nothing to
    # try until that unit's PR merges into the base branch.
    "waiting_for_merge",
    # A prerequisite PR was identified but is not ported anywhere: the user
    # has to add it to the session (or merge it upstream) first.
    "missing_prereq",
    # An attempt cap was hit (auto-continue / build-resume). Only a config
    # bump or a manual fix moves this.
    "retries_exhausted",
    # The resolver judged the conflict and could not fix it.
    "unresolvable",
    # The auto-prereq dive stopped on its depth cap, a cycle, or a fetch
    # failure — the search itself ran out, not the conflict.
    "prereq_search_exhausted",
    # No verdict was reached: AI resolution is off, or the backend died.
    "resolver_unavailable",
    # The resolution landed but the build / tests never went green.
    "build_unfixed",
]

# Stalls that no amount of re-running fixes on its own: the thing being
# waited for lives outside the unit. ``run`` skips these instead of paying
# for a resolution that can only reach the same verdict — see
# :func:`releasy.pipeline._stall_still_blocks`. ``retries_exhausted`` is
# deliberately absent: the attempt caps are re-read from config on every
# run, so raising one has to take effect immediately.
BLOCKING_STALL_KINDS: frozenset[str] = frozenset(
    {"waiting_for_merge", "missing_prereq"}
)

# Generic one-liner per kind. ``{targets}`` is filled from waiting_on_*.
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
    """``#N`` for a PR URL, the URL itself when it doesn't parse."""
    m = _STALL_PR_NUMBER_RE.search(url or "")
    return f"#{m.group(1)}" if m else (url or "?")


@dataclass
class StallReason:
    """Why a unit is parked, in a form both code and humans can read."""

    kind: StallKind
    # Short free-form specifics ("3/3 attempts", "depth 2/2", a build error).
    detail: str = ""
    # Tracked feature IDs whose port PR must merge before a retry is useful.
    waiting_on_units: list[str] = field(default_factory=list)
    # Source PR URLs that must land (merged upstream, or ported by releasy).
    waiting_on_prs: list[str] = field(default_factory=list)
    # ISO-8601 UTC of the run that first recorded this stall, and how many
    # consecutive runs have ended in it. Both survive re-records of the same
    # stall so `releasy status` can show "stuck here since …".
    since: str | None = None
    runs: int = 1

    def targets(self) -> str:
        """What this stall waits on, named as briefly as possible.

        Units win over PRs: when a prereq is queued in another unit, that
        unit's ID is the thing the reader acts on — repeating the source PR
        it carries just makes the line longer.
        """
        bits = (
            [f"`{u}`" for u in self.waiting_on_units] if self.waiting_on_units
            else [_pr_ref(u) for u in self.waiting_on_prs]
        )
        return ", ".join(bits) or "an external change"

    def summary(self, *, max_detail: int = 90) -> str:
        """One short line: the generic label plus its specifics."""
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
    """Retire the bookkeeping of a conflict that no longer applies.

    Call this wherever an entry leaves ``conflict`` for a status that
    describes finished (or abandoned) work — merged, closed, superseded,
    skipped, resolved. The stall goes with it: a merged unit that still
    claims to be waiting for something reads as a bug in every report.
    """
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
    """Build a :class:`StallReason`, ageing it against ``prior``'s stall.

    Re-recording the *same* stall keeps its original ``since`` and bumps
    ``runs``; a different one starts fresh. Pass the feature's pre-existing
    state as ``prior`` — most pipeline exit paths build a new
    :class:`FeatureState` from scratch, so the age would otherwise reset on
    every run.
    """
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


# Order in which status groups are shown to humans (``releasy status``
# sub-tables, ``releasy list`` summary). Highest-attention first.
STATUS_DISPLAY_ORDER: tuple[str, ...] = (
    "conflict",
    "build_failed",
    "blocked",
    "branch_created",
    "needs_review",
    "skipped",
    "closed",
    "superseded",
    "merged",
)


# Most recent ``config_path`` history entries to keep in the state file.
# Trimmed to a small window — the field is mostly an audit trail for
# users who move a config repeatedly; nobody needs the full history.
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
    # Source PR meta. For singleton features, the *_url / *_number / *_title
    # fields hold the one and only PR. For sequential PR groups, they hold
    # the FIRST PR (for backward-compat with display code), and the
    # ``pr_numbers`` / ``pr_urls`` lists hold every PR in cherry-pick order.
    pr_url: str | None = None
    pr_number: int | None = None
    pr_title: str | None = None
    pr_body: str | None = None
    pr_numbers: list[int] = field(default_factory=list)
    pr_urls: list[str] = field(default_factory=list)
    # Source PRs the unit's own PRs say they cherry-picked (parsed from
    # their ``Cherry-picked from …`` bodies). A combined port carries code
    # from PRs that appear nowhere in ``pr_urls``; recording them lets the
    # queued-elsewhere guard see that this unit already brings a prereq.
    contained_pr_urls: list[str] = field(default_factory=list)
    # GitHub login of the (first) source PR's author. Used by the project
    # board sync to seed the ``Assignee Dev`` field once, when the card is
    # first created. Stored on state so re-runs and ``releasy continue``
    # can rebuild the board without re-fetching every PR from GitHub.
    pr_author: str | None = None
    rebase_pr_url: str | None = None  # auto-created PR targeting base branch
    ai_resolved: bool = False
    ai_iterations: int | None = None
    # Cumulative USD cost reported by Claude across every resolve
    # invocation that touched this entry (cherry-pick steps + later
    # ``releasy refresh`` merges). ``None`` means we have no cost data
    # for this entry — either AI never ran, or Claude didn't report a
    # cost. Synced to the GitHub Project board's "AI Cost" number field.
    ai_cost_usd: float | None = None
    # Set iff the verifier returned NEEDS_ATTENTION; drives verify_label.
    verify_needs_attention: bool = False
    # Set once the findings comment was posted, to prevent re-post on
    # ``releasy continue`` re-runs (label is idempotent, comment isn't).
    verify_comment_posted: bool = False
    # Frozen at unit-build time so ``releasy refresh`` honours per-source
    # mode overrides without re-running the detection ladder. ``None``
    # for pre-existing state entries — refresh falls back to the ladder.
    mode: PortMode | None = None
    # For partially-applied groups: 0-based index of the cherry-pick step that
    # failed conflict resolution, and how many earlier picks were committed.
    failed_step_index: int | None = None
    partial_pr_count: int | None = None
    # How many times ``releasy run`` has auto-resumed this partially-applied
    # group (bounded by ``pr_policy.max_partial_continue_attempts``). Reset to
    # 0 once the group finally lands clean.
    partial_continue_attempts: int = 0
    # ----- Deterministic build/test verification state (build_failed) -----
    build_attempts: int = 0  # build-fix attempts spent in the last verify pass
    verify_resume_attempts: int = 0  # cross-run resumes (cap: max_verify_resume_attempts)
    last_verify_error: str | None = None
    # ----- Missing-prerequisite detection / auto-recovery state -----
    # Populated by the AI resolver when Claude judges the conflict to be
    # caused by an unported upstream PR. Even in detection-only mode (no
    # auto-recovery), these fields persist on the feature so the project
    # board card and re-runs can surface the trail.
    #
    # ``missing_prereq_prs`` — most recent set of PR URLs Claude reported
    # as the missing foundation (cleared once the unit lands cleanly).
    # ``missing_prereq_note`` — Claude's one-line REASON.
    missing_prereq_prs: list[str] = field(default_factory=list)
    missing_prereq_note: str | None = None
    # Auto-recovery bookkeeping (irrelevant when
    # ``ai_resolve.auto_add_prerequisite_prs.enabled`` is false):
    # ``dynamic_prereq_urls`` — PRs that were prepended to the unit by
    # the auto-recovery loop, in cherry-pick order. Empty when no dives
    # happened. Survives ``releasy continue`` so a re-run resumes with
    # the expanded unit shape rather than the original config-listed
    # PRs only.
    dynamic_prereq_urls: list[str] = field(default_factory=list)
    # ``prereq_discovery_depth`` — number of times the detection fired
    # recursively on this unit. PR_A → PR_B → PR_C is depth 2.
    prereq_discovery_depth: int = 0
    # ``prereq_trail`` — audit log of every dive, used to render the
    # dependency trail in stdout / project board / PR body. Each entry is
    # ``{at_depth: int, triggering_pr: url, discovered: [urls],
    # reason: str}``; the most recent dive is last.
    prereq_trail: list[dict] = field(default_factory=list)
    # ``prereq_recovery_exhausted`` — True iff a dive was aborted because
    # ``max_prereq_depth`` was exceeded or a cycle was detected. Selects
    # the "exhausted" body / label / message variant in reporting code.
    prereq_recovery_exhausted: bool = False
    # ``queued_prereq_units`` — cross-references to other units (or
    # config entries) where the discovered prereq is already going to be
    # ported. Each entry is ``{prereq_url: str, queued_in: str,
    # queued_in_pr_url: str | None, carried: bool}`` where ``queued_in``
    # is a human-readable identifier (feature_id, "config:include_prs",
    # "config:groups[<id>]") and ``carried`` marks a unit that brings the
    # prereq inside a combined port rather than listing it. Drives the
    # "merge unit X first" message; cleared once the unit lands cleanly.
    queued_prereq_units: list[dict] = field(default_factory=list)
    # ----- ``refresh --address-review`` tracking -----
    # ISO-8601 UTC timestamp of the most recent successful
    # ``refresh --address-review`` run on this feature's rebase PR.
    # When present, the next address-review pass on the same PR uses
    # it as an implicit (exclusive) --since default so re-runs only
    # consider comments posted after the last pass. Opportunistic:
    # stateless runs (PR not tracked here) simply don't read or write
    # this field.
    last_review_addressed_at: str | None = None
    # ----- Sequential-gating state (depends_on) -----
    # When ``status == "blocked"``, the unit IDs this entry is waiting on.
    # Each entry is the ``feature_id`` of another tracked unit (group ID
    # for groups, ``pr-<N>`` / ``<owner>-<repo>-pr-<N>`` for singletons).
    # Cleared once the unit unblocks and starts processing.
    blocked_by: list[str] = field(default_factory=list)
    # Set once the ``config.merged_label`` post-merge bookkeeping has run
    # for this unit (label applied to the rebase PR, stripped from source
    # PRs hosted on origin). Idempotent flag — avoids re-hitting GitHub on
    # every subsequent ``releasy run``. Stays ``False`` when
    # ``merged_label`` is unset in config.
    merged_label_applied: bool = False
    # Free-form one-line explanation of why ``status == "skipped"``.
    # Written by the pipeline (e.g. "already in target — empty cherry-pick")
    # and surfaced by ``releasy status``. ``None`` for skips that predate
    # the field or were applied without a reason.
    skip_reason: str | None = None
    # Why this unit stopped short of a mergeable PR (see :class:`StallReason`).
    # Set on every non-clean exit path, cleared once the unit lands. Read by
    # the run gate (skip a retry that cannot succeed yet), `releasy status`
    # and the graph issue.
    stall: StallReason | None = None


@dataclass
class PipelineState:
    started_at: str | None = None
    onto: str | None = None
    phase: PipelinePhase = "init"
    base_branch: str | None = None
    features: dict[str, FeatureState] = field(default_factory=dict)
    # Provenance (filled by load_state / save_state, not user-visible config):
    config_path: str | None = None
    config_path_history: list[str] = field(default_factory=list)

    def set_started(self, onto: str) -> None:
        self.started_at = datetime.now(timezone.utc).isoformat()
        self.onto = onto

    def all_features_ok(self) -> bool:
        return all(
            fs.status == "needs_review"
            for fs in self.features.values()
        )


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
        )
    return features


def _read_raw_state(path: Path) -> dict:
    """Read ``path`` as a state-file dict, returning ``{}`` if missing/empty."""
    if not path.exists():
        return {}
    with open(path) as f:
        raw = yaml.safe_load(f) or {}
    if not isinstance(raw, dict):
        return {}
    return raw


def load_state(config: Config) -> PipelineState:
    """Load the pipeline state for ``config``'s project.

    Returns an empty :class:`PipelineState` (with provenance fields filled
    from the config) when the state file does not exist yet — matches the
    "first run" case so callers don't need to special-case it.
    """
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
    """Persist ``state`` to ``config``'s per-project state file.

    Always rewrites ``config_path`` to the loaded config's absolute
    location and appends to ``config_path_history`` if it changed.
    """
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
    """Raise :class:`OwnershipCollisionError` if state belongs to a different config.

    No-op when:

    * the state file does not yet exist (first-time run),
    * the file exists but carries no ``config_path`` (legacy or hand-edited),
    * the stored ``config_path`` matches the loaded config's path.
    """
    state_path = state_file_path(config.name)
    raw = _read_raw_state(state_path)
    stored = raw.get("config_path")
    if not stored:
        return
    loaded_resolved = config.config_path.resolve()
    try:
        stored_resolved = Path(stored).resolve()
    except (OSError, RuntimeError):
        # If the stored path can no longer be resolved (deleted, missing
        # mount, …) there's no meaningful collision to flag — treat the
        # current config as the new owner.
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
    """Forcibly rebind the state file's ``config_path`` to the current config.

    Returns ``(previous_config_path, new_config_path)``. ``previous`` is
    ``None`` when there was no state file yet (creates a fresh one) or
    when the file already pointed at the current config.
    """
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
    """Locate the tracked feature whose source or rebase URL matches ``pr_url``.

    Compares on ``(owner, repo, number)`` so cosmetic differences (trailing
    slash, fragment, ``.git`` suffix) don't break the match. Returns
    ``(feature_id, FeatureState)`` on hit, ``None`` otherwise.

    Used by ``releasy pr remove`` to find the entry to purge and by
    ``refresh._pr_url_in_state_scope`` to scope-gate URL-driven flows.
    The import of ``parse_pr_url`` is deferred — ``github_ops`` already
    imports this module, so a top-level import here would loop.
    """
    from releasy.github_ops import parse_pr_url

    target = parse_pr_url(pr_url)
    if target is None:
        return None
    if not state.features:
        return None
    for fid, fs in state.features.items():
        for url in (fs.rebase_pr_url, fs.pr_url, *fs.pr_urls):
            if not url:
                continue
            if parse_pr_url(url) == target:
                return (fid, fs)
    return None
