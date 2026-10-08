"""``releasy rebase``: re-open existing rebase PRs against a different target branch.

Cherry-picks each PR's commits (falling back to a squashed merge), opens a new PR
and closes the original. Never writes the state file.
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from releasy.pipeline import OnlyFilter

from releasy.termlog import console

from releasy.ai_resolve import (
    AIResolveContext,
    attempt_ai_resolve,
    flag_resolution_warnings_on_pr,
)
from releasy.config import Config, lookup_pr_ai_context
from releasy.git_ops import (
    abort_in_progress_op,
    fetch_commit,
    fetch_remote,
    get_conflict_files,
    is_operation_in_progress,
    local_branch_exists,
    remote_branch_exists,
    run_git,
    stash_and_clean,
)
from releasy.github_ops import (
    PRInfo,
    close_pull_request,
    create_pull_request,
    fetch_pr_by_url,
    fetch_pr_head,
    get_origin_repo_slug,
    parse_pr_url,
    slug_to_https_url,
)
from releasy.state import load_state


@dataclass
class RebaseOutcome:
    pr_url: str
    skipped: bool = False
    skip_reason: str | None = None
    new_pr_url: str | None = None
    new_branch: str | None = None
    fallback_used: bool = False
    error: str | None = None

    @property
    def success(self) -> bool:
        return self.error is None


@dataclass
class RebaseSummary:
    outcomes: list[RebaseOutcome] = field(default_factory=list)

    @property
    def all_succeeded(self) -> bool:
        return all(o.success for o in self.outcomes)


def _short_id() -> str:
    return secrets.token_hex(3)


def _new_branch_name(pr_number: int, target: str) -> str:
    """Branch name with PR number, target and a random suffix to avoid collisions."""
    sanitized_target = "".join(
        c if (c.isalnum() or c in "._-") else "-" for c in target
    )
    return f"releasy/rebase/pr-{pr_number}-onto-{sanitized_target}-{_short_id()}"


def _commits_in_range(repo_path: Path, base_ref: str, tip_ref: str) -> list[str]:
    """Non-merge commits in ``base_ref..tip_ref``, oldest first."""
    result = run_git(
        ["rev-list", "--reverse", "--no-merges", f"{base_ref}..{tip_ref}"],
        repo_path, check=False,
    )
    if result.returncode != 0:
        return []
    return [s for s in result.stdout.strip().splitlines() if s]


def _commit_subject(repo_path: Path, sha: str) -> str:
    res = run_git(
        ["log", "-1", "--format=%s", sha], repo_path, check=False,
    )
    return res.stdout.strip() if res.returncode == 0 else sha[:12]


def _abort_any(repo_path: Path) -> None:
    if is_operation_in_progress(repo_path):
        abort_in_progress_op(repo_path)


def _hard_reset(repo_path: Path, ref: str) -> None:
    run_git(["reset", "--hard", ref], repo_path, check=False)
    run_git(["clean", "-fd"], repo_path, check=False)


def _ai_active(config: Config, resolve_conflicts: bool) -> bool:
    return resolve_conflicts and config.ai_resolve.enabled


def _ai_resolve(
    config: Config,
    repo_path: Path,
    new_branch: str,
    target_branch: str,
    source_pr: PRInfo,
    conflict_files: list[str],
):
    head = run_git(
        ["rev-parse", "--verify", "HEAD"], repo_path, check=False,
    )
    start_sha = head.stdout.strip() if head.returncode == 0 else None
    ctx = AIResolveContext(
        port_branch=new_branch,
        base_branch=target_branch,
        source_pr=source_pr,
        conflict_files=conflict_files,
        start_sha=start_sha,
        operation="cherry-pick",
        user_context=lookup_pr_ai_context(
            config.pr_sources, source_pr.url,
        ),
    )
    ai_result = attempt_ai_resolve(config, repo_path, ctx)
    if ai_result.cost_usd is not None:
        console.print(
            f"      [dim](claude cost: ${ai_result.cost_usd:.4f})[/dim]"
        )
    return ai_result


def _try_cherry_pick_path(
    config: Config,
    repo_path: Path,
    new_branch: str,
    target_branch: str,
    source_pr: PRInfo,
    commits: list[str],
    ai_active: bool,
) -> tuple[bool, str | None, list[str]]:
    """Cherry-pick each commit onto the checked-out ``new_branch``, AI-resolving conflicts.

    Returns ``(ok, err, warnings)``; on failure the branch is left as-is for the caller.
    """
    warnings: list[str] = []
    for idx, sha in enumerate(commits, start=1):
        subject = _commit_subject(repo_path, sha)
        console.print(
            f"    [dim]({idx}/{len(commits)})[/dim] cherry-pick "
            f"[cyan]{sha[:12]}[/cyan]  {subject}"
        )
        # Don't halt on commits that became no-ops or were empty to begin with.
        result = run_git(
            [
                "cherry-pick", "--no-edit",
                "--keep-redundant-commits",
                "--allow-empty", "--allow-empty-message",
                sha,
            ],
            repo_path, check=False,
        )
        if result.returncode == 0:
            continue

        conflict_files = get_conflict_files(repo_path)
        if not conflict_files:
            err = (result.stderr or "").strip().splitlines()[:3]
            for line in err:
                console.print(f"      [red]•[/red] [dim]{line}[/dim]")
            _abort_any(repo_path)
            return False, (
                f"cherry-pick of {sha[:12]} failed without conflict markers "
                "(wrong tree state? merge commit?)"
            ), warnings

        console.print(
            f"      [yellow]conflict in {len(conflict_files)} file(s)[/yellow]"
        )
        for cf in conflict_files:
            console.print(f"        [red]•[/red] {cf}")

        if not ai_active:
            _abort_any(repo_path)
            return False, "cherry-pick conflicted and AI resolver disabled", warnings

        ai_result = _ai_resolve(
            config, repo_path, new_branch, target_branch, source_pr, conflict_files,
        )
        if not ai_result.success:
            reason = ai_result.error or (
                "timed out" if ai_result.timed_out else "unknown failure"
            )
            return False, f"AI resolve failed on {sha[:12]}: {reason}", warnings
        warnings.extend(ai_result.warnings)
        iters = (
            f" (iterations: {ai_result.iterations})"
            if ai_result.iterations else ""
        )
        console.print(
            f"      [green]✓[/green] AI resolved cherry-pick conflict{iters}"
        )

    return True, None, warnings


def _try_diff_fallback(
    config: Config,
    repo_path: Path,
    new_branch: str,
    target_branch: str,
    target_ref: str,
    source_pr: PRInfo,
    head_sha: str,
    ai_active: bool,
) -> tuple[bool, str | None, list[str]]:
    """Fallback: reset to target and replay ``head_sha`` as one squashed merge."""
    _abort_any(repo_path)
    _hard_reset(repo_path, target_ref)

    console.print(
        f"    [yellow]↻[/yellow] cherry-pick path failed — falling back to "
        f"a squashed merge of [cyan]{head_sha[:12]}[/cyan] onto "
        f"[cyan]{target_branch}[/cyan]"
    )

    merge = run_git(
        ["merge", "--squash", "--no-commit", head_sha],
        repo_path, check=False,
    )
    conflict_files = get_conflict_files(repo_path)

    if merge.returncode != 0 and not conflict_files:
        err = (merge.stderr or "").strip().splitlines()[:3]
        for line in err:
            console.print(f"      [dim]{line}[/dim]")
        _abort_any(repo_path)
        _hard_reset(repo_path, target_ref)
        return False, (
            "git merge --squash failed without producing conflict markers"
        ), []

    if conflict_files:
        console.print(
            f"      [yellow]conflict in {len(conflict_files)} file(s)[/yellow]"
        )
        for cf in conflict_files:
            console.print(f"        [red]•[/red] {cf}")
        if not ai_active:
            _abort_any(repo_path)
            _hard_reset(repo_path, target_ref)
            return False, (
                "squashed merge conflicted and AI resolver disabled"
            ), []

        ai_result = _ai_resolve(
            config, repo_path, new_branch, target_branch, source_pr, conflict_files,
        )
        if not ai_result.success:
            reason = ai_result.error or (
                "timed out" if ai_result.timed_out else "unknown failure"
            )
            _hard_reset(repo_path, target_ref)
            return False, f"AI resolve failed on squashed merge: {reason}", []
        # The AI commits the resolution itself.
        return True, None, list(ai_result.warnings)

    title = source_pr.title or f"Rebase PR #{source_pr.number}"
    commit_msg = f"{title}\n\nSquashed port of {source_pr.url}"
    commit = run_git(
        ["commit", "-m", commit_msg], repo_path, check=False,
    )
    if commit.returncode != 0:
        err = (commit.stderr or "").strip()
        _hard_reset(repo_path, target_ref)
        return False, f"failed to commit squashed merge: {err}", []
    return True, None, []


def _ported_body(old_pr_url: str, target_branch: str, original_body: str) -> str:
    prefix = f"_Port of {old_pr_url} onto `{target_branch}`._\n\n"
    return prefix + (original_body or "")


def rebase_one_pr(
    config: Config,
    repo_path: Path,
    pr_url: str,
    target_branch: str,
    *,
    resolve_conflicts: bool = True,
) -> RebaseOutcome:
    parsed = parse_pr_url(pr_url)
    if parsed is None:
        return RebaseOutcome(
            pr_url=pr_url,
            error=f"could not parse PR URL: {pr_url!r}",
        )
    pr_owner, pr_repo, _pr_num = parsed
    pr_slug = f"{pr_owner}/{pr_repo}"

    origin_slug = get_origin_repo_slug(config)
    if origin_slug and origin_slug.lower() != pr_slug.lower():
        return RebaseOutcome(
            pr_url=pr_url,
            error=(
                f"PR lives on {pr_slug} but the configured origin is "
                f"{origin_slug}. RelEasy can only push to origin."
            ),
        )

    pr_info = fetch_pr_by_url(config, pr_url, include_closed=True)
    if pr_info is None:
        return RebaseOutcome(
            pr_url=pr_url,
            error=f"could not fetch PR {pr_url} (token scope? URL?)",
        )

    refs = fetch_pr_head(pr_url)
    if refs is None:
        return RebaseOutcome(
            pr_url=pr_url,
            error=f"could not look up head/base refs for {pr_url}",
        )
    head_ref, head_repo, base_ref_branch, head_sha, pr_number = refs

    if base_ref_branch == target_branch:
        console.print(
            f"  [dim]{pr_url} already targets [cyan]{target_branch}[/cyan] "
            "— skipping[/dim]"
        )
        return RebaseOutcome(
            pr_url=pr_url, skipped=True,
            skip_reason=f"already targets {target_branch}",
        )

    if origin_slug and head_repo.lower() != origin_slug.lower():
        return RebaseOutcome(
            pr_url=pr_url,
            error=(
                f"PR head branch lives on {head_repo}, but RelEasy only "
                f"pushes to origin ({origin_slug}). Cannot rebase a PR "
                "whose head is on a fork."
            ),
        )

    remote = config.origin.remote_name
    target_ref = f"{remote}/{target_branch}"

    if not remote_branch_exists(repo_path, target_branch, remote):
        return RebaseOutcome(
            pr_url=pr_url,
            error=(
                f"target branch {target_branch!r} not found on {remote}. "
                "Push it first."
            ),
        )

    # The head branch may be gone on origin (closed PR); fetch the SHA directly.
    if not remote_branch_exists(repo_path, head_ref, remote):
        if not fetch_commit(repo_path, slug_to_https_url(pr_slug), head_sha):
            return RebaseOutcome(
                pr_url=pr_url,
                error=(
                    f"PR head ref {head_ref!r} missing on origin and could "
                    f"not fetch {head_sha[:12]} directly."
                ),
            )

    merge_base = run_git(
        ["merge-base", target_ref, head_sha], repo_path, check=False,
    )
    if merge_base.returncode != 0 or not merge_base.stdout.strip():
        return RebaseOutcome(
            pr_url=pr_url,
            error=(
                f"could not find merge-base between {target_ref} and "
                f"{head_sha[:12]}"
            ),
        )
    base_for_range = merge_base.stdout.strip()
    commits = _commits_in_range(repo_path, base_for_range, head_sha)

    new_branch = _new_branch_name(pr_number, target_branch)
    console.print(
        f"\n  [bold]Rebasing PR #{pr_number}[/bold] ({pr_info.title or '?'})"
    )
    console.print(
        f"    [dim]from {base_ref_branch} → onto {target_branch}, "
        f"branch [cyan]{new_branch}[/cyan][/dim]"
    )

    if config.dry_run:
        console.print(
            f"    [magenta]dry-run:[/magenta] would create "
            f"[cyan]{new_branch}[/cyan] off [cyan]{target_ref}[/cyan], "
            f"cherry-pick {len(commits)} commit(s), push, and open a "
            "fresh PR (closing the original as superseded). Conflict "
            "outcome unknown — not simulated."
        )
        return RebaseOutcome(pr_url=pr_url, new_branch=new_branch)

    stash_and_clean(repo_path)
    _abort_any(repo_path)
    if local_branch_exists(repo_path, new_branch):
        run_git(["branch", "-D", new_branch], repo_path, check=False)
    co = run_git(
        ["checkout", "-b", new_branch, target_ref], repo_path, check=False,
    )
    if co.returncode != 0:
        return RebaseOutcome(
            pr_url=pr_url,
            error=f"could not create {new_branch} off {target_ref}",
        )

    ai_active = _ai_active(config, resolve_conflicts)
    fallback_used = False
    resolve_warnings: list[str] = []

    if commits:
        console.print(
            f"    [dim]{len(commits)} commit(s) to cherry-pick[/dim]"
        )
        ok, err, resolve_warnings = _try_cherry_pick_path(
            config, repo_path, new_branch, target_branch,
            pr_info, commits, ai_active,
        )
        if not ok:
            console.print(
                f"    [yellow]cherry-pick path failed:[/yellow] {err}"
            )
            ok2, err2, resolve_warnings = _try_diff_fallback(
                config, repo_path, new_branch, target_branch, target_ref,
                pr_info, head_sha, ai_active,
            )
            if not ok2:
                _hard_reset(repo_path, target_ref)
                run_git(["branch", "-D", new_branch], repo_path, check=False)
                return RebaseOutcome(
                    pr_url=pr_url, new_branch=new_branch,
                    error=f"diff fallback failed: {err2}",
                )
            fallback_used = True
    else:
        console.print(
            "    [dim]no commits in range — PR is already on top of "
            f"{target_branch}[/dim]"
        )
        run_git(
            ["checkout", "--detach", target_ref], repo_path, check=False,
        )
        run_git(["branch", "-D", new_branch], repo_path, check=False)
        return RebaseOutcome(
            pr_url=pr_url, skipped=True,
            skip_reason="no commits between merge-base and head",
        )

    push = run_git(
        ["push", remote, new_branch], repo_path, check=False,
    )
    if push.returncode != 0:
        err = (push.stderr or "").strip()
        return RebaseOutcome(
            pr_url=pr_url, new_branch=new_branch, fallback_used=fallback_used,
            error=f"push failed: {err}",
        )
    console.print(
        f"    [green]✓[/green] pushed [cyan]{new_branch}[/cyan]"
    )

    title = pr_info.title or f"Rebase PR #{pr_number}"
    body = _ported_body(pr_url, target_branch, pr_info.body or "")
    new_pr_url = create_pull_request(
        config, new_branch, target_branch, title, body,
    )
    if not new_pr_url:
        return RebaseOutcome(
            pr_url=pr_url, new_branch=new_branch, fallback_used=fallback_used,
            error="branch pushed but PR creation failed (open it manually)",
        )
    console.print(
        f"    [green]✓[/green] PR opened: [link={new_pr_url}]{new_pr_url}[/link]"
    )

    if resolve_warnings:
        flag_resolution_warnings_on_pr(config, new_pr_url, resolve_warnings)

    closed = close_pull_request(
        config, pr_number,
        comment=f"Superseded by {new_pr_url} (rebased onto `{target_branch}`).",
    )
    if not closed:
        console.print(
            f"    [yellow]![/yellow] could not close original PR "
            f"{pr_url} automatically — close it manually."
        )

    return RebaseOutcome(
        pr_url=pr_url, new_pr_url=new_pr_url, new_branch=new_branch,
        fallback_used=fallback_used,
    )


def _setup(config: Config, work_dir: Path | None, target_branch: str) -> Path:
    from releasy.pipeline import _setup_repo  # late import to avoid a cycle

    repo_path = _setup_repo(config, work_dir, target_branch)
    fetch_remote(repo_path, config.origin.remote_name)
    return repo_path


def rebase_single(
    config: Config,
    pr_url: str,
    target_branch: str,
    *,
    work_dir: Path | None = None,
    resolve_conflicts: bool = True,
) -> RebaseSummary:
    repo_path = _setup(config, work_dir, target_branch)
    if config.dry_run:
        console.print(
            "\n[bold magenta]DRY RUN[/bold magenta]: no branches, "
            "cherry-picks, pushes, or PR opens/closes will happen."
        )
    summary = RebaseSummary()
    summary.outcomes.append(
        rebase_one_pr(
            config, repo_path, pr_url, target_branch,
            resolve_conflicts=resolve_conflicts,
        )
    )
    _print_summary(summary)
    return summary


def rebase_all_tracked(
    config: Config,
    target_branch: str,
    *,
    work_dir: Path | None = None,
    resolve_conflicts: bool = True,
    only: OnlyFilter | None = None,
) -> RebaseSummary:
    """Rebase every tracked rebase PR (or just ``only``) onto ``target_branch``."""
    state = load_state(config)
    candidates: list[tuple[str, str]] = []  # (feature_id, rebase_pr_url)
    for fid, fs in state.features.items():
        if fs.status == "skipped":
            continue
        if not fs.rebase_pr_url:
            continue
        if only is not None and not only.matches_state(fid, fs):
            continue
        candidates.append((fid, fs.rebase_pr_url))

    summary = RebaseSummary()
    if only is not None and not candidates:
        console.print(
            f"\n[red]✗[/red] --only={only.label!r} matched no tracked "
            "rebase PRs. Check the URL / group id and re-run."
        )
        return summary
    if not candidates:
        console.print(
            "[yellow]No tracked rebase PRs found in state — nothing "
            "to rebase.[/yellow]"
        )
        return summary

    repo_path = _setup(config, work_dir, target_branch)
    scope = (
        f" (--only={only.label})" if only is not None else ""
    )
    console.print(
        f"\n[bold]Rebasing {len(candidates)} tracked PR(s) onto "
        f"[cyan]{target_branch}[/cyan]{scope}[/bold]"
    )
    if config.dry_run:
        console.print(
            "[bold magenta]DRY RUN[/bold magenta]: no branches, "
            "cherry-picks, pushes, or PR opens/closes will happen."
        )
    for _fid, url in candidates:
        summary.outcomes.append(
            rebase_one_pr(
                config, repo_path, url, target_branch,
                resolve_conflicts=resolve_conflicts,
            )
        )
    _print_summary(summary)
    return summary


def _print_summary(summary: RebaseSummary) -> None:
    if not summary.outcomes:
        return
    console.print("\n[bold]Rebase summary[/bold]")
    for o in summary.outcomes:
        if o.skipped:
            console.print(
                f"  [dim]·[/dim] {o.pr_url}  [dim]skipped — "
                f"{o.skip_reason}[/dim]"
            )
        elif o.success:
            tag = " [dim](diff fallback)[/dim]" if o.fallback_used else ""
            dest = o.new_pr_url or f"{o.new_branch} [dim](dry-run)[/dim]"
            console.print(
                f"  [green]✓[/green] {o.pr_url} → {dest}{tag}"
            )
        else:
            console.print(
                f"  [red]✗[/red] {o.pr_url}  [red]{o.error}[/red]"
            )
