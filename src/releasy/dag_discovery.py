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
    ensure_remote,
    ensure_work_repo,
    fetch_remote,
    get_conflict_files,
    is_operation_in_progress,
    run_git,
)
from releasy.github_ops import (
    PRInfo,
    add_issue_comment,
    create_issue,
    ensure_label,
    fetch_issue_comments,
    fetch_pr_by_url,
    get_origin_repo_slug,
    parse_pr_url,
    update_issue,
)
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
    # Member-vetoed PRs [{"url","reason"}]; enforced via exclude_prs.
    excluded: list[dict] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def _resolve_base_branch(config: Config, onto: str | None) -> str:
    """Base branch shared by discover + update: ``--onto``, else target_branch."""
    if onto:
        return config.base_branch_name(onto)
    if config.target_branch:
        return config.target_branch
    raise ValueError(
        "cannot resolve base branch — pass --onto or set target_branch in config.yaml"
    )


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
    open_issue: bool = False,
    issue_title: str | None = None,
) -> DiscoveryReport:
    """Run dep discovery and write the report (and optionally the sidecar).

    Returns the in-memory :class:`DiscoveryReport`. The caller is expected
    to print a summary; the YAML output(s) are written here as a side effect.
    """
    # --- Resolve target branch + scratch worktree ---
    base_branch = _resolve_base_branch(config, onto)

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

    console.print(
        f"  [dim]{len(candidates)} candidate unit(s); checking which are "
        f"already in {base_branch}…[/dim]"
    )

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
    console.print(
        f"  [dim]{len(fully_merged_units)} already in target · "
        f"{len(active_for_traversal)} to trial-pick[/dim]"
    )

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
    # Recursion cap for upstream-backport pull-in: `--max-depth` overrides,
    # else ai_resolve.auto_add_prerequisite_prs.max_prereq_depth.
    cap = (
        max_depth if max_depth is not None
        else config.ai_resolve.auto_add_prerequisite_prs.max_prereq_depth
    )

    scratch = _open_scratch_worktree(repo_path, scratch_parent, target_ref)
    try:
        # Process oldest-merged-at first (ascending), so a prereq is always
        # trial-picked before any dependent — no in-set recursion needed.
        # Out-of-set upstream prereqs (cross-repo backports) are appended to
        # the queue with depth+1 and bounded by max_depth.
        queue: list[tuple[str, int]] = [
            (cu.unit_id, 0)
            for cu in sorted(
                active_for_traversal,
                key=lambda c: (
                    c.earliest_merged_at or "0000",
                    c.prs[0].number if c.prs else 0,
                ),
            )
        ]
        console.print(
            f"  [dim]trial-picking {len(queue)} unit(s) onto {base_branch} "
            "(oldest first)…[/dim]"
        )

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
            if depth > cap:
                warnings_acc.append(
                    f"unit {unit_id!r} hit max recursion depth={cap}; "
                    "upstream prerequisites may be incomplete"
                )
                # Still record the node so edges pointing at it resolve.
                # Use a distinct ``discovery_method`` so the YAML reader
                # can tell "we never trial-picked this" from "we picked
                # it and it was clean".
                nodes[unit_id] = _make_node(
                    cu, deps=[], method="depth-cutoff",
                    conflict_files=[],
                )
                console.print(f"  [dim]· {unit_id}: depth-cutoff[/dim]")
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
                console.print(f"  [dim]· {unit_id}: clean[/dim]")
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
            console.print(
                f"  [dim]· {unit_id}: conflict in {len(outcome.conflict_files)} "
                "file(s), tracing prerequisites…[/dim]"
            )
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
                        # Upstream-backport recursion: pull out-of-set
                        # prereqs from upstream for cross-repo units, gated
                        # on auto_add_prerequisite_prs.enabled + upstream +
                        # depth cap. The pulled unit is queued (depth+1) and
                        # grouped via the dependency edge.
                        if (
                            fb.external_prereq_urls
                            and config.ai_resolve.auto_add_prerequisite_prs.enabled
                            and config.upstream is not None
                            and _is_cross_repo(cu, origin_slug)
                            and depth < cap
                        ):
                            for ext_url in fb.external_prereq_urls:
                                new_cu = _pull_upstream_prereq(
                                    config, repo_path, ext_url,
                                    by_unit_id, pr_url_to_unit,
                                    merge_sha_to_unit, warnings_acc,
                                )
                                if new_cu is None:
                                    continue
                                edges.add((unit_id, new_cu.unit_id))
                                if new_cu.unit_id not in nodes:
                                    queue.append((new_cu.unit_id, depth + 1))
                                console.print(
                                    f"  [dim]· {unit_id}: pulled upstream "
                                    f"prereq {new_cu.unit_id}[/dim]"
                                )
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
            detail = f" → groups with: {', '.join(deps)}" if deps else ""
            console.print(f"  [dim]· {unit_id}: {method}{detail}[/dim]")
            # Record the directed prereq edges (dependent → prereq) used to
            # order PRs within the collapsed group. Oldest-first means every
            # in-set prereq is already processed; no recursion needed here.
            for d in deps:
                edges.add((unit_id, d))
    finally:
        _close_scratch_worktree(repo_path, scratch)

    # --- Break spurious cycles, find components, collapse them to groups ---
    sort_keys = _sort_keys_from_candidates(by_unit_id)
    edges = _break_cycles(edges, sort_keys, warnings_acc)
    components, _singletons = _components(nodes, edges, sort_keys)
    folded, components = _collapse_components_to_groups(
        nodes, components, warnings_acc,
    )
    # The group isn't cached (run cherry-picks it fresh in the emitted
    # order), so drop the now-superseded per-member cache branches.
    if cache_enabled:
        for uid in folded:
            run_git(
                ["branch", "-D", _cache_branch_name(base_branch, uid)],
                repo_path, check=False,
            )
    if folded:
        console.print(
            f"  [dim]grouped {len(folded)} unit(s) into combined port(s)[/dim]"
        )
    # `components` now holds only the kept (user-group-bearing) components;
    # singletons are the true standalone single-PR auto units.
    _in_component = {uid for c in components for uid in c.unit_ids}
    singletons = sorted(
        n.unit_id for n in nodes.values()
        if len(n.pr_urls) == 1
        and not n.is_user_group
        and n.unit_id not in _in_component
    )

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

    # Open/refresh the issue before the final write so issue_number persists
    # in the same report; carry a prior issue over to avoid duplicates.
    if open_issue:
        if output_path.exists():
            try:
                prior = load_report(output_path)
                report.issue_number = prior.issue_number
                report.issue_url = prior.issue_url
                # Preserve ingest watermark + vetoes across a re-discover.
                report.last_ingested_at = prior.last_ingested_at
                report.excluded = list(prior.excluded)
            except (OSError, ValueError):
                pass
        title = issue_title or f"Port graph for {base_branch}"
        if open_or_update_graph_issue(config, report, title=title) is None:
            warnings_acc.append("failed to open/update the graph issue")
        else:
            console.print(
                f"  [green]✓[/green] graph issue: "
                f"{report.issue_url or '#' + str(report.issue_number)}"
            )

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
    # Prereq PR URLs the resolver named that are NOT in the candidate set —
    # candidates for upstream-backport recursion (cross-repo pull-in).
    external_prereq_urls: list[str] = field(default_factory=list)


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
            # Map URLs → unit IDs; collect out-of-set URLs separately so the
            # caller can pull them in from upstream (cross-repo backports).
            confirmed: list[str] = []
            external: list[str] = []
            seen: set[str] = set()
            for url in result.missing_prereq_prs:
                uid = pr_url_to_unit.get(url)
                if uid is None:
                    if url not in external:
                        external.append(url)
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
                external_prereq_urls=external,
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


def _is_cross_repo(cu: _CandidateUnit, origin_slug: str | None) -> bool:
    """True if any of the unit's PRs lives in a repo other than origin."""
    if not origin_slug:
        return False
    return any((p.repo_slug or origin_slug) != origin_slug for p in cu.prs)


def _pull_upstream_prereq(
    config: Config,
    repo_path: Path,
    url: str,
    by_unit_id: dict[str, _CandidateUnit],
    pr_url_to_unit: dict[str, str],
    merge_sha_to_unit: dict[str, str],
    warnings_acc: list[str],
) -> _CandidateUnit | None:
    """Fetch an out-of-set upstream prerequisite PR and register it as a unit.

    Returns the new (or already-registered) ``_CandidateUnit``, or ``None``
    if it can't be fetched / its merge commit can't be made available. Needs
    ``config.upstream``. Best-effort: any failure degrades to ``None`` so the
    prereq is just flagged, never crashing discovery.
    """
    if url in pr_url_to_unit:
        return by_unit_id.get(pr_url_to_unit[url])
    if config.upstream is None:
        return None
    parsed = parse_pr_url(url)
    if parsed is None:
        warnings_acc.append(f"upstream prereq {url!r}: unparseable URL; flagging missing")
        return None
    owner, repo, number = parsed
    pr = fetch_pr_by_url(config, url, include_closed=True)
    if pr is None or not pr.merge_commit_sha:
        warnings_acc.append(
            f"upstream prereq {url!r}: unreachable or no merge commit; flagging missing"
        )
        return None
    # Make the merge commit available locally from the upstream remote.
    ensure_remote(repo_path, config.upstream.remote_name, config.upstream.remote)
    run_git(
        ["fetch", config.upstream.remote_name, pr.merge_commit_sha],
        repo_path, check=False,
    )
    if run_git(["cat-file", "-e", pr.merge_commit_sha], repo_path, check=False).returncode != 0:
        run_git(
            ["fetch", config.upstream.remote_name, config.upstream.branch],
            repo_path, check=False,
        )
    if run_git(["cat-file", "-e", pr.merge_commit_sha], repo_path, check=False).returncode != 0:
        warnings_acc.append(
            f"upstream prereq {url!r}: merge commit {pr.merge_commit_sha[:8]} "
            "not fetchable from upstream; flagging missing"
        )
        return None
    feature_id = f"{owner}-{repo}-pr-{number}"
    fu = FeatureUnit(feature_id=feature_id, prs=[pr], if_exists="skip", is_group=False)
    cu = _CandidateUnit(
        unit_id=feature_id, is_user_group=False, prs=[pr],
        earliest_merged_at=pr.merged_at, feature_unit=fu,
    )
    by_unit_id[feature_id] = cu
    pr_url_to_unit[url] = feature_id
    merge_sha_to_unit[pr.merge_commit_sha] = feature_id
    return cu


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


def _collapse_components_to_groups(
    nodes: dict[str, DAGNode],
    components: list[DAGComponent],
    warnings_acc: list[str],
) -> tuple[set[str], list[DAGComponent]]:
    """Merge each PURE-auto component into one ordered group node.

    A real (non-cosmetic) dependency means the PRs port together, so a
    component of only auto-discovered units collapses into a single group
    whose ``pr_urls`` are in the component's topological (prereq-first)
    order. A component that ALSO contains a user-declared group is NOT
    merged (the user owns that entry) — it's kept as-is so its ``depends_on``
    edges still reach the overlay; we only warn about user-group→auto deps
    that can't be applied without editing the session.

    Mutates ``nodes``. Returns ``(folded_member_ids, kept_components)`` —
    folded ids for cache-branch cleanup, kept_components (the un-merged,
    user-group-bearing ones) for the report so the overlay still emits them.
    """
    folded: set[str] = set()
    kept_components: list[DAGComponent] = []
    for comp in components:
        member_ids = [uid for uid in comp.unit_ids if uid in nodes]
        auto_ids = [uid for uid in member_ids if not nodes[uid].is_user_group]
        user_ids = [uid for uid in member_ids if nodes[uid].is_user_group]
        if user_ids:
            # Can't merge across a user-declared group: keep the component
            # as depends_on edges (auto nodes' deps reach the overlay). Warn
            # only about user-group→auto deps we can't auto-apply.
            kept_components.append(comp)
            for uid in user_ids:
                ug_deps = [d for d in nodes[uid].deps if d in member_ids]
                if ug_deps:
                    warnings_acc.append(
                        f"user group {uid!r} depends on {', '.join(ug_deps)}; "
                        "add these to its `depends_on:` in the session so "
                        "`run` gates it correctly"
                    )
            continue
        if len(auto_ids) < 2:
            continue
        pr_urls: list[str] = []
        pr_titles: list[str] = []
        merged_ats: list[str] = []
        for uid in auto_ids:  # comp.unit_ids is topo order: prereq first
            n = nodes[uid]
            pr_urls.extend(n.pr_urls)
            pr_titles.extend(n.pr_titles)
            if n.earliest_merged_at:
                merged_ats.append(n.earliest_merged_at)
        # Key the group id on the lead (prereq-most) unit id, which is
        # globally unique — `auto-grp-<min PR number>` collides across repos.
        gid = f"auto-grp-{auto_ids[0]}"
        for uid in auto_ids:
            del nodes[uid]
            folded.add(uid)
        nodes[gid] = DAGNode(
            unit_id=gid,
            is_user_group=False,
            pr_urls=pr_urls,
            pr_titles=pr_titles,
            earliest_merged_at=min(merged_ats) if merged_ats else None,
            deps=[],
            discovery_method="grouped",
            cached=False,
        )
    return folded, kept_components


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
    if report.excluded:
        data["excluded"] = [
            {"url": e.get("url", ""), "reason": e.get("reason", "")}
            for e in report.excluded
        ]
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
    # Keep multi-PR auto groups even without edges (their PRs are atomic).
    for n in report.nodes:
        if not n.is_user_group and len(n.pr_urls) > 1:
            relevant_unit_ids.add(n.unit_id)
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
        if len(node.pr_urls) > 1:
            # prs are in apply order (prereq first) — honor it verbatim,
            # don't re-sort by merged_at at port time (breaks cross-repo).
            entry["sort"] = "listed"
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
        excluded=[
            {"url": e.get("url", ""), "reason": e.get("reason", "")}
            for e in (raw.get("excluded", []) or [])
            if isinstance(e, dict) and e.get("url")
        ],
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
# Graph issue: render + open/update  (`graph discover --open-issue`)
# ---------------------------------------------------------------------------

# Marker hidden in the issue body so the issue is self-identifying.
def _issue_marker(base_branch: str) -> str:
    return f"<!-- releasy-graph:{base_branch} -->"


# Marker on RelEasy's own comments so graph update skips them on ingest.
_GRAPH_BOT_MARKER = "<!-- releasy-graph-bot -->"


def render_graph_issue_body(report: DiscoveryReport) -> str:
    """Render a DiscoveryReport as a GitHub issue body (markdown)."""
    lines: list[str] = [_issue_marker(report.base_branch)]
    lines.append(f"## Port dependency graph — `{report.base_branch}`")
    lines.append("")
    lines.append(
        f"_Generated by `releasy graph` at {report.generated_at}._"
    )
    lines.append("")
    groups = [n for n in report.nodes if len(n.pr_urls) > 1]
    singles = [n for n in report.nodes if len(n.pr_urls) == 1]
    lines.append(
        f"**{report.candidate_unit_count} unit(s) across "
        f"{report.candidate_pr_count} PR(s)** — {len(groups)} group(s), "
        f"{len(singles)} standalone. PRs inside a group port together as one "
        "combined PR, cherry-picked in the listed order (prerequisite first)."
    )
    lines.append("")

    def _title_of(n: DAGNode, i: int) -> str:
        return n.pr_titles[i] if i < len(n.pr_titles) and n.pr_titles[i] else ""

    # --- Groups (port together, in order) ---
    if groups:
        lines.append("### Groups (port together, in apply order)")
        lines.append("")
        for n in groups:
            lines.append(f"**`{n.unit_id}`**")
            for i, url in enumerate(n.pr_urls):
                lines.append(
                    f"{i + 1}. [{_pr_short(url)}]({url}) {_title_of(n, i)}".rstrip()
                )
            lines.append("")

    # --- Standalone PRs ---
    if singles:
        lines.append("### Standalone PRs")
        lines.append("")
        for n in singles:
            url = n.pr_urls[0]
            lines.append(f"- [{_pr_short(url)}]({url}) {_title_of(n, 0)}".rstrip())
        lines.append("")

    # --- Excluded ---
    if report.excluded:
        lines.append("### Excluded (vetoed by members)")
        lines.append("")
        for e in report.excluded:
            url = e.get("url", "")
            reason = e.get("reason", "") or "(no reason given)"
            lines.append(f"- [{_pr_short(url)}]({url}) — {reason}")
        lines.append("")

    # --- Footer ---
    lines.append("---")
    lines.append(
        "Org members can **comment on this issue** to change the graph — "
        "add or veto PRs, regroup, or set ordering — then run "
        "`releasy graph update` to apply your feedback."
    )
    lines.append("")
    lines.append(
        "> Dependencies set by `graph update` are human/AI-asserted, not "
        "trial-pick-verified. Re-run `releasy graph discover` for "
        "conflict-verified dependencies."
    )
    return "\n".join(lines)


def _pr_short(url: str) -> str:
    """``owner/repo#N`` or ``#N`` short label for a PR URL (fallback: url)."""
    m = _PR_NUMBER_RE.search(url)
    return f"#{m.group(1)}" if m else url


def open_or_update_graph_issue(
    config: Config, report: DiscoveryReport, *, title: str,
) -> tuple[int, str] | None:
    """Create the graph issue on origin, or update its body if it exists.

    Sets report.issue_number/issue_url on create. Returns (number, url) or
    None on failure/dry-run.
    """
    body = render_graph_issue_body(report)
    if report.issue_number is not None:
        res = update_issue(config, report.issue_number, body=body)
        if res is True:
            return report.issue_number, (report.issue_url or "")
        if res is False:
            return None  # transient failure — keep the number, retry later
        # res is None → issue was deleted; fall through to recreate.
        report.issue_number = None
        report.issue_url = None
    # Configured labels + the target-branch name, created if missing.
    labels: list[str] = []
    for name in list(config.graph.issue_labels) + [report.base_branch]:
        if name and name not in labels:
            labels.append(name)
    for name in labels:
        ensure_label(config, name)
    result = create_issue(config, title, body, labels=labels)
    if result is None:
        return None
    number, url = result
    report.issue_number = number
    report.issue_url = url
    return number, url


# ---------------------------------------------------------------------------
# `releasy graph update` — refine the graph from trusted issue comments
# ---------------------------------------------------------------------------

# A fenced ```yaml block holding the new graph spec Claude returns.
_GRAPH_SPEC_FENCE_RE = re.compile(
    r"```(?:ya?ml)?\s*\n(.*?)```", re.DOTALL,
)


def _comment_is_trusted(comment, config: Config) -> bool:  # noqa: ANN001
    assoc = (comment.author_association or "").upper()
    if assoc in set(config.graph.trusted_associations):
        return True
    login = (comment.author or "").lower()
    return login in {r.lower() for r in config.graph.trusted_reviewers}


def _parse_graph_spec(text: str) -> dict | None:
    """Parse the fenced YAML graph spec from Claude's reply, or None.

    Scans every fenced block and keeps the last that parses to a mapping
    with a `units` list (skips leading example/prose fences).
    """
    chosen: dict | None = None
    for block in _GRAPH_SPEC_FENCE_RE.findall(text):
        try:
            parsed = yaml.safe_load(block)
        except yaml.YAMLError:
            continue
        if isinstance(parsed, dict) and isinstance(parsed.get("units"), list):
            chosen = parsed  # keep scanning — last valid block wins
    return chosen


def _render_comments_block(comments: list) -> str:  # noqa: ANN001
    out: list[str] = []
    for c in comments:
        assoc = c.author_association or "?"
        out.append(
            f"### Comment by @{c.author or 'unknown'} ({assoc}) "
            f"at {c.created_at}\n{c.body.strip()}"
        )
    return "\n\n".join(out) or "_(none)_"


def _render_current_graph_block(report: DiscoveryReport) -> str:
    out: list[str] = []
    for n in report.nodes:
        deps = ", ".join(n.deps) if n.deps else "(none)"
        prs = ", ".join(n.pr_urls)
        title = n.pr_titles[0] if n.pr_titles else ""
        out.append(
            f"- id: {n.unit_id}\n"
            f"  prs: [{prs}]\n"
            f"  depends_on: [{deps}]\n"
            f"  title: {title}"
        )
    if report.excluded:
        out.append("")
        out.append("Currently excluded:")
        for e in report.excluded:
            out.append(f"- {e.get('url','')} — {e.get('reason','')}")
    return "\n".join(out) or "_(empty graph)_"


def _ask_claude_for_new_graph(
    config: Config,
    report: DiscoveryReport,
    comments: list,  # noqa: ANN001
    warnings_acc: list[str],
) -> dict | None:
    """Render the adjust-graph prompt, run Claude (text-only), parse the
    spec. Returns the spec mapping or None on any failure."""
    prompt_path = config.config_path.parent / config.graph.prompt_file
    if not prompt_path.exists():
        prompt_path = Path(__file__).parent / "prompts" / "adjust_graph.md"
    if not prompt_path.exists():
        warnings_acc.append(
            "adjust_graph.md prompt template not found; cannot run graph update"
        )
        return None

    template = prompt_path.read_text(encoding="utf-8")
    candidate_pr_list = "\n".join(
        f"- {url}" for n in report.nodes for url in n.pr_urls
    ) or "_(none)_"
    placeholders = {
        "base_branch": report.base_branch,
        "current_graph_block": _render_current_graph_block(report),
        "candidate_pr_list": candidate_pr_list,
        "comments_block": _render_comments_block(comments),
    }

    def _replace(match: re.Match[str]) -> str:
        return placeholders.get(match.group(1), match.group(0))

    rendered = re.sub(r"\{([a-zA-Z_][a-zA-Z0-9_]*)\}", _replace, template)

    res = synthesize_text(
        config, rendered,
        label="graph-update",
        timeout_seconds=config.graph.timeout_seconds,
        command=config.graph.command,
    )
    if not res.success or not res.text:
        warnings_acc.append(
            f"Claude graph-update call failed: {res.error or 'no output'}"
        )
        return None
    spec = _parse_graph_spec(res.text)
    if spec is None:
        warnings_acc.append(
            "Claude reply did not contain a parseable YAML graph spec; "
            "changing nothing"
        )
    return spec


def _build_report_from_spec(
    prior: DiscoveryReport,
    spec: dict,
    warnings_acc: list[str],
) -> DiscoveryReport | None:
    """Build a new DiscoveryReport from Claude's spec + the prior graph.

    Validates URLs/deps, rejects cycles (returns None), recomputes
    components. No git, no trial-picks.
    """
    title_map: dict[str, str] = {}
    merged_map: dict[str, str | None] = {}
    # Keep is_user_group for prior user-declared groups (overlay skips them).
    prior_user_groups = {n.unit_id for n in prior.nodes if n.is_user_group}
    for n in prior.nodes:
        for url, t in zip(n.pr_urls, n.pr_titles or []):
            title_map[url] = t
        for url in n.pr_urls:
            merged_map[url] = n.earliest_merged_at
    prior_urls = set(title_map)

    units = spec.get("units", [])
    nodes: list[DAGNode] = []
    seen_ids: set[str] = set()
    declared_urls: set[str] = set()
    for u in units:
        if not isinstance(u, dict) or not u.get("id"):
            warnings_acc.append(f"graph update: skipping malformed unit {u!r}")
            continue
        uid = str(u["id"]).strip()
        if uid in seen_ids:
            warnings_acc.append(f"graph update: duplicate unit id {uid!r}; skipping")
            continue
        prs: list[str] = []
        for raw_url in u.get("prs", []) or []:
            url = str(raw_url).strip()
            if parse_pr_url(url) is None:
                warnings_acc.append(
                    f"graph update: unit {uid!r} has unparseable PR URL "
                    f"{url!r}; dropping it"
                )
                continue
            if url in declared_urls:
                warnings_acc.append(
                    f"graph update: PR {url} assigned to more than one unit; "
                    f"keeping the first"
                )
                continue
            if url not in prior_urls:
                warnings_acc.append(
                    f"graph update: PR {url} is new (not in the prior graph) "
                    f"— it will be added to the session and ported"
                )
            prs.append(url)
            declared_urls.add(url)
        if not prs:
            warnings_acc.append(f"graph update: unit {uid!r} has no valid PRs; skipping")
            continue
        deps = [str(d).strip() for d in (u.get("depends_on", []) or [])]
        nodes.append(DAGNode(
            unit_id=uid,
            is_user_group=uid in prior_user_groups,
            pr_urls=prs,
            pr_titles=[title_map.get(url, "") for url in prs],
            earliest_merged_at=min(
                (merged_map[url] for url in prs if merged_map.get(url)),
                default=None,
            ),
            deps=deps,
            discovery_method="graph-update",
            cached=False,
        ))
        seen_ids.add(uid)

    if not nodes:
        warnings_acc.append("graph update: spec produced no units; changing nothing")
        return None

    # Drop dangling dep references, then reject cycles.
    valid_ids = {n.unit_id for n in nodes}
    for n in nodes:
        kept = [d for d in n.deps if d in valid_ids]
        for d in n.deps:
            if d not in valid_ids:
                warnings_acc.append(
                    f"graph update: unit {n.unit_id!r} depends on unknown "
                    f"{d!r}; dropping that edge"
                )
        n.deps = kept
    if _has_cycle(nodes):
        warnings_acc.append(
            "graph update: the requested dependencies form a cycle; "
            "refusing to apply (no changes made)"
        )
        return None

    # Exclusions = prior vetoes + this spec's vetoes − any PR now in a unit
    # (a veto persists unless the PR is re-added, which un-vetoes it).
    excluded_map: dict[str, str] = {
        e["url"]: e.get("reason", "")
        for e in prior.excluded
        if e.get("url")
    }
    raw_exclude = spec.get("exclude") or []
    if not isinstance(raw_exclude, list):
        warnings_acc.append(
            f"graph update: `exclude` is not a list ({type(raw_exclude).__name__}); "
            "ignoring it (prior vetoes preserved)"
        )
        raw_exclude = []
    for e in raw_exclude:
        if not isinstance(e, dict):
            warnings_acc.append(f"graph update: exclude entry is not a mapping ({e!r}); skipping")
            continue
        url = str(e.get("url", "")).strip()
        if not url or parse_pr_url(url) is None:
            warnings_acc.append(f"graph update: exclude entry has bad URL {e!r}; skipping")
            continue
        excluded_map[url] = str(e.get("reason", "")).strip()
    for url in declared_urls:  # a live unit PR cannot also be excluded
        excluded_map.pop(url, None)
    excluded = [{"url": u, "reason": r} for u, r in excluded_map.items()]

    new = DiscoveryReport(
        base_branch=prior.base_branch,
        target_sha=prior.target_sha,
        generated_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        candidate_unit_count=len(nodes),
        candidate_pr_count=sum(len(n.pr_urls) for n in nodes),
        skipped_already_in_target=list(prior.skipped_already_in_target),
        nodes=sorted(nodes, key=_node_sort_key),
        components=[],
        singletons=[],
        warnings=[],
        issue_number=prior.issue_number,
        issue_url=prior.issue_url,
        last_ingested_at=prior.last_ingested_at,
        excluded=excluded,
    )
    new.components, new.singletons = recompute_components(new)
    return new


def _has_cycle(nodes: list[DAGNode]) -> bool:
    """DFS cycle check over the directed dep graph (edge unit -> dep)."""
    succ = {n.unit_id: list(n.deps) for n in nodes}
    WHITE, GRAY, BLACK = 0, 1, 2
    color = {nid: WHITE for nid in succ}

    def visit(start: str) -> bool:
        stack = [(start, iter(succ.get(start, [])))]
        color[start] = GRAY
        while stack:
            node, it = stack[-1]
            advanced = False
            for nb in it:
                if nb not in color:
                    continue
                if color[nb] == GRAY:
                    return True
                if color[nb] == WHITE:
                    color[nb] = GRAY
                    stack.append((nb, iter(succ.get(nb, []))))
                    advanced = True
                    break
            if not advanced:
                color[node] = BLACK
                stack.pop()
        return False

    for nid in succ:
        if color[nid] == WHITE and visit(nid):
            return True
    return False


def _reconcile_session_user_groups(
    config: Config, report: DiscoveryReport, warnings_acc: list[str],
) -> int:
    """Write member edits to user-declared groups back into session.groups[]
    (the overlay skips user groups). Returns the count changed; saves if any."""
    if config.session is None:
        return 0
    groups_by_id = {g.id: g for g in config.session.pr_sources.groups}
    changed = 0
    for n in report.nodes:
        if not n.is_user_group:
            continue
        g = groups_by_id.get(n.unit_id)
        if g is None:
            warnings_acc.append(
                f"graph update: {n.unit_id!r} is flagged a user group but no "
                "matching session group exists; its edits were not applied"
            )
            continue
        new_prs = list(n.pr_urls)
        new_deps = list(n.deps)
        if g.prs != new_prs or g.depends_on != new_deps:
            g.prs = new_prs
            g.depends_on = new_deps
            changed += 1
    if changed:
        from releasy.config import save_session
        save_session(config.session)
    return changed


def run_graph_update(
    config: Config,
    *,
    onto: str | None,
    since: str | None,
    work_dir: Path | None,
    dry_run: bool,
    post_comment: bool,
) -> int:
    """Refine the saved graph from trusted member comments on its issue.

    No git: rebuilds the graph from Claude's reply, reconciles the session,
    rewrites report + overlay, refreshes the issue. Returns an exit code.
    """
    from releasy.config import resolve_deps_file_path
    from releasy import pr_membership

    try:
        base_branch = _resolve_base_branch(config, onto)
    except ValueError as e:
        console.print(f"[red]graph update: {e}[/red]")
        return 1
    report_path = _default_report_path(config, base_branch)
    if not report_path.exists():
        console.print(
            f"[red]No graph report at {report_path}.[/red] Run "
            "`releasy graph discover --open-issue` first."
        )
        return 1
    report = load_report(report_path)
    if report.issue_number is None:
        console.print(
            "[red]The saved graph has no issue.[/red] Run "
            "`releasy graph discover --open-issue` to open one first."
        )
        return 1

    res = fetch_issue_comments(config, report.issue_number)
    if res.error:
        console.print(f"[red]graph update: {res.error}[/red]")
        return 1

    cutoff = since if since is not None else report.last_ingested_at
    ingest: list = []
    for c in res.comments:
        if _GRAPH_BOT_MARKER in (c.body or ""):
            continue  # never ingest our own summary comments
        if cutoff and c.created_at and c.created_at <= cutoff:
            continue
        if not _comment_is_trusted(c, config):
            continue
        ingest.append(c)

    if not ingest:
        console.print(
            "[green]graph update:[/green] no new trusted comments on issue "
            f"#{report.issue_number} — nothing to do."
        )
        return 0

    console.print(
        f"  [dim]Feeding {len(ingest)} trusted comment(s) to Claude...[/dim]"
    )
    warnings_acc: list[str] = []
    spec = _ask_claude_for_new_graph(config, report, ingest, warnings_acc)
    for w in warnings_acc:
        console.print(f"  [yellow]warning:[/yellow] {w}")
    if spec is None:
        return 1

    build_warnings: list[str] = []
    new_report = _build_report_from_spec(report, spec, build_warnings)
    for w in build_warnings:
        console.print(f"  [yellow]warning:[/yellow] {w}")
    if new_report is None:
        return 1
    new_report.warnings = build_warnings

    # Stamp the ingest watermark to the newest comment we folded in.
    new_report.last_ingested_at = max(c.created_at for c in ingest)

    prior_urls = {url for n in report.nodes for url in n.pr_urls}
    prior_excluded_urls = {e["url"] for e in report.excluded if e.get("url")}
    new_urls = {url for n in new_report.nodes for url in n.pr_urls}
    # User-group PRs go to the session group (below), not include_prs.
    user_group_urls = {
        url for n in new_report.nodes if n.is_user_group for url in n.pr_urls
    }
    added = sorted(new_urls - prior_urls - user_group_urls)
    excluded_urls = [e["url"] for e in new_report.excluded]
    # Only enforce vetoes new this run (prior ones already in exclude_prs).
    newly_excluded = [u for u in excluded_urls if u not in prior_excluded_urls]

    _print_graph_update_summary(new_report, added, excluded_urls)

    if dry_run:
        console.print("[dim](--dry-run: no report / overlay / session / issue writes)[/dim]")
        return 0

    # --- Reconcile the session so `run` honors the changes ---
    # Honor add_pr/remove_pr False (unreachable PR / token / locked group):
    # collect failures so we don't claim a change the session never got.
    failures: list[str] = []
    for url in added:
        if not pr_membership.add_pr(config, url):
            failures.append(f"add {_pr_short(url)}")
    grp_changed = _reconcile_session_user_groups(
        config, new_report, new_report.warnings,
    )
    if grp_changed:
        console.print(
            f"  [green]✓[/green] applied edits to {grp_changed} "
            "user-declared group(s) in the session"
        )
    if config.graph.apply_exclusions:
        for url in newly_excluded:
            if not pr_membership.remove_pr(config, url):
                failures.append(f"veto {_pr_short(url)}")
    elif newly_excluded:
        console.print(
            "[dim]  (graph.apply_exclusions=false — vetoes recorded in the "
            "graph only, not added to exclude_prs)[/dim]"
        )

    # --- Persist the new graph (report + overlay) ---
    _write_report(new_report, report_path)
    if config.session and config.session.session_path:
        overlay_path = resolve_deps_file_path(
            config.session.session_path,
            config.session.pr_sources.deps_file,
        )
        try:
            _write_session_overlay(new_report, overlay_path)
        except OSError as e:
            console.print(f"  [yellow]warning:[/yellow] failed to write overlay: {e}")
        else:
            console.print(f"  [green]✓[/green] wrote deps overlay → [cyan]{overlay_path}[/cyan]")

    # --- Refresh the issue + optional summary comment ---
    if open_or_update_graph_issue(
        config, new_report, title=f"Port graph for {base_branch}",
    ) is None:
        console.print("  [yellow]warning:[/yellow] failed to update the graph issue")
    else:
        console.print(f"  [green]✓[/green] updated issue #{new_report.issue_number}")

    if post_comment:
        summary = _render_update_comment(
            new_report, added, newly_excluded, len(ingest), failures,
        )
        add_issue_comment(config, new_report.issue_number, summary)

    if failures:
        console.print(
            f"  [yellow]warning:[/yellow] {len(failures)} session edit(s) did "
            f"not apply: {', '.join(failures)}. The graph/issue reflect the "
            "requested change but the session was not fully updated — resolve "
            "manually (e.g. `releasy pr add/remove`)."
        )
        return 1
    return 0


def _print_graph_update_summary(
    report: DiscoveryReport, added: list[str], excluded: list[str],
) -> None:
    console.print("")
    console.print(f"graph update · base={report.base_branch}")
    console.print(
        f"  new graph: {report.candidate_unit_count} unit(s), "
        f"{report.candidate_pr_count} PR(s), {len(report.components)} component(s)"
    )
    if added:
        console.print(f"  added PRs: {', '.join(_pr_short(u) for u in added)}")
    if excluded:
        console.print(f"  vetoed PRs: {', '.join(_pr_short(u) for u in excluded)}")


def _render_update_comment(
    report: DiscoveryReport,
    added: list[str],
    newly_excluded: list[str],
    n_comments: int,
    failures: list[str] | None = None,
) -> str:
    failures = failures or []
    lines = [
        _GRAPH_BOT_MARKER,
        f"🤖 **`releasy graph update`** applied {n_comments} member "
        "comment(s) and rebuilt the graph.",
        "",
        f"- units: {report.candidate_unit_count} · PRs: "
        f"{report.candidate_pr_count} · components: {len(report.components)}",
    ]
    if added:
        lines.append(f"- added: {', '.join(_pr_short(u) for u in added)}")
    if newly_excluded:
        lines.append(
            f"- vetoed (added to `exclude_prs`): "
            f"{', '.join(_pr_short(u) for u in newly_excluded)}"
        )
    if failures:
        lines.append(
            f"- ⚠️ **not applied to the session** ({', '.join(failures)}) — "
            "needs manual follow-up; the graph above shows the requested state."
        )
    lines.append("")
    lines.append("The graph above has been updated. Comment again to refine further.")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Misc helpers
# ---------------------------------------------------------------------------


def _resolve_sha(repo_path: Path, ref: str) -> str:
    res = run_git(["rev-parse", ref], repo_path, check=False)
    if res.returncode != 0:
        return ""
    return res.stdout.strip()
