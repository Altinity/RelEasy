"""``releasy rebase`` — port an existing rebase PR onto a different target.

Single-PR mode (``--pr <url> --target <branch>``) or bulk mode
(``--target <branch>`` only). Bulk reads the per-project state file to
enumerate every rebase PR currently tracked. Each PR is rebased
independently:

1. Skip when the PR already targets ``<branch>``.
2. Make a fresh branch off ``origin/<target>``.
3. Cherry-pick the PR's commits one-by-one, AI-resolving conflicts as
   they appear. If the cherry-pick path can't be made to apply, fall
   back to a single squashed ``git merge --squash`` of the PR's head
   onto the new target and AI-resolve from there.
4. Push the new branch and open a new PR (same title / body, prefixed
   with a ``Port of <old PR> onto <target>`` reference).
5. Close the original PR with a ``superseded by <new PR>`` comment.

Stateless by design — the state file is per-project (target branch),
and rebased PRs belong to a different project. We never mutate the
loaded state file; we only read it in bulk mode to enumerate PRs.
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
from releasy.config import Config, get_github_token, lookup_pr_ai_context
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
    get_origin_repo_slug,
    parse_pr_url,
    slug_to_https_url,
)
from releasy.state import load_state


# ---------------------------------------------------------------------------
# Public dataclasses
# ---------------------------------------------------------------------------


@dataclass
class RebaseOutcome:
    """Per-PR result reported back to the CLI for summarisation."""
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
    """Aggregate result for the CLI's exit-code shaping."""
    outcomes: list[RebaseOutcome] = field(default_factory=list)

    @property
    def all_succeeded(self) -> bool:
        return all(o.success for o in self.outcomes)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _short_id() -> str:
    return secrets.token_hex(3)


def _new_branch_name(old_head_ref: str, pr_number: int, target: str) -> str:
    """Build a fresh branch name for the rebased PR.

    Carries enough provenance (old PR number + target) for humans
    inspecting ``git branch -a`` to figure out where it came from, and a
    6-hex suffix so re-runs don't collide with a previous attempt.
    """
    sanitized_target = "".join(
        c if (c.isalnum() or c in "._-") else "-" for c in target
    )
    return f"releasy/rebase/pr-{pr_number}-onto-{sanitized_target}-{_short_id()}"


def _fetch_pr_refs(
    pr_url: str,
) -> tuple[str, str, str, str, int] | None:
    """Look up the PR's head ref / head repo / base ref / head sha / number.

    Local copy of refresh._fetch_pr_refs to avoid dragging the refresh
    module's import surface into the rebase flow.
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


def _commits_in_range(repo_path: Path, base_ref: str, tip_ref: str) -> list[str]:
    """Commits in ``base_ref..tip_ref`` (oldest first), excluding merges.

    Octopus / merge commits can't be cherry-picked without an explicit
    ``-m <parent>`` choice; in a PR head branch they're virtually always
    "merged base into branch" noise that we don't want anyway — the
    target-side equivalent will get re-introduced by branching off the
    new target. Drop them here so the per-commit pick loop never has to
    guess a mainline.
    """
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


# ---------------------------------------------------------------------------
# Cherry-pick attempt
# ---------------------------------------------------------------------------


def _try_cherry_pick_path(
    config: Config,
    repo_path: Path,
    new_branch: str,
    target_branch: str,
    source_pr: PRInfo,
    commits: list[str],
    ai_active: bool,
) -> tuple[bool, str | None, list[str]]:
    """Cherry-pick each commit; AI-resolve any conflicts.

    Returns ``(ok, err, warnings)``; ``warnings`` are postcondition
    complaints the resolver kept a resolution despite, for the caller to
    flag on the PR it opens.

    Caller is responsible for putting HEAD on ``new_branch`` (already
    checked out off ``origin/<target_branch>``) before invoking this and
    for resetting / branching elsewhere on a False return — we leave the
    branch in whatever shape the failure produced so the caller can pick
    a recovery strategy (try the diff fallback, etc.).
    """
    warnings: list[str] = []
    for idx, sha in enumerate(commits, start=1):
        subject = _commit_subject(repo_path, sha)
        console.print(
            f"    [dim]({idx}/{len(commits)})[/dim] cherry-pick "
            f"[cyan]{sha[:12]}[/cyan]  {subject}"
        )
        # ``--keep-redundant-commits`` so commits that became no-ops on
        # the new target don't halt the loop; ``--allow-empty[-message]``
        # rescues the rare PR with an empty source commit.
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


# ---------------------------------------------------------------------------
# Squashed-diff fallback
# ---------------------------------------------------------------------------


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
    """Replay the PR as a single squashed merge of ``head_sha`` onto target.

    Used as a last resort when the per-commit cherry-pick path can't be
    made to apply (e.g. moved files, history rewrites, AI gave up
    mid-stream). Resets the branch to target, runs ``git merge --squash
    <head_sha>``, lets the AI resolve any conflicts, then commits.
    """
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
        if not ai_result.success:
            reason = ai_result.error or (
                "timed out" if ai_result.timed_out else "unknown failure"
            )
            _hard_reset(repo_path, target_ref)
            return False, f"AI resolve failed on squashed merge: {reason}", []
        # AI committed the resolution itself (the prompt instructs it to
        # `git commit` after fixing). Nothing else to do here.
        return True, None, list(ai_result.warnings)

    # Clean squashed merge — index has the changes staged but no commit
    # was made (we passed --no-commit). Make one now so we have something
    # to push.
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


# ---------------------------------------------------------------------------
# Per-PR driver
# ---------------------------------------------------------------------------


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
    """Rebase one PR onto ``target_branch``. Stateless beyond GitHub I/O."""
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

    refs = _fetch_pr_refs(pr_url)
    if refs is None:
        return RebaseOutcome(
            pr_url=pr_url,
            error=f"could not look up head/base refs for {pr_url}",
        )
    head_ref, head_repo, base_ref_branch, head_sha, pr_number = refs

    # Skip when the PR already targets the requested branch — the user's
    # explicit "no-op" case.
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

    # Make sure the PR's head sha is locally available — the PR's head
    # branch may have been deleted on origin (e.g. the PR is closed); we
    # fall back to fetching the SHA directly from the origin URL.
    if not remote_branch_exists(repo_path, head_ref, remote):
        if not fetch_commit(repo_path, slug_to_https_url(pr_slug), head_sha):
            return RebaseOutcome(
                pr_url=pr_url,
                error=(
                    f"PR head ref {head_ref!r} missing on origin and could "
                    f"not fetch {head_sha[:12]} directly."
                ),
            )

    # Determine the commit list from merge-base(target, head) so we don't
    # replay any commit that's already on the new target — common when
    # both branches share an ancestor in master.
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

    new_branch = _new_branch_name(head_ref, pr_number, target_branch)
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
        return RebaseOutcome(pr_url=pr_url, success=True)

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
    # Postcondition complaints a kept resolution carries; flagged on the
    # new PR below.
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
        # Edge case: no commits in the range (PR head already in target).
        # Skip rather than push an empty branch.
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


# ---------------------------------------------------------------------------
# Top-level entry points
# ---------------------------------------------------------------------------


def _setup(config: Config, work_dir: Path | None, target_branch: str) -> Path:
    """Set up the work repo and fetch origin so the target ref is present."""
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
    """Drive ``releasy rebase --pr <url> --target <branch>``."""
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
    """Drive ``releasy rebase --target <branch>`` (every tracked rebase PR).

    ``only`` (optional) restricts the walk to a single tracked PR
    (matched by URL — source or rebase) or a single feature / group ID.
    """
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
            console.print(
                f"  [green]✓[/green] {o.pr_url} → {o.new_pr_url}{tag}"
            )
        else:
            console.print(
                f"  [red]✗[/red] {o.pr_url}  [red]{o.error}[/red]"
            )
