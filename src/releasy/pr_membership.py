"""PR membership management: add, remove, list.

PRs live in the session file (``<target_branch>.session.yaml`` by default) under
``pr_sources.include_prs`` (top-level) and ``pr_sources.groups[].prs``
(grouped). State entries (``FeatureState``) live in the project state
file. Every mutation here writes back via :func:`save_session` and
:func:`save_state` so the user never edits YAML by hand.
"""

from __future__ import annotations

from releasy.termlog import console
from rich.table import Table

from releasy.config import Config, save_session
from releasy.github_ops import fetch_pr_by_url, parse_pr_url
from releasy.state import (
    find_feature_by_pr_url,
    load_state,
    save_state,
)


def _require_session(config: Config):
    """Return the live session, or raise — same contract as feature.py."""
    if config.session is None:
        raise RuntimeError(
            "pr subcommands need the session file loaded — this is a "
            "CLI wiring bug."
        )
    return config.session


def _url_in_list(url: str, urls: list[str]) -> bool:
    """``url`` matches any entry in ``urls`` after canonicalisation."""
    target = parse_pr_url(url)
    if target is None:
        return False
    return any(parse_pr_url(u) == target for u in urls)


def _remove_url_from_list(url: str, urls: list[str]) -> bool:
    """Drop ``url`` from ``urls`` (in place) by canonical match. True if removed."""
    target = parse_pr_url(url)
    if target is None:
        return False
    removed = False
    kept: list[str] = []
    for u in urls:
        if parse_pr_url(u) == target:
            removed = True
            continue
        kept.append(u)
    urls[:] = kept
    return removed


def _drop_context(url: str, contexts: dict[str, str]) -> None:
    """Remove any ai_context whose key canonicalises to the same PR as ``url``."""
    target = parse_pr_url(url)
    if target is None:
        return
    for key in list(contexts.keys()):
        if parse_pr_url(key) == target:
            del contexts[key]


def _lookup_context(url: str, contexts: dict[str, str]) -> str:
    """Find ``url``'s ai_context, comparing keys canonically."""
    target = parse_pr_url(url)
    if target is None:
        return ""
    for key, val in contexts.items():
        if parse_pr_url(key) == target:
            return val
    return ""


def add_pr(
    config: Config,
    url: str,
    *,
    group_id: str | None = None,
    context: str | None = None,
) -> bool:
    """Add a PR to the session.

    Top-level (no ``group_id``) → ``pr_sources.include_prs``.
    Grouped → the named group's ``prs``.

    Validates the URL syntax, then fetches the PR via the GitHub API to
    confirm it's reachable (closed PRs accepted). If the URL is already
    in the target list with the same context, returns ``True`` without
    mutating. If it's in ``exclude_prs``, it's removed (re-add overrides
    a prior exclusion).
    """
    session = _require_session(config)

    if parse_pr_url(url) is None:
        console.print(f"[red]Malformed PR URL: {url}[/red]")
        return False

    ps = session.pr_sources

    # Locate target list + context dict.
    if group_id is not None:
        group = next((g for g in ps.groups if g.id == group_id), None)
        if group is None:
            console.print(
                f"[red]Group '{group_id}' not found in session.[/red] "
                f"Existing groups: "
                f"{', '.join(g.id for g in ps.groups) or '(none)'}"
            )
            return False
        target_list = group.prs
        target_ctx = group.pr_ai_contexts
        loc_label = f"group [cyan]{group_id}[/cyan]"
    else:
        target_list = ps.include_prs
        target_ctx = ps.include_pr_contexts
        loc_label = "[cyan]include_prs[/cyan]"

    # A PR belongs in exactly one place: top-level include_prs, OR one
    # group. The session loader warns about cross-list duplicates; at
    # add-time we refuse so the user moves explicitly. They can re-add
    # the same URL into the same group to update its ai_context — that
    # falls through to the idempotency / context-update branch below.
    for g in ps.groups:
        if g.id == group_id:
            continue
        if _url_in_list(url, g.prs):
            console.print(
                f"[red]PR is already in group '{g.id}'.[/red] Remove it "
                f"with `releasy pr remove` first, then re-add."
            )
            return False
    if group_id is not None and _url_in_list(url, ps.include_prs):
        console.print(
            "[red]PR is already in top-level include_prs.[/red] Remove "
            "it with `releasy pr remove` first, then re-add with "
            f"--group {group_id}."
        )
        return False

    pr_info = fetch_pr_by_url(config, url, include_closed=True)
    if pr_info is None:
        console.print(
            f"[red]Could not reach PR {url}[/red] — check the URL, your "
            f"GitHub token (RELEASY_GITHUB_TOKEN), and network."
        )
        return False

    existing_ctx = _lookup_context(url, target_ctx)
    already_present = _url_in_list(url, target_list)
    incoming_ctx = context or ""

    if already_present and existing_ctx == incoming_ctx:
        console.print(
            f"[yellow]PR already present in {loc_label}[/yellow] — no change."
        )
        return True

    # Apply mutations.
    if not already_present:
        target_list.append(url)

    # Refresh ai_context: drop any prior canonical-matched entry, then
    # set the new one if non-empty.
    _drop_context(url, target_ctx)
    if incoming_ctx:
        target_ctx[url] = incoming_ctx

    # Re-adding a previously excluded PR clears the exclusion.
    removed_from_exclude = _remove_url_from_list(url, ps.exclude_prs)

    save_session(session)

    note = (
        f" (updated ai_context)"
        if already_present
        else f" — {pr_info.repo_slug}#{pr_info.number}: {pr_info.title}"
    )
    console.print(
        f"[green]✓[/green] Added [cyan]{url}[/cyan] to {loc_label}{note}"
    )
    if removed_from_exclude:
        console.print(
            "[dim]  (removed from exclude_prs — re-add overrides prior "
            "exclusion)[/dim]"
        )
    return True


def remove_pr(
    config: Config,
    url: str,
    *,
    keep_discovery: bool = False,
) -> bool:
    """Remove a PR from session + state.

    Drops the URL from every session-level location (top-level
    ``include_prs``, every group's ``prs``, both ai_context dicts).
    Unless ``keep_discovery``, appends to ``exclude_prs`` so the next
    refresh's label-driven discovery doesn't re-add it.

    Locates the corresponding ``FeatureState`` and deletes it for
    singleton features. Multi-PR groups are atomic: removing one URL
    from a state-tracked group is refused with a clear message.
    """
    session = _require_session(config)

    if parse_pr_url(url) is None:
        console.print(f"[red]Malformed PR URL: {url}[/red]")
        return False

    ps = session.pr_sources

    # Check state first so we can refuse atomic-group removals before
    # touching session.
    state = load_state(config)
    match = find_feature_by_pr_url(state, url)
    if match is not None:
        fid, fs = match
        if len(fs.pr_urls) > 1:
            console.print(
                f"[red]PR {url} is part of multi-PR group "
                f"feature '{fid}' (which has {len(fs.pr_urls)} PRs).[/red] "
                f"Remove the whole group via the session file, or use "
                f"`releasy clear --branch {fs.branch_name or fid}` to "
                f"purge state for that group."
            )
            return False

    removed_from_top = _remove_url_from_list(url, ps.include_prs)
    _drop_context(url, ps.include_pr_contexts)

    removed_from_groups: list[str] = []
    overlay_groups: list[str] = []
    for g in ps.groups:
        if _remove_url_from_list(url, g.prs):
            # save_session() only writes hand-curated groups; an overlay
            # entry is regenerated by `graph discover`, so say so instead
            # of claiming a durable edit. exclude_prs (below) is what
            # actually keeps the PR out of the group at discovery time.
            (overlay_groups if g.auto_discovered else removed_from_groups).append(g.id)
        _drop_context(url, g.pr_ai_contexts)

    state_purged = False
    if match is not None:
        fid, _fs = match
        del state.features[fid]
        state_purged = True

    appended_to_exclude = False
    if not keep_discovery:
        if not _url_in_list(url, ps.exclude_prs):
            ps.exclude_prs.append(url)
            appended_to_exclude = True

    nothing_to_do = (
        not removed_from_top
        and not removed_from_groups
        and not overlay_groups
        and not state_purged
        and not appended_to_exclude
    )
    if nothing_to_do:
        console.print(
            f"[yellow]PR {url} not found anywhere — nothing to do.[/yellow]"
        )
        return True

    save_session(session)
    if state_purged:
        save_state(state, config)

    console.print(f"[green]✓[/green] Removed [cyan]{url}[/cyan]")
    if removed_from_top:
        console.print("[dim]  - dropped from include_prs[/dim]")
    for gid in removed_from_groups:
        console.print(f"[dim]  - dropped from group '{gid}'[/dim]")
    for gid in overlay_groups:
        console.print(
            f"[dim]  - dropped from deps-overlay group '{gid}' for this run "
            "(exclude_prs keeps it out; `graph discover` regenerates the "
            "overlay)[/dim]"
        )
    if state_purged:
        console.print("[dim]  - purged FeatureState from state file[/dim]")
    if appended_to_exclude:
        console.print("[dim]  - appended to exclude_prs[/dim]")
    elif keep_discovery:
        console.print(
            "[dim]  - kept out of exclude_prs (--keep-discovery)[/dim]"
        )
    return True


def list_prs(config: Config) -> None:
    """Print every PR URL the session references, grouped by location."""
    session = _require_session(config)
    ps = session.pr_sources
    any_printed = False

    if ps.include_prs:
        table = Table(title="include_prs (top-level)")
        table.add_column("URL", style="cyan")
        table.add_column("ai_context", style="dim")
        for url in ps.include_prs:
            table.add_row(url, _lookup_context(url, ps.include_pr_contexts))
        console.print(table)
        any_printed = True

    for g in ps.groups:
        if not g.prs:
            continue
        table = Table(title=f"group: {g.id}")
        table.add_column("URL", style="cyan")
        table.add_column("ai_context", style="dim")
        for url in g.prs:
            table.add_row(url, _lookup_context(url, g.pr_ai_contexts))
        console.print(table)
        any_printed = True

    if ps.exclude_prs:
        table = Table(title="exclude_prs")
        table.add_column("URL", style="yellow")
        for url in ps.exclude_prs:
            table.add_row(url)
        console.print(table)
        any_printed = True

    if not any_printed:
        console.print("[dim]No PRs configured in session.[/dim]")
