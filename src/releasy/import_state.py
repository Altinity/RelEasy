"""``releasy project pull``: rebuild the state file from GitHub PRs and the project board.

Read-only on GitHub; merges into existing local state, keeping local-only fields.
"""

from __future__ import annotations

from releasy.termlog import console

from releasy.config import Config, get_github_token
from releasy.github_ops import (
    ProjectBoardCard,
    fetch_project_board_snapshot,
    find_latest_pr_for_branch,
    get_origin_repo_slug,
    pr_ref_label,
)
from releasy.state import FeatureState, load_state, save_state


# Board status forcing a local ``skipped`` entry (compared case-insensitively).
_BOARD_SKIPPED = "skipped"


def _index_cards(
    cards: list[ProjectBoardCard],
) -> tuple[dict[str, ProjectBoardCard], dict[str, ProjectBoardCard]]:
    """Index board cards as ``(by_pr_url, by_feature_id)``; the latter from DraftIssue titles."""
    by_pr_url: dict[str, ProjectBoardCard] = {}
    by_feature_id: dict[str, ProjectBoardCard] = {}
    for card in cards:
        if card.pr_url:
            by_pr_url[card.pr_url] = card
        if card.draft_title:
            fid = _extract_feature_id_from_draft_title(card.draft_title)
            if fid:
                by_feature_id[fid] = card
    return by_pr_url, by_feature_id


def _extract_feature_id_from_draft_title(title: str) -> str | None:
    """Pull ``<feature_id>`` out of a ``"<branch> (<feature_id>)"`` title, or ``None``."""
    stripped = title.rstrip()
    if not stripped.endswith(")"):
        return None
    open_idx = stripped.rfind("(")
    if open_idx == -1:
        return None
    candidate = stripped[open_idx + 1 : -1].strip()
    return candidate or None


def import_from_github(config: Config) -> bool:
    """Rebuild the state file from PRs + the project board; ``False`` on a missing precondition."""
    if not get_github_token():
        console.print(
            "[red]RELEASY_GITHUB_TOKEN not set.[/red] "
            "`releasy project pull` needs API access to fetch source PRs, "
            "rebase PRs, and the project board."
        )
        return False

    if not config.notifications.github_project:
        console.print(
            "[red]notifications.github_project is not configured.[/red] "
            "The project board is the source of truth for `Skipped` "
            "decisions and `AI Cost` on import. Run "
            "[cyan]releasy setup-project[/cyan] first, then retry."
        )
        return False

    # Deferred to avoid a circular import with pipeline.py.
    from releasy.pipeline import discover_feature_units

    console.print(
        "\n[bold]Discovering source PRs from config...[/bold]"
    )
    units = discover_feature_units(config)
    if not units:
        console.print(
            "\n[yellow]No PRs discovered via pr_sources.[/yellow] "
            "Check `by_labels`, `include_prs`, and `groups` in config.yaml."
        )

    state = load_state(config)
    had_prior_state = bool(state.features)

    base_branch = state.base_branch
    if not base_branch and config.target_branch:
        base_branch = config.target_branch
    if not base_branch:
        console.print(
            "[red]Cannot determine base branch.[/red] Set "
            "[cyan]target_branch[/cyan] in config.yaml (or run "
            "[cyan]releasy run --onto <ref>[/cyan] once to seed state)."
        )
        return False

    state.base_branch = base_branch
    if state.onto is None:
        state.onto = base_branch
    if state.phase == "init" and had_prior_state is False:
        # Imported ports already live on GitHub.
        state.phase = "ports_done"

    console.print(
        f"\n[bold]Reading project board[/bold] "
        f"([cyan]{config.notifications.github_project}[/cyan])..."
    )
    cards = fetch_project_board_snapshot(config)
    if cards is None:
        console.print(
            "[red]Could not read the GitHub Project board.[/red] "
            "Check the URL in config, and that RELEASY_GITHUB_TOKEN has "
            "the `project` scope."
        )
        return False
    by_pr_url, by_feature_id = _index_cards(cards)
    console.print(f"  [dim]{len(cards)} card(s) on the board[/dim]")

    origin_slug = get_origin_repo_slug(config)
    ai_label = config.ai_resolve.label

    summary: list[tuple[str, str, str]] = []  # (feature_id, outcome, note)

    console.print(
        f"\n[bold]Reconciling {len(units)} unit(s) against GitHub...[/bold]"
    )
    for unit in units:
        feature_id = unit.feature_id
        branch_name = config.feature_branch_name(feature_id, base_branch)
        primary = unit.primary_pr()
        label = pr_ref_label(primary.repo_slug, primary.number, origin_slug)

        rebase_pr = find_latest_pr_for_branch(
            config, branch_name, base_branch,
        )

        card: ProjectBoardCard | None = None
        if rebase_pr is not None and rebase_pr.url in by_pr_url:
            card = by_pr_url[rebase_pr.url]
        elif feature_id in by_feature_id:
            card = by_feature_id[feature_id]

        board_status_lc = (card.status or "").strip().lower() if card else ""
        is_skipped_on_board = board_status_lc == _BOARD_SKIPPED

        if rebase_pr is None and card is None:
            summary.append(
                (feature_id, "no-pr", f"{label} — no rebase PR, no board card")
            )
            continue

        existing_fs = state.features.get(feature_id)

        status = _derive_status(rebase_pr, is_skipped_on_board)

        new_fs = FeatureState(
            status=status,
            branch_name=branch_name,
            pr_url=primary.url,
            pr_number=primary.number,
            pr_title=primary.title,
            pr_body=primary.body,
            pr_author=primary.author,
            pr_numbers=[pr.number for pr in unit.prs] if unit.is_group else [],
            pr_urls=[pr.url for pr in unit.prs] if unit.is_group else [],
            rebase_pr_url=rebase_pr.url if rebase_pr else None,
        )

        if rebase_pr and ai_label and ai_label in (rebase_pr.labels or []):
            new_fs.ai_resolved = True

        # The board is the source of truth for AI cost.
        if card is not None and card.ai_cost_usd is not None:
            new_fs.ai_cost_usd = card.ai_cost_usd

        if existing_fs is not None:
            _merge_preserving_local(new_fs, existing_fs)

        state.features[feature_id] = new_fs

        kind = "updated" if existing_fs else "added"
        if is_skipped_on_board:
            kind = f"{kind}-skipped"
        note_parts: list[str] = [label]
        if rebase_pr:
            rebase_n = rebase_pr.url.rsplit("/", 1)[-1]
            note_parts.append(f"rebase PR #{rebase_n} [{rebase_pr.state}]")
        if new_fs.ai_resolved:
            note_parts.append("ai-resolved")
        if new_fs.ai_cost_usd is not None:
            note_parts.append(f"${new_fs.ai_cost_usd:.2f}")
        summary.append(
            (feature_id, kind, " — ".join(note_parts))
        )

    save_state(state, config)

    _print_summary(summary)
    console.print(
        f"\n[green]Wrote state to {config.state_path}.[/green]  "
        "Run [cyan]releasy refresh[/cyan] to re-probe merge conflicts, "
        "or [cyan]releasy continue[/cyan] to reconcile the project board."
    )
    return True


def _derive_status(
    rebase_pr: "object | None", is_skipped_on_board: bool,
) -> str:
    """Map (rebase PR state, board status) to a local status; board ``Skipped`` wins."""
    if is_skipped_on_board:
        return "skipped"
    if rebase_pr is None:
        return "branch_created"
    pr_state = getattr(rebase_pr, "state", None)
    if pr_state == "open":
        ms = (getattr(rebase_pr, "mergeable_state", None) or "").lower()
        if ms == "dirty":
            return "conflict"
        return "needs_review"
    if pr_state == "merged":
        return "merged"
    if pr_state == "closed":
        return "closed"
    return "needs_review"


def _merge_preserving_local(
    new_fs: FeatureState, existing: FeatureState,
) -> None:
    """Fold local-only fields from ``existing`` into the GitHub-built ``new_fs`` in place."""
    new_fs.ai_iterations = existing.ai_iterations
    new_fs.failed_step_index = existing.failed_step_index
    new_fs.partial_pr_count = existing.partial_pr_count
    if not new_fs.base_commit and existing.base_commit:
        new_fs.base_commit = existing.base_commit
    if existing.ai_resolved:
        new_fs.ai_resolved = True
    if new_fs.status == "conflict" and existing.conflict_files:
        new_fs.conflict_files = existing.conflict_files


def _print_summary(summary: list[tuple[str, str, str]]) -> None:
    if not summary:
        console.print("  [dim]Nothing to reconcile.[/dim]")
        return
    groups: dict[str, list[tuple[str, str]]] = {}
    for fid, kind, note in summary:
        groups.setdefault(kind, []).append((fid, note))

    order = ["added", "updated", "added-skipped", "updated-skipped", "no-pr"]
    for kind in order + [k for k in groups if k not in order]:
        rows = groups.get(kind)
        if not rows:
            continue
        style = {
            "added": "green",
            "updated": "cyan",
            "added-skipped": "yellow",
            "updated-skipped": "yellow",
            "no-pr": "dim",
        }.get(kind, "white")
        heading = {
            "added": f"Added ({len(rows)})",
            "updated": f"Updated ({len(rows)})",
            "added-skipped": f"Added, marked skipped ({len(rows)})",
            "updated-skipped": f"Updated, marked skipped ({len(rows)})",
            "no-pr": f"No PR / no board card ({len(rows)}) — skipped",
        }.get(kind, f"{kind} ({len(rows)})")
        console.print(f"\n  [{style}]{heading}[/{style}]")
        for fid, note in rows:
            console.print(f"    [cyan]{fid}[/cyan]  [dim]{note}[/dim]")
