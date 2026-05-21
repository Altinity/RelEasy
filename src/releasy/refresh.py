"""``releasy refresh`` — keep already-tracked rebase PRs current.

This is a strict maintenance loop. It does **not** create new branches,
discover new source PRs, or open new pull requests. It only walks the
PRs RelEasy is already tracking in its state file and tries to refresh
each one against the latest target branch.

After ``releasy run`` opens a batch of rebase PRs, the target branch
(``origin/<base_branch>``) keeps moving as other work lands. Some PRs
will eventually conflict with the new tip even though they were clean
when first opened. That's what this command exists for:

1. Promote any tracked PR whose rebase PR has merged on GitHub since
   the last run to ``status: merged`` (one cheap GraphQL call per
   tracked unit), and run the configured ``merged_label`` sweep —
   apply the label to the merged port PR, strip it from the source
   PRs the port was cherry-picked from. Idempotent across runs via
   the ``merged_label_applied`` flag.
2. Iterate every still-active tracked PR (entries in ``state.features``
   that have a ``rebase_pr_url`` + ``branch_name`` and aren't already
   ``merged`` / ``skipped``). No discovery, no new entries, no new PRs.
3. Fetch the latest tips of the target branch and the PR branch from
   origin (so we don't operate on a stale local copy).
4. Attempt ``git merge --no-ff origin/<base_branch>`` into the PR branch.
   - Clean merge → leave the PR alone (we only act on conflicts).
   - Conflict → invoke the same AI resolver used for cherry-picks (with
     the merge-flavoured prompt) and, on success, push the resolved
     merge commit to the PR branch (status preserved).
   - AI gives up / disabled → abort the merge, reset the local branch
     back to its original tip, and mark the entry as ``conflict`` in
     state + project board.

The AI resolver invocation is shared with the cherry-pick path
(``ai_resolve.attempt_ai_resolve``) so prompt rendering, claude
subprocess management, postcondition verification, and worktree cleanup
all behave identically; only the prompt template (``merge_prompt_file``)
and the in-progress operation differ.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from releasy.pipeline import OnlyFilter

from releasy.termlog import console

from releasy.ai_resolve import AIResolveContext, attempt_ai_resolve
from releasy.config import (
    Config, PortMode, get_github_token, lookup_pr_ai_context,
)
from releasy.git_ops import (
    fetch_remote,
    get_conflict_files,
    is_operation_in_progress,
    remote_branch_exists,
    run_git,
    stash_and_clean,
)
from releasy.github_ops import (
    PRInfo,
    fetch_pr_by_url,
    get_origin_repo_slug,
    parse_pr_url,
    sync_project,
)
from releasy.state import FeatureState, PipelineState, load_state, save_state


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _persist(config: Config, state: PipelineState) -> None:
    """Mirror ``pipeline._persist_state`` — kept local to avoid a circular
    import (``pipeline`` itself imports nothing from here).

    Always writes the per-project state file. When ``push`` is on we
    also reconcile the GitHub Project board so the project view reflects
    the new ``conflict`` status as soon as we mark it (instead of
    waiting for the next ``releasy continue`` pass).
    """
    if config.dry_run:
        return
    save_state(state, config)
    if config.push:
        sync_project(config, state)


def _synthesise_source_pr(fs: FeatureState) -> PRInfo | None:
    """Build a ``PRInfo`` from cached FeatureState fields.

    Used to feed the AI resolver context without re-fetching from
    GitHub. We have everything the prompt needs (URL, title, body,
    number, repo slug parsed from the URL); the head SHA / merge SHA
    are irrelevant for merge-conflict resolution so we leave them
    blank.
    """
    if not fs.pr_url:
        return None
    parsed = parse_pr_url(fs.pr_url)
    if not parsed:
        return None
    owner, repo, num = parsed
    return PRInfo(
        number=fs.pr_number or num,
        title=fs.pr_title or "",
        body=fs.pr_body or "",
        state="merged",
        merge_commit_sha=None,
        head_sha="",
        url=fs.pr_url,
        repo_slug=f"{owner}/{repo}",
    )


def _refresh_local_branch(
    repo_path: Path, branch: str, remote: str,
) -> str | None:
    """Force-checkout ``branch`` to match ``origin/<branch>``.

    Returns the resulting HEAD SHA, or ``None`` if the checkout failed
    (caller logs and skips the entry). Stashes / cleans any leftover
    state in the worktree first so we never start a merge on top of
    half-resolved files from a previous run.
    """
    stash_and_clean(repo_path)
    co = run_git(
        ["checkout", "-B", branch, f"{remote}/{branch}"],
        repo_path, check=False,
    )
    if co.returncode != 0:
        return None
    head = run_git(["rev-parse", "--verify", "HEAD"], repo_path, check=False)
    if head.returncode != 0:
        return None
    return head.stdout.strip()


def _abort_any_merge(repo_path: Path, fallback_sha: str | None) -> None:
    """Best-effort cleanup after a failed / unwanted merge.

    Aborts whatever git op is in progress, then hard-resets to
    ``fallback_sha`` if provided so we never leave the working tree on
    an unintended merge commit.
    """
    if is_operation_in_progress(repo_path):
        run_git(["merge", "--abort"], repo_path, check=False)
        run_git(["cherry-pick", "--abort"], repo_path, check=False)
        run_git(["rebase", "--abort"], repo_path, check=False)
    if fallback_sha:
        run_git(["reset", "--hard", fallback_sha], repo_path, check=False)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def refresh_tracked_prs(
    config: Config,
    work_dir: Path | None = None,
    resolve_conflicts: bool = True,
    only: OnlyFilter | None = None,
    force_merge: bool = False,
    address_review: bool = False,
    analyze_fails: bool = False,
    no_flaky_check: bool = False,
    post_comment: bool | None = None,
) -> bool:
    """Walk tracked PRs and run the requested maintenance passes.

    Strictly a maintenance / refresh pass: never opens new PRs, never
    creates new branches, never discovers new PR sources. Only
    operates on entries already present in ``state.features``.

    Status sync (merged / superseded / labels) always runs. The three
    branch-mutating passes are opt-in:

      * ``force_merge`` (``--merge-target``) — merge the latest target
        tip into each tracked PR branch, AI-resolve any conflicts, and
        push the resulting merge commit.
      * ``analyze_fails`` (``--analyze-fails``) — bundle each PR's
        failing CI tests per shard and let the AI run the iterative
        fix-build-rerun loop. Skipped per-PR if the merge step left
        that PR in ``conflict``.
      * ``address_review`` (``--address-review``) — fetch trusted
        review feedback on each tracked PR and let the AI append fix
        commits. Skipped per-PR if the merge step left that PR in
        ``conflict`` (the unresolved merge has to be sorted first).

    When more than one is set the phase order inside a single
    invocation is fixed: ``merge-target → analyze-fails →
    address-review``. ``analyze-fails`` reads commit statuses tied to
    the *current* head SHA, so any pass that pushes first would
    invalidate the CI report it needs to consume.

    ``only`` (optional) restricts the walk to a single tracked PR
    (matched by URL — source or rebase) or a single feature / group ID.

    See module docstring for the detailed contract. Returns False when
    one or more PRs ended up in (or stayed in) ``conflict`` status,
    when an address-review run failed, or when an analyze-fails per-PR
    run errored — so the CLI can exit non-zero for shell scripts.
    """
    # Late import to avoid a cycle: pipeline imports nothing from here,
    # but it owns the shared repo-setup helper we want to reuse.
    from releasy.pipeline import (
        _apply_merged_labels,
        _refresh_all_merge_status_from_github,
        _setup_repo,
    )

    state = load_state(config)
    if not state.features:
        console.print(
            "[yellow]No features in state. Run 'releasy run' first.[/yellow]"
        )
        return True

    base_branch = state.base_branch or (
        config.base_branch_name(state.onto or "") if state.onto else None
    )
    if not base_branch:
        console.print(
            "[red]Cannot determine base branch from state.[/red] "
            "Run 'releasy run' first."
        )
        return False

    repo_path = _setup_repo(config, work_dir, base_branch)

    if is_operation_in_progress(repo_path):
        console.print(
            f"\n[red]✗[/red] A git operation is still in progress in "
            f"[cyan]{repo_path}[/cyan]."
        )
        console.print(
            "  Finish (`git merge --continue`) or abort it first, then re-run."
        )
        return False

    remote = config.origin.remote_name
    base_ref = f"{remote}/{base_branch}"

    if not remote_branch_exists(repo_path, base_branch, remote):
        console.print(
            f"\n[red]✗[/red] Base branch [cyan]{base_branch}[/cyan] not "
            f"found on [cyan]{remote}[/cyan] (already fetched). "
            "Cannot merge."
        )
        return False

    ai_active = resolve_conflicts and config.ai_resolve.enabled
    if force_merge:
        if ai_active:
            console.print(
                f"[dim]AI conflict resolver: enabled "
                f"(command='{config.ai_resolve.command}', "
                f"prompt='{config.ai_resolve.merge_prompt_file}', "
                f"max_iterations={config.ai_resolve.max_iterations})[/dim]"
            )
        else:
            why = (
                "disabled via --no-resolve-conflicts"
                if not resolve_conflicts
                else "disabled in config"
            )
            console.print(f"[dim]AI conflict resolver: {why}[/dim]")

    if config.dry_run:
        console.print(
            "\n[bold magenta]DRY RUN[/bold magenta]: no state, repo, or "
            "GitHub writes will happen. Output shows intended actions only."
        )

    # Catch ports merged externally since the previous run, then apply
    # the configured merged_label (if any) — same post-merge bookkeeping
    # the main pipeline does. Refresh is the natural home for it: this
    # command runs on a schedule, while ``releasy run`` only runs when
    # the user wants to port more work.
    _refresh_all_merge_status_from_github(config, state)
    from releasy.pipeline import _refresh_all_superseded_status_from_github
    _refresh_all_superseded_status_from_github(
        config, state, repo_path, base_branch,
    )
    _apply_merged_labels(config, state)
    _persist(config, state)

    if force_merge:
        console.print(
            f"\n[bold]Phase:[/bold] Merging [cyan]{base_ref}[/cyan] "
            f"into tracked PR branches"
        )

    candidates: list[tuple[str, FeatureState]] = []
    closed_count = 0
    superseded_count = 0
    for fid, fs in state.features.items():
        if fs.status in ("skipped", "merged"):
            continue
        if fs.status == "closed":
            # Out of scope: rebase PR was closed without merging. The
            # pre-loop ``_refresh_all_merge_status_from_github`` call
            # already promoted this entry; don't touch it on this or
            # any future refresh. See state.BranchStatus docstring.
            closed_count += 1
            continue
        if fs.status == "superseded":
            # Out of scope: another PR already cherry-picks the source.
            # Promoted by the supersede sweep — no further refresh work.
            superseded_count += 1
            continue
        if not fs.branch_name or not fs.rebase_pr_url:
            continue
        candidates.append((fid, fs))

    if closed_count:
        console.print(
            f"  [dim]Skipping {closed_count} entry/ies whose rebase PR "
            "was closed without merging.[/dim]"
        )
    if superseded_count:
        console.print(
            f"  [dim]Skipping {superseded_count} entry/ies superseded by "
            "another PR targeting the same base.[/dim]"
        )

    if only is not None:
        before = len(candidates)
        candidates = [(fid, fs) for fid, fs in candidates if only.matches_state(fid, fs)]
        console.print(
            f"  [dim]--only={only.label}: kept "
            f"{len(candidates)}/{before} tracked PR(s)[/dim]"
        )
        if not candidates:
            console.print(
                f"\n[red]✗[/red] --only={only.label!r} matched no tracked "
                "PRs. Check the URL / group id and re-run."
            )
            return False

    if not candidates:
        console.print(
            "  [dim]No tracked PRs with a branch + rebase PR URL — nothing "
            "to do.[/dim]"
        )
        return True

    # Session labels: add any missing ones to in-scope rebase PRs.
    # Cheap N-GET pass; only writes when a PR is short a label.
    from releasy.pipeline import (
        _pr_number_from_url, _session_pr_labels,
        reconcile_session_labels_on_prs,
    )
    session_labels = _session_pr_labels(config)
    if session_labels:
        console.print(
            f"\n[bold]Reconciling session labels[/bold] "
            f"([cyan]{', '.join(session_labels)}[/cyan]) "
            f"across {len(candidates)} tracked PR(s)…"
        )
        pr_refs: list[tuple[str, int]] = []
        for _fid, fs in candidates:
            if not fs.rebase_pr_url:
                continue
            num = _pr_number_from_url(fs.rebase_pr_url)
            if num is not None:
                pr_refs.append((fs.rebase_pr_url, num))
        added_per_pr = reconcile_session_labels_on_prs(config, pr_refs)
        if added_per_pr:
            for pr_url, labels in added_per_pr:
                console.print(
                    f"  [green]+[/green] [link={pr_url}]{pr_url}[/link] — "
                    f"added: [cyan]{', '.join(labels)}[/cyan]"
                )
            total = sum(len(ls) for _, ls in added_per_pr)
            console.print(
                f"  [green]✓[/green] {total} label(s) added across "
                f"{len(added_per_pr)} PR(s)"
            )
        else:
            console.print(
                "  [dim]all tracked PRs already carry every session "
                "label — nothing to do[/dim]"
            )

    any_unresolved = False
    in_conflict: set[str] = set()
    if force_merge:
        for fid, fs in candidates:
            outcome = _process_one(
                config, repo_path, state, fid, fs, base_branch, base_ref,
                remote, ai_active, force_merge=force_merge,
            )
            if outcome == "conflict":
                any_unresolved = True
                in_conflict.add(fid)
            # _process_one already persisted state/board on every
            # meaningful transition; nothing to do here.

        console.print(
            f"\n[bold]Merge pass complete.[/bold] "
            f"{len(candidates)} tracked PR(s) inspected."
        )
        if any_unresolved:
            console.print(
                "[yellow]Some PRs are still in conflict — see above. "
                "Resolve them on GitHub (or locally + push), then "
                "re-run.[/yellow]"
            )

    any_analyze_failed = False
    if analyze_fails:
        from releasy.analyze_fails import print_summary, run_analyze_fails_pass

        af_pr_urls = [
            fs.rebase_pr_url for fid, fs in candidates
            if fid not in in_conflict and fs.rebase_pr_url
        ]
        skipped = len(candidates) - len(af_pr_urls)
        console.print(
            f"\n[bold]Phase:[/bold] Analyzing failed CI on "
            f"{len(af_pr_urls)} tracked PR(s)"
            + (
                f" [dim]({skipped} skipped — left in conflict by the "
                "merge phase)[/dim]"
                if skipped else ""
            )
        )
        if af_pr_urls:
            runs, _flaky_map, _flaky_warnings, cost_attributed_any = (
                run_analyze_fails_pass(
                    config, state, repo_path, af_pr_urls,
                    push=not config.dry_run,
                    dry_run=config.dry_run,
                    no_flaky_check=no_flaky_check,
                    post_comment=post_comment,
                )
            )
            print_summary(runs)
            if any(r.error is not None for r in runs):
                any_analyze_failed = True
            if cost_attributed_any:
                _persist(config, state)

    any_address_failed = False
    if address_review:
        console.print(
            f"\n[bold]Phase:[/bold] Addressing review feedback "
            f"on {len(candidates)} tracked PR(s)"
        )
        for fid, fs in candidates:
            if fid in in_conflict:
                # Don't ask the AI to act on top of unresolved conflicts;
                # the head ref isn't in a coherent state for it.
                console.print(
                    f"\n  [dim]Skipping {fs.rebase_pr_url} — merge "
                    "ended in conflict; resolve that first.[/dim]"
                )
                continue
            ok = _address_review_one(config, fs, work_dir)
            if not ok:
                any_address_failed = True

    return not (
        any_unresolved or any_address_failed or any_analyze_failed
    )


# ---------------------------------------------------------------------------
# Per-PR worker
# ---------------------------------------------------------------------------


@dataclass
class MergeResolveOutcome:
    """Structured result of one merge-into-PR-branch attempt.

    Decoupled from :class:`FeatureState` so the same core helper can be
    driven by ``releasy refresh`` (state-tracked PRs) and
    ``releasy resolve-conflicts`` (URL-driven, possibly stateless).
    Callers translate ``status`` + cost / iteration counters into
    whatever bookkeeping they own.

    ``ai_used`` distinguishes a clean ``--merge-target`` push (no AI
    involvement) from a conflict the AI resolver fixed: both end up as
    ``status="resolved"``, but only the latter should bump
    ``ai_resolved`` / ``ai_iterations`` on the FeatureState.

    .. note::
        Calling this helper at all implies the caller has opted in to
        merging (e.g. via ``releasy refresh --merge-target`` or the
        URL-driven path). Refresh no longer attempts a merge by
        default — bare ``refresh`` is status-sync only. ``force_merge``
        controls whether a *clean* merge still produces a push (it
        does, since the user explicitly asked for the merge).
    """
    status: str  # "clean" | "resolved" | "conflict" | "skipped"
    conflict_files: list[str] = field(default_factory=list)
    ai_iterations: int | None = None
    ai_cost_usd: float | None = None
    pushed: bool = False
    error: str | None = None
    ai_used: bool = False


def run_merge_resolve(
    config: Config,
    repo_path: Path,
    *,
    head_branch: str,
    base_branch: str,
    source_pr: PRInfo,
    rebase_pr_url: str | None,
    ai_active: bool,
    remote: str | None = None,
    force_merge: bool = False,
    feature_mode: PortMode | None = None,
) -> MergeResolveOutcome:
    """Drive merge + (optional) AI resolve for ONE PR branch.

    Pure git/AI/push: never touches the state file. Caller decides how
    to record the outcome (FeatureState updates, GitHub project sync,
    nothing at all in the stateless case).

    Preconditions:
      * ``repo_path`` is a usable clone with origin already fetched.
      * ``head_branch`` (the PR's head ref) and ``base_branch`` exist
        on ``remote``.

    Postconditions:
      * On ``"resolved"``: merge commit pushed to origin/<head_branch>.
      * On ``"clean"``: working tree reset to head_branch's original
        tip — we deliberately don't push clean merges (use the
        ``force_merge`` flag if you want a fresh merge commit even
        without conflicts).
      * On ``"conflict"`` / ``"skipped"``: working tree reset to the
        original tip; nothing pushed.

    ``force_merge`` (the ``--merge-target`` flag) flips the clean-merge
    path to "push the merge commit anyway" — useful when you want every
    PR branch to ingest the latest target tip even though it would
    cherry-pick cleanly. AI resolution still kicks in for actual
    conflicts.
    """
    if remote is None:
        remote = config.origin.remote_name
    base_ref = f"{remote}/{base_branch}"

    if not remote_branch_exists(repo_path, head_branch, remote):
        console.print(
            f"    [yellow]−[/yellow] branch [cyan]{head_branch}[/cyan] "
            f"missing on [cyan]{remote}[/cyan], skipping"
        )
        return MergeResolveOutcome(
            status="skipped",
            error=f"branch {head_branch!r} missing on {remote}",
        )

    if config.dry_run:
        tail = (
            " (always push --merge-target)"
            if force_merge
            else " (push only if conflicts AI-resolved)"
        )
        console.print(
            f"    [magenta]dry-run:[/magenta] would merge "
            f"[cyan]{base_ref}[/cyan] into [cyan]{head_branch}[/cyan]{tail}"
        )
        return MergeResolveOutcome(status="clean")

    start_sha = _refresh_local_branch(repo_path, head_branch, remote)
    if start_sha is None:
        console.print(
            f"    [yellow]−[/yellow] could not check out "
            f"[cyan]{head_branch}[/cyan], skipping"
        )
        return MergeResolveOutcome(
            status="skipped",
            error=f"could not check out {head_branch!r}",
        )

    # Attempt the merge. ``--no-ff`` mirrors GitHub's "Update branch"
    # button — we want a real merge commit so the PR explicitly records
    # the conflict-resolution decision in its history.
    merge_msg = f"Merge {base_ref} into {head_branch}"
    merge = run_git(
        ["merge", "--no-ff", "--no-edit", "-m", merge_msg, base_ref],
        repo_path, check=False,
    )

    if merge.returncode == 0:
        # Clean merge or already up-to-date.
        new_sha = run_git(
            ["rev-parse", "--verify", "HEAD"], repo_path, check=False,
        )
        new_head = new_sha.stdout.strip() if new_sha.returncode == 0 else start_sha
        if new_head != start_sha:
            if force_merge:
                push = run_git(
                    ["push", remote, head_branch], repo_path, check=False,
                )
                if push.returncode != 0:
                    console.print(
                        "    [yellow]![/yellow] clean merge produced "
                        "locally but push failed (origin moved? auth?). "
                        "Re-run to retry."
                    )
                    for line in (push.stderr or "").strip().splitlines()[:3]:
                        console.print(f"      [dim]{line}[/dim]")
                    run_git(
                        ["reset", "--hard", start_sha], repo_path, check=False,
                    )
                    return MergeResolveOutcome(
                        status="skipped",
                        error=f"push failed: {(push.stderr or '').strip()}",
                    )
                console.print(
                    f"    [green]✓[/green] clean merge pushed "
                    f"[cyan]{head_branch}[/cyan] [dim](--merge-target)[/dim]"
                )
                return MergeResolveOutcome(status="resolved", pushed=True)
            console.print(
                "    [dim]clean merge — no conflicts, leaving the PR "
                "untouched (pass --merge-target to push a fresh merge "
                "commit anyway)[/dim]"
            )
            run_git(["reset", "--hard", start_sha], repo_path, check=False)
        else:
            console.print(
                "    [dim]already up-to-date with "
                f"[cyan]{base_ref}[/cyan][/dim]"
            )
        return MergeResolveOutcome(status="clean")

    conflict_files = get_conflict_files(repo_path)
    if not conflict_files:
        # Merge failed for some other reason — abort and warn so the
        # user can investigate manually instead of silently flipping the
        # PR to "conflict" with no useful info.
        msg = (merge.stderr or "").strip().splitlines()[:3]
        _abort_any_merge(repo_path, start_sha)
        console.print(
            "    [yellow]![/yellow] merge failed without producing "
            "conflict markers — leaving PR alone."
        )
        for line in msg:
            console.print(f"      [dim]{line}[/dim]")
        return MergeResolveOutcome(
            status="skipped",
            error=(merge.stderr or "merge failed without conflict markers").strip(),
        )

    console.print(
        f"    [red]✗[/red] {len(conflict_files)} conflicted file(s) "
        f"after merging [cyan]{base_ref}[/cyan]:"
    )
    for cf in conflict_files:
        console.print(f"      [red]•[/red] {cf}")

    if not ai_active:
        _abort_any_merge(repo_path, start_sha)
        return MergeResolveOutcome(
            status="conflict", conflict_files=conflict_files,
            error="AI resolver disabled",
        )

    # Honour the unit-build-time mode when persisted; fall back to the
    # ladder for entries from before FeatureState.mode existed.
    from releasy.pipeline import _detect_port_mode
    if feature_mode in ("backport", "forward_port"):
        resolved_mode = feature_mode
    else:
        resolved_mode = _detect_port_mode(
            config, source_pr, explicit_mode="auto",
        )
    ctx = AIResolveContext(
        port_branch=head_branch,
        base_branch=base_branch,
        source_pr=source_pr,
        conflict_files=conflict_files,
        start_sha=start_sha,
        operation="merge",
        rebase_pr_url=rebase_pr_url,
        user_context=lookup_pr_ai_context(
            config.pr_sources, source_pr.url,
        ),
        mode=resolved_mode,
    )

    result = attempt_ai_resolve(config, repo_path, ctx)

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
        # ``attempt_ai_resolve`` already aborted + reset on failure.
        return MergeResolveOutcome(
            status="conflict",
            conflict_files=conflict_files,
            ai_iterations=result.iterations,
            ai_cost_usd=result.cost_usd,
            error=f"AI resolve failed: {reason}",
        )

    # Push the merge commit. Plain (non-force) push is correct: local
    # is start_sha + merge commit, origin is at start_sha — fast-forward.
    # ``force_push`` would risk clobbering any new commit the PR author
    # themselves pushed between our fetch and our push.
    push = run_git(
        ["push", remote, head_branch], repo_path, check=False,
    )
    if push.returncode != 0:
        console.print(
            f"    [yellow]![/yellow] merge resolved locally but push "
            f"failed (origin moved? auth?). Leaving local commit; "
            f"re-run to retry."
        )
        for line in (push.stderr or "").strip().splitlines()[:3]:
            console.print(f"      [dim]{line}[/dim]")
        return MergeResolveOutcome(
            status="skipped",
            ai_iterations=result.iterations,
            ai_cost_usd=result.cost_usd,
            error=f"push failed: {(push.stderr or '').strip()}",
        )

    iters = (
        f" (iterations: {result.iterations})" if result.iterations else ""
    )
    cost = (
        f" [dim](cost: ${result.cost_usd:.4f})[/dim]"
        if result.cost_usd is not None else ""
    )
    console.print(
        f"    [green]✓[/green] AI resolved + pushed "
        f"[cyan]{head_branch}[/cyan]{iters}{cost}"
    )
    return MergeResolveOutcome(
        status="resolved",
        ai_iterations=result.iterations,
        ai_cost_usd=result.cost_usd,
        pushed=True,
        ai_used=True,
    )


def _process_one(
    config: Config,
    repo_path: Path,
    state: PipelineState,
    fid: str,
    fs: FeatureState,
    base_branch: str,
    base_ref: str,
    remote: str,
    ai_active: bool,
    *,
    force_merge: bool = False,
) -> str:
    """Drive merge + (optional) AI resolve for ONE state-tracked PR.

    Thin wrapper over :func:`run_merge_resolve` that translates the
    structured outcome into FeatureState updates + project-board sync.
    """
    branch = fs.branch_name
    assert branch is not None  # caller filtered

    label = (
        f"PR #{fs.pr_number}" if fs.pr_number else fid
    )
    rebase_label = (
        fs.rebase_pr_url.rsplit("/", 1)[-1]
        if fs.rebase_pr_url else "?"
    )
    console.print(
        f"\n  [cyan]{branch}[/cyan]  "
        f"[dim](source {label} → rebase PR #{rebase_label})[/dim]"
    )

    source_pr = _synthesise_source_pr(fs)
    if source_pr is None:
        # Defensive — we only iterate entries with ``pr_url`` set, but
        # without source PR context the prompt has nothing to ground
        # the resolution in.
        console.print(
            "    [yellow]![/yellow] no source PR metadata — cannot "
            "build resolver prompt, marking conflict."
        )
        _record_conflict(config, state, fs, [])
        return "conflict"

    outcome = run_merge_resolve(
        config, repo_path,
        head_branch=branch,
        base_branch=base_branch,
        source_pr=source_pr,
        rebase_pr_url=fs.rebase_pr_url,
        ai_active=ai_active,
        remote=remote,
        force_merge=force_merge,
        feature_mode=fs.mode,
    )

    # Cost is billed even when the resolve failed — accumulate before
    # branching on outcome so the project board reflects what we spent.
    if outcome.ai_cost_usd is not None:
        prior = fs.ai_cost_usd or 0.0
        fs.ai_cost_usd = prior + outcome.ai_cost_usd

    if outcome.status == "clean":
        # If the entry was previously marked conflict but now merges
        # cleanly (someone else fixed it), surface that by clearing the
        # conflict markers on the local entry. Status itself is left
        # alone — GitHub will reflect mergeable state on its own.
        if fs.status == "conflict" and fs.conflict_files:
            fs.conflict_files = []
            _persist(config, state)
        return "clean"

    if outcome.status == "skipped":
        # Cost may still have been incurred (push race after AI resolve);
        # persist if so, leave status untouched otherwise.
        if outcome.ai_cost_usd is not None:
            _persist(config, state)
        return "skipped"

    if outcome.status == "conflict":
        _record_conflict(config, state, fs, outcome.conflict_files)
        return "conflict"

    # outcome.status == "resolved"
    fs.conflict_files = []
    if fs.status == "conflict":
        fs.status = "needs_review"
        # Clear the cherry-pick-failure markers if they were left over
        # from an earlier ``releasy run`` that punted into conflict
        # state — once the PR is mergeable they no longer apply.
        fs.failed_step_index = None
        fs.partial_pr_count = None
    if outcome.ai_used:
        fs.ai_resolved = True
        if outcome.ai_iterations:
            prior = fs.ai_iterations or 0
            fs.ai_iterations = prior + outcome.ai_iterations
    _persist(config, state)
    return "resolved"


def _record_conflict(
    config: Config,
    state: PipelineState,
    fs: FeatureState,
    conflict_files: list[str],
) -> None:
    """Flip a tracked PR to ``conflict`` and persist.

    Leaves ``branch_name`` / ``rebase_pr_url`` intact — the PR and its
    branch still exist on origin; only the *mergeability* changed.
    Doesn't touch ``failed_step_index`` / ``partial_pr_count`` either:
    those describe cherry-pick-time failures, not merge-time ones, and
    overwriting them would lose history.
    """
    fs.status = "conflict"
    fs.conflict_files = conflict_files
    _persist(config, state)


# ---------------------------------------------------------------------------
# ``--address-review`` per-PR worker
# ---------------------------------------------------------------------------


def _address_review_one(
    config: Config, fs: FeatureState, work_dir: Path | None,
) -> bool:
    """Address review on a single tracked PR.

    Thin wrapper around :func:`releasy.review_response.address_review`:
    decides whether to invoke it (skip merged / closed / superseded /
    untracked entries), prints a per-PR banner, and translates the
    result into a bool the multi-PR loop can aggregate.
    """
    if not fs.rebase_pr_url:
        return True
    # No early bail on empty trusted_reviewers: trust now also flows
    # from author_association (OWNER / MEMBER / COLLABORATOR by
    # default). address_review() itself refuses if BOTH gates are
    # empty, so the user still gets a clear error when they actively
    # disable both.
    return _address_review_for_pr_url(config, fs.rebase_pr_url, work_dir)


def _address_review_for_pr_url(
    config: Config, pr_url: str, work_dir: Path | None,
) -> bool:
    """Drive :func:`address_review` for one PR, printing a banner first.

    Shared by the tracked-state loop and the URL-driven entry point so
    both surface the same per-PR header and failure handling. Returns
    True on success (including "nothing to address") and False on any
    error — caller turns that into an exit code.
    """
    from releasy.review_response import address_review

    console.print(
        f"\n  [bold]address-review[/bold] [cyan]{pr_url}[/cyan]"
    )
    result = address_review(
        config,
        pr_url,
        work_dir=work_dir,
        dry_run=getattr(config, "dry_run", False),
    )
    if not result.success:
        if result.error:
            console.print(f"    [red]✗[/red] {result.error}")
        else:
            console.print(
                "    [red]✗[/red] address-review failed (see above)"
            )
        return False
    return True


def _analyze_fails_for_pr_url(
    config: Config, pr_url: str, repo_path: Path,
    *, no_flaky_check: bool, post_comment: bool | None,
) -> bool:
    """Run the analyze-fails pass against a single PR URL.

    Mirrors :func:`_address_review_for_pr_url` for the URL-driven
    refresh path. Reuses the already-prepared ``repo_path`` so we don't
    re-clone / re-fetch, and folds any incurred Anthropic cost into the
    matching tracked feature's ``ai_cost_usd`` when state exists.

    Returns True on success (including "no failing CI to act on") and
    False when the per-PR run errored — caller turns that into an
    exit code.
    """
    from releasy.analyze_fails import (
        flaky_scan_extra_for,
        print_summary,
        run_analyze_fails_pass,
    )

    state: PipelineState | None = None
    if not getattr(config, "stateless", False):
        try:
            state = load_state(config)
        except Exception as exc:
            console.print(
                f"    [yellow]![/yellow] state file unreadable ({exc}); "
                "running analyze-fails without flaky-elsewhere assessment"
            )
            state = None

    console.print(
        f"\n  [bold]analyze-fails[/bold] [cyan]{pr_url}[/cyan]"
    )
    runs, _flaky_map, _flaky_warnings, cost_attributed_any = (
        run_analyze_fails_pass(
            config, state, repo_path, [pr_url],
            push=not getattr(config, "dry_run", False),
            dry_run=getattr(config, "dry_run", False),
            no_flaky_check=no_flaky_check,
            post_comment=post_comment,
            flaky_scan_extra=flaky_scan_extra_for(state, [pr_url]),
        )
    )
    print_summary(runs)

    if state is not None and cost_attributed_any:
        _persist(config, state)

    return all(r.error is None for r in runs)


# ---------------------------------------------------------------------------
# URL-driven entry point (``releasy refresh --pr <url>``)
# ---------------------------------------------------------------------------


# Match a "Cherry-picked from #N" / "Cherry-picked from owner/repo#N" /
# full GitHub PR URL near the top of a rebase PR body. RelEasy's own
# pipeline writes one of these on every rebase PR it opens (see
# ``pipeline._build_pr_body``), so for PRs we ourselves created we can
# recover the source PR without an extra CLI flag.
_SOURCE_PR_URL_RE = re.compile(
    r"https://github\.com/[^/\s]+/[^/\s]+?(?:\.git)?/pull/\d+\b",
)
_SOURCE_PR_SLUG_RE = re.compile(
    r"\b([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)#(\d+)\b",
)
_SOURCE_PR_HASH_RE = re.compile(r"(?<![\w/])#(\d+)\b")


def _find_source_pr_url(rebase_pr_body: str, rebase_slug: str) -> str | None:
    """Best-effort source-PR-URL extraction from a rebase PR's body.

    RelEasy's pipeline writes the source PR reference near the top of
    every rebase PR body (``Cherry-picked from <ref>``). We search the
    whole body since the marker may shift slightly between versions or
    when ``update_existing_prs`` rewrites it.

    Returns the source PR's URL, or ``None`` when no recognisable
    reference was found — caller falls back to using the rebase PR as
    its own "source" for prompt-rendering purposes.
    """
    if not rebase_pr_body:
        return None

    m = _SOURCE_PR_URL_RE.search(rebase_pr_body)
    if m:
        return m.group(0)

    m2 = _SOURCE_PR_SLUG_RE.search(rebase_pr_body)
    if m2:
        return f"https://github.com/{m2.group(1)}/pull/{m2.group(2)}"

    m3 = _SOURCE_PR_HASH_RE.search(rebase_pr_body)
    if m3 and rebase_slug:
        return f"https://github.com/{rebase_slug}/pull/{m3.group(1)}"

    return None


def _resolve_source_pr(
    config: Config, rebase_pr: PRInfo,
) -> PRInfo:
    """Resolve the upstream source PR a rebase PR ports.

    For RelEasy-created rebase PRs the body includes a ``Cherry-picked
    from <ref>`` marker — we follow that to fetch the real source PR's
    metadata so the merge prompt has accurate ``source_pr_*``
    placeholders. When the marker is missing or unfetchable, we fall
    back to the rebase PR itself: the prompt loses some specificity
    but still has *something* to ground the resolution in.
    """
    source_url = _find_source_pr_url(rebase_pr.body or "", rebase_pr.repo_slug)
    if source_url and source_url != rebase_pr.url:
        fetched = fetch_pr_by_url(config, source_url, include_closed=True)
        if fetched is not None:
            return fetched
        console.print(
            f"    [dim]source PR {source_url} referenced in rebase PR "
            "body but couldn't be fetched — falling back to rebase PR "
            "metadata for prompt context.[/dim]"
        )
    return rebase_pr


def _fetch_pr_refs(
    pr_url: str,
) -> tuple[str, str, str, str, int] | None:
    """Look up the PR's head branch / head repo / base branch / head sha.

    Returns ``(head_ref, head_repo_slug, base_ref, head_sha, number)``
    or ``None`` if the lookup fails. Mirrors
    :func:`releasy.review_response._fetch_pr_head` — kept local to avoid
    a cross-module dependency for one helper.
    """
    token = get_github_token()
    if not token:
        return None
    parsed = parse_pr_url(pr_url)
    if parsed is None:
        return None
    owner, repo, number = parsed
    try:
        from github import Github

        gh = Github(token)
        ghrepo = gh.get_repo(f"{owner}/{repo}")
        pr = ghrepo.get_pull(number)
        head_repo = None
        if pr.head.repo is not None:
            head_repo = pr.head.repo.full_name
        return (
            pr.head.ref,
            head_repo or f"{owner}/{repo}",
            pr.base.ref,
            pr.head.sha,
            pr.number,
        )
    except Exception:  # pragma: no cover — network / permissions
        return None


def resolve_conflicts_for_pr(
    config: Config,
    pr_url: str,
    work_dir: Path | None = None,
    *,
    resolve_conflicts: bool = True,
    force_merge: bool = False,
    address_review: bool = False,
    analyze_fails: bool = False,
    no_flaky_check: bool = False,
    post_comment: bool | None = None,
) -> bool:
    """Drive ``releasy refresh --pr <url>`` end-to-end.

    Looks up the PR's head/base refs from GitHub, sets up the work
    repo, and runs the requested maintenance passes for one PR
    identified by URL (rather than walking every tracked PR in state):

      * ``force_merge`` — merge target + AI-resolve + push.
      * ``analyze_fails`` — investigate failed CI on the PR (skipped
        if the merge step left the PR in ``conflict``).
      * ``address_review`` — run the address-review flow against the
        PR (skipped if the merge step left the PR in ``conflict``).

    Phase order when multiple flags are set:
    ``merge-target → analyze-fails → address-review``. ``analyze-fails``
    needs a CI report tied to the *current* head SHA; running
    ``address-review`` first would push commits that invalidate it.

    When ``config.stateless`` is False and the PR matches a tracked
    rebase PR, the corresponding :class:`FeatureState` is updated as
    if the PR had been picked up by ``refresh``. Otherwise no state is
    touched.

    Returns False on any unresolved conflict, address-review failure,
    analyze-fails per-PR error, or hard error so the CLI can pick a
    non-zero exit code.
    """
    from releasy.pipeline import _setup_repo

    parsed = parse_pr_url(pr_url)
    if parsed is None:
        console.print(f"[red]✗[/red] Could not parse PR URL: {pr_url!r}")
        return False
    pr_owner, pr_repo, _pr_num = parsed
    pr_slug = f"{pr_owner}/{pr_repo}"

    origin_slug = get_origin_repo_slug(config)
    if origin_slug and origin_slug.lower() != pr_slug.lower():
        console.print(
            f"[red]✗[/red] --pr points at {pr_slug} but the configured "
            f"origin is {origin_slug}. RelEasy can only push to the "
            "configured origin — use --stateless --origin to target a "
            "different repo, or run from a config.yaml whose origin "
            "matches."
        )
        return False

    # In-scope check: in stateful mode, ``--pr`` should only act on
    # PRs RelEasy itself tracks (source-or-rebase). A URL that doesn't
    # match any tracked entry is silently skipped so cron / webhook
    # callers can fire blindly without erroring on out-of-scope PRs.
    # ``--stateless`` bypasses this so the operator can still target
    # arbitrary PRs from the same project's config.
    if not getattr(config, "stateless", False):
        if not _pr_url_in_state_scope(config, pr_url):
            console.print(
                f"[dim]--pr={pr_url} is not in this project's tracked "
                "scope (no FeatureState matches its source or rebase "
                "URL) — nothing to do.[/dim]"
            )
            return True

    rebase_pr = fetch_pr_by_url(config, pr_url, include_closed=True)
    if rebase_pr is None:
        console.print(
            f"[red]✗[/red] Could not fetch PR {pr_url} — check "
            "RELEASY_GITHUB_TOKEN scope and the URL."
        )
        return False

    refs = _fetch_pr_refs(pr_url)
    if refs is None:
        console.print(
            f"[red]✗[/red] Could not look up head/base refs for "
            f"{pr_url} — check token scope."
        )
        return False
    head_ref, head_repo, base_ref_branch, _head_sha, _pr_number = refs

    if origin_slug and head_repo.lower() != origin_slug.lower():
        console.print(
            f"[red]✗[/red] PR head branch lives on {head_repo}, but "
            f"RelEasy only pushes to origin ({origin_slug}). Cannot "
            "resolve conflicts on a PR whose head is on a fork."
        )
        return False

    repo_path = _setup_repo(config, work_dir, base_ref_branch)

    if is_operation_in_progress(repo_path):
        console.print(
            f"\n[red]✗[/red] A git operation is still in progress in "
            f"[cyan]{repo_path}[/cyan]. Finish or abort it first."
        )
        return False

    remote = config.origin.remote_name
    # Refresh remote pointers so we operate on the latest tips.
    fetch_remote(repo_path, remote)

    if not remote_branch_exists(repo_path, base_ref_branch, remote):
        console.print(
            f"\n[red]✗[/red] Base branch [cyan]{base_ref_branch}[/cyan] "
            f"not found on [cyan]{remote}[/cyan]. Cannot merge."
        )
        return False

    ai_active = resolve_conflicts and config.ai_resolve.enabled
    if force_merge:
        if ai_active:
            console.print(
                f"[dim]AI conflict resolver: enabled "
                f"(command='{config.ai_resolve.command}', "
                f"prompt='{config.ai_resolve.merge_prompt_file}', "
                f"max_iterations={config.ai_resolve.max_iterations})[/dim]"
            )
        else:
            why = (
                "disabled via --no-resolve-conflicts"
                if not resolve_conflicts else "disabled in config"
            )
            console.print(f"[dim]AI conflict resolver: {why}[/dim]")

    if config.dry_run:
        console.print(
            "\n[bold magenta]DRY RUN[/bold magenta]: no state, repo, or "
            "GitHub writes will happen. Output shows intended actions only."
        )

    # Session labels: add any missing ones to this PR.
    from releasy.pipeline import (
        _session_pr_labels, reconcile_session_labels_on_prs,
    )
    session_labels = _session_pr_labels(config)
    if session_labels:
        console.print(
            f"\n[bold]Reconciling session labels[/bold] "
            f"([cyan]{', '.join(session_labels)}[/cyan]) on the PR…"
        )
        added_per_pr = reconcile_session_labels_on_prs(
            config, [(rebase_pr.url, rebase_pr.number)],
        )
        if added_per_pr:
            for pr_url_, labels_ in added_per_pr:
                console.print(
                    f"  [green]+[/green] [link={pr_url_}]{pr_url_}[/link] — "
                    f"added: [cyan]{', '.join(labels_)}[/cyan]"
                )
        else:
            console.print(
                "  [dim]PR already carries every session label — nothing "
                "to do[/dim]"
            )

    merge_outcome: MergeResolveOutcome | None = None
    if force_merge:
        source_pr = _resolve_source_pr(config, rebase_pr)

        console.print(
            f"\n[bold]Phase:[/bold] Merging "
            f"[cyan]{remote}/{base_ref_branch}[/cyan] into "
            f"[cyan]{head_ref}[/cyan]"
        )
        console.print(
            f"  [dim](rebase PR #{rebase_pr.number} → source "
            f"{source_pr.url})[/dim]"
        )

        merge_outcome = run_merge_resolve(
            config, repo_path,
            head_branch=head_ref,
            base_branch=base_ref_branch,
            source_pr=source_pr,
            rebase_pr_url=rebase_pr.url,
            ai_active=ai_active,
            remote=remote,
            force_merge=force_merge,
        )

        # Optional state sync: if a state file is around AND a tracked
        # FeatureState's rebase_pr_url matches, fold the outcome into it
        # so the project board / status views stay coherent.
        _maybe_update_tracked_state(config, rebase_pr.url, merge_outcome)

    merge_left_conflict = (
        merge_outcome is not None and merge_outcome.status == "conflict"
    )

    if analyze_fails:
        if merge_left_conflict:
            console.print(
                "\n[dim]Skipping analyze-fails — merge ended in "
                "conflict; resolve that first.[/dim]"
            )
        else:
            af_ok = _analyze_fails_for_pr_url(
                config, pr_url, repo_path,
                no_flaky_check=no_flaky_check,
                post_comment=post_comment,
            )
            if not af_ok:
                return False

    if address_review:
        if merge_left_conflict:
            console.print(
                "\n[dim]Skipping address-review — merge ended in "
                "conflict; resolve that first.[/dim]"
            )
        else:
            ar_ok = _address_review_for_pr_url(config, pr_url, work_dir)
            if not ar_ok:
                return False

    if merge_outcome is not None:
        if merge_outcome.status == "conflict":
            console.print(
                "[yellow]PR is still in conflict — resolve it on GitHub "
                "(or locally + push), then re-run.[/yellow]"
            )
            return False
        if merge_outcome.status == "skipped" and merge_outcome.error:
            console.print(f"[yellow]Skipped: {merge_outcome.error}[/yellow]")
            return False
    return True


def _pr_url_in_state_scope(config: Config, pr_url: str) -> bool:
    """True when ``pr_url`` matches any tracked entry's source or rebase URL.

    Compared on ``(owner, repo, number)`` so cosmetic differences
    (trailing slash, fragment, ``.git`` suffix) don't break the match.
    Used by ``refresh --pr`` to scope-gate the URL-driven flow:
    out-of-scope URLs are silently no-op'd in stateful mode rather
    than triggering a full GitHub fetch + repo setup.

    Failing state load is treated as "not in scope" — the same safe
    fallback applies to a missing / corrupt state file.
    """
    target = parse_pr_url(pr_url)
    if target is None:
        return False
    try:
        state = load_state(config)
    except Exception:  # pragma: no cover — bad state file
        return False
    if not state.features:
        return False
    for fs in state.features.values():
        for url in (fs.rebase_pr_url, fs.pr_url, *fs.pr_urls):
            if not url:
                continue
            if parse_pr_url(url) == target:
                return True
    return False


def _maybe_update_tracked_state(
    config: Config, rebase_pr_url: str, outcome: MergeResolveOutcome,
) -> None:
    """Reflect a URL-driven outcome on a matching tracked FeatureState.

    No-op when ``config.stateless`` is set or no FeatureState matches
    the rebase PR URL — keeping the URL-driven flow usable without a
    state file at all.
    """
    if getattr(config, "stateless", False):
        return
    try:
        state = load_state(config)
    except Exception:  # pragma: no cover — bad state file
        return
    if not state.features:
        return

    target = rebase_pr_url.rstrip("/")
    matched: FeatureState | None = None
    for fs in state.features.values():
        if fs.rebase_pr_url and fs.rebase_pr_url.rstrip("/") == target:
            matched = fs
            break
    if matched is None:
        return

    if outcome.ai_cost_usd is not None:
        prior = matched.ai_cost_usd or 0.0
        matched.ai_cost_usd = prior + outcome.ai_cost_usd

    if outcome.status == "clean":
        if matched.status == "conflict" and matched.conflict_files:
            matched.conflict_files = []
            _persist(config, state)
        return

    if outcome.status == "resolved":
        matched.conflict_files = []
        if matched.status == "conflict":
            matched.status = "needs_review"
            matched.failed_step_index = None
            matched.partial_pr_count = None
        if outcome.ai_used:
            matched.ai_resolved = True
            if outcome.ai_iterations:
                prior = matched.ai_iterations or 0
                matched.ai_iterations = prior + outcome.ai_iterations
        _persist(config, state)
        return

    if outcome.status == "conflict":
        _record_conflict(config, state, matched, outcome.conflict_files)
        return

    if outcome.status == "skipped" and outcome.ai_cost_usd is not None:
        _persist(config, state)
