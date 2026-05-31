"""Auto-discover a PR dependency DAG and emit a recommended grouping.

The engine behind the ``releasy graph discover`` command:

1. Walks the candidate PR set defined by ``config.pr_sources``, treating
   each user-declared group as a single super-node.
2. Excludes units already merged into the target branch (state.yaml +
   ``Source-PR:`` trailers + ``git cherry``).
3. Trial-cherry-picks each remaining unit onto the target tip in a
   scratch git worktree (``git worktree add --detach``).
4. On a clean pick, emits the unit as a leaf in the DAG.
5. On conflict, looks up older un-ported units that touched the
   conflicting files (via ``git log target..source -- <file>`` mapped
   through merge-commit / Source-PR: trailer / merge-containment rules),
   then optionally hands the candidates to Claude to confirm.
6. After all units are processed, computes weakly-connected components
   → the recommended groups, with articulation points called out as
   ``recommend_first``.

The command is read-only: it never writes ``state.yaml`` and never
touches the main worktree. By default it also writes a deps overlay
to ``<session-stem>.deps.yaml`` next to the session file (override via
``pr_sources.deps_file:`` in the session) — the session loader merges
that file's ``groups[]`` into ``pr_sources.groups`` on the next
``releasy run``. Pass ``--no-write`` to skip the overlay write
(preview mode), or ``--deps-file <path>`` to redirect it to a one-off
path. The main session file is never modified.
"""

from __future__ import annotations

import atexit
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import yaml

from releasy.ai_resolve import (
    AIResolveContext,
    _MISSING_PREREQS_RE,
    _parse_missing_prereqs,
    attempt_ai_resolve,
    synthesize_text,
)
from releasy.config import Config
from releasy.git_ops import (
    abort_in_progress_op,
    append_commit_trailer,
    cherry_pick_merge_commit,
    ensure_work_repo,
    fetch_remote,
    get_conflict_files,
    is_operation_in_progress,
    run_git,
)
from releasy.github_ops import PRInfo, get_origin_repo_slug
from releasy.pipeline import (
    FeatureUnit,
    _SOURCE_PR_URL_RE,
    discover_feature_units,
)
from releasy.state import PipelineState, load_state
from releasy.termlog import get_console

console = get_console()


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------


@dataclass
class _PickOutcome:
    clean: bool
    conflict_files: list[str]
    error_message: str | None = None
    # Index into ``unit.feature_unit.prs`` of the PR whose cherry-pick
    # failed. ``None`` for clean outcomes or for failures that didn't
    # reach a real cherry-pick (e.g. a PR with no merge_commit_sha).
    conflicting_pr_idx: int | None = None
    # Name of the local branch the cache was attempted on. Always set;
    # the caller decides whether to keep or delete it based on outcome
    # and AI fallback result.
    cache_branch: str | None = None


@dataclass
class _CandidateUnit:
    """A unit (singleton or user-declared group) under consideration.

    Wraps a :class:`FeatureUnit` with bookkeeping fields that only matter
    during dep discovery (e.g. earliest merge timestamp for the latest →
    oldest queue order).
    """
    unit_id: str
    is_user_group: bool
    prs: list[PRInfo]
    earliest_merged_at: str | None
    feature_unit: FeatureUnit  # Backing FeatureUnit, used for cherry-pick order


@dataclass
class DAGNode:
    unit_id: str
    is_user_group: bool
    pr_urls: list[str]
    pr_titles: list[str]
    earliest_merged_at: str | None
    deps: list[str]
    # "trial-clean" | "git-graph" | "git-graph+claude" |
    # "ai-resolve" | "ai-resolve-clean" | "depth-cutoff"
    discovery_method: str
    conflict_files_at_discovery: list[str] = field(default_factory=list)
    # ``True`` iff a local port branch was preserved at
    # ``feature/<base>/<unit_id>`` for ``releasy run`` to reuse.
    # ``True`` for trial-clean and AI-resolved outcomes. ``False`` for
    # conflict-with-empty-deps (we couldn't resolve), refinement-only
    # (no resolution attempted), or depth-cutoff. The presence of the
    # branch lets ``run`` skip the cherry-pick step entirely.
    cached: bool = False


@dataclass
class DAGComponent:
    component_id: str
    unit_ids: list[str]
    recommend_first: list[str]
    edges: list[tuple[str, str]]


@dataclass
class DiscoveryReport:
    base_branch: str
    target_sha: str
    generated_at: str
    candidate_unit_count: int
    # Total PRs across all candidate units, after group-claim dedup.
    # ``candidate_unit_count`` is the unit count (where a user-declared
    # group is one super-node); this is the underlying PR count so the
    # summary can show e.g. "26 PRs across 15 units" — answering the
    # question "where did the 26 PRs from `by_labels` go?" without the
    # reader having to re-do the arithmetic.
    candidate_pr_count: int
    skipped_already_in_target: list[str]
    nodes: list[DAGNode]
    components: list[DAGComponent]
    singletons: list[str]
    warnings: list[str] = field(default_factory=list)
    # Diff of auto-discovered unit IDs between this run and the existing
    # deps overlay file (if one was found at ``deps_overlay_path``).
    # Populated only when there's an existing file to compare against
    # AND the diff is non-empty. ``removed_since_last_run`` typically
    # means "landed in target since last run"; ``added_since_last_run``
    # means "newly discovered candidates / dependencies".
    refresh_removed: list[str] = field(default_factory=list)
    refresh_added: list[str] = field(default_factory=list)
    # GitHub issue this graph was posted to via ``releasy graph discover
    # --open-issue`` (``None`` until posted). ``last_ingested_at`` is the
    # ``created_at`` of the newest issue comment already folded in by
    # ``releasy graph update`` — used as the default ``--since`` so a
    # re-run only considers comments posted after the last ingest.
    issue_number: int | None = None
    issue_url: str | None = None
    last_ingested_at: str | None = None


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def run_discover_deps(
    config: Config,
    onto: str | None,
    work_dir: Path | None,
    *,
    output_path: Path | None,
    deps_overlay_path: Path | None,
    use_ai: bool,
    max_depth: int,
    pr_limit: int | None,
    include_already_merged: bool,
) -> DiscoveryReport:
    """Run dep discovery and write the report (and optionally the sidecar).

    Returns the in-memory :class:`DiscoveryReport`. The caller is expected
    to print a summary; the YAML output(s) are written here as a side effect.
    """
    # --- Resolve target branch + scratch worktree ---
    if onto:
        base_branch = config.base_branch_name(onto)
    elif config.target_branch:
        base_branch = config.target_branch
    else:
        raise ValueError(
            "graph discover: cannot resolve base branch — pass --onto or set "
            "target_branch in config.yaml."
        )

    wd = config.resolve_work_dir(work_dir)
    repo_path, _ = ensure_work_repo(config, wd)
    if is_operation_in_progress(repo_path):
        raise RuntimeError(
            f"main repo {repo_path} has an in-progress git op (cherry-pick "
            f"/ merge / rebase) — finish or abort it first, then re-run "
            "graph discover."
        )
    # Scratch parent is always the user-blessed work_dir. Plan §6:
    # ``<work_dir>/.releasy-discover-deps-<short_id>``. We deliberately
    # don't use ``repo_path.parent`` because ``ensure_work_repo`` returns
    # ``repo_path == work_dir`` when work_dir already has a ``.git``, in
    # which case ``repo_path.parent`` would be the user's home directory.
    scratch_parent = wd
    scratch_parent.mkdir(parents=True, exist_ok=True)

    remote = config.origin.remote_name
    # Broad fetch first — pulls history needed to classify conflict
    # commits (origin/master, PR merge SHAs, etc.).
    console.print(f"  [dim]Fetching {remote}...[/dim]")
    fetch_remote(repo_path, remote)
    # Then an explicit targeted fetch of the target branch. Two
    # benefits over relying on the broad fetch alone: (1) fails fast
    # with a clear message if ``base_branch`` doesn't exist on origin
    # — the alternative is silently resolving an empty SHA later;
    # (2) guarantees freshness even if origin's refspec is unusual.
    console.print(
        f"  [dim]Fetching latest [cyan]{base_branch}[/cyan] from {remote}...[/dim]"
    )
    target_fetch = run_git(
        ["fetch", remote, base_branch], repo_path, check=False,
    )
    if target_fetch.returncode != 0:
        err = (target_fetch.stderr or "").strip() or "fetch failed"
        raise RuntimeError(
            f"target branch {base_branch!r} not found on remote "
            f"{remote!r}: {err}. Verify the branch exists on the "
            "configured origin and re-run."
        )
    target_ref = f"{remote}/{base_branch}"
    target_sha = _resolve_sha(repo_path, target_ref)
    if not target_sha:
        raise RuntimeError(
            f"could not resolve {target_ref!r} after fetch — the local "
            "object database is in an unexpected state."
        )

    # Caching is enabled iff we're also writing the deps overlay file —
    # i.e. NOT in ``--no-write`` mode. ``--no-write`` is "true dry-run":
    # no deps file, no cache branches, no persistent state changes
    # beyond the diagnostic report.
    cache_enabled = deps_overlay_path is not None
    origin_slug = get_origin_repo_slug(config)

    # Capture the auto-discovered unit IDs from the existing overlay (if
    # any) so we can show a refresh diff after the new overlay is built.
    # ``deps_overlay_path`` is None in --no-write mode; in that case we
    # skip the diff (nothing's being rewritten anyway).
    previous_auto_unit_ids: set[str] = (
        _read_previous_overlay_auto_ids(deps_overlay_path)
        if deps_overlay_path is not None else set()
    )

    # --- Build candidate units ---
    units = discover_feature_units(config)
    candidates = _build_candidate_unit_set(units, config)
    if pr_limit is not None and len(candidates) > pr_limit:
        candidates = candidates[-pr_limit:]  # most-recent N (sorted newest-last)

    warnings_acc: list[str] = []
    candidate_pr_urls = {p.url for cu in candidates for p in cu.prs}

    # --- Detect already-merged units ---
    state = load_state(config)
    state_already = _state_already_in_target(candidates, state)
    trailer_already = _trailer_scan(repo_path, target_ref, candidate_pr_urls)
    cherry_already = _git_cherry_already(
        repo_path, target_ref, candidates, warnings_acc,
    )
    pr_in_target: set[str] = state_already | trailer_already | cherry_already

    fully_merged_units: set[str] = set()
    for cu in candidates:
        if all(p.url in pr_in_target for p in cu.prs):
            fully_merged_units.add(cu.unit_id)

    # ``include_already_merged`` only changes the *report* (already-merged
    # units are appended as zero-edge nodes near the end of the function);
    # the trial-pick traversal always operates on the active set, since
    # there is nothing to learn from re-picking an already-applied PR.
    active_for_traversal = [
        cu for cu in candidates if cu.unit_id not in fully_merged_units
    ]

    # Build pr_url → unit_id and merge_sha → unit_id indices for unit
    # projection in the conflict-mapping step. Filter merge SHAs to those
    # actually present in the local object DB — cross-repo PRs from
    # ``include_prs`` typically aren't fetched, and including their SHAs
    # in ``git log --not target_ref <shas...>`` makes git error out and
    # drop the whole file's classification.
    pr_url_to_unit: dict[str, str] = {}
    merge_sha_to_unit: dict[str, str] = {}
    skipped_remote_sha: list[str] = []
    for cu in candidates:
        for p in cu.prs:
            pr_url_to_unit[p.url] = cu.unit_id
            if not p.merge_commit_sha:
                continue
            chk = run_git(
                ["cat-file", "-e", p.merge_commit_sha],
                repo_path, check=False,
            )
            if chk.returncode == 0:
                merge_sha_to_unit[p.merge_commit_sha] = cu.unit_id
            else:
                skipped_remote_sha.append(p.url)
    if skipped_remote_sha:
        warnings_acc.append(
            f"{len(skipped_remote_sha)} PR merge commit(s) not present "
            "locally (cross-repo / unfetched); excluded from conflict "
            "classification — these units will only appear as candidate "
            "deps when their unit_id is referenced directly"
        )

    # --- Run trial picks in scratch worktree ---
    nodes: dict[str, DAGNode] = {}
    edges: set[tuple[str, str]] = set()
    by_unit_id: dict[str, _CandidateUnit] = {cu.unit_id: cu for cu in candidates}
    merge_containment_cache: dict[str, str] | None = None

    scratch = _open_scratch_worktree(repo_path, scratch_parent, target_ref)
    try:
        # Process latest-merged-at first (descending). Recursion into older
        # candidates is pushed onto the queue.
        queue: list[tuple[str, int]] = [
            (cu.unit_id, 0)
            for cu in sorted(
                active_for_traversal,
                key=lambda c: (c.earliest_merged_at or "0000", c.prs[0].number),
                reverse=True,
            )
        ]

        while queue:
            unit_id, depth = queue.pop(0)
            if unit_id in nodes:
                continue
            cu = by_unit_id.get(unit_id)
            if cu is None:
                # An edge pointed at a unit that isn't in the candidate set
                # (or is fully merged). Caller's filtering should have
                # prevented this; warn and continue.
                warnings_acc.append(
                    f"unit {unit_id!r} referenced as a dep but not in candidate set; skipping"
                )
                continue
            if depth > max_depth:
                warnings_acc.append(
                    f"unit {unit_id!r} hit max-depth={max_depth}; transitive "
                    "deps may be incomplete"
                )
                # Still record the node so edges pointing at it resolve.
                # Use a distinct ``discovery_method`` so the YAML reader
                # can tell "we never trial-picked this" from "we picked
                # it and it was clean".
                nodes[unit_id] = _make_node(
                    cu, deps=[], method="depth-cutoff",
                    conflict_files=[],
                )
                continue

            # Cache branch path: when caching is enabled (the default),
            # trial-pick onto a named branch ``feature/<base>/<unit_id>``
            # so a successful pick is preserved for ``releasy run`` to
            # reuse. When caching is disabled (``--no-write``), the
            # trial runs detached and always resets — pure dry-run.
            cache_branch = (
                _cache_branch_name(base_branch, unit_id)
                if cache_enabled else None
            )
            outcome = _trial_pick_unit(
                scratch, cu, target_ref,
                cache_branch=cache_branch,
                is_group=cu.is_user_group,
                origin_slug=origin_slug,
            )
            cache_kept = False  # decided below

            if outcome.clean:
                # Trial-clean: keep the cache branch (it carries the
                # cherry-pick at target_ref tip, ready for ``run``).
                cache_kept = bool(cache_branch)
                if cache_branch:
                    _release_cache_branch(
                        scratch, target_ref, cache_branch, keep=True,
                    )
                nodes[unit_id] = _make_node(
                    cu, deps=[], method="trial-clean", conflict_files=[],
                    cached=cache_kept,
                )
                continue

            if outcome.error_message and not outcome.conflict_files:
                warnings_acc.append(
                    f"unit {unit_id!r}: trial pick failed without "
                    f"conflict files: {outcome.error_message}"
                )

            # --- Conflict path ---
            # Worktree is on cache_branch in conflict state (when caching)
            # OR detached at target_ref already-reset (when --no-write).
            # Either way we can compute the deterministic candidate-deps
            # via ``git log``, which doesn't depend on worktree state.
            if merge_containment_cache is None:
                merge_containment_cache = _build_merge_containment_map(
                    repo_path, target_ref, candidates, warnings_acc,
                )
            cand_dep_unit_ids = _candidate_deps_for_conflict(
                scratch, target_ref, outcome.conflict_files,
                candidate_merge_shas=list(merge_sha_to_unit.keys()),
                merge_sha_to_unit=merge_sha_to_unit,
                pr_url_to_unit=pr_url_to_unit,
                merge_containment=merge_containment_cache,
                exclude_unit_ids={unit_id},
                already_in_target_units=fully_merged_units,
            )

            method = "git-graph"
            ai_path_invoked = False

            if use_ai:
                if cand_dep_unit_ids:
                    # Refinement path: deterministic gave candidates,
                    # confirm them via lightweight Claude call. We
                    # never keep the cache branch on this path — the
                    # cherry-pick conflicted and we didn't try to
                    # resolve, so the branch would carry conflict
                    # markers. Drop it.
                    confirmed = _ask_claude_for_prereqs(
                        config, cu, outcome.conflict_files,
                        cand_dep_unit_ids, by_unit_id, base_branch,
                        warnings_acc,
                    )
                    if confirmed is not None:
                        cand_dep_unit_ids = confirmed
                        method = "git-graph+claude"
                    ai_path_invoked = True
                elif cache_branch and outcome.conflicting_pr_idx is not None:
                    # Fallback path: deterministic empty AND we have the
                    # conflict state preserved in the cache branch. Hand
                    # it directly to the AI resolver — no need to
                    # recreate the conflict.
                    fb = _ai_resolve_fallback(
                        config, scratch, base_branch, cu,
                        outcome.conflicting_pr_idx,
                        pr_url_to_unit, fully_merged_units,
                        outcome.conflict_files, warnings_acc,
                    )
                    ai_path_invoked = True
                    if fb is None:
                        warnings_acc.append(
                            f"unit {unit_id!r}: AI resolver could not "
                            "classify the conflict; deps left empty"
                        )
                    else:
                        cand_dep_unit_ids = fb.deps
                        method = fb.method or "git-graph"
                        cache_kept = fb.resolved
                # In ``--no-write`` (no cache_branch), we skip the AI
                # fallback entirely — without the conflict state
                # preserved we'd have to recreate it, defeating the
                # caching simplification. Use whatever the deterministic
                # mapping gave us.

            # Drop dep references to unit IDs not in the candidate set —
            # ``--limit`` truncation, already-merged exclusion, etc.
            dropped_deps: list[str] = []
            deps: list[str] = []
            for d in cand_dep_unit_ids:
                if d in by_unit_id:
                    deps.append(d)
                else:
                    dropped_deps.append(d)
            if dropped_deps:
                warnings_acc.append(
                    f"unit {unit_id!r}: dropped {len(dropped_deps)} dep "
                    f"reference(s) outside the candidate set: "
                    f"{', '.join(dropped_deps)} — likely truncated by "
                    "--limit or already-merged exclusion"
                )

            # Always end the unit's processing with scratch detached at
            # target_ref. ``cache_kept`` decides whether the branch
            # stays in the ref namespace for ``releasy run`` to find or
            # gets hard-deleted.
            if cache_branch:
                _release_cache_branch(
                    scratch, target_ref, cache_branch, keep=cache_kept,
                )

            nodes[unit_id] = _make_node(
                cu, deps=deps, method=method,
                conflict_files=outcome.conflict_files,
                cached=cache_kept,
            )
            for d in deps:
                edges.add((unit_id, d))
                if d not in nodes:
                    queue.append((d, depth + 1))
    finally:
        _close_scratch_worktree(repo_path, scratch)

    # --- Break spurious cycles (older→newer wins) ---
    sort_keys = _sort_keys_from_candidates(by_unit_id)
    edges = _break_cycles(edges, sort_keys, warnings_acc)

    # User-declared groups whose discovery showed deps deserve to be
    # called out. We don't auto-mutate the main session — the user owns
    # those entries — but we do tell them via a warning so the
    # recommendation isn't buried inside ``nodes[].deps``.
    for cu in candidates:
        if cu.is_user_group:
            node = nodes.get(cu.unit_id)
            if node and node.deps:
                warnings_acc.append(
                    f"user group {cu.unit_id!r} depends on: "
                    f"{', '.join(node.deps)} — "
                    "add these to its `depends_on:` in the session file "
                    "if you want sequential gating in `releasy run`"
                )

    # --- Compute weakly-connected components and articulation points ---
    components, singletons = _components(nodes, edges, sort_keys)

    # --- Build report ---
    skipped = sorted(fully_merged_units)
    if include_already_merged:
        for uid in skipped:
            if uid not in nodes:
                cu = by_unit_id[uid]
                nodes[uid] = _make_node(
                    cu, deps=[], method="trial-clean",
                    conflict_files=[],
                )

    # --- Refresh diff: what changed since the previous overlay file? ---
    # Compares the auto-discovered unit IDs the new run would write to
    # the ones that were in the existing deps overlay (if any).
    # ``removed`` = present in old, absent in new — typically "landed in
    # target since last run" or "no longer needs porting because a
    # dependency was satisfied by something else".
    # ``added``   = present in new, absent in old — "newly discovered
    # candidate / dependency since last run".
    new_auto_unit_ids: set[str] = {
        nid for nid, n in nodes.items() if not n.is_user_group
    }
    refresh_removed = sorted(previous_auto_unit_ids - new_auto_unit_ids)
    refresh_added = sorted(new_auto_unit_ids - previous_auto_unit_ids)

    report = DiscoveryReport(
        base_branch=base_branch,
        target_sha=target_sha,
        generated_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        candidate_unit_count=len(candidates),
        candidate_pr_count=sum(len(cu.prs) for cu in candidates),
        skipped_already_in_target=skipped,
        nodes=sorted(nodes.values(), key=_node_sort_key),
        components=components,
        singletons=singletons,
        warnings=warnings_acc,
        refresh_removed=refresh_removed,
        refresh_added=refresh_added,
    )

    # --- Write outputs ---
    # Deps-file overlay first so any failure-to-write warning lands in
    # the diagnostic report's on-disk YAML (``report.warnings`` and
    # ``warnings_acc`` are the same list object). Any failure writing
    # the overlay is captured as a warning rather than propagated, so
    # the diagnostic report — the durable artifact — always lands.
    if deps_overlay_path is not None:
        try:
            _write_session_overlay(report, deps_overlay_path)
        except OSError as e:
            warnings_acc.append(
                f"failed to write deps overlay {deps_overlay_path}: {e}"
            )
        else:
            console.print(
                f"  [green]✓[/green] wrote deps overlay → "
                f"[cyan]{deps_overlay_path}[/cyan]"
            )
    elif config.session and config.session.session_path:
        # We're skipping (--no-write). Note where the overlay *would*
        # have gone so the user knows we noticed and chose to skip.
        from releasy.config import resolve_deps_file_path
        target = resolve_deps_file_path(
            config.session.session_path,
            config.session.pr_sources.deps_file,
        )
        console.print(
            f"  [dim]--no-write: skipping deps overlay "
            f"(would have written to {target})[/dim]"
        )

    output_path = output_path or _default_report_path(config, base_branch)
    _write_report(report, output_path)

    return report


# ---------------------------------------------------------------------------
# Candidate set + already-merged detection
# ---------------------------------------------------------------------------


def _build_candidate_unit_set(
    units: list[FeatureUnit], config: Config,
) -> list[_CandidateUnit]:
    """Flatten ``discover_feature_units`` output into _CandidateUnit's.

    Sort by earliest merged_at ascending (oldest first); the traversal
    later iterates this in reverse for the "latest → oldest" walk.
    """
    out: list[_CandidateUnit] = []
    for u in units:
        merged = [p.merged_at for p in u.prs if p.merged_at]
        earliest = min(merged) if merged else None
        out.append(_CandidateUnit(
            unit_id=u.feature_id,
            is_user_group=u.is_group,
            prs=list(u.prs),
            earliest_merged_at=earliest,
            feature_unit=u,
        ))
    out.sort(key=lambda c: (
        c.earliest_merged_at or "9999",
        c.prs[0].number if c.prs else 0,
    ))
    return out


def _state_already_in_target(
    candidates: list[_CandidateUnit], state: PipelineState,
) -> set[str]:
    """PR URLs whose unit is already recorded as merged/branch_created in state."""
    out: set[str] = set()
    state_urls: set[str] = set()
    for fs in state.features.values():
        if fs.status in ("merged",):
            if fs.pr_url:
                state_urls.add(fs.pr_url)
            for u in fs.pr_urls or []:
                state_urls.add(u)
    for cu in candidates:
        for p in cu.prs:
            if p.url in state_urls:
                out.add(p.url)
    return out


def _trailer_scan(
    repo_path: Path, target_ref: str, candidate_urls: set[str],
) -> set[str]:
    """Scan target's recent history for ``Source-PR:`` trailers; return matched URLs."""
    if not candidate_urls:
        return set()
    rev_range = f"{target_ref}~2000..{target_ref}"
    # If target has fewer than 2000 commits, fall back to full history.
    result = run_git(
        ["log", rev_range,
         "--format=%(trailers:key=Source-PR,unfold=true,valueonly=true)"],
        repo_path, check=False,
    )
    if result.returncode != 0:
        result = run_git(
            ["log", target_ref,
             "--format=%(trailers:key=Source-PR,unfold=true,valueonly=true)"],
            repo_path, check=False,
        )
    if result.returncode != 0:
        return set()
    out: set[str] = set()
    for line in result.stdout.splitlines():
        for m in _SOURCE_PR_URL_RE.finditer(line):
            url = m.group(0)
            if url in candidate_urls:
                out.add(url)
    return out


def _git_cherry_already(
    repo_path: Path, target_ref: str,
    candidates: list[_CandidateUnit], warnings_acc: list[str],
) -> set[str]:
    """For each candidate PR's merge_commit_sha, ask ``git cherry`` whether
    every commit the PR introduced has a patch-id equivalent in target.

    Implementation note (was a bug, now fixed):

    ``git cherry <upstream> <head>`` walks ``<upstream>..<head>`` —
    EVERY non-merge commit between the merge-base and ``<head>``. For a
    PR's merge commit on master, that's typically *hundreds* of master
    commits, including unrelated PRs the user may have cherry-picked
    into target. Marking the candidate PR as "already in target"
    because *any* of those master commits had a patch-id match is
    wrong — that's what produced false positives where PRs that were
    never ported showed up under ``skipped_already_in_target``.

    Two scoping changes:

    1. Constrain the walk to the PR's *own* commits via the
       ``<limit>`` argument: ``git cherry <target> <head> <limit>``
       walks ``<limit>..<head>`` only.
       * For a true merge commit (2+ parents), ``<limit>`` is
         ``parents[0]`` and ``<head>`` is ``parents[1]`` — the PR
         branch's own commits.
       * For a single-parent commit (squash-merged PR — and rebase-
         merged PRs whose ``merge_commit_sha`` happens to be the last
         commit), ``<limit>`` is ``<sha>~1``. For squashes this
         walks exactly the squash commit; for rebase-merged PRs with
         multiple commits this is an under-approximation (we only
         check the last one), which is a deliberate trade-off:
         missing a true positive (false negative) is far less harmful
         than mistakenly skipping a PR that needs porting.

    2. Require *every* line in the constrained output to start with
       ``- `` (patch-id match) before marking the PR as already-in-
       target. The previous "any match" policy was the actual source
       of false positives even after scoping is corrected.

    Cross-repo / unfetched merge SHAs are skipped silently via the
    ``cat-file -e`` precheck, same as before.
    """
    out: set[str] = set()
    for cu in candidates:
        for p in cu.prs:
            sha = p.merge_commit_sha
            if not sha:
                continue
            # Verify the SHA is present locally; otherwise skip — cross-repo
            # PRs from include_prs may not have been fetched.
            check = run_git(
                ["cat-file", "-e", sha], repo_path, check=False,
            )
            if check.returncode != 0:
                continue

            # Determine the right (head, limit) pair for ``git cherry``
            # by inspecting the merge commit's parents.
            parents_res = run_git(
                ["rev-list", "--parents", "-n", "1", sha],
                repo_path, check=False,
            )
            if parents_res.returncode != 0 or not parents_res.stdout.strip():
                continue
            parts = parents_res.stdout.strip().split()
            # parts: [<sha>, <p1>] for non-merge commits (squash / rebase).
            # parts: [<sha>, <p1>, <p2>, ...] for merge commits.
            if len(parts) >= 3:
                _, p1, p2 = parts[0], parts[1], parts[2]
                cherry = run_git(
                    ["cherry", target_ref, p2, p1],
                    repo_path, check=False,
                )
            elif len(parts) == 2:
                cherry = run_git(
                    ["cherry", target_ref, sha, parts[1]],
                    repo_path, check=False,
                )
            else:
                # Initial commit / no parents — can't scope; skip.
                continue
            if cherry.returncode != 0:
                continue

            lines = [
                line.strip() for line in cherry.stdout.splitlines()
                if line.strip()
            ]
            if not lines:
                # Empty range (degenerate merge / no commits to compare).
                # Be conservative: don't mark.
                continue
            # Strict: every PR commit must have a patch-id equivalent in
            # target before we conclude the PR is already there.
            if all(line.startswith("- ") for line in lines):
                out.add(p.url)
    return out


# ---------------------------------------------------------------------------
# Trial cherry-pick environment
# ---------------------------------------------------------------------------


# Module-level registry of cleanup flags keyed on scratch worktree path.
# Stored separately from the ``Path`` object because 3.12+ slots out
# arbitrary attribute assignment on ``pathlib.Path``. Each entry is
# ``[bool]`` (single-element list, used as a mutable cell) so the
# atexit closure and the explicit close path can both flip it from True
# to indicate "already cleaned, skip the redundant `worktree remove`".
_SCRATCH_CLEANUP_FLAGS: dict[str, list[bool]] = {}


def _open_scratch_worktree(
    repo_path: Path, scratch_parent: Path, target_ref: str,
) -> Path:
    """Create a detached scratch worktree at ``target_ref`` under
    ``scratch_parent`` and register a best-effort cleanup via
    :mod:`atexit` (in addition to the caller's try/finally).

    The parent is the user-blessed work_dir, never derived from
    ``repo_path.parent`` (see :func:`run_discover_deps` for the rationale).
    """
    # Reuse the project's standard short-id helper so scratch dirs sort
    # alongside other releasy-managed names.
    from releasy.cli import _short_id

    short_id = _short_id()
    scratch = scratch_parent / f".releasy-discover-deps-{short_id}"
    run_git(
        ["worktree", "add", "--detach", str(scratch), target_ref],
        repo_path,
    )

    cleaned = [False]
    _SCRATCH_CLEANUP_FLAGS[str(scratch)] = cleaned

    def _cleanup() -> None:
        if cleaned[0]:
            return
        cleaned[0] = True
        try:
            run_git(
                ["worktree", "remove", "--force", str(scratch)],
                repo_path, check=False,
            )
        except Exception:  # pragma: no cover — best-effort cleanup
            pass
    atexit.register(_cleanup)
    return scratch


def _close_scratch_worktree(repo_path: Path, scratch: Path) -> None:
    flag = _SCRATCH_CLEANUP_FLAGS.pop(str(scratch), None)
    if flag is not None:
        flag[0] = True
    run_git(
        ["worktree", "remove", "--force", str(scratch)],
        repo_path, check=False,
    )


def _cache_branch_name(base_branch: str, unit_id: str) -> str:
    """Same naming convention :func:`Config.feature_branch_name` uses,
    so a branch left here by ``discover-deps`` is automatically picked
    up by ``releasy run`` via its existing ``if_exists`` policy.
    """
    return f"feature/{base_branch}/{unit_id}"


def _trial_pick_unit(
    scratch: Path, unit: _CandidateUnit, target_ref: str,
    *,
    cache_branch: str | None,
    is_group: bool,
    origin_slug: str | None,
) -> _PickOutcome:
    """Sequentially cherry-pick every PR in the unit onto a named branch.

    On clean: returns ``clean=True``; the worktree is left on
    ``cache_branch`` at ``target_ref + unit_PRs`` (caller decides whether
    to keep it). For multi-PR groups, each commit gets a ``Source-PR:``
    trailer mirroring the convention ``releasy run`` uses, so the
    resulting PR's commit list is self-attributing.

    On conflict: returns ``clean=False`` with the offending PR's index
    and conflict files. The worktree is **left in conflict state on
    ``cache_branch``** so the caller can hand it to the AI resolver
    without having to recreate the conflict.

    No automatic reset — the caller drives cleanup based on the outcome
    and any AI fallback decision.

    When ``cache_branch`` is ``None`` (e.g. ``--no-write`` mode), the
    worktree stays detached at ``target_ref`` and the function ALWAYS
    resets afterwards regardless of outcome — pure dry-run behaviour.
    """
    prs = _ordered_prs_for_pick(unit)

    if cache_branch is None:
        # Pure dry-run mode: detached HEAD, always reset.
        try:
            for idx, p in enumerate(prs):
                sha = p.merge_commit_sha
                if not sha:
                    return _PickOutcome(
                        clean=False, conflict_files=[],
                        error_message=f"PR {p.url} has no merge_commit_sha",
                        conflicting_pr_idx=idx,
                    )
                res = cherry_pick_merge_commit(
                    scratch, sha, abort_on_conflict=False,
                )
                if not res.success:
                    return _PickOutcome(
                        clean=False,
                        conflict_files=list(res.conflict_files),
                        error_message=res.error_message,
                        conflicting_pr_idx=idx,
                    )
            return _PickOutcome(clean=True, conflict_files=[])
        finally:
            abort_in_progress_op(scratch)
            run_git(["reset", "--hard", target_ref], scratch, check=False)
            run_git(["clean", "-fdx"], scratch, check=False)

    # Caching path: switch scratch to the cache branch, force-resetting
    # any prior cache for this unit. ``-B`` is "create-or-reset to ref".
    run_git(["checkout", "-B", cache_branch, target_ref], scratch, check=False)

    for idx, p in enumerate(prs):
        sha = p.merge_commit_sha
        if not sha:
            # No merge SHA — caller will reset/delete the branch. Don't
            # leave junk state behind.
            return _PickOutcome(
                clean=False, conflict_files=[],
                error_message=f"PR {p.url} has no merge_commit_sha",
                conflicting_pr_idx=idx,
                cache_branch=cache_branch,
            )
        res = cherry_pick_merge_commit(
            scratch, sha, abort_on_conflict=False,
        )
        if not res.success:
            # Leave the worktree in conflict state on cache_branch — the
            # caller's AI fallback path can operate on it directly.
            return _PickOutcome(
                clean=False,
                conflict_files=list(res.conflict_files),
                error_message=res.error_message,
                conflicting_pr_idx=idx,
                cache_branch=cache_branch,
            )
        # Tag commit with Source-PR trailer for multi-PR groups, mirroring
        # ``pipeline._tag_commit_with_source_pr``. Singletons skip this —
        # the branch IS the source PR, trailer would be redundant noise.
        if is_group and len(prs) > 1:
            from releasy.github_ops import pr_ref_label
            ref = pr_ref_label(p.repo_slug, p.number, origin_slug)
            append_commit_trailer(
                scratch, "Source-PR", f"{ref} ({p.url})",
            )

    return _PickOutcome(
        clean=True, conflict_files=[],
        cache_branch=cache_branch,
    )


def _release_cache_branch(
    scratch: Path, target_ref: str, branch_name: str | None,
    *, keep: bool,
) -> None:
    """Detach scratch from the cache branch and (optionally) delete it.

    ``keep=True``: branch persists in the main repo's ref namespace —
    ``releasy run`` will find it via its ``if_exists`` policy.
    ``keep=False``: branch is hard-deleted (used when caching wasn't
    appropriate for this unit, e.g. AI fallback failed).

    Always aborts any in-progress op and resets the scratch worktree
    to a detached state at ``target_ref`` so the next unit starts
    from a clean slate.
    """
    abort_in_progress_op(scratch)
    # Detach so the branch (if kept) isn't holding a checkout lock.
    run_git(["checkout", "--detach", target_ref], scratch, check=False)
    run_git(["clean", "-fdx"], scratch, check=False)
    if branch_name and not keep:
        run_git(["branch", "-D", branch_name], scratch, check=False)


def _ordered_prs_for_pick(unit: _CandidateUnit) -> list[PRInfo]:
    """Return the unit's PRs in cherry-pick order — same logic
    :func:`_build_group_units` uses (group.sort honoured).
    """
    fu = unit.feature_unit
    if fu.is_group:
        # The FeatureUnit was already sorted in _build_group_units when
        # group.sort == "merged_at". For "listed", keep current order.
        return list(fu.prs)
    return list(fu.prs)


# ---------------------------------------------------------------------------
# Conflict file → candidate unit mapping
# ---------------------------------------------------------------------------


def _build_merge_containment_map(
    repo_path: Path, target_ref: str,
    candidates: list[_CandidateUnit], warnings_acc: list[str],
) -> dict[str, str]:
    """Return ``{non_merge_sha: enclosing_merge_sha}`` for commits between
    target_ref and any candidate merge commit's first-parent diff.

    Only candidate merge commits matter — drift commits don't get
    classified anyway. Built once per discover-deps run.
    """
    containment: dict[str, str] = {}
    for cu in candidates:
        for p in cu.prs:
            mc = p.merge_commit_sha
            if not mc:
                continue
            # Verify object exists locally
            chk = run_git(["cat-file", "-e", mc], repo_path, check=False)
            if chk.returncode != 0:
                continue
            # Get the merge's parents
            parents_res = run_git(
                ["rev-list", "--parents", "-n", "1", mc],
                repo_path, check=False,
            )
            if parents_res.returncode != 0 or not parents_res.stdout.strip():
                continue
            parts = parents_res.stdout.strip().split()
            if len(parts) < 3:
                # Not a merge commit (only 1 parent) — skip.
                continue
            p1, p2 = parts[1], parts[2]
            log_res = run_git(
                ["log", "--format=%H", f"{p1}..{p2}"],
                repo_path, check=False,
            )
            if log_res.returncode != 0:
                continue
            for sha in log_res.stdout.split():
                containment.setdefault(sha, mc)
    return containment


def _candidate_deps_for_conflict(
    repo_path: Path,
    target_ref: str,
    conflict_files: list[str],
    *,
    candidate_merge_shas: list[str],
    merge_sha_to_unit: dict[str, str],
    pr_url_to_unit: dict[str, str],
    merge_containment: dict[str, str],
    exclude_unit_ids: set[str],
    already_in_target_units: set[str],
) -> list[str]:
    """Map conflict files back to candidate unit IDs that touched them.

    Algorithm: for each conflict file, run ``git log --not target_ref
    <merge_shas...> -- file`` to enumerate commits reachable from any
    candidate but not target that touched the file. Classify each commit
    via merge-commit match → Source-PR trailer → containment map. Project
    PR URLs to unit IDs. Drop the trial-pick's own unit and units already
    in target.
    """
    if not conflict_files or not candidate_merge_shas:
        return []
    cand_set = set(candidate_merge_shas)
    found_units: list[str] = []
    seen: set[str] = set()
    for f in conflict_files:
        log_args = ["log", "--format=%H", "--not", target_ref] + list(cand_set) + ["--", f]
        try:
            res = run_git(log_args, repo_path, check=False)
        except Exception:
            continue
        if res.returncode != 0:
            continue
        for sha in res.stdout.split():
            unit_id = _classify_commit_to_unit(
                repo_path, sha, candidate_merge_shas=cand_set,
                merge_sha_to_unit=merge_sha_to_unit,
                pr_url_to_unit=pr_url_to_unit,
                merge_containment=merge_containment,
            )
            if not unit_id:
                continue
            if unit_id in exclude_unit_ids:
                continue
            if unit_id in already_in_target_units:
                continue
            if unit_id in seen:
                continue
            seen.add(unit_id)
            found_units.append(unit_id)
    return found_units


def _classify_commit_to_unit(
    repo_path: Path,
    sha: str,
    *,
    candidate_merge_shas: set[str],
    merge_sha_to_unit: dict[str, str],
    pr_url_to_unit: dict[str, str],
    merge_containment: dict[str, str],
) -> str | None:
    """Three-rule precedence:
    1. ``sha`` is a candidate's merge_commit_sha → direct lookup.
    2. Commit carries a ``Source-PR:`` trailer matching a candidate URL.
    3. Commit is contained in one of the candidate merge commits (merge_containment).
    """
    # Rule 1: direct merge match
    if sha in candidate_merge_shas:
        uid = merge_sha_to_unit.get(sha)
        if uid is not None:
            return uid

    # Rule 2: Source-PR trailer
    show = run_git(
        ["show", "-s",
         "--format=%(trailers:key=Source-PR,unfold=true,valueonly=true)",
         sha],
        repo_path, check=False,
    )
    if show.returncode == 0 and show.stdout.strip():
        for line in show.stdout.splitlines():
            for m in _SOURCE_PR_URL_RE.finditer(line):
                url = m.group(0)
                if url in pr_url_to_unit:
                    return pr_url_to_unit[url]

    # Rule 3: containment in a candidate merge commit
    enclosing = merge_containment.get(sha)
    if enclosing:
        uid = merge_sha_to_unit.get(enclosing)
        if uid is not None:
            return uid

    return None


# ---------------------------------------------------------------------------
# Claude integration
# ---------------------------------------------------------------------------


def _ask_claude_for_prereqs(
    config: Config,
    unit: _CandidateUnit,
    conflict_files: list[str],
    candidate_unit_ids: list[str],
    by_unit_id: dict[str, _CandidateUnit],
    base_branch: str,
    warnings_acc: list[str],
) -> list[str] | None:
    """Ask Claude to confirm/refine the deterministic candidate-deps list.

    Renders ``prompts/discover_prereqs.md`` and parses the model's
    ``MISSING_PREREQS:`` output. Returns the confirmed subset (URLs
    mapped back to unit_ids) or ``None`` to signal "AI unavailable, use
    deterministic candidates as-is".
    """
    prompt_path = (
        config.config_path.parent / "prompts" / "discover_prereqs.md"
    )
    if not prompt_path.exists():
        # Fallback to bundled template
        prompt_path = (
            Path(__file__).parent / "prompts" / "discover_prereqs.md"
        )
    if not prompt_path.exists():
        warnings_acc.append(
            "discover_prereqs.md prompt template not found; "
            "skipping Claude refinement"
        )
        return None

    template = prompt_path.read_text(encoding="utf-8")

    cand_block_lines: list[str] = []
    url_to_unit: dict[str, str] = {}
    for cuid in candidate_unit_ids:
        cu = by_unit_id.get(cuid)
        if not cu:
            continue
        for p in cu.prs:
            url_to_unit[p.url] = cuid
        urls = ", ".join(p.url for p in cu.prs)
        titles = "; ".join(p.title for p in cu.prs)
        cand_block_lines.append(f"- `{cuid}` — {titles} ({urls})")
    cand_block = "\n".join(cand_block_lines) or "_(none)_"

    primary = unit.prs[0] if unit.prs else None
    placeholders = {
        "source_pr_url": primary.url if primary else "",
        "source_pr_title": primary.title if primary else "",
        "unit_id": unit.unit_id,
        "conflict_files": "\n".join(f"- {f}" for f in conflict_files),
        "candidate_deps_block": cand_block,
        "base_branch": base_branch,
    }

    def _replace(match: re.Match[str]) -> str:
        key = match.group(1)
        return placeholders.get(key, match.group(0))

    rendered = re.sub(r"\{([a-zA-Z_][a-zA-Z0-9_]*)\}", _replace, template)

    res = synthesize_text(
        config, rendered,
        label=f"discover-deps:{unit.unit_id}",
        timeout_seconds=config.ai_resolve.timeout_seconds,
        command=config.ai_resolve.command,
    )
    if not res.success or not res.text:
        warnings_acc.append(
            f"Claude refinement failed for {unit.unit_id!r}: "
            f"{res.error or 'no output'}; falling back to deterministic candidates"
        )
        return None

    # Distinguish "Claude said no prereqs" (a deliberate empty list under a
    # ``MISSING_PREREQS:`` line) from "Claude misformatted" (no marker line
    # at all). Without this check, both look identical to the parser and a
    # malformed response would silently look like a confident "no prereqs".
    if _MISSING_PREREQS_RE.search(res.text) is None:
        warnings_acc.append(
            f"Claude refinement for {unit.unit_id!r}: response did not "
            "include a MISSING_PREREQS: line; treating as malformed and "
            "falling back to deterministic candidates"
        )
        return None

    confirmed_urls, _reason = _parse_missing_prereqs(res.text)
    confirmed_units: list[str] = []
    seen: set[str] = set()
    for url in confirmed_urls:
        uid = url_to_unit.get(url)
        if uid is None:
            warnings_acc.append(
                f"Claude returned URL {url!r} for {unit.unit_id!r} that is "
                "not in the candidate-deps list; ignoring"
            )
            continue
        if uid in seen:
            continue
        seen.add(uid)
        confirmed_units.append(uid)
    return confirmed_units


@dataclass
class _AIFallbackResult:
    """Outcome of the AI-resolve fallback path. Distinguishes the cases
    so the caller can pick a ``discovery_method`` AND decide whether to
    keep the cache branch.
    """
    # Confirmed missing-prereq unit IDs, mapped from URLs Claude returned.
    # Empty list when the resolver found no real prereqs.
    deps: list[str]
    # ``True``  — resolver advanced HEAD with a real resolution. Caller
    #             should keep the cache branch (it carries the
    #             AI-resolved cherry-pick).
    # ``False`` — resolver couldn't resolve cleanly. Caller should drop
    #             the cache branch.
    resolved: bool
    # ``"ai-resolve"``       — deps populated, resolver gave up, prereqs
    #                          point at older un-ported units.
    # ``"ai-resolve-clean"`` — resolver succeeded without prereqs (drift).
    # ``None``               — neither: resolver failed without info.
    method: str | None


def _ai_resolve_fallback(
    config: Config,
    scratch: Path,
    base_branch: str,
    unit: _CandidateUnit,
    conflicting_pr_idx: int,
    pr_url_to_unit: dict[str, str],
    already_in_target_units: set[str],
    conflict_files: list[str],
    warnings_acc: list[str],
) -> _AIFallbackResult | None:
    """Heavyweight fallback: hand an *existing* conflict state to the
    full AI resolver and read its missing-prereqs / resolution outcome.

    Caller guarantees the worktree is currently in conflict state on
    ``unit.feature_unit.prs[conflicting_pr_idx]`` (left there by the
    trial-pick on the cache branch). We don't recreate the conflict —
    we just hand it to ``attempt_ai_resolve`` and read the result.

    Three possible returns:

    * ``_AIFallbackResult(deps=[...], resolved=True, method="ai-resolve")``
      — Claude reported ``MISSING_PREREQS`` AND advanced HEAD. Deps
      populated; cache branch carries the resolution.
    * ``_AIFallbackResult(deps=[...], resolved=False, method="ai-resolve")``
      — Claude reported ``MISSING_PREREQS`` but did NOT resolve.
      Deps populated; cache branch should be dropped.
    * ``_AIFallbackResult(deps=[], resolved=True,
      method="ai-resolve-clean")`` — Claude resolved cleanly without
      prereqs (drift). Cache branch carries the resolution.
    * ``None`` — resolver failed uncertainly (no resolution, no
      missing-prereq info). Caller should fall back to empty deps and
      drop the cache.

    On resolver failure ``attempt_ai_resolve`` already resets HEAD to
    ``ctx.start_sha`` (which is the cache branch's tip BEFORE the
    failing pick — i.e. the prefix that DID apply cleanly for groups).
    The caller's branch-disposal logic resets to ``target_ref`` afterwards.
    """
    try:
        prs = unit.feature_unit.prs
        if not (0 <= conflicting_pr_idx < len(prs)):
            return None
        conflicting_pr = prs[conflicting_pr_idx]

        ctx = AIResolveContext(
            port_branch=f"discover-deps-trial-{unit.unit_id}",
            base_branch=base_branch,
            source_pr=conflicting_pr,
            conflict_files=list(conflict_files),
            operation="cherry-pick",
            user_context=unit.feature_unit.ai_context or "",
        )
        result = attempt_ai_resolve(config, scratch, ctx)

        if result.cost_usd:
            warnings_acc.append(
                f"unit {unit.unit_id!r}: AI-resolve fallback used "
                f"${result.cost_usd:.2f}"
            )

        if result.missing_prereq_prs:
            # Map URLs → unit IDs.
            confirmed: list[str] = []
            seen: set[str] = set()
            for url in result.missing_prereq_prs:
                uid = pr_url_to_unit.get(url)
                if uid is None:
                    warnings_acc.append(
                        f"unit {unit.unit_id!r}: AI-resolve fallback "
                        f"reported {url!r} as a prereq but it's not in "
                        "the candidate set; ignoring"
                    )
                    continue
                if uid == unit.unit_id:
                    continue
                if uid in already_in_target_units:
                    continue
                if uid in seen:
                    continue
                seen.add(uid)
                confirmed.append(uid)
            return _AIFallbackResult(
                deps=confirmed,
                # ``MISSING_PREREQS`` always pairs with success=False per
                # the resolver's contract — no resolution happened.
                resolved=False,
                method="ai-resolve",
            )

        if result.success:
            # Resolved cleanly without prereqs → drift.
            return _AIFallbackResult(
                deps=[], resolved=True, method="ai-resolve-clean",
            )

        # Failed without info.
        return None
    except Exception as e:  # pragma: no cover — defensive
        warnings_acc.append(
            f"unit {unit.unit_id!r}: AI-resolve fallback crashed: {e}"
        )
        return None


# ---------------------------------------------------------------------------
# Components and articulation
# ---------------------------------------------------------------------------


def _components(
    nodes: dict[str, DAGNode],
    edges: set[tuple[str, str]],
    sort_keys: dict[str, tuple[str, int]],
) -> tuple[list[DAGComponent], list[str]]:
    """Return (components, singletons). Components are the WCCs with ≥ 2
    nodes OR with at least one edge; singletons are leaf nodes with no
    edges in either direction.
    """
    # Build undirected adjacency
    adj: dict[str, set[str]] = {nid: set() for nid in nodes}
    for a, b in edges:
        if a in adj and b in adj:
            adj[a].add(b)
            adj[b].add(a)

    visited: set[str] = set()
    out_components: list[DAGComponent] = []
    out_singletons: list[str] = []
    next_id = 1

    sorted_ids = sorted(nodes.keys(), key=lambda nid: _node_sort_key(nodes[nid]))
    for nid in sorted_ids:
        if nid in visited:
            continue
        # BFS the WCC
        comp_nodes: list[str] = []
        stack = [nid]
        while stack:
            cur = stack.pop()
            if cur in visited:
                continue
            visited.add(cur)
            comp_nodes.append(cur)
            for nb in adj[cur]:
                if nb not in visited:
                    stack.append(nb)

        if len(comp_nodes) == 1 and not adj[comp_nodes[0]]:
            out_singletons.append(comp_nodes[0])
            continue

        topo = _topo_sort_within(comp_nodes, edges, sort_keys)
        articulations = _articulation_points(comp_nodes, adj)
        comp_edges = sorted(
            [(a, b) for (a, b) in edges if a in comp_nodes and b in comp_nodes],
        )
        out_components.append(DAGComponent(
            component_id=f"wcc-{next_id}",
            unit_ids=topo,
            recommend_first=sorted(articulations),
            edges=comp_edges,
        ))
        next_id += 1

    out_singletons.sort()
    return out_components, out_singletons


def _topo_sort_within(
    comp_nodes: list[str],
    edges: set[tuple[str, str]],
    sort_keys: dict[str, tuple[str, int]],
) -> list[str]:
    """Topo-sort a single component so deps come before dependents.

    Edge ``(a, b)`` means ``a`` depends on ``b``, so ``b`` should come first.
    """
    in_set = set(comp_nodes)
    indeg: dict[str, int] = {n: 0 for n in comp_nodes}
    succ: dict[str, list[str]] = {n: [] for n in comp_nodes}
    for a, b in edges:
        if a in in_set and b in in_set:
            indeg[a] += 1
            succ[b].append(a)
    ready = sorted(
        [n for n, d in indeg.items() if d == 0],
        key=lambda n: _sort_key(sort_keys, n),
    )
    out: list[str] = []
    while ready:
        n = ready.pop(0)
        out.append(n)
        for s in succ[n]:
            indeg[s] -= 1
            if indeg[s] == 0:
                key = _sort_key(sort_keys, s)
                lo, hi = 0, len(ready)
                while lo < hi:
                    mid = (lo + hi) // 2
                    if _sort_key(sort_keys, ready[mid]) < key:
                        lo = mid + 1
                    else:
                        hi = mid
                ready.insert(lo, s)
    if len(out) != len(comp_nodes):
        # Cycle — shouldn't happen post-cycle-break, but be defensive.
        leftover = [n for n in comp_nodes if n not in out]
        out.extend(sorted(leftover))
    return out


def _sort_key(
    sort_keys: dict[str, tuple[str, int]], unit_id: str,
) -> tuple[str, int]:
    """Topo/cycle tie-break key for ``unit_id`` (``(merged_at, pr_number)``).

    Decoupled from :class:`_CandidateUnit` so the same graph algorithms
    serve both fresh discovery (keys built from candidates) and
    ``releasy graph update`` (keys rebuilt from :class:`DAGNode`s).
    """
    return sort_keys.get(unit_id, ("9999", 0))


def _sort_keys_from_candidates(
    by_unit_id: dict[str, _CandidateUnit],
) -> dict[str, tuple[str, int]]:
    return {
        uid: (cu.earliest_merged_at or "9999", cu.prs[0].number if cu.prs else 0)
        for uid, cu in by_unit_id.items()
    }


_PR_NUMBER_RE = re.compile(r"/pull/(\d+)")


def _sort_keys_from_nodes(
    nodes: list[DAGNode],
) -> dict[str, tuple[str, int]]:
    out: dict[str, tuple[str, int]] = {}
    for n in nodes:
        num = 0
        if n.pr_urls:
            m = _PR_NUMBER_RE.search(n.pr_urls[0])
            num = int(m.group(1)) if m else 0
        out[n.unit_id] = (n.earliest_merged_at or "9999", num)
    return out


def _articulation_points(
    comp_nodes: list[str], adj: dict[str, set[str]],
) -> set[str]:
    """Tarjan's articulation-point algorithm on the undirected subgraph.

    Iterative implementation — Python's default recursion limit (~1000)
    isn't enough for a long-chain component (200+ PRs in series), and
    raising ``setrecursionlimit`` is fragile. The state machine below is
    the standard "neighbour iterator on the stack" formulation.
    """
    if not comp_nodes:
        return set()
    disc: dict[str, int] = {}
    low: dict[str, int] = {}
    parent: dict[str, str | None] = {}
    children_count: dict[str, int] = {}
    art: set[str] = set()
    timer = 0

    for root in comp_nodes:
        if root in disc:
            continue
        parent[root] = None
        children_count[root] = 0
        disc[root] = low[root] = timer
        timer += 1
        # Stack entries: (node, iterator over its neighbours).
        stack: list[tuple[str, "iter"]] = [(root, iter(sorted(adj[root])))]
        while stack:
            u, it = stack[-1]
            v = next(it, None)
            if v is None:
                # Done visiting u — propagate low-link to parent on pop.
                stack.pop()
                p = parent.get(u)
                if p is not None:
                    low[p] = min(low[p], low[u])
                    if low[u] >= disc[p] and parent.get(p) is not None:
                        art.add(p)
                continue
            if v not in disc:
                parent[v] = u
                children_count[v] = 0
                children_count[u] = children_count.get(u, 0) + 1
                disc[v] = low[v] = timer
                timer += 1
                stack.append((v, iter(sorted(adj[v]))))
            elif v != parent.get(u):
                low[u] = min(low[u], disc[v])
        # Root-of-DFS is articulation iff it has > 1 DFS child.
        if children_count.get(root, 0) > 1:
            art.add(root)
    return art


def _break_cycles(
    edges: set[tuple[str, str]],
    sort_keys: dict[str, tuple[str, int]],
    warnings_acc: list[str],
) -> set[tuple[str, str]]:
    """Drop reverse edges in any 2-cycle to keep the graph acyclic.

    Spec calls for ``older→newer`` to be kept and ``newer→older`` dropped,
    keyed on ``(merged_at, number)``. For longer cycles we don't try to
    do anything clever — just warn.
    """
    out = set(edges)
    # Iterate in deterministic order so the warning ordering is stable
    # across runs and a re-run produces a byte-identical YAML report.
    seen_pairs: set[tuple[str, str]] = set()
    for (a, b) in sorted(edges):
        pair = (a, b) if a < b else (b, a)
        if pair in seen_pairs:
            continue
        if (b, a) in out and a != b:
            seen_pairs.add(pair)
            ka = _sort_key(sort_keys, a)
            kb = _sort_key(sort_keys, b)
            # Convention: an edge ``(x, y)`` means "x depends on y", so
            # we want to keep the edge that points from the NEWER unit
            # to the OLDER one (newer-depends-on-older).
            if ka == kb:
                # Same merged_at + same PR number across two distinct
                # units (degenerate but possible if a candidate has
                # ``merged_at=None``). Tie-break on unit_id lexically so
                # we drop a deterministic edge instead of leaving both
                # in place (which would produce a true 2-cycle and
                # break the topological sort downstream).
                if a < b:
                    out.discard((a, b))
                    warnings_acc.append(
                        f"cycle broken between {a!r} and {b!r}; kept "
                        f"{b!r} → {a!r} (lexical tie-break: identical merge_at)"
                    )
                else:
                    out.discard((b, a))
                    warnings_acc.append(
                        f"cycle broken between {a!r} and {b!r}; kept "
                        f"{a!r} → {b!r} (lexical tie-break: identical merge_at)"
                    )
            elif ka < kb:
                # a is older; b is newer. Keep (b, a) — newer depends on older.
                out.discard((a, b))
                warnings_acc.append(
                    f"cycle broken between {a!r} and {b!r}; kept {b!r} → {a!r} "
                    "(newer depends on older)"
                )
            else:
                out.discard((b, a))
                warnings_acc.append(
                    f"cycle broken between {a!r} and {b!r}; kept {a!r} → {b!r} "
                    "(newer depends on older)"
                )
    return out


# ---------------------------------------------------------------------------
# Output writers
# ---------------------------------------------------------------------------


def _make_node(
    cu: _CandidateUnit, *, deps: list[str], method: str,
    conflict_files: list[str], cached: bool = False,
) -> DAGNode:
    return DAGNode(
        unit_id=cu.unit_id,
        is_user_group=cu.is_user_group,
        pr_urls=[p.url for p in cu.prs],
        pr_titles=[p.title for p in cu.prs],
        earliest_merged_at=cu.earliest_merged_at,
        deps=sorted(deps),
        discovery_method=method,
        conflict_files_at_discovery=list(conflict_files),
        cached=cached,
    )


def _node_sort_key(node: DAGNode) -> tuple[str, str]:
    return (node.earliest_merged_at or "9999", node.unit_id)


def _default_report_path(config: Config, base_branch: str) -> Path:
    """``<config-dir>/graph.<base-branch>.yaml``."""
    return config.config_path.parent / f"graph.{base_branch}.yaml"


def _read_previous_overlay_auto_ids(overlay_path: Path) -> set[str]:
    """Extract auto-discovered unit IDs from an existing deps overlay file.

    Used to compute the refresh diff (which units disappeared / were
    added since the last run). Best-effort: any read / parse error
    returns an empty set so a malformed previous file doesn't block
    the new run.
    """
    if not overlay_path.exists():
        return set()
    try:
        with open(overlay_path) as f:
            raw = yaml.safe_load(f) or {}
    except Exception:
        return set()
    if not isinstance(raw, dict):
        return set()
    out: set[str] = set()
    for entry in raw.get("groups", []) or []:
        if not isinstance(entry, dict):
            continue
        if not entry.get("auto_discovered"):
            continue
        gid = entry.get("id")
        if isinstance(gid, str):
            out.add(gid)
    return out


def _write_report(report: DiscoveryReport, path: Path) -> None:
    data: dict = {
        "base_branch": report.base_branch,
        "target_sha": report.target_sha,
        "generated_at": report.generated_at,
        "candidate_unit_count": report.candidate_unit_count,
        "candidate_pr_count": report.candidate_pr_count,
    }
    if report.issue_number is not None:
        gi: dict = {"number": report.issue_number}
        if report.issue_url:
            gi["url"] = report.issue_url
        if report.last_ingested_at:
            gi["last_ingested_at"] = report.last_ingested_at
        data["graph_issue"] = gi
    if report.skipped_already_in_target:
        data["skipped_already_in_target"] = list(report.skipped_already_in_target)
    if report.refresh_removed or report.refresh_added:
        data["refresh"] = {
            k: v for k, v in {
                "removed_since_last_run": list(report.refresh_removed) or None,
                "added_since_last_run": list(report.refresh_added) or None,
            }.items()
            if v is not None
        }
    if report.warnings:
        data["warnings"] = list(report.warnings)
    if report.components:
        data["components"] = [
            {
                "component_id": c.component_id,
                "unit_ids": list(c.unit_ids),
                "recommend_first": list(c.recommend_first),
                "edges": [list(e) for e in c.edges],
            }
            for c in report.components
        ]
    if report.singletons:
        data["singletons"] = list(report.singletons)
    data["nodes"] = [
        {
            k: v
            for k, v in {
                "unit_id": n.unit_id,
                "is_user_group": n.is_user_group,
                "pr_urls": n.pr_urls,
                "pr_titles": n.pr_titles,
                "earliest_merged_at": n.earliest_merged_at,
                "deps": n.deps or None,
                "discovery_method": n.discovery_method,
                "conflict_files_at_discovery": (
                    n.conflict_files_at_discovery or None
                ),
                "cached": True if n.cached else None,
            }.items()
            if v is not None
        }
        for n in report.nodes
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        yaml.dump(data, f, default_flow_style=False, sort_keys=False)


def _write_session_overlay(
    report: DiscoveryReport, overlay_path: Path,
) -> None:
    """Emit the deps overlay file at the path declared in
    ``pr_sources.deps_file``.

    Only writes entries for units that participate in the DAG (≥ 1 edge
    in or out). Pure singletons are omitted — the loader doesn't need
    them, they'll be re-discovered by ``by_labels`` / ``include_prs``.
    """
    relevant_unit_ids: set[str] = set()
    for c in report.components:
        relevant_unit_ids.update(c.unit_ids)
    nodes_by_id = {n.unit_id: n for n in report.nodes}

    overlay_groups: list[dict] = []
    for uid in sorted(
        relevant_unit_ids,
        key=lambda u: _node_sort_key(nodes_by_id[u]),
    ):
        node = nodes_by_id[uid]
        if node.is_user_group:
            # Don't replicate user groups in the overlay — they live in
            # the main session. We only emit deps as a separate single-PR
            # group when we own the entry (auto_discovered unit).
            continue
        entry: dict = {
            "id": uid,
            "prs": list(node.pr_urls),
            "auto_discovered": True,
        }
        if node.deps:
            entry["depends_on"] = list(node.deps)
        overlay_groups.append(entry)

    data: dict = {
        "generated_at": report.generated_at,
        "base_branch": report.base_branch,
    }
    if overlay_groups:
        data["groups"] = overlay_groups

    overlay_path.parent.mkdir(parents=True, exist_ok=True)
    with open(overlay_path, "w") as f:
        f.write(
            "# AUTO-GENERATED by `releasy graph discover`.\n"
            "# Hand-edits will be overwritten on next run; remove this file\n"
            "# (or move entries into the main session file) to make them permanent.\n\n"
        )
        yaml.dump(data, f, default_flow_style=False, sort_keys=False)


def load_report(path: Path) -> DiscoveryReport:
    """Reconstruct a :class:`DiscoveryReport` from a YAML written by
    :func:`_write_report`.

    Inverse of the writer; tolerant of its omit-when-empty conventions
    (a missing ``deps`` / ``conflict_files_at_discovery`` → ``[]``, a
    missing ``cached`` → ``False``) and re-tuples the 2-element edge
    lists. Used by ``releasy graph update`` to reload the last
    discovered graph without re-running the (expensive) trial picks.
    """
    with open(path) as f:
        raw = yaml.safe_load(f) or {}
    if not isinstance(raw, dict):
        raise ValueError(f"{path}: expected a mapping at the top level")

    nodes: list[DAGNode] = []
    for nd in raw.get("nodes", []) or []:
        nodes.append(DAGNode(
            unit_id=nd["unit_id"],
            is_user_group=bool(nd.get("is_user_group", False)),
            pr_urls=list(nd.get("pr_urls", []) or []),
            pr_titles=list(nd.get("pr_titles", []) or []),
            earliest_merged_at=nd.get("earliest_merged_at"),
            deps=list(nd.get("deps", []) or []),
            discovery_method=nd.get("discovery_method", ""),
            conflict_files_at_discovery=list(
                nd.get("conflict_files_at_discovery", []) or []
            ),
            cached=bool(nd.get("cached", False)),
        ))

    components: list[DAGComponent] = []
    for cd in raw.get("components", []) or []:
        components.append(DAGComponent(
            component_id=cd.get("component_id", ""),
            unit_ids=list(cd.get("unit_ids", []) or []),
            recommend_first=list(cd.get("recommend_first", []) or []),
            edges=[tuple(e) for e in (cd.get("edges", []) or []) if len(e) == 2],
        ))

    refresh = raw.get("refresh", {}) or {}
    gi = raw.get("graph_issue", {}) or {}
    return DiscoveryReport(
        base_branch=raw.get("base_branch", ""),
        target_sha=raw.get("target_sha", ""),
        generated_at=raw.get("generated_at", ""),
        candidate_unit_count=int(raw.get("candidate_unit_count", 0)),
        candidate_pr_count=int(raw.get("candidate_pr_count", 0)),
        skipped_already_in_target=list(
            raw.get("skipped_already_in_target", []) or []
        ),
        nodes=nodes,
        components=components,
        singletons=list(raw.get("singletons", []) or []),
        warnings=list(raw.get("warnings", []) or []),
        refresh_removed=list(refresh.get("removed_since_last_run", []) or []),
        refresh_added=list(refresh.get("added_since_last_run", []) or []),
        issue_number=gi.get("number"),
        issue_url=gi.get("url"),
        last_ingested_at=gi.get("last_ingested_at"),
    )


def recompute_components(
    report: DiscoveryReport,
) -> tuple[list[DAGComponent], list[str]]:
    """Recompute WCCs / topo order / articulation points from a report's
    nodes and their ``deps``.

    Used by ``releasy graph update`` after human corrections have mutated
    the node/edge set: it mirrors the component-derivation step of
    :func:`run_discover_deps` but works purely from :class:`DAGNode`
    data — no ``_CandidateUnit`` or git worktree required. Edges are
    re-derived from ``node.deps`` (the canonical per-node dependency
    list), so a corrected ``deps`` is all the caller needs to mutate.
    """
    node_map = {n.unit_id: n for n in report.nodes}
    edges: set[tuple[str, str]] = {
        (n.unit_id, dep)
        for n in report.nodes
        for dep in n.deps
        if dep in node_map
    }
    sort_keys = _sort_keys_from_nodes(report.nodes)
    return _components(node_map, edges, sort_keys)


# ---------------------------------------------------------------------------
# Misc helpers
# ---------------------------------------------------------------------------


def _resolve_sha(repo_path: Path, ref: str) -> str:
    res = run_git(["rev-parse", ref], repo_path, check=False)
    if res.returncode != 0:
        return ""
    return res.stdout.strip()
