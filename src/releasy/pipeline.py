"""Port pipeline: cherry-pick source PRs onto port branches off the base branch and open port PRs."""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from releasy.termlog import console

from releasy.config import (
    Config, FeatureConfig, PortMode, PRSourceConfig,
)
from releasy.git_ops import (
    OperationResult,
    abort_in_progress_op,
    append_commit_trailer,
    branch_exists,
    ensure_remote,
    local_branch_exists,
    remote_branch_exists,
    cherry_pick_merge_commit,
    commit_cherry_pick_conflict_as_is,
    create_branch_from_ref,
    ensure_work_repo,
    fetch_commit,
    fetch_pr_ref,
    fetch_remote,
    force_push,
    is_operation_in_progress,
    run_git,
    stash_and_clean,
    update_submodules,
)
from releasy.github_ops import (
    CHERRY_PICK_FROM_RE,
    PRInfo,
    add_label_to_pr,
    close_pull_request,
    create_pull_request,
    ensure_label,
    fetch_open_prs_with_commits_to_base,
    fetch_pr_by_number,
    fetch_pr_by_url,
    find_pr_for_branch,
    get_origin_repo_slug,
    mark_pr_ready_for_review,
    parse_cherry_picked_refs,
    parse_pr_url,
    pr_has_label,
    pr_ref_label,
    remove_label_from_pr,
    require_origin_repo_slug,
    same_pr_url,
    search_prs_by_labels,
    slug_to_https_url,
    sync_project,
    update_pull_request,
)
from releasy.state import (
    BLOCKING_STALL_KINDS,
    CAPPED_STALL_KINDS,
    FeatureState,
    PipelineState,
    StallReason,
    clear_conflict_markers,
    OUTDATABLE_STATUSES,
    find_merged_feature_for_prs,
    load_state,
    make_stall,
    save_state,
)

if TYPE_CHECKING:
    from releasy.build_verify import VerifyResult


def _persist_state(config: Config, state: PipelineState) -> None:
    """Save state and, when pushing, sync the project board."""
    if config.dry_run:
        return
    save_state(state, config)
    if config.push:
        sync_project(config, state)


def _dry_record(state: PipelineState, action: str) -> None:
    """Count a planned dry-run action (no-op unless ``run_pipeline`` seeded ``state._dry_run_actions``)."""
    counts = getattr(state, "_dry_run_actions", None)
    if counts is not None:
        counts[action] = counts.get(action, 0) + 1


def _setup_repo(
    config: Config, work_dir: Path | None, base_branch: str | None = None,
) -> Path:
    """Set up the work repo and fetch origin; on a fresh clone, check out ``base_branch`` and init submodules."""
    wd = config.resolve_work_dir(work_dir)
    console.print(f"[dim]Working directory: {wd}[/dim]")

    console.print("[dim]Setting up repository...[/dim]")
    repo_path, freshly_cloned = ensure_work_repo(config, wd)
    console.print(f"[dim]Repo: {repo_path}[/dim]")

    console.print(f"Fetching [cyan]{config.origin.remote_name}[/cyan]...", end=" ")
    fetch_remote(repo_path, config.origin.remote_name)
    console.print("[green]done[/green]")

    if freshly_cloned and base_branch:
        remote = config.origin.remote_name
        base_ref = f"{remote}/{base_branch}"
        if branch_exists(repo_path, base_branch, remote):
            console.print(
                f"Checking out base branch [cyan]{base_branch}[/cyan]...", end=" ",
            )
            run_git(
                ["checkout", "-B", base_branch, base_ref], repo_path, check=False,
            )
            console.print("[green]done[/green]")
        console.print(
            "[dim]Initialising submodules (this can take a few minutes)...[/dim]",
        )
        update_submodules(repo_path)
        console.print("[green]Submodules ready[/green]")

    return repo_path


def _push(config: Config, repo_path: Path, branch: str) -> None:
    force_push(repo_path, branch, config)


@dataclass
class FeatureUnit:
    """One PR or a sequential group of PRs (in cherry-pick order) ported together."""
    feature_id: str
    prs: list[PRInfo]
    if_exists: str
    title_prefix: str = ""
    is_group: bool = False
    group_id: str | None = None
    # Group comes from the deps_file overlay rather than ``pr_sources.groups``.
    auto_discovered: bool = False
    # Extra note for the AI conflict-resolver prompt (from by_labels/groups ``ai_context``).
    ai_context: str = ""
    # PR URL → per-PR ai_context, combined with ``ai_context``.
    per_pr_ai_context: dict[str, str] = field(default_factory=dict)
    # ``pr_sources.on_hold`` reason (``""`` if none); ``None`` = not held.
    hold_reason: str | None = None
    # Per-run bookkeeping set by _process_feature_unit.
    ai_resolved_count: int = 0
    ai_iterations_total: int = 0
    # Summed Claude cost (incl. changelog synthesis); ``None`` until a cost is reported.
    ai_cost_usd_total: float | None = None
    verify_needs_attention: bool = False
    verify_findings: list[str] = field(default_factory=list)
    # Detected from the primary PR; groups are assumed homogeneous.
    mode: PortMode = "forward_port"
    # AI-synthesized CHANGELOG entry; ``None`` = use the source PRs' own entries.
    synthesized_changelog: str | None = None
    # Source-PR URLs already on the existing port branch (``if_exists: append``).
    applied_pr_urls: set[str] = field(default_factory=set)
    # Auto-continue attempts including this run, for ``max_partial_continue_attempts``.
    partial_continue_attempts: int = 0
    # Unit IDs that must be merged in target before this unit is processed.
    depends_on: list[str] = field(default_factory=list)
    # Overrides the canonical port branch (``releasy run --redo``).
    port_branch: str | None = None

    @property
    def sort_key(self) -> tuple[str, int]:
        """Earliest merged_at across constituent PRs, fallback to PR number."""
        merged = [pr.merged_at for pr in self.prs if pr.merged_at]
        first = min(merged) if merged else "9999"
        return (first, min(pr.number for pr in self.prs))

    def primary_pr(self) -> PRInfo:
        return self.prs[0]


PRRef = tuple[str, str, int]


@dataclass
class OnlyFilter:
    """``--only`` / ``--pr`` filter by PR ref or group/feature ID; ``soft`` (``--pr``) makes no-match a clean exit."""
    raw: str
    pr_ref: PRRef | None
    name: str | None
    soft: bool = False

    @property
    def label(self) -> str:
        if self.pr_ref is not None:
            owner, repo, num = self.pr_ref
            return f"{owner}/{repo}#{num}"
        return self.name or self.raw

    def matches_unit(self, unit: "FeatureUnit") -> bool:
        if self.pr_ref is not None:
            return any(pr.ref() == self.pr_ref for pr in unit.prs)
        return unit.feature_id == self.name or unit.group_id == self.name

    def matches_state(self, fid: str, fs: FeatureState) -> bool:
        if self.pr_ref is not None:
            for url in (fs.rebase_pr_url, fs.pr_url, *fs.pr_urls):
                if not url:
                    continue
                if parse_pr_url(url) == self.pr_ref:
                    return True
            return False
        return fid == self.name


def parse_only(only: str | None) -> OnlyFilter | None:
    """Parse ``--only`` as a PR URL or a group / feature ID."""
    if not only:
        return None
    only = only.strip()
    if not only:
        return None
    parsed = parse_pr_url(only)
    if parsed is not None:
        return OnlyFilter(raw=only, pr_ref=parsed, name=None)
    if only.startswith(("http://", "https://")):
        raise ValueError(
            f"--only={only!r} looks like a URL but isn't a "
            "https://github.com/<owner>/<repo>/pull/<N> link."
        )
    return OnlyFilter(raw=only, pr_ref=None, name=only)


def parse_pr_url_filter(pr: str | None) -> OnlyFilter | None:
    """Parse ``--pr <URL>`` into a soft :class:`OnlyFilter`."""
    if not pr:
        return None
    raw = pr.strip()
    if not raw:
        return None
    parsed = parse_pr_url(raw)
    if parsed is None:
        raise ValueError(
            f"--pr={pr!r} is not a GitHub PR URL "
            "(expected https://github.com/<owner>/<repo>/pull/<N>)."
        )
    return OnlyFilter(raw=raw, pr_ref=parsed, name=None, soft=True)


def _detect_port_mode(
    config: Config,
    pr: PRInfo,
    *,
    explicit_mode: str = "auto",
) -> PortMode:
    """Explicit override → forward_port_labels → cross-origin → upstream configured → forward_port."""
    if explicit_mode in ("backport", "forward_port"):
        return explicit_mode

    if config.session is not None:
        fp_labels_lower = {
            l.lower() for l in config.session.pr_sources.forward_port_labels
        }
        if fp_labels_lower:
            pr_labels_lower = {(l or "").lower() for l in (pr.labels or [])}
            if fp_labels_lower & pr_labels_lower:
                return "forward_port"

    origin_slug = get_origin_repo_slug(config)
    if origin_slug and pr.repo_slug != origin_slug:
        return "backport"

    if config.upstream is not None:
        return "backport"

    return "forward_port"


def _singleton_feature_id(pr: PRInfo, origin_slug: str | None) -> str:
    """``pr-<N>`` for origin PRs, ``<owner>-<repo>-pr-<N>`` for other repos."""
    if origin_slug and pr.repo_slug == origin_slug:
        return f"pr-{pr.number}"
    owner, repo = pr.repo_slug.split("/", 1)
    return f"{owner}-{repo}-pr-{pr.number}"


def hold_map(config: Config) -> dict[PRRef, str]:
    """``pr_sources.on_hold`` as PR ref → reason (``""`` if none); unparseable URLs dropped."""
    out: dict[PRRef, str] = {}
    reasons = config.pr_sources.on_hold_reasons
    for url in config.pr_sources.on_hold:
        parsed = parse_pr_url(url)
        if parsed is not None:
            out[parsed] = reasons.get(url, "")
    return out


def _unit_hold_reason(
    unit: "FeatureUnit", holds: dict[PRRef, str],
) -> str | None:
    """Joined hold reasons of ``unit``'s PRs, or ``None``; one held PR holds the whole group."""
    reasons = [
        holds[ref] for pr in unit.prs
        if (ref := pr.ref()) in holds
    ]
    if not reasons:
        return None
    return "; ".join(dict.fromkeys(r for r in reasons if r))


def _author_filter_reason(
    author: str | None,
    included_authors: set[str],
    excluded_authors: set[str],
) -> str | None:
    """Reason to drop a PR by author, or ``None``; a non-empty ``included_authors`` is an allowlist."""
    login = (author or "").lower()
    if excluded_authors and login and login in excluded_authors:
        return f"author @{author} is in pr_sources.exclude_authors"
    if included_authors:
        if not login:
            return (
                "author is unknown but pr_sources.include_authors is set"
            )
        if login not in included_authors:
            return (
                f"author @{author} is not in pr_sources.include_authors"
            )
    return None


def _build_singleton_units(
    config: Config,
    collected: dict[PRRef, tuple[PRInfo, PRSourceConfig]],
) -> list[FeatureUnit]:
    units: list[FeatureUnit] = []
    origin_slug = get_origin_repo_slug(config)
    include_pr_contexts = config.pr_sources.include_pr_contexts
    for pr, src in collected.values():
        unit_ai_context = src.ai_context or include_pr_contexts.get(pr.url, "")
        units.append(FeatureUnit(
            feature_id=_singleton_feature_id(pr, origin_slug),
            prs=[pr],
            if_exists=src.if_exists,
            title_prefix=src.description,
            ai_context=unit_ai_context,
            mode=_detect_port_mode(config, pr, explicit_mode=src.mode),
        ))
    return units


def _build_group_units(
    config: Config,
    excluded_pr_refs: set[PRRef],
    excluded_labels: set[str],
    included_authors: set[str],
    excluded_authors: set[str],
) -> tuple[list[FeatureUnit], set[PRRef]]:
    """Materialise group units; also returns the PR refs they claim (not to be ported as singletons)."""
    origin_slug = get_origin_repo_slug(config)
    units: list[FeatureUnit] = []
    claimed: set[PRRef] = set()
    for group in config.pr_sources.groups:
        console.print(
            f"\n  Resolving group [yellow]{group.id}[/yellow] "
            f"({len(group.prs)} PR(s))"
        )
        group_prs: list[PRInfo] = []
        for url in group.prs:
            parsed = parse_pr_url(url)
            if parsed is None:
                console.print(f"    [red]✗[/red] Bad PR URL: {url}")
                continue
            owner, repo, num = parsed
            ref_label = pr_ref_label(f"{owner}/{repo}", num, origin_slug)
            if parsed in excluded_pr_refs:
                console.print(
                    f"    [yellow]−[/yellow] {ref_label} excluded via "
                    "pr_sources.exclude_prs, dropping from group"
                )
                continue
            pr = fetch_pr_by_number(config, num, slug=f"{owner}/{repo}")
            if pr is None:
                console.print(f"    [red]✗[/red] Could not fetch PR {ref_label}")
                continue
            if excluded_labels and (set(pr.labels) & excluded_labels):
                console.print(
                    f"    [yellow]−[/yellow] {ref_label} carries an excluded "
                    "label, dropping from group"
                )
                continue
            reason = _author_filter_reason(
                pr.author, included_authors, excluded_authors,
            )
            if reason is not None:
                console.print(
                    f"    [yellow]−[/yellow] {ref_label} {reason}, "
                    "dropping from group"
                )
                continue
            group_prs.append(pr)
            claimed.add(parsed)
            console.print(
                f"    [dim]+ {ref_label}[/dim] {pr.title} [{pr.state}]"
            )
        if not group_prs:
            console.print(
                f"    [yellow]Group {group.id!r} has no PRs left after "
                "filtering, skipping[/yellow]"
            )
            continue
        if group.sort == "merged_at":
            group_prs.sort(key=lambda p: (p.merged_at or "9999", p.number))
            console.print(
                f"    [dim]Sorted by merged_at: "
                f"{', '.join(pr_ref_label(pr.repo_slug, pr.number, origin_slug) for pr in group_prs)}[/dim]"
            )
        units.append(FeatureUnit(
            feature_id=group.id,
            prs=group_prs,
            if_exists=group.if_exists,
            title_prefix=group.description,
            is_group=True,
            group_id=group.id,
            auto_discovered=group.auto_discovered,
            ai_context=group.ai_context,
            per_pr_ai_context=dict(group.pr_ai_contexts),
            depends_on=list(group.depends_on),
            mode=_detect_port_mode(
                config, group_prs[0], explicit_mode=group.mode,
            ),
        ))
    return units, claimed


def _prune_superseded_singletons(config: Config, state: PipelineState) -> bool:
    """Drop singleton state entries for PRs now in a ``pr_sources.groups`` entry.

    An in-flight singleton's open port PR is closed as superseded unless the
    group is on hold. Branches are left on origin. Returns True if any entry was removed.
    """
    group_of: dict[PRRef, str] = {}
    group_feature_ids: set[str] = set()
    holds = hold_map(config)
    held_groups: set[str] = set()
    for group in config.pr_sources.groups:
        group_feature_ids.add(group.id)
        for url in group.prs:
            parsed = parse_pr_url(url)
            if parsed is not None:
                group_of.setdefault(parsed, group.id)
                if parsed in holds:
                    held_groups.add(group.id)
    group_pr_refs = set(group_of)

    if not group_pr_refs:
        return False

    origin_slug = get_origin_repo_slug(config)
    stale: list[tuple[str, FeatureState, PRRef]] = []
    for fid, fs in state.features.items():
        if fid in group_feature_ids:
            continue  # the group's own state entry
        if len(fs.pr_numbers) > 1:
            continue  # a different multi-PR unit
        if not fs.pr_url:
            continue
        parsed = parse_pr_url(fs.pr_url)
        if parsed is not None and parsed in group_pr_refs:
            stale.append((fid, fs, parsed))

    for fid, fs, ref in stale:
        owner, repo, num = ref
        ref_label = pr_ref_label(f"{owner}/{repo}", num, origin_slug)
        gid = group_of[ref]
        console.print(
            f"  [yellow]⚠[/yellow] Dropping stale singleton "
            f"[cyan]{fid}[/cyan] (PR {ref_label} is now in group "
            f"[cyan]{gid}[/cyan])"
        )
        if fs.rebase_pr_url and fs.status in OUTDATABLE_STATUSES:
            if gid in held_groups:
                console.print(
                    f"    [dim]group is on hold — port PR {fs.rebase_pr_url} "
                    "left open[/dim]"
                )
            elif _close_port_pr_if_open(
                config, fs.rebase_pr_url,
                f"Superseded: {ref_label} is now ported as part of group "
                f"`{gid}`.",
            ) is None:
                console.print(
                    f"    [red]✗[/red] could not close port PR "
                    f"{fs.rebase_pr_url} — close it manually"
                )
        del state.features[fid]

    return bool(stale)


def discover_feature_units(config: Config) -> list["FeatureUnit"]:
    """Filtered, dependency-ordered units defined by ``config.pr_sources`` (GitHub only, no git)."""
    origin_slug = get_origin_repo_slug(config)

    collected: dict[PRRef, tuple[PRInfo, PRSourceConfig]] = {}
    for pr_source in config.pr_sources.by_labels:
        labels_str = ", ".join(pr_source.labels)
        filter_str = " (merged only)" if pr_source.merged_only else ""
        console.print(
            f"\n  Searching for PRs with labels "
            f"[yellow]{labels_str}[/yellow]{filter_str}"
        )
        prs = search_prs_by_labels(config, pr_source.labels, pr_source.merged_only)

        if not prs:
            console.print("    [dim]No PRs found[/dim]")
            continue

        console.print(f"    Found {len(prs)} PR(s)")
        for pr in prs:
            ref = pr.ref()
            if ref not in collected:
                collected[ref] = (pr, pr_source)

    prs_cfg = config.pr_sources
    include_pr_refs: set[PRRef] = {
        parsed
        for url in prs_cfg.include_prs
        if (parsed := parse_pr_url(url)) is not None
    }
    exclude_pr_refs: set[PRRef] = {
        parsed
        for url in prs_cfg.exclude_prs
        if (parsed := parse_pr_url(url)) is not None
    }

    if prs_cfg.exclude_labels:
        exclude_set = set(prs_cfg.exclude_labels)
        before = len(collected)
        collected = {
            ref: (pr, src)
            for ref, (pr, src) in collected.items()
            if not (set(pr.labels) & exclude_set) or ref in include_pr_refs
        }
        removed = before - len(collected)
        if removed:
            console.print(
                f"\n  [dim]Excluded {removed} PR(s) by label filter "
                f"({', '.join(prs_cfg.exclude_labels)})[/dim]"
            )

    included_authors = {a.lower() for a in prs_cfg.include_authors if a}
    excluded_authors = {a.lower() for a in prs_cfg.exclude_authors if a}
    if included_authors or excluded_authors:
        kept: dict[PRRef, tuple[PRInfo, PRSourceConfig]] = {}
        dropped: list[tuple[PRInfo, str]] = []
        for ref, (pr, src) in collected.items():
            if ref in include_pr_refs:
                kept[ref] = (pr, src)
                continue
            reason = _author_filter_reason(
                pr.author, included_authors, excluded_authors,
            )
            if reason is None:
                kept[ref] = (pr, src)
            else:
                dropped.append((pr, reason))
        collected = kept
        if dropped:
            console.print(
                f"\n  [dim]Excluded {len(dropped)} PR(s) by author filter:"
                "[/dim]"
            )
            for pr, reason in dropped:
                ref_label = pr_ref_label(
                    pr.repo_slug, pr.number, origin_slug,
                )
                console.print(f"    [dim]− {ref_label}: {reason}[/dim]")

    if include_pr_refs:
        default_source = (
            config.pr_sources.by_labels[0]
            if config.pr_sources.by_labels
            else PRSourceConfig(labels=[], if_exists=config.pr_policy.if_exists)
        )
        for ref in sorted(include_pr_refs):
            if ref in collected:
                continue
            owner, repo, pr_num = ref
            ref_label = pr_ref_label(f"{owner}/{repo}", pr_num, origin_slug)
            console.print(f"\n  Fetching explicitly included PR {ref_label}...")
            pr_info = fetch_pr_by_number(config, pr_num, slug=f"{owner}/{repo}")
            if pr_info:
                collected[ref] = (pr_info, default_source)
                console.print(f"    [green]✓[/green] {pr_info.title}")
            else:
                console.print(f"    [red]✗[/red] Could not fetch PR {ref_label}")

    for ref in exclude_pr_refs:
        if ref in collected:
            pr_info, _ = collected.pop(ref)
            owner, repo, pr_num = ref
            ref_label = pr_ref_label(f"{owner}/{repo}", pr_num, origin_slug)
            console.print(
                f"\n  [dim]Excluded PR {ref_label} ({pr_info.title})[/dim]"
            )

    excluded_label_set = set(prs_cfg.exclude_labels)
    group_units, claimed_pr_refs = _build_group_units(
        config, exclude_pr_refs, excluded_label_set,
        included_authors, excluded_authors,
    )
    for ref in claimed_pr_refs:
        if ref in collected:
            pr_info, _ = collected.pop(ref)
            owner, repo, pr_num = ref
            ref_label = pr_ref_label(f"{owner}/{repo}", pr_num, origin_slug)
            console.print(
                f"  [dim]{ref_label} ({pr_info.title}) belongs to a group — "
                "removed from singletons[/dim]"
            )

    units: list[FeatureUnit] = (
        _build_singleton_units(config, collected) + group_units
    )
    _mark_held_units(config, units, origin_slug)
    return _topo_sort_units(units)


def _mark_held_units(
    config: Config, units: list[FeatureUnit], origin_slug: str | None,
) -> None:
    """Stamp ``hold_reason`` on held units; they stay in the list so the graph keeps them."""
    holds = hold_map(config)
    if not holds:
        return
    held = 0
    for unit in units:
        reason = _unit_hold_reason(unit, holds)
        if reason is None:
            continue
        unit.hold_reason = reason
        held += 1
    if held:
        console.print(
            f"\n  [dim]{held} unit(s) on hold (pr_sources.on_hold) — "
            "kept in the graph, skipped by `run`[/dim]"
        )
    unknown = sorted(
        ref for ref in holds
        if not any(pr.ref() == ref for u in units for pr in u.prs)
    )
    for owner, repo, num in unknown:
        console.print(
            f"  [dim]  on_hold: {pr_ref_label(f'{owner}/{repo}', num, origin_slug)} "
            "matches no discovered unit[/dim]"
        )


def _topo_sort_units(units: list[FeatureUnit]) -> list[FeatureUnit]:
    """Topologically sort on ``depends_on`` (deps outside ``units`` ignored), ties by ``sort_key``.

    Raises ValueError on cycles.
    """
    by_id = {u.feature_id: u for u in units}
    indeg: dict[str, int] = {u.feature_id: 0 for u in units}
    children: dict[str, list[str]] = {u.feature_id: [] for u in units}
    for u in units:
        for dep in u.depends_on:
            if dep in by_id:
                indeg[u.feature_id] += 1
                children[dep].append(u.feature_id)

    ready = sorted(
        (fid for fid, n in indeg.items() if n == 0),
        key=lambda fid: by_id[fid].sort_key,
    )
    out: list[FeatureUnit] = []
    while ready:
        fid = ready.pop(0)
        out.append(by_id[fid])
        for child in children[fid]:
            indeg[child] -= 1
            if indeg[child] == 0:
                key = by_id[child].sort_key
                lo, hi = 0, len(ready)
                while lo < hi:
                    mid = (lo + hi) // 2
                    if by_id[ready[mid]].sort_key < key:
                        lo = mid + 1
                    else:
                        hi = mid
                ready.insert(lo, child)

    if len(out) != len(units):
        unresolved = [fid for fid, n in indeg.items() if n > 0]
        raise ValueError(
            "depends_on cycle (or unresolved deps) among units: "
            + ", ".join(sorted(unresolved))
        )
    return out


def _unmet_deps(unit: FeatureUnit, state: PipelineState) -> list[str]:
    """IDs in ``unit.depends_on`` whose state is not ``merged`` (state lookup only)."""
    unmet: list[str] = []
    for dep_id in unit.depends_on:
        dep_fs = state.features.get(dep_id)
        if dep_fs is None or dep_fs.status != "merged":
            unmet.append(dep_id)
    return unmet


# Terminal status → (message, ``pr_policy`` flag that opts it back in on a renumbered branch).
_RECREATABLE: dict[str, tuple[str, str]] = {
    "closed": ("Rebase PR closed without merge", "recreate_closed_prs"),
    "reverted": ("Port was reverted on target", "recreate_reverted_prs"),
}


def _skip_ported_elsewhere(
    config: Config, state: PipelineState, unit: FeatureUnit,
) -> bool:
    """True (and say so) when another, merged unit already ported all of ``unit``'s PRs."""
    hit = find_merged_feature_for_prs(
        state, [p.url for p in unit.prs], exclude_feature_id=unit.feature_id,
    )
    if hit is None:
        return False
    fid, fs = hit
    primary = unit.primary_pr()
    ref = pr_ref_label(
        primary.repo_slug, primary.number, get_origin_repo_slug(config),
    )
    via = f" ({fs.rebase_pr_url})" if fs.rebase_pr_url else ""
    console.print(
        f"  [dim]{unit.feature_id} ({ref}) — already ported by merged "
        f"{fid}{via}, skipping[/dim]"
    )
    return True


def terminal_statuses(config: Config) -> set[str]:
    """Statuses ``releasy run`` will not re-enter, given the ``recreate_*`` opt-ins."""
    terminal = {"merged", "skipped", "superseded"}
    for status, (_, flag) in _RECREATABLE.items():
        if not getattr(config.pr_policy, flag):
            terminal.add(status)
    return terminal


def _recreate_opt_in(config: Config, status: str | None) -> tuple[str, str] | None:
    """``(why, flag_name)`` when ``status`` is opted back in, else ``None``."""
    entry = _RECREATABLE.get(status or "")
    if entry is None or not getattr(config.pr_policy, entry[1]):
        return None
    return entry


def _refresh_all_merge_status_from_github(
    config: Config, state: PipelineState,
) -> int:
    """Mark in-flight features whose port PR merged / closed on GitHub; returns the number changed."""
    refreshable = {"branch_created", "conflict", "needs_review"}
    changed = 0
    for fid, fs in state.features.items():
        if fs.status not in refreshable or not fs.rebase_pr_url:
            continue
        info = fetch_pr_by_url(config, fs.rebase_pr_url, include_closed=True)
        if info is None:
            continue
        if info.state == "merged":
            fs.status = "merged"
            clear_conflict_markers(fs)
            changed += 1
        elif info.state == "closed":
            fs.status = "closed"
            clear_conflict_markers(fs)
            fs.skip_reason = "rebase PR closed without merging"
            changed += 1
    return changed


def _scan_target_for_cherry_picks(
    repo_path: Path, base_ref: str, source_shas: set[str],
) -> dict[str, str]:
    """Map ``{source_sha: citing_commit_sha}`` from cherry-pick footers in the last 2000 commits of ``base_ref``."""
    if not source_shas:
        return {}
    result = run_git(
        ["log", base_ref, "-n", "2000", "--format=%H%x1f%B%x1e"],
        repo_path, check=False,
    )
    if result.returncode != 0 or not result.stdout:
        return {}
    out: dict[str, str] = {}
    for record in result.stdout.split("\x1e"):
        record = record.strip()
        if not record:
            continue
        citing_sha, _, body = record.partition("\x1f")
        for m in CHERRY_PICK_FROM_RE.finditer(body):
            cited = m.group(1).lower()
            if cited in source_shas and cited not in out:
                out[cited] = citing_sha
        if len(out) == len(source_shas):
            break
    return out


def _scan_open_prs_for_cherry_picks(
    config: Config, base_branch: str,
    source_shas: set[str], exclude_pr_urls: set[str],
) -> dict[str, str]:
    """Map ``{source_sha: superseding_pr_url}`` from commits of open PRs on ``base_branch``."""
    if not source_shas:
        return {}
    out: dict[str, str] = {}
    for pr_url, msgs in fetch_open_prs_with_commits_to_base(
        config, base_branch,
    ):
        if pr_url in exclude_pr_urls:
            continue
        for msg in msgs:
            for m in CHERRY_PICK_FROM_RE.finditer(msg):
                cited = m.group(1).lower()
                if cited in source_shas and cited not in out:
                    out[cited] = pr_url
        if len(out) == len(source_shas):
            break
    return out


def _refresh_all_superseded_status_from_github(
    config: Config, state: PipelineState,
    repo_path: Path, base_branch: str,
) -> int:
    """Mark entries ``superseded`` when target or another open PR cherry-picked their sources or port commits.

    A group needs every source cited; any one cited port commit suffices.
    ``closed`` entries are included. Returns the number promoted.
    """
    if not config.pr_policy.detect_superseded:
        return 0

    refreshable = {
        "branch_created", "needs_review", "conflict", "blocked", "closed",
    }
    feature_sources: dict[str, list[str]] = {}
    for fid, fs in state.features.items():
        if fs.status not in refreshable:
            continue
        urls = _source_pr_urls(fs)
        if urls:
            feature_sources[fid] = urls

    if not feature_sources:
        return 0

    # Unmerged sources have no merge SHA to match and are dropped.
    all_source_urls: set[str] = set()
    for urls in feature_sources.values():
        all_source_urls.update(urls)
    source_sha: dict[str, str] = {}
    for url in all_source_urls:
        info = fetch_pr_by_url(config, url, include_closed=True)
        if info and info.merge_commit_sha:
            source_sha[url] = info.merge_commit_sha.lower()

    remote = config.origin.remote_name
    base_ref = f"{remote}/{base_branch}"

    port_shas_by_feature: dict[str, list[str]] = {}
    all_port_shas: set[str] = set()
    for fid in feature_sources:
        fs = state.features.get(fid)
        if fs is None or not fs.branch_name:
            continue
        branch_ref = f"{remote}/{fs.branch_name}"
        result = run_git(
            ["rev-list", branch_ref, f"^{base_ref}", "-n", "100"],
            repo_path, check=False,
        )
        if result.returncode != 0:
            continue
        shas = [
            line.strip().lower()
            for line in result.stdout.splitlines() if line.strip()
        ]
        if shas:
            port_shas_by_feature[fid] = shas
            all_port_shas.update(shas)

    if not source_sha and not all_port_shas:
        return 0

    our_rebase_pr_urls = {
        fs.rebase_pr_url for fs in state.features.values()
        if fs.rebase_pr_url
    }

    fingerprints = set(source_sha.values()) | all_port_shas
    cited_on_target = _scan_target_for_cherry_picks(
        repo_path, base_ref, fingerprints,
    )
    cited_in_open = _scan_open_prs_for_cherry_picks(
        config, base_branch, fingerprints, our_rebase_pr_urls,
    )

    def _evidence_for(sha: str) -> tuple[str, str] | None:
        if sha in cited_in_open:
            return ("open PR", cited_in_open[sha])
        if sha in cited_on_target:
            return ("commit", cited_on_target[sha][:12])
        return None

    changed = 0
    for fid, urls in feature_sources.items():
        port_evidence: tuple[str, str] | None = None
        for sha in port_shas_by_feature.get(fid, []):
            port_evidence = _evidence_for(sha)
            if port_evidence:
                break

        if port_evidence:
            fs = state.features[fid]
            fs.status = "superseded"
            clear_conflict_markers(fs)
            where, ref = port_evidence
            fs.skip_reason = f"superseded by {where} {ref}"
            changed += 1
            continue

        evidence: list[tuple[str, str]] = []
        all_cited = True
        for url in urls:
            sha = source_sha.get(url)
            if not sha:
                all_cited = False
                break
            ev = _evidence_for(sha)
            if ev is None:
                all_cited = False
                break
            evidence.append(ev)
        if not (all_cited and evidence):
            continue
        fs = state.features[fid]
        fs.status = "superseded"
        clear_conflict_markers(fs)
        if len(evidence) == 1:
            where, ref = evidence[0]
            fs.skip_reason = f"superseded by {where} {ref}"
        else:
            fs.skip_reason = (
                "superseded ("
                + ", ".join(f"{w} {r}" for w, r in evidence) + ")"
            )
        changed += 1
    return changed


def _source_pr_urls(fs: FeatureState) -> list[str]:
    """``pr_urls`` if set, else ``[pr_url]``, else ``[]``."""
    if fs.pr_urls:
        return list(fs.pr_urls)
    if fs.pr_url:
        return [fs.pr_url]
    return []


def _apply_merged_labels(config: Config, state: PipelineState) -> None:
    """Apply ``config.merged_label`` to merged port PRs once, and strip it from origin-hosted source PRs."""
    label = config.merged_label
    if not label or not config.push:
        return

    pending = [
        (fid, fs) for fid, fs in state.features.items()
        if fs.status == "merged"
        and not fs.merged_label_applied
        and fs.rebase_pr_url
    ]
    if not pending:
        return

    if config.dry_run:
        console.print(
            f"  [magenta]dry-run:[/magenta] would apply merged label "
            f"[cyan]{label}[/cyan] to {len(pending)} merged port PR(s)"
        )
        return

    if not ensure_label(
        config, label, config.merged_label_color,
        f"Port merged into {state.base_branch or 'target'}",
    ):
        return

    origin_slug = get_origin_repo_slug(config)

    for fid, fs in pending:
        rebase_number = _pr_number_from_url(fs.rebase_pr_url or "")
        if rebase_number is None:
            continue
        if not add_label_to_pr(config, rebase_number, label):
            continue
        console.print(
            f"  [green]✓[/green] Labelled merged port "
            f"[cyan]{fid}[/cyan] (#{rebase_number}) with [cyan]{label}[/cyan]"
        )

        for src_url in _source_pr_urls(fs):
            parsed = parse_pr_url(src_url)
            if parsed is None:
                continue
            owner, repo, src_number = parsed
            src_slug = f"{owner}/{repo}"
            if origin_slug and src_slug != origin_slug:
                console.print(
                    f"    [dim]source #{src_number} on {src_slug}: "
                    f"skipped — not on origin[/dim]"
                )
                continue
            if remove_label_from_pr(config, src_number, label):
                console.print(
                    f"    [dim]stripped [cyan]{label}[/cyan] from "
                    f"source #{src_number}[/dim]"
                )

        fs.merged_label_applied = True


def _refresh_dep_states_from_github(
    config: Config, state: PipelineState, units: list[FeatureUnit],
) -> None:
    """Refresh merged / closed status of in-flight deps referenced by ``units`` from GitHub."""
    referenced: set[str] = set()
    for u in units:
        referenced.update(u.depends_on)
    if not referenced:
        return
    refreshable = {"branch_created", "conflict", "needs_review"}
    for dep_id in referenced:
        dep_fs = state.features.get(dep_id)
        if dep_fs is None or dep_fs.status not in refreshable:
            continue
        if not dep_fs.rebase_pr_url:
            continue
        info = fetch_pr_by_url(
            config, dep_fs.rebase_pr_url, include_closed=True,
        )
        if info is None:
            continue
        if info.state == "merged":
            dep_fs.status = "merged"
            clear_conflict_markers(dep_fs)
        elif info.state == "closed":
            dep_fs.status = "closed"
            clear_conflict_markers(dep_fs)
            dep_fs.skip_reason = "rebase PR closed without merging"


def _handle_in_progress_op(config: Config, repo_path: Path) -> None:
    """Abort a leftover git op under ``if_exists: recreate``, otherwise exit 2."""
    if not is_operation_in_progress(repo_path):
        return
    if config.pr_policy.if_exists == "recreate":
        if config.dry_run:
            console.print(
                f"\n[magenta]dry-run:[/magenta] would abort in-progress "
                f"git op in [cyan]{repo_path}[/cyan] "
                "(pr_policy.if_exists: recreate)"
            )
        else:
            kind = abort_in_progress_op(repo_path)
            console.print(
                f"\n[yellow]↻ Aborted in-progress {kind} in [cyan]{repo_path}[/cyan][/yellow] "
                f"(pr_policy.if_exists: recreate)"
            )
    else:
        console.print(
            f"\n[red]✗[/red] A cherry-pick/merge/rebase is already in progress "
            f"in [cyan]{repo_path}[/cyan]."
        )
        console.print(
            "  Resolve it first (or run `git cherry-pick --abort`), then re-run.\n"
            "  Or set [cyan]pr_policy.if_exists: recreate[/cyan] in config to "
            "auto-abort it."
        )
        raise SystemExit(2)


def _require_base_branch(repo_path: Path, base_branch: str, remote: str) -> None:
    if not branch_exists(repo_path, base_branch, remote):
        console.print(
            f"\n[red]✗[/red] Base branch [cyan]{base_branch}[/cyan] does not exist "
            f"on remote [cyan]{remote}[/cyan].\n"
            f"  Create and push it first, then re-run."
        )
        raise SystemExit(2)


def _ensure_run_labels(config: Config, resolve_conflicts: bool) -> bool:
    """Ensure run labels exist and report the AI resolver; returns whether it is active."""
    if config.push:
        ensure_label(
            config, RELEASY_LABEL, RELEASY_LABEL_COLOR, RELEASY_LABEL_DESCRIPTION,
        )
        for sess_label in _all_session_label_names(config):
            ensure_label(
                config, sess_label, RELEASY_LABEL_COLOR,
                "Session label (releasy session config)",
            )

    ai_active = resolve_conflicts and config.ai_resolve.enabled
    if ai_active:
        from releasy.ai_resolve import _backend_label

        console.print(
            f"[dim]AI conflict resolver: enabled "
            f"(backend='{_backend_label(config, config.ai_resolve.command)}', "
            f"label='{config.ai_resolve.label}', "
            f"max_iterations={config.ai_resolve.max_iterations})[/dim]"
        )
        if config.push:
            ensure_label(
                config,
                config.ai_resolve.label,
                config.ai_resolve.label_color,
                "Port conflict auto-resolved by Claude",
            )
    else:
        why = (
            "disabled via --no-resolve-conflicts" if not resolve_conflicts
            else "disabled in config"
        )
        console.print(f"[dim]AI conflict resolver: {why}[/dim]")

    if config.push:
        _ensure_conflict_labels(config)

    return ai_active


def _apply_only_filter(
    units: list[FeatureUnit], only: OnlyFilter,
) -> list[FeatureUnit] | None:
    """Units matching ``only``; ``None`` for a soft no-match, exit 2 for a hard one."""
    before = len(units)
    units = [u for u in units if only.matches_unit(u)]
    flag = "--pr" if only.soft else "--only"
    console.print(
        f"\n  [dim]{flag}={only.label}: "
        f"kept {len(units)}/{before} discovered unit(s)[/dim]"
    )
    if not units:
        if only.soft:
            console.print(
                f"\n  [dim]--pr={only.label!r} is not in this "
                "session's scope — nothing to do.[/dim]"
            )
            return None
        console.print(
            f"\n[red]✗[/red] --only={only.label!r} matched no "
            "discovered units. Check the URL / group id and re-run."
        )
        raise SystemExit(2)
    return units


def run_pipeline(
    config: Config,
    onto: str,
    work_dir: Path | None = None,
    resolve_conflicts: bool = True,
    retry_failed: bool = True,
    only: OnlyFilter | None = None,
    force_merge: bool = False,
    redo: bool = False,
) -> PipelineState:
    """Port PRs onto ``origin/<base_branch>``.

    ``retry_failed``: re-attempt units with a prior ``conflict`` from base
    instead of skipping them. ``redo``: re-port every kept unit from scratch.
    """
    state = load_state(config)
    _prune_superseded_singletons(config, state)
    repo_path = _setup_repo(config, work_dir, config.base_branch_name(onto))

    _handle_in_progress_op(config, repo_path)

    base_branch = config.base_branch_name(onto)
    remote = config.origin.remote_name

    _require_base_branch(repo_path, base_branch, remote)

    base_ref = f"{remote}/{base_branch}"
    console.print(f"Base: [cyan]{base_ref}[/cyan]")
    console.print(
        f"PRs will be opened against [bold cyan]{require_origin_repo_slug(config)}[/bold cyan] "
        "(origin) — RelEasy never opens PRs against any other repo."
    )
    if config.dry_run:
        state._dry_run_actions = {}  # type: ignore[attr-defined]
        console.print(
            "\n[bold magenta]DRY RUN[/bold magenta]: no state, repo, or "
            "GitHub writes will happen. Output shows intended actions only."
        )

    state.set_started(onto)
    state.base_branch = base_branch
    state.phase = "init"
    _persist_state(config, state)

    ai_active = _ensure_run_labels(config, resolve_conflicts)

    console.print(
        f"\n[bold]Phase:[/bold] Porting PRs onto [cyan]{base_branch}[/cyan]"
    )

    units = discover_feature_units(config)

    if only is not None:
        units = _apply_only_filter(units, only)
        if units is None:
            return load_state(config)

    if not units:
        console.print("\n  [dim]No PRs or groups to process after filtering[/dim]")

    _refresh_dep_states_from_github(config, state, units)
    _refresh_all_merge_status_from_github(config, state)
    _refresh_all_superseded_status_from_github(
        config, state, repo_path, base_branch,
    )
    _apply_merged_labels(config, state)
    _persist_state(config, state)

    existing_ids = {f.id for f in config.features}
    for unit in units:
        if unit.feature_id in existing_ids:
            continue
        existing_ids.add(unit.feature_id)
        prev_fs = state.features.get(unit.feature_id)
        outdated = prev_fs is not None and bool(prev_fs.outdated)
        if outdated and unit.hold_reason is None:
            console.print(
                f"\n    [yellow]♻[/yellow] [cyan]{unit.feature_id}[/cyan] — "
                f"port outdated ({prev_fs.outdated})"
            )
        if (
            (redo or outdated) and unit.hold_reason is None
            and not _reset_unit_for_redo(
                config, repo_path, state, unit, onto, remote,
            )
        ):
            continue
        # Re-read: _reset_unit_for_redo may have dropped the entry.
        prev_fs = state.features.get(unit.feature_id)
        terminal = terminal_statuses(config)
        if prev_fs is not None and prev_fs.status in terminal:
            primary = unit.primary_pr()
            origin_slug = get_origin_repo_slug(config)
            ref = pr_ref_label(primary.repo_slug, primary.number, origin_slug)
            console.print(
                f"  [dim]{unit.feature_id} ({ref}) — {prev_fs.status}, skipping[/dim]"
            )
            if config.dry_run:
                if unit.is_group:
                    console.print(
                        f"    [dim]· group of {len(unit.prs)} PR(s); "
                        f"primary {ref}[/dim]"
                    )
                if prev_fs.rebase_pr_url:
                    console.print(
                        f"    [dim]· rebase PR: "
                        f"[link={prev_fs.rebase_pr_url}]"
                        f"{prev_fs.rebase_pr_url}[/link][/dim]"
                    )
                if (
                    prev_fs.status in (
                        "skipped", "closed", "superseded", "reverted",
                    )
                    and prev_fs.skip_reason
                ):
                    console.print(
                        f"    [dim]· reason: {prev_fs.skip_reason}[/dim]"
                    )
                _dry_record(state, f"skip-{prev_fs.status}")
            continue
        if _skip_ported_elsewhere(config, state, unit):
            _dry_record(state, "skip-merged")
            continue
        if unit.hold_reason is not None:
            _report_hold(config, state, unit)
            continue
        _process_feature_unit(
            config, repo_path, state, unit, base_branch, base_ref, onto,
            remote, ai_active, retry_failed=retry_failed,
            force_merge=force_merge,
        )

    state.phase = "ports_done"
    _persist_state(config, state)

    if config.dry_run:
        counts = getattr(state, "_dry_run_actions", {}) or {}
        total = sum(counts.values())
        console.print(
            f"\n[bold magenta]Dry-run plan[/bold magenta] — "
            f"{total} unit(s) inspected"
        )
        if state.base_branch:
            console.print(f"  Base branch: [cyan]{state.base_branch}[/cyan]")
        if counts:
            order = [
                ("fresh-port",                  "would fresh-port"),
                ("rebuild-from-base",           "would rebuild from base (retry-failed)"),
                ("append",                      "would append to existing branch"),
                ("skip-existing-pr",            "skip — open rebase PR (use `releasy refresh`)"),
                ("open-pr-for-existing-branch", "would open PR for already-pushed branch"),
                ("record-existing-branch-state","would record state for existing branch"),
                ("resume-build-failed",         "would resume build/test on a parked branch"),
                ("skip-build-failed-exhausted", "skip — build_failed, resume cap reached"),
                ("skip-partial-continue-exhausted",
                                                "skip — partial group, auto-continue cap reached"),
                ("blocked-by-deps",             "blocked by unmet deps (no action)"),
                ("skip-on-hold",                "skip — on hold (pr_sources.on_hold)"),
                ("skip-stalled",                "skip — stalled on something outside the unit"),
                ("skip-conflict-retry-off",     "skip — prior conflict, retry-off"),
                ("skip-merged",                 "skip — already merged"),
                ("skip-skipped",                "skip — user-marked skipped"),
                ("skip-closed",                 "skip — rebase PR closed unmerged"),
                ("skip-superseded",             "skip — superseded by another PR"),
            ]
            for code, label in order:
                if counts.get(code):
                    console.print(f"  [magenta]·[/magenta] {label}: {counts[code]}")
            mapped = {code for code, _ in order}
            for code, n in counts.items():
                if code not in mapped:
                    console.print(f"  [magenta]·[/magenta] {code}: {n}")
        return state

    console.print(f"\n[bold]Pipeline complete.[/bold] Phase: {state.phase}")
    if state.base_branch:
        console.print(f"  Base branch: [cyan]{state.base_branch}[/cyan]")
    ready = sum(
        1 for fs in state.features.values() if fs.status == "needs_review"
    )
    branch_only = sum(
        1 for fs in state.features.values() if fs.status == "branch_created"
    )
    ai_assisted = sum(
        1 for fs in state.features.values()
        if fs.status in ("needs_review", "branch_created") and fs.ai_resolved
    )
    conflicts = sum(
        1 for fs in state.features.values() if fs.status == "conflict"
    )
    build_failed = sum(
        1 for fs in state.features.values() if fs.status == "build_failed"
    )
    if ready or branch_only or conflicts or build_failed:
        bf = f", {build_failed} build-failed" if build_failed else ""
        console.print(
            f"  Ports: {ready} needs-review, {branch_only} branch-created, "
            f"{conflicts} conflict{bf} ({ai_assisted} ai-assisted)"
        )

    return state


def run_sequential(
    config: Config,
    onto: str,
    work_dir: Path | None = None,
    resolve_conflicts: bool = True,
    retry_failed: bool = True,
    only: OnlyFilter | None = None,
    force_merge: bool = False,
) -> PipelineState:
    """Sequential mode: port the next queued unit, or exit 1 until the in-flight port PR merges.

    A ``conflict`` entry exits 1 unless ``retry_failed``.
    """
    state = load_state(config)
    _prune_superseded_singletons(config, state)
    repo_path = _setup_repo(config, work_dir, config.base_branch_name(onto))

    _handle_in_progress_op(config, repo_path)

    base_branch = config.base_branch_name(onto)
    remote = config.origin.remote_name

    _require_base_branch(repo_path, base_branch, remote)

    base_ref = f"{remote}/{base_branch}"
    console.print(f"Base: [cyan]{base_ref}[/cyan]")
    console.print(
        "[bold]Mode:[/bold] [cyan]sequential[/cyan] "
        "(one PR per invocation; previous PR must merge before the next)"
    )
    console.print(
        f"PRs will be opened against [bold cyan]{require_origin_repo_slug(config)}[/bold cyan] "
        "(origin) — RelEasy never opens PRs against any other repo."
    )

    state.set_started(onto)
    state.base_branch = base_branch
    state.phase = "init"
    _persist_state(config, state)

    ai_active = _ensure_run_labels(config, resolve_conflicts)

    console.print(
        f"\n[bold]Phase:[/bold] Sequential porting onto [cyan]{base_branch}[/cyan]"
    )

    units = discover_feature_units(config)

    if only is not None:
        units = _apply_only_filter(units, only)
        if units is None:
            return load_state(config)

    _refresh_all_merge_status_from_github(config, state)
    _refresh_all_superseded_status_from_github(
        config, state, repo_path, base_branch,
    )
    _apply_merged_labels(config, state)
    _persist_state(config, state)

    if not units:
        console.print("\n  [dim]No PRs to process after filtering[/dim]")
        state.phase = "ports_done"
        _persist_state(config, state)
        return state

    origin_slug = get_origin_repo_slug(config)

    for unit in units:
        fs = state.features.get(unit.feature_id)
        primary = unit.primary_pr()
        ref = pr_ref_label(primary.repo_slug, primary.number, origin_slug)

        seq_terminal = terminal_statuses(config)
        if fs is not None and fs.status in seq_terminal:
            console.print(
                f"  [dim]{unit.feature_id} ({ref}) — {fs.status}, skipping[/dim]"
            )
            continue
        if _skip_ported_elsewhere(config, state, unit):
            continue

        if unit.hold_reason is not None:
            _report_hold(config, state, unit)
            continue

        if fs is not None and fs.status == "conflict":
            if not retry_failed:
                console.print(
                    f"\n[red]✗[/red] [cyan]{unit.feature_id}[/cyan] ({ref}) is in "
                    "[red]conflict[/red] state — sequential mode will not advance "
                    "until it is resolved."
                )
                if fs.conflict_files:
                    for cf in fs.conflict_files:
                        console.print(f"      [red]•[/red] {cf}")
                console.print(
                    "  Resolve it manually and re-run "
                    "[cyan]releasy continue[/cyan], or pass "
                    "[cyan]--retry-failed[/cyan] (or set "
                    "[cyan]pr_policy.retry_failed: true[/cyan] in config) "
                    "to force a fresh cherry-pick attempt."
                )
                raise SystemExit(1)
            console.print(
                f"\n  [yellow]↻[/yellow] [cyan]{unit.feature_id}[/cyan] ({ref}) "
                "was in [red]conflict[/red] — retrying (retry_failed: true)"
            )

        if fs is not None and fs.status in ("needs_review", "branch_created"):
            if not fs.rebase_pr_url:
                console.print(
                    f"\n[red]✗[/red] [cyan]{unit.feature_id}[/cyan] ({ref}) has "
                    f"status [yellow]{fs.status}[/yellow] but no rebase PR URL "
                    "on file — cannot determine merge state."
                )
                console.print(
                    "  Open the PR manually (or run "
                    "[cyan]releasy continue --branch <id>[/cyan]) and try again."
                )
                raise SystemExit(1)

            console.print(
                f"\n  Checking in-flight PR for [cyan]{unit.feature_id}[/cyan] "
                f"({ref}): [link={fs.rebase_pr_url}]{fs.rebase_pr_url}[/link]"
            )
            info = fetch_pr_by_url(
                config, fs.rebase_pr_url, include_closed=True,
            )
            if info is None:
                console.print(
                    f"\n[red]✗[/red] Could not determine merge state of "
                    f"[link={fs.rebase_pr_url}]{fs.rebase_pr_url}[/link]. "
                    "Check RELEASY_GITHUB_TOKEN / network and retry."
                )
                raise SystemExit(1)
            if info.state == "closed":
                console.print(
                    f"\n[red]✗[/red] Rebase PR "
                    f"[link={fs.rebase_pr_url}]{fs.rebase_pr_url}[/link] was "
                    "[red]closed without merging[/red]."
                )
                console.print(
                    "  Sequential mode cannot advance past a closed PR. "
                    "Re-open and merge it, or remove the entry, then re-run."
                )
                raise SystemExit(1)
            if info.state != "merged":
                console.print(
                    f"\n[red]✗[/red] Rebase PR "
                    f"[link={fs.rebase_pr_url}]{fs.rebase_pr_url}[/link] is "
                    "[yellow]not merged yet[/yellow]."
                )
                console.print(
                    "  Sequential mode requires it to merge into "
                    f"[cyan]{base_branch}[/cyan] before the next port. "
                    "Approve and merge it, then re-run."
                )
                raise SystemExit(1)

            fs.status = "merged"
            clear_conflict_markers(fs)
            console.print(
                "    [green]✓[/green] PR merged — advancing the queue"
            )
            _apply_merged_labels(config, state)
            _persist_state(config, state)
            continue

        console.print(
            f"\n[bold]Porting next unit:[/bold] [cyan]{unit.feature_id}[/cyan] ({ref})"
        )
        console.print(
            f"  [dim]Re-fetching {remote} so the port branch is based on "
            f"the current {base_ref}...[/dim]"
        )
        fetch_remote(repo_path, remote)

        existing_ids = {f.id for f in config.features}
        if unit.feature_id in existing_ids:
            console.print(
                f"  [yellow]![/yellow] feature {unit.feature_id!r} already "
                "in config.features — skipping config-list mutation"
            )
            continue

        _process_feature_unit(
            config, repo_path, state, unit, base_branch, base_ref, onto,
            remote, ai_active, retry_failed=retry_failed,
            force_merge=force_merge,
        )

        new_fs = state.features.get(unit.feature_id)
        if new_fs is not None and new_fs.status == "conflict":
            console.print(
                "\n[yellow]Sequential run stopped on an unresolved conflict.[/yellow] "
                "Fix it manually, then re-run [cyan]releasy continue[/cyan]."
            )
        else:
            console.print(
                "\n[bold]Sequential run paused.[/bold] Review and merge the new "
                "PR, then re-run [cyan]releasy continue[/cyan] to port the next one."
            )
        return state

    state.phase = "ports_done"
    _persist_state(config, state)
    console.print(
        "\n[green]✓ All sequential ports processed[/green] — queue is empty."
    )
    return state


def _unit_pr_meta(unit: FeatureUnit) -> dict:
    """State-meta dict for a unit; PR fields come from the primary (first) PR."""
    primary = unit.primary_pr()
    return {
        "pr_url": primary.url,
        "pr_number": primary.number,
        "pr_title": primary.title,
        "pr_body": primary.body,
        "pr_numbers": [pr.number for pr in unit.prs],
        "pr_urls": [pr.url for pr in unit.prs],
        "contained_pr_urls": _contained_source_urls(unit),
        "pr_author": primary.author,
    }


def _contained_source_urls(unit: FeatureUnit) -> list[str]:
    """Source PRs the unit's own PRs say they cherry-picked (``Cherry-picked from #…``)."""
    out: list[str] = []
    seen: set[PRRef] = set()
    for pr in unit.prs:
        for ref in parse_cherry_picked_refs(pr.body, pr.repo_slug):
            if ref in seen:
                continue
            seen.add(ref)
            owner, repo, number = ref
            out.append(f"https://github.com/{owner}/{repo}/pull/{number}")
    return out


_VERSION_TOKEN = r"v?\d+(?:\.\d+)+"
_RELEASY_PREFIX_RE = re.compile(r"^\[releasy\b[^\]]*\]\s*", re.IGNORECASE)

# Applied to every PR RelEasy opens or updates.
RELEASY_LABEL = "releasy"
RELEASY_LABEL_COLOR = "1F6FEB"
RELEASY_LABEL_DESCRIPTION = "Created/managed by RelEasy"


def _ensure_conflict_labels(config: Config) -> None:
    """Pre-create every label the conflict-handling paths might apply."""
    ensure_label(
        config,
        config.ai_resolve.needs_attention_label,
        config.ai_resolve.needs_attention_label_color,
        "Releasy stopped on a conflict it could not resolve — needs human review",
    )
    ensure_label(
        config,
        config.ai_resolve.missing_prereqs_label,
        config.ai_resolve.missing_prereqs_label_color,
        "Conflict caused by an unported prerequisite PR",
    )
    ensure_label(
        config,
        config.ai_resolve.auto_prereq_label,
        config.ai_resolve.auto_prereq_label_color,
        "Combined PR includes auto-added prerequisite PR(s)",
    )
    if config.ai_resolve.verify_resolution:
        ensure_label(
            config,
            config.ai_resolve.verify_label,
            config.ai_resolve.verify_label_color,
            "Post-resolve audit flagged the AI's resolution for human review",
        )


def _display_project(project: str | None) -> str:
    """``config.project`` for PR titles: all-lowercase names are title-cased, others kept."""
    if not project:
        return ""
    if project.islower():
        return project.title()
    return project


def _version_label(project: str | None, base_branch: str | None) -> str:
    """``26.3`` from ``<project>-26.3``; otherwise the whole base branch name."""
    if not base_branch:
        return ""
    if project:
        prefix = f"{project.lower()}-"
        if base_branch.lower().startswith(prefix):
            return base_branch[len(prefix):]
    return base_branch


def _subject_prefix(project: str | None, base_branch: str | None) -> str:
    """``"Antalya 26.3"``-style PR title prefix (either part may be missing)."""
    proj = _display_project(project)
    ver = _version_label(project, base_branch)
    if proj and ver:
        return f"{proj} {ver}"
    return proj or ver or ""


def _strip_misleading_title_prefix(title: str, project: str | None) -> str:
    """Strip a ``[releasy …]`` tag and a leading ``"26.1 Antalya: "``-style version prefix from a title.

    A version without a ``:``/``-`` separator is kept.
    """
    cleaned = _RELEASY_PREFIX_RE.sub("", title.strip(), count=1)

    patterns: list[str] = []
    if project:
        proj = re.escape(project)
        patterns.append(rf"^{_VERSION_TOKEN}\s+{proj}\s*[:\-]\s+")
        patterns.append(rf"^{proj}\s+{_VERSION_TOKEN}\s*[:\-]\s+")
    patterns.append(rf"^{_VERSION_TOKEN}\s*[:\-]\s+")

    for pat in patterns:
        new = re.sub(pat, "", cleaned, count=1, flags=re.IGNORECASE)
        if new != cleaned:
            cleaned = new
            break

    return cleaned.strip() or title


def _unit_title(
    unit: FeatureUnit,
    project: str | None,
    base_branch: str | None,
) -> str:
    """PR title ``"<Project> <version>: <subject>"``."""
    prefix = _subject_prefix(project, base_branch)

    if unit.is_group:
        if unit.title_prefix:
            subject = unit.title_prefix.rstrip()
        elif len(unit.prs) == 1:
            subject = _strip_misleading_title_prefix(unit.prs[0].title, project)
        else:
            subject = (
                f"{unit.group_id}: combined port of {len(unit.prs)} PRs"
            )
    else:
        pr = unit.primary_pr()
        subject = _strip_misleading_title_prefix(pr.title, project)
        if unit.title_prefix:
            subject = f"{unit.title_prefix}{subject}"

    return f"{prefix}: {subject}" if prefix else subject


_MD_HEADING_RE = re.compile(r"^(#{1,6})[ \t]+(.+?)[ \t]*$", re.MULTILINE)


def _extract_md_section(body: str, keyword: str) -> str | None:
    """Text under the first heading containing ``keyword`` up to the next heading, or ``None``."""
    if not body:
        return None
    key = keyword.lower()
    headings = list(_MD_HEADING_RE.finditer(body))
    for i, m in enumerate(headings):
        if key in m.group(2).lower():
            start = m.end()
            end = headings[i + 1].start() if i + 1 < len(headings) else len(body)
            section = body[start:end].strip()
            return section or None
    return None


def _extract_md_section_with_subsections(
    body: str, keyword: str,
) -> str | None:
    """Like :func:`_extract_md_section` but keeps the heading and nested subheadings."""
    if not body:
        return None
    key = keyword.lower()
    headings = list(_MD_HEADING_RE.finditer(body))
    for i, m in enumerate(headings):
        if key not in m.group(2).lower():
            continue
        level = len(m.group(1))
        start = m.start()
        end = len(body)
        for j in range(i + 1, len(headings)):
            n = headings[j]
            if len(n.group(1)) <= level:
                end = n.start()
                break
        section = body[start:end].rstrip()
        return section or None
    return None


def _strip_md_sections(body: str, keywords: list[str]) -> str:
    """Remove every markdown section (with its subheadings) whose heading contains any of ``keywords``."""
    if not body:
        return body
    headings = list(_MD_HEADING_RE.finditer(body))
    if not headings:
        return body
    keys_lc = [k.lower() for k in keywords]
    spans: list[tuple[int, int]] = []
    for i, m in enumerate(headings):
        if not any(k in m.group(2).lower() for k in keys_lc):
            continue
        level = len(m.group(1))
        start = m.start()
        end = len(body)
        for j in range(i + 1, len(headings)):
            if len(headings[j].group(1)) <= level:
                end = headings[j].start()
                break
        spans.append((start, end))
    if not spans:
        return body
    out = body
    for start, end in sorted(spans, reverse=True):
        out = out[:start] + out[end:]
    out = re.sub(r"\n{3,}", "\n\n", out)
    return out.strip()


# Source-PR body sections the port PR body renders once on its own.
_DEDUP_PR_BODY_SECTIONS = (
    "changelog category",
    "changelog entry",
    "ci/cd options",
)

_DEFAULT_CI_CD_OPTIONS_BLOCK = """### CI/CD Options
#### Exclude tests:
- [ ] <!---ci_exclude_fast--> Fast test
- [ ] <!---ci_exclude_integration--> Integration Tests
- [ ] <!---ci_exclude_stateless--> Stateless tests
- [ ] <!---ci_exclude_stateful--> Stateful tests
- [ ] <!---ci_exclude_performance--> Performance tests
- [ ] <!---ci_exclude_asan--> All with ASAN
- [x] <!---ci_exclude_tsan--> All with TSAN
- [x] <!---ci_exclude_msan--> All with MSAN
- [x] <!---ci_exclude_ubsan--> All with UBSAN
- [x] <!---ci_exclude_coverage--> All with Coverage
- [ ] <!---ci_exclude_aarch64|arm--> All with Aarch64
- [ ] <!---ci_exclude_regression--> All Regression
- [ ] <!---no_ci_cache--> Disable CI Cache

#### Regression jobs to run:
- [ ] <!---ci_regression_common--> Fast suites (mostly <1h)
- [ ] <!---ci_regression_aggregate_functions--> Aggregate Functions (2h)
- [ ] <!---ci_regression_alter--> Alter (1.5h)
- [ ] <!---ci_regression_benchmark--> Benchmark (30m)
- [ ] <!---ci_regression_clickhouse_keeper--> ClickHouse Keeper (1h)
- [x] <!---ci_regression_iceberg--> Iceberg (2h)
- [ ] <!---ci_regression_ldap--> LDAP (1h)
- [x] <!---ci_regression_parquet--> Parquet (1.5h)
- [ ] <!---ci_regression_rbac--> RBAC (1.5h)
- [ ] <!---ci_regression_ssl_server--> SSL Server (1h)
- [ ] <!---ci_regression_s3--> S3 (2h)
- [x] <!---ci_regression_s3_export--> S3 Export (2h)
- [x] <!---ci_regression_swarms--> Swarms (30m)
- [ ] <!---ci_regression_tiered_storage--> Tiered Storage (2h)"""


def _extract_changelog_category(body: str) -> str | None:
    """First non-placeholder line under the 'Changelog category' heading."""
    section = _extract_md_section(body, "changelog category")
    if not section:
        return None
    for line in section.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        m = re.match(r"^[-*+]\s+(.+)$", stripped)
        text = (m.group(1) if m else stripped).strip()
        if text.startswith("(") or text.lower().startswith("leave one"):
            continue
        return text
    return None


def _extract_changelog_entry(body: str) -> str | None:
    """The 'Changelog entry' paragraph, or ``None`` if only a placeholder."""
    section = _extract_md_section(body, "changelog entry")
    if not section:
        return None
    cleaned = re.sub(r"<!--.*?-->", "", section, flags=re.DOTALL).strip()
    if not cleaned:
        return None
    low = cleaned.lower()
    if low.startswith("...") or low in {"n/a", "na", "none", "-"}:
        return None
    return cleaned


def _build_changelog_block(unit: FeatureUnit) -> str | None:
    """Changelog block: first category found, synthesized entry or else the first PR's entry."""
    category: str | None = None
    for pr in unit.prs:
        cat = _extract_changelog_category(pr.body or "")
        if cat:
            category = cat
            break

    entry_text: str | None = unit.synthesized_changelog
    if not entry_text:
        for pr in unit.prs:
            entry = _extract_changelog_entry(pr.body or "")
            if entry:
                entry_text = entry.strip()
                break

    return render_changelog_block(category, entry_text, unit.prs)


def render_changelog_block(
    category: str | None, entry_text: str | None, prs: "list[PRInfo]",
) -> str | None:
    """Render a changelog category + entry block with a ``(<url> by @author, …)`` suffix."""
    if not category and not entry_text:
        return None

    out: list[str] = []
    if category:
        out.append("### Changelog category (leave one):")
        out.append("")
        out.append(f"- {category}")
        out.append("")
    if entry_text:
        out.append(
            "### Changelog entry (a user-readable short description of the "
            "changes that goes to CHANGELOG.md):"
        )
        out.append("")
        attribution = _format_pr_attribution(prs)
        final_entry = entry_text.strip()
        if attribution:
            if final_entry.endswith("."):
                final_entry = final_entry[:-1]
            final_entry = f"{final_entry} ({attribution})."
        out.append(final_entry)
        out.append("")
    return "\n".join(out).rstrip()


def _truncate_for_prompt(text: str, max_chars: int) -> str:
    """Trim ``text`` to ``max_chars`` with a visible truncation marker."""
    if not text:
        return ""
    if max_chars <= 0 or len(text) <= max_chars:
        return text
    return text[:max_chars].rstrip() + "\n\n…(truncated)"


def _build_pr_blocks_for_synthesis(unit: FeatureUnit, max_pr_body_chars: int) -> str:
    """``{pr_blocks}`` for the changelog-synthesis prompt, in cherry-pick order."""
    blocks: list[str] = []
    for idx, pr in enumerate(unit.prs, start=1):
        body = _truncate_for_prompt((pr.body or "").strip(), max_pr_body_chars)
        author = f"@{pr.author}" if pr.author else "(unknown author)"
        blocks.append(
            f"### PR {idx}/{len(unit.prs)}: {pr.title}\n"
            f"- Author: {author}\n"
            f"- URL: {pr.url}\n\n"
            f"{body if body else '_(empty body)_'}"
        )
    return "\n\n---\n\n".join(blocks)


def _maybe_synthesize_changelog(
    config: Config, unit: FeatureUnit, base_branch: str,
) -> None:
    """Populate ``unit.synthesized_changelog`` for multi-PR groups when ``ai_changelog`` is on; failures are non-fatal."""
    if unit.synthesized_changelog is not None:
        return
    if not config.ai_changelog.enabled:
        return
    if not unit.is_group or len(unit.prs) <= 1:
        return

    from releasy.ai_resolve import synthesize_changelog_entry

    pr_blocks = _build_pr_blocks_for_synthesis(
        unit, config.ai_changelog.max_pr_body_chars,
    )
    label = unit.group_id or unit.feature_id
    source_repo = unit.primary_pr().repo_slug

    result = synthesize_changelog_entry(
        config,
        unit_label=label,
        pr_blocks=pr_blocks,
        n_prs=len(unit.prs),
        base_branch=base_branch,
        source_repo=source_repo,
    )

    if result.cost_usd is not None:
        unit.ai_cost_usd_total = (
            (unit.ai_cost_usd_total or 0.0) + result.cost_usd
        )

    if not result.success or not result.text:
        reason = result.error or (
            "timed out" if result.timed_out else "unknown failure"
        )
        cost_note = (
            f" [dim](cost: ${result.cost_usd:.4f})[/dim]"
            if result.cost_usd is not None else ""
        )
        console.print(
            f"    [yellow]changelog synthesis failed:[/yellow] {reason} "
            f"— falling back to first PR's entry{cost_note}"
        )
        return

    unit.synthesized_changelog = result.text.strip()
    cost_note = (
        f" [dim](cost: ${result.cost_usd:.4f})[/dim]"
        if result.cost_usd is not None else ""
    )
    console.print(
        f"    [green]\u2713[/green] Synthesized CHANGELOG entry for "
        f"[cyan]{label}[/cyan]{cost_note}"
    )


def _build_ci_options_block(unit: FeatureUnit) -> str | None:
    """The default CI/CD Options block if any source PR body has that section."""
    for pr in unit.prs:
        section = _extract_md_section_with_subsections(
            pr.body or "", "ci/cd options",
        )
        if section:
            return _DEFAULT_CI_CD_OPTIONS_BLOCK.rstrip()
    return None


def _format_pr_attribution(prs: "list[PRInfo]") -> str:
    """Comma-separated ``<url> by @<author>`` (just ``<url>`` when the author is unknown)."""
    parts: list[str] = []
    for pr in prs:
        if pr.author:
            parts.append(f"{pr.url} by @{pr.author}")
        else:
            parts.append(pr.url)
    return ", ".join(parts)


def _unit_body(
    unit: FeatureUnit,
    origin_slug: str | None,
    *,
    needs_intervention: bool = False,
    failed_index: int | None = None,
    failed_pr: PRInfo | None = None,
    conflict_files: list[str] | None = None,
    auto_prereq_urls: list[str] | None = None,
    auto_prereq_trail: list[dict] | None = None,
    dropped_items: list[str] | None = None,
    stall: StallReason | None = None,
) -> str:
    """Build the PR body, with optional banners for intervention, auto-prereqs and dropped scope."""
    lines: list[str] = []
    if needs_intervention:
        applied = failed_index if failed_index is not None else 0
        remaining = max(0, len(unit.prs) - applied - 1)
        failed_ref = (
            pr_ref_label(failed_pr.repo_slug, failed_pr.number, origin_slug)
            if failed_pr is not None
            else "unknown"
        )
        lines.append(
            "> **This PR needs manual intervention.**"
        )
        why = (
            stall.summary() if stall is not None
            else "AI resolver was disabled, exhausted its iteration "
                 "budget, or gave up"
        )
        lines.append(
            f"> Cherry-pick of {failed_ref} could not be resolved "
            f"automatically — {why}."
        )
        lines.append(
            f"> The branch contains the first {applied} commit(s) of "
            f"the group; {remaining} later PR(s) were not attempted."
        )
        if conflict_files:
            lines.append("> Conflicted files at the failure point:")
            for cf in conflict_files:
                lines.append(f"> - `{cf}`")
        lines.append(
            "> Resolve the conflict locally, push the fix, and mark this "
            "PR ready for review."
        )
        lines.append("")

    if dropped_items:
        lines.append(
            "> **Dropped from this backport:** the AI dropped these "
            "surfaces rather than pulling in a missing prerequisite. "
            "Reviewers: confirm each is genuinely optional."
        )
        for item in dropped_items:
            lines.append(f"> - {item}")
        lines.append("")

    if auto_prereq_urls:
        lines.append(
            "> **Auto-ported prerequisites:** RelEasy detected that the "
            "requested port depended on PR(s) not yet on the target "
            f"branch and auto-ported them first ({len(auto_prereq_urls)} "
            "PR(s) added). Reviewers: please confirm the prereq scope "
            "is appropriate."
        )
        for url in auto_prereq_urls:
            lines.append(f"> - {url}")
        if auto_prereq_trail:
            chain = " → ".join(
                entry.get("triggering_pr") or "(unknown)"
                for entry in auto_prereq_trail
            )
            if chain:
                lines.append(f"> _Detection trail:_ {chain}")
        lines.append("")

    changelog = _build_changelog_block(unit)
    if changelog:
        lines.append(changelog)
        lines.append("")

    ci_block = _build_ci_options_block(unit)
    if ci_block:
        lines.append(ci_block)
        lines.append("")

    refs = [pr_ref_label(pr.repo_slug, pr.number, origin_slug) for pr in unit.prs]
    source_refs = ", ".join(refs)
    if unit.is_group or len(unit.prs) > 1:
        lines.append(
            f"Combined port of {len(unit.prs)} PR(s) "
            f"(group `{unit.group_id or unit.feature_id}`). "
            f"Cherry-picked from {source_refs}.\n"
        )
        for pr, ref in zip(unit.prs, refs):
            lines.append(f"- {ref} — {pr.title}")
        lines.append("")
    else:
        pr = unit.prs[0]
        lines.append(f"Cherry-picked from {source_refs}.")
        cleaned = _strip_md_sections(
            pr.body or "", list(_DEDUP_PR_BODY_SECTIONS),
        )
        if cleaned:
            lines.append(f"\n---\n\n{cleaned}")
    return "\n".join(lines)


def _tag_commit_with_source_pr(
    repo_path: Path, unit: FeatureUnit, pr: PRInfo, origin_slug: str | None,
) -> None:
    """Append a ``Source-PR`` trailer to the just-made commit (multi-PR groups only)."""
    if not unit.is_group or len(unit.prs) <= 1:
        return
    ref = pr_ref_label(pr.repo_slug, pr.number, origin_slug)
    append_commit_trailer(
        repo_path, "Source-PR", f"{ref} ({pr.url})",
    )


def _cherry_pick_pr(
    repo_path: Path, config: Config, pr: PRInfo,
) -> OperationResult:
    """Cherry-pick one PR into the current branch; non-origin PRs are fetched by HTTPS URL."""
    origin_slug = get_origin_repo_slug(config)
    is_external = origin_slug is None or pr.repo_slug != origin_slug
    fetch_target = (
        slug_to_https_url(pr.repo_slug) if is_external
        else config.origin.remote_name
    )

    if pr.state == "merged" and pr.merge_commit_sha:
        if is_external and not fetch_commit(
            repo_path, fetch_target, pr.merge_commit_sha,
        ):
            return OperationResult(
                success=False, conflict_files=[],
                error_message=(
                    f"could not fetch commit {pr.merge_commit_sha[:12]} "
                    f"from {pr.repo_slug}"
                ),
            )
        return cherry_pick_merge_commit(
            repo_path, pr.merge_commit_sha, abort_on_conflict=False,
        )

    if not fetch_pr_ref(repo_path, fetch_target, pr.number):
        return OperationResult(
            success=False, conflict_files=[],
            error_message=f"could not fetch PR #{pr.number} from {pr.repo_slug}",
        )
    return cherry_pick_merge_commit(
        repo_path, "FETCH_HEAD", abort_on_conflict=False,
    )


_SOURCE_PR_URL_RE = re.compile(r"https?://\S+?/pull/\d+", re.IGNORECASE)


def _read_commit_subjects(
    repo_path: Path, base_ref: str, branch: str,
) -> list[str]:
    """Subjects of ``base_ref..branch`` commits, oldest first; empty on error."""
    out = run_git(
        ["log", "--reverse", "--format=%s", f"{base_ref}..{branch}"],
        repo_path, check=False,
    )
    if out.returncode != 0:
        return []
    return [line for line in out.stdout.splitlines()]


def _read_source_pr_trailers(
    repo_path: Path, base_ref: str, branch: str,
) -> tuple[list[str | None], int]:
    """Per-commit ``Source-PR:`` trailer URL (or ``None``) for ``base_ref..branch``, oldest first, and the commit count."""
    rev_list = run_git(
        ["rev-list", "--reverse", f"{base_ref}..{branch}"],
        repo_path, check=False,
    )
    if rev_list.returncode != 0:
        return [], 0
    shas = [s for s in rev_list.stdout.split() if s]

    urls: list[str | None] = []
    for sha in shas:
        out = run_git(
            [
                "log", "-1",
                "--format=%(trailers:key=Source-PR,unfold=true,valueonly=true)",
                sha,
            ],
            repo_path, check=False,
        )
        url: str | None = None
        if out.returncode == 0:
            for line in out.stdout.splitlines():
                m = _SOURCE_PR_URL_RE.search(line)
                if m:
                    url = m.group(0)
                    break
        urls.append(url)
    return urls, len(shas)


def _collect_dropped_trailers(
    repo_path: Path, base_ref: str, branch: str,
) -> list[str]:
    """Return ``Dropped:`` trailer values in commit order, de-duplicated."""
    rev_list = run_git(
        ["rev-list", "--reverse", f"{base_ref}..{branch}"],
        repo_path, check=False,
    )
    if rev_list.returncode != 0:
        return []
    shas = [s for s in rev_list.stdout.split() if s]

    seen: set[str] = set()
    values: list[str] = []
    for sha in shas:
        out = run_git(
            [
                "log", "-1",
                "--format=%(trailers:key=Dropped,unfold=true,valueonly=true)",
                sha,
            ],
            repo_path, check=False,
        )
        if out.returncode != 0:
            continue
        for line in out.stdout.splitlines():
            value = line.strip()
            if not value or value in seen:
                continue
            seen.add(value)
            values.append(value)
    return values


@dataclass
class _AppendDecision:
    feasible: bool
    reason: str
    applied_urls: set[str] = field(default_factory=set)
    missing_count: int = 0


def _decide_append(
    repo_path: Path,
    base_ref: str,
    branch: str,
    unit: FeatureUnit,
    origin_slug: str | None,
) -> _AppendDecision:
    """Decide whether ``if_exists: append`` can proceed for this unit.

    A ``Source-PR:`` trailer naming an undeclared PR makes it infeasible.
    Untagged origin PRs are matched by their ``Merge pull request #<N> from``
    subject (origin only: cross-repo PR numbers can collide).
    """
    per_commit, _total = _read_source_pr_trailers(repo_path, base_ref, branch)
    declared_urls = [pr.url for pr in unit.prs]
    declared_set = set(declared_urls)

    tagged_urls = [u for u in per_commit if u is not None]
    foreign = sorted({u for u in tagged_urls if u not in declared_set})
    if foreign:
        return _AppendDecision(
            feasible=False,
            reason=(
                f"branch carries commits whose Source-PR trailer points at "
                f"PR(s) not in the declared group: {', '.join(foreign)}"
            ),
        )

    applied: set[str] = set(tagged_urls)

    unmatched_origin = [
        pr for pr in unit.prs
        if pr.url not in applied
        and origin_slug is not None
        and pr.repo_slug == origin_slug
    ]
    if unmatched_origin:
        subjects = _read_commit_subjects(repo_path, base_ref, branch)
        for pr in unmatched_origin:
            needle = f"Merge pull request #{pr.number} from"
            if any(needle in s for s in subjects):
                applied.add(pr.url)

    missing_count = sum(1 for url in declared_urls if url not in applied)
    return _AppendDecision(
        feasible=True,
        reason="ok",
        applied_urls=applied,
        missing_count=missing_count,
    )


def _group_branch_missing_members(
    repo_path: Path,
    base_ref: str,
    branch: str,
    unit: FeatureUnit,
    origin_slug: str | None,
) -> bool:
    """True when ``branch`` lacks some of ``unit``'s PRs and they can be appended."""
    decision = _decide_append(repo_path, base_ref, branch, unit, origin_slug)
    return decision.feasible and decision.missing_count > 0


def _next_free_renumbered_port_branch(
    repo_path: Path,
    remote: str,
    canonical_branch: str,
) -> str:
    """``<canonical_branch>-N`` for the smallest ``N >= 1`` with no local or remote ref."""
    n = 1
    while True:
        candidate = f"{canonical_branch}-{n}"
        if not remote_branch_exists(
            repo_path, candidate, remote,
        ) and not local_branch_exists(repo_path, candidate):
            return candidate
        n += 1


def _close_port_pr_if_open(
    config: Config, pr_url: str, comment: str,
) -> str | None:
    """Close port PR ``pr_url`` if open; returns its prior state, or ``None`` if unreadable / not closed."""
    info = fetch_pr_by_url(config, pr_url, include_closed=True)
    if info is None:
        return None
    if info.state != "open":
        return info.state
    parsed = parse_pr_url(pr_url)
    if parsed is None or not close_pull_request(
        config, parsed[2], comment=comment,
    ):
        return None
    verb = "would close" if config.dry_run else "closed"
    console.print(
        f"\n    [yellow]✗[/yellow] {verb} port PR "
        f"[link={pr_url}]{pr_url}[/link]"
    )
    return "open"


def _reset_unit_for_redo(
    config: Config,
    repo_path: Path,
    state: PipelineState,
    unit: FeatureUnit,
    onto: str,
    remote: str,
) -> bool:
    """Discard ``unit``'s prior port so this run re-ports it from base.

    With a port PR (closed if open), the unit moves to a renumbered branch;
    otherwise its branch is rebuilt in place. Drops the state entry.
    Returns False (unit untouched) for a merged port.
    """
    fs = state.features.get(unit.feature_id)
    if fs is None:
        unit.if_exists = "recreate"
        return True

    canonical = config.feature_branch_name(unit.feature_id, onto)
    merged_hint = (
        "not redoing. [dim](mark it with `releasy mark-reverted` first if "
        "it was reverted on target)[/dim]"
    )
    if fs.status == "merged":
        console.print(
            f"\n    [red]✗[/red] [cyan]{unit.feature_id}[/cyan] — port already "
            f"merged ({fs.rebase_pr_url or 'no PR recorded'}); {merged_hint}"
        )
        return False

    if fs.rebase_pr_url:
        new_branch = _next_free_renumbered_port_branch(
            repo_path, remote, canonical,
        )
        # A reverted port's PR is merged on GitHub, by definition.
        was = "merged" if fs.status == "reverted" else _close_port_pr_if_open(
            config, fs.rebase_pr_url,
            f"Superseded: re-porting from scratch on `{new_branch}` "
            + (f"({fs.outdated})." if fs.outdated
               else "(`releasy run --redo`)."),
        )
        if was == "merged" and fs.status != "reverted":
            console.print(
                f"\n    [red]✗[/red] [cyan]{unit.feature_id}[/cyan] — port PR "
                f"{fs.rebase_pr_url} is merged; {merged_hint}"
            )
            return False
        if was is None:
            console.print(
                f"\n    [red]✗[/red] [cyan]{unit.feature_id}[/cyan] — could "
                f"not close port PR {fs.rebase_pr_url}; not redoing."
            )
            return False
    else:
        new_branch = fs.branch_name or canonical

    console.print(
        f"    [yellow]↻[/yellow] [cyan]{unit.feature_id}[/cyan] — redo: "
        f"dropping its state ({fs.status}), re-porting from base on "
        f"[cyan]{new_branch}[/cyan]"
    )
    unit.if_exists = "recreate"
    unit.port_branch = new_branch
    state.features.pop(unit.feature_id, None)
    _persist_state(config, state)
    return True


def _is_partial_group(fs: FeatureState | None) -> bool:
    """True for a ``conflict`` unit a prior run left with some PRs already committed."""
    return (
        fs is not None
        and fs.status == "conflict"
        and (fs.partial_pr_count or 0) > 0
    )


def _partial_continue_allowed(
    config: Config,
    prev_state: FeatureState | None,
    if_exists: str,
    retry_failed: bool,
) -> bool:
    """True when a prior run's partial group should be resumed (also under ``if_exists: recreate``)."""
    return (
        retry_failed
        and config.pr_policy.max_partial_continue_attempts > 0
        and _is_partial_group(prev_state)
        and if_exists != "append"
    )


_LANDED_STATUSES = frozenset({"merged", "superseded"})


def _stall_still_blocks(
    config: Config,
    state: PipelineState,
    stall: StallReason,
    *,
    exclude_feature_id: str | None = None,
) -> bool:
    """True while nothing ``stall`` waits on has landed (a merely queued prereq does not count)."""
    if stall.kind == "waiting_for_merge":
        if not stall.waiting_on_units:
            return False
        return all(
            (fs := state.features.get(uid)) is not None
            and fs.status not in _LANDED_STATUSES
            for uid in stall.waiting_on_units
        )
    if stall.kind == "missing_prereq":
        if not stall.waiting_on_prs:
            return False
        queued = _find_already_queued_prereqs(
            config, state, list(stall.waiting_on_prs),
            exclude_feature_id=exclude_feature_id,
        )
        return not any(
            q["queued_status"] in _LANDED_STATUSES for q in queued
        )
    return False


def _prereq_now_queued_stall(
    config: Config,
    state: PipelineState,
    prev_state: FeatureState,
    feature_id: str,
) -> StallReason | None:
    """A ``waiting_for_merge`` replacing a ``missing_prereq`` stall whose prereq a tracked unit now ports."""
    stall = prev_state.stall
    if stall is None or stall.kind != "missing_prereq":
        return None
    queued = [
        q for q in _find_already_queued_prereqs(
            config, state, list(stall.waiting_on_prs),
            exclude_feature_id=feature_id,
        )
        if q["queued_status"] is not None
    ]
    return _queued_stall(queued, prior=prev_state) if queued else None


def _dead_end_budget_spent(
    config: Config,
    state: PipelineState,
    prev_state: FeatureState,
    canonical_branch: str,
    label: str,
) -> bool:
    """True when a :data:`CAPPED_STALL_KINDS` stall used up ``max_dead_end_attempts`` (partial groups excluded)."""
    from rich.markup import escape

    cap = config.ai_resolve.max_dead_end_attempts
    stall = prev_state.stall
    if cap <= 0 or stall.runs < cap or _is_partial_group(prev_state):
        return False
    console.print(
        f"\n    [yellow]⏭[/yellow]  [cyan]{canonical_branch}[/cyan] "
        f"({label}) — {escape(stall.summary())}; {stall.runs}/{cap} "
        "attempts spent, not re-resolving [dim](bump "
        "ai_resolve.max_dead_end_attempts, fix it by hand, or "
        "--ignore-stalls)[/dim]"
    )
    _dry_record(state, "skip-dead-end-exhausted")
    return True


def _skip_for_stall(
    config: Config,
    state: PipelineState,
    prev_state: FeatureState | None,
    feature_id: str,
    canonical_branch: str,
    label: str,
) -> bool:
    """Honour a recorded stall; True when the unit should be left untouched this run."""
    from rich.markup import escape

    stall = prev_state.stall if prev_state is not None else None
    if (
        stall is None
        or config.ignore_stalls
        or not config.pr_policy.honor_stall_reasons
    ):
        return False

    if stall.kind in CAPPED_STALL_KINDS:
        return _dead_end_budget_spent(
            config, state, prev_state, canonical_branch, label,
        )
    if stall.kind not in BLOCKING_STALL_KINDS:
        return False
    if not _stall_still_blocks(
        config, state, stall, exclude_feature_id=feature_id,
    ):
        prev_state.stall = None
        return False

    upgraded = _prereq_now_queued_stall(config, state, prev_state, feature_id)
    if upgraded is not None:
        stall = prev_state.stall = upgraded  # a new wait, counted from here
    else:
        stall.runs += 1
    console.print(
        f"\n    [yellow]⏳[/yellow] [cyan]{canonical_branch}[/cyan] "
        f"({label}) — {escape(stall.summary())}; not re-resolving "
        f"[dim](run {stall.runs} in this state; --ignore-stalls to force)"
        "[/dim]"
    )
    _persist_state(config, state)
    _dry_record(state, "skip-stalled")
    return True


def _report_hold(
    config: Config, state: PipelineState, unit: FeatureUnit,
) -> None:
    """Announce that ``unit`` is on hold; nothing is written to state."""
    from rich.markup import escape

    primary = unit.primary_pr()
    ref = pr_ref_label(
        primary.repo_slug, primary.number, get_origin_repo_slug(config),
    )
    why = f": {unit.hold_reason}" if unit.hold_reason else ""
    console.print(
        f"\n    [yellow]⏸[/yellow] [cyan]{unit.feature_id}[/cyan] ({ref}) "
        f"— on hold{escape(why)}\n"
        "      [dim]drop it from pr_sources.on_hold (or comment on the "
        "graph issue) to put it back in work[/dim]"
    )
    _dry_record(state, "skip-on-hold")


def _process_feature_unit(
    config: Config,
    repo_path: Path,
    state: PipelineState,
    unit: FeatureUnit,
    base_branch: str,
    base_ref: str,
    onto: str,
    remote: str,
    ai_active: bool,
    retry_failed: bool = True,
    force_merge: bool = False,
) -> str:
    """Process one feature unit (single PR or sequential group). Always returns ``"continue"``."""
    origin_slug = get_origin_repo_slug(config)
    canonical_branch = config.feature_branch_name(unit.feature_id, onto)
    label = (
        f"group {unit.group_id} ({len(unit.prs)} PRs)"
        if unit.is_group
        else (
            f"PR {pr_ref_label(unit.primary_pr().repo_slug, unit.primary_pr().number, origin_slug)}: "
            f"{unit.primary_pr().title}"
        )
    )

    prev_state = state.features.get(unit.feature_id)
    is_failed_prev = prev_state is not None and prev_state.status == "conflict"
    is_build_failed_prev = (
        prev_state is not None and prev_state.status == "build_failed"
    )
    force_retry = is_failed_prev and retry_failed

    if unit.depends_on:
        unmet = _unmet_deps(unit, state)
        if unmet:
            console.print(
                f"\n    [yellow]⏸[/yellow] [cyan]{canonical_branch}[/cyan] "
                f"({label}) — blocked by: {', '.join(unmet)}\n"
                f"      [dim]re-run after merging the upstream cherry-pick "
                f"PRs into {base_branch}[/dim]"
            )
            blocked_state = prev_state or FeatureState()
            blocked_state.status = "blocked"
            blocked_state.blocked_by = list(unmet)
            # Keep prior branch / PR / AI-cost fields; clear stale conflict bookkeeping.
            blocked_state.conflict_files = []
            blocked_state.failed_step_index = None
            blocked_state.partial_pr_count = None
            blocked_state.prereq_recovery_exhausted = False
            blocked_state.stall = make_stall(
                "waiting_for_merge",
                detail="declared depends_on",
                waiting_on_units=list(unmet),
                prior=prev_state,
            )
            state.features[unit.feature_id] = blocked_state
            _persist_state(config, state)
            _dry_record(state, "blocked-by-deps")
            return "continue"
        if prev_state and prev_state.status == "blocked":
            prev_state.status = "needs_review"
            prev_state.blocked_by = []
            prev_state.stall = None

    if (is_failed_prev or is_build_failed_prev) and not retry_failed:
        kind = "build-failed" if is_build_failed_prev else "previously conflicted"
        console.print(
            f"\n    [dim]{canonical_branch} ({label}) — {kind}, "
            "skipping (pr_policy.retry_failed: false / "
            "--no-retry-failed)[/dim]"
        )
        _dry_record(state, "skip-conflict-retry-off")
        return "continue"

    if _skip_for_stall(
        config, state, prev_state, unit.feature_id, canonical_branch, label,
    ):
        return "continue"

    # A parked build_failed unit has no PR, so it is resumed even under ``if_exists: recreate``.
    if (
        is_build_failed_prev and retry_failed
        and config.ai_resolve.deterministic_build
        and unit.if_exists != "append"
    ):
        resumed = _resume_build_failed_unit(
            config, repo_path, state, unit, prev_state,
            canonical_branch, base_branch, base_ref, label,
        )
        if resumed is not None:
            return resumed

    cap = config.pr_policy.max_partial_continue_attempts
    configured_if_exists = unit.if_exists
    auto_continued = False
    if _partial_continue_allowed(
        config, prev_state, unit.if_exists, retry_failed,
    ):
        attempts = prev_state.partial_continue_attempts
        if attempts >= cap:
            console.print(
                f"\n    [yellow]⏭[/yellow]  [cyan]{canonical_branch}[/cyan] "
                f"({label}) — partial group: auto-continue cap reached "
                f"({attempts}/{cap}); leaving the draft PR for manual help. "
                "[dim](bump pr_policy.max_partial_continue_attempts to retry, "
                "or finish it by hand)[/dim]"
            )
            prev_state.stall = make_stall(
                "retries_exhausted",
                detail=f"auto-continue {attempts}/{cap}",
                prior=prev_state,
            )
            _persist_state(config, state)
            _dry_record(state, "skip-partial-continue-exhausted")
            return "continue"
        unit.if_exists = "append"
        unit.partial_continue_attempts = attempts + 1
        auto_continued = True
        console.print(
            f"\n    [yellow]↻[/yellow]  [cyan]{canonical_branch}[/cyan] "
            f"({label}) — resuming partial group from a prior run "
            f"(auto-continue attempt {attempts + 1}/{cap})"
        )

    on_remote_canon = remote_branch_exists(
        repo_path, canonical_branch, remote,
    )
    on_local_canon = local_branch_exists(repo_path, canonical_branch)

    if (
        force_retry and (on_remote_canon or on_local_canon)
        and unit.if_exists == "recreate"
    ):
        console.print(
            f"\n    [yellow]↻[/yellow] [cyan]{canonical_branch}[/cyan] ({label}) — "
            "previously conflicted, rebuilding from base "
            "([cyan]if_exists: recreate[/cyan] + [cyan]retry_failed: true[/cyan])"
        )

    # Opted-back-in ``closed`` / ``reverted``: the canonical branch carries a dead PR, so use a fresh name.
    recreate = _recreate_opt_in(
        config, prev_state.status if prev_state is not None else None,
    )
    prev_was_dead = recreate is not None

    new_branch = unit.port_branch or canonical_branch
    if recreate is not None and not force_retry:
        why, flag = recreate
        new_branch = _next_free_renumbered_port_branch(
            repo_path, remote, canonical_branch,
        )
        console.print(
            f"\n    [yellow]↻[/yellow] {why} — "
            f"opening a new port branch [cyan]{new_branch}[/cyan] "
            f"([cyan]pr_policy.{flag}[/cyan])"
        )

    on_remote = remote_branch_exists(repo_path, new_branch, remote)
    on_local = local_branch_exists(repo_path, new_branch)

    # An open rebase PR is left to ``releasy refresh``, unless members must be appended.
    if (
        on_remote
        and prev_state is not None
        and prev_state.rebase_pr_url
        and unit.if_exists != "append"
        and not prev_was_dead
    ):
        if unit.is_group and _group_branch_missing_members(
            repo_path, base_ref,
            new_branch if on_local else f"{remote}/{new_branch}",
            unit, origin_slug,
        ):
            unit.if_exists = "append"
        else:
            console.print(
                f"\n    [dim]{new_branch} ({label}) — rebase PR already "
                f"open ({prev_state.rebase_pr_url}); leaving as-is. "
                "Use [cyan]releasy refresh[/cyan] to merge target in.[/dim]"
            )
            _dry_record(state, "skip-existing-pr")
            return "continue"

    # ``if_exists`` alone decides branch disposition; ``force_retry`` does not override it.
    append_active = False
    cherry_pick_base = base_ref
    if (
        unit.if_exists == "append"
        and (on_remote or on_local)
    ):
        if not unit.is_group:
            console.print(
                f"\n    [yellow]![/yellow] [cyan]{new_branch}[/cyan] "
                f"({label}) — if_exists: append is meaningful only for "
                "groups; treating as 'skip' for this singleton"
            )
            _ensure_pr_for_existing_remote_branch(
                config, state, unit, new_branch, base_branch,
            )
            return "continue"
        else:
            if not on_local:
                run_git(
                    ["branch", "-f", new_branch, f"{remote}/{new_branch}"],
                    repo_path,
                )
                on_local = True
            decision = _decide_append(
                repo_path, base_ref, new_branch, unit, origin_slug,
            )
            if not decision.feasible and auto_continued:
                console.print(
                    f"\n    [yellow]![/yellow] [cyan]{new_branch}[/cyan] "
                    f"({label}) — cannot resume the partial group: "
                    f"{decision.reason}; falling back to "
                    f"[cyan]if_exists: {configured_if_exists}[/cyan]"
                )
                unit.if_exists = configured_if_exists
                unit.partial_continue_attempts = (
                    prev_state.partial_continue_attempts if prev_state else 0
                )
            elif not decision.feasible:
                console.print(
                    f"\n    [yellow]![/yellow] [cyan]{new_branch}[/cyan] "
                    f"({label}) — append not feasible: {decision.reason}; "
                    "skipping (set if_exists: recreate to rebuild)"
                )
                _ensure_pr_for_existing_remote_branch(
                    config, state, unit, new_branch, base_branch,
                )
                return "continue"
            elif decision.missing_count == 0:
                console.print(
                    f"\n    [cyan]{new_branch}[/cyan] ({label}) — every "
                    "declared PR is already on the branch; nothing to append"
                )
                _ensure_pr_for_existing_remote_branch(
                    config, state, unit, new_branch, base_branch,
                )
                return "continue"
            else:
                unit.applied_pr_urls = decision.applied_urls
                append_active = True
                cherry_pick_base = run_git(
                    ["rev-parse", "--verify", new_branch], repo_path,
                ).stdout.strip()
                console.print(
                    f"\n    [yellow]+[/yellow] [cyan]{new_branch}[/cyan] "
                    f"({label}) — appending {decision.missing_count} new PR(s) "
                    f"on top of {len(decision.applied_urls)} already applied"
                )

    if on_remote and unit.if_exists != "recreate" and not append_active:
        console.print(
            f"\n    [cyan]{new_branch}[/cyan] ({label}) — already exists on "
            f"[cyan]{remote}[/cyan], skipping cherry-pick "
            "(set [cyan]if_exists: recreate[/cyan] to rebuild from base, "
            "or [cyan]if_exists: append[/cyan] to add new PRs on top)"
        )
        _ensure_pr_for_existing_remote_branch(
            config, state, unit, new_branch, base_branch,
        )
        return "continue"

    if on_local and unit.if_exists == "skip":
        console.print(
            f"\n    [cyan]{new_branch}[/cyan] ({label}) — local branch "
            "exists, skipping ([cyan]if_exists: skip[/cyan]; "
            "set [cyan]if_exists: recreate[/cyan] to rebuild from base, "
            "or [cyan]if_exists: append[/cyan] to add new PRs on top)"
        )
        return "continue"

    if on_local and unit.if_exists == "recreate" and not append_active:
        console.print(
            f"\n    [yellow]↻[/yellow] [cyan]{new_branch}[/cyan] exists "
            "locally, rebuilding from base ([cyan]if_exists: recreate[/cyan])"
        )

    desc = (
        unit.title_prefix or unit.group_id or unit.feature_id
        if unit.is_group
        else (
            f"{unit.title_prefix}{unit.primary_pr().title}"
            if unit.title_prefix else unit.primary_pr().title
        )
    )
    config.features.append(FeatureConfig(
        id=unit.feature_id, description=desc,
        source_branch="", enabled=True,
    ))

    # Each pass cherry-picks ``unit.prs``; a missing-prereq report may prepend prereqs and restart.
    fs_dynamic_prereq_urls: list[str] = []
    fs_prereq_trail: list[dict] = []
    prereq_discovery_depth = 0
    auto_cfg = config.ai_resolve.auto_add_prerequisite_prs

    while True:
        console.print(f"\n  [cyan]{new_branch}[/cyan] ({label})")
        for pr in unit.prs:
            ref = pr_ref_label(pr.repo_slug, pr.number, origin_slug)
            tag = ""
            if pr.url in fs_dynamic_prereq_urls:
                tag = " [dim](auto-prereq)[/dim]"
            console.print(f"    PR {ref}: {pr.url}  [{pr.state}]{tag}")

        if config.dry_run:
            if append_active:
                action = (
                    f"cherry-pick {len(unit.prs)} PR(s) onto existing branch tip "
                    f"(append)"
                )
                action_code = "append"
            elif force_retry:
                action = (
                    f"rebuild [cyan]{new_branch}[/cyan] from [cyan]{base_ref}[/cyan] "
                    f"and cherry-pick {len(unit.prs)} PR(s) (retry-failed)"
                )
                action_code = "rebuild-from-base"
            else:
                action = (
                    f"create [cyan]{new_branch}[/cyan] from [cyan]{base_ref}[/cyan] "
                    f"and cherry-pick {len(unit.prs)} PR(s)"
                )
                action_code = "fresh-port"
            console.print(f"    [magenta]dry-run:[/magenta] would {action}")
            console.print(
                "    [magenta]dry-run:[/magenta] then push + open rebase PR "
                "(conflict outcome unknown — not simulated)"
            )
            _dry_record(state, action_code)
            return "continue"

        pr_meta = _unit_pr_meta(unit)
        stash_and_clean(repo_path)
        create_branch_from_ref(repo_path, new_branch, cherry_pick_base)

        outcome = _attempt_cherry_picks(
            config, repo_path, unit, new_branch, base_branch, ai_active,
            origin_slug,
        )

        if outcome.kind == "success":
            _maybe_synthesize_changelog(config, unit, base_branch)
            if _should_verify_build(config, unit):
                vres = _run_verify_phase(
                    config, repo_path, unit, new_branch, base_branch, onto,
                )
                if not vres.success:
                    _park_build_failed(
                        config, repo_path, state, unit, new_branch, onto,
                        vres, resume_attempts=0,
                    )
                    return "continue"
            _finish_clean_unit(
                config, repo_path, state, unit, new_branch, base_branch,
                onto, pr_meta,
                was_failed_prev=is_failed_prev,
                dynamic_prereq_urls=fs_dynamic_prereq_urls,
                prereq_trail=fs_prereq_trail,
                prereq_discovery_depth=prereq_discovery_depth,
            )
            return "continue"

        if outcome.kind == "already_applied":
            _handle_already_in_target(
                config, repo_path, state, unit, new_branch, base_ref,
                outcome.already_in_target_urls, onto, pr_meta,
            )
            return "continue"

        if outcome.kind == "missing_prereqs":
            should_dive, exit_reason = _decide_prereq_dive(
                config, state, unit, outcome, fs_dynamic_prereq_urls,
                prereq_discovery_depth,
            )
            if not should_dive:
                _handle_missing_prereqs_no_dive(
                    config, repo_path, state, unit, new_branch, base_branch,
                    base_ref, onto, outcome, pr_meta,
                    fs_dynamic_prereq_urls=fs_dynamic_prereq_urls,
                    fs_prereq_trail=fs_prereq_trail,
                    prereq_discovery_depth=prereq_discovery_depth,
                    exit_reason=exit_reason,
                )
                return "continue"

            prereq_infos, fetch_failed = _fetch_prereq_prs(
                config, exit_reason["dive_urls"],
            )
            if fetch_failed:
                console.print(
                    f"    [yellow]![/yellow] Could not fetch "
                    f"{len(fetch_failed)} prereq PR(s) — falling back to "
                    "detection-only labelling:"
                )
                for url in fetch_failed:
                    console.print(f"      • {url}")
                _handle_missing_prereqs_no_dive(
                    config, repo_path, state, unit, new_branch, base_branch,
                    base_ref, onto, outcome, pr_meta,
                    fs_dynamic_prereq_urls=fs_dynamic_prereq_urls,
                    fs_prereq_trail=fs_prereq_trail,
                    prereq_discovery_depth=prereq_discovery_depth,
                    exit_reason={"reason": "fetch_failed",
                                 "failed_urls": fetch_failed},
                )
                return "continue"

            unlabeled = _reject_unlabeled_origin_prereqs(
                config, outcome.failed_pr, prereq_infos,
            )
            if unlabeled:
                console.print(
                    f"    [yellow]![/yellow] {len(unlabeled)} discovered "
                    "prereq(s) are in origin but lack the configured "
                    "selection labels — falling back to detection-only "
                    "(label them or add to include_prs to port):"
                )
                for pi in unlabeled:
                    console.print(
                        f"      • {pr_ref_label(pi.repo_slug, pi.number, origin_slug)}"
                        f" {pi.url}"
                    )
                _handle_missing_prereqs_no_dive(
                    config, repo_path, state, unit, new_branch, base_branch,
                    base_ref, onto, outcome, pr_meta,
                    fs_dynamic_prereq_urls=fs_dynamic_prereq_urls,
                    fs_prereq_trail=fs_prereq_trail,
                    prereq_discovery_depth=prereq_discovery_depth,
                    exit_reason={"reason": "unlabeled_origin_prereq",
                                 "unlabeled_urls": [pi.url for pi in unlabeled]},
                )
                return "continue"

            prereq_discovery_depth += 1
            new_dynamic_urls = [pi.url for pi in prereq_infos]
            fs_dynamic_prereq_urls = new_dynamic_urls + fs_dynamic_prereq_urls
            fs_prereq_trail.append({
                "at_depth": prereq_discovery_depth,
                "triggering_pr": outcome.failed_pr.url,
                "discovered": new_dynamic_urls,
                "reason": outcome.missing_prereq_note or "",
            })
            unit.prs = prereq_infos + unit.prs

            console.print(
                f"\n    [magenta]↻ auto-prereq dive #{prereq_discovery_depth}"
                f"/{auto_cfg.max_prereq_depth}[/magenta] — prepending "
                f"{len(prereq_infos)} prereq PR(s) to unit and restarting:"
            )
            for pi in prereq_infos:
                console.print(
                    f"      → {pr_ref_label(pi.repo_slug, pi.number, origin_slug)}"
                    f" {pi.url}"
                )

            _persist_dive_progress(
                config, state, unit, new_branch, onto, pr_meta,
                fs_dynamic_prereq_urls=fs_dynamic_prereq_urls,
                fs_prereq_trail=fs_prereq_trail,
                prereq_discovery_depth=prereq_discovery_depth,
            )
            continue

        # The resolver never judged the conflict, so the auto-continue attempt is refunded.
        if outcome.api_aborted and unit.partial_continue_attempts:
            unit.partial_continue_attempts -= 1
            console.print(
                "    [dim]resolver never ran (API/backend failure) — "
                "auto-continue attempt not counted[/dim]"
            )
        _handle_unresolved_conflict(
            config, repo_path, state, unit, new_branch, base_branch,
            base_ref, onto, outcome.failed_idx, outcome.failed_pr,
            outcome.conflict_files, pr_meta,
            ai_attempted=ai_active,
            api_aborted=outcome.api_aborted,
            dynamic_prereq_urls=fs_dynamic_prereq_urls,
            prereq_trail=fs_prereq_trail,
            prereq_discovery_depth=prereq_discovery_depth,
        )
        return "continue"


@dataclass
class _CherryPickOutcome:
    """Outcome of one ``_attempt_cherry_picks`` pass."""
    # "success" | "already_applied" (every PR already in target) | "unresolved" | "missing_prereqs"
    kind: str
    failed_idx: int = 0
    failed_pr: PRInfo | None = None
    conflict_files: list[str] = field(default_factory=list)
    missing_prereq_prs: list[str] = field(default_factory=list)
    missing_prereq_note: str | None = None
    already_in_target_urls: list[str] = field(default_factory=list)
    # The AI step died before judging the conflict.
    api_aborted: bool = False


def _attempt_cherry_picks(
    config: Config,
    repo_path: Path,
    unit: FeatureUnit,
    new_branch: str,
    base_branch: str,
    ai_active: bool,
    origin_slug: str | None,
) -> _CherryPickOutcome:
    """Cherry-pick ``unit.prs`` (minus ``applied_pr_urls``) into the current branch, stopping at the first unresolved conflict."""
    already_in_target: list[str] = []
    real_pick_count = 0

    for idx, pr in enumerate(unit.prs):
        ref = pr_ref_label(pr.repo_slug, pr.number, origin_slug)
        if pr.url in unit.applied_pr_urls:
            console.print(
                f"    [dim]→ {ref} ({idx + 1}/{len(unit.prs)}) "
                "already on branch (append mode), skipping[/dim]"
            )
            continue
        if len(unit.prs) > 1:
            console.print(
                f"    [dim]→ cherry-picking {ref} "
                f"({idx + 1}/{len(unit.prs)})[/dim]"
            )

        # Rollback target if AI resolution fails.
        head_before = run_git(
            ["rev-parse", "--verify", "HEAD"], repo_path, check=False,
        )
        start_sha = head_before.stdout.strip() if head_before.returncode == 0 else None

        cp_result = _cherry_pick_pr(repo_path, config, pr)

        if cp_result.success:
            _tag_commit_with_source_pr(repo_path, unit, pr, origin_slug)
            real_pick_count += 1
            continue

        # Empty cherry-pick, already ``--skip``'d by git_ops.
        if cp_result.already_applied:
            console.print(
                f"    [dim]↳ {ref} already in target — skipped "
                "(empty cherry-pick)[/dim]"
            )
            already_in_target.append(pr.url)
            continue

        msg = f"Conflict on {ref}!"
        if cp_result.error_message and not cp_result.conflict_files:
            msg = f"{msg} ({cp_result.error_message})"
        console.print(f"    [red]✗[/red] {msg}")
        for cf in cp_result.conflict_files:
            console.print(f"      [red]•[/red] {cf}")

        # Split-commit mode: commit the conflict markers as-is; the AI's resolution goes on top.
        pre_resolve_sha: str | None = None
        if ai_active and config.ai_resolve.split_conflict_commit:
            committed, pre_resolve_sha = commit_cherry_pick_conflict_as_is(
                repo_path, source_pr_url=pr.url,
            )
            if committed and pre_resolve_sha:
                console.print(
                    f"    [dim]✎ committed conflict markers as "
                    f"{pre_resolve_sha[:12]} — Claude will add a "
                    f"resolution commit on top[/dim]"
                )
            else:
                console.print(
                    "    [yellow]![/yellow] could not pre-commit conflict "
                    "markers — falling back to single-commit AI resolve"
                )
                pre_resolve_sha = None

        ai_outcome: _AIStepOutcome | None = None
        if ai_active:
            ai_outcome = _try_ai_resolve_step(
                config, repo_path, unit, new_branch, base_branch, pr,
                cp_result.conflict_files,
                start_sha=start_sha,
                pre_resolve_sha=pre_resolve_sha,
            )

        if ai_outcome is not None and ai_outcome.handled:
            _tag_commit_with_source_pr(repo_path, unit, pr, origin_slug)
            real_pick_count += 1
            continue

        if ai_outcome is not None and ai_outcome.missing_prereq_prs:
            return _CherryPickOutcome(
                kind="missing_prereqs",
                failed_idx=idx,
                failed_pr=pr,
                conflict_files=cp_result.conflict_files,
                missing_prereq_prs=ai_outcome.missing_prereq_prs,
                missing_prereq_note=ai_outcome.missing_prereq_note,
            )

        return _CherryPickOutcome(
            kind="unresolved",
            failed_idx=idx,
            failed_pr=pr,
            conflict_files=cp_result.conflict_files,
            api_aborted=ai_outcome is not None and ai_outcome.api_aborted,
        )

    if real_pick_count == 0 and already_in_target:
        return _CherryPickOutcome(
            kind="already_applied",
            already_in_target_urls=already_in_target,
        )
    return _CherryPickOutcome(
        kind="success", already_in_target_urls=already_in_target,
    )


def _decide_prereq_dive(
    config: Config,
    state: PipelineState,
    unit: FeatureUnit,
    outcome: _CherryPickOutcome,
    fs_dynamic_prereq_urls: list[str],
    prereq_discovery_depth: int,
) -> tuple[bool, dict]:
    """Decide whether to auto-port reported missing prereqs.

    Returns ``(should_dive, exit_reason)``; ``exit_reason["reason"]`` is
    ``ok`` (with ``dive_urls``), ``detection_only``, ``queued_elsewhere``,
    ``cycle`` or ``depth_exhausted``.
    """
    auto_cfg = config.ai_resolve.auto_add_prerequisite_prs

    if not auto_cfg.enabled:
        return False, {"reason": "detection_only"}

    queued = _find_already_queued_prereqs(
        config, state, outcome.missing_prereq_prs,
        exclude_feature_id=unit.feature_id,
    )
    if queued:
        return False, {"reason": "queued_elsewhere", "queued": queued}

    unit_urls = {pr.url for pr in unit.prs}
    unit_urls.update(fs_dynamic_prereq_urls)
    cycle_hits = [u for u in outcome.missing_prereq_prs if u in unit_urls]
    if cycle_hits:
        return False, {"reason": "cycle", "cycle_urls": cycle_hits}

    if prereq_discovery_depth >= auto_cfg.max_prereq_depth:
        return False, {
            "reason": "depth_exhausted",
            "depth": prereq_discovery_depth,
            "max_depth": auto_cfg.max_prereq_depth,
        }

    return True, {"reason": "ok", "dive_urls": list(outcome.missing_prereq_prs)}


def _success_status(rebase_pr_url: str | None) -> str:
    """``needs_review`` once a port PR exists, else ``branch_created``."""
    return "needs_review" if rebase_pr_url else "branch_created"


def _ensure_pr_for_existing_remote_branch(
    config: Config,
    state: PipelineState,
    unit: FeatureUnit,
    new_branch: str,
    base_branch: str,
) -> None:
    """For a branch already on origin: open (or find) its PR and update state / labels. No-op without ``push``."""
    if not config.push:
        return

    if config.dry_run:
        verb = (
            "open PR (if missing) for"
            if config.pr_policy.auto_pr
            else "record state for"
        )
        console.print(
            f"    [magenta]dry-run:[/magenta] would {verb} existing remote "
            f"branch [cyan]{new_branch}[/cyan]"
        )
        _dry_record(
            state,
            "open-pr-for-existing-branch"
            if config.pr_policy.auto_pr
            else "record-existing-branch-state",
        )
        return

    fs = state.features.get(unit.feature_id)

    # A PR still labelled needs-attention was never reconciled; treat this as a deferred recovery.
    needs_recovery = False
    existing_pr_url = fs.rebase_pr_url if fs else None
    if existing_pr_url:
        existing_pr_number = _pr_number_from_url(existing_pr_url)
        if existing_pr_number and pr_has_label(
            config, existing_pr_number,
            config.ai_resolve.needs_attention_label,
        ):
            needs_recovery = True
            console.print(
                f"    [yellow]\u21bb[/yellow] PR carries stale "
                f"[cyan]{config.ai_resolve.needs_attention_label}[/cyan] "
                "label \u2014 treating this re-run as a deferred recovery"
            )

    if config.pr_policy.auto_pr:
        title = _unit_title(unit, config.project, base_branch)
        body = _unit_body(unit, get_origin_repo_slug(config))
        pr_url, outcome = _ensure_pr_for_branch(
            config, new_branch, base_branch, title, body,
            force_update=needs_recovery,
        )
        _log_pr_action(outcome, pr_url)
    else:
        pr_url = None

    if fs is None:
        fs = FeatureState(
            status=_success_status(pr_url), branch_name=new_branch,
            mode=unit.mode,
            **_unit_pr_meta(unit),
        )
        state.features[unit.feature_id] = fs
    if pr_url:
        fs.rebase_pr_url = pr_url
        fs.status = "needs_review"
        clear_conflict_markers(fs)
        _apply_releasy_label_to_pr(config, pr_url)
        _apply_session_labels_to_pr(config, pr_url, mode=unit.mode)
        if fs.ai_resolved:
            _apply_ai_label_to_pr(config, pr_url)
        if needs_recovery:
            relabelled = _reconcile_recovered_pr(config, pr_url)
            if relabelled and not fs.ai_resolved:
                fs.ai_resolved = True
    _persist_state(config, state)


def _should_verify_build(config: Config, unit: "FeatureUnit") -> bool:
    """Verify only with deterministic_build on and an AI-resolved conflict this run."""
    return config.ai_resolve.deterministic_build and unit.ai_resolved_count > 0


def _run_verify_phase(
    config: Config, repo_path: Path, unit: "FeatureUnit",
    branch: str, base_branch: str, onto: str,
) -> "VerifyResult":
    """Build the branch + run the PR's tests; accumulate AI cost on the unit."""
    from releasy.build_verify import verify_build_and_tests

    result = verify_build_and_tests(
        config, repo_path, unit.primary_pr(),
        port_branch=branch, base_branch=base_branch, base_sha=onto,
    )
    if result.cost_usd is not None:
        unit.ai_cost_usd_total = (
            (unit.ai_cost_usd_total or 0.0) + result.cost_usd
        )
    return result


def _park_build_failed(
    config: Config, repo_path: Path, state: PipelineState, unit: "FeatureUnit",
    branch: str, onto: str, result: "VerifyResult", *, resume_attempts: int,
) -> None:
    """Park the unit as ``build_failed``: push the branch, open no PR; resumed next run."""
    from releasy.ai_resolve import build_log_path

    fs = FeatureState(
        status="build_failed", branch_name=branch, base_commit=onto,
        **_unit_pr_meta(unit),
    )
    fs.ai_resolved = True
    fs.mode = unit.mode
    fs.build_attempts = result.build_attempts
    fs.verify_resume_attempts = resume_attempts
    fs.last_verify_error = result.error
    fs.stall = make_stall(
        "build_unfixed",
        detail=result.error or "",
        prior=state.features.get(unit.feature_id),
    )
    if unit.ai_cost_usd_total is not None:
        fs.ai_cost_usd = unit.ai_cost_usd_total

    if config.push:
        try:
            _push(config, repo_path, branch)
        except subprocess.CalledProcessError as exc:
            console.print(
                f"    [yellow]![/yellow] could not push [cyan]{branch}[/cyan] "
                f"[dim]({exc})[/dim]"
            )
        else:
            slug = get_origin_repo_slug(config)
            if slug and not config.dry_run:
                fs.branch_url = f"https://github.com/{slug}/tree/{branch}"
            console.print(f"    [green]✓[/green] Pushed [cyan]{branch}[/cyan]")
    else:
        console.print("    [dim]Skipping push[/dim]")

    state.features[unit.feature_id] = fs
    _persist_state(config, state)
    console.print(
        f"    [yellow]⏸ parked[/yellow] [cyan]{branch}[/cyan] as "
        f"[yellow]build_failed[/yellow] [dim]({result.error}; resumes next "
        f"run; build log: {repo_path / build_log_path(branch)})[/dim]"
    )


def _commits_behind(repo_path: Path, branch: str, base_ref: str) -> int:
    """Commits ``base_ref`` has that ``branch`` doesn't. 0 when unknowable."""
    res = run_git(
        ["rev-list", "--count", f"{branch}..{base_ref}"], repo_path,
        check=False,
    )
    if res.returncode != 0:
        return 0
    try:
        return int(res.stdout.strip())
    except ValueError:
        return 0


def _resume_build_failed_unit(
    config: Config, repo_path: Path, state: PipelineState, unit: "FeatureUnit",
    prev_state: FeatureState, branch: str, base_branch: str, base_ref: str,
    label: str,
) -> str | None:
    """Re-run build/tests on a parked ``build_failed`` branch; ``None`` means fall back to a fresh port."""
    cap = config.ai_resolve.max_verify_resume_attempts
    attempts = prev_state.verify_resume_attempts
    if cap <= 0 or attempts >= cap:
        console.print(
            f"\n    [yellow]⏭[/yellow]  [cyan]{branch}[/cyan] ({label}) — "
            f"build_failed resume cap reached ({attempts}/{cap}); leaving the "
            "branch for manual help. [dim](bump "
            "ai_resolve.max_verify_resume_attempts, or fix it by hand)[/dim]"
        )
        prev_state.stall = make_stall(
            "retries_exhausted",
            detail=(
                f"build resume {attempts}/{cap}"
                + (f" — {prev_state.last_verify_error}"
                   if prev_state.last_verify_error else "")
            ),
            prior=prev_state,
        )
        _persist_state(config, state)
        _dry_record(state, "skip-build-failed-exhausted")
        return "continue"

    onto = prev_state.base_commit
    if not onto or not local_branch_exists(repo_path, branch):
        return None

    drift_cap = config.ai_resolve.max_resume_base_drift
    behind = _commits_behind(repo_path, branch, base_ref) if drift_cap else 0
    if drift_cap and behind > drift_cap:
        console.print(
            f"\n    [yellow]↺[/yellow]  [cyan]{branch}[/cyan] ({label}) — "
            f"parked resolution is {behind} commits behind {base_ref} "
            f"(cap {drift_cap}); re-porting from base instead of building "
            "stale code [dim](ai_resolve.max_resume_base_drift)[/dim]"
        )
        return None

    if config.dry_run:
        console.print(
            f"\n    [magenta]·[/magenta] [cyan]{branch}[/cyan] ({label}) — "
            f"would resume build/test on the existing resolution "
            f"(resume attempt {attempts + 1}/{cap})"
        )
        _dry_record(state, "resume-build-failed")
        return "continue"

    stash_and_clean(repo_path)
    co = run_git(["checkout", branch], repo_path, check=False)
    if co.returncode != 0:
        console.print(
            f"    [yellow]![/yellow] could not checkout {branch} to resume "
            "build_failed — re-porting from scratch"
        )
        return None

    console.print(
        f"\n    [yellow]↻[/yellow]  [cyan]{branch}[/cyan] ({label}) — resuming "
        f"build/test on the existing resolution "
        f"(resume attempt {attempts + 1}/{cap})"
    )
    if prev_state.ai_resolved and unit.ai_resolved_count == 0:
        unit.ai_resolved_count = 1
    if prev_state.ai_cost_usd is not None and unit.ai_cost_usd_total is None:
        unit.ai_cost_usd_total = prev_state.ai_cost_usd

    vres = _run_verify_phase(config, repo_path, unit, branch, base_branch, onto)
    if vres.success:
        _maybe_synthesize_changelog(config, unit, base_branch)
        _finish_clean_unit(
            config, repo_path, state, unit, branch, base_branch, onto,
            _unit_pr_meta(unit), was_failed_prev=True,
        )
        return "continue"

    if vres.outcome == "error":
        console.print(
            "    [dim]build/tests never ran (environment fault) — resume "
            "attempt not counted[/dim]"
        )
    _park_build_failed(
        config, repo_path, state, unit, branch, onto, vres,
        resume_attempts=attempts if vres.outcome == "error" else attempts + 1,
    )
    return "continue"


def _finish_clean_unit(
    config: Config,
    repo_path: Path,
    state: PipelineState,
    unit: FeatureUnit,
    new_branch: str,
    base_branch: str,
    onto: str,
    pr_meta: dict,
    *,
    was_failed_prev: bool = False,
    dynamic_prereq_urls: list[str] | None = None,
    prereq_trail: list[dict] | None = None,
    prereq_discovery_depth: int = 0,
) -> None:
    """Push the branch, open / update the unit's PR, and record state and labels.

    ``was_failed_prev`` force-rewrites a stale PR and reconciles it as recovered.
    """
    ai_used = unit.ai_resolved_count > 0
    has_auto_prereqs = bool(dynamic_prereq_urls)
    verify_flagged = unit.verify_needs_attention

    if config.push:
        _push(config, repo_path, new_branch)
        console.print(f"    [green]✓[/green] Pushed [cyan]{new_branch}[/cyan]")
    else:
        console.print("    [dim]Skipping push[/dim]")

    fs = FeatureState(
        status="branch_created" if config.push else "needs_review",
        branch_name=new_branch, base_commit=onto, **pr_meta,
    )
    if ai_used:
        fs.ai_resolved = True
        fs.ai_iterations = unit.ai_iterations_total or None
    if unit.ai_cost_usd_total is not None:
        fs.ai_cost_usd = unit.ai_cost_usd_total
    if verify_flagged:
        fs.verify_needs_attention = True
    fs.mode = unit.mode
    if has_auto_prereqs:
        fs.dynamic_prereq_urls = list(dynamic_prereq_urls or [])
        fs.prereq_trail = list(prereq_trail or [])
        fs.prereq_discovery_depth = prereq_discovery_depth
    state.features[unit.feature_id] = fs

    appended_to_existing = bool(unit.applied_pr_urls)

    dropped_items = _collect_dropped_trailers(repo_path, onto, new_branch)

    if config.push and config.pr_policy.auto_pr:
        title = _unit_title(unit, config.project, base_branch)
        rebase_pr_url, outcome = _ensure_pr_for_branch(
            config, new_branch, base_branch, title,
            _unit_body(
                unit, get_origin_repo_slug(config),
                auto_prereq_urls=dynamic_prereq_urls,
                auto_prereq_trail=prereq_trail,
                dropped_items=dropped_items,
            ),
            force_update=(
                was_failed_prev or has_auto_prereqs
                or appended_to_existing or bool(dropped_items)
            ),
        )
        _log_pr_action(outcome, rebase_pr_url)
        if rebase_pr_url:
            state.features[unit.feature_id].rebase_pr_url = rebase_pr_url
            state.features[unit.feature_id].status = "needs_review"
            _apply_releasy_label_to_pr(config, rebase_pr_url)
            _apply_session_labels_to_pr(
                config, rebase_pr_url, mode=unit.mode,
            )
            if ai_used:
                _apply_ai_label_to_pr(config, rebase_pr_url)
            if has_auto_prereqs:
                _apply_auto_prereq_label_to_pr(config, rebase_pr_url)
            if verify_flagged:
                _apply_verify_label_to_pr(config, rebase_pr_url)
                fs_for_unit = state.features[unit.feature_id]
                if not fs_for_unit.verify_comment_posted:
                    posted = _post_verify_findings_comment(
                        config, rebase_pr_url, list(unit.verify_findings),
                    )
                    if posted:
                        fs_for_unit.verify_comment_posted = True
            if was_failed_prev:
                relabelled = _reconcile_recovered_pr(config, rebase_pr_url)
                if relabelled and not state.features[unit.feature_id].ai_resolved:
                    state.features[unit.feature_id].ai_resolved = True
    elif config.push and (ai_used or has_auto_prereqs or verify_flagged):
        existing = find_pr_for_branch(config, new_branch, base_branch)
        if existing:
            _apply_releasy_label_to_pr(
                config, existing.url, pr_number=existing.number,
            )
            _apply_session_labels_to_pr(
                config, existing.url, pr_number=existing.number,
                mode=unit.mode,
            )
            if ai_used:
                _apply_ai_label_to_pr(
                    config, existing.url, pr_number=existing.number,
                )
            if has_auto_prereqs:
                _apply_auto_prereq_label_to_pr(
                    config, existing.url, pr_number=existing.number,
                )
            if verify_flagged:
                _apply_verify_label_to_pr(
                    config, existing.url, pr_number=existing.number,
                )
                fs_for_unit = state.features[unit.feature_id]
                if not fs_for_unit.verify_comment_posted:
                    posted = _post_verify_findings_comment(
                        config, existing.url, list(unit.verify_findings),
                    )
                    if posted:
                        fs_for_unit.verify_comment_posted = True
            state.features[unit.feature_id].rebase_pr_url = existing.url
            state.features[unit.feature_id].status = "needs_review"
            if was_failed_prev:
                _reconcile_recovered_pr(
                    config, existing.url, pr_number=existing.number,
                )

    _persist_state(config, state)


def _reconcile_recovered_pr(
    config: Config, pr_url: str, pr_number: int | None = None,
) -> bool:
    """Swap needs-attention for the ai-resolved label and mark the PR ready (best-effort).

    Returns True when the PR carried the needs-attention label.
    """
    if pr_number is None:
        pr_number = _pr_number_from_url(pr_url)
    if pr_number is None:
        return False

    needs_attention_label = config.ai_resolve.needs_attention_label
    had_needs_attention = pr_has_label(
        config, pr_number, needs_attention_label,
    )

    if had_needs_attention:
        if remove_label_from_pr(config, pr_number, needs_attention_label):
            console.print(
                f"    [green]✓[/green] Removed [cyan]"
                f"{needs_attention_label}[/cyan] label "
                "from previously-conflicted PR"
            )
        _apply_ai_label_to_pr(config, pr_url, pr_number=pr_number)

    ready = mark_pr_ready_for_review(config, pr_number)
    if ready is True:
        console.print(
            "    [green]✓[/green] Marked PR ready for review "
            "(was draft after previous failure)"
        )
    elif ready is False:
        console.print(
            "    [yellow]![/yellow] Could not mark PR ready for review — "
            "flip it manually on GitHub if it's still in draft"
        )

    return had_needs_attention


def _ensure_pr_for_branch(
    config: Config,
    branch: str,
    base_branch: str,
    title: str,
    body: str,
    *,
    force_update: bool = False,
) -> tuple[str | None, str]:
    """Create a PR for ``branch`` or reuse an open one (rewritten if ``update_existing_prs`` or ``force_update``).

    Returns ``(url, "created" | "updated" | "reused" | "failed")``.
    """
    existing = find_pr_for_branch(config, branch, base_branch)
    if existing:
        if config.update_existing_prs or force_update:
            ok = update_pull_request(
                config, existing.number, title=title, body=body,
            )
            if ok:
                return existing.url, "updated"
            return existing.url, "reused"
        return existing.url, "reused"

    url = create_pull_request(config, branch, base_branch, title, body)
    if url:
        return url, "created"
    return None, "failed"


def _log_pr_action(outcome: str, url: str | None) -> None:
    """Pretty-print the result of ``_ensure_pr_for_branch``."""
    if outcome == "created" and url:
        console.print(
            f"    [green]✓[/green] PR opened: [link={url}]{url}[/link]"
        )
    elif outcome == "updated" and url:
        console.print(
            f"    [green]✓[/green] PR updated: [link={url}]{url}[/link]"
        )
    elif outcome == "reused" and url:
        console.print(
            f"    [dim]PR already open — left as-is: "
            f"[link={url}]{url}[/link] "
            f"(set [cyan]update_existing_prs: true[/cyan] to overwrite "
            f"title/body)[/dim]"
        )
    else:
        console.print(
            "    [yellow]![/yellow] Branch pushed but PR not created "
            "(see warnings above — common causes: PR already exists but "
            "was closed, or head/base have no difference)"
        )


def _apply_ai_label_to_pr(
    config: Config, pr_url: str, pr_number: int | None = None,
) -> None:
    """Best-effort: add the ai_resolve.label to the PR identified by URL."""
    if pr_number is None:
        pr_number = _pr_number_from_url(pr_url)
    if pr_number is None:
        return
    ok = add_label_to_pr(config, pr_number, config.ai_resolve.label)
    if ok:
        console.print(
            f"    [magenta]🤖[/magenta] Labelled PR with "
            f"[magenta]{config.ai_resolve.label}[/magenta]"
        )
    else:
        console.print(
            f"    [yellow]![/yellow] Could not add label "
            f"'{config.ai_resolve.label}' to PR"
        )


def _apply_releasy_label_to_pr(
    config: Config, pr_url: str, pr_number: int | None = None,
) -> None:
    """Best-effort: tag the PR with the ``releasy`` label."""
    if pr_number is None:
        pr_number = _pr_number_from_url(pr_url)
    if pr_number is None:
        return
    add_label_to_pr(config, pr_number, RELEASY_LABEL)


def _session_pr_labels(
    config: Config, mode: PortMode | None = None,
) -> list[str]:
    """``session.pr_labels`` plus the ``pr_labels_by_mode[mode]`` bucket."""
    if config.session is None:
        return []
    labels = list(config.session.pr_labels)
    if mode is not None:
        for name in config.session.pr_labels_by_mode.get(mode, []):
            if name not in labels:
                labels.append(name)
    return labels


def _all_session_label_names(config: Config) -> list[str]:
    """Every session label name, mode-conditional ones included."""
    if config.session is None:
        return []
    names = list(config.session.pr_labels)
    for bucket in config.session.pr_labels_by_mode.values():
        for name in bucket:
            if name not in names:
                names.append(name)
    return names


def _apply_session_labels_to_pr(
    config: Config, pr_url: str, pr_number: int | None = None,
    mode: PortMode | None = None,
) -> None:
    """Best-effort: attach this PR's session labels."""
    labels = _session_pr_labels(config, mode)
    if not labels:
        return
    if pr_number is None:
        pr_number = _pr_number_from_url(pr_url)
    if pr_number is None:
        return
    for label in labels:
        add_label_to_pr(config, pr_number, label)


def reconcile_session_labels_on_prs(
    config: Config,
    pr_refs: list[tuple[str, int, PortMode | None]],
) -> list[tuple[str, list[str]]]:
    """Add missing session labels to ``(pr_url, pr_number, mode)`` PRs; returns ``[(pr_url, added_labels)]``."""
    from releasy.github_ops import fetch_pr_by_url

    result: list[tuple[str, list[str]]] = []
    if not _all_session_label_names(config) or not pr_refs:
        return result
    for pr_url, pr_number, mode in pr_refs:
        labels = _session_pr_labels(config, mode)
        if not labels:
            continue
        info = fetch_pr_by_url(config, pr_url, include_closed=True)
        current = {
            (l or "").lower() for l in ((info.labels if info else None) or [])
        }
        missing = [l for l in labels if l.lower() not in current]
        if not missing:
            continue
        actually_added: list[str] = []
        for lbl in missing:
            if add_label_to_pr(config, pr_number, lbl):
                actually_added.append(lbl)
        if actually_added:
            result.append((pr_url, actually_added))
    return result


def _apply_auto_prereq_label_to_pr(
    config: Config, pr_url: str, pr_number: int | None = None,
) -> None:
    """Best-effort: apply ``auto_prereq_label`` to a PR whose scope auto-recovery expanded."""
    if pr_number is None:
        pr_number = _pr_number_from_url(pr_url)
    if pr_number is None:
        return
    add_label_to_pr(config, pr_number, config.ai_resolve.auto_prereq_label)


def _apply_verify_label_to_pr(
    config: Config, pr_url: str, pr_number: int | None = None,
) -> None:
    """Best-effort: apply ``verify_label`` to a PR the verifier flagged."""
    if pr_number is None:
        pr_number = _pr_number_from_url(pr_url)
    if pr_number is None:
        return
    ok = add_label_to_pr(config, pr_number, config.ai_resolve.verify_label)
    if ok:
        console.print(
            f"    [yellow]🔎[/yellow] Labelled PR with "
            f"[yellow]{config.ai_resolve.verify_label}[/yellow] "
            "[dim](verifier flagged the resolution)[/dim]"
        )


def _post_verify_findings_comment(
    config: Config, pr_url: str, findings: list[str],
) -> bool:
    """Post verifier findings as a top-level PR comment; True on success."""
    if not findings:
        return False

    body_lines = [
        "## RelEasy post-resolve verifier — needs attention",
        "",
        "The automated audit of the AI-resolved cherry-pick(s) on this "
        "PR raised one or more concerns. **No changes were rolled back** "
        "— please review the points below and either dismiss them as "
        "false positives or push corrections.",
        "",
    ]
    body_lines.extend(findings)
    body = "\n".join(body_lines).rstrip() + "\n"

    from releasy.config import get_github_token

    token = get_github_token()
    if not token:
        console.print(
            "    [yellow]![/yellow] RELEASY_GITHUB_TOKEN not set — skipping "
            "verifier-findings comment"
        )
        return False
    parsed = parse_pr_url(pr_url)
    if parsed is None:
        console.print(
            f"    [yellow]![/yellow] Could not parse PR URL for verifier "
            f"comment: {pr_url!r}"
        )
        return False
    owner, repo, number = parsed
    try:
        from github import Github

        gh = Github(token)
        ghrepo = gh.get_repo(f"{owner}/{repo}")
        pr = ghrepo.get_pull(number)
        ic = pr.create_issue_comment(body)
        console.print(
            f"    [yellow]💬[/yellow] Verifier findings posted: "
            f"[link={ic.html_url}]comment[/link]"
        )
        return True
    except Exception as exc:
        console.print(
            f"    [yellow]![/yellow] Could not post verifier-findings "
            f"comment: {exc}"
        )
        return False


def _ensure_upstream_remote(config: Config, repo_path: Path) -> None:
    """Register the configured upstream remote on the local clone (idempotent)."""
    if config.upstream is None:
        return
    changed = ensure_remote(
        repo_path, config.upstream.remote_name, config.upstream.remote,
    )
    if changed:
        console.print(
            f"    [dim]Registered upstream remote "
            f"[cyan]{config.upstream.remote_name}[/cyan] "
            f"→ {config.upstream.remote}[/dim]"
        )


def _find_already_queued_prereqs(
    config: Config,
    state: PipelineState,
    candidate_urls: list[str],
    *,
    exclude_feature_id: str | None = None,
) -> list[dict]:
    """Where state or config already tracks each of ``candidate_urls`` (matched by PR ref).

    Returns one dict per hit, in input order: ``prereq_url``, ``queued_in``
    (feature id / ``config:include_prs`` / ``config:groups[<id>]``),
    ``queued_in_pr_url``, ``carried`` and ``queued_status`` (``None`` for
    config-only hits). A unit listing the prereq wins over one that only
    carries it via ``contained_pr_urls`` (``carried=True``).
    """
    # ref → (queued_in, queued_in_pr_url, status)
    index: dict[
        tuple[str, str, int], tuple[str, str | None, str | None]
    ] = {}

    # State before config: only a tracked unit has a merge to wait for.
    for fid, fs in state.features.items():
        if fid == exclude_feature_id:
            continue
        for url in _source_pr_urls(fs):
            if not url:
                continue
            ref = parse_pr_url(url)
            if ref and ref not in index:
                index[ref] = (fid, fs.rebase_pr_url, fs.status)
        for url in fs.dynamic_prereq_urls:
            ref = parse_pr_url(url)
            if ref and ref not in index:
                index[ref] = (fid, fs.rebase_pr_url, fs.status)
        # A missing-prereq report may name our own in-flight port PR instead of its source PR.
        if fs.rebase_pr_url:
            ref = parse_pr_url(fs.rebase_pr_url)
            if ref and ref not in index:
                index[ref] = (fid, fs.rebase_pr_url, fs.status)

    for url in config.pr_sources.include_prs:
        ref = parse_pr_url(url)
        if ref and ref not in index:
            index[ref] = ("config:include_prs", None, None)

    for group in config.pr_sources.groups:
        for url in group.prs:
            ref = parse_pr_url(url)
            if ref and ref not in index:
                index[ref] = (f"config:groups[{group.id}]", None, None)

    carried: dict[
        tuple[str, str, int], tuple[str, str | None, str | None]
    ] = {}
    for fid, fs in state.features.items():
        if fid == exclude_feature_id:
            continue
        contained = list(fs.contained_pr_urls)
        if not contained:
            # Older state lacks ``contained_pr_urls``; parse the primary PR body instead.
            primary_slug = None
            primary_ref = parse_pr_url(fs.pr_url or "")
            if primary_ref:
                primary_slug = f"{primary_ref[0]}/{primary_ref[1]}"
            contained = [
                f"https://github.com/{o}/{r}/pull/{n}"
                for o, r, n in parse_cherry_picked_refs(
                    fs.pr_body, primary_slug,
                )
            ]
        for url in contained:
            ref = parse_pr_url(url)
            if ref and ref not in index and ref not in carried:
                carried[ref] = (fid, fs.rebase_pr_url, fs.status)

    out: list[dict] = []
    seen: set[tuple[str, str, int]] = set()
    for url in candidate_urls:
        ref = parse_pr_url(url)
        if ref is None or ref in seen:
            continue
        hit = index.get(ref)
        if hit is not None or ref in carried:
            queued_in, queued_pr_url, queued_status = (
                hit if hit is not None else carried[ref]
            )
            out.append({
                "prereq_url": url,
                "queued_in": queued_in,
                "queued_in_pr_url": queued_pr_url,
                "carried": hit is None,
                "queued_status": queued_status,
            })
            seen.add(ref)
    return out


def _matches_config_labels(config: Config, pr: PRInfo) -> bool:
    """True iff label-based discovery would select ``pr`` (all labels of some ``by_labels`` entry, no excluded label)."""
    pr_labels = {(lbl or "").lower() for lbl in (pr.labels or [])}
    exclude = {lbl.lower() for lbl in config.pr_sources.exclude_labels}
    if pr_labels & exclude:
        return False
    for entry in config.pr_sources.by_labels:
        wanted = {lbl.lower() for lbl in entry.labels}
        if wanted and wanted <= pr_labels:
            return True
    return False


def _reject_unlabeled_origin_prereqs(
    config: Config,
    triggering_pr: PRInfo | None,
    prereq_infos: list[PRInfo],
) -> list[PRInfo]:
    """Origin prereqs of an origin PR that lack the selection labels (``require_origin_prereq_label``)."""
    auto_cfg = config.ai_resolve.auto_add_prerequisite_prs
    if not auto_cfg.require_origin_prereq_label:
        return []
    if not config.pr_sources.by_labels:
        return []
    origin_slug = get_origin_repo_slug(config)
    if not origin_slug:
        return []
    if triggering_pr is not None and triggering_pr.repo_slug != origin_slug:
        return []
    rejected: list[PRInfo] = []
    for pr in prereq_infos:
        if pr.repo_slug != origin_slug:
            continue
        if not _matches_config_labels(config, pr):
            rejected.append(pr)
    return rejected


def _fetch_prereq_prs(
    config: Config, urls: list[str],
) -> tuple[list[PRInfo], list[str]]:
    """Fetch each prereq URL; returns ``(fetched, failed_urls)``."""
    fetched: list[PRInfo] = []
    failed: list[str] = []
    for url in urls:
        info = fetch_pr_by_url(config, url)
        if info is None:
            failed.append(url)
            continue
        fetched.append(info)
    return fetched, failed


def _persist_dive_progress(
    config: Config,
    state: PipelineState,
    unit: FeatureUnit,
    new_branch: str,
    onto: str,
    pr_meta: dict,
    *,
    fs_dynamic_prereq_urls: list[str],
    fs_prereq_trail: list[dict],
    prereq_discovery_depth: int,
) -> None:
    """Persist the in-progress dive trail (status ``branch_created``) between dives."""
    fs = state.features.get(unit.feature_id) or FeatureState()
    fs.status = "branch_created"
    fs.branch_name = new_branch
    fs.base_commit = onto
    for k, v in pr_meta.items():
        setattr(fs, k, v)
    fs.dynamic_prereq_urls = list(fs_dynamic_prereq_urls)
    fs.prereq_trail = list(fs_prereq_trail)
    fs.prereq_discovery_depth = prereq_discovery_depth
    if unit.ai_cost_usd_total is not None:
        fs.ai_cost_usd = unit.ai_cost_usd_total
    state.features[unit.feature_id] = fs
    _persist_state(config, state)


def _print_prereq_dive_failure(
    fs_prereq_trail: list[dict],
    exit_reason: dict,
    auto_cfg,
    final_discovered: list[str],
) -> None:
    """Print why the auto-prereq dive stopped and its dependency trail."""
    reason = exit_reason.get("reason")
    headline_map = {
        "depth_exhausted": (
            f"Auto-prereq dive hit the depth limit "
            f"(max_prereq_depth={auto_cfg.max_prereq_depth})."
        ),
        "cycle": "Auto-prereq dive aborted: cycle detected.",
        "queued_elsewhere": (
            "Auto-prereq dive aborted: discovered prereq is already queued "
            "elsewhere."
        ),
        "fetch_failed": "Auto-prereq dive aborted: prereq fetch failed.",
        "detection_only": (
            "Detection-only mode "
            "(set ai_resolve.auto_add_prerequisite_prs.enabled: true to "
            "auto-port)."
        ),
        "all_already_in_base": (
            "Auto-prereq dive aborted: all discovered prereqs are already "
            "merged into base_branch."
        ),
        "unlabeled_origin_prereq": (
            "Auto-prereq dive aborted: an in-origin prereq lacks the "
            "configured selection labels (out of scope)."
        ),
    }
    headline = headline_map.get(reason, "Auto-prereq dive aborted.")
    console.print(f"    [red]✗[/red] {headline}")

    if fs_prereq_trail:
        console.print("    [bold]Dependency trail:[/bold]")
        for i, entry in enumerate(fs_prereq_trail, start=1):
            trig = entry.get("triggering_pr") or "(unknown)"
            disc = entry.get("discovered", []) or []
            reason_txt = entry.get("reason") or ""
            disc_str = ", ".join(disc) or "(none)"
            line = (
                f"      {i}. {trig} → needed {disc_str}"
            )
            if reason_txt:
                line += f"  [dim]({reason_txt})[/dim]"
            console.print(line)

    if reason == "depth_exhausted":
        next_str = ", ".join(final_discovered) or "(none)"
        console.print(
            f"    [bold]Next prereq exceeding the limit:[/bold] {next_str}"
        )
        console.print(
            "    [dim]Consider porting the next prereq manually first, "
            "or bump ai_resolve.auto_add_prerequisite_prs.max_prereq_depth.[/dim]"
        )
    elif reason == "cycle":
        cyc = exit_reason.get("cycle_urls") or []
        cyc_str = ", ".join(cyc) or "(none)"
        console.print(
            f"    [bold]Cycle on:[/bold] {cyc_str}"
        )
    elif reason == "queued_elsewhere":
        queued = exit_reason.get("queued") or []
        for q in queued:
            url = q.get("prereq_url", "?")
            where = q.get("queued_in", "?")
            qpr = q.get("queued_in_pr_url")
            extra = f" → {qpr}" if qpr else ""
            verb = "carried by" if q.get("carried") else "queued in"
            console.print(
                f"      • {url} ({verb} {where}{extra})"
            )
        console.print(
            "    [dim]Action: wait for the queued unit's PR to merge, "
            "then re-run releasy.[/dim]"
        )
    elif reason == "fetch_failed":
        for url in exit_reason.get("failed_urls", []):
            console.print(f"      • could not fetch {url}")
    elif reason == "unlabeled_origin_prereq":
        for url in exit_reason.get("unlabeled_urls", []):
            console.print(f"      • unlabeled (in origin): {url}")
        console.print(
            "    [dim]Action: add the configured selection label(s) to the "
            "prereq PR(s), or list them in pr_sources.include_prs, then "
            "re-run releasy. Set "
            "ai_resolve.auto_add_prerequisite_prs.require_origin_prereq_label: "
            "false to disable this gate.[/dim]"
        )


def _queued_stall(
    queued: list[dict], *, prior: FeatureState | None,
) -> StallReason:
    """``waiting_for_merge`` naming the (deduped) units that already port the prereq."""
    units = dict.fromkeys(
        str(q.get("queued_in")) for q in queued if q.get("queued_in")
    )
    return make_stall(
        "waiting_for_merge",
        waiting_on_units=list(units),
        waiting_on_prs=[
            str(q.get("prereq_url")) for q in queued if q.get("prereq_url")
        ],
        prior=prior,
    )


def _prereq_stall(
    config: Config,
    state: PipelineState,
    unit: FeatureUnit,
    exit_reason: dict,
    discovered: list[str],
    auto_cfg,  # noqa: ANN001 — AutoAddPrereqConfig, imported lazily elsewhere
    depth: int,
    *,
    prior: FeatureState | None,
) -> StallReason:
    """Map a ``_decide_prereq_dive`` exit reason onto a stall."""
    reason = exit_reason.get("reason")
    if reason == "queued_elsewhere":
        return _queued_stall(exit_reason.get("queued") or [], prior=prior)
    if reason == "depth_exhausted":
        return make_stall(
            "prereq_search_exhausted",
            detail=f"depth {depth}/{auto_cfg.max_prereq_depth}",
            waiting_on_prs=list(discovered),
            prior=prior,
        )
    if reason == "cycle":
        return make_stall(
            "prereq_search_exhausted",
            detail="prereq cycle — the dive pointed back into the unit",
            waiting_on_prs=list(discovered),
            prior=prior,
        )
    if reason == "fetch_failed":
        return make_stall(
            "prereq_search_exhausted",
            detail="could not fetch the discovered prereq PR(s)",
            waiting_on_prs=list(exit_reason.get("failed_urls") or discovered),
            prior=prior,
        )
    if reason == "all_already_in_base":
        return make_stall(
            "prereq_search_exhausted",
            detail="every discovered prereq is already in base",
            waiting_on_prs=list(discovered),
            prior=prior,
        )
    if reason == "unlabeled_origin_prereq":
        return make_stall(
            "missing_prereq",
            detail="prereq is in origin but lacks the selection label(s)",
            waiting_on_prs=list(
                exit_reason.get("unlabeled_urls") or discovered
            ),
            prior=prior,
        )
    # The dive never ran, so neither did the queued-elsewhere check.
    queued = _find_already_queued_prereqs(
        config, state, discovered, exclude_feature_id=unit.feature_id,
    )
    if queued:
        return _queued_stall(queued, prior=prior)
    return make_stall(
        "missing_prereq",
        detail=(
            "auto-recovery off" if reason == "detection_only" else str(reason)
        ),
        waiting_on_prs=list(discovered),
        prior=prior,
    )


def _abort_git_op(repo_path: Path) -> None:
    if is_operation_in_progress(repo_path):
        run_git(["cherry-pick", "--abort"], repo_path, check=False)
        run_git(["merge", "--abort"], repo_path, check=False)
        run_git(["rebase", "--abort"], repo_path, check=False)


def _drop_local_branch(repo_path: Path, branch: str, base_ref: str) -> None:
    if local_branch_exists(repo_path, branch):
        run_git(["checkout", "--detach", base_ref], repo_path, check=False)
        run_git(["branch", "-D", branch], repo_path, check=False)


def _handle_missing_prereqs_no_dive(
    config: Config,
    repo_path: Path,
    state: PipelineState,
    unit: FeatureUnit,
    new_branch: str,
    base_branch: str,
    base_ref: str,
    onto: str,
    outcome: _CherryPickOutcome,
    pr_meta: dict,
    *,
    fs_dynamic_prereq_urls: list[str],
    fs_prereq_trail: list[dict],
    prereq_discovery_depth: int,
    exit_reason: dict,
) -> None:
    """Missing prereqs without a dive: report, drop the local branch, record ``conflict`` state."""
    auto_cfg = config.ai_resolve.auto_add_prerequisite_prs

    _abort_git_op(repo_path)

    final_discovered = list(outcome.missing_prereq_prs)
    exhausted = exit_reason.get("reason") in (
        "depth_exhausted", "cycle", "fetch_failed", "all_already_in_base",
    )

    _print_prereq_dive_failure(
        fs_prereq_trail, exit_reason, auto_cfg, final_discovered,
    )

    _drop_local_branch(repo_path, new_branch, base_ref)
    console.print(
        f"    [yellow]Dropped local branch[/yellow] [cyan]{new_branch}[/cyan]"
        " (auto-prereq dive aborted; nothing kept)."
    )

    fs = FeatureState(
        status="conflict",
        branch_name=None,
        base_commit=onto,
        conflict_files=outcome.conflict_files,
        failed_step_index=outcome.failed_idx,
        partial_pr_count=0,
        ai_cost_usd=unit.ai_cost_usd_total,
        missing_prereq_prs=final_discovered,
        missing_prereq_note=outcome.missing_prereq_note,
        dynamic_prereq_urls=list(fs_dynamic_prereq_urls),
        prereq_discovery_depth=prereq_discovery_depth,
        prereq_trail=list(fs_prereq_trail),
        prereq_recovery_exhausted=exhausted,
        queued_prereq_units=list(exit_reason.get("queued") or []),
        stall=_prereq_stall(
            config, state, unit, exit_reason, final_discovered, auto_cfg,
            prereq_discovery_depth,
            prior=state.features.get(unit.feature_id),
        ),
        **pr_meta,
    )
    state.features[unit.feature_id] = fs
    _persist_state(config, state)


def _handle_already_in_target(
    config: Config,
    repo_path: Path,
    state: PipelineState,
    unit: FeatureUnit,
    new_branch: str,
    base_ref: str,
    already_in_target_urls: list[str],
    onto: str,
    pr_meta: dict,
) -> None:
    """Drop the empty port branch of a unit already fully in target and mark it ``skipped``."""
    origin_slug = get_origin_repo_slug(config)

    _abort_git_op(repo_path)

    _drop_local_branch(repo_path, new_branch, base_ref)

    refs = ", ".join(
        pr_ref_label(pr.repo_slug, pr.number, origin_slug)
        for pr in unit.prs if pr.url in set(already_in_target_urls)
    ) or "(none)"
    reason = f"already in target — empty cherry-pick ({refs})"

    console.print(
        f"    [green]✓[/green] Skipped [cyan]{unit.feature_id}[/cyan]: "
        f"{reason}"
    )

    state.features[unit.feature_id] = FeatureState(
        status="skipped",
        branch_name=None,
        base_commit=onto,
        ai_cost_usd=unit.ai_cost_usd_total,
        skip_reason=reason,
        **pr_meta,
    )
    _persist_state(config, state)


def _handle_unresolved_conflict(
    config: Config,
    repo_path: Path,
    state: PipelineState,
    unit: FeatureUnit,
    new_branch: str,
    base_branch: str,
    base_ref: str,
    onto: str,
    idx: int,
    failed_pr: PRInfo,
    conflict_files: list[str],
    pr_meta: dict,
    *,
    ai_attempted: bool,
    api_aborted: bool = False,
    dynamic_prereq_urls: list[str] | None = None,
    prereq_trail: list[dict] | None = None,
    prereq_discovery_depth: int = 0,
) -> None:
    """Record an unresolved conflict as ``conflict`` state.

    At ``idx == 0`` the local branch is dropped; for a partial group the
    applied picks are pushed as a draft PR labelled needs-attention.
    """
    origin_slug = get_origin_repo_slug(config)
    ref = pr_ref_label(failed_pr.repo_slug, failed_pr.number, origin_slug)

    _abort_git_op(repo_path)

    why = (
        "AI resolver gave up" if ai_attempted
        else "AI resolver disabled"
    )
    prior_state = state.features.get(unit.feature_id)
    if not ai_attempted:
        stall = make_stall(
            "resolver_unavailable", detail="AI resolution disabled",
            prior=prior_state,
        )
    elif api_aborted:
        stall = make_stall(
            "resolver_unavailable",
            detail="backend failed before judging the conflict",
            prior=prior_state,
        )
    else:
        n_files = len(conflict_files)
        stall = make_stall(
            "unresolvable",
            detail=f"{ref}, {n_files} conflicted file(s)",
            prior=prior_state,
        )

    if idx == 0:
        _drop_local_branch(repo_path, new_branch, base_ref)
        console.print(
            f"    [yellow]Dropped local branch[/yellow] [cyan]{new_branch}[/cyan] "
            f"({why}; nothing to keep)."
        )
        if unit.is_group:
            remaining = max(0, len(unit.prs) - 1)
            console.print(
                f"    [dim]Group {unit.group_id!r}: first PR {ref} could "
                f"not be resolved; {remaining} later PR(s) abandoned.[/dim]"
            )
        state.features[unit.feature_id] = FeatureState(
            status="conflict",
            branch_name=None,
            base_commit=onto,
            conflict_files=conflict_files,
            failed_step_index=idx,
            partial_pr_count=0,
            ai_cost_usd=unit.ai_cost_usd_total,
            dynamic_prereq_urls=list(dynamic_prereq_urls or []),
            prereq_trail=list(prereq_trail or []),
            prereq_discovery_depth=prereq_discovery_depth,
            stall=stall,
            **pr_meta,
        )
        _persist_state(config, state)
        return

    applied = idx
    remaining = max(0, len(unit.prs) - applied - 1)
    console.print(
        f"    [yellow]Partial group:[/yellow] {applied} PR(s) applied, "
        f"{ref} unresolved ({why}); {remaining} later PR(s) abandoned."
    )

    rebase_pr_url: str | None = None
    pushed = False
    if config.push:
        _push(config, repo_path, new_branch)
        console.print(f"    [green]✓[/green] Pushed [cyan]{new_branch}[/cyan]")
        pushed = True

    if pushed and config.pr_policy.auto_pr:
        title = _unit_title(unit, config.project, base_branch)
        dropped_items = _collect_dropped_trailers(
            repo_path, base_ref, new_branch,
        )
        body = _unit_body(
            unit,
            origin_slug,
            needs_intervention=True,
            failed_index=applied,
            failed_pr=failed_pr,
            conflict_files=conflict_files,
            dropped_items=dropped_items,
            stall=stall,
        )
        # A retry may already have a draft PR open; creating another would 422.
        existing = find_pr_for_branch(config, new_branch, base_branch)
        if existing is not None:
            rebase_pr_url = existing.url
            updated = update_pull_request(
                config, existing.number, title=title, body=body,
            )
            if updated:
                console.print(
                    f"    [green]✓[/green] Refreshed banner on existing "
                    f"draft PR: [link={rebase_pr_url}]{rebase_pr_url}[/link]"
                )
            else:
                console.print(
                    f"    [yellow]![/yellow] Could not refresh existing "
                    f"PR {rebase_pr_url} (see warnings above)"
                )
            add_label_to_pr(
                config, existing.number,
                config.ai_resolve.needs_attention_label,
            )
            _apply_releasy_label_to_pr(
                config, rebase_pr_url, pr_number=existing.number,
            )
            _apply_session_labels_to_pr(
                config, rebase_pr_url, pr_number=existing.number,
                mode=unit.mode,
            )
            if unit.verify_needs_attention:
                _apply_verify_label_to_pr(
                    config, rebase_pr_url, pr_number=existing.number,
                )
        else:
            rebase_pr_url = create_pull_request(
                config, new_branch, base_branch, title, body,
                draft=True,
                labels=[config.ai_resolve.needs_attention_label],
            )
            if rebase_pr_url:
                console.print(
                    f"    [green]✓[/green] Draft PR opened: "
                    f"[link={rebase_pr_url}]{rebase_pr_url}[/link] "
                    f"[dim](label: {config.ai_resolve.needs_attention_label})[/dim]"
                )
                _apply_releasy_label_to_pr(config, rebase_pr_url)
                _apply_session_labels_to_pr(
                    config, rebase_pr_url, mode=unit.mode,
                )
                if unit.verify_needs_attention:
                    _apply_verify_label_to_pr(config, rebase_pr_url)
            else:
                console.print(
                    "    [yellow]![/yellow] Could not open draft PR for "
                    f"[cyan]{new_branch}[/cyan] (see warnings above)"
                )

    verify_comment_posted = bool(
        prior_state and prior_state.verify_comment_posted
    )
    if (
        unit.verify_needs_attention
        and rebase_pr_url
        and not verify_comment_posted
    ):
        if _post_verify_findings_comment(
            config, rebase_pr_url, list(unit.verify_findings),
        ):
            verify_comment_posted = True

    fs = FeatureState(
        status="conflict",
        branch_name=new_branch,
        base_commit=onto,
        conflict_files=conflict_files,
        failed_step_index=applied,
        partial_pr_count=applied,
        partial_continue_attempts=unit.partial_continue_attempts,
        ai_cost_usd=unit.ai_cost_usd_total,
        verify_needs_attention=unit.verify_needs_attention,
        verify_comment_posted=verify_comment_posted,
        mode=unit.mode,
        dynamic_prereq_urls=list(dynamic_prereq_urls or []),
        prereq_trail=list(prereq_trail or []),
        prereq_discovery_depth=prereq_discovery_depth,
        stall=stall,
        **pr_meta,
    )
    if rebase_pr_url:
        fs.rebase_pr_url = rebase_pr_url
    state.features[unit.feature_id] = fs
    _persist_state(config, state)


def _combine_user_context(unit: FeatureUnit, pr: PRInfo) -> str:
    """Unit-level ``ai_context`` plus the per-PR one for ``pr``."""
    parts: list[str] = []
    if unit.ai_context:
        parts.append(unit.ai_context)
    per_pr = unit.per_pr_ai_context.get(pr.url, "")
    if per_pr and per_pr != unit.ai_context:
        parts.append(per_pr)
    return "\n\n".join(parts)


def _try_ai_resolve_step(
    config: Config,
    repo_path: Path,
    unit: FeatureUnit,
    new_branch: str,
    base_branch: str,
    pr: PRInfo,
    conflict_files: list[str],
    *,
    start_sha: str | None = None,
    pre_resolve_sha: str | None = None,
) -> "_AIStepOutcome":
    """Have Claude resolve and commit one conflicted cherry-pick step in place."""
    from releasy.ai_resolve import AIResolveContext, attempt_ai_resolve

    _ensure_upstream_remote(config, repo_path)

    ctx = AIResolveContext(
        port_branch=new_branch,
        base_branch=base_branch,
        source_pr=pr,
        conflict_files=conflict_files,
        operation="cherry-pick",
        user_context=_combine_user_context(unit, pr),
        split_mode=pre_resolve_sha is not None,
        pre_resolve_sha=pre_resolve_sha,
        start_sha=start_sha,
        mode=unit.mode,
        skip_build=config.ai_resolve.deterministic_build,
    )

    result = attempt_ai_resolve(config, repo_path, ctx)

    if result.cost_usd is not None:
        unit.ai_cost_usd_total = (
            (unit.ai_cost_usd_total or 0.0) + result.cost_usd
        )

    if not result.success:
        reason = result.error or (
            "timed out" if result.timed_out else "unknown failure"
        )
        cost_note = (
            f" [dim](cost: ${result.cost_usd:.4f})[/dim]"
            if result.cost_usd is not None else ""
        )
        console.print(
            f"    [yellow]AI resolve failed:[/yellow] {reason}{cost_note}"
        )
        return _AIStepOutcome(
            handled=False,
            missing_prereq_prs=list(result.missing_prereq_prs or []),
            missing_prereq_note=result.missing_prereq_note,
            api_aborted=result.api_aborted,
        )

    unit.ai_resolved_count += 1
    if result.iterations:
        unit.ai_iterations_total += result.iterations
    iters = (
        f" (iterations: {result.iterations})" if result.iterations else ""
    )
    cost = (
        f" [dim](cost: ${result.cost_usd:.4f})[/dim]"
        if result.cost_usd is not None else ""
    )
    console.print(
        f"    [green]✓[/green] AI resolved #{pr.number}{iters}{cost}"
    )

    # A resolution kept despite a failing postcondition is reported via the verifier findings.
    if result.warnings:
        from releasy.ai_resolve import flatten_resolve_warnings

        unit.verify_needs_attention = True
        unit.verify_findings.append(
            f"**#{pr.number} — resolution kept with a failing "
            f"post-resolution check**"
        )
        for line in flatten_resolve_warnings(result.warnings):
            unit.verify_findings.append(f"- {line}")
        unit.verify_findings.append("")

    if config.ai_resolve.verify_resolution and result.new_head and start_sha:
        _run_verify_pass(
            config, repo_path, unit, new_branch, base_branch, pr,
            conflict_files=conflict_files,
            start_sha=start_sha,
            new_head=result.new_head,
        )

    return _AIStepOutcome(handled=True)


def _run_verify_pass(
    config: Config,
    repo_path: Path,
    unit: FeatureUnit,
    new_branch: str,
    base_branch: str,
    pr: PRInfo,
    *,
    conflict_files: list[str],
    start_sha: str,
    new_head: str,
) -> None:
    """Run the advisory verifier; mutate ``unit`` in place, never raise."""
    from releasy.ai_resolve import VerifyContext, verify_ai_resolution

    ctx = VerifyContext(
        port_branch=new_branch,
        base_branch=base_branch,
        source_pr=pr,
        start_sha=start_sha,
        new_head=new_head,
        conflict_files=conflict_files,
        user_context=_combine_user_context(unit, pr),
        mode=unit.mode,
    )

    vr = verify_ai_resolution(config, repo_path, ctx)

    if vr.cost_usd is not None:
        unit.ai_cost_usd_total = (
            (unit.ai_cost_usd_total or 0.0) + vr.cost_usd
        )

    cost_note = (
        f" [dim](cost: ${vr.cost_usd:.4f})[/dim]"
        if vr.cost_usd is not None else ""
    )

    if not vr.success:
        console.print(
            f"    [yellow]⚠[/yellow] Verifier did not produce a verdict "
            f"for #{pr.number}: {vr.error}{cost_note} "
            "[dim](treating as advisory — port proceeds)[/dim]"
        )
        return

    if vr.verdict == "ok":
        console.print(
            f"    [green]✓[/green] Verifier: resolution of #{pr.number} "
            f"looks in scope{cost_note}"
        )
        if vr.summary:
            console.print(f"      [dim]{vr.summary}[/dim]")
        return

    unit.verify_needs_attention = True
    header = f"#{pr.number}"
    if vr.summary:
        unit.verify_findings.append(f"**{header} — {vr.summary}**")
    else:
        unit.verify_findings.append(f"**{header}**")
    for finding in vr.findings:
        unit.verify_findings.append(f"- {finding}")
    unit.verify_findings.append("")

    console.print(
        f"    [yellow]⚠[/yellow] Verifier flagged #{pr.number}: "
        f"{vr.summary or 'see findings'}{cost_note}"
    )
    for finding in vr.findings:
        console.print(f"      [yellow]•[/yellow] {finding}")


@dataclass
class _AIStepOutcome:
    # True iff the AI committed the cherry-pick locally.
    handled: bool
    missing_prereq_prs: list[str] = field(default_factory=list)
    missing_prereq_note: str | None = None
    # The resolver never reached a verdict.
    api_aborted: bool = False


def _resolve_branch_target(
    config: Config, state: PipelineState, branch_name: str,
) -> FeatureConfig | None:
    """Resolve a user-supplied branch name or feature ID."""
    feat = config.get_feature(branch_name) or config.get_feature_by_branch(
        branch_name, state.onto or "",
    )
    if feat is None:
        for fid, fs in state.features.items():
            if fs.branch_name == branch_name or fid == branch_name:
                feat = config.get_feature(fid)
                if feat is None:
                    feat = FeatureConfig(id=fid, description=fid, source_branch="")
                break
    return feat


def continue_branch(config: Config, branch_name: str) -> bool:
    """Mark a previously-conflicted port as resolved."""
    if config.dry_run:
        console.print(
            "[bold magenta]DRY RUN[/bold magenta]: no state, repo, or "
            "GitHub writes will happen."
        )
    state = load_state(config)
    feat = _resolve_branch_target(config, state, branch_name)

    if feat is None:
        console.print(f"[red]Unknown branch or feature: {branch_name}[/red]")
        return False

    work_dir = config.resolve_work_dir()
    repo_path = work_dir if (work_dir / ".git").exists() else work_dir / "repo"
    if (repo_path / ".git").exists() and is_operation_in_progress(repo_path):
        console.print(
            "[red]A git operation is still in progress.[/red]\n"
            f"  cd {repo_path}\n"
            "  git add <resolved files>\n"
            "  git cherry-pick --continue  (or git commit)\n"
            "  Then re-run this command."
        )
        return False

    fs = state.features.get(feat.id)
    if fs is None or fs.status != "conflict":
        current = fs.status if fs else "unknown"
        console.print(
            f"[yellow]Feature {feat.id} is not in conflict "
            f"(status: {current})[/yellow]"
        )
        return False

    state.features[feat.id].status = _success_status(
        state.features[feat.id].rebase_pr_url
    )
    clear_conflict_markers(state.features[feat.id])
    _persist_state(config, state)
    console.print(
        f"[green]✓[/green] Feature [cyan]{feat.id}[/cyan] "
        f"({fs.branch_name}) → {state.features[feat.id].status}"
    )
    _reconcile_project_board(config, state)
    return True


def _branch_resolution_state(
    repo_path: Path, branch: str, base_ref: str,
) -> tuple[bool, str | None]:
    """Check out ``branch``; resolved = clean tree, no op in progress, commits beyond ``base_ref``.

    Returns ``(resolved, reason_if_not)``.
    """
    co = run_git(["checkout", branch], repo_path, check=False)
    if co.returncode != 0:
        return False, "could not checkout branch (uncommitted changes elsewhere?)"

    if is_operation_in_progress(repo_path):
        return False, "cherry-pick/merge/rebase still in progress"

    unmerged = run_git(["ls-files", "--unmerged"], repo_path, check=False)
    if unmerged.stdout.strip():
        files = sorted({line.split("\t", 1)[1] for line in unmerged.stdout.splitlines()})
        return False, "unmerged files: " + ", ".join(files)

    porc = run_git(
        ["status", "--porcelain", "--untracked-files=no"],
        repo_path, check=False,
    )
    if porc.stdout.strip():
        return False, "working tree has uncommitted changes"

    cnt = run_git(
        ["rev-list", "--count", f"{base_ref}..{branch}"], repo_path, check=False,
    )
    if cnt.returncode != 0 or cnt.stdout.strip() == "0":
        return False, f"branch has no commits beyond {base_ref}"

    return True, None


def _open_pr_for_resolved(
    config: Config, repo_path: Path, state: PipelineState, fs: FeatureState,
    base_branch: str,
) -> None:
    """Push and open a PR for an already-resolved port branch."""
    branch = fs.branch_name
    assert branch is not None

    if not config.push:
        console.print("    [dim]push disabled — branch left local[/dim]")
        return

    if remote_branch_exists(repo_path, branch, config.origin.remote_name):
        console.print(
            "    [dim]already on origin, not force-pushing[/dim]"
        )
    else:
        _push(config, repo_path, branch)
        console.print(f"    [green]✓[/green] Pushed [cyan]{branch}[/cyan]")

    subject = (
        _strip_misleading_title_prefix(fs.pr_title, config.project)
        if fs.pr_title else branch
    )
    prefix = _subject_prefix(config.project, base_branch)
    title = f"{prefix}: {subject}" if prefix else subject

    body_parts: list[str] = []
    origin_slug = get_origin_repo_slug(config)
    pr_urls = _source_pr_urls(fs)
    refs: list[str] = []
    for url in pr_urls:
        parsed = parse_pr_url(url) if url else None
        if parsed:
            owner, repo, n = parsed
            refs.append(pr_ref_label(f"{owner}/{repo}", n, origin_slug))
    if refs:
        body_parts.append(f"Cherry-picked from {', '.join(refs)}.")
    if fs.pr_body:
        body_parts.append(f"\n---\n\n{fs.pr_body}")
    body = "\n".join(body_parts) or branch

    if fs.rebase_pr_url:
        pr_num = _pr_number_from_url(fs.rebase_pr_url)
        if config.update_existing_prs:
            if pr_num is not None and update_pull_request(
                config, pr_num, title=title, body=body,
            ):
                console.print(
                    f"    [green]✓[/green] PR updated: "
                    f"[link={fs.rebase_pr_url}]{fs.rebase_pr_url}[/link]"
                )
            else:
                console.print(
                    f"    [yellow]![/yellow] Could not update PR "
                    f"{fs.rebase_pr_url}"
                )
        else:
            console.print(
                f"    [dim]PR already opened — left as-is: "
                f"[link={fs.rebase_pr_url}]{fs.rebase_pr_url}[/link] "
                f"(set [cyan]update_existing_prs: true[/cyan] to overwrite "
                f"title/body)[/dim]"
            )
        _apply_releasy_label_to_pr(config, fs.rebase_pr_url, pr_number=pr_num)
        _apply_session_labels_to_pr(
            config, fs.rebase_pr_url, pr_number=pr_num, mode=fs.mode,
        )
        if fs.ai_resolved:
            _apply_ai_label_to_pr(
                config, fs.rebase_pr_url, pr_number=pr_num,
            )
        return

    remote_base = f"{config.origin.remote_name}/{base_branch}"
    remote_head = f"{config.origin.remote_name}/{branch}"
    ahead = run_git(
        ["rev-list", "--count", f"{remote_base}..{remote_head}"],
        repo_path, check=False,
    )
    if ahead.returncode == 0:
        try:
            ahead_n = int(ahead.stdout.strip())
        except ValueError:
            ahead_n = -1
        if ahead_n == 0:
            console.print(
                f"    [yellow]![/yellow] Branch has no commits ahead of "
                f"[cyan]{base_branch}[/cyan] — skipping PR creation "
                f"(stale branch from an earlier run? delete it with "
                f"[cyan]git push {config.origin.remote_name} "
                f":{branch}[/cyan])"
            )
            return

    pr_url, outcome = _ensure_pr_for_branch(
        config, branch, base_branch, title, body,
    )
    _log_pr_action(outcome, pr_url)
    if pr_url:
        fs.rebase_pr_url = pr_url
        fs.status = "needs_review"
        state.features[_feature_id_from_branch(state, branch)] = fs
        _apply_releasy_label_to_pr(config, pr_url)
        _apply_session_labels_to_pr(config, pr_url, mode=fs.mode)
        if fs.ai_resolved:
            _apply_ai_label_to_pr(config, pr_url)


def _pr_number_from_url(url: str) -> int | None:
    """Parse the trailing ``/pull/<N>`` segment of a PR URL."""
    try:
        return int(url.rstrip("/").rsplit("/", 1)[-1])
    except (ValueError, IndexError):
        return None


def _feature_id_from_branch(state: PipelineState, branch: str) -> str:
    for fid, fs in state.features.items():
        if fs.branch_name == branch:
            return fid
    return branch


def continue_all(config: Config, work_dir: Path | None = None) -> bool:
    """Re-check every feature in state: open PRs for resolved / ``branch_created`` ports, report the rest.

    Ends with a project-board reconciliation pass.
    """
    state = load_state(config)
    if not state.features:
        console.print(
            "[yellow]No features in state. Run 'releasy run' first.[/yellow]"
        )
        return False

    if _prune_superseded_singletons(config, state):
        _persist_state(config, state)

    repo_path = _setup_repo(config, work_dir, state.base_branch)

    if is_operation_in_progress(repo_path):
        console.print(
            f"\n[red]✗[/red] A git operation is still in progress in "
            f"[cyan]{repo_path}[/cyan]."
        )
        console.print(
            "  Finish (`git cherry-pick --continue`) or abort it first, "
            "then re-run."
        )
        return False

    base_branch = state.base_branch or (
        config.base_branch_name(state.onto or "") if state.onto else None
    )
    if not base_branch:
        console.print(
            "[red]Cannot determine base branch from state.[/red] Run "
            "'releasy run' first."
        )
        return False
    base_ref = f"{config.origin.remote_name}/{base_branch}"
    remote_name = config.origin.remote_name

    console.print(
        f"\n[bold]Continuing[/bold] — base [cyan]{base_branch}[/cyan]"
    )
    if config.dry_run:
        console.print(
            "\n[bold magenta]DRY RUN[/bold magenta]: no state, repo, or "
            "GitHub writes will happen."
        )

    _refresh_all_merge_status_from_github(config, state)
    _refresh_all_superseded_status_from_github(
        config, state, repo_path, base_branch,
    )
    _apply_merged_labels(config, state)
    _persist_state(config, state)

    any_unresolved = False
    for feat_id, fs in state.features.items():
        branch = fs.branch_name or feat_id
        header = f"\n  [cyan]{branch}[/cyan]"

        if fs.status == "skipped":
            console.print(f"{header} — [dim]skipped[/dim]")
            continue

        if fs.status == "closed":
            reason = fs.skip_reason or "rebase PR closed without merging"
            console.print(f"{header} — [dim]closed: {reason}[/dim]")
            continue

        if fs.status == "superseded":
            reason = fs.skip_reason or "another PR cherry-picks the source"
            console.print(f"{header} — [dim]superseded: {reason}[/dim]")
            continue

        if fs.status == "reverted":
            reason = fs.skip_reason or "port reverted on target"
            console.print(f"{header} — [dim]reverted: {reason}[/dim]")
            continue

        if fs.status == "conflict" and (
            fs.failed_step_index is not None
            or fs.partial_pr_count is not None
            or fs.rebase_pr_url
        ):
            console.print(
                f"{header} — [dim]conflict (AI gave up)[/dim] "
                "— fix locally / on the draft PR, then re-run"
            )
            continue

        if fs.status == "build_failed":
            err = fs.last_verify_error or "build/tests not green"
            console.print(
                f"{header} — [dim]build-failed: {err}[/dim] "
                "— re-run [cyan]releasy run[/cyan] to retry the build/tests"
            )
            continue

        if fs.status == "needs_review":
            console.print(
                f"{header} — [dim]needs-review, PR open[/dim]"
            )
            continue
        if fs.status == "branch_created":
            if not (config.push and config.pr_policy.auto_pr):
                console.print(
                    f"{header} — [dim]branch-created (auto_pr off, "
                    "open PR manually)[/dim]"
                )
                continue
            if not fs.branch_name or not (
                local_branch_exists(repo_path, fs.branch_name)
                or remote_branch_exists(repo_path, fs.branch_name, remote_name)
            ):
                console.print(
                    f"{header} [yellow]branch missing (local & remote), "
                    "skipping[/yellow]"
                )
                continue
            console.print(
                f"{header} — [green]branch-created[/green], opening PR"
            )
            _open_pr_for_resolved(config, repo_path, state, fs, base_branch)
            _persist_state(config, state)
            continue

        if not fs.branch_name or not local_branch_exists(repo_path, fs.branch_name):
            console.print(
                f"{header} [yellow]branch missing locally, skipping[/yellow]"
            )
            continue

        if fs.status != "conflict":
            console.print(f"{header} — [dim]status {fs.status}, skipping[/dim]")
            continue

        resolved, reason = _branch_resolution_state(
            repo_path, fs.branch_name, base_ref,
        )
        if not resolved:
            any_unresolved = True
            console.print(f"{header} [red]✗ still unresolved[/red] — {reason}")
            if fs.conflict_files:
                for cf in fs.conflict_files:
                    console.print(f"      [red]•[/red] {cf}")
            console.print(
                f"      [dim]cd {repo_path} && git status     # then resolve, "
                "git add -A && git cherry-pick --continue[/dim]"
            )
            continue

        console.print(f"{header} [green]✓ resolved[/green]")
        fs.conflict_files = []
        fs.status = _success_status(fs.rebase_pr_url)
        state.features[feat_id] = fs
        _open_pr_for_resolved(config, repo_path, state, fs, base_branch)
        _persist_state(config, state)

    _reconcile_project_board(config, state)

    if any_unresolved:
        console.print(
            "\n[yellow]Some ports still have unresolved conflicts (see above). "
            "Fix them and re-run [bold]releasy continue[/bold].[/yellow]"
        )
        return False

    console.print("\n[green]All ports processed.[/green]")
    return True


def sync_to_project(config: Config) -> bool:
    """Push local state to the project board (pruning orphans); False when the sync did not happen or errored."""
    if not config.notifications.github_project:
        console.print(
            "[yellow]No GitHub Project configured.[/yellow] Set "
            "[cyan]notifications.github_project[/cyan] in config.yaml or "
            "run [cyan]releasy setup-project[/cyan] first."
        )
        return False

    state = load_state(config)
    if not state.features and not config.features:
        console.print(
            "[yellow]Nothing to sync.[/yellow] No features in state and "
            "no static features in config — run [cyan]releasy run[/cyan] "
            "first."
        )
        return False

    console.print(
        f"\n[bold]Syncing local state[/bold] → "
        f"[cyan]{config.notifications.github_project}[/cyan]"
    )
    summary = sync_project(config, state, prune_orphans=True)

    if summary.skipped:
        console.print(
            f"  [yellow]project sync skipped:[/yellow] {summary.skipped_reason}"
        )
        return False
    if summary.added:
        console.print(
            f"  [green]✓[/green] added {summary.added} missing item(s) "
            "to the project board"
        )
    if summary.updated:
        console.print(
            f"  [dim]refreshed {summary.updated} existing card(s)[/dim]"
        )
    if summary.removed:
        console.print(
            f"  [yellow]✓[/yellow] removed {summary.removed} orphan card(s) "
            "(not in local state)"
        )
    if not summary.changed and not summary.errors:
        console.print("  [dim]project board already up to date[/dim]")
    if summary.errors:
        console.print(
            f"  [yellow]![/yellow] {summary.errors} item(s) could not be "
            "synced — see warnings above"
        )
        return False
    return True


def _reconcile_project_board(config: Config, state: PipelineState) -> None:
    """Sync the project board with local state (also when ``push`` is off)."""
    if not config.notifications.github_project:
        return
    console.print("\n[dim]Reconciling GitHub Project board...[/dim]")
    summary = sync_project(config, state)
    if summary.skipped:
        console.print(
            f"  [yellow]project sync skipped:[/yellow] {summary.skipped_reason}"
        )
        return
    if summary.added and summary.errors == 0:
        console.print(
            f"  [green]✓[/green] added {summary.added} missing item(s) to "
            "the project board"
        )
    if summary.updated:
        console.print(
            f"  [dim]refreshed {summary.updated} existing card(s)[/dim]"
        )
    if not summary.added and not summary.updated and not summary.errors:
        console.print("  [dim]project board already up to date[/dim]")
    if summary.errors:
        console.print(
            f"  [yellow]![/yellow] {summary.errors} item(s) could not be "
            "synced — see warnings above"
        )


def skip_branch(config: Config, branch_name: str) -> bool:
    """Mark a port branch as skipped."""
    state = load_state(config)
    feat = _resolve_branch_target(config, state, branch_name)

    if feat is None:
        console.print(f"[red]Unknown branch or feature: {branch_name}[/red]")
        return False

    fs = state.features.get(feat.id)
    if fs is None:
        console.print(f"[red]No state found for feature {feat.id}[/red]")
        return False

    state.features[feat.id].status = "skipped"
    clear_conflict_markers(state.features[feat.id])
    _persist_state(config, state)
    console.print(f"[yellow]⏭[/yellow] Feature [cyan]{feat.id}[/cyan] skipped")
    return True


def mark_reverted(
    config: Config, branch_name: str, reason: str | None = None,
) -> bool:
    """Mark a port ``reverted`` (state only; terminal, never re-ported)."""
    state = load_state(config)
    feat = _resolve_branch_target(config, state, branch_name)

    if feat is None:
        console.print(f"[red]Unknown branch or feature: {branch_name}[/red]")
        return False

    fs = state.features.get(feat.id)
    if fs is None:
        console.print(f"[red]No state found for feature {feat.id}[/red]")
        return False

    if fs.status == "reverted":
        console.print(
            f"[dim]Feature [cyan]{feat.id}[/cyan] is already reverted: "
            f"{fs.skip_reason or 'no reason recorded'}[/dim]"
        )
        return True

    was = fs.status
    fs.status = "reverted"
    fs.skip_reason = reason or "port reverted on target — do not re-port"
    clear_conflict_markers(fs)
    _persist_state(config, state)
    console.print(
        f"[red]↩[/red] Feature [cyan]{feat.id}[/cyan] marked reverted "
        f"(was {was}) — releasy will not port it again"
    )
    console.print(f"  [dim]{fs.skip_reason}[/dim]")
    console.print(
        "  [dim]Run `releasy graph sync` to state it on the graph issue.[/dim]"
    )
    return True


def abort_run(config: Config) -> None:
    """Abort the current run, leaving all branches as-is."""
    state = load_state(config)
    console.print("[yellow]Aborting current run. All branches left as-is.[/yellow]")
    _persist_state(config, state)


# Local-only damage that ``clear`` without an identifier cleans up.
_CLEARABLE_STATUSES: tuple[str, ...] = ("conflict", "branch_created")


def _resolve_clear_target(
    config: Config, state: PipelineState, ident: str,
) -> tuple[str, FeatureState] | None:
    """Find a state entry by feature ID, branch name, source-PR number or URL."""
    feat = _resolve_branch_target(config, state, ident)
    if feat is not None and feat.id in state.features:
        return feat.id, state.features[feat.id]

    if ident.isdigit():
        n = int(ident)
        for fid, fs in state.features.items():
            if fs.pr_number == n or n in fs.pr_numbers:
                return fid, fs

    if parse_pr_url(ident) is not None:
        for fid, fs in state.features.items():
            if any(same_pr_url(u, ident) for u in (fs.pr_url, *fs.pr_urls)):
                return fid, fs

    return None


def _clear_one_feature(
    config: Config,
    state: PipelineState,
    feat_id: str,
    fs: FeatureState,
    work_dir: Path | None,
    dry_run: bool,
) -> bool:
    """Delete one feature's local branch and drop its state entry (caller persists)."""
    branch = fs.branch_name
    work = config.resolve_work_dir(work_dir)
    repo_path = work if (work / ".git").exists() else work / "repo"
    repo_ok = (repo_path / ".git").exists()

    plan: list[str] = []
    op_in_progress = repo_ok and is_operation_in_progress(repo_path)
    if op_in_progress:
        plan.append("abort in-progress git operation")
    if branch and repo_ok and local_branch_exists(repo_path, branch):
        plan.append(f"delete local branch {branch}")
    plan.append(f"drop state entry for {feat_id}")

    label = f"[cyan]{feat_id}[/cyan]" + (
        f" (branch [cyan]{branch}[/cyan])" if branch else ""
    )
    prefix = "[yellow]Would clear[/yellow]" if dry_run else "[yellow]Clearing[/yellow]"
    console.print(f"{prefix} {label}")
    for step in plan:
        console.print(f"  • {step}")

    if dry_run:
        return True

    if repo_ok:
        if op_in_progress:
            kind = abort_in_progress_op(repo_path)
            if kind:
                console.print(f"  [green]✓[/green] aborted {kind}")

        if branch and local_branch_exists(repo_path, branch):
            head = run_git(
                ["rev-parse", "--abbrev-ref", "HEAD"], repo_path, check=False,
            )
            on_branch = head.returncode == 0 and head.stdout.strip() == branch
            if on_branch:
                base = state.base_branch or config.target_branch or "HEAD"
                run_git(["checkout", "--detach", base], repo_path, check=False)

            del_res = run_git(["branch", "-D", branch], repo_path, check=False)
            if del_res.returncode == 0:
                console.print(
                    f"  [green]✓[/green] deleted local branch [cyan]{branch}[/cyan]"
                )
            else:
                console.print(
                    f"  [red]✗[/red] could not delete branch {branch}: "
                    f"{del_res.stderr.strip() or 'unknown error'}"
                )
                return False

    state.features.pop(feat_id, None)
    console.print("  [green]✓[/green] state entry removed")
    return True


def clear_branch(
    config: Config,
    identifier: str,
    work_dir: Path | None = None,
    dry_run: bool = False,
) -> bool:
    """Clean up local artifacts for one feature that has no port PR."""
    state = load_state(config)
    resolved = _resolve_clear_target(config, state, identifier)
    if resolved is None:
        console.print(
            f"[red]Unknown feature / branch / PR: {identifier}[/red]\n"
            f"  Run `releasy status` to see tracked feature IDs."
        )
        return False
    feat_id, fs = resolved

    if fs.rebase_pr_url:
        console.print(
            f"[yellow]Feature [cyan]{feat_id}[/cyan] has an open rebase PR — "
            f"refusing to clear.[/yellow]\n"
            f"  PR: {fs.rebase_pr_url}\n"
            f"  `clear` only removes never-merged local artifacts. "
            f"Close the PR on GitHub first if you really want it gone."
        )
        return False

    ok = _clear_one_feature(config, state, feat_id, fs, work_dir, dry_run)
    if not dry_run and ok:
        _persist_state(config, state)
    return ok


def clear_all_dirty(
    config: Config,
    work_dir: Path | None = None,
    dry_run: bool = False,
    assume_yes: bool = False,
) -> bool:
    """Clear every PR-less feature in a :data:`_CLEARABLE_STATUSES` status (confirms unless ``assume_yes``)."""
    import click

    state = load_state(config)
    targets: list[tuple[str, FeatureState]] = [
        (fid, fs)
        for fid, fs in state.features.items()
        if not fs.rebase_pr_url and fs.status in _CLEARABLE_STATUSES
    ]

    if not targets:
        console.print(
            "[dim]Nothing to clear — no local-only damaged features in state.[/dim]"
        )
        return True

    console.print(
        f"[yellow]Found {len(targets)} local-only damaged feature(s):[/yellow]"
    )
    for fid, fs in targets:
        branch = fs.branch_name or "(no branch recorded)"
        console.print(
            f"  [cyan]{fid}[/cyan]  status=[red]{fs.status}[/red]  branch={branch}"
        )

    if dry_run:
        console.print()
        for fid, fs in targets:
            _clear_one_feature(config, state, fid, fs, work_dir, dry_run=True)
        console.print("[dim]--dry-run: nothing changed.[/dim]")
        return True

    if not assume_yes and not click.confirm(
        "Proceed with clearing all of these?", default=False,
    ):
        console.print("[dim]Aborted, nothing changed.[/dim]")
        return False

    all_ok = True
    for fid, fs in targets:
        if not _clear_one_feature(config, state, fid, fs, work_dir, dry_run=False):
            all_ok = False
    _persist_state(config, state)
    return all_ok


def print_status(config: Config) -> None:
    """Print the current pipeline state, one table per status."""
    from rich.markup import escape
    from rich.table import Table
    from releasy.state import STATUS_DISPLAY_ORDER
    from releasy.status import STATUS_HEADINGS, STATUS_ICONS

    state = load_state(config)

    console.print()
    console.print(
        f"Last run: {state.started_at or 'N/A'}  ·  "
        f"Onto: {state.onto or 'N/A'}  ·  "
        f"Phase: {state.phase}"
    )
    if state.base_branch:
        console.print(f"Base branch: [cyan]{state.base_branch}[/cyan]")

    section_styles = {
        "needs_review": "blue", "branch_created": "yellow",
        "conflict": "red", "skipped": "yellow",
        "blocked": "yellow", "closed": "bright_black",
        "superseded": "bright_black", "reverted": "red",
    }

    origin_slug = get_origin_repo_slug(config)

    def _ai_cell(fs: FeatureState) -> str:
        if not fs.ai_resolved:
            return ""
        iters = f" ({fs.ai_iterations}×)" if fs.ai_iterations else ""
        return f"[magenta]ai-resolved[/magenta]{iters}"

    def _pr_cell(fs: FeatureState) -> str:
        if not fs.rebase_pr_url:
            return ""
        label = "PR"
        if fs.pr_url:
            parsed = parse_pr_url(fs.pr_url)
            if parsed:
                owner, repo, n = parsed
                label = pr_ref_label(f"{owner}/{repo}", n, origin_slug)
        elif fs.pr_number:
            label = f"#{fs.pr_number}"
        return f"[link={fs.rebase_pr_url}]{label}[/link]"

    if not state.features:
        console.print("\n[dim]No ports tracked yet.[/dim]")
        return

    by_status: dict[str, list[tuple[str, FeatureState]]] = {}
    for fid, fs in state.features.items():
        by_status.setdefault(fs.status, []).append((fid, fs))

    ordered = [s for s in STATUS_DISPLAY_ORDER if s in by_status]
    ordered.extend(sorted(s for s in by_status if s not in STATUS_DISPLAY_ORDER))

    summary_parts = [
        f"{len(by_status[s])} {STATUS_ICONS.get(s, s)}"
        for s in ordered
    ]
    console.print(f"\n[bold]Summary:[/bold] {'  ·  '.join(summary_parts)}")

    for status in ordered:
        rows = by_status[status]
        style = section_styles.get(status, "white")
        heading = STATUS_HEADINGS.get(status, status)
        icon = STATUS_ICONS.get(status, status)
        table = Table(
            title=f"[{style}]{icon} — {heading} ({len(rows)})[/{style}]",
            title_justify="left",
            show_header=True,
        )
        table.add_column("Branch", style="cyan")
        table.add_column("AI", style="magenta")
        table.add_column("Based On")
        table.add_column("Source PR")
        table.add_column("Rebase PR")
        if status == "conflict":
            table.add_column("Conflict Files", style="red")
        if status == "blocked":
            table.add_column("Blocked By", style="yellow")
        if status == "skipped":
            table.add_column("Reason", style="yellow")
        show_why = any(fs.stall is not None for _, fs in rows)
        if show_why:
            table.add_column("Why", style="yellow")
        for fid, fs in rows:
            feat = next((f for f in config.features if f.id == fid), None)
            label = fs.branch_name or (feat.source_branch if feat else None) or fid
            source_pr = ""
            if fs.pr_url:
                parsed = parse_pr_url(fs.pr_url)
                if parsed:
                    owner, repo, n = parsed
                    source_pr = (
                        f"[link={fs.pr_url}]"
                        f"{pr_ref_label(f'{owner}/{repo}', n, origin_slug)}"
                        f"[/link]"
                    )
                elif fs.pr_number:
                    source_pr = f"[link={fs.pr_url}]#{fs.pr_number}[/link]"
            row = [
                label,
                _ai_cell(fs),
                (fs.base_commit or "")[:12],
                source_pr,
                _pr_cell(fs),
            ]
            if status == "conflict":
                row.append(", ".join(fs.conflict_files))
            if status == "blocked":
                blockers: list[str] = []
                for dep_id in fs.blocked_by:
                    dep_fs = state.features.get(dep_id)
                    dep_status = dep_fs.status if dep_fs else "unknown"
                    blockers.append(f"{dep_id} ({dep_status})")
                row.append(", ".join(blockers))
            if status == "skipped":
                row.append(fs.skip_reason or "")
            if show_why:
                why = escape(fs.stall.summary()) if fs.stall else ""
                if fs.stall and fs.stall.runs > 1:
                    why += f" [dim](×{fs.stall.runs} runs)[/dim]"
                row.append(why)
            table.add_row(*row)
        console.print()
        console.print(table)
