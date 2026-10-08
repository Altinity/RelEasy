"""``releasy refresh`` — maintenance passes over already-tracked port PRs
(status sync, merge target + AI resolve, analyze-fails, address-review)."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from releasy.pipeline import OnlyFilter

from releasy.termlog import console

from releasy.ai_resolve import (
    AIResolveContext,
    _backend_label,
    attempt_ai_resolve,
    flag_resolution_warnings_on_pr,
)
from releasy.config import (
    Config, PortMode, lookup_pr_ai_context,
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
    fetch_pr_head,
    get_origin_repo_slug,
    parse_pr_url,
    same_pr_url,
    sync_project,
)
from releasy.state import (
    FeatureState,
    PipelineState,
    clear_conflict_markers,
    find_feature_by_pr_url,
    load_state,
    save_state,
)


def _persist(config: Config, state: PipelineState) -> None:
    """Save state and, with ``push`` on, sync the project board."""
    if config.dry_run:
        return
    save_state(state, config)
    if config.push:
        sync_project(config, state)


def _synthesise_source_pr(fs: FeatureState) -> PRInfo | None:
    """Build a ``PRInfo`` from cached FeatureState fields (no GitHub fetch)."""
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
    """Clean the worktree, reset ``branch`` to ``<remote>/<branch>``; return HEAD or None."""
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
    """Abort any in-progress git op, then hard-reset to ``fallback_sha`` if given."""
    if is_operation_in_progress(repo_path):
        run_git(["merge", "--abort"], repo_path, check=False)
        run_git(["cherry-pick", "--abort"], repo_path, check=False)
        run_git(["rebase", "--abort"], repo_path, check=False)
    if fallback_sha:
        run_git(["reset", "--hard", fallback_sha], repo_path, check=False)


def _print_ai_resolver_status(
    config: Config, ai_active: bool, resolve_conflicts: bool,
) -> None:
    if ai_active:
        console.print(
            f"[dim]AI conflict resolver: enabled "
            f"(backend='{_backend_label(config, config.ai_resolve.command)}', "
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
    """Status-sync tracked PRs, then run the opted-in passes on them.

    Pass order is merge-target → analyze-fails → address-review:
    analyze-fails reads CI tied to the current head SHA, so nothing may
    push before it. PRs left in conflict by the merge pass are skipped
    by the later passes. Returns False on any conflict or pass failure.
    """
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
        _print_ai_resolver_status(config, ai_active, resolve_conflicts)

    if config.dry_run:
        console.print(
            "\n[bold magenta]DRY RUN[/bold magenta]: no state, repo, or "
            "GitHub writes will happen. Output shows intended actions only."
        )

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
    reverted_count = 0
    for fid, fs in state.features.items():
        if fs.status in ("skipped", "merged"):
            continue
        if fs.status == "reverted":
            reverted_count += 1
            continue
        if fs.status == "closed":
            closed_count += 1
            continue
        if fs.status == "superseded":
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
    if reverted_count:
        console.print(
            f"  [dim]Skipping {reverted_count} entry/ies whose port was "
            "reverted on the target branch.[/dim]"
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

    from releasy.pipeline import (
        _all_session_label_names, _pr_number_from_url,
        reconcile_session_labels_on_prs,
    )
    session_labels = _all_session_label_names(config)
    if session_labels:
        console.print(
            f"\n[bold]Reconciling session labels[/bold] "
            f"([cyan]{', '.join(session_labels)}[/cyan]) "
            f"across {len(candidates)} tracked PR(s)…"
        )
        pr_refs: list[tuple[str, int, PortMode | None]] = []
        for _fid, fs in candidates:
            if not fs.rebase_pr_url:
                continue
            num = _pr_number_from_url(fs.rebase_pr_url)
            if num is not None:
                pr_refs.append((fs.rebase_pr_url, num, fs.mode))
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


@dataclass
class MergeResolveOutcome:
    """Result of one merge-into-PR-branch attempt, independent of state."""
    status: str  # "clean" | "resolved" | "conflict" | "skipped"
    conflict_files: list[str] = field(default_factory=list)
    ai_iterations: int | None = None
    ai_cost_usd: float | None = None
    error: str | None = None
    # True only when the AI resolved conflicts (a clean --merge-target push is also "resolved").
    ai_used: bool = False
    # Postcondition complaints kept as warnings on a "resolved" push.
    ai_warnings: list[str] = field(default_factory=list)


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
    """Merge ``base_branch`` into ``head_branch``, AI-resolve conflicts, push.

    Never touches state. A clean merge is pushed only with ``force_merge``;
    otherwise the branch is reset to its original tip.
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

    merge_msg = f"Merge {base_ref} into {head_branch}"
    merge = run_git(
        ["merge", "--no-ff", "--no-edit", "-m", merge_msg, base_ref],
        repo_path, check=False,
    )

    if merge.returncode == 0:
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
                return MergeResolveOutcome(status="resolved")
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

    # Plain push: a fast-forward; force would clobber commits pushed since our fetch.
    push = run_git(
        ["push", remote, head_branch], repo_path, check=False,
    )
    if push.returncode != 0:
        console.print(
            "    [yellow]![/yellow] merge resolved locally but push "
            "failed (origin moved? auth?). Leaving local commit; "
            "re-run to retry."
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

    if result.warnings:
        flag_resolution_warnings_on_pr(config, rebase_pr_url, result.warnings)

    return MergeResolveOutcome(
        status="resolved",
        ai_iterations=result.iterations,
        ai_cost_usd=result.cost_usd,
        ai_used=True,
        ai_warnings=list(result.warnings),
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
    """:func:`run_merge_resolve` for one tracked PR, recorded in state."""
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
    _apply_merge_outcome(config, state, fs, outcome)
    return outcome.status


def _apply_merge_outcome(
    config: Config,
    state: PipelineState,
    fs: FeatureState,
    outcome: MergeResolveOutcome,
) -> None:
    """Fold a :class:`MergeResolveOutcome` into ``fs`` and persist."""
    # Cost is billed even when the resolve failed.
    if outcome.ai_cost_usd is not None:
        prior = fs.ai_cost_usd or 0.0
        fs.ai_cost_usd = prior + outcome.ai_cost_usd

    if outcome.status == "clean":
        if fs.status == "conflict" and fs.conflict_files:
            fs.conflict_files = []
            _persist(config, state)
        return

    if outcome.status == "skipped":
        if outcome.ai_cost_usd is not None:
            _persist(config, state)
        return

    if outcome.status == "conflict":
        _record_conflict(config, state, fs, outcome.conflict_files)
        return

    fs.conflict_files = []
    if fs.status == "conflict":
        fs.status = "needs_review"
        clear_conflict_markers(fs)
    if outcome.ai_used:
        fs.ai_resolved = True
        if outcome.ai_iterations:
            prior = fs.ai_iterations or 0
            fs.ai_iterations = prior + outcome.ai_iterations
    if outcome.ai_warnings:
        fs.verify_needs_attention = True
    _persist(config, state)


def _record_conflict(
    config: Config,
    state: PipelineState,
    fs: FeatureState,
    conflict_files: list[str],
) -> None:
    """Flip a tracked PR to ``conflict`` (cherry-pick failure markers untouched) and persist."""
    fs.status = "conflict"
    fs.conflict_files = conflict_files
    _persist(config, state)


def _address_review_one(
    config: Config, fs: FeatureState, work_dir: Path | None,
) -> bool:
    if not fs.rebase_pr_url:
        return True
    return _address_review_for_pr_url(config, fs.rebase_pr_url, work_dir)


def _address_review_for_pr_url(
    config: Config, pr_url: str, work_dir: Path | None,
) -> bool:
    """Run :func:`address_review` on one PR; False on any error."""
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
    """Run analyze-fails on one PR; False when the run errored."""
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


# Source-PR reference forms in a rebase PR body: full URL, owner/repo#N, #N.
_SOURCE_PR_URL_RE = re.compile(
    r"https://github\.com/[^/\s]+/[^/\s]+?(?:\.git)?/pull/\d+\b",
)
_SOURCE_PR_SLUG_RE = re.compile(
    r"\b([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)#(\d+)\b",
)
_SOURCE_PR_HASH_RE = re.compile(r"(?<![\w/])#(\d+)\b")


def _find_source_pr_url(rebase_pr_body: str, rebase_slug: str) -> str | None:
    """First source-PR reference anywhere in a rebase PR body, as a URL."""
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
    """Source PR referenced in the rebase PR body, else the rebase PR itself."""
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
    """``releasy refresh --pr <url>``: the same passes as
    :func:`refresh_tracked_prs`, for one PR identified by URL.

    Updates the matching tracked FeatureState unless ``config.stateless``.
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

    # Out-of-scope URLs are a silent no-op so cron / webhook callers can fire blindly.
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

    refs = fetch_pr_head(pr_url)
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
    fetch_remote(repo_path, remote)

    if not remote_branch_exists(repo_path, base_ref_branch, remote):
        console.print(
            f"\n[red]✗[/red] Base branch [cyan]{base_ref_branch}[/cyan] "
            f"not found on [cyan]{remote}[/cyan]. Cannot merge."
        )
        return False

    ai_active = resolve_conflicts and config.ai_resolve.enabled
    if force_merge:
        _print_ai_resolver_status(config, ai_active, resolve_conflicts)

    if config.dry_run:
        console.print(
            "\n[bold magenta]DRY RUN[/bold magenta]: no state, repo, or "
            "GitHub writes will happen. Output shows intended actions only."
        )

    from releasy.pipeline import (
        _all_session_label_names, reconcile_session_labels_on_prs,
    )
    session_labels = _all_session_label_names(config)
    if session_labels:
        console.print(
            f"\n[bold]Reconciling session labels[/bold] "
            f"([cyan]{', '.join(session_labels)}[/cyan]) on the PR…"
        )
        added_per_pr = reconcile_session_labels_on_prs(
            config,
            [(
                rebase_pr.url, rebase_pr.number,
                _tracked_port_mode(config, rebase_pr.url),
            )],
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
    """True when ``pr_url`` matches a tracked entry's source or rebase URL."""
    try:
        state = load_state(config)
    except Exception:  # pragma: no cover — bad state file
        return False
    return find_feature_by_pr_url(state, pr_url) is not None


def _tracked_port_mode(config: Config, pr_url: str) -> PortMode | None:
    """Persisted port mode of the entry tracking ``pr_url`` (None if none)."""
    if getattr(config, "stateless", False):
        return None
    try:
        state = load_state(config)
    except Exception:  # pragma: no cover — bad state file
        return None
    hit = find_feature_by_pr_url(state, pr_url)
    return hit[1].mode if hit is not None else None


def _maybe_update_tracked_state(
    config: Config, rebase_pr_url: str, outcome: MergeResolveOutcome,
) -> None:
    """Fold ``outcome`` into the tracked entry whose rebase PR is ``rebase_pr_url``."""
    if getattr(config, "stateless", False):
        return
    try:
        state = load_state(config)
    except Exception:  # pragma: no cover — bad state file
        return
    if not state.features:
        return

    matched: FeatureState | None = None
    for fs in state.features.values():
        if same_pr_url(fs.rebase_pr_url, rebase_pr_url):
            matched = fs
            break
    if matched is None:
        return
    _apply_merge_outcome(config, state, matched, outcome)
