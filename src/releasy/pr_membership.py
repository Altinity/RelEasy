"""PR membership management (session ``pr_sources``): add, remove, hold, list."""

from __future__ import annotations

from releasy.termlog import console
from rich.table import Table

from releasy.config import Config, save_session
from releasy.github_ops import (
    fetch_pr_by_url, get_origin_repo_slug, parse_pr_url, pr_ref_label,
)
from releasy.state import (
    find_features_by_pr_url,
    load_state,
    mark_outdated,
    save_state,
)


def _require_session(config: Config):
    if config.session is None:
        raise RuntimeError(
            "pr subcommands need the session file loaded — this is a "
            "CLI wiring bug."
        )
    return config.session


def _url_in_list(url: str, urls: list[str]) -> bool:
    target = parse_pr_url(url)
    if target is None:
        return False
    return any(parse_pr_url(u) == target for u in urls)


def _remove_url_from_list(url: str, urls: list[str]) -> bool:
    """Drop ``url`` from ``urls`` in place by canonical match; True if removed."""
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
    """Add a reachable PR to ``include_prs`` or group ``group_id``; clears a prior exclusion."""
    session = _require_session(config)

    if parse_pr_url(url) is None:
        console.print(f"[red]Malformed PR URL: {url}[/red]")
        return False

    ps = session.pr_sources

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

    # A PR belongs to include_prs or exactly one group.
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

    if not already_present:
        target_list.append(url)

    _drop_context(url, target_ctx)
    if incoming_ctx:
        target_ctx[url] = incoming_ctx

    removed_from_exclude = _remove_url_from_list(url, ps.exclude_prs)

    save_session(session)

    note = (
        " (updated ai_context)"
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
    """Remove a PR from session + state; unless ``keep_discovery``, add it to ``exclude_prs``.

    Singleton state entries are deleted; a group's in-flight port is marked outdated.
    """
    session = _require_session(config)

    if parse_pr_url(url) is None:
        console.print(f"[red]Malformed PR URL: {url}[/red]")
        return False

    ps = session.pr_sources

    state = load_state(config)
    owner, repo, num = parse_pr_url(url)
    ref = pr_ref_label(f"{owner}/{repo}", num, get_origin_repo_slug(config))
    matches: list[str] = []
    outdated_groups: list[str] = []
    for fid, fs in find_features_by_pr_url(state, url):
        if len(fs.pr_urls) <= 1:
            matches.append(fid)
        elif mark_outdated(fs, f"{ref} removed"):
            outdated_groups.append(fid)

    removed_from_top = _remove_url_from_list(url, ps.include_prs)
    _drop_context(url, ps.include_pr_contexts)

    # A veto outranks a hold.
    removed_from_hold = _remove_url_from_list(url, ps.on_hold)
    _drop_context(url, ps.on_hold_reasons)

    removed_from_groups: list[str] = []
    overlay_groups: list[str] = []
    for g in ps.groups:
        if _remove_url_from_list(url, g.prs):
            # save_session() doesn't write overlay groups; `graph discover` regenerates them.
            (overlay_groups if g.auto_discovered else removed_from_groups).append(g.id)
        _drop_context(url, g.pr_ai_contexts)

    for fid in matches:
        del state.features[fid]
    state_purged = bool(matches)

    appended_to_exclude = False
    if not keep_discovery:
        if not _url_in_list(url, ps.exclude_prs):
            ps.exclude_prs.append(url)
            appended_to_exclude = True

    nothing_to_do = (
        not removed_from_top
        and not removed_from_hold
        and not removed_from_groups
        and not overlay_groups
        and not state_purged
        and not outdated_groups
        and not appended_to_exclude
    )
    if nothing_to_do:
        console.print(
            f"[yellow]PR {url} not found anywhere — nothing to do.[/yellow]"
        )
        return True

    save_session(session)
    if state_purged or outdated_groups:
        save_state(state, config)

    console.print(f"[green]✓[/green] Removed [cyan]{url}[/cyan]")
    if removed_from_top:
        console.print("[dim]  - dropped from include_prs[/dim]")
    if removed_from_hold:
        console.print("[dim]  - dropped from on_hold[/dim]")
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
    for fid in outdated_groups:
        console.print(
            f"[dim]  - marked port of '{fid}' outdated (next `releasy run` "
            "re-ports it from scratch)[/dim]"
        )
    if appended_to_exclude:
        console.print("[dim]  - appended to exclude_prs[/dim]")
    elif keep_discovery:
        console.print(
            "[dim]  - kept out of exclude_prs (--keep-discovery)[/dim]"
        )
    return True


def hold_pr(config: Config, url: str, reason: str = "") -> bool:
    """Park a PR in ``pr_sources.on_hold`` (waiting, not vetoed); re-holding updates the reason."""
    session = _require_session(config)

    if parse_pr_url(url) is None:
        console.print(f"[red]Malformed PR URL: {url}[/red]")
        return False

    ps = session.pr_sources
    if _url_in_list(url, ps.exclude_prs):
        console.print(
            f"[red]PR {url} is vetoed in exclude_prs.[/red] A hold is for "
            "PRs still on the list — re-add it with `releasy pr add` first."
        )
        return False

    already = _url_in_list(url, ps.on_hold)
    if already and _lookup_context(url, ps.on_hold_reasons) == reason:
        console.print("[yellow]PR already on hold[/yellow] — no change.")
        return True

    if not already:
        ps.on_hold.append(url)
    _drop_context(url, ps.on_hold_reasons)
    if reason:
        ps.on_hold_reasons[url] = reason

    save_session(session)
    verb = "Updated the hold on" if already else "Put"
    tail = " on hold" if not already else ""
    console.print(
        f"[green]✓[/green] {verb} [cyan]{url}[/cyan]{tail}"
        + (f" — {reason}" if reason else "")
    )
    return True


def unhold_pr(config: Config, url: str) -> bool:
    """Drop a PR from ``pr_sources.on_hold`` — back in work next run."""
    session = _require_session(config)

    if parse_pr_url(url) is None:
        console.print(f"[red]Malformed PR URL: {url}[/red]")
        return False

    ps = session.pr_sources
    if not _remove_url_from_list(url, ps.on_hold):
        console.print(f"[yellow]PR {url} was not on hold — nothing to do.[/yellow]")
        return True
    _drop_context(url, ps.on_hold_reasons)
    save_session(session)
    console.print(
        f"[green]✓[/green] Took [cyan]{url}[/cyan] off hold — it ports on "
        "the next `releasy run`"
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

    if ps.on_hold:
        table = Table(title="on_hold (parked, not vetoed)")
        table.add_column("URL", style="cyan")
        table.add_column("reason", style="dim")
        for url in ps.on_hold:
            table.add_row(url, _lookup_context(url, ps.on_hold_reasons))
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
