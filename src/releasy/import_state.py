"""``releasy project pull`` — rebuild the per-project state file from GitHub.

The per-project state lives under ``$XDG_STATE_HOME/releasy/<name>.state.yaml``
and isn't shared between machines, so a fresh checkout / new teammate /
throwaway CI runner starts with nothing. Everything RelEasy actually
*needs* to keep running (source PRs, rebase PRs, mergeability) is
discoverable from GitHub, and the fields that aren't (``Skipped``
decisions, cumulative ``AI Cost``) live on the configured GitHub
Project board. This module stitches those two reads together into the
local state file.

Scope (deliberately narrow):

1. Read-only: no git checkouts, no clones, no pushes, no new PRs. The
   command relies entirely on the GitHub REST/GraphQL APIs.
2. Requires a GitHub Project to be configured
   (``notifications.github_project``). That's the only place
   ``Skipped`` + ``AI Cost`` are durable — without it we can't honour
   the "board is source of truth for those" contract.
3. Merges into existing local state instead of clobbering it: local
   fields survive unless the board has a newer/authoritative value
   (currently just ``status: skipped`` and ``ai_cost_usd``), and local-
   only signals that don't exist on GitHub at all (``ai_iterations``,
   ``failed_step_index``, ``partial_pr_count``, ``conflict_files``) are
   preserved verbatim.

What the command does NOT try to recover:

* Branches pushed under ``pr_policy.auto_pr: false`` that never had a
  PR opened — we'd have no way to identify them without cloning. Users
  who hit this can re-run ``releasy run`` (idempotent) to reopen them.
* Mid-flight ``failed_step_index`` / ``partial_pr_count`` — these only
  make sense during a live cherry-pick; re-running the pipeline would
  produce a different (and more accurate) failure mode anyway.
"""

from __future__ import annotations

from releasy.termlog import console

from releasy.config import Config, get_github_token
from releasy.github_ops import (
    ProjectBoardCard,
    fetch_project_board_snapshot,
    find_latest_pr_for_branch,
    get_origin_repo_slug,
    parse_pr_url,
    pr_ref_label,
)
from releasy.state import FeatureState, PipelineState, load_state, save_state


# Status on the GitHub Project board that forces a local ``skipped``
# entry. Matches the canonical label provisioned by ``setup_project``
# (see ``STATUS_MAP`` in github_ops.py). Comparisons are
# case-insensitive because humans hand-edit the board labels.
_BOARD_SKIPPED = "skipped"


# ---------------------------------------------------------------------------
# Board card → feature matching
# ---------------------------------------------------------------------------


def _index_cards(
    cards: list[ProjectBoardCard],
) -> tuple[dict[str, ProjectBoardCard], dict[str, ProjectBoardCard]]:
    """Build two lookup tables over the board snapshot.

    Return value is ``(by_pr_url, by_feature_id)``:

    * ``by_pr_url``: keyed by the canonical PR URL attached to the card.
      Populated only for real PR cards (``__typename == PullRequest``).
    * ``by_feature_id``: keyed by the ``<feature_id>`` extracted from a
      DraftIssue title — ``sync_project`` formats draft titles as
      ``"<branch_name> (<feature_id>)"``, so pulling out the parenthesised
      suffix gives us the feature id back. Only DraftIssue cards show up
      here (PR cards never carry a feature-id in their title).

    Matching a feature uses ``by_pr_url`` first (most reliable, since the
    PR URL is stable across re-runs); ``by_feature_id`` catches the
    feature-has-no-PR-yet / feature-has-Skipped-marker-as-draft cases.
    """
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
    """Pull ``<feature_id>`` out of a ``"<branch> (<feature_id>)"`` title.

    Returns ``None`` if the title doesn't match that shape — callers
    fall back to PR-URL matching in that case.
    """
    stripped = title.rstrip()
    if not stripped.endswith(")"):
        return None
    open_idx = stripped.rfind("(")
    if open_idx == -1:
        return None
    candidate = stripped[open_idx + 1 : -1].strip()
    return candidate or None


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------


def import_from_github(config: Config) -> bool:
    """Rebuild the per-project state file by reading back PRs + the project board.

    Returns ``True`` on success. Returns ``False`` (caller propagates to a
    non-zero exit) when a precondition is missing: no token, no project
    board, unresolvable base branch, or the board snapshot itself fails.

    The command is additive / idempotent: running it twice on a clean
    state produces the same file both times, and running it on top of a
    hand-edited state file only changes fields that the remote side
    is authoritative for.
    """
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

    # Reuse the pipeline's discovery so the import exactly mirrors what
    # ``releasy run`` would act on. Late import avoids a circular-import
    # risk with pipeline.py.
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
        # Not a hard error — a legitimately empty board is possible (all
        # work has moved on to a different base). Fall through so we
        # still refresh state from whatever already exists.

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

    # Seed the pipeline header fields so downstream commands
    # (``continue`` / ``refresh``) have what they expect. Only fill in
    # gaps — never overwrite a prior run's ``started_at`` / ``onto``.
    state.base_branch = base_branch
    if state.onto is None:
        state.onto = base_branch
    if state.phase == "init" and had_prior_state is False:
        # First-time import on a repo with no state. Ports all live on
        # GitHub already, so the pipeline is effectively past
        # ports_done; mark it accordingly so ``releasy continue``
        # doesn't treat the imported entries as unstarted.
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

    # Per-unit reconciliation. We accumulate a summary list so the final
    # printout can group outcomes by kind (new / updated / board-skipped
    # / no-pr).
    summary: list[tuple[str, str, str]] = []  # (feature_id, outcome, note)

    console.print(
        f"\n[bold]Reconciling {len(units)} unit(s) against GitHub...[/bold]"
    )
    for unit in units:
        feature_id = unit.feature_id
        branch_name = config.feature_branch_name(feature_id, base_branch)
        primary = unit.primary_pr()
        label = pr_ref_label(primary.repo_slug, primary.number, origin_slug)

        # Look up whatever PR was most recently opened for this port
        # branch. State="all" so a merged rebase PR still registers.
        rebase_pr = find_latest_pr_for_branch(
            config, branch_name, base_branch,
        )

        # Match a board card: PR URL first, feature id fallback. A card
        # keyed by feature id only exists when the card is still a
        # DraftIssue stub (i.e. no PR has been attached yet).
        card: ProjectBoardCard | None = None
        if rebase_pr is not None and rebase_pr.url in by_pr_url:
            card = by_pr_url[rebase_pr.url]
        elif feature_id in by_feature_id:
            card = by_feature_id[feature_id]

        board_status_lc = (card.status or "").strip().lower() if card else ""
        is_skipped_on_board = board_status_lc == _BOARD_SKIPPED

        # Decide what we actually have to record. If the PR was never
        # opened AND the board carries no card for this unit, there's
        # nothing remote to reflect — skip so we don't invent a bogus
        # entry (``releasy run`` will re-discover and port it).
        if rebase_pr is None and card is None:
            summary.append(
                (feature_id, "no-pr", f"{label} — no rebase PR, no board card")
            )
            continue

        existing_fs = state.features.get(feature_id)

        status = _derive_status(rebase_pr, is_skipped_on_board)

        # Build the authoritative FeatureState from what we just learned.
        # Source PR metadata always refreshed from the unit (GitHub is
        # authoritative for titles/bodies/authors).
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

        # ai_resolved: true iff the rebase PR carries the configured
        # ai-resolve label. This is the only GitHub-side signal for it.
        if rebase_pr and ai_label and ai_label in (rebase_pr.labels or []):
            new_fs.ai_resolved = True

        # ai_cost_usd from the board (source of truth by user's
        # decision). Board cards report ``None`` both for "never billed"
        # and for "billed $0.00 then cleared by hand" — we treat both
        # identically and drop the field from local state.
        if card is not None and card.ai_cost_usd is not None:
            new_fs.ai_cost_usd = card.ai_cost_usd

        # Merge with any pre-existing local entry. Fields that GitHub /
        # the board don't know about are kept verbatim; conflict markers
        # we successfully cleared on the remote side (merged PR) get
        # dropped regardless of what the local copy said.
        if existing_fs is not None:
            _merge_preserving_local(new_fs, existing_fs)

        state.features[feature_id] = new_fs

        # Summary kind: distinguish "added" vs "updated" for the end
        # report, so a rerun vs a first-time bootstrap both read
        # meaningfully.
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


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _derive_status(
    rebase_pr: "object | None", is_skipped_on_board: bool,
) -> str:
    """Map (rebase PR state, board status) → local ``FeatureState.status``.

    Precedence (spec'd by user):

    1. Board says ``Skipped`` → always ``skipped``, even if a PR is still
       open. Humans use ``releasy skip`` (or the board directly) to take
       a port out of consideration; that decision should survive an
       import even while the underlying PR is mergeable.
    2. Open rebase PR, ``mergeable_state == "dirty"`` → ``conflict``.
       Every other open state ("clean", "unstable", "blocked", "behind",
       "unknown") maps to ``needs_review`` — if GitHub hasn't computed
       mergeability yet, assume the optimistic path and let ``releasy
       refresh`` re-probe.
    3. Merged rebase PR → ``merged``; closed-unmerged rebase PR →
       ``closed`` (terminal — refresh skips it, run skips it unless
       ``pr_policy.recreate_closed_prs`` opts back in).
    4. No rebase PR at all → ``branch_created`` (covers the
       ``auto_pr: false`` case where the caller reached us via the
       board-card path).
    """
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
    # Unrecognised state: be optimistic and surface for human review.
    return "needs_review"


def _merge_preserving_local(
    new_fs: FeatureState, existing: FeatureState,
) -> None:
    """Fold local-only fields from ``existing`` into ``new_fs`` in place.

    ``new_fs`` is the freshly-built state from GitHub; ``existing`` is
    whatever was already in the local state file. We want to keep the remote
    side authoritative for anything it knows about (status, rebase PR
    URL, source-PR metadata, AI cost) while preserving the bookkeeping
    the remote can't see:

    * ``ai_iterations``: cumulative count of Claude invocations; no
      GitHub mirror.
    * ``failed_step_index`` / ``partial_pr_count``: cherry-pick-time
      bookkeeping; preserved verbatim. Stale markers left over from
      previous runs are acceptable here — the import is additive.
    * ``conflict_files``: only carried over when the remote still
      reports a conflict; otherwise cleared (the PR either merged or
      got resolved out-of-band, so the old file list is misleading).
    * ``base_commit``: informational only; prefer the existing value
      so re-runs don't lose the originally-recorded commit pointer.
    * ``ai_resolved``: sticky once true — a previous run that saw the
      AI label flipped the bit, and GitHub may have since had the label
      removed manually without the original AI involvement becoming
      untrue.

    ``ai_cost_usd`` is NOT merged here: the caller already applied the
    "board wins if populated" rule before calling this helper, so
    ``new_fs.ai_cost_usd`` is exactly what we want.
    """
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
    """Print per-feature import outcomes grouped by kind."""
    if not summary:
        console.print("  [dim]Nothing to reconcile.[/dim]")
        return
    groups: dict[str, list[tuple[str, str]]] = {}
    for fid, kind, note in summary:
        groups.setdefault(kind, []).append((fid, note))

    # Fixed ordering: freshly-added entries first (bootstrap case),
    # then updates, then skipped, then no-pr stragglers.
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
