"""One-off, stateless cross-repo cherry-pick.

No config file, no state file, no lock, no project board. The user hands
us an origin remote, a target branch, and a GitHub URL pointing at a PR /
commit / tag in any public repo; we clone, pick, optionally have Claude
resolve conflicts, push, and optionally open a PR back against origin.

This module deliberately does NOT touch any of the persistence layers
that the rest of releasy uses (``state.py``, ``locks.py``, project
sync). Anything calling those would couple the one-off flow to the
multi-project bookkeeping it's meant to side-step.
"""

from __future__ import annotations

import re
import secrets
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from releasy.termlog import console

from releasy.config import Config, PortMode, make_stateless_config
from releasy.git_ops import (
    OperationResult,
    abort_in_progress_op,
    branch_exists,
    cherry_pick_sha,
    create_branch_from_ref,
    ensure_work_repo,
    fetch_commit,
    fetch_pr_ref,
    fetch_remote,
    force_push,
    is_operation_in_progress,
    local_branch_exists,
    resolve_remote_tag,
    run_git,
    stash_and_clean,
)
from releasy.github_ops import (
    PRInfo,
    create_pull_request,
    fetch_pr_by_number,
    get_origin_repo_slug,
    parse_source_url,
    slug_to_https_url,
)


FORMATTING_SECTION_HEADER = "CI/CD Options"


SourceKind = Literal["pr", "commit", "tag"]


# ---------------------------------------------------------------------------
# Public dataclasses
# ---------------------------------------------------------------------------


@dataclass
class StatelessOptions:
    """All inputs the stateless cherry-pick command needs.

    Constructed by the CLI from flags; passed through unchanged so the
    pipeline below has a single, easy-to-test entry point.
    """
    origin: str           # origin remote URL (ssh / https / slug-form)
    target: str           # base branch on origin
    source_url: str       # PR / commit / tag URL
    work_dir: Path | None = None
    branch_name: str | None = None
    push: bool = True
    open_pr: bool = False
    resolve_conflicts: bool = False
    # Port direction for the AI resolver. ``backport`` unlocks the
    # adapt/drop latitude (bucket 0 + bucket 2) so the resolver adjusts
    # code rather than pulling prereqs; ``forward_port`` keeps the strict
    # "report MISSING_PREREQS" behavior. Cross-repo one-offs are usually
    # backports, hence the default.
    mode: PortMode = "backport"
    build_command: str = ""
    claude_command: str = "claude"
    prompt_file: str | None = None
    timeout_seconds: int = 7200
    max_iterations: int = 5
    formatting_example_url: str | None = None


@dataclass
class StatelessResult:
    """Outcome reported back to the CLI for exit-code shaping."""
    success: bool
    branch_name: str | None = None
    pr_url: str | None = None
    conflict_files: list[str] | None = None
    error: str | None = None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _short_id() -> str:
    return secrets.token_hex(3)


def _short_ref(kind: SourceKind, ident: str) -> str:
    """A filesystem-friendly token derived from the source identifier.

    Used as the human-readable middle slug of the auto-generated branch
    name (``releasy/port/<short>-<6hex>``). Tags can contain ``/`` and
    other shell-unfriendly characters, so we sanitise.
    """
    if kind == "pr":
        return f"pr-{ident}"
    if kind == "commit":
        return ident[:8]
    safe = "".join(c if (c.isalnum() or c in "._-") else "-" for c in ident)
    return f"tag-{safe[:32]}"


def _default_branch_name(kind: SourceKind, ident: str) -> str:
    return f"releasy/port/{_short_ref(kind, ident)}-{_short_id()}"


def _git_show_subject_body(repo_path: Path, sha: str) -> tuple[str, str]:
    """Return ``(subject, body)`` of the commit at ``sha`` (best effort).

    Falls back to ``("", "")`` when the commit isn't reachable yet (the
    caller fetches it before invoking us, but we don't want this helper
    to crash the flow if something weird happens).
    """
    subj = run_git(
        ["log", "-1", "--format=%s", sha], repo_path, check=False,
    )
    body = run_git(
        ["log", "-1", "--format=%b", sha], repo_path, check=False,
    )
    return (
        subj.stdout.strip() if subj.returncode == 0 else "",
        body.stdout.strip() if body.returncode == 0 else "",
    )


def _synthesize_pr_info(
    *,
    slug: str,
    source_url: str,
    sha: str,
    title: str,
    body: str,
    is_merge_commit: bool,
) -> PRInfo:
    """Build a PRInfo for non-PR sources so AI resolve / PR-body code reuses
    the existing :class:`PRInfo` plumbing without a parallel type.

    ``number`` is set to 0 for non-PR sources — it never gets used as an
    actual PR number (no GitHub call accepts a synthesized one) and is
    only echoed back in log lines / prompt placeholders, where the ``url``
    is what reviewers actually click.
    """
    return PRInfo(
        number=0,
        title=title or sha[:12],
        body=body or "",
        state="merged",
        merge_commit_sha=sha if is_merge_commit else None,
        head_sha=sha,
        url=source_url,
        repo_slug=slug,
    )


def _fetch_and_pick_pr(
    config: Config,
    repo_path: Path,
    slug: str,
    pr_number: int,
) -> tuple[OperationResult, PRInfo | None]:
    """Resolve a PR URL → merge commit (or PR ref) → cherry-pick with -m 1.

    Returns ``(result, pr_info)`` where ``pr_info`` is the GitHub-side
    PRInfo (or ``None`` if we couldn't fetch it). The result's
    ``conflict_files`` populates only when ``success is False`` and
    git left the working tree mid-conflict.
    """
    pr = fetch_pr_by_number(config, pr_number, slug=slug)
    if pr is None:
        return (
            OperationResult(
                success=False, conflict_files=[],
                error_message=f"could not fetch PR {slug}#{pr_number}",
            ),
            None,
        )

    fetch_url = slug_to_https_url(slug)

    if pr.state == "merged" and pr.merge_commit_sha:
        if not fetch_commit(repo_path, fetch_url, pr.merge_commit_sha):
            return (
                OperationResult(
                    success=False, conflict_files=[],
                    error_message=(
                        f"could not fetch merge commit "
                        f"{pr.merge_commit_sha[:12]} from {slug}"
                    ),
                ),
                pr,
            )
        return (
            cherry_pick_sha(
                repo_path, pr.merge_commit_sha,
                mainline=1, abort_on_conflict=False,
            ),
            pr,
        )

    if not fetch_pr_ref(repo_path, fetch_url, pr_number):
        return (
            OperationResult(
                success=False, conflict_files=[],
                error_message=f"could not fetch PR #{pr_number} from {slug}",
            ),
            pr,
        )
    return (
        cherry_pick_sha(
            repo_path, "FETCH_HEAD",
            mainline=1, abort_on_conflict=False,
        ),
        pr,
    )


def _fetch_and_pick_commit(
    repo_path: Path,
    slug: str,
    sha: str,
) -> OperationResult:
    """Fetch a single commit by SHA from the source repo and cherry-pick it.

    Always uses plain ``cherry-pick <sha>`` (no ``-m``); a merge commit
    passed via the ``/commit/<sha>`` URL would fail here, which is the
    intended signal for "use the PR URL instead — git can't tell which
    parent you want without ``-m``".
    """
    fetch_url = slug_to_https_url(slug)
    if not fetch_commit(repo_path, fetch_url, sha):
        return OperationResult(
            success=False, conflict_files=[],
            error_message=f"could not fetch commit {sha[:12]} from {slug}",
        )
    return cherry_pick_sha(
        repo_path, sha, mainline=None, abort_on_conflict=False,
    )


def _fetch_and_pick_tag(
    repo_path: Path,
    slug: str,
    tag: str,
) -> tuple[OperationResult, str | None]:
    """Resolve ``tag`` on the source repo → commit SHA → cherry-pick.

    Returns ``(result, sha)`` so the caller can synthesize a PRInfo
    referencing the actual commit (the tag itself is just a label).
    """
    fetch_url = slug_to_https_url(slug)
    sha = resolve_remote_tag(repo_path, fetch_url, tag)
    if not sha:
        return (
            OperationResult(
                success=False, conflict_files=[],
                error_message=f"could not resolve tag {tag!r} on {slug}",
            ),
            None,
        )
    if not fetch_commit(repo_path, fetch_url, sha):
        return (
            OperationResult(
                success=False, conflict_files=[],
                error_message=f"could not fetch tag commit {sha[:12]} from {slug}",
            ),
            sha,
        )
    return (
        cherry_pick_sha(
            repo_path, sha, mainline=None, abort_on_conflict=False,
        ),
        sha,
    )


def _try_ai_resolve(
    config: Config,
    repo_path: Path,
    branch: str,
    target: str,
    pr_info: PRInfo,
    conflict_files: list[str],
    mode: PortMode = "backport",
) -> tuple[bool, str | None]:
    """Invoke Claude on a conflicted cherry-pick. Returns ``(ok, error)``."""
    from releasy.ai_resolve import AIResolveContext, attempt_ai_resolve

    ctx = AIResolveContext(
        port_branch=branch,
        base_branch=target,
        source_pr=pr_info,
        conflict_files=conflict_files,
        operation="cherry-pick",
        mode=mode,
    )
    result = attempt_ai_resolve(config, repo_path, ctx)

    if result.cost_usd is not None:
        console.print(
            f"    [dim](claude cost: ${result.cost_usd:.4f})[/dim]"
        )

    if result.success:
        iters = (
            f" (iterations: {result.iterations})" if result.iterations else ""
        )
        console.print(f"    [green]✓[/green] AI resolved conflict{iters}")
        return True, None

    reason = result.error or (
        "timed out" if result.timed_out else "unknown failure"
    )
    return False, reason


# ---------------------------------------------------------------------------
# PR title / body
# ---------------------------------------------------------------------------


_HEADER_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*$")
_CHECKBOX_RE = re.compile(r"^\s*[-*+]\s*\[[ xX]\]")


def _extract_markdown_section(body: str, header_text: str) -> str | None:
    """Return the section from ``header_text`` to EOF, trimmed after the
    last ``- [ ]`` / ``- [x]`` checkbox if any exist. ``None`` if the
    header isn't found. Header match is case-insensitive."""
    target = header_text.strip().lower()
    lines = body.splitlines()
    start: int | None = None
    for i, line in enumerate(lines):
        m = _HEADER_RE.match(line)
        if m and m.group(2).strip().lower() == target:
            start = i
            break
    if start is None:
        return None

    section = lines[start:]
    last_checkbox = -1
    for j, line in enumerate(section):
        if _CHECKBOX_RE.match(line):
            last_checkbox = j
    if last_checkbox >= 0:
        section = section[: last_checkbox + 1]
    return "\n".join(section).rstrip()


def _fetch_formatting_section(
    config: Config, url: str,
) -> tuple[str | None, str | None]:
    """Fetch a same-origin PR and return ``(section, error)`` — exactly
    one is non-None. The PR slug must match origin."""
    parsed = parse_source_url(url)
    if parsed is None or parsed[0] != "pr":
        return None, (
            f"--formatting-example must be a PR URL (/pull/N), got: {url!r}"
        )
    _, owner, repo, ident = parsed
    example_slug = f"{owner}/{repo}"
    origin_slug = get_origin_repo_slug(config)
    if origin_slug and example_slug.lower() != origin_slug.lower():
        return None, (
            f"--formatting-example PR must live in the origin repo "
            f"({origin_slug}); got {example_slug}"
        )

    pr = fetch_pr_by_number(
        config, int(ident), slug=example_slug, include_closed=True,
    )
    if pr is None:
        return None, (
            f"could not fetch --formatting-example PR {example_slug}#{ident}"
        )

    section = _extract_markdown_section(
        pr.body or "", FORMATTING_SECTION_HEADER,
    )
    if section is None:
        return None, (
            f"--formatting-example PR {pr.url} has no "
            f"{FORMATTING_SECTION_HEADER!r} section"
        )
    return section, None


def _pr_title(
    kind: SourceKind, slug: str, ident: str, pr_info: PRInfo | None,
) -> str:
    if kind == "pr" and pr_info is not None:
        return f"Cherry-pick: {pr_info.title}"
    if kind == "commit":
        subject = (pr_info.title if pr_info else "") or ident[:12]
        return f"Cherry-pick {ident[:12]}: {subject}"
    if kind == "tag":
        return f"Cherry-pick tag {ident} from {slug}"
    return f"Cherry-pick from {slug}"


def _pr_body(
    kind: SourceKind, source_url: str, pr_info: PRInfo | None,
    *,
    extra_section: str | None = None,
) -> str:
    """Compose the rebase PR body.

    Starts with a one-line "cherry-picked from <url>" so reviewers can
    jump back to the source, then folds the source title / body in
    when we have it. Body is truncated for non-PR commits to avoid
    pasting in a 1000-line conventional-commit dump. ``extra_section``
    is appended verbatim.
    """
    lines: list[str] = [f"Cherry-picked from {source_url}."]
    if pr_info is not None:
        if pr_info.title:
            lines.append("")
            lines.append(f"**{pr_info.title}**")
        body = (pr_info.body or "").strip()
        if body:
            lines.append("")
            lines.append("---")
            lines.append("")
            if kind != "pr" and len(body) > 4000:
                body = body[:4000] + "\n\n_(truncated)_"
            lines.append(body)
    if extra_section:
        lines.append("")
        lines.append("---")
        lines.append("")
        lines.append(extra_section)
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Cleanup
# ---------------------------------------------------------------------------


def _cleanup_failed(
    repo_path: Path, branch: str, target_ref: str,
) -> None:
    """Abort any in-progress git op and delete ``branch`` locally."""
    if is_operation_in_progress(repo_path):
        abort_in_progress_op(repo_path)
    if local_branch_exists(repo_path, branch):
        run_git(["checkout", "--detach", target_ref], repo_path, check=False)
        run_git(["branch", "-D", branch], repo_path, check=False)


# ---------------------------------------------------------------------------
# Main entry
# ---------------------------------------------------------------------------


def run_stateless_cherry_pick(opts: StatelessOptions) -> StatelessResult:
    """Execute the one-off cherry-pick described by ``opts``.

    Returns a ``StatelessResult`` so the CLI can pick the exit code; this
    function never calls ``sys.exit`` itself, which keeps it trivially
    testable end-to-end (with a fake remote / GH token).
    """
    parsed = parse_source_url(opts.source_url)
    if parsed is None:
        return StatelessResult(
            success=False,
            error=(
                f"unrecognised source URL: {opts.source_url!r}. "
                "Expected a GitHub PR (/pull/N), commit (/commit/<sha>), "
                "tag (/releases/tag/<tag>), or tree-ref (/tree/<tag>) URL."
            ),
        )
    kind, owner, repo, ident = parsed
    slug = f"{owner}/{repo}"

    config = make_stateless_config(
        opts.origin,
        work_dir=opts.work_dir,
        push=opts.push,
        auto_pr=opts.open_pr,
        ai_enabled=opts.resolve_conflicts,
        ai_command=opts.claude_command,
        ai_build_command=opts.build_command,
        ai_prompt_file=opts.prompt_file,
        ai_timeout_seconds=opts.timeout_seconds,
        ai_max_iterations=opts.max_iterations,
    )

    wd = config.resolve_work_dir(opts.work_dir)
    console.print(f"[dim]Working directory: {wd}[/dim]")
    console.print(f"[dim]Origin: {opts.origin}[/dim]")
    console.print(
        f"[dim]Source: {kind} {slug} {ident} ({opts.source_url})[/dim]"
    )

    repo_path, _ = ensure_work_repo(config, wd)
    console.print(f"[dim]Repo: {repo_path}[/dim]")

    remote = config.origin.remote_name
    console.print(f"Fetching [cyan]{remote}[/cyan]...", end=" ")
    fetch_remote(repo_path, remote)
    console.print("[green]done[/green]")

    if is_operation_in_progress(repo_path):
        kind_op = abort_in_progress_op(repo_path)
        console.print(
            f"[yellow]↻ Aborted in-progress {kind_op}[/yellow] — "
            "starting from a clean tree."
        )

    if not branch_exists(repo_path, opts.target, remote):
        return StatelessResult(
            success=False,
            error=(
                f"target branch {opts.target!r} does not exist on remote "
                f"{remote!r} ({opts.origin}). Create + push it first."
            ),
        )

    target_ref = f"{remote}/{opts.target}"
    branch = opts.branch_name or _default_branch_name(kind, ident)
    console.print(
        f"\nBranching [cyan]{branch}[/cyan] off [cyan]{target_ref}[/cyan]"
    )
    stash_and_clean(repo_path)
    create_branch_from_ref(repo_path, branch, target_ref)

    pr_info: PRInfo | None = None
    picked_sha: str | None = None
    cp_result: OperationResult

    if kind == "pr":
        cp_result, pr_info = _fetch_and_pick_pr(
            config, repo_path, slug, int(ident),
        )
        if pr_info is not None:
            picked_sha = (
                pr_info.merge_commit_sha or pr_info.head_sha
            )
    elif kind == "commit":
        cp_result = _fetch_and_pick_commit(repo_path, slug, ident)
        picked_sha = ident
        subj, body = _git_show_subject_body(repo_path, ident)
        pr_info = _synthesize_pr_info(
            slug=slug, source_url=opts.source_url, sha=ident,
            title=subj, body=body, is_merge_commit=False,
        )
    else:  # tag
        cp_result, picked_sha = _fetch_and_pick_tag(repo_path, slug, ident)
        if picked_sha:
            subj, body = _git_show_subject_body(repo_path, picked_sha)
            pr_info = _synthesize_pr_info(
                slug=slug, source_url=opts.source_url, sha=picked_sha,
                title=subj or f"tag {ident}", body=body, is_merge_commit=False,
            )

    if not cp_result.success:
        msg = cp_result.error_message or "cherry-pick failed"
        console.print(f"[red]✗[/red] {msg}")
        for cf in cp_result.conflict_files:
            console.print(f"    [red]•[/red] {cf}")

        if not cp_result.conflict_files:
            # Hard failure (couldn't fetch / commit not found / etc.) —
            # nothing to attempt-resolve, just clean up and bail.
            _cleanup_failed(repo_path, branch, target_ref)
            return StatelessResult(
                success=False, branch_name=branch,
                error=msg,
            )

        if opts.resolve_conflicts and pr_info is not None:
            ok, err = _try_ai_resolve(
                config, repo_path, branch, opts.target, pr_info,
                cp_result.conflict_files, opts.mode,
            )
            if not ok:
                _cleanup_failed(repo_path, branch, target_ref)
                return StatelessResult(
                    success=False, branch_name=branch,
                    conflict_files=cp_result.conflict_files,
                    error=f"AI resolve failed: {err}",
                )
            # AI succeeded — fall through to push / PR.
        else:
            _cleanup_failed(repo_path, branch, target_ref)
            return StatelessResult(
                success=False, branch_name=branch,
                conflict_files=cp_result.conflict_files,
                error=(
                    "cherry-pick conflicted — re-run with "
                    "--resolve-conflicts --build-command '<cmd>' to let "
                    "Claude attempt a fix."
                ),
            )

    console.print(f"[green]✓[/green] Cherry-pick applied on [cyan]{branch}[/cyan]")

    if opts.push:
        force_push(repo_path, branch, config)
        console.print(f"[green]✓[/green] Pushed [cyan]{branch}[/cyan] to {remote}")
    else:
        console.print("[dim]Skipping push (--no-push)[/dim]")

    pr_url: str | None = None
    if opts.open_pr:
        if not opts.push:
            console.print(
                "[yellow]![/yellow] --with-pr requires push; skipping PR creation."
            )
        else:
            extra_section: str | None = None
            if opts.formatting_example_url:
                extra_section, err = _fetch_formatting_section(
                    config, opts.formatting_example_url,
                )
                if err:
                    console.print(f"[yellow]![/yellow] {err}")
                else:
                    console.print(
                        f"[green]✓[/green] Copied "
                        f"{FORMATTING_SECTION_HEADER!r} section from "
                        f"[link={opts.formatting_example_url}]"
                        f"formatting example[/link]"
                    )
            title = _pr_title(kind, slug, ident, pr_info)
            body = _pr_body(
                kind, opts.source_url, pr_info,
                extra_section=extra_section,
            )
            pr_url = create_pull_request(
                config, branch, opts.target, title, body,
            )
            if pr_url:
                console.print(
                    f"[green]✓[/green] PR opened: [link={pr_url}]{pr_url}[/link]"
                )
            else:
                console.print(
                    "[yellow]![/yellow] Could not open PR (see warnings above). "
                    "The branch is pushed; open the PR manually."
                )

    return StatelessResult(
        success=True, branch_name=branch, pr_url=pr_url,
    )
