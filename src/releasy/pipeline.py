"""Port-cherry-pick pipeline.

Assumes the base branch (e.g. ``antalya-26.3``) already exists on origin.
For each source PR discovered via configured labels, the pipeline creates a
port branch off ``origin/<base_branch>``, cherry-picks the PR merge commit,
and on conflict either invokes the AI resolver or — if the resolver is
disabled or fails — drops the local branch (singletons / first-of-group)
or opens a draft PR labelled ``ai-needs-attention`` (partial groups), then
records the entry as ``Conflict`` in the GitHub Project.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from releasy.termlog import console

from releasy.config import (
    Config, FeatureConfig, PortMode, PRGroupConfig, PRSourceConfig,
)
from releasy.git_ops import (
    OperationResult,
    abort_in_progress_op,
    append_commit_trailer,
    branch_exists,
    ensure_remote,
    is_ancestor,
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
    ref_exists_locally,
    run_git,
    stash_and_clean,
    update_submodules,
)
from releasy.github_ops import (
    CHERRY_PICK_FROM_RE,
    PRInfo,
    add_label_to_pr,
    create_pull_request,
    ensure_label,
    fetch_open_prs_with_commits_to_base,
    fetch_pr_by_number,
    fetch_pr_by_url,
    find_pr_for_branch,
    get_origin_repo_slug,
    mark_pr_ready_for_review,
    parse_pr_url,
    pr_has_label,
    pr_ref_label,
    remove_label_from_pr,
    require_origin_repo_slug,
    search_prs_by_labels,
    slug_to_https_url,
    sync_project,
    update_pull_request,
)
from releasy.state import FeatureState, PipelineState, load_state, save_state


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _persist_state(config: Config, state: PipelineState) -> None:
    """Persist state to the per-project state file and (optionally) sync the project board."""
    if config.dry_run:
        return
    save_state(state, config)
    if config.push:
        sync_project(config, state)


def _dry_record(state: PipelineState, action: str) -> None:
    """Increment a dry-run action counter on ``state``.

    Counts are populated only when ``run_pipeline`` is invoked with
    ``config.dry_run`` (it seeds ``state._dry_run_actions``); otherwise
    this is a no-op. ``run_pipeline`` reads them back at the end to
    print a planned-actions summary.
    """
    counts = getattr(state, "_dry_run_actions", None)
    if counts is not None:
        counts[action] = counts.get(action, 0) + 1


def _setup_repo(
    config: Config, work_dir: Path | None, base_branch: str | None = None,
) -> Path:
    """Set up work repo and fetch origin. The ``onto`` argument is used
    only for branch naming.

    If the repo was just cloned and ``base_branch`` is provided, check it
    out from origin and initialise submodules — saves the user (and Claude)
    from doing it manually before the first build.
    """
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
    """Push a branch to the origin remote (the only push path we use)."""
    force_push(repo_path, branch, config)


# ---------------------------------------------------------------------------
# Feature units (singletons + sequential PR groups)
# ---------------------------------------------------------------------------


@dataclass
class FeatureUnit:
    """A logical unit of porting work — one PR or a sequential group.

    For singletons, ``prs`` contains exactly one ``PRInfo``. For groups,
    ``prs`` are listed in cherry-pick order.
    """
    feature_id: str
    prs: list[PRInfo]
    if_exists: str
    title_prefix: str = ""        # used for both single-PR and group titles
    is_group: bool = False
    group_id: str | None = None   # filled when is_group
    # Free-form note appended to the AI conflict-resolver prompt for
    # every cherry-pick step in this unit. Populated from the matching
    # ``pr_sources.by_labels[].ai_context`` (singletons) or
    # ``pr_sources.groups[].ai_context`` (groups). Empty string when the
    # user supplied none.
    ai_context: str = ""
    # Per-PR ai_context for entries inside ``include_prs`` /
    # ``pr_sources.groups[].prs`` that used the dict form. Keyed by PR
    # URL. Combined with ``ai_context`` above when a step's source PR
    # has a matching entry.
    per_pr_ai_context: dict[str, str] = field(default_factory=dict)
    # Mutable per-run bookkeeping (set by _process_feature_unit):
    ai_resolved_count: int = 0
    ai_iterations_total: int = 0
    # Sum of USD cost reported by Claude across every cherry-pick step
    # in this unit (groups accumulate; singletons hit at most one step).
    # Also includes the changelog-synthesis cost when ``ai_changelog``
    # is enabled. ``None`` until Claude reports a cost at least once —
    # keeps downstream code able to distinguish "AI ran but produced no
    # cost data" (None) from "AI ran and the bill was 0.0".
    ai_cost_usd_total: float | None = None
    # Post-resolve verifier outcome (only set when verify_resolution is on).
    verify_needs_attention: bool = False
    verify_findings: list[str] = field(default_factory=list)
    # Detected from the primary PR; groups are assumed homogeneous.
    mode: PortMode = "forward_port"
    # Cached AI-synthesized CHANGELOG entry for multi-PR groups. Filled
    # by :func:`_maybe_synthesize_changelog` once per run and read by
    # :func:`_build_changelog_block`. ``None`` means "use the source
    # PRs' own entries" (singletons, ai_changelog disabled, or a
    # synthesis failure).
    synthesized_changelog: str | None = None
    # Set of source-PR URLs already present on the existing port branch
    # (read from ``Source-PR:`` commit trailers) when running with
    # ``if_exists: append``. The cherry-pick loop skips PRs whose URL
    # appears here, while ``_unit_body`` still iterates the full
    # ``prs`` list — so the rebase PR description reflects the declared
    # group order regardless of whether a given PR was applied on this
    # run or a prior one.
    applied_pr_urls: set[str] = field(default_factory=set)
    # Other unit IDs this unit depends on. Populated from
    # :class:`PRGroupConfig.depends_on` for group units (singletons can
    # only carry deps via single-PR groups in the deps_file overlay).
    # Processing order is topologically sorted on this field; processing
    # gates on every entry having ``status: merged`` in target.
    depends_on: list[str] = field(default_factory=list)

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
    """``--only`` / ``--pr`` filter: scope an action to a single PR / group / feature.

    Built from either ``--only <URL|id>`` or ``--pr <URL>``. The
    name is matched against group IDs (``pr_sources.groups[].id``) and
    singleton feature IDs (``pr-<N>``, ``<owner>-<repo>-pr-<N>``). URLs
    are parsed into a ``(owner, repo, number)`` tuple so cross-repo
    sources can't collide with same-numbered origin PRs.

    ``soft`` distinguishes the two CLI sources:

      * ``--only`` (soft=False) — user intent is "I know this exists,
        run it"; if nothing matches, the command exits non-zero so a
        typo doesn't fail silently.
      * ``--pr``   (soft=True)  — user intent is "if this PR is in our
        scope, act on it; otherwise do nothing"; designed for
        webhook / cron callers that don't know in advance whether
        the PR belongs to this project. A no-match exits cleanly with
        a "not in scope" notice.
    """
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
    """Parse a ``--only`` argument into an :class:`OnlyFilter`.

    Returns ``None`` when the flag was not given. A value that looks
    like a GitHub PR URL is parsed to ``(owner, repo, number)``;
    anything else is taken as a group / feature ID.
    """
    if not only:
        return None
    only = only.strip()
    if not only:
        return None
    parsed = parse_pr_url(only)
    if parsed is not None:
        return OnlyFilter(raw=only, pr_ref=parsed, name=None)
    if only.startswith(("http://", "https://")):
        # Looks like a URL but didn't parse as a PR URL — better to
        # surface this clearly than silently treat it as a group name.
        raise ValueError(
            f"--only={only!r} looks like a URL but isn't a "
            "https://github.com/<owner>/<repo>/pull/<N> link."
        )
    return OnlyFilter(raw=only, pr_ref=None, name=only)


def parse_pr_url_filter(pr: str | None) -> OnlyFilter | None:
    """Parse a ``--pr <URL>`` argument into a soft :class:`OnlyFilter`.

    URL-only counterpart to :func:`parse_only`: the user is filtering
    by PR identity, so we require a real PR URL and never fall through
    to the name path. ``soft=True`` so no-match exits cleanly — the
    caller's intent is "act on this PR if you can, otherwise quietly
    skip", not "fail if it's missing".
    """
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
    """Detection ladder: explicit override → forward_port_labels →
    cross-origin → upstream-configured → forward_port default.
    """
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
    """Branch / state ID for a single-PR port.

    Origin PRs keep the historical short ``pr-<N>`` form so existing state
    files and branches keep working. PRs from other repos get a slug-prefixed
    ID (``<owner>-<repo>-pr-<N>``) so a same-numbered origin PR can't clash
    with them.
    """
    if origin_slug and pr.repo_slug == origin_slug:
        return f"pr-{pr.number}"
    owner, repo = pr.repo_slug.split("/", 1)
    return f"{owner}-{repo}-pr-{pr.number}"


def _author_filter_reason(
    author: str | None,
    included_authors: set[str],
    excluded_authors: set[str],
) -> str | None:
    """Return a human-readable reason to drop a PR by author, or ``None``
    if the PR survives the author filter.

    ``included_authors`` and ``excluded_authors`` are lower-cased GitHub
    logins. When ``included_authors`` is non-empty it's an allowlist:
    PRs whose author is unknown or not on the list are dropped.
    """
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
        # Singletons get the matching by_labels entry's ai_context as
        # unit-level context. include_prs entries use the per-PR dict
        # form, which is keyed by URL — promote it to unit-level for the
        # singleton (only one cherry-pick step, so the distinction is
        # cosmetic).
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
    """Materialise group units, fetching each PR. Returns (units, claimed_refs)
    where ``claimed_refs`` are ``(owner, repo, number)`` tuples that should
    NOT also appear as standalone units.

    ``included_authors`` and ``excluded_authors`` are lower-cased GitHub
    logins; they filter group members the same way they filter top-level
    discovered PRs.
    """
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
            # Unmerged PRs sort to the end via the "9999" sentinel; PR
            # number breaks ties between same-timestamp (or both-unmerged)
            # entries deterministically.
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
            ai_context=group.ai_context,
            per_pr_ai_context=dict(group.pr_ai_contexts),
            depends_on=list(group.depends_on),
            mode=_detect_port_mode(
                config, group_prs[0], explicit_mode=group.mode,
            ),
        ))
    return units, claimed


def _prune_superseded_singletons(config: Config, state: PipelineState) -> bool:
    """Drop singleton state entries for PRs now claimed by a ``pr_sources.group``.

    When a PR that used to be ported as its own feature (``pr-<N>``) is
    moved into a group entry in ``pr_sources.groups``, the group becomes the
    unit of work (``feature/<base>/<group-id>``) and the old singleton state
    entry is stale. Left alone, ``continue`` would keep pushing it and opening
    a duplicate PR alongside the group's combined PR.

    This helper removes those stale singleton entries from state. Any branches
    / PRs they already produced are left on GitHub — the user decides whether
    to close them.

    Returns True if any entry was removed (so the caller can persist state).
    """
    group_pr_refs: set[PRRef] = set()
    group_feature_ids: set[str] = set()
    for group in config.pr_sources.groups:
        group_feature_ids.add(group.id)
        for url in group.prs:
            parsed = parse_pr_url(url)
            if parsed is not None:
                group_pr_refs.add(parsed)

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
        extra = ""
        if fs.rebase_pr_url:
            extra = (
                f" — open rebase PR {fs.rebase_pr_url} left untouched; "
                "close it manually if superseded by the group PR"
            )
        console.print(
            f"  [yellow]⚠[/yellow] Dropping stale singleton "
            f"[cyan]{fid}[/cyan] (PR {ref_label} is now in a group){extra}"
        )
        del state.features[fid]

    return bool(stale)


# ---------------------------------------------------------------------------
# PR discovery (shared by run_pipeline and import_state)
# ---------------------------------------------------------------------------


def discover_feature_units(config: Config) -> list["FeatureUnit"]:
    """Discover the PR units defined by ``config.pr_sources``.

    Pure discovery: hits GitHub but touches no git worktree. Returns the
    fully-filtered, ordered list of :class:`FeatureUnit`s the pipeline
    would attempt to port, so other commands (``releasy project pull``) can
    rebuild state from the same source-of-truth definition.

    Output (``console.print``) mirrors ``releasy run``'s "Phase: Porting"
    discovery block — this helper is called in place of the inline block
    that used to live in :func:`run_pipeline`.
    """
    origin_slug = get_origin_repo_slug(config)

    # --- Collect PRs from all sources (union) ---
    # Keyed by (owner, repo, number) so cross-repo PR references can never
    # collide with same-numbered origin PRs.
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

    # --- Build sequential PR groups (each becomes one combined feature) ---
    excluded_label_set = set(prs_cfg.exclude_labels)
    group_units, claimed_pr_refs = _build_group_units(
        config, exclude_pr_refs, excluded_label_set,
        included_authors, excluded_authors,
    )
    # Drop any singletons that the groups have claimed.
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
    return _topo_sort_units(units)


def _topo_sort_units(units: list[FeatureUnit]) -> list[FeatureUnit]:
    """Order units so every ``depends_on`` reference is processed before its
    dependent, with merge-time as the tie-breaker.

    Kahn's algorithm: at each step, pick the ready unit (no unprocessed deps
    pointing into the ``units`` set) with the smallest ``sort_key``. Deps
    pointing OUTSIDE the ``units`` set are ignored here — the runtime gate
    in ``_deps_satisfied`` is what enforces "must be merged in target". The
    sort only ensures that *within this run*, a parent unit gets a chance
    before its dependents.

    Cycles raise ValueError listing the offending feature_ids.
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
                # Insertion-sorted to keep deterministic merge-time order.
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
    """Return the list of unit IDs in ``unit.depends_on`` whose state is not
    yet ``merged``. Empty list means every dep is satisfied.

    Pure lookup — does not touch the network. Call
    :func:`_refresh_dep_states_from_github` once before the iteration
    loop to promote ``branch_created`` deps that have merged upstream.
    """
    unmet: list[str] = []
    for dep_id in unit.depends_on:
        dep_fs = state.features.get(dep_id)
        if dep_fs is None or dep_fs.status != "merged":
            unmet.append(dep_id)
    return unmet


def _refresh_all_merge_status_from_github(
    config: Config, state: PipelineState,
) -> int:
    """Promote / demote tracked features based on what GitHub says.

    For every feature whose status is still in-flight and that has a
    ``rebase_pr_url``, do one PR fetch and apply the terminal state:

    * GitHub reports merged → ``status = "merged"``.
    * GitHub reports closed-unmerged → ``status = "closed"`` with a
      ``skip_reason`` so ``releasy status`` explains WHY the entry is
      terminal. Closed entries are then ignored by the refresh loop and
      by ``releasy run``; only ``pr_policy.recreate_closed_prs`` re-enters
      them — via a renumbered port branch in ``_process_feature_unit``.
    * Still open → no change.

    Broader than :func:`_refresh_dep_states_from_github`: that one only
    refreshes deps referenced by units in the current run; this one
    walks every tracked feature so post-merge bookkeeping (the
    ``merged_label`` sweep) doesn't miss merges that landed on PRs
    nobody depends on.

    Returns the number of features whose status changed in this pass
    (merged + newly-closed combined).
    """
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
            fs.conflict_files = []
            fs.failed_step_index = None
            fs.partial_pr_count = None
            changed += 1
        elif info.state == "closed":
            fs.status = "closed"
            fs.conflict_files = []
            fs.failed_step_index = None
            fs.partial_pr_count = None
            fs.skip_reason = "rebase PR closed without merging"
            changed += 1
    return changed


def _scan_target_for_cherry_picks(
    repo_path: Path, base_ref: str, source_shas: set[str],
) -> dict[str, str]:
    """Map ``{source_sha: citing_commit_sha}`` for cherry-pick footers on target.

    Walks the last 2 000 commits on ``base_ref`` (cheap after a fetch) and
    parses ``(cherry picked from commit <sha>)`` footers — `git cherry-pick
    -x` always cites the full 40-char SHA, so set membership is exact.
    First match wins (commits are in reverse-chrono order).
    """
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
    """Map ``{source_sha: superseding_pr_url}`` for open PRs on ``base_branch``.

    Single batched GraphQL fetch of every open PR's commit messages
    targeting the same base (paginated 50 PRs at a time, 250 commits per
    PR). PRs in ``exclude_pr_urls`` (typically our own rebase PRs) are
    skipped so we don't self-supersede.
    """
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
    """Mark entries whose source PRs were cherry-picked elsewhere.

    Two flavours of evidence, both surfaced via the same scans of
    target-branch git history and open PRs targeting the same base:

    * **Cherry-picks the same source** — another PR's commit cites the
      upstream source PR's ``merge_commit_sha``. For groups, every
      source URL must have its merge SHA cited.

    * **Cherry-picks FROM our port** — another PR's commit cites one of
      OUR port branch's new commit SHAs (the ones we added via
      ``cherry-pick -x``). Any single port-commit match is enough to
      mark the feature superseded; this case typically arises when
      someone took our branch as a starting point. Cheap to fingerprint
      via ``git rev-list <remote>/<branch> ^<remote>/<base>``.

    ``closed`` entries are included on purpose: a closed rebase PR is
    often closed *because* a parallel port landed. Promoting closed →
    superseded gives a more informative terminal state AND blocks the
    ``recreate_closed_prs`` opt-back-in (superseded is unconditionally
    terminal).

    Our own rebase PRs are excluded from the open-PR scan so they
    don't mark themselves.

    Gated by ``pr_policy.detect_superseded`` (default on). Returns the
    number of features promoted to ``superseded`` on this pass.
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
        urls = list(fs.pr_urls) if fs.pr_urls else (
            [fs.pr_url] if fs.pr_url else []
        )
        if urls:
            feature_sources[fid] = urls

    if not feature_sources:
        return 0

    # Resolve each source URL to its merge_commit_sha. Sources that
    # aren't merged on origin can't be matched against cherry-pick
    # footers (those cite the merged commit), so we simply drop them.
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

    # Fingerprint our port branches' new commits — anything someone
    # could cherry-pick FROM us. ``rev-list <branch> ^<base> -n 100``
    # is bounded and post-fetch cheap; missing branches just skip.
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
        # First check: any of OUR port commits cited elsewhere.
        # A single hit is enough — "they cherry-picked from us".
        port_evidence: tuple[str, str] | None = None
        for sha in port_shas_by_feature.get(fid, []):
            port_evidence = _evidence_for(sha)
            if port_evidence:
                break

        if port_evidence:
            fs = state.features[fid]
            fs.status = "superseded"
            fs.conflict_files = []
            fs.failed_step_index = None
            fs.partial_pr_count = None
            where, ref = port_evidence
            fs.skip_reason = f"superseded by {where} {ref}"
            changed += 1
            continue

        # Fallback: every source merge SHA must be cited elsewhere.
        # Groups need all sources covered before the group is retired.
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
        fs.conflict_files = []
        fs.failed_step_index = None
        fs.partial_pr_count = None
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
    """Source PR URLs the port was cherry-picked from.

    Prefers ``pr_urls`` (populated for multi-PR groups) and falls back to
    ``pr_url`` for singletons. Returns an empty list when neither is set.
    """
    if fs.pr_urls:
        return list(fs.pr_urls)
    if fs.pr_url:
        return [fs.pr_url]
    return []


def _apply_merged_labels(config: Config, state: PipelineState) -> None:
    """Apply ``config.merged_label`` to merged port PRs, strip from sources.

    Idempotent across runs via the per-feature ``merged_label_applied``
    flag: each merged unit is processed exactly once. No-op when
    ``config.merged_label`` is unset, when ``push`` is off (we never write
    to GitHub then), or when there are no newly-merged units to process.

    Source-PR cleanup is best-effort and only touches PRs hosted on the
    configured origin — source PRs on a different repo are skipped (we
    never write outside origin).
    """
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
            # Logged inside add_label_to_pr; skip the source-side work
            # so we retry the whole unit on the next run.
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
    """Promote ``branch_created`` deps to ``merged`` if their rebase PR
    has merged on GitHub since the state file was written.

    Called once before the unit-iteration loop so ``_unmet_deps`` can run
    purely off the state dict. Touches at most one GitHub call per unique
    dep ID with an outstanding rebase PR.
    """
    referenced: set[str] = set()
    for u in units:
        referenced.update(u.depends_on)
    if not referenced:
        return
    # Any dep that has an open rebase PR could have merged upstream since
    # the last run — including ones that previously hit a conflict and
    # were later fixed by hand. Don't over-narrow to ``branch_created``.
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
            dep_fs.conflict_files = []
            dep_fs.failed_step_index = None
            dep_fs.partial_pr_count = None
        elif info.state == "closed":
            dep_fs.status = "closed"
            dep_fs.conflict_files = []
            dep_fs.failed_step_index = None
            dep_fs.partial_pr_count = None
            dep_fs.skip_reason = "rebase PR closed without merging"


# ---------------------------------------------------------------------------
# Main pipeline
# ---------------------------------------------------------------------------


def run_pipeline(
    config: Config,
    onto: str,
    work_dir: Path | None = None,
    resolve_conflicts: bool = True,
    retry_failed: bool = True,
    only: OnlyFilter | None = None,
    force_merge: bool = False,
) -> PipelineState:
    """Port PRs onto ``origin/<base_branch>``.

    ``resolve_conflicts`` is a CLI-level kill-switch. The AI resolver only
    runs when both this flag and ``config.ai_resolve.enabled`` are true.

    ``retry_failed`` controls what happens when a discovered unit already
    has a ``conflict`` entry in state from a previous run: when true, the
    existing local / remote port branch is discarded and the cherry-pick
    is re-attempted from base; when false, the entry is skipped entirely
    (no cherry-pick, no PR side-effects). Defaults to true to match the
    config-level default.

    ``only`` (optional) restricts processing to a single PR (matched by
    URL) or a single group / feature ID. Other discovered units are
    dropped before any side-effects.
    """
    state = load_state(config)
    _prune_superseded_singletons(config, state)
    repo_path = _setup_repo(config, work_dir, config.base_branch_name(onto))

    if is_operation_in_progress(repo_path):
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

    base_branch = config.base_branch_name(onto)
    remote = config.origin.remote_name

    # Verify the base branch already exists on origin.
    if not branch_exists(repo_path, base_branch, remote):
        console.print(
            f"\n[red]✗[/red] Base branch [cyan]{base_branch}[/cyan] does not exist "
            f"on remote [cyan]{remote}[/cyan].\n"
            f"  Create and push it first, then re-run."
        )
        raise SystemExit(2)

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

    if config.push:
        ensure_label(
            config, RELEASY_LABEL, RELEASY_LABEL_COLOR, RELEASY_LABEL_DESCRIPTION,
        )
        for sess_label in _session_pr_labels(config):
            ensure_label(
                config, sess_label, RELEASY_LABEL_COLOR,
                "Session label (session.pr_labels)",
            )

    ai_active = resolve_conflicts and config.ai_resolve.enabled
    if ai_active:
        console.print(
            f"[dim]AI conflict resolver: enabled "
            f"(command='{config.ai_resolve.command}', "
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
        why = "disabled via --no-resolve-conflicts" if not resolve_conflicts else "disabled in config"
        console.print(f"[dim]AI conflict resolver: {why}[/dim]")

    # The needs-attention label is used for partial-group draft PRs whenever
    # an unresolved conflict surfaces, regardless of whether the AI resolver
    # was enabled — pre-create it so the PR-creation path doesn't fail on a
    # missing label. Same goes for the missing-prereqs and auto-prereq
    # labels: cheaper to ensure them once up-front than on every conflict.
    if config.push:
        _ensure_conflict_labels(config)

    console.print(
        f"\n[bold]Phase:[/bold] Porting PRs onto [cyan]{base_branch}[/cyan]"
    )

    units = discover_feature_units(config)

    if only is not None:
        before = len(units)
        units = [u for u in units if only.matches_unit(u)]
        flag = "--pr" if only.soft else "--only"
        console.print(
            f"\n  [dim]{flag}={only.label}: "
            f"kept {len(units)}/{before} discovered unit(s)[/dim]"
        )
        if not units:
            if only.soft:
                # ``--pr`` is the "act-if-in-scope" form — a no-match
                # is the expected outcome for PRs that don't belong to
                # this session. Exit cleanly so cron / webhook callers
                # don't have to special-case it.
                console.print(
                    f"\n  [dim]--pr={only.label!r} is not in this "
                    "session's scope — nothing to do.[/dim]"
                )
                return load_state(config)
            console.print(
                f"\n[red]✗[/red] --only={only.label!r} matched no "
                "discovered units. Check the URL / group id and re-run."
            )
            raise SystemExit(2)

    if not units:
        console.print("\n  [dim]No PRs or groups to process after filtering[/dim]")

    # Refresh dep state once so any depends_on entry that has merged
    # upstream since the last run gets promoted before the per-unit gate.
    # Persist immediately: the promotion is the result of a real GitHub
    # call, and if the iteration below crashes (Ctrl-C, OOM, exception in
    # an early unit) the merge status would otherwise be re-fetched on
    # the next run — wasting tokens and risking a transient False that
    # leaves a now-mergeable dep stuck blocked.
    _refresh_dep_states_from_github(config, state, units)
    # Catch ports merged externally since the previous run so the
    # ``merged_label`` sweep below can apply / strip the configured label.
    # Cheap when ``merged_label`` is unset (the call is still useful to
    # keep the state file's ``status`` accurate for ``releasy status``).
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
        # Skip units already in a terminal state on the local state file —
        # ``merged`` (port already shipped, no work left to do) or ``skipped``
        # (user opted out). Without this, a re-run after `releasy project pull`
        # or after the merged-status sweep promoted a unit would re-cherry-
        # pick source PRs whose port is already merged.
        #
        # ``closed`` is also terminal — the rebase PR was closed without
        # merging — but ``pr_policy.recreate_closed_prs`` opts back in: the
        # renumbered-branch path in ``_process_feature_unit`` re-attempts
        # the port on ``<canonical>-1`` / ``-2`` / … . Without that flag,
        # closed entries stay terminal so we don't waste a cherry-pick on
        # a PR the user (or someone else) already decided against.
        prev_fs = state.features.get(unit.feature_id)
        terminal = {"merged", "skipped", "superseded"}
        if not config.pr_policy.recreate_closed_prs:
            terminal.add("closed")
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
                    prev_fs.status in ("skipped", "closed", "superseded")
                    and prev_fs.skip_reason
                ):
                    console.print(
                        f"    [dim]· reason: {prev_fs.skip_reason}[/dim]"
                    )
                _dry_record(state, f"skip-{prev_fs.status}")
            continue
        # _process_feature_unit always returns "continue" — unresolved
        # conflicts are now handled in-place (drop the branch or open a
        # draft PR), and the pipeline keeps moving so a single bad PR
        # can't strand the whole queue.
        _process_feature_unit(
            config, repo_path, state, unit, base_branch, base_ref, onto,
            remote, ai_active, retry_failed=retry_failed,
            force_merge=force_merge,
        )

    state.phase = "ports_done"
    _persist_state(config, state)

    # --- Summary ---
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
                ("blocked-by-deps",             "blocked by unmet deps (no action)"),
                ("skip-conflict-retry-off",     "skip — prior conflict, retry-off"),
                ("skip-merged",                 "skip — already merged"),
                ("skip-skipped",                "skip — user-marked skipped"),
                ("skip-closed",                 "skip — rebase PR closed unmerged"),
                ("skip-superseded",             "skip — superseded by another PR"),
            ]
            for code, label in order:
                if counts.get(code):
                    console.print(f"  [magenta]·[/magenta] {label}: {counts[code]}")
            # Catch any unmapped action codes so the summary stays honest
            # if a new one is added but not registered above.
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
    if ready or branch_only or conflicts:
        console.print(
            f"  Ports: {ready} needs-review, {branch_only} branch-created, "
            f"{conflicts} conflict ({ai_assisted} ai-assisted)"
        )

    return state


# ---------------------------------------------------------------------------
# Sequential mode
# ---------------------------------------------------------------------------


def run_sequential(
    config: Config,
    onto: str,
    work_dir: Path | None = None,
    resolve_conflicts: bool = True,
    retry_failed: bool = True,
    only: OnlyFilter | None = None,
    force_merge: bool = False,
) -> PipelineState:
    """Process the merged-time-sorted PR queue one PR per invocation.

    Loop semantics — for each unit in ``discover_feature_units(config)``
    order (already sorted by ``merged_at``):

      * state ``merged`` / ``skipped``     → already done, skip.
      * state ``needs_review`` /
        ``branch_created`` with rebase PR  → ask GitHub if the PR is
                                            merged. Yes → mark
                                            ``merged`` in state and
                                            continue. No → exit 1 with
                                            the in-flight PR URL.
                                            Lookup failure → exit 1.
      * state ``branch_created`` with no
        rebase PR                           → exit 1 (something went
                                            wrong opening the PR — fix
                                            it and retry).
      * state ``conflict``                  → exit 1 with the conflict
                                            files (sequential mode never
                                            auto-retries).
      * no state at all                    → port it (cherry-pick onto
                                            the freshly-fetched
                                            ``origin/<base>``, push, open
                                            PR), then return so the
                                            caller exits cleanly.

    ``releasy run`` and ``releasy continue`` (no ``--branch``) both
    dispatch here when ``config.sequential`` is true; the function
    is the single source of truth for sequential-mode behaviour.
    """
    state = load_state(config)
    _prune_superseded_singletons(config, state)
    repo_path = _setup_repo(config, work_dir, config.base_branch_name(onto))

    if is_operation_in_progress(repo_path):
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

    base_branch = config.base_branch_name(onto)
    remote = config.origin.remote_name

    if not branch_exists(repo_path, base_branch, remote):
        console.print(
            f"\n[red]✗[/red] Base branch [cyan]{base_branch}[/cyan] does not exist "
            f"on remote [cyan]{remote}[/cyan].\n"
            f"  Create and push it first, then re-run."
        )
        raise SystemExit(2)

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

    if config.push:
        ensure_label(
            config, RELEASY_LABEL, RELEASY_LABEL_COLOR, RELEASY_LABEL_DESCRIPTION,
        )
        for sess_label in _session_pr_labels(config):
            ensure_label(
                config, sess_label, RELEASY_LABEL_COLOR,
                "Session label (session.pr_labels)",
            )

    ai_active = resolve_conflicts and config.ai_resolve.enabled
    if ai_active:
        console.print(
            f"[dim]AI conflict resolver: enabled "
            f"(command='{config.ai_resolve.command}', "
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

    console.print(
        f"\n[bold]Phase:[/bold] Sequential porting onto [cyan]{base_branch}[/cyan]"
    )

    units = discover_feature_units(config)

    if only is not None:
        before = len(units)
        units = [u for u in units if only.matches_unit(u)]
        flag = "--pr" if only.soft else "--only"
        console.print(
            f"\n  [dim]{flag}={only.label}: "
            f"kept {len(units)}/{before} discovered unit(s)[/dim]"
        )
        if not units:
            if only.soft:
                # ``--pr`` is the "act-if-in-scope" form — a no-match
                # is the expected outcome for PRs that don't belong to
                # this session. Exit cleanly so cron / webhook callers
                # don't have to special-case it.
                console.print(
                    f"\n  [dim]--pr={only.label!r} is not in this "
                    "session's scope — nothing to do.[/dim]"
                )
                return load_state(config)
            console.print(
                f"\n[red]✗[/red] --only={only.label!r} matched no "
                "discovered units. Check the URL / group id and re-run."
            )
            raise SystemExit(2)

    # Pick up ports that merged externally since the last run so the
    # ``merged_label`` sweep below has accurate state to work from.
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

        seq_terminal = {"merged", "skipped", "superseded"}
        if not config.pr_policy.recreate_closed_prs:
            seq_terminal.add("closed")
        if fs is not None and fs.status in seq_terminal:
            console.print(
                f"  [dim]{unit.feature_id} ({ref}) — {fs.status}, skipping[/dim]"
            )
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
            # Fall through: _process_feature_unit will see the conflict
            # state below, force-recreate the branch, and re-cherry-pick.

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
            fs.conflict_files = []
            console.print(
                f"    [green]✓[/green] PR merged — advancing the queue"
            )
            _apply_merged_labels(config, state)
            _persist_state(config, state)
            continue

        # No state yet — this is the next unit to port. Refresh remote so
        # the new branch is created off the latest base (which now
        # includes any previously-merged sequential ports).
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
            # _process_feature_unit appends to config.features; guard against
            # a duplicate by short-circuiting if it would clash. This matches
            # the run_pipeline behaviour (which also skips in this case).
            continue

        _process_feature_unit(
            config, repo_path, state, unit, base_branch, base_ref, onto,
            remote, ai_active, retry_failed=retry_failed,
            force_merge=force_merge,
        )

        # _process_feature_unit may have ended in either a clean port or
        # an unresolved conflict. Either way, sequential mode stops here:
        # the user reviews the PR (or fixes the conflict) before invoking
        # again.
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

    # Queue exhausted — nothing left.
    state.phase = "ports_done"
    _persist_state(config, state)
    console.print(
        "\n[green]✓ All sequential ports processed[/green] — queue is empty."
    )
    return state


def _unit_pr_meta(unit: FeatureUnit) -> dict:
    """Build state-meta dict for a unit (single PR or group).

    ``pr_author`` is the GitHub login of the *primary* (first) PR — for
    groups, that's the author of the first PR in cherry-pick order, per
    the user's spec for the project board's default ``Assignee Dev``.
    May be ``None`` when GitHub didn't expose the author (rare:
    deleted accounts).
    """
    primary = unit.primary_pr()
    return {
        "pr_url": primary.url,
        "pr_number": primary.number,
        "pr_title": primary.title,
        "pr_body": primary.body,
        "pr_numbers": [pr.number for pr in unit.prs],
        "pr_urls": [pr.url for pr in unit.prs],
        "pr_author": primary.author,
    }


_VERSION_TOKEN = r"v?\d+(?:\.\d+)+"
_RELEASY_PREFIX_RE = re.compile(r"^\[releasy\b[^\]]*\]\s*", re.IGNORECASE)

# Label automatically applied to every PR RelEasy opens or updates, so a
# project's GitHub UI can filter "everything releasy created/touched" at a
# glance — and so the title itself stays clean.
RELEASY_LABEL = "releasy"
RELEASY_LABEL_COLOR = "1F6FEB"  # GitHub blue
RELEASY_LABEL_DESCRIPTION = "Created/managed by RelEasy"


def _ensure_conflict_labels(config: Config) -> None:
    """Pre-create every label the conflict-handling paths might apply.

    Cheaper than re-checking on each conflict and removes the chance that
    a label-application call inside the hot path fails on a missing
    label (which would degrade gracefully but spam warnings).
    """
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
    """Render ``config.project`` for inclusion in PR titles.

    Lower-case names get title-cased (``antalya`` → ``Antalya``,
    ``stable-26`` → ``Stable-26``); names that already carry mixed case
    are preserved verbatim (so e.g. ``ClickHouse`` stays ``ClickHouse``).
    """
    if not project:
        return ""
    if project.islower():
        return project.title()
    return project


def _version_label(project: str | None, base_branch: str | None) -> str:
    """Pull the version suffix out of the base branch name when possible.

    If ``base_branch`` follows the conventional ``<project>-<version>``
    shape (``antalya-26.3``), the bit after ``<project>-`` is the version
    label. Otherwise the whole branch name is used so the prefix still
    points at the real target — never silently drops information.
    """
    if not base_branch:
        return ""
    if project:
        prefix = f"{project.lower()}-"
        if base_branch.lower().startswith(prefix):
            return base_branch[len(prefix):]
    return base_branch


def _subject_prefix(project: str | None, base_branch: str | None) -> str:
    """Build the ``"Antalya 26.3"``-style PR title prefix.

    Falls back gracefully:

      - both pieces present → ``"Antalya 26.3"``
      - project only        → ``"Antalya"``
      - base only           → ``"antalya-26.3"``
      - neither             → ``""`` (caller handles)
    """
    proj = _display_project(project)
    ver = _version_label(project, base_branch)
    if proj and ver:
        return f"{proj} {ver}"
    return proj or ver or ""


def _strip_misleading_title_prefix(title: str, project: str | None) -> str:
    """Strip a misleading version-prefix from a source PR title.

    Source repos often title backport PRs with their own target version,
    e.g. ``"26.1 Antalya: Token Authentication and Authorization"`` for a
    PR landing on ``antalya-26.1``. When that PR is re-ported onto a
    different base (say ``antalya-26.3``), the embedded ``"26.1"`` becomes
    actively misleading in the rebase PR title.

    This helper drops, in order of preference:

      1. A leading ``[releasy …]`` tag from a previous run (so we never
         double-tag when porting one of our own rebase PRs).
      2. ``"<version> <project>[:|-] "`` (e.g. ``"26.1 Antalya: "``).
      3. ``"<project> <version>[:|-] "`` (e.g. ``"Antalya 26.1: "``).
      4. Bare ``"<version>[:|-] "`` (e.g. ``"v3.2 - "``).

    A bare leading version with no separator (``"26.1 Foo"``) is left
    alone — without a colon/dash we can't tell a target-version prefix
    from a genuine title.
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
    """Synthesise the PR title for a unit.

    Format: ``"<Project> <version>: <subject>"`` — e.g.
    ``"Antalya 26.3: Token Authentication and Authorization"``. The
    ``[releasy]`` text tag is gone; identification is done via the
    ``releasy`` label on the PR (see :data:`RELEASY_LABEL`).

    Source PR titles are sanitised via ``_strip_misleading_title_prefix``
    so a leading ``"26.1 Antalya: …"`` (the source's own target version)
    doesn't leak into a rebase PR that actually targets a different
    version.
    """
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


# Matches any level markdown heading: "# title", "## title", … up to 6.
_MD_HEADING_RE = re.compile(r"^(#{1,6})[ \t]+(.+?)[ \t]*$", re.MULTILINE)


def _extract_md_section(body: str, keyword: str) -> str | None:
    """Return the text under the first markdown heading whose title
    contains ``keyword`` (case-insensitive), up to the next heading or
    end of document. ``None`` if not found or section is empty.
    """
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
    """Like :func:`_extract_md_section` but keeps the heading line and
    any nested subheadings.

    Boundary rule: the section runs from its heading line to the next
    heading of *equal or higher* level (fewer or equal ``#``s), or to
    end-of-document. So ``### CI/CD Options`` followed by
    ``#### Exclude tests:`` + ``#### Regression jobs to run:`` is
    returned as a single block instead of being cut off at the first
    ``####``. Returns ``None`` when no matching heading exists.
    """
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
    """Remove every markdown section whose heading contains any of ``keywords``.

    Section boundaries follow the same equal-or-higher-level rule used
    by :func:`_extract_md_section_with_subsections`, so removing
    ``CI/CD Options`` takes its ``####`` subheadings (Exclude tests /
    Regression jobs to run) along with it instead of leaving orphans.

    Used by :func:`_unit_body` to keep per-source-PR bodies from
    re-inserting Changelog / CI/CD blocks the combined PR already
    presents once at the top.
    """
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
    # Collapse runs of blank lines created by the deletions so the
    # remaining body doesn't end up with awkward 3-line gaps.
    out = re.sub(r"\n{3,}", "\n\n", out)
    return out.strip()


# Per-PR body sections that the combined PR already presents once at
# the top (changelog) or that we deliberately deduplicate / reset (CI
# options). Stripping them from each per-PR body keeps the combined
# rebase PR readable instead of repeating the same template scaffolding
# N times.
_DEDUP_PR_BODY_SECTIONS = (
    "changelog category",
    "changelog entry",
    "ci/cd options",
)

# Canonical CI/CD Options block for rebase PR bodies when a source PR
# uses the same template (heading match). Checkbox defaults are fixed
# here so ports do not inherit ad-hoc source-PR toggles or an all-empty
# reset.
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
    """Pull the single chosen 'Changelog category' value from a PR body.

    ClickHouse's template lists every category as a bullet and authors
    usually delete all but one. We return the first non-empty bullet (or
    plain line) we find under the heading; ``None`` if absent.
    """
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
    """Pull the 'Changelog entry' paragraph from a PR body, stripped.

    Drops placeholder-only sections (e.g. the raw template hint line
    left behind when the author wrote nothing).
    """
    section = _extract_md_section(body, "changelog entry")
    if not section:
        return None
    # Some PR bodies contain an HTML comment or the default hint inside
    # the section when no entry was added. Remove obvious placeholders.
    cleaned = re.sub(r"<!--.*?-->", "", section, flags=re.DOTALL).strip()
    if not cleaned:
        return None
    low = cleaned.lower()
    if low.startswith("...") or low in {"n/a", "na", "none", "-"}:
        return None
    return cleaned


def _build_changelog_block(unit: FeatureUnit) -> str | None:
    """Synthesise a 'Changelog category' + 'Changelog entry' block for
    the combined PR body.

    Rules:
    - Category = first PR's category (fallback: any PR in the unit
      that does specify one, in listed order).
    - Entry source order:
        1. ``unit.synthesized_changelog`` when set — the AI-composed
           summary for groups (see :func:`_maybe_synthesize_changelog`).
           Only ever populated for multi-PR groups when
           ``ai_changelog.enabled`` is true.
        2. Otherwise the first PR's own changelog entry, in listed
           cherry-pick order. For singletons that's always the
           authoritative wording (1:1 from source — no Claude call).
    - Suffix = ``(<url1> by <author1>, …)`` listing every PR in the
      unit, appended even for singletons so reviewers have one-click
      access to the source PR and its author.

    Returns ``None`` when no PR in the unit has either a category or
    an entry, so we don't clutter the body with empty headings.
    """
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
        attribution = _format_pr_attribution(unit.prs)
        final_entry = entry_text
        if attribution:
            # If the entry already ends with ')' keep them on the same
            # line; otherwise append with a space. Either way we strip a
            # trailing period before the paren so punctuation reads
            # cleanly.
            if final_entry.endswith("."):
                final_entry = final_entry[:-1]
            final_entry = f"{final_entry} ({attribution})."
        out.append(final_entry)
        out.append("")
    return "\n".join(out).rstrip()


def _truncate_for_prompt(text: str, max_chars: int) -> str:
    """Trim ``text`` to ``max_chars`` with a visible truncation marker.

    Used when packing source-PR bodies into the changelog-synthesis
    prompt so a single long-winded PR can't blow the prompt size out
    for a group with many entries.
    """
    if not text:
        return ""
    if max_chars <= 0 or len(text) <= max_chars:
        return text
    return text[:max_chars].rstrip() + "\n\n…(truncated)"


def _build_pr_blocks_for_synthesis(unit: FeatureUnit, max_pr_body_chars: int) -> str:
    """Render each PR's title + author + body into a markdown block.

    Output is fed directly into the ``{pr_blocks}`` placeholder in the
    changelog-synthesis prompt. PRs appear in cherry-pick order so
    Claude can reason about which fixes supersede which.
    """
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
    """Populate ``unit.synthesized_changelog`` for multi-PR groups.

    Idempotent: returns early when already populated, when
    ``ai_changelog.enabled`` is false, or when the unit has fewer than
    two PRs (singletons take the source PR's wording verbatim — no
    Claude call, no token spend, no surprises).

    Failures are non-fatal: a synthesis error logs a warning and
    leaves ``unit.synthesized_changelog`` as ``None``, which means
    :func:`_build_changelog_block` falls back to the first PR's own
    changelog entry — the pre-AI behaviour.
    """
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
    """Return a single ``### CI/CD Options`` block for the combined PR body.

    When any source PR body contains a ``### CI/CD Options`` heading, we
    insert :data:`_DEFAULT_CI_CD_OPTIONS_BLOCK` — a fixed checklist with
    intended default checkmarks — rather than copying the source text
    (which would inherit arbitrary reviewer toggles) or clearing every
    box (which lost the project's preferred defaults).

    Returns ``None`` when no PR in the unit carries a CI options
    section — in that case the combined body omits the block so repos
    without this template are unaffected.
    """
    for pr in unit.prs:
        section = _extract_md_section_with_subsections(
            pr.body or "", "ci/cd options",
        )
        if section:
            return _DEFAULT_CI_CD_OPTIONS_BLOCK.rstrip()
    return None


def _format_pr_attribution(prs: "list[PRInfo]") -> str:
    """Build the ``<url> by <author>`` comma-separated attribution suffix.

    Falls back to just the URL when the author is unknown.
    """
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
) -> str:
    """Build the PR body. Optional banners cover the partial-group
    intervention case, auto-ported prerequisites, and dropped scope."""
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
        lines.append(
            f"> Cherry-pick of {failed_ref} could not be resolved "
            "automatically (AI resolver was disabled, exhausted its "
            "iteration budget, or gave up)."
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
        lines.append("")  # blank separator before the rest

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
        lines.append("")  # blank separator before the rest

    changelog = _build_changelog_block(unit)
    if changelog:
        lines.append(changelog)
        lines.append("")  # blank separator before the rest

    ci_block = _build_ci_options_block(unit)
    if ci_block:
        lines.append(ci_block)
        lines.append("")  # blank separator before the rest

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
        lines.append("")  # blank
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
    """For grouped units, append a ``Source-PR`` trailer to the just-made
    commit so the combined PR's commit list is self-attributing.

    For singleton units this is skipped — the branch IS the source PR, so
    a trailer would be redundant noise on the commit.
    """
    if not unit.is_group or len(unit.prs) <= 1:
        return
    ref = pr_ref_label(pr.repo_slug, pr.number, origin_slug)
    append_commit_trailer(
        repo_path, "Source-PR", f"{ref} ({pr.url})",
    )


def _cherry_pick_pr(
    repo_path: Path, config: Config, pr: PRInfo,
) -> OperationResult:
    """Cherry-pick one PR into the current branch.

    For PRs from the configured ``origin`` repo we use the origin remote
    (already fetched) and rely on the merge commit being locally present.
    For PRs from any other repo (cross-repo references in
    ``pr_sources.include_prs`` / ``pr_sources.groups[].prs``) we fetch the
    needed commit / PR ref directly from that repo's HTTPS URL — no extra
    git remote is added.
    """
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


# Pulls every URL out of the Source-PR trailers we ourselves wrote
# (e.g. ``#42 (https://github.com/owner/repo/pull/42)``).
_SOURCE_PR_URL_RE = re.compile(r"https?://\S+?/pull/\d+", re.IGNORECASE)


def _read_commit_subjects(
    repo_path: Path, base_ref: str, branch: str,
) -> list[str]:
    """Return the subject (first line) of every commit in
    ``base_ref..branch``, oldest → newest. Empty list on error.
    """
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
    """Walk every commit in ``base_ref..branch`` and pull its
    ``Source-PR:`` trailer URL.

    Returns a tuple ``(per_commit_urls, total_commits)``. ``per_commit_urls``
    has one entry per commit (oldest → newest); the entry is the URL when
    the commit carries a ``Source-PR:`` trailer (the format
    :func:`_tag_commit_with_source_pr` writes), or ``None`` when no trailer
    is present (e.g. commits made before the trailer convention, or AI
    fix-up commits that didn't get re-tagged).
    """
    rev_list = run_git(
        ["rev-list", "--reverse", f"{base_ref}..{branch}"],
        repo_path, check=False,
    )
    if rev_list.returncode != 0:
        return [], 0
    shas = [s for s in rev_list.stdout.split() if s]

    urls: list[str | None] = []
    for sha in shas:
        # ``%(trailers:key=...,unfold=true,valueonly=true)`` only emits the
        # value half of the matching trailer lines (one per line) — won't
        # false-positive on a URL that happens to appear in the body.
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
    """Result of ``_decide_append`` — whether we can append to an existing
    port branch and which declared PRs are still missing.
    """
    feasible: bool
    reason: str                            # human-readable explanation
    applied_urls: set[str] = field(default_factory=set)  # PRs already present
    missing_count: int = 0                 # declared PRs not yet on branch


def _decide_append(
    repo_path: Path,
    base_ref: str,
    branch: str,
    unit: FeatureUnit,
    origin_slug: str | None,
) -> _AppendDecision:
    """Decide whether ``if_exists: append`` can proceed for this unit.

    Trailers are the primary signal: every URL in a ``Source-PR:`` trailer
    must point at a declared PR (foreign trailers fail the check). For
    declared PRs that don't appear in any trailer we scan the branch's
    commit *subjects* for the merge subject line — when releasy cherry-
    picks a merge commit (``-m 1 --no-edit``), the original
    ``Merge pull request #<N> from …`` subject is preserved verbatim, so
    a declared origin PR whose merge was cherry-picked but whose tagging
    step was skipped (e.g. an older releasy version) still shows up.

    Cross-repo PRs (declared with a non-origin URL) can collide on PR
    number with origin PRs, so subject matching is gated on the PR's
    repo_slug equalling ``origin_slug``. Cross-repo PRs without trailers
    fall through to "missing" — the cherry-pick step will then either
    succeed (creating a duplicate commit, harmless) or surface a no-op
    empty cherry-pick which is easy to debug.

    AI conflict-resolution fix-up commits and base-merged-in commits sit
    on the branch with no trailers and no matching subject; they're
    correctly ignored.
    """
    per_commit, _total = _read_source_pr_trailers(repo_path, base_ref, branch)
    declared_urls = [pr.url for pr in unit.prs]
    declared_set = set(declared_urls)

    # 1) Trailers: every URL we wrote must point at a declared PR.
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

    # 2) Subject scan for declared PRs not yet matched.
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


def _next_free_renumbered_port_branch(
    repo_path: Path,
    remote: str,
    canonical_branch: str,
) -> str:
    """Pick ``<canonical_branch>-N`` for the smallest ``N >= 1`` with no
    matching local or remote ref.
    """
    n = 1
    while True:
        candidate = f"{canonical_branch}-{n}"
        if not remote_branch_exists(
            repo_path, candidate, remote,
        ) and not local_branch_exists(repo_path, candidate):
            return candidate
        n += 1


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
    """Process one feature unit (single PR or sequential group).

    Always returns ``"continue"`` — unresolved conflicts are handled
    in-place by :func:`_handle_unresolved_conflict` (drop the local branch
    for singletons / first-of-group, or open a draft PR for partial
    groups), and the pipeline keeps moving.

    ``retry_failed`` controls behaviour for units whose previous run
    ended in ``conflict`` status: when true, any existing local / remote
    port branch is force-recreated from base and the cherry-pick is
    re-attempted; when false, the unit is left exactly as-is (no
    cherry-pick, no PR side-effects).

    ``pr_policy.recreate_closed_prs`` allocates ``feature/.../<id>-1``,
    ``-2``, … when the stored ``rebase_pr_url`` PR was closed without merging,
    then runs the normal cherry-pick + push + open-PR path for that name.
    """
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
    force_retry = is_failed_prev and retry_failed

    # --- Sequential gate: every depends_on must be merged in target ---
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
            # Preserve branch_name / pr_url / etc. from prior state if present
            # — a unit can be temporarily blocked between runs without
            # discarding the work it produced earlier. Conflict / partial-pick
            # bookkeeping is no longer accurate (we never re-attempted the
            # pick this run), so clear it so `releasy status` and the YAML
            # dump don't show stale conflict files under a Blocked entry.
            #
            # ``ai_resolved`` / ``ai_iterations`` / ``ai_cost_usd`` are
            # *intentionally* preserved here: they're historical facts
            # about a previous successful (or partially-successful) AI
            # run on this unit. A future re-attempt may add to them, but
            # we don't want to lose audit / cost-accounting info just
            # because the unit was temporarily blocked between runs.
            blocked_state.conflict_files = []
            blocked_state.failed_step_index = None
            blocked_state.partial_pr_count = None
            blocked_state.prereq_recovery_exhausted = False
            state.features[unit.feature_id] = blocked_state
            _persist_state(config, state)
            _dry_record(state, "blocked-by-deps")
            return "continue"
        # Deps were just satisfied — clear stale blocked state so the
        # unit re-enters the normal flow below.
        if prev_state and prev_state.status == "blocked":
            prev_state.status = "needs_review"
            prev_state.blocked_by = []

    if is_failed_prev and not retry_failed:
        # User opted out of retries — leave the conflicted entry exactly
        # as it is so manual fix-ups (or a later --retry-failed run) can
        # take over without us touching the PR / branch / project board.
        console.print(
            f"\n    [dim]{canonical_branch} ({label}) — previously conflicted, "
            "skipping (pr_policy.retry_failed: false / "
            "--no-retry-failed)[/dim]"
        )
        _dry_record(state, "skip-conflict-retry-off")
        return "continue"

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

    # The merge-status sweep at the top of every run / refresh / continue
    # has already promoted closed-on-GitHub PRs to ``status="closed"``,
    # so the local status IS the source of truth here — no GitHub call.
    prev_was_closed = (
        prev_state is not None and prev_state.status == "closed"
    )

    new_branch = canonical_branch
    if (
        config.pr_policy.recreate_closed_prs
        and not force_retry
        and prev_was_closed
    ):
        new_branch = _next_free_renumbered_port_branch(
            repo_path, remote, canonical_branch,
        )
        console.print(
            f"\n    [yellow]↻[/yellow] Rebase PR closed without merge — "
            f"opening a new port branch [cyan]{new_branch}[/cyan] "
            "([cyan]pr_policy.recreate_closed_prs[/cyan])"
        )

    on_remote = remote_branch_exists(repo_path, new_branch, remote)
    on_local = local_branch_exists(repo_path, new_branch)

    # --- Existing rebase PR → leave it alone in ``releasy run`` ---
    # ``run`` is the "port new PRs" command; once a unit has an open
    # rebase PR on origin, the merge-target-into-branch dance belongs
    # to ``releasy refresh`` (the maintenance command). Doing it here
    # would touch a PR the user didn't ask us to revisit — even when
    # there's nothing to do (PR is clean, target hasn't moved). The
    # only exception is ``if_exists: append``: the user explicitly
    # asked to cherry-pick new sources onto the existing branch, so
    # we fall through to the append handler below.
    if (
        on_remote
        and prev_state is not None
        and prev_state.rebase_pr_url
        and unit.if_exists != "append"
        and not prev_was_closed
    ):
        console.print(
            f"\n    [dim]{new_branch} ({label}) — rebase PR already "
            f"open ({prev_state.rebase_pr_url}); leaving as-is. "
            "Use [cyan]releasy refresh[/cyan] to merge target in.[/dim]"
        )
        _dry_record(state, "skip-existing-pr")
        return "continue"

    # --- if_exists: append ---
    # ``cherry_pick_base`` defaults to ``base_ref`` for the from-scratch
    # path; in append mode it's the existing branch tip so each loop
    # iteration resets to the prefix we want to keep.
    #
    # Branch preservation is the default. ``if_exists`` is the only
    # signal that can drive a from-base rebuild — explicit user intent.
    # ``force_retry`` (from prev-state == ``conflict`` + ``retry_failed:
    # true``) does NOT override ``if_exists``: it only controls whether
    # the unit is processed at all this run, not how. A user who set
    # ``if_exists: append`` keeps the existing branch even when a prior
    # run left a stale ``conflict`` state (common when the prior failure
    # was unrelated to the cherry-pick itself — e.g. a missing prompt
    # file, an OOM, an aborted run).
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
            # Need a local branch to inspect commits on.
            if not on_local:
                run_git(
                    ["branch", "-f", new_branch, f"{remote}/{new_branch}"],
                    repo_path,
                )
                on_local = True
            decision = _decide_append(
                repo_path, base_ref, new_branch, unit, origin_slug,
            )
            if not decision.feasible:
                console.print(
                    f"\n    [yellow]![/yellow] [cyan]{new_branch}[/cyan] "
                    f"({label}) — append not feasible: {decision.reason}; "
                    "skipping (set if_exists: recreate to rebuild)"
                )
                _ensure_pr_for_existing_remote_branch(
                    config, state, unit, new_branch, base_branch,
                )
                return "continue"
            if decision.missing_count == 0:
                console.print(
                    f"\n    [cyan]{new_branch}[/cyan] ({label}) — every "
                    "declared PR is already on the branch; nothing to append"
                )
                _ensure_pr_for_existing_remote_branch(
                    config, state, unit, new_branch, base_branch,
                )
                return "continue"
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

    # ---------------------------------------------------------------
    # Branch-disposition decision matrix (no longer gated on force_retry):
    #
    #   if_exists == "skip"     → existing branch is left alone, period.
    #                             retry_failed/force_retry do NOT override
    #                             this. To retry a previously-conflicted
    #                             unit, the user must explicitly switch to
    #                             "recreate" or "append".
    #   if_exists == "append"   → handled in the block above; either set
    #                             append_active and proceed, or skip with
    #                             a warning.
    #   if_exists == "recreate" → rebuild from base. This is the only
    #                             explicit "I want to re-do existing work"
    #                             signal; it bypasses the on-remote-branch
    #                             safety lock too (the user owns it).
    # ---------------------------------------------------------------

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

    # Auto-recovery loop. Each iteration runs the full cherry-pick sequence
    # for whatever ``unit.prs`` currently contains. On a clean finish or a
    # plain conflict (no missing-prereq signal) the loop exits. On a
    # missing-prereq report we either label-and-stop (detection-only mode)
    # or prepend the discovered prereq(s) and restart with the expanded
    # unit. ``max_prereq_depth`` and the cycle check bound the loop.
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
        # In append mode ``cherry_pick_base`` is the existing branch tip
        # (the commits we keep); otherwise it's the rebase base. Each
        # iteration of the auto-prereq loop resets to the same point so
        # we never duplicate or lose the kept-prefix.
        create_branch_from_ref(repo_path, new_branch, cherry_pick_base)

        outcome = _attempt_cherry_picks(
            config, repo_path, unit, new_branch, base_branch, ai_active,
            origin_slug,
        )

        if outcome.kind == "success":
            # --- All PRs cherry-picked cleanly (possibly via AI) ---
            # Synthesise the combined-port CHANGELOG entry now (groups only,
            # ai_changelog enabled). Done after the cherry-pick succeeded so
            # we don't burn tokens on units that ended up in conflict —
            # those already produced a draft PR with the per-PR fallback
            # wording, and a successful retry will hit this same path.
            _maybe_synthesize_changelog(config, unit, base_branch)
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
            # Every PR in the unit was already in target — port branch
            # ended up empty. Mark feature ``skipped`` with reason and
            # drop the empty branch instead of pushing / opening a PR.
            _handle_already_in_target(
                config, repo_path, state, unit, new_branch, base_ref,
                outcome.already_in_target_urls, onto, pr_meta,
            )
            return "continue"

        if outcome.kind == "missing_prereqs":
            # Either label-and-stop (detection-only) or dive deeper.
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

            # --- Dive: prepend discovered prereq(s) and restart ---
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

            # Persist the in-progress trail so a Ctrl-C / crash mid-dive
            # leaves a paper trail in state for the next `releasy continue`.
            _persist_dive_progress(
                config, state, unit, new_branch, onto, pr_meta,
                fs_dynamic_prereq_urls=fs_dynamic_prereq_urls,
                fs_prereq_trail=fs_prereq_trail,
                prereq_discovery_depth=prereq_discovery_depth,
            )
            continue  # restart the loop with the expanded unit

        # outcome.kind == "unresolved"
        # Plain unresolved conflict — flag for manual review and stop.
        _handle_unresolved_conflict(
            config, repo_path, state, unit, new_branch, base_branch,
            base_ref, onto, outcome.failed_idx, outcome.failed_pr,
            outcome.conflict_files, pr_meta,
            ai_attempted=ai_active,
            dynamic_prereq_urls=fs_dynamic_prereq_urls,
            prereq_trail=fs_prereq_trail,
            prereq_discovery_depth=prereq_discovery_depth,
        )
        return "continue"


@dataclass
class _CherryPickOutcome:
    """Outcome of a single full ``_attempt_cherry_picks`` pass over a unit.

    ``kind`` is one of:
      * ``"success"`` — every PR in ``unit.prs`` was cherry-picked cleanly
        (possibly via AI). ``already_in_target_urls`` lists any PRs that
        git reported as "now empty" mid-loop (already in target, ``--skip``'d
        and not part of the resulting commit count) — informational only.
      * ``"already_applied"`` — every PR in the unit was already in target,
        the port branch ended up empty. ``already_in_target_urls`` lists
        all of them. The caller drops the empty branch and marks the
        feature ``skipped`` with a reason rather than opening a PR.
      * ``"unresolved"`` — a conflict on PR ``failed_idx`` could not be
        resolved by the AI (or AI is disabled). ``failed_pr`` and
        ``conflict_files`` are populated.
      * ``"missing_prereqs"`` — the AI identified the conflict as caused
        by an unported prerequisite. ``missing_prereq_prs`` and
        ``missing_prereq_note`` carry Claude's report; ``failed_pr``
        is the source PR that triggered the conflict.
    """
    kind: str
    failed_idx: int = 0
    failed_pr: PRInfo | None = None
    conflict_files: list[str] = field(default_factory=list)
    missing_prereq_prs: list[str] = field(default_factory=list)
    missing_prereq_note: str | None = None
    already_in_target_urls: list[str] = field(default_factory=list)


def _attempt_cherry_picks(
    config: Config,
    repo_path: Path,
    unit: FeatureUnit,
    new_branch: str,
    base_branch: str,
    ai_active: bool,
    origin_slug: str | None,
) -> _CherryPickOutcome:
    """Cherry-pick every PR in ``unit.prs`` into the current branch.

    Stops on the first conflict and returns the appropriate outcome
    (``unresolved`` or ``missing_prereqs``). Returns ``success`` only
    when every PR landed cleanly.

    PRs whose URL is in ``unit.applied_pr_urls`` are skipped — this is
    the ``if_exists: append`` path, where the existing port branch
    already has those commits applied from a prior run.
    """
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

        # Capture the port-branch tip BEFORE the cherry-pick so we can
        # roll back cleanly on AI failure (and use it as a known-good
        # reset target in split-commit mode where one or two commits
        # may need to come off).
        head_before = run_git(
            ["rev-parse", "--verify", "HEAD"], repo_path, check=False,
        )
        start_sha = head_before.stdout.strip() if head_before.returncode == 0 else None

        cp_result = _cherry_pick_pr(repo_path, config, pr)

        if cp_result.success:
            _tag_commit_with_source_pr(repo_path, unit, pr, origin_slug)
            real_pick_count += 1
            continue

        # Already-in-target: git reported "previous cherry-pick is now
        # empty" — the patch is reachable from target via some other path
        # (sibling backport, prior port, squash). cherry_pick_sha already
        # ran ``--skip`` to drop it, so the working tree is clean and we
        # can move on. No AI call, no conflict markers.
        if cp_result.already_applied:
            console.print(
                f"    [dim]↳ {ref} already in target — skipped "
                "(empty cherry-pick)[/dim]"
            )
            already_in_target.append(pr.url)
            continue

        # --- Conflict path on this PR ---
        msg = f"Conflict on {ref}!"
        if cp_result.error_message and not cp_result.conflict_files:
            msg = f"{msg} ({cp_result.error_message})"
        console.print(f"    [red]✗[/red] {msg}")
        for cf in cp_result.conflict_files:
            console.print(f"      [red]•[/red] {cf}")

        # Split-commit mode: before handing control to Claude, conclude
        # the in-progress cherry-pick by committing the conflict markers
        # as a stand-alone "with conflicts" commit. The AI then makes a
        # second commit on top with the resolution, so the port branch's
        # `git log` shows the conflict and its fix as separate diffs.
        # Only triggered when both AI is active (otherwise there's no
        # second commit to make and the legacy unresolved-conflict path
        # below is the right behaviour) and the config knob is on.
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
                # Failed to pre-commit (very unusual — empty stage,
                # missing MERGE_MSG). Fall back to the legacy in-progress
                # cherry-pick flow so the pipeline doesn't get stuck.
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
        )

    # Loop completed without hitting a real conflict. If every PR landed
    # as a no-op (empty cherry-pick → ``--skip``'d), the port branch is
    # empty: signal this so the caller can mark the feature ``skipped``
    # with reason rather than push and open a PR with zero commits.
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
    """Decide whether to dive into auto-recovery on a missing-prereq report.

    Returns ``(should_dive, exit_reason)``. ``exit_reason`` is a dict
    that always carries a ``"reason"`` key. When ``should_dive`` is True,
    it also has ``"dive_urls"``: the URLs to actually port (after
    queued-elsewhere / depth / cycle / ancestor pre-flight have winnowed
    the list). When False, the reason explains what stopped us:

      * ``"detection_only"`` — auto-recovery is disabled in config
      * ``"queued_elsewhere"`` — at least one prereq is already known to
        releasy in another unit / config entry; ``"queued"`` lists them
      * ``"cycle"`` — a discovered prereq is already in the unit's PR list
      * ``"depth_exhausted"`` — bumping depth would exceed
        ``max_prereq_depth``
      * ``"all_already_in_base"`` — every discovered prereq is already
        merged into ``base_branch`` per the local ancestor pre-flight
    """
    auto_cfg = config.ai_resolve.auto_add_prerequisite_prs

    if not auto_cfg.enabled:
        return False, {"reason": "detection_only"}

    # 1) Queued-elsewhere check (cheapest, most informative).
    queued = _find_already_queued_prereqs(
        config, state, outcome.missing_prereq_prs,
        exclude_feature_id=unit.feature_id,
    )
    if queued:
        return False, {"reason": "queued_elsewhere", "queued": queued}

    # 2) Cycle: any prereq already in the unit's PR list (original
    # members or already-prepended dynamic ones)?
    unit_urls = {pr.url for pr in unit.prs}
    unit_urls.update(fs_dynamic_prereq_urls)
    cycle_hits = [u for u in outcome.missing_prereq_prs if u in unit_urls]
    if cycle_hits:
        return False, {"reason": "cycle", "cycle_urls": cycle_hits}

    # 3) Depth cap.
    if prereq_discovery_depth >= auto_cfg.max_prereq_depth:
        return False, {
            "reason": "depth_exhausted",
            "depth": prereq_discovery_depth,
            "max_depth": auto_cfg.max_prereq_depth,
        }

    # 4) Made it through the gates — return the candidates as
    # ``dive_urls``. The actual ancestor pre-flight needs ``PRInfo``
    # (we need ``merge_commit_sha``), so it happens in the caller after
    # the fetch step. Returning the raw URLs here keeps this function
    # cheap and pure (no GitHub fetches).
    return True, {"reason": "ok", "dive_urls": list(outcome.missing_prereq_prs)}


def _success_status(rebase_pr_url: str | None) -> str:
    """Status for a port branch that finished cleanly (no conflicts).

    ``needs_review`` once a PR exists for the rebase branch — that's the
    "ready for human review" state. ``branch_created`` when the branch is
    around but no PR has been opened yet (e.g. ``pr_policy.auto_pr:
    false``, or PR creation hit a transient failure). The latter shows up
    on the project board with a branch link and a GitHub *compare* URL so
    the user can open the PR manually with one click.
    """
    return "needs_review" if rebase_pr_url else "branch_created"


def _ensure_pr_for_existing_remote_branch(
    config: Config,
    state: PipelineState,
    unit: FeatureUnit,
    new_branch: str,
    base_branch: str,
) -> None:
    """For a unit whose branch is already on origin: open (or find) the PR
    and update state / labels accordingly.

    Triggered on re-runs when the cherry-pick is skipped because the branch
    is already pushed (typical case: a prior run that ran with
    ``pr_policy.auto_pr: false`` and only pushed the branch). Idempotent:
    if a PR is already on file, this just makes sure the labels and project
    board reflect it.

    No-op unless ``config.push`` is enabled. When ``pr_policy.auto_pr``
    is off, the helper still ensures a state entry exists for the branch
    (status ``branch_created``) so the project board can show it with a
    compare-URL link.
    """
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

    # Heal stale "needs human attention" state on already-pushed PRs.
    # This catches the case where a previous retry succeeded at the
    # cherry-pick / push step but didn't run the recovery treatment
    # (e.g. ran with older code, or `_reconcile_recovered_pr` hadn't
    # been added yet). Without this, re-running `releasy run` would
    # skip the cherry-pick (branch is on remote) and never touch the
    # body / labels / draft state — leaving the PR perpetually stuck
    # with the "needs intervention" banner. We detect the stuck-ness
    # by reading the live PR's labels: if `ai-needs-attention` is
    # still attached, the PR clearly wasn't reconciled and we treat
    # this re-run as a deferred recovery (force-update body + title,
    # remove the label, add `ai-resolved`, flip draft → ready).
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
        # Branch on remote but no state entry — record a minimal one so the
        # project board picks it up. Without prior context we can't tell
        # whether AI was involved; ``ai_resolved`` stays at its default
        # (False).
        fs = FeatureState(
            status=_success_status(pr_url), branch_name=new_branch,
            mode=unit.mode,
            **_unit_pr_meta(unit),
        )
        state.features[unit.feature_id] = fs
    if pr_url:
        fs.rebase_pr_url = pr_url
        fs.status = "needs_review"
        _apply_releasy_label_to_pr(config, pr_url)
        _apply_session_labels_to_pr(config, pr_url)
        if fs.ai_resolved:
            _apply_ai_label_to_pr(config, pr_url)
        if needs_recovery:
            relabelled = _reconcile_recovered_pr(config, pr_url)
            if relabelled and not fs.ai_resolved:
                fs.ai_resolved = True
    _persist_state(config, state)


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
    """Push and (optionally) open a single combined PR for the unit.

    If any PR in the unit was AI-resolved, the resulting PR is tagged with
    ``ai_resolve.label`` and the feature state is marked accordingly.

    ``was_failed_prev`` is set by ``_process_feature_unit`` when this
    unit's previous run ended in ``conflict`` status (and we just retried
    it). When true, the existing PR is treated as stale: title + body
    are force-rewritten regardless of ``update_existing_prs``, the
    ``ai-needs-attention`` label (if any) is removed, and a draft PR is
    flipped to ready-for-review — so reviewers don't see a "needs
    intervention" banner on a port that is now actually clean.

    ``dynamic_prereq_urls`` / ``prereq_trail`` / ``prereq_discovery_depth``
    carry the auto-recovery bookkeeping. When non-empty they:
      * persist on the FeatureState so the project board card and
        re-runs surface the trail,
      * tag the merged PR with ``ai_resolve.auto_prereq_label``.
    """
    ai_used = unit.ai_resolved_count > 0
    has_auto_prereqs = bool(dynamic_prereq_urls)
    verify_flagged = unit.verify_needs_attention

    if config.push:
        _push(config, repo_path, new_branch)
        console.print(f"    [green]✓[/green] Pushed [cyan]{new_branch}[/cyan]")
    else:
        console.print("    [dim]Skipping push[/dim]")

    # Provisional status — refined below once we know if a PR got opened.
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

    # In append mode (PRs were added to an already-ported group) the body
    # is stale by definition — it lists fewer PRs than the unit now
    # carries. Force the rewrite even when ``update_existing_prs: false``
    # so reviewers see the full declared group, not the prior subset.
    appended_to_existing = bool(unit.applied_pr_urls)

    dropped_items = _collect_dropped_trailers(repo_path, onto, new_branch)

    if config.push and config.pr_policy.auto_pr:
        # Title format is identical regardless of AI involvement; the
        # `ai-resolved` label (applied below) is what marks it.
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
            _apply_session_labels_to_pr(config, rebase_pr_url)
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
                # The previous failed run flagged this PR for human
                # attention — mirror the first-run-resolved appearance
                # so reviewers can't tell it apart: status `ai_resolved`
                # in state, and the `ai-resolved` label on the PR.
                if relabelled and not state.features[unit.feature_id].ai_resolved:
                    state.features[unit.feature_id].ai_resolved = True
    elif config.push and (ai_used or has_auto_prereqs or verify_flagged):
        # Branch pushed but pr_policy.auto_pr disabled — try to label any
        # pre-existing PR for this branch.
        existing = find_pr_for_branch(config, new_branch, base_branch)
        if existing:
            _apply_releasy_label_to_pr(
                config, existing.url, pr_number=existing.number,
            )
            _apply_session_labels_to_pr(
                config, existing.url, pr_number=existing.number,
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
    """Bring a previously-conflicted PR back into a clean reviewable shape.

    Called after a successful retry of a unit whose previous run had
    landed in ``conflict`` status. Side effects:

      * remove the ``ai_resolve.needs_attention_label`` (no-op if the
        label was never attached — e.g. the partial-group draft path
        wasn't taken),
      * apply the ``ai_resolve.label`` (``ai-resolved``) so the
        recovered PR is visually indistinguishable from one that
        landed cleanly with AI on its very first run — the user
        explicitly asked for this to keep dashboards consistent,
      * mark the PR ready-for-review (no-op if it isn't draft).

    Returns ``True`` when the PR carried the needs-attention label
    (and thus genuinely went through the AI-failure path) so the
    caller can promote ``FeatureState.ai_resolved`` to ``True``;
    returns ``False`` when the label wasn't there (nothing to mirror).

    All GitHub calls are best-effort: failures are logged but never
    raised, so a transient GitHub blip can't undo an otherwise-
    successful retry.
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
        # Mirror first-run-resolved appearance: tag with the
        # ``ai-resolved`` label so dashboards / filters that key off
        # it can't tell a recovered PR apart from one that cleared on
        # its very first attempt.
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
    """Create a PR for ``branch`` or reuse / update an existing one.

    Behaviour:
      - If GitHub already has an open PR from ``branch`` → ``base_branch``:
          * with ``update_existing_prs: true`` in config OR
            ``force_update=True``, edit its title and body to match what
            releasy would have set, then return ``(url, "updated")``.
          * otherwise return ``(url, "reused")`` without touching the PR.
      - If no matching PR is open, create a new one and return
        ``(url, "created")``. On creation failure returns ``(None, "failed")``.

    ``force_update`` is the per-call override the pipeline uses when it
    *knows* the existing PR is stale (e.g. carries a "needs intervention"
    banner from a previous failed run that we just successfully retried) —
    in that case we always rewrite title + body, regardless of the global
    ``update_existing_prs`` switch, because leaving misleading copy on a
    PR that's now actually clean would be worse than the configured
    "leave PRs alone" default.
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
    """Best-effort: tag the PR with the ``releasy`` label.

    This is the replacement for the old ``[releasy]`` text prefix in the
    title — the label conveys the same identification, while keeping the
    title clean (``"Antalya 26.3: <subject>"``).
    """
    if pr_number is None:
        pr_number = _pr_number_from_url(pr_url)
    if pr_number is None:
        return
    add_label_to_pr(config, pr_number, RELEASY_LABEL)


def _session_pr_labels(config: Config) -> list[str]:
    """Session-defined labels to apply to every rebase PR (may be empty)."""
    if config.session is None:
        return []
    return list(config.session.pr_labels)


def _apply_session_labels_to_pr(
    config: Config, pr_url: str, pr_number: int | None = None,
) -> None:
    """Best-effort: attach every ``session.pr_labels`` label to the PR.

    Idempotent — ``add_label_to_pr`` is a no-op for labels the PR
    already carries.
    """
    labels = _session_pr_labels(config)
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
    pr_refs: list[tuple[str, int]],
) -> list[tuple[str, list[str]]]:
    """Ensure every PR in ``pr_refs`` carries every session pr_label.

    ``pr_refs`` is ``[(pr_url, pr_number), …]``. Reads each PR's current
    labels via ``fetch_pr_by_url`` (one GET per PR) and only adds the
    missing ones — so a fully-labelled set costs N GETs and zero writes.
    Returns ``[(pr_url, [added_labels])]`` for every PR where at least
    one label was added — so the caller can render per-PR detail.
    """
    from releasy.github_ops import fetch_pr_by_url

    labels = _session_pr_labels(config)
    result: list[tuple[str, list[str]]] = []
    if not labels or not pr_refs:
        return result
    for pr_url, pr_number in pr_refs:
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


def _apply_missing_prereqs_label_to_pr(
    config: Config, pr_url: str, pr_number: int | None = None,
) -> None:
    """Best-effort: tag a draft / placeholder PR with the
    ``missing_prereqs_label``.

    Called whenever a unit's conflict was identified as caused by a
    missing prerequisite (detection-only mode), an exhausted
    auto-recovery dive, or a prereq that's already queued elsewhere.
    """
    if pr_number is None:
        pr_number = _pr_number_from_url(pr_url)
    if pr_number is None:
        return
    add_label_to_pr(config, pr_number, config.ai_resolve.missing_prereqs_label)


def _apply_auto_prereq_label_to_pr(
    config: Config, pr_url: str, pr_number: int | None = None,
) -> None:
    """Best-effort: tag a successfully merged-up combined PR with the
    ``auto_prereq_label`` so reviewers know the PR's scope was expanded
    by auto-recovery (one or more prereq PRs were prepended).
    """
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
    """Post verifier findings as a top-level PR comment. Returns True on
    successful post — caller persists that to suppress duplicates."""
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
    """Register the configured upstream remote on the local clone.

    Idempotent: a no-op when ``config.upstream`` is ``None`` or when the
    alias already points at the configured URL. Called lazily, just before
    invoking the AI resolver, so users who never enable AI never pay the
    cost (and never have a stray remote sitting in their clone).
    """
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
    """For each URL in ``candidate_urls``, find where releasy already
    tracks it (config or state), if anywhere.

    Returns a list of dicts, one per matching candidate, in input order
    and with no duplicates. Each dict has::

        {
            "prereq_url": "<the candidate URL>",
            "queued_in": "<feature_id | 'config:include_prs' | 'config:groups[<id>]'>",
            "queued_in_pr_url": "<rebase PR URL or None>",
        }

    Empty list when no candidate is already queued — the caller falls
    through to the auto-recovery dive (or detection-only labelling).

    ``exclude_feature_id`` skips state entries with that id, used so we
    don't flag a unit's own prior dynamic prereqs (which we already
    know about) as "queued elsewhere".

    URL normalisation: candidates are matched against config / state by
    their parsed ``(owner, repo, number)`` tuple, so query strings,
    trailing slashes, and ``http`` vs ``https`` differences don't cause
    false negatives.
    """
    def _normalize(url: str) -> tuple[str, str, int] | None:
        return parse_pr_url(url)

    # Build the lookup table from config and state.
    # value = (queued_in_label, queued_in_pr_url_or_None)
    index: dict[tuple[str, str, int], tuple[str, str | None]] = {}

    for url in config.pr_sources.include_prs:
        ref = _normalize(url)
        if ref and ref not in index:
            index[ref] = ("config:include_prs", None)

    for group in config.pr_sources.groups:
        for url in group.prs:
            ref = _normalize(url)
            if ref and ref not in index:
                index[ref] = (f"config:groups[{group.id}]", None)

    for fid, fs in state.features.items():
        if fid == exclude_feature_id:
            continue
        # All PRs being processed by this feature, original + dynamic.
        for url in (fs.pr_urls or ([fs.pr_url] if fs.pr_url else [])):
            if not url:
                continue
            ref = _normalize(url)
            if ref and ref not in index:
                index[ref] = (fid, fs.rebase_pr_url)
        for url in fs.dynamic_prereq_urls:
            ref = _normalize(url)
            if ref and ref not in index:
                index[ref] = (fid, fs.rebase_pr_url)

    out: list[dict] = []
    seen: set[tuple[str, str, int]] = set()
    for url in candidate_urls:
        ref = _normalize(url)
        if ref is None or ref in seen:
            continue
        if ref in index:
            queued_in, queued_pr_url = index[ref]
            out.append({
                "prereq_url": url,
                "queued_in": queued_in,
                "queued_in_pr_url": queued_pr_url,
            })
            seen.add(ref)
    return out


def _is_prereq_already_in_base(
    repo_path: Path, base_branch: str, pr: PRInfo,
) -> bool:
    """Pre-flight: is ``pr`` already merged into the local ``base_branch``?

    Returns True when ``pr.merge_commit_sha`` is set AND is reachable from
    ``base_branch`` per ``git merge-base --is-ancestor``. Returns False
    in every other case (no merge SHA, ancestor check failed / errored,
    not reachable) — falsing-out is the safe default because dive logic
    will then proceed and any double-application will surface as an
    empty cherry-pick downstream.
    """
    if not pr.merge_commit_sha:
        return False
    answer = is_ancestor(repo_path, pr.merge_commit_sha, base_branch)
    return answer is True


def _fetch_prereq_prs(
    config: Config, urls: list[str],
) -> tuple[list[PRInfo], list[str]]:
    """Fetch ``PRInfo`` for each prereq URL.

    Returns ``(fetched, failed)``: ``fetched`` is the list of successful
    fetches in input order, ``failed`` is the list of URLs that couldn't
    be resolved (parse error, GitHub fetch failed, etc.). Callers treat
    a non-empty ``failed`` list as a soft failure of the dive — log it
    and fall through to the detection-only path so the user can sort
    out the unfetchable URLs manually.
    """
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
    """Persist the unit's in-progress dive state.

    Called between dives so a Ctrl-C / crash mid-recovery leaves a
    paper trail that ``releasy continue`` can read. Status stays at
    ``branch_created`` (a non-terminal "we're working on it" marker)
    until the loop exits with success or a final failure outcome.
    """
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
    prereq_discovery_depth: int,
    exit_reason: dict,
    auto_cfg,
    triggering_pr: PRInfo | None,
    final_discovered: list[str],
) -> None:
    """Pretty-print the auto-recovery dependency trail to the console.

    Shared by every "dive aborted" path (depth exhausted, cycle, prereq
    already queued elsewhere, fetch failed). Layout matches the project
    board card body so the user sees the same trail in both places.
    """
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
            console.print(
                f"      • {url} (queued in {where}{extra})"
            )
        console.print(
            "    [dim]Action: wait for the queued unit's PR to merge, "
            "then re-run releasy.[/dim]"
        )
    elif reason == "fetch_failed":
        for url in exit_reason.get("failed_urls", []):
            console.print(f"      • could not fetch {url}")


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
    """Roll back the unit, persist the prereq trail, label, and report.

    Shared exit path for every "we know what's missing but we are not
    going to dive" outcome:
      * detection-only mode (auto-recovery disabled)
      * prereq queued elsewhere
      * depth exhausted
      * cycle detected
      * dive's prereq fetch failed
      * every dive candidate is already in base_branch (rare; surfaces
        when Claude misidentified a prereq we *just* merged)

    Always:
      * aborts any in-progress git op
      * resets the port branch state (drops local branch when no
        successful picks were committed; keeps partial-group commits
        otherwise — same rule as ``_handle_unresolved_conflict``)
      * persists the prereq trail + ``missing_prereq_prs`` /
        ``missing_prereq_note`` on FeatureState
      * applies the ``missing-prerequisites`` label to any opened PR
      * emits the dependency trail to stdout
    """
    auto_cfg = config.ai_resolve.auto_add_prerequisite_prs

    # First: clean up any in-progress git op so the working tree is
    # safe to operate on.
    if is_operation_in_progress(repo_path):
        run_git(["cherry-pick", "--abort"], repo_path, check=False)
        run_git(["merge", "--abort"], repo_path, check=False)
        run_git(["rebase", "--abort"], repo_path, check=False)

    final_discovered = list(outcome.missing_prereq_prs)
    exhausted = exit_reason.get("reason") in (
        "depth_exhausted", "cycle", "fetch_failed", "all_already_in_base",
    )

    _print_prereq_dive_failure(
        fs_prereq_trail, prereq_discovery_depth, exit_reason,
        auto_cfg, outcome.failed_pr, final_discovered,
    )
    if exit_reason.get("reason") == "queued_elsewhere":
        # Print the prereq cross-references in the standard "queued"
        # variant of the trail printer. (Already done above in
        # ``_print_prereq_dive_failure``; leave a marker so the next
        # step doesn't rewrite the line.)
        pass

    # Discard any local branch we built up. Failed at idx 0 means no
    # commit landed; idx > 0 means partial-group commits exist. Drop
    # everything either way — when auto-recovery is in play we don't
    # publish a half-built branch with confusing prereqs.
    if local_branch_exists(repo_path, new_branch):
        run_git(["checkout", "--detach", base_ref], repo_path, check=False)
        run_git(["branch", "-D", new_branch], repo_path, check=False)
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
        **pr_meta,
    )
    state.features[unit.feature_id] = fs
    _persist_state(config, state)
    # Sync above already labelled the project card body. There is no
    # rebase PR to label here (we dropped the branch and didn't push) —
    # the missing-prereqs label only attaches to PRs in the partial-
    # group draft path inside ``_handle_unresolved_conflict``, which is
    # not the auto-recovery roll-back path.


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
    """Cleanup for a unit whose every PR was already in target.

    The port branch was created off ``base_ref`` and ended up empty after
    every cherry-pick was ``--skip``'d as "now empty". Drop the empty
    branch and record the feature as ``skipped`` with a reason — so
    ``releasy status`` makes clear no port was needed and ``releasy
    continue`` won't try to re-pick it.
    """
    origin_slug = get_origin_repo_slug(config)

    # Defensive: if anything is somehow still in-progress (shouldn't be,
    # since cherry_pick_sha already --skip'd), wind it down.
    if is_operation_in_progress(repo_path):
        run_git(["cherry-pick", "--abort"], repo_path, check=False)
        run_git(["merge", "--abort"], repo_path, check=False)
        run_git(["rebase", "--abort"], repo_path, check=False)

    if local_branch_exists(repo_path, new_branch):
        run_git(["checkout", "--detach", base_ref], repo_path, check=False)
        run_git(["branch", "-D", new_branch], repo_path, check=False)

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
    dynamic_prereq_urls: list[str] | None = None,
    prereq_trail: list[dict] | None = None,
    prereq_discovery_depth: int = 0,
) -> None:
    """Centralised cleanup for an unresolved cherry-pick conflict.

    Drops in two flavours, depending on whether earlier picks in the unit
    landed cleanly:

    * ``idx == 0`` (singleton, or the very first pick of a group): the
      branch has no commits worth keeping — abort the in-progress git op,
      detach from the branch, delete it locally, and record a
      ``conflict`` state entry with no PR / no push.

    * ``idx > 0`` (a partial group): the prior ``idx`` picks are valid
      commits — abort the current pick, push the branch as-is, open a
      DRAFT PR labelled ``ai-needs-attention`` with a banner explaining
      what failed, and record a ``conflict`` state entry pointing at the
      new draft PR. Remaining PRs in the group are NOT attempted.

    Either way the state is persisted (and synced to the GitHub Project
    when ``push`` is enabled) before returning, so the caller can simply
    move on to the next unit.
    """
    origin_slug = get_origin_repo_slug(config)
    ref = pr_ref_label(failed_pr.repo_slug, failed_pr.number, origin_slug)

    # 1. Make sure no git op is mid-flight. Idempotent: if the resolver
    # already aborted/reset (the AI path does this), these are no-ops.
    if is_operation_in_progress(repo_path):
        run_git(["cherry-pick", "--abort"], repo_path, check=False)
        run_git(["merge", "--abort"], repo_path, check=False)
        run_git(["rebase", "--abort"], repo_path, check=False)

    why = (
        "AI resolver gave up" if ai_attempted
        else "AI resolver disabled"
    )

    if idx == 0:
        # Nothing to keep — drop the branch entirely.
        if local_branch_exists(repo_path, new_branch):
            run_git(["checkout", "--detach", base_ref], repo_path, check=False)
            run_git(["branch", "-D", new_branch], repo_path, check=False)
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
            **pr_meta,
        )
        _persist_state(config, state)
        return

    # Partial group: keep the n-1 successful commits, push, draft PR.
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
        )
        # On a retry of a previously-failed unit a draft PR may already
        # exist for this branch — `create_pull_request` would 422 in that
        # case. Look it up first and refresh title/body/labels in place;
        # only call `create_pull_request` when no PR is open.
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
                _apply_session_labels_to_pr(config, rebase_pr_url)
                if unit.verify_needs_attention:
                    _apply_verify_label_to_pr(config, rebase_pr_url)
            else:
                console.print(
                    "    [yellow]![/yellow] Could not open draft PR for "
                    f"[cyan]{new_branch}[/cyan] (see warnings above)"
                )

    # Carry the prior verify_comment_posted across the FeatureState
    # rebuild below so re-runs don't stack duplicate comments.
    prior_fs = state.features.get(unit.feature_id)
    verify_comment_posted = bool(
        prior_fs and prior_fs.verify_comment_posted
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
        ai_cost_usd=unit.ai_cost_usd_total,
        verify_needs_attention=unit.verify_needs_attention,
        verify_comment_posted=verify_comment_posted,
        mode=unit.mode,
        dynamic_prereq_urls=list(dynamic_prereq_urls or []),
        prereq_trail=list(prereq_trail or []),
        prereq_discovery_depth=prereq_discovery_depth,
        **pr_meta,
    )
    if rebase_pr_url:
        fs.rebase_pr_url = rebase_pr_url
    state.features[unit.feature_id] = fs
    _persist_state(config, state)


def _combine_user_context(unit: FeatureUnit, pr: PRInfo) -> str:
    """Build the ``user_context`` string for a single cherry-pick step.

    Combines the unit-level ``ai_context`` (set during discovery from
    ``pr_sources.by_labels[].ai_context`` / ``pr_sources.groups[].ai_context``
    / ``pr_sources.include_prs[].ai_context``) with the per-PR override
    for this step's source PR (``pr_sources.groups[].prs[].ai_context``).
    Returns ``""`` when the user supplied no context.
    """
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
    """Invoke Claude to resolve ONE conflicted cherry-pick step in place.

    Step-mode contract: on success Claude has resolved, built, and committed
    locally — the cherry-pick is concluded, the working tree is clean, and
    HEAD has advanced. RelEasy stays in charge of pushing the branch and
    opening the (possibly combined) PR. This contract is the same for
    singletons and for any step inside a sequential group.

    Returns an :class:`_AIStepOutcome`. ``handled`` is True iff the step
    succeeded and the caller should continue with the next pick. When
    Claude reported ``MISSING_PREREQS`` the outcome carries the discovered
    URLs / reason, even though ``handled`` is False — callers branch on
    this to enter the auto-recovery dive (or detection-only labelling)
    instead of routing to :func:`_handle_unresolved_conflict`.

    On non-success the working tree is reset to a clean state at
    ``start_sha`` (handled inside ``attempt_ai_resolve``).
    """
    from releasy.ai_resolve import AIResolveContext, attempt_ai_resolve

    # Lazy: register the upstream remote so Claude's prereq-detection
    # `git fetch` / `git log` queries can resolve it.
    _ensure_upstream_remote(config, repo_path)

    ctx = AIResolveContext(
        port_branch=new_branch,
        base_branch=base_branch,
        source_pr=pr,
        conflict_files=conflict_files,
        operation="cherry-pick",
        user_context=_combine_user_context(unit, pr),
        # Split-commit mode: ``pre_resolve_sha`` is set iff RelEasy already
        # concluded the cherry-pick as a "with conflicts" commit before
        # this call (see ``_attempt_cherry_picks``). The AI prompt and
        # post-condition checks both branch on this. ``start_sha`` is the
        # branch tip BEFORE the cherry-pick so cleanup-on-failure rolls
        # back both the pre-commit and any partial resolution.
        split_mode=pre_resolve_sha is not None,
        pre_resolve_sha=pre_resolve_sha,
        start_sha=start_sha,
        mode=unit.mode,
    )

    result = attempt_ai_resolve(config, repo_path, ctx)

    # Cost is billed even when Claude failed — record it before deciding
    # what to do about the failure.
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

    # needs_attention
    unit.verify_needs_attention = True
    header = f"#{pr.number}"
    if vr.summary:
        unit.verify_findings.append(f"**{header} — {vr.summary}**")
    else:
        unit.verify_findings.append(f"**{header}**")
    for finding in vr.findings:
        unit.verify_findings.append(f"- {finding}")
    # blank-line separator between PR blocks in the eventual PR comment
    unit.verify_findings.append("")

    console.print(
        f"    [yellow]⚠[/yellow] Verifier flagged #{pr.number}: "
        f"{vr.summary or 'see findings'}{cost_note}"
    )
    for finding in vr.findings:
        console.print(f"      [yellow]•[/yellow] {finding}")


@dataclass
class _AIStepOutcome:
    """Result of one ``_try_ai_resolve_step`` call.

    ``handled`` is True iff the AI committed the cherry-pick locally.
    When False, ``missing_prereq_prs`` may be non-empty (Claude reported
    a missing-prereq situation) — callers route on that distinction
    instead of falling straight through to ``_handle_unresolved_conflict``.
    """
    handled: bool
    missing_prereq_prs: list[str] = field(default_factory=list)
    missing_prereq_note: str | None = None


# ---------------------------------------------------------------------------
# Continue / Skip / Abort / Status
# ---------------------------------------------------------------------------


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
    state.features[feat.id].conflict_files = []
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
    """Inspect a port branch and decide whether it has been resolved.

    Returns ``(resolved, reason_if_not)``. Resolved means: branch is
    checked out cleanly, no unmerged files, no in-progress cherry-pick,
    and HEAD has at least one commit beyond ``base_ref``.
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
            f"    [dim]already on origin, not force-pushing[/dim]"
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
    pr_urls = fs.pr_urls or ([fs.pr_url] if fs.pr_url else [])
    refs: list[str] = []
    for url in pr_urls:
        parsed = parse_pr_url(url) if url else None
        if parsed:
            owner, repo, n = parsed
            refs.append(pr_ref_label(f"{owner}/{repo}", n, origin_slug))
    # Fallback if old state lacks pr_urls but does have pr_numbers (assume origin).
    if not refs:
        pr_numbers = fs.pr_numbers or ([fs.pr_number] if fs.pr_number else [])
        refs = [f"#{n}" for n in pr_numbers if n is not None]
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
        # Make sure the `releasy` label is present even on PRs from older
        # runs that predated label-based identification.
        _apply_releasy_label_to_pr(config, fs.rebase_pr_url, pr_number=pr_num)
        _apply_session_labels_to_pr(
            config, fs.rebase_pr_url, pr_number=pr_num,
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
        _apply_session_labels_to_pr(config, pr_url)
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
    """Re-check every feature in state and finish whatever can be finished.

    This is the catch-all "reconcile everything" command. Per feature:

      - ``skipped`` → log and skip.
      - ``conflict`` from an AI-gave-up partial group / dropped singleton
        (any of ``failed_step_index`` / ``partial_pr_count`` /
        ``rebase_pr_url`` set) → highlight; user must act on the draft
        PR or source PR, then re-run.
      - ``conflict``, branch now clean → push, open PR (if ``auto_pr``),
        flip to ``needs_review`` or ``branch_created``.
      - ``conflict``, still unresolved → highlight, leave alone.
      - ``branch_created`` (branch on origin, no PR yet) → try to open
        the PR. Covers the case where the previous run had
        ``pr_policy.auto_pr: false`` and only pushed the branch, or
        where an earlier failure prevented PR creation. Stays as
        ``branch_created`` if PR creation is still disabled / failing.
      - ``needs_review`` already linked to a PR → leave alone.

    Always finishes with a project-board reconciliation pass so the GitHub
    Project reflects the current state (and stale draft stubs get replaced
    by the real PR cards).
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

    # Pick up ports merged externally since the last run; ``continue``
    # is the natural place to reconcile, so apply the merged_label sweep
    # here as well.
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

        # AI-gave-up flavour of conflict (partial group / dropped
        # singleton) — these have an explicit human-action checkpoint
        # (the draft PR or the source PR), so we never auto-flip them
        # below; the user re-runs ``continue`` after the manual fix.
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

        # Already-finished states. ``needs_review`` is terminal (PR exists);
        # ``branch_created`` is the "branch pushed but no PR" case, where
        # ``releasy continue`` will try to open the PR.
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

        # Conflict path needs the branch locally so we can inspect / continue.
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
        # Provisional — flips to needs_review inside _open_pr_for_resolved
        # if a PR is opened (or already exists for the branch).
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
    """Standalone reconciliation: push current local state to the board.

    Loads the per-project state file and calls the same reconciliation
    used at the end of ``releasy continue``, so the user can refresh the
    project board without running the whole pipeline (handy after editing
    state by hand, after rotating tokens, or right after wiring up a new
    project URL on an in-flight rebase).

    Returns False — for non-zero CLI exit — only when the user asked for a
    sync but nothing happened: no project configured, missing token,
    unparseable URL, or sync errors. A clean "already up to date" is
    success.
    """
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
    """Make sure every local port is reflected on the GitHub Project board.

    Per-feature state changes during the run already trigger
    ``sync_project`` from ``_persist_state`` (when ``push`` is
    on). This is the belt-and-braces pass: even with ``push: false``, or
    when the project URL was added to config after some ports were
    already in state, we still want ``releasy continue`` to leave the
    board in sync with what we have locally.

    Output is a single, friendly line — quiet when there's nothing to do,
    informative when there is.
    """
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
    state.features[feat.id].conflict_files = []
    _persist_state(config, state)
    console.print(f"[yellow]⏭[/yellow] Feature [cyan]{feat.id}[/cyan] skipped")
    return True


def abort_run(config: Config) -> None:
    """Abort the current run, leaving all branches as-is."""
    state = load_state(config)
    console.print("[yellow]Aborting current run. All branches left as-is.[/yellow]")
    _persist_state(config, state)


# Statuses that represent purely-local damage (no rebase PR, never merged).
# `clear` only touches features in these states.
_CLEARABLE_STATUSES: tuple[str, ...] = ("conflict", "branch_created")


def _resolve_clear_target(
    config: Config, state: PipelineState, ident: str,
) -> tuple[str, FeatureState] | None:
    """Resolve any of: feature ID, branch name, source-PR number, source-PR URL.

    Returns ``(feature_id, FeatureState)`` for an entry that exists in
    state, or ``None`` if nothing matches. Unlike :func:`_resolve_branch_target`,
    this only succeeds when state actually tracks the feature — `clear`
    has nothing to do for features releasy has never touched.
    """
    feat = _resolve_branch_target(config, state, ident)
    if feat is not None and feat.id in state.features:
        return feat.id, state.features[feat.id]

    if ident.isdigit():
        n = int(ident)
        for fid, fs in state.features.items():
            if fs.pr_number == n or n in fs.pr_numbers:
                return fid, fs

    if parse_pr_url(ident) is not None:
        norm = ident.rstrip("/")
        for fid, fs in state.features.items():
            if fs.pr_url and fs.pr_url.rstrip("/") == norm:
                return fid, fs
            if any(u.rstrip("/") == norm for u in fs.pr_urls):
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
    """Wipe one feature's local artifacts and drop its state entry.

    Caller must have already vetted ``fs.rebase_pr_url`` is empty.
    Mutates ``state.features`` (pops the entry) but does NOT persist —
    callers persist once at the end (so multi-clear is one save).
    """
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
                # Detach so we can delete the branch we're sitting on.
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
    console.print(f"  [green]✓[/green] state entry removed")
    return True


def clear_branch(
    config: Config,
    identifier: str,
    work_dir: Path | None = None,
    dry_run: bool = False,
) -> bool:
    """Clean up local artifacts for one feature that never made it to a PR.

    Refuses to touch a feature whose ``rebase_pr_url`` is set — those are
    user-visible on GitHub and outside the scope of `clear`.
    """
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
    """Clean every feature in a local-only damaged state.

    Selects features where ``rebase_pr_url`` is empty and status is in
    :data:`_CLEARABLE_STATUSES`. Confirms interactively unless
    ``assume_yes`` or ``dry_run``.
    """
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
    """Print the current pipeline state, grouped by status.

    One sub-table per status section (in :data:`STATUS_DISPLAY_ORDER`),
    so the most-attention-needing entries (conflicts) surface at the top.
    """
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
        "superseded": "bright_black",
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
            table.add_row(*row)
        console.print()
        console.print(table)
