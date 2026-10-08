"""PR dependency DAG discovery: engine behind ``releasy graph discover`` and ``graph update``."""

from __future__ import annotations

import atexit
import re
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import yaml

from releasy.ai_resolve import (
    AIResolveContext,
    _MISSING_PREREQS_RE,
    _fill_placeholders,
    _parse_missing_prereqs,
    _prompt_path,
    attempt_ai_resolve,
    synthesize_text,
)
from releasy.config import Config
from releasy.git_ops import (
    abort_in_progress_op,
    append_commit_trailer,
    ensure_remote,
    ensure_work_repo,
    fetch_commit,
    fetch_remote,
    is_operation_in_progress,
    local_branch_exists,
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
    minimize_comment,
    parse_cherry_picked_refs,
    parse_follow_up_refs,
    parse_pr_url,
    search_pr_urls,
    slug_to_https_url,
    update_issue,
)
from releasy.pipeline import (
    FeatureUnit,
    _SOURCE_PR_URL_RE,
    _cherry_pick_pr,
    discover_feature_units,
    hold_map,
)
from releasy.state import (
    FeatureState,
    PipelineState,
    find_merged_feature_for_prs,
    load_state,
    mark_outdated,
    save_state,
)
from releasy.termlog import get_console

console = get_console()


@dataclass
class _PickOutcome:
    clean: bool
    conflict_files: list[str]
    error_message: str | None = None
    # Index into ``unit.feature_unit.prs`` of the PR whose cherry-pick failed.
    conflicting_pr_idx: int | None = None
    cache_branch: str | None = None


@dataclass
class _CandidateUnit:
    """A unit (singleton or group) under consideration during dep discovery.

    ``is_group``: the unit cherry-picks several PRs as one. ``is_user_group``:
    the group is hand-curated in ``pr_sources.groups`` and must not be
    rewritten. They differ for overlay (auto) groups.
    """
    unit_id: str
    is_group: bool
    is_user_group: bool
    prs: list[PRInfo]
    earliest_merged_at: str | None
    feature_unit: FeatureUnit


@dataclass
class DAGNode:
    unit_id: str
    is_user_group: bool
    pr_urls: list[str]
    pr_titles: list[str]
    earliest_merged_at: str | None
    deps: list[str]
    # "trial-clean" | "git-graph" | "git-graph+claude" |
    # "ai-resolve" | "ai-resolve-clean" | "depth-cutoff" | "grouped" |
    # "reused-group-member" | "graph-update" | "graph-update-unanalysed"
    discovery_method: str
    conflict_files_at_discovery: list[str] = field(default_factory=list)
    # A port branch was preserved at ``feature/<base>/<unit_id>`` for ``run`` to reuse.
    cached: bool = False
    # Merge-commit SHAs parallel to ``pr_urls``; detects re-merged PRs on re-discovery.
    merge_shas: list[str] = field(default_factory=list)


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
    candidate_pr_count: int
    skipped_already_in_target: list[str]
    nodes: list[DAGNode]
    components: list[DAGComponent]
    singletons: list[str]
    warnings: list[str] = field(default_factory=list)
    # Diff of auto-discovered unit IDs vs the existing deps overlay file.
    refresh_removed: list[str] = field(default_factory=list)
    refresh_added: list[str] = field(default_factory=list)
    issue_number: int | None = None
    issue_url: str | None = None
    # ``created_at`` of the newest issue comment already ingested; default ``--since``.
    last_ingested_at: str | None = None
    # Member-vetoed PRs [{"url","reason"}]; enforced via exclude_prs.
    excluded: list[dict] = field(default_factory=list)


def _resolve_base_branch(config: Config, onto: str | None) -> str:
    if onto:
        return config.base_branch_name(onto)
    if config.target_branch:
        return config.target_branch
    raise ValueError(
        "cannot resolve base branch — pass --onto or set target_branch in config.yaml"
    )


def _carried_session_fields(prior: DiscoveryReport | None) -> dict:
    """Session fields (issue link, ingest cursor, vetoes) every rebuilt report inherits."""
    if prior is None:
        return {}
    return {
        "issue_number": prior.issue_number,
        "issue_url": prior.issue_url,
        "last_ingested_at": prior.last_ingested_at,
        "excluded": list(prior.excluded),
    }


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
    redo: bool = False,
    open_issue: bool = False,
    issue_title: str | None = None,
) -> DiscoveryReport:
    """Run dep discovery; write the report (and the deps overlay unless --no-write)."""
    base_branch = _resolve_base_branch(config, onto)

    wd = config.resolve_work_dir(work_dir)
    repo_path, _ = ensure_work_repo(config, wd)
    if is_operation_in_progress(repo_path):
        raise RuntimeError(
            f"main repo {repo_path} has an in-progress git op (cherry-pick "
            f"/ merge / rebase) — finish or abort it first, then re-run "
            "graph discover."
        )
    # Not ``repo_path.parent``: repo_path may equal work_dir, whose parent is outside it.
    scratch_parent = wd
    scratch_parent.mkdir(parents=True, exist_ok=True)

    remote = config.origin.remote_name
    console.print(f"  [dim]Fetching {remote}...[/dim]")
    fetch_remote(repo_path, remote)
    # Explicit fetch fails fast if base_branch is missing on origin.
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

    # --no-write is a true dry run: no deps file, no cache branches.
    cache_enabled = deps_overlay_path is not None
    origin_slug = get_origin_repo_slug(config)

    # Incremental by default: reuse prior units, trial-pick only new PRs.
    # If the base moved, groupings are reused but cached branches are stale.
    report_path = output_path or _default_report_path(config, base_branch)
    warnings_acc: list[str] = []
    # Read even under --redo / --no-write: it carries the session fields.
    prior_report: DiscoveryReport | None = None
    target_moved = False
    if report_path.exists():
        try:
            prior_report = load_report(report_path)
        except Exception:
            # A corrupt prior report degrades to a full re-scan.
            prior_report = None
    reuse_prior = prior_report if (cache_enabled and not redo) else None
    reuse_index: dict[str, DAGNode] = {}
    prior_groups: list[DAGNode] = []
    if reuse_prior is not None:
        target_moved = reuse_prior.target_sha != target_sha
        if target_moved:
            warnings_acc.append(
                f"base {base_branch} moved since last discover "
                f"({reuse_prior.target_sha[:8]} → {target_sha[:8]}); reused "
                "picks may be stale — run `graph discover --redo` to rebuild."
            )
            console.print(
                "  [yellow]base moved since last discover — reusing prior "
                "picks (may be stale); --redo to rebuild[/yellow]"
            )
        else:
            console.print(
                "  [dim]incremental: reusing units from the previous run "
                "(only new PRs are trial-picked)[/dim]"
            )
        for n in reuse_prior.nodes:
            if n.discovery_method == "grouped":
                prior_groups.append(n)
            # Overlay groups merged back into pr_sources.groups return as one
            # candidate unit carrying the group id.
            reuse_index[n.unit_id] = n

    previous_auto_unit_ids: set[str] = (
        _read_previous_overlay_auto_ids(deps_overlay_path)
        if deps_overlay_path is not None else set()
    )

    units = discover_feature_units(config)
    candidates = _build_candidate_unit_set(units, config)
    if pr_limit is not None and len(candidates) > pr_limit:
        candidates = candidates[-pr_limit:]  # most-recent N (sorted newest-last)

    candidate_pr_urls = {p.url for cu in candidates for p in cu.prs}

    console.print(
        f"  [dim]{len(candidates)} candidate unit(s); checking which are "
        f"already in {base_branch}…[/dim]"
    )

    state = load_state(config)
    state_already = _state_already_in_target(candidates, state)
    # Port branches `run` tracks: discovery never resets or deletes them.
    run_branches = {
        fs.branch_name for fs in state.features.values() if fs.branch_name
    }
    trailer_already = _trailer_scan(repo_path, target_ref, candidate_pr_urls)
    cherry_already = _git_cherry_already(
        repo_path, target_ref, candidates, warnings_acc,
    )
    pr_in_target: set[str] = state_already | trailer_already | cherry_already

    fully_merged_units: set[str] = set()
    for cu in candidates:
        if all(p.url in pr_in_target for p in cu.prs):
            fully_merged_units.add(cu.unit_id)

    active_for_traversal = [
        cu for cu in candidates if cu.unit_id not in fully_merged_units
    ]
    active_unit_ids = {cu.unit_id for cu in active_for_traversal}
    console.print(
        f"  [dim]{len(fully_merged_units)} already in target · "
        f"{len(active_for_traversal)} to trial-pick[/dim]"
    )

    # Only index merge SHAs present locally: an unknown SHA in
    # ``git log --not ...`` makes git error out (cross-repo PRs aren't fetched).
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

    carried_pr_url_to_unit = _carried_pr_url_index(candidates, pr_url_to_unit)

    nodes: dict[str, DAGNode] = {}
    edges: set[tuple[str, str]] = set()
    reused: list[str] = []
    by_unit_id: dict[str, _CandidateUnit] = {cu.unit_id: cu for cu in candidates}
    merge_containment_cache: dict[str, str] | None = None

    # Unchanged prior groups skip member trial-picks; their prereq chain is
    # re-injected so the component collapse rebuilds them.
    url_to_sha = {
        p.url: (p.merge_commit_sha or "")
        for cu in candidates for p in cu.prs
    }
    reused_group_member_ids, preseeded_edges, prior_group_pr_urls = (
        _reusable_prior_groups(
            prior_groups, pr_url_to_unit, fully_merged_units, url_to_sha,
        )
    )

    cap = (
        max_depth if max_depth is not None
        else config.ai_resolve.auto_add_prerequisite_prs.max_prereq_depth
    )

    scratch = _open_scratch_worktree(repo_path, scratch_parent, target_ref)
    try:
        # Oldest first, so an in-set prereq is always picked before its
        # dependents. Pulled upstream prereqs are appended with depth+1.
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

        def _checkpoint() -> None:
            """Persist units discovered so far (plus unreached prior nodes) so a killed run resumes."""
            carried = [
                n for n in (reuse_prior.nodes if reuse_prior else [])
                if n.unit_id not in nodes
            ]
            snapshot = DiscoveryReport(
                base_branch=base_branch,
                target_sha=target_sha,
                generated_at=datetime.now(timezone.utc).isoformat(
                    timespec="seconds",
                ),
                candidate_unit_count=len(candidates),
                candidate_pr_count=sum(len(cu.prs) for cu in candidates),
                skipped_already_in_target=sorted(fully_merged_units),
                nodes=sorted(
                    list(nodes.values()) + carried, key=_node_sort_key,
                ),
                components=[],
                singletons=[],
                warnings=warnings_acc,
                **_carried_session_fields(prior_report),
            )
            _write_report(snapshot, report_path)

        while queue:
            _checkpoint()
            unit_id, depth = queue.pop(0)
            if unit_id in nodes:
                continue
            cu = by_unit_id.get(unit_id)
            if cu is None:
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
                nodes[unit_id] = _make_node(
                    cu, deps=[], method="depth-cutoff",
                    conflict_files=[],
                )
                console.print(f"  [dim]· {unit_id}: depth-cutoff[/dim]")
                continue

            if unit_id in reused_group_member_ids:
                nodes[unit_id] = _make_node(
                    cu, deps=[], method="reused-group-member",
                    conflict_files=[],
                )
                reused.append(unit_id)
                console.print(f"  [dim]· {unit_id}: reused (group member)[/dim]")
                continue

            # Reuse an unchanged prior unit: ``cached`` only while its branch
            # is anchored to the target tip; a conflicted unit is reused for
            # its traced deps; a stale branch is dropped so ``run`` re-picks.
            cache_br = _cache_branch_name(base_branch, unit_id)
            prior_n = reuse_index.get(unit_id)
            if (
                prior_n is not None
                and _is_reusable_unit(prior_n, cu, active_unit_ids)
            ):
                branch_live = (
                    local_branch_exists(repo_path, cache_br)
                    and _branch_anchored_to(repo_path, cache_br, target_ref)
                )
                if branch_live or target_moved or not prior_n.cached:
                    if (
                        not branch_live
                        and cache_br not in run_branches
                        and local_branch_exists(repo_path, cache_br)
                    ):
                        run_git(["branch", "-D", cache_br], repo_path, check=False)
                    nodes[unit_id] = _make_node(
                        cu, deps=list(prior_n.deps),
                        method=prior_n.discovery_method,
                        conflict_files=list(prior_n.conflict_files_at_discovery),
                        cached=branch_live,
                    )
                    if prior_n.discovery_method == "grouped":
                        # Tell the group cache builder the combined branch is unchanged.
                        prior_group_pr_urls[unit_id] = list(prior_n.pr_urls)
                    for dep in prior_n.deps:
                        edges.add((unit_id, dep))
                    reused.append(unit_id)
                    if branch_live:
                        suffix = ""
                    elif target_moved:
                        suffix = " (base moved — run re-picks)"
                    else:
                        suffix = (
                            f" (conflicts with {', '.join(prior_n.deps)} — "
                            "run resolves)"
                        )
                    console.print(f"  [dim]· {unit_id}: reused{suffix}[/dim]")
                    continue

            # A branch `run` owns is trial-picked detached instead.
            cache_branch = (
                cache_br
                if cache_enabled and cache_br not in run_branches else None
            )
            outcome = _trial_pick_unit(
                scratch, cu, target_ref,
                config=config,
                cache_branch=cache_branch,
                is_group=cu.is_group,
                origin_slug=origin_slug,
            )
            cache_kept = False

            if outcome.clean:
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
                carried_pr_url_to_unit=carried_pr_url_to_unit,
                merge_containment=merge_containment_cache,
                exclude_unit_ids={unit_id},
                already_in_target_units=fully_merged_units,
            )

            method = "git-graph"

            if use_ai:
                if cand_dep_unit_ids:
                    confirmed = _ask_claude_for_prereqs(
                        config, cu, outcome.conflict_files,
                        cand_dep_unit_ids, by_unit_id, base_branch,
                        warnings_acc,
                    )
                    if confirmed is not None:
                        cand_dep_unit_ids = confirmed
                        method = "git-graph+claude"
                elif (
                    cache_branch
                    and outcome.conflicting_pr_idx is not None
                    and outcome.conflict_files
                ):
                    # Nothing traced: hand the conflict preserved on the
                    # cache branch to the AI resolver. Skipped under --no-write.
                    fb = _ai_resolve_fallback(
                        config, scratch, base_branch, cache_branch, cu,
                        outcome.conflicting_pr_idx,
                        pr_url_to_unit, carried_pr_url_to_unit,
                        fully_merged_units,
                        outcome.conflict_files, warnings_acc,
                    )
                    if fb is None:
                        warnings_acc.append(
                            f"unit {unit_id!r}: AI resolver could not "
                            "classify the conflict; deps left empty"
                        )
                    else:
                        cand_dep_unit_ids = fb.deps
                        method = fb.method or "git-graph"
                        cache_kept = fb.resolved
                        pulled: set[str] = set()
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
                                pulled.add(ext_url)
                                edges.add((unit_id, new_cu.unit_id))
                                if new_cu.unit_id not in nodes:
                                    queue.append((new_cu.unit_id, depth + 1))
                                console.print(
                                    f"  [dim]· {unit_id}: pulled upstream "
                                    f"prereq {new_cu.unit_id}[/dim]"
                                )
                        unresolved = [
                            u for u in fb.external_prereq_urls
                            if u not in pulled
                        ]
                        if unresolved:
                            warnings_acc.append(
                                f"unit {unit_id!r}: prereq(s) "
                                f"{', '.join(unresolved)} are in no unit and "
                                "were not pulled in — the graph cannot order "
                                "around them; add them to the session or "
                                "merge them into the base branch"
                            )
                            console.print(
                                f"  [yellow]![/yellow] {unit_id}: "
                                f"{len(unresolved)} unprovided prereq(s): "
                                f"{', '.join(unresolved)}"
                            )

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
            for d in deps:
                edges.add((unit_id, d))

        # The last unit's result isn't covered by the top-of-loop write.
        _checkpoint()

        if reused:
            console.print(
                f"  [dim]reused {len(reused)} unit(s) from the "
                "previous run[/dim]"
            )

        edges |= preseeded_edges

        edges |= _declared_edges(candidates, nodes, warnings_acc)
        edges |= _follow_up_edges(
            candidates, nodes, pr_url_to_unit, carried_pr_url_to_unit,
            warnings_acc,
        )

        sort_keys = _sort_keys_from_candidates(by_unit_id)
        edges = _break_cycles(edges, sort_keys, warnings_acc)
        components, _singletons = _components(nodes, edges, sort_keys)
        folded, components = _collapse_components_to_groups(
            nodes, components, warnings_acc,
        )
        if cache_enabled:
            _build_group_cache_branches(
                scratch, base_branch, target_ref, nodes, by_unit_id,
                origin_slug, warnings_acc, config=config, repo_path=repo_path,
                reusable_group_urls=({} if target_moved else prior_group_pr_urls),
                run_branches=run_branches,
            )
            for uid in folded:
                folded_br = _cache_branch_name(base_branch, uid)
                if folded_br not in run_branches:
                    run_git(["branch", "-D", folded_br], repo_path, check=False)
        if folded:
            console.print(
                f"  [dim]grouped {len(folded)} unit(s) into combined port(s)[/dim]"
            )
        _in_component = {uid for c in components for uid in c.unit_ids}
        singletons = sorted(
            n.unit_id for n in nodes.values()
            if len(n.pr_urls) == 1
            and not n.is_user_group
            and n.unit_id not in _in_component
        )
    finally:
        _close_scratch_worktree(repo_path, scratch)

    # A prior unit whose PRs have all landed keeps its node so the graph
    # issue still shows it as merged; its deps are dropped.
    carried_merged_urls: set[str] = set()
    for pn in (prior_report.nodes if prior_report is not None else []):
        if pn.unit_id in nodes or not pn.pr_urls:
            continue
        if all(url in pr_in_target for url in pn.pr_urls):
            nodes[pn.unit_id] = replace(pn, deps=[])
            carried_merged_urls.update(pn.pr_urls)

    skipped = sorted(
        uid for uid in fully_merged_units
        if uid not in nodes
        and not all(p.url in carried_merged_urls for p in by_unit_id[uid].prs)
    )
    if include_already_merged:
        for uid in skipped:
            if uid not in nodes:
                cu = by_unit_id[uid]
                nodes[uid] = _make_node(
                    cu, deps=[], method="trial-clean",
                    conflict_files=[],
                )

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
        **_carried_session_fields(prior_report),
    )

    # Overlay first so a write failure lands in report.warnings (same list).
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
            _report_outdated(mark_outdated_units(config, report))
    elif config.session and config.session.session_path:
        from releasy.config import resolve_deps_file_path
        target = resolve_deps_file_path(
            config.session.session_path,
            config.session.pr_sources.deps_file,
        )
        console.print(
            f"  [dim]--no-write: skipping deps overlay "
            f"(would have written to {target})[/dim]"
        )

    output_path = report_path

    # Before the final write so issue_number persists in the same report.
    if open_issue:
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


def _build_candidate_unit_set(
    units: list[FeatureUnit], config: Config,
) -> list[_CandidateUnit]:
    """Wrap feature units as candidates, sorted oldest-merged first."""
    out: list[_CandidateUnit] = []
    for u in units:
        merged = [p.merged_at for p in u.prs if p.merged_at]
        earliest = min(merged) if merged else None
        out.append(_CandidateUnit(
            unit_id=u.feature_id,
            is_group=u.is_group,
            is_user_group=u.is_group and not u.auto_discovered,
            prs=list(u.prs),
            earliest_merged_at=earliest,
            feature_unit=u,
        ))
    out.sort(key=lambda c: (
        c.earliest_merged_at or "9999",
        c.prs[0].number if c.prs else 0,
    ))
    return out


def _carried_pr_url_index(
    candidates: list[_CandidateUnit], pr_url_to_unit: dict[str, str],
) -> dict[str, str]:
    """PRs cherry-picked *inside* a candidate's combined port → its unit id.

    Never overrides ``pr_url_to_unit``: a unit listing a PR outright wins.
    """
    out: dict[str, str] = {}
    for cu in candidates:
        for p in cu.prs:
            for owner, repo, number in parse_cherry_picked_refs(
                p.body, p.repo_slug,
            ):
                url = f"https://github.com/{owner}/{repo}/pull/{number}"
                if url in pr_url_to_unit or url in out:
                    continue
                out[url] = cu.unit_id
    return out


def _declared_edges(
    candidates: list[_CandidateUnit],
    nodes: dict[str, DAGNode],
    warnings_acc: list[str],
) -> set[tuple[str, str]]:
    """Edges hand-declared via user groups' ``depends_on`` (overlay groups excluded)."""
    out: set[tuple[str, str]] = set()
    for cu in candidates:
        if not cu.is_user_group or cu.unit_id not in nodes:
            continue
        for dep in cu.feature_unit.depends_on:
            if dep in nodes:
                out.add((cu.unit_id, dep))
            else:
                warnings_acc.append(
                    f"user group {cu.unit_id!r} declares depends_on {dep!r}, "
                    "which is not a unit in this run (merged, excluded, or a "
                    "typo); edge not applied"
                )
    return out


def _follow_up_edges(
    candidates: list[_CandidateUnit],
    nodes: dict[str, DAGNode],
    pr_url_to_unit: dict[str, str],
    carried_pr_url_to_unit: dict[str, str],
    warnings_acc: list[str],
) -> set[tuple[str, str]]:
    """Edges from a PR body declaring it a follow-up for another PR."""
    out: set[tuple[str, str]] = set()
    for cu in candidates:
        if cu.unit_id not in nodes:
            continue
        for p in cu.prs:
            for owner, repo, number in parse_follow_up_refs(
                p.body, p.repo_slug,
            ):
                url = f"https://github.com/{owner}/{repo}/pull/{number}"
                dep = pr_url_to_unit.get(url) or carried_pr_url_to_unit.get(url)
                if dep == cu.unit_id:
                    continue
                if dep in nodes:
                    out.add((cu.unit_id, dep))
                elif dep is None:
                    warnings_acc.append(
                        f"{p.url} is a follow-up for {url}, which is not a "
                        "candidate in this run (already in target, excluded, "
                        "or not selected); edge not applied"
                    )
    return out


def _state_already_in_target(
    candidates: list[_CandidateUnit], state: PipelineState,
) -> set[str]:
    """Candidate PR URLs whose feature is recorded as merged in state."""
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
    """Candidate URLs found in ``Source-PR:`` trailers of target's recent history."""
    if not candidate_urls:
        return set()
    rev_range = f"{target_ref}~2000..{target_ref}"
    # Falls back to full history if target has fewer than 2000 commits.
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
    """PR URLs whose own commits all have a patch-id equivalent in target (``git cherry``).

    The walk is limited to the PR's commits (merge: ``p1..p2``; squash /
    rebase: just the merge SHA) so unrelated history can't match, and every
    commit must match. Rebase-merged multi-commit PRs are under-checked.
    """
    out: set[str] = set()
    for cu in candidates:
        for p in cu.prs:
            sha = p.merge_commit_sha
            if not sha:
                continue
            # Cross-repo PRs may not have been fetched.
            check = run_git(
                ["cat-file", "-e", sha], repo_path, check=False,
            )
            if check.returncode != 0:
                continue

            parents_res = run_git(
                ["rev-list", "--parents", "-n", "1", sha],
                repo_path, check=False,
            )
            if parents_res.returncode != 0 or not parents_res.stdout.strip():
                continue
            parts = parents_res.stdout.strip().split()
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
                continue
            if cherry.returncode != 0:
                continue

            lines = [
                line.strip() for line in cherry.stdout.splitlines()
                if line.strip()
            ]
            if not lines:
                continue
            if all(line.startswith("- ") for line in lines):
                out.add(p.url)
    return out


# Scratch worktree path → "already cleaned" cell shared by atexit and explicit close.
_SCRATCH_CLEANUP_FLAGS: dict[str, list[bool]] = {}


def _open_scratch_worktree(
    repo_path: Path, scratch_parent: Path, target_ref: str,
) -> Path:
    """Create a detached scratch worktree at ``target_ref``; atexit removes it as a fallback."""
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


_AUTO_GRP_PREFIX = "auto-grp-"


def _auto_group_id(lead_unit_id: str) -> str:
    """Group id from the lead (prereq-most) member's id; idempotent for auto groups."""
    if lead_unit_id.startswith(_AUTO_GRP_PREFIX):
        return lead_unit_id
    return f"{_AUTO_GRP_PREFIX}{lead_unit_id}"


def _cache_branch_name(base_branch: str, unit_id: str) -> str:
    """Same naming as :meth:`Config.feature_branch_name`, so ``run`` picks the branch up."""
    return f"feature/{base_branch}/{unit_id}"


def _trial_pick_unit(
    scratch: Path, unit: _CandidateUnit, target_ref: str,
    *,
    config: Config,
    cache_branch: str | None,
    is_group: bool,
    origin_slug: str | None,
) -> _PickOutcome:
    """Cherry-pick every PR of the unit onto ``cache_branch`` the way ``run`` does.

    On conflict the worktree is left in conflict state on ``cache_branch``
    for the AI fallback; the caller does cleanup. With no ``cache_branch``
    the pick runs detached and is always reset.
    """
    prs = _ordered_prs_for_pick(unit)

    if cache_branch is None:
        try:
            for idx, p in enumerate(prs):
                res = _cherry_pick_pr(scratch, config, p)
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

    run_git(["checkout", "-B", cache_branch, target_ref], scratch, check=False)

    for idx, p in enumerate(prs):
        res = _cherry_pick_pr(scratch, config, p)
        if not res.success:
            return _PickOutcome(
                clean=False,
                conflict_files=list(res.conflict_files),
                error_message=res.error_message,
                conflicting_pr_idx=idx,
                cache_branch=cache_branch,
            )
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
    """Reset scratch to a clean detached ``target_ref``; delete the branch unless ``keep``."""
    abort_in_progress_op(scratch)
    # Detach so the branch (if kept) isn't holding a checkout lock.
    run_git(["checkout", "--detach", target_ref], scratch, check=False)
    run_git(["clean", "-fdx"], scratch, check=False)
    if branch_name and not keep:
        run_git(["branch", "-D", branch_name], scratch, check=False)


def _ordered_prs_for_pick(unit: _CandidateUnit) -> list[PRInfo]:
    """The unit's PRs in cherry-pick order (already sorted by ``_build_group_units``)."""
    return list(unit.feature_unit.prs)


def _build_merge_containment_map(
    repo_path: Path, target_ref: str,
    candidates: list[_CandidateUnit], warnings_acc: list[str],
) -> dict[str, str]:
    """``{commit_sha: enclosing_merge_sha}`` for the branch commits of each candidate merge."""
    containment: dict[str, str] = {}
    for cu in candidates:
        for p in cu.prs:
            mc = p.merge_commit_sha
            if not mc:
                continue
            chk = run_git(["cat-file", "-e", mc], repo_path, check=False)
            if chk.returncode != 0:
                continue
            parents_res = run_git(
                ["rev-list", "--parents", "-n", "1", mc],
                repo_path, check=False,
            )
            if parents_res.returncode != 0 or not parents_res.stdout.strip():
                continue
            parts = parents_res.stdout.strip().split()
            if len(parts) < 3:
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
    carried_pr_url_to_unit: dict[str, str],
    merge_containment: dict[str, str],
    exclude_unit_ids: set[str],
    already_in_target_units: set[str],
) -> list[str]:
    """Candidate unit IDs whose not-yet-in-target commits touched the conflict files."""
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
                carried_pr_url_to_unit=carried_pr_url_to_unit,
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
    carried_pr_url_to_unit: dict[str, str],
    merge_containment: dict[str, str],
) -> str | None:
    """Unit owning ``sha``: candidate merge SHA, then ``Source-PR:`` trailer, then containment."""
    if sha in candidate_merge_shas:
        uid = merge_sha_to_unit.get(sha)
        if uid is not None:
            return uid

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
                if url in carried_pr_url_to_unit:
                    return carried_pr_url_to_unit[url]

    enclosing = merge_containment.get(sha)
    if enclosing:
        uid = merge_sha_to_unit.get(enclosing)
        if uid is not None:
            return uid

    return None


def _ask_claude_for_prereqs(
    config: Config,
    unit: _CandidateUnit,
    conflict_files: list[str],
    candidate_unit_ids: list[str],
    by_unit_id: dict[str, _CandidateUnit],
    base_branch: str,
    warnings_acc: list[str],
) -> list[str] | None:
    """Ask Claude to confirm the traced candidate deps; ``None`` means keep them as-is."""
    prompt_path = _prompt_path(config, "prompts/discover_prereqs.md")
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
            # Claude may name a PR carried inside this unit's combined port.
            for owner, repo, number in parse_cherry_picked_refs(
                p.body, p.repo_slug,
            ):
                url_to_unit.setdefault(
                    f"https://github.com/{owner}/{repo}/pull/{number}", cuid,
                )
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

    rendered = _fill_placeholders(template, placeholders)

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

    # No marker line means malformed, not "no prereqs".
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
    deps: list[str]
    # The resolver produced a resolution; keep the cache branch.
    resolved: bool
    # "ai-resolve" (prereqs found) | "ai-resolve-clean" (resolved, no prereqs) | None
    method: str | None
    # Named prereq URLs outside the candidate set (upstream pull-in candidates).
    external_prereq_urls: list[str] = field(default_factory=list)


def _ai_resolve_fallback(
    config: Config,
    scratch: Path,
    base_branch: str,
    cache_branch: str,
    unit: _CandidateUnit,
    conflicting_pr_idx: int,
    pr_url_to_unit: dict[str, str],
    carried_pr_url_to_unit: dict[str, str],
    already_in_target_units: set[str],
    conflict_files: list[str],
    warnings_acc: list[str],
) -> _AIFallbackResult | None:
    """Hand the existing conflict state to the AI resolver; ``None`` if it failed without info."""
    try:
        prs = unit.feature_unit.prs
        if not (0 <= conflicting_pr_idx < len(prs)):
            return None
        conflicting_pr = prs[conflicting_pr_idx]

        ctx = AIResolveContext(
            port_branch=cache_branch,
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
            confirmed: list[str] = []
            external: list[str] = []
            seen: set[str] = set()
            for url in result.missing_prereq_prs:
                uid = pr_url_to_unit.get(url)
                if uid is None:
                    # A combined port in the set may carry it: an in-set dep.
                    uid = carried_pr_url_to_unit.get(url)
                    if uid is not None and uid != unit.unit_id:
                        warnings_acc.append(
                            f"unit {unit.unit_id!r}: prereq {url} is carried "
                            f"by {uid!r} (combined port) — treating as an "
                            "in-set dependency"
                        )
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
                # MISSING_PREREQS always pairs with success=False.
                resolved=False,
                method="ai-resolve",
                external_prereq_urls=external,
            )

        if result.success:
            for w in result.warnings:
                warnings_acc.append(
                    f"unit {unit.unit_id!r}: cached AI resolution kept with "
                    f"a failing postcondition: {' '.join(w.split())}"
                )
            return _AIFallbackResult(
                deps=[], resolved=True, method="ai-resolve-clean",
            )

        return None
    except Exception as e:  # pragma: no cover — defensive
        warnings_acc.append(
            f"unit {unit.unit_id!r}: AI-resolve fallback crashed: {e}"
        )
        return None


def _is_cross_repo(cu: _CandidateUnit, origin_slug: str | None) -> bool:
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
    """Fetch an out-of-set upstream prereq PR and register it as a unit; ``None`` on failure."""
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
        unit_id=feature_id, is_group=False, is_user_group=False, prs=[pr],
        earliest_merged_at=pr.merged_at, feature_unit=fu,
    )
    by_unit_id[feature_id] = cu
    pr_url_to_unit[url] = feature_id
    merge_sha_to_unit[pr.merge_commit_sha] = feature_id
    return cu


def _components(
    nodes: dict[str, DAGNode],
    edges: set[tuple[str, str]],
    sort_keys: dict[str, tuple[str, int]],
) -> tuple[list[DAGComponent], list[str]]:
    """Weakly-connected components with at least one edge, and edgeless singletons."""
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
    """Topo-sort a component so deps come first (edge ``(a, b)``: a depends on b)."""
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
        leftover = [n for n in comp_nodes if n not in out]
        out.extend(sorted(leftover))
    return out


def _sort_key(
    sort_keys: dict[str, tuple[str, int]], unit_id: str,
) -> tuple[str, int]:
    """Topo/cycle tie-break key ``(merged_at, pr_number)`` for ``unit_id``."""
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
    """Tarjan's articulation points, iterative to avoid the recursion limit on long chains."""
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
        stack: list[tuple[str, "iter"]] = [(root, iter(sorted(adj[root])))]
        while stack:
            u, it = stack[-1]
            v = next(it, None)
            if v is None:
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
        if children_count.get(root, 0) > 1:
            art.add(root)
    return art


def _break_cycles(
    edges: set[tuple[str, str]],
    sort_keys: dict[str, tuple[str, int]],
    warnings_acc: list[str],
) -> set[tuple[str, str]]:
    """Break 2-cycles, keeping the newer-depends-on-older edge."""
    out = set(edges)
    seen_pairs: set[tuple[str, str]] = set()
    for (a, b) in sorted(edges):
        pair = (a, b) if a < b else (b, a)
        if pair in seen_pairs:
            continue
        if (b, a) in out and a != b:
            seen_pairs.add(pair)
            ka = _sort_key(sort_keys, a)
            kb = _sort_key(sort_keys, b)
            if ka == kb:
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
        merge_shas=[p.merge_commit_sha or "" for p in cu.prs],
    )


def _node_sort_key(node: DAGNode) -> tuple[str, str]:
    return (node.earliest_merged_at or "9999", node.unit_id)


def _is_reusable_unit(
    prior_node: DAGNode, cu: _CandidateUnit, active_unit_ids: set[str],
) -> bool:
    """Can a prior node be reused without re-trial-picking?

    Needs the same PRs in the same order with the same merge SHAs, and an
    outcome that still holds: a cached branch, or traced deps that are all
    still active. A conflict that traced no deps is retried.
    """
    if prior_node.pr_urls != [p.url for p in cu.prs]:
        return False
    if not (prior_node.cached or prior_node.deps):
        return False
    if not set(prior_node.deps) <= active_unit_ids:
        return False
    return bool(prior_node.merge_shas) and (
        prior_node.merge_shas == [p.merge_commit_sha or "" for p in cu.prs]
    )


def _reusable_prior_groups(
    prior_groups: list[DAGNode],
    pr_url_to_unit: dict[str, str],
    fully_merged_units: set[str],
    url_to_sha: dict[str, str],
) -> tuple[set[str], set[tuple[str, str]], dict[str, list[str]]]:
    """Prior groups whose member PRs are all active with unchanged merge SHAs.

    Returns ``(reused_member_ids, preseeded_edges, prior_group_pr_urls)``:
    the preseeded ``member[i] → member[i-1]`` chain lets the component
    collapse rebuild the same group.
    """
    reused_member_ids: set[str] = set()
    preseeded_edges: set[tuple[str, str]] = set()
    prior_group_pr_urls: dict[str, list[str]] = {}
    for g in prior_groups:
        if not g.merge_shas or len(g.merge_shas) != len(g.pr_urls):
            continue
        member_uids: list[str] = []
        ok = True
        for url, sha in zip(g.pr_urls, g.merge_shas):
            uid = pr_url_to_unit.get(url)
            if (uid is None or uid in fully_merged_units
                    or url_to_sha.get(url, "") != sha):
                ok = False
                break
            if uid not in member_uids:
                member_uids.append(uid)
        if not ok or len(member_uids) < 2:
            continue
        gid = _auto_group_id(member_uids[0])
        prior_group_pr_urls[gid] = list(g.pr_urls)
        reused_member_ids.update(member_uids)
        for i in range(1, len(member_uids)):
            preseeded_edges.add((member_uids[i], member_uids[i - 1]))
    return reused_member_ids, preseeded_edges, prior_group_pr_urls


def _branch_anchored_to(repo_path: Path, branch: str, base_ref: str) -> bool:
    """True if ``base_ref`` is an ancestor of ``branch``."""
    return run_git(
        ["merge-base", "--is-ancestor", base_ref, branch],
        repo_path, check=False,
    ).returncode == 0


def _collapse_components_to_groups(
    nodes: dict[str, DAGNode],
    components: list[DAGComponent],
    warnings_acc: list[str],
) -> tuple[set[str], list[DAGComponent]]:
    """Merge each all-auto component into one group node, in topo (prereq-first) order.

    Components containing a user group are kept unmerged. Mutates ``nodes``;
    returns ``(folded_member_ids, kept_components)``.
    """
    folded: set[str] = set()
    kept_components: list[DAGComponent] = []
    for comp in components:
        member_ids = [uid for uid in comp.unit_ids if uid in nodes]
        auto_ids = [uid for uid in member_ids if not nodes[uid].is_user_group]
        user_ids = [uid for uid in member_ids if nodes[uid].is_user_group]
        if user_ids:
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
        merge_shas: list[str] = []
        merged_ats: list[str] = []
        for uid in auto_ids:  # comp.unit_ids is topo order: prereq first
            n = nodes[uid]
            pr_urls.extend(n.pr_urls)
            pr_titles.extend(n.pr_titles)
            merge_shas.extend(n.merge_shas)
            if n.earliest_merged_at:
                merged_ats.append(n.earliest_merged_at)
        gid = _auto_group_id(auto_ids[0])
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
            merge_shas=merge_shas,
        )
    return folded, kept_components


def _ensure_member_commits(
    scratch: Path, prs: list[PRInfo], origin_slug: str | None,
) -> list[str]:
    """Fetch missing merge commits (cross-repo from their own repo); return refs still missing."""
    missing: list[str] = []
    for p in prs:
        sha = p.merge_commit_sha
        if not sha:
            missing.append(f"{p.repo_slug}#{p.number}")
            continue
        if run_git(["cat-file", "-e", sha], scratch, check=False).returncode == 0:
            continue
        if origin_slug is None or p.repo_slug != origin_slug:
            fetch_commit(scratch, slug_to_https_url(p.repo_slug), sha)
        if run_git(["cat-file", "-e", sha], scratch, check=False).returncode != 0:
            missing.append(sha[:8])
    return missing


def _build_group_cache_branches(
    scratch: Path,
    base_branch: str,
    target_ref: str,
    nodes: dict[str, DAGNode],
    by_unit_id: dict[str, _CandidateUnit],
    origin_slug: str | None,
    warnings_acc: list[str],
    *,
    config: Config,
    repo_path: Path,
    reusable_group_urls: dict[str, list[str]] | None = None,
    run_branches: set[str] | frozenset[str] = frozenset(),
) -> None:
    """Build each collapsed group's combined branch; keep it (``cached``) only if clean.

    ``reusable_group_urls`` (gid → prior member urls) skips groups whose
    membership is unchanged and whose branch is still anchored.
    """
    reusable_group_urls = reusable_group_urls or {}
    url_to_pr: dict[str, PRInfo] = {
        p.url: p for cu in by_unit_id.values() for p in cu.prs
    }
    for node in [n for n in nodes.values() if n.discovery_method == "grouped"]:
        cache_branch = _cache_branch_name(base_branch, node.unit_id)
        if cache_branch in run_branches:
            continue
        if (
            reusable_group_urls.get(node.unit_id) == node.pr_urls
            and local_branch_exists(repo_path, cache_branch)
            and _branch_anchored_to(repo_path, cache_branch, target_ref)
        ):
            node.cached = True
            console.print(
                f"  [dim]· {node.unit_id}: group unchanged — reused cache[/dim]"
            )
            continue
        prs = [url_to_pr[u] for u in node.pr_urls if u in url_to_pr]
        if len(prs) != len(node.pr_urls):
            warnings_acc.append(
                f"group {node.unit_id!r}: could not resolve all member PRs; "
                "not caching (run will build it)"
            )
            continue
        # Skip up front so a missing commit isn't misreported as a conflict.
        missing = _ensure_member_commits(scratch, prs, origin_slug)
        if missing:
            warnings_acc.append(
                f"group {node.unit_id!r}: member commit(s) {', '.join(missing)} "
                "not fetchable locally; not cached — `run` will fetch + build"
            )
            continue
        group_cu = _CandidateUnit(
            unit_id=node.unit_id,
            is_group=True,
            is_user_group=False,
            prs=prs,
            earliest_merged_at=node.earliest_merged_at,
            feature_unit=FeatureUnit(
                feature_id=node.unit_id, prs=prs, if_exists="skip",
                is_group=True, group_id=node.unit_id, auto_discovered=True,
            ),
        )
        outcome = _trial_pick_unit(
            scratch, group_cu, target_ref, config=config,
            cache_branch=cache_branch, is_group=True, origin_slug=origin_slug,
        )
        if outcome.clean:
            _release_cache_branch(scratch, target_ref, cache_branch, keep=True)
            node.cached = True
            console.print(
                f"  [dim]· {node.unit_id}: group builds clean — cached[/dim]"
            )
        else:
            _release_cache_branch(scratch, target_ref, cache_branch, keep=False)
            node.cached = False
            reason = (
                f"combined cherry-pick conflicts ({len(outcome.conflict_files)} "
                "file(s))"
                if outcome.conflict_files
                else "combined cherry-pick failed (member empty/already applied)"
            )
            warnings_acc.append(
                f"group {node.unit_id!r}: {reason}; not "
                "cached — `run` will resolve it"
            )


def _default_report_path(config: Config, base_branch: str) -> Path:
    """``<config-dir>/graph.<base-branch>.yaml``."""
    return config.config_path.parent / f"graph.{base_branch}.yaml"


def _read_previous_overlay_auto_ids(overlay_path: Path) -> set[str]:
    """Auto-discovered unit IDs in an existing deps overlay; empty on any error."""
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
                "merge_shas": n.merge_shas or None,
            }.items()
            if v is not None
        }
        for n in report.nodes
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    # Atomic: a kill mid-checkpoint must not leave a truncated report.
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w") as f:
        yaml.dump(data, f, default_flow_style=False, sort_keys=False)
    tmp.replace(path)


def _write_session_overlay(
    report: DiscoveryReport, overlay_path: Path,
) -> None:
    """Write the deps overlay: auto units in a component, plus multi-PR auto groups."""
    relevant_unit_ids: set[str] = set()
    for c in report.components:
        relevant_unit_ids.update(c.unit_ids)
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
            continue
        entry: dict = {
            "id": uid,
            "prs": list(node.pr_urls),
            "auto_discovered": True,
        }
        if len(node.pr_urls) > 1:
            # prs are already in apply (prereq-first) order.
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
    """Inverse of :func:`_write_report`."""
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
            merge_shas=list(nd.get("merge_shas", []) or []),
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
    """Recompute components and singletons from the report nodes' ``deps``."""
    node_map = {n.unit_id: n for n in report.nodes}
    edges: set[tuple[str, str]] = {
        (n.unit_id, dep)
        for n in report.nodes
        for dep in n.deps
        if dep in node_map
    }
    sort_keys = _sort_keys_from_nodes(report.nodes)
    return _components(node_map, edges, sort_keys)


def _issue_marker(base_branch: str) -> str:
    return f"<!-- releasy-graph:{base_branch} -->"


# Marker on RelEasy's own comments so graph update skips them on ingest.
_GRAPH_BOT_MARKER = "<!-- releasy-graph-bot -->"

_PROGRESS_MARKER: dict[str, str] = {
    "needs_review": "🟡 in review",
    "branch_created": "🟠 branch pushed, no PR yet",
    "conflict": "🔴 conflict",
    "build_failed": "🚧 build failed",
    "skipped": "⏭ skipped",
    "merged": "✅ merged",
    "blocked": "⏸ blocked",
    "closed": "⛔ PR closed unmerged",
    "superseded": "♻ superseded",
    "reverted": "↩ reverted (do not re-port)",
}

_PROGRESS_SUMMARY_ORDER: tuple[str, ...] = (
    "merged", "needs_review", "branch_created", "build_failed",
    "conflict", "blocked", "closed", "superseded", "reverted", "skipped",
)

_NOT_STARTED_MARKER = "⬜ not started"

# Not a BranchStatus: a hold prefixes the progress note instead of replacing it.
_HOLD_MARKER = "⏸ on hold"

# Statuses whose group is folded shut in the issue.
_FOLDED_STATUSES: frozenset[str] = frozenset({"merged"})

# Terminal statuses listed (in this order) under the folded "Discarded" section.
_DISCARDED_STATUSES: tuple[str, ...] = ("closed", "skipped", "superseded")

# Gets its own unfolded section so nobody re-ports it.
_REVERTED_STATUS = "reverted"


def mark_outdated_units(config: Config, report: DiscoveryReport) -> list[str]:
    """Mark tracked ports carrying PRs their unit in ``report`` no longer has.

    Returns the unit IDs whose port is outdated (marked now or before).
    """
    state = load_state(config)
    out: list[str] = []
    changed = False
    for n in report.nodes:
        fs = state.features.get(n.unit_id)
        if fs is None:
            continue
        kept = {parse_pr_url(u) for u in n.pr_urls}
        dynamic = {parse_pr_url(u) for u in fs.dynamic_prereq_urls}
        lost = [
            u for u in (fs.pr_urls or [fs.pr_url])
            if u and parse_pr_url(u) not in kept | dynamic
        ]
        if not lost:
            continue
        if mark_outdated(
            fs, f"{', '.join(_pr_short(u) for u in lost)} left the unit",
        ):
            changed = True
        if fs.outdated:
            out.append(n.unit_id)
    if changed and not config.dry_run:
        save_state(state, config)
    return out


def build_progress_map(
    report: DiscoveryReport, state: PipelineState,
) -> dict[str, FeatureState]:
    """Map each graph unit to its state entry, by unit_id then by source-PR URL."""
    by_pr: dict[tuple, FeatureState] = {}
    for fs in state.features.values():
        for url in ([fs.pr_url] if fs.pr_url else []) + list(fs.pr_urls):
            key = parse_pr_url(url) if url else None
            if key is not None:
                by_pr.setdefault(key, fs)

    out: dict[str, FeatureState] = {}
    for n in report.nodes:
        fs = state.features.get(n.unit_id)
        if fs is None:
            for url in n.pr_urls:
                key = parse_pr_url(url)
                if key is not None and key in by_pr:
                    fs = by_pr[key]
                    break
        if fs is not None:
            out[n.unit_id] = fs
    return out


def _unit_ported(fs: FeatureState | None) -> bool:
    """True once releasy has opened a port PR for the unit (not closed/superseded/reverted)."""
    if fs is None:
        return False
    if fs.status in ("superseded", _REVERTED_STATUS):
        return False
    return bool(fs.rebase_pr_url) and fs.status != "closed"


def _picks_landed(fs: FeatureState | None, total: int) -> int:
    """How many of a unit's PRs are committed on its port branch."""
    if fs is None:
        return 0
    if fs.status == "conflict":
        return min(fs.partial_pr_count or 0, total)
    if fs.status in ("merged", "needs_review", "branch_created",
                     "build_failed", "superseded"):
        return total
    return 0  # skipped / blocked / closed


def _stall_note(fs: FeatureState) -> str:
    """`` · <why>`` for a stalled unit, else ``""`` (blocked/skipped markers already say why)."""
    if fs.stall is None or fs.status in ("blocked", "skipped"):
        return ""
    why = fs.stall.summary(max_detail=60)
    if fs.stall.runs > 1:
        why += f" (×{fs.stall.runs} runs)"
    return f" · ⏳ {why}"


def _progress_note(
    fs: FeatureState | None, total: int, *, html: bool = False,
    lead: str = " — ",
) -> str:
    """``<lead><marker> [#N](url) · <why>`` suffix for a unit's issue entry.

    ``html``: for use inside ``<summary>``, where GitHub doesn't parse markdown.
    """
    if fs is None:
        return f"{lead}{_NOT_STARTED_MARKER}"
    marker = _PROGRESS_MARKER.get(fs.status, fs.status)
    if fs.status == "blocked" and fs.blocked_by:
        marker += f" by {', '.join(_code(b, html) for b in fs.blocked_by)}"
    elif fs.status == "skipped" and fs.skip_reason:
        marker += f": {fs.skip_reason}"
    elif fs.status == "superseded" and fs.skip_reason:
        # Superseded reasons open with "superseded"; avoid saying it twice.
        reason = fs.skip_reason
        marker += (
            reason[len("superseded"):] if reason.startswith("superseded")
            else f": {reason}"
        )
    elif fs.status == _REVERTED_STATUS and fs.skip_reason:
        marker += f" · {fs.skip_reason}"
    elif fs.status == "conflict" and total > 1:
        marker += f" · {_picks_landed(fs, total)}/{total} picked"
    note = f"{lead}{marker}"
    if fs.rebase_pr_url:
        short = _pr_short(fs.rebase_pr_url)
        note += (
            f' <a href="{fs.rebase_pr_url}">{short}</a>' if html
            else f" [{short}]({fs.rebase_pr_url})"
        )
    elif fs.branch_url:
        note += (
            f' <a href="{fs.branch_url}">branch</a>' if html
            else f" [branch]({fs.branch_url})"
        )
    stall = _stall_note(fs)
    if fs.outdated:
        stall += f" · ♻ outdated, re-ported on next run: {fs.outdated}"
    return note + (_to_html_inline(stall) if html else stall)


def _code(text: str, html: bool) -> str:
    return f"<code>{text}</code>" if html else f"`{text}`"


def _to_html_inline(text: str) -> str:
    """Escape ``&<>`` and turn backtick spans into ``<code>`` for use inside ``<summary>``."""
    text = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    return re.sub(r"`([^`]+)`", r"<code>\1</code>", text)


def _node_hold_reason(
    node: DAGNode, held: dict[tuple, str] | None,
) -> str | None:
    """Hold reason for ``node`` (any held PR holds it), or ``None``. Mirrors ``pipeline._unit_hold_reason``."""
    if not held:
        return None
    reasons = [
        held[key] for url in node.pr_urls
        if (key := parse_pr_url(url)) is not None and key in held
    ]
    if not reasons:
        return None
    return "; ".join(dict.fromkeys(r for r in reasons if r))


def _hold_note(reason: str, fs: FeatureState | None, total: int) -> str:
    """`` — ⏸ on hold: <why> · <progress>`` suffix; progress only if the unit has state."""
    note = f" — {_HOLD_MARKER}" + (f": {reason}" if reason else "")
    if fs is not None:
        note += _progress_note(fs, total, lead=" · ")
    return note


def _progress_summary(
    report: DiscoveryReport, progress: dict[str, FeatureState],
    held_ids: set[str] | None = None,
) -> str:
    """One-line tally: ported / total, then per-status counts (held units only under on-hold)."""
    held_ids = held_ids or set()
    counts: dict[str, int] = {}
    ported = 0
    for n in report.nodes:
        fs = progress.get(n.unit_id)
        if _unit_ported(fs):
            ported += 1
        if n.unit_id in held_ids:
            continue
        counts[fs.status if fs else ""] = counts.get(fs.status if fs else "", 0) + 1
    bits = [
        f"{_PROGRESS_MARKER.get(s, s)}: {counts[s]}"
        for s in _PROGRESS_SUMMARY_ORDER if counts.get(s)
    ]
    if counts.get(""):
        bits.append(f"{_NOT_STARTED_MARKER}: {counts['']}")
    if held_ids:
        bits.append(f"{_HOLD_MARKER}: {len(held_ids)}")
    total = len(report.nodes)
    line = f"**Progress: {ported}/{total} unit(s) ported**"
    return line + (" — " + " · ".join(bits) if bits else "")


def render_graph_issue_body(
    report: DiscoveryReport,
    progress: dict[str, FeatureState] | None = None,
    held: dict[tuple, str] | None = None,
) -> str:
    """Render a DiscoveryReport as a GitHub issue body (markdown).

    ``progress``: from :func:`build_progress_map`. ``held``: from :func:`pipeline.hold_map`.
    """
    progress = progress or {}
    lines: list[str] = [_issue_marker(report.base_branch)]
    lines.append(f"## Port dependency graph — `{report.base_branch}`")
    lines.append("")
    lines.append(
        f"_Generated by `releasy graph` at {report.generated_at}._"
    )
    lines.append("")
    all_groups = [n for n in report.nodes if len(n.pr_urls) > 1]
    _rank = {s: i for i, s in enumerate(_DISCARDED_STATUSES)}
    discarded = sorted(
        (n for n in report.nodes
         if progress.get(n.unit_id) is not None
         and progress[n.unit_id].status in _rank),
        key=lambda n: _rank[progress[n.unit_id].status],
    )
    reverted = [
        n for n in report.nodes
        if progress.get(n.unit_id) is not None
        and progress[n.unit_id].status == _REVERTED_STATUS
    ]
    parked_ids = {n.unit_id for n in discarded} | {n.unit_id for n in reverted}
    # A held unit whose port already merged stays where it is.
    hold_reasons = {
        n.unit_id: reason
        for n in report.nodes
        if n.unit_id not in parked_ids
        and (reason := _node_hold_reason(n, held)) is not None
        and (
            progress.get(n.unit_id) is None
            or progress[n.unit_id].status != "merged"
        )
    }
    on_hold = [n for n in report.nodes if n.unit_id in hold_reasons]
    parked_ids |= set(hold_reasons)
    groups = [n for n in all_groups if n.unit_id not in parked_ids]
    singles = [
        n for n in report.nodes
        if len(n.pr_urls) == 1 and n.unit_id not in parked_ids
    ]
    headline = (
        f"**{report.candidate_unit_count} unit(s) across "
        f"{report.candidate_pr_count} PR(s)** — {len(all_groups)} group(s), "
        f"{len(report.nodes) - len(all_groups)} standalone. PRs inside a group "
        "port together as one combined PR, cherry-picked in the listed order "
        "(prerequisite first)."
    )
    if discarded:
        headline += (
            f" {len(discarded)} terminal unit(s) sit under **Discarded** at "
            "the bottom."
        )
    if reverted:
        headline += (
            f" {len(reverted)} unit(s) were ported and then **reverted** on "
            "target — see the Reverted section; do not port them again."
        )
    if on_hold:
        headline += (
            f" {len(on_hold)} unit(s) are **on hold** and are not being "
            "ported right now."
        )
    lines.append(headline)
    lines.append("")
    lines.append(_progress_summary(report, progress, set(hold_reasons)))
    lines.append("")
    lines.append(
        "_A box is ticked once releasy has opened the port PR (a "
        "partially-applied group counts — its draft PR is linked). Merged "
        "groups and the Discarded / Excluded lists are folded shut. Run "
        "`releasy graph sync` to refresh._"
    )
    lines.append("")

    def _title_of(n: DAGNode, i: int) -> str:
        return n.pr_titles[i] if i < len(n.pr_titles) and n.pr_titles[i] else ""

    def _box(done: bool) -> str:
        return "[x]" if done else "[ ]"

    def _parked_entry(n: DAGNode, note: str | None = None) -> list[str]:
        """Checkbox-less bullet for a parked unit; ``note`` overrides the progress note."""
        fs = progress.get(n.unit_id)
        total = len(n.pr_urls)
        if total == 1:
            url = n.pr_urls[0]
            return [
                f"- [{_pr_short(url)}]({url}) "
                f"{_title_of(n, 0)}".rstrip()
                + (note if note is not None else _progress_note(fs, 1))
            ]
        out = [
            f"- **`{n.unit_id}`** · {total} PRs"
            + (note if note is not None else _progress_note(fs, total))
        ]
        out += [
            f"  {i + 1}. [{_pr_short(url)}]({url}) "
            f"{_title_of(n, i)}".rstrip()
            for i, url in enumerate(n.pr_urls)
        ]
        return out

    if groups:
        lines.append("### Groups (port together, in apply order)")
        lines.append("")
        for n in groups:
            fs = progress.get(n.unit_id)
            total = len(n.pr_urls)
            landed = _picks_landed(fs, total)
            folded = fs is not None and fs.status in _FOLDED_STATUSES
            glyph = "☑" if _unit_ported(fs) else "☐"
            lines.append("<details>" if folded else "<details open>")
            lines.append(
                f"<summary>{glyph} <b><code>{n.unit_id}</code></b> "
                f"· {total} PRs{_progress_note(fs, total, html=True)}</summary>"
            )
            # Blank line ends the raw-HTML block so the list renders as markdown.
            lines.append("")
            for i, url in enumerate(n.pr_urls):
                lines.append(
                    f"{i + 1}. {_box(i < landed)} [{_pr_short(url)}]({url}) "
                    f"{_title_of(n, i)}".rstrip()
                )
            lines.append("")
            lines.append("</details>")
            lines.append("")

    if singles:
        lines.append("### Standalone PRs")
        lines.append("")
        for n in singles:
            url = n.pr_urls[0]
            fs = progress.get(n.unit_id)
            lines.append(
                f"- {_box(_unit_ported(fs))} [{_pr_short(url)}]({url}) "
                f"{_title_of(n, 0)}".rstrip() + _progress_note(fs, 1)
            )
        lines.append("")

    if on_hold:
        lines.append("### ⏸ On hold — not being ported right now")
        lines.append("")
        lines.append(
            "Parked in `pr_sources.on_hold`: **not vetoed**, just waiting on "
            "something (a follow-up PR, a decision). `releasy run` skips "
            "them, and anything that depends on one reports as blocked. "
            "Comment here to put one back in work (or take it off "
            "`on_hold` in the session file) and it ports on the next run."
        )
        lines.append("")
        for n in on_hold:
            lines += _parked_entry(
                n,
                _hold_note(
                    hold_reasons[n.unit_id],
                    progress.get(n.unit_id),
                    len(n.pr_urls),
                ),
            )
        lines.append("")

    if reverted:
        lines.append("### ↩ Reverted — do NOT re-port")
        lines.append("")
        lines.append(
            "Ported and merged, then the port was **reverted on "
            f"`{report.base_branch}` deliberately**. These are terminal for "
            "releasy: it will not port them again, and it will not reopen "
            "the port PR. Re-add one only if whoever reverted it asks for "
            "it — the revert had a reason."
        )
        lines.append("")
        for n in reverted:
            lines += _parked_entry(n)
        lines.append("")

    in_target = report.skipped_already_in_target
    if discarded or in_target:
        tally = f"{len(discarded)} unit(s)"
        if in_target:
            tally += f" + {len(in_target)} already in target"
        lines.append("<details>")
        lines.append(f"<summary>🗑 <b>Discarded</b> · {tally}</summary>")
        lines.append("")
        for n in discarded:
            lines += _parked_entry(n)
        lines.append("")
        # The report keeps only unit IDs for these, so no links.
        if in_target:
            lines.append(
                "Already in target at discovery time (never ported): "
                + " · ".join(f"`{uid}`" for uid in in_target)
            )
            lines.append("")
        lines.append("</details>")
        lines.append("")

    if report.excluded:
        lines.append("<details>")
        lines.append(
            f"<summary>🚫 <b>Excluded</b> · {len(report.excluded)} PR(s) — "
            "vetoed by members</summary>"
        )
        lines.append("")
        for e in report.excluded:
            url = e.get("url", "")
            reason = e.get("reason", "") or "(no reason given)"
            lines.append(f"- [{_pr_short(url)}]({url}) — {reason}")
        lines.append("")
        lines.append("</details>")
        lines.append("")

    lines.append("---")
    lines.append(
        "Org members can **comment on this issue** to change the graph — "
        "add or veto PRs, put one on hold (or back in work), regroup, or "
        "set ordering — then run `releasy graph update` to apply your "
        "feedback."
    )
    lines.append("")
    lines.append(
        "> Dependencies set by `graph update` are human/AI-asserted, not "
        "trial-pick-verified. Re-run `releasy graph discover` for "
        "conflict-verified dependencies."
    )
    return "\n".join(lines)


def _pr_short(url: str) -> str:
    """``#N`` short label for a PR URL (fallback: url)."""
    m = _PR_NUMBER_RE.search(url)
    return f"#{m.group(1)}" if m else url


def open_or_update_graph_issue(
    config: Config, report: DiscoveryReport, *, title: str,
    progress: dict[str, FeatureState] | None = None,
) -> tuple[int, str] | None:
    """Create the graph issue on origin, or update its body if it exists.

    Sets report.issue_number/issue_url on create. Returns (number, url) or
    None on failure/dry-run. ``progress`` defaults to the current state.
    """
    if progress is None:
        progress = build_progress_map(report, load_state(config))
    body = render_graph_issue_body(report, progress, hold_map(config))
    if report.issue_number is not None:
        res = update_issue(config, report.issue_number, body=body)
        if res is True:
            return report.issue_number, (report.issue_url or "")
        if res is False:
            return None  # transient failure — keep the number, retry later
        # None: issue was deleted; recreate.
        report.issue_number = None
        report.issue_url = None
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


_GRAPH_SPEC_FENCE_RE = re.compile(
    r"```(?:ya?ml)?\s*\n(.*?)```", re.DOTALL,
)

# Appended on a one-shot retry when the first reply had no parseable graph.
_GRAPH_UPDATE_RETRY_SUFFIX = (
    "\n\n---\n\nYour previous reply had no usable graph. Reply with the "
    "COMPLETE new graph as one fenced ```yaml block with a top-level "
    "`units:` list (per Output above) — every unit that should remain, not "
    "a diff or a prose summary. Output the YAML block now."
)


def _comment_is_trusted(comment, config: Config) -> bool:  # noqa: ANN001
    assoc = (comment.author_association or "").upper()
    if assoc in set(config.graph.trusted_associations):
        return True
    login = (comment.author or "").lower()
    return login in {r.lower() for r in config.graph.trusted_reviewers}


def _normalize_spec(parsed: object) -> dict | None:
    """Coerce a parsed block into ``{'units': [...], ...}`` (also from an id→fields map or a bare list)."""
    if isinstance(parsed, dict):
        units = parsed.get("units")
        if isinstance(units, list):
            return parsed
        if isinstance(units, dict):
            out: list[dict] = []
            for uid, fields in units.items():
                d = dict(fields) if isinstance(fields, dict) else {}
                d.setdefault("id", uid)
                out.append(d)
            new = dict(parsed)
            new["units"] = out
            return new
        return None
    if isinstance(parsed, list) and parsed and all(
        isinstance(x, dict) and ("prs" in x or "id" in x) for x in parsed
    ):
        return {"units": parsed}
    return None


def _parse_graph_spec(text: str) -> dict | None:
    """The last fenced YAML block in Claude's reply that normalises to a spec, or None."""
    chosen: dict | None = None
    for block in _GRAPH_SPEC_FENCE_RE.findall(text):
        try:
            parsed = yaml.safe_load(block)
        except yaml.YAMLError:
            continue
        norm = _normalize_spec(parsed)
        if norm is not None:
            chosen = norm
    return chosen


def _handle_comments(comments: list) -> list[tuple[str, object]]:  # noqa: ANN001
    """Assign each comment a stable ``C<n>`` handle."""
    return [(f"C{i}", c) for i, c in enumerate(comments, start=1)]


# A GitHub web PR-list link, e.g. ``https://github.com/o/r/pulls?q=label:x``.
_PR_SEARCH_URL_RE = re.compile(
    r"https://github\.com/([\w.-]+)/([\w.-]+)/(?:pulls|issues)\?[^\s)>\]]+"
)


def _pr_search_query(url: str) -> str | None:
    """Search-API query for a GitHub web PR-list ``url``, or None."""
    m = _PR_SEARCH_URL_RE.fullmatch(url)
    if m is None:
        return None
    q = " ".join(parse_qs(urlparse(url).query).get("q", [])).strip()
    if not q:
        return None
    # The web UI accepts ``state:merged``; the Search API only ``is:merged``.
    q = re.sub(r"\bstate:merged\b", "is:merged", q)
    if not re.search(r"\brepo:", q):
        q = f"repo:{m.group(1)}/{m.group(2)} {q}"
    if not re.search(r"\bis:pr\b", q):
        q += " is:pr"
    return q


def _expand_pr_searches(
    handled: list[tuple[str, object]], warnings_acc: list[str],
) -> dict[str, list[tuple[str, list[str]]]]:
    """Run every PR-search link in the comments: handle → [(link, PR URLs)]."""
    out: dict[str, list[tuple[str, list[str]]]] = {}
    for handle, c in handled:
        for m in _PR_SEARCH_URL_RE.finditer(c.body or ""):
            url = m.group(0)
            query = _pr_search_query(url)
            if query is None:
                continue
            urls = search_pr_urls(query)
            if urls is None:
                warnings_acc.append(
                    f"graph update: could not expand PR search in [{handle}]: {url}"
                )
                continue
            out.setdefault(handle, []).append((url, urls))
    return out


def _render_comments_block(
    handled: list[tuple[str, object]],
    searches: dict[str, list[tuple[str, list[str]]]] | None = None,
) -> str:
    out: list[str] = []
    for handle, c in handled:
        assoc = c.author_association or "?"
        text = (
            f"### [{handle}] Comment by @{c.author or 'unknown'} ({assoc}) "
            f"at {c.created_at}\n{c.body.strip()}"
        )
        for url, urls in (searches or {}).get(handle, []):
            text += f"\n\nPR search {url} currently lists {len(urls)} PR(s):"
            text += "".join(f"\n- {u}" for u in urls) or "\n_(none)_"
        out.append(text)
    return "\n\n".join(out) or "_(none)_"


def _normalize_addressed(value: object) -> set[str]:
    """Normalise Claude's ``addressed`` list into a set of ``C<n>`` handles."""
    if value is None:
        return set()
    items = value if isinstance(value, list) else [value]
    out: set[str] = set()
    for x in items:
        handle = re.sub(r"[^A-Za-z0-9]", "", str(x)).upper()  # "[c1]" → "C1"
        if handle.isdigit():  # bare int "3" → "C3"
            handle = "C" + handle
        if handle:
            out.add(handle)
    return out


def _render_current_graph_block(
    report: DiscoveryReport, held: dict[tuple, str] | None = None,
) -> str:
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
    if held:
        out.append("")
        out.append("Currently on hold:")
        for (owner, repo, num), reason in held.items():
            out.append(
                f"- https://github.com/{owner}/{repo}/pull/{num} — "
                f"{reason or '(no reason recorded)'}"
            )
    return "\n".join(out) or "_(empty graph)_"


def _ask_claude_for_new_graph(
    config: Config,
    report: DiscoveryReport,
    handled: list[tuple[str, object]],
    warnings_acc: list[str],
) -> dict | None:
    """Run the adjust-graph prompt through Claude and parse the spec; None on failure."""
    prompt_path = _prompt_path(config, config.graph.prompt_file)
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
        "current_graph_block": _render_current_graph_block(
            report, hold_map(config),
        ),
        "candidate_pr_list": candidate_pr_list,
        "comments_block": _render_comments_block(
            handled, _expand_pr_searches(handled, warnings_acc),
        ),
    }

    rendered = _fill_placeholders(template, placeholders)

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
    last_text = res.text
    if spec is None:
        retry = synthesize_text(
            config, rendered + _GRAPH_UPDATE_RETRY_SUFFIX,
            label="graph-update-retry",
            timeout_seconds=config.graph.timeout_seconds,
            command=config.graph.command,
        )
        if retry.success and retry.text:
            last_text = retry.text
            spec = _parse_graph_spec(retry.text)

    if spec is None:
        note = "Claude reply had no parseable YAML graph spec; changing nothing"
        reply_path = _default_report_path(config, report.base_branch).with_name(
            f"graph-update-reply.{report.base_branch}.md"
        )
        try:
            reply_path.write_text(last_text, encoding="utf-8")
            note += f". Raw reply saved to {reply_path}"
        except OSError:
            pass
        warnings_acc.append(note)
    return spec


def _fetch_spec_pr_metadata(
    config: Config,
    spec: dict,
    prior_urls: set[str],
    warnings_acc: list[str],
) -> dict[str, PRInfo]:
    """Fetch metadata for PRs the spec introduces that the prior graph never saw."""
    wanted: list[str] = []
    seen: set[str] = set()
    for u in spec.get("units", []) or []:
        if not isinstance(u, dict):
            continue
        for raw_url in u.get("prs", []) or []:
            url = str(raw_url).strip()
            if url in prior_urls or url in seen or parse_pr_url(url) is None:
                continue
            seen.add(url)
            wanted.append(url)
    if not wanted:
        return {}
    console.print(
        f"  [dim]fetching metadata for {len(wanted)} new PR(s)…[/dim]"
    )
    out: dict[str, PRInfo] = {}
    for url in wanted:
        pr = fetch_pr_by_url(config, url, include_closed=True)
        if pr is None:
            warnings_acc.append(
                f"graph update: could not fetch {url} — it enters the graph "
                "without a title or merge date"
            )
            continue
        out[url] = pr
    return out


def _build_report_from_spec(
    prior: DiscoveryReport,
    spec: dict,
    warnings_acc: list[str],
    config: Config | None = None,
    state: PipelineState | None = None,
) -> DiscoveryReport | None:
    """Build a new DiscoveryReport from Claude's spec + the prior graph; None on a cycle.

    With ``config``, new PRs' metadata is fetched. With ``state``, a new PR
    a merged unit already ported is dropped.
    """
    title_map: dict[str, str] = {}
    merged_map: dict[str, str | None] = {}
    sha_map: dict[str, str] = {}
    prior_user_groups = {n.unit_id for n in prior.nodes if n.is_user_group}
    prior_methods = {n.unit_id: n.discovery_method for n in prior.nodes}
    for n in prior.nodes:
        for url, t in zip(n.pr_urls, n.pr_titles or []):
            title_map[url] = t
        for url, sha in zip(n.pr_urls, n.merge_shas or []):
            if sha:
                sha_map[url] = sha
        for url in n.pr_urls:
            merged_map[url] = n.earliest_merged_at
    prior_urls = set(title_map)

    fetched = (
        _fetch_spec_pr_metadata(config, spec, prior_urls, warnings_acc)
        if config is not None else {}
    )

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
        has_new_pr = False
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
                ported = (
                    find_merged_feature_for_prs(state, [url])
                    if state is not None else None
                )
                if ported is not None:
                    fid, fs = ported
                    via = f" ({fs.rebase_pr_url})" if fs.rebase_pr_url else ""
                    warnings_acc.append(
                        f"graph update: PR {url} was already ported by merged "
                        f"{fid}{via}; not adding it again"
                    )
                    continue
                has_new_pr = True
                warnings_acc.append(
                    f"graph update: PR {url} is new (not in the prior graph) "
                    f"— it will be added to the session and ported, but its "
                    f"dependencies were NOT analysed (no trial-pick runs "
                    f"here); run `releasy graph discover` before `run` unless "
                    f"the update declared its depends_on"
                )
            prs.append(url)
            declared_urls.add(url)
        if not prs:
            warnings_acc.append(f"graph update: unit {uid!r} has no valid PRs; skipping")
            continue
        deps = [str(d).strip() for d in (u.get("depends_on", []) or [])]
        if has_new_pr:
            method = "graph-update-unanalysed"
        else:
            method = prior_methods.get(uid) or "graph-update"
        titles: list[str] = []
        merged_ats: list[str] = []
        shas: list[str] = []
        for url in prs:
            pr = fetched.get(url)
            titles.append(pr.title if pr is not None else title_map.get(url, ""))
            merged_at = pr.merged_at if pr is not None else merged_map.get(url)
            if merged_at:
                merged_ats.append(merged_at)
            shas.append(
                (pr.merge_commit_sha if pr is not None else sha_map.get(url))
                or ""
            )
        nodes.append(DAGNode(
            unit_id=uid,
            is_user_group=uid in prior_user_groups,
            pr_urls=prs,
            pr_titles=titles,
            earliest_merged_at=min(merged_ats, default=None),
            deps=deps,
            discovery_method=method,
            cached=False,
            # Element-wise consumers need one SHA per PR or none at all.
            merge_shas=shas if all(shas) else [],
        ))
        seen_ids.add(uid)

    if not nodes:
        warnings_acc.append("graph update: spec produced no units; changing nothing")
        return None

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

    # Prior vetoes + this spec's vetoes, minus any PR re-added to a unit.
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
    for url in declared_urls:
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


def _parse_spec_holds(
    spec: dict, warnings_acc: list[str],
) -> dict[str, str] | None:
    """The spec's ``on_hold`` block as URL → reason; ``None`` if absent (holds untouched)."""
    if "on_hold" not in spec:
        return None
    raw = spec.get("on_hold") or []
    if not isinstance(raw, list):
        warnings_acc.append(
            f"graph update: `on_hold` is not a list "
            f"({type(raw).__name__}); ignoring it (current holds preserved)"
        )
        return None
    out: dict[str, str] = {}
    for e in raw:
        if isinstance(e, str):
            url, reason = e.strip(), ""
        elif isinstance(e, dict):
            url = str(e.get("url", "")).strip()
            reason = str(e.get("reason", "")).strip()
        else:
            warnings_acc.append(
                f"graph update: on_hold entry is not a URL or mapping "
                f"({e!r}); skipping"
            )
            continue
        if not url or parse_pr_url(url) is None:
            warnings_acc.append(
                f"graph update: on_hold entry has bad URL {e!r}; skipping"
            )
            continue
        out[url] = reason
    return out


def _apply_spec_holds(
    config: Config, holds: dict[str, str],
) -> tuple[list[str], list[str], list[str]]:
    """Make ``pr_sources.on_hold`` match ``holds``; returns ``(newly_held, released, failures)``."""
    from releasy import pr_membership

    ps = config.pr_sources
    current = {
        key: url for url in ps.on_hold
        if (key := parse_pr_url(url)) is not None
    }
    wanted = {
        key: url for url in holds
        if (key := parse_pr_url(url)) is not None
    }
    reasons = ps.on_hold_reasons
    newly_held: list[str] = []
    released: list[str] = []
    failures: list[str] = []
    for key, url in wanted.items():
        reason = holds[url]
        was_held = key in current
        if was_held and reasons.get(current[key], "") == reason:
            continue
        if not pr_membership.hold_pr(config, url, reason):
            failures.append(f"hold {_pr_short(url)}")
            continue
        if not was_held:
            newly_held.append(url)
    for key, url in current.items():
        if key in wanted:
            continue
        if not pr_membership.unhold_pr(config, url):
            failures.append(f"release {_pr_short(url)}")
            continue
        released.append(url)
    return newly_held, released, failures


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
    """Write user-group edits back into session groups (creating missing ones); returns count changed."""
    if config.session is None:
        return 0
    from releasy.config import PRGroupConfig

    ps = config.session.pr_sources
    groups_by_id = {g.id: g for g in ps.groups}
    changed = 0
    for n in report.nodes:
        if not n.is_user_group:
            continue
        g = groups_by_id.get(n.unit_id)
        if g is None:
            # if_exists as the session loader would apply it (default would pin "skip").
            g = PRGroupConfig(
                id=n.unit_id, prs=[], if_exists=config.pr_policy.if_exists,
            )
            ps.groups.append(g)
            groups_by_id[n.unit_id] = g
            warnings_acc.append(
                f"graph update: {n.unit_id!r} was flagged a user group with no "
                "matching session group; created it in the session"
            )
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
            "`releasy graph sync --open-issue` to open one from this graph, "
            "then let members comment on it."
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
            continue
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
    handled = _handle_comments(ingest)
    spec = _ask_claude_for_new_graph(config, report, handled, warnings_acc)
    for w in warnings_acc:
        console.print(f"  [yellow]warning:[/yellow] {w}")
    if spec is None:
        return 1

    build_warnings: list[str] = []
    new_report = _build_report_from_spec(
        report, spec, build_warnings, config=config, state=load_state(config),
    )
    for w in build_warnings:
        console.print(f"  [yellow]warning:[/yellow] {w}")
    if new_report is None:
        return 1
    new_report.warnings = build_warnings

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
    hold_warnings: list[str] = []
    spec_holds = _parse_spec_holds(spec, hold_warnings)
    for w in hold_warnings:
        console.print(f"  [yellow]warning:[/yellow] {w}")
    new_report.warnings += hold_warnings

    _print_graph_update_summary(
        new_report, added, excluded_urls,
        sorted(spec_holds) if spec_holds is not None else None,
    )

    if dry_run:
        console.print("[dim](--dry-run: no report / overlay / session / issue writes)[/dim]")
        return 0

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

    newly_held: list[str] = []
    released: list[str] = []
    outdated: list[str] = []
    if spec_holds is not None:
        newly_held, released, hold_failures = _apply_spec_holds(
            config, spec_holds,
        )
        failures += hold_failures

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
            outdated = mark_outdated_units(config, new_report)
            _report_outdated(outdated)

    if open_or_update_graph_issue(
        config, new_report, title=f"Port graph for {base_branch}",
    ) is None:
        console.print("  [yellow]warning:[/yellow] failed to update the graph issue")
    else:
        console.print(f"  [green]✓[/green] updated issue #{new_report.issue_number}")

    # Minimize only addressed comments; the rest stay visible as pending.
    if config.graph.minimize_addressed_comments:
        handle_to_comment = dict(handled)
        addressed = _normalize_addressed(spec.get("addressed"))
        n_min = 0
        for handle in addressed:
            c = handle_to_comment.get(handle)
            if c and c.node_id and minimize_comment(c.node_id, "OUTDATED"):
                n_min += 1
        if n_min:
            console.print(
                f"  [green]✓[/green] marked {n_min} addressed comment(s) as outdated"
            )

    if post_comment:
        summary = _render_update_comment(
            new_report, added, newly_excluded, len(ingest), failures,
            newly_held, released, outdated,
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


def sync_graph_progress(
    config: Config, *, onto: str | None = None, quiet: bool = False,
    open_issue: bool = False,
) -> int:
    """Re-render the graph issue with progress from state; returns an exit code.

    ``quiet`` silences the "no graph / no issue" cases (automatic hook).
    ``open_issue`` opens an issue from the saved graph when it has none.
    """
    try:
        base_branch = _resolve_base_branch(config, onto)
    except ValueError as e:
        if quiet:
            return 0
        console.print(f"[red]graph sync: {e}[/red]")
        return 1

    report_path = _default_report_path(config, base_branch)
    if not report_path.exists():
        if quiet:
            return 0
        console.print(
            f"[red]No graph report at {report_path}.[/red] Run "
            "`releasy graph discover --open-issue` first."
        )
        return 1
    try:
        report = load_report(report_path)
    except (OSError, ValueError, yaml.YAMLError) as e:
        if quiet:
            return 0
        console.print(f"[red]graph sync: unreadable graph report: {e}[/red]")
        return 1

    if report.issue_number is None and not open_issue:
        if quiet:
            return 0
        console.print(
            "[red]The saved graph has no issue.[/red] Re-run as "
            "`releasy graph sync --open-issue` to open one from this graph "
            "— no re-discovery needed."
        )
        return 1

    progress = build_progress_map(report, load_state(config))
    prior_number = report.issue_number
    opening = prior_number is None
    if config.dry_run and not quiet:
        console.print(
            render_graph_issue_body(report, progress, hold_map(config)),
            markup=False, highlight=False,
        )
    res = open_or_update_graph_issue(
        config, report, title=f"Port graph for {base_branch}",
        progress=progress,
    )
    if res is None:
        if opening and config.dry_run:
            # ``create_issue`` returns None on dry run.
            console.print(
                f"  [dim]would open a graph issue for {base_branch}[/dim]"
            )
            return 0
        target = (
            "open the graph issue" if opening
            else f"update graph issue #{prior_number}"
        )
        console.print(f"  [yellow]warning:[/yellow] failed to {target}")
        return 1
    # Freshly opened, or 404'd and recreated — persist the number.
    if report.issue_number != prior_number and not config.dry_run:
        _write_report(report, report_path)

    ported = sum(
        1 for n in report.nodes if _unit_ported(progress.get(n.unit_id))
    )
    verb = (
        "opened" if opening
        else "would refresh" if config.dry_run
        else "refreshed"
    )
    console.print(
        f"  [green]✓[/green] {verb} graph issue #{report.issue_number} — "
        f"{ported}/{len(report.nodes)} unit(s) ported"
    )
    return 0


def _report_outdated(unit_ids: list[str]) -> None:
    for uid in unit_ids:
        console.print(
            f"  [yellow]♻[/yellow] port of [cyan]{uid}[/cyan] is outdated — "
            "the next `releasy run` re-ports it from scratch"
        )


def _print_graph_update_summary(
    report: DiscoveryReport, added: list[str], excluded: list[str],
    on_hold: list[str] | None = None,
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
    if on_hold is not None:
        console.print(
            "  on hold: "
            + (", ".join(_pr_short(u) for u in on_hold) or "(none)")
        )


def _render_update_comment(
    report: DiscoveryReport,
    added: list[str],
    newly_excluded: list[str],
    n_comments: int,
    failures: list[str] | None = None,
    newly_held: list[str] | None = None,
    released: list[str] | None = None,
    outdated: list[str] | None = None,
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
    if newly_held:
        lines.append(
            f"- ⏸ put on hold (added to `on_hold`; still in the graph, not "
            f"ported): {', '.join(_pr_short(u) for u in newly_held)}"
        )
    if released:
        lines.append(
            f"- ▶ back in work (removed from `on_hold`): "
            f"{', '.join(_pr_short(u) for u in released)}"
        )
    if outdated:
        lines.append(
            f"- ♻ outdated ports, re-ported from scratch on the next "
            f"`releasy run`: {', '.join(f'`{u}`' for u in outdated)}"
        )
    if failures:
        lines.append(
            f"- ⚠️ **not applied to the session** ({', '.join(failures)}) — "
            "needs manual follow-up; the graph above shows the requested state."
        )
    lines.append("")
    lines.append("The graph above has been updated. Comment again to refine further.")
    return "\n".join(lines)


def _resolve_sha(repo_path: Path, ref: str) -> str:
    res = run_git(["rev-parse", ref], repo_path, check=False)
    if res.returncode != 0:
        return ""
    return res.stdout.strip()
