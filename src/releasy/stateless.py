"""One-off cross-repo cherry-pick of a PR / commit / tag onto an origin branch.

Uses no config, state file, lock, or project board.
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


@dataclass
class StatelessOptions:
    origin: str           # origin remote URL (ssh / https / slug-form)
    target: str           # base branch on origin
    source_url: str       # PR / commit / tag URL
    work_dir: Path | None = None
    branch_name: str | None = None
    push: bool = True
    open_pr: bool = False
    resolve_conflicts: bool = False
    # Port direction for the AI resolver; ``backport`` lets it adapt code instead of
    # reporting missing prereqs.
    mode: PortMode = "backport"
    build_command: str = ""
    claude_command: str = "claude"
    # "cli" spawns ``claude_command``; "api" drives the Anthropic API with
    # $ANTHROPIC_API_KEY instead (no config.yaml needed).
    ai_backend: str = "cli"
    prompt_file: str | None = None
    timeout_seconds: int = 7200
    max_iterations: int = 5
    formatting_example_url: str | None = None


@dataclass
class StatelessResult:
    success: bool
    branch_name: str | None = None
    pr_url: str | None = None
    conflict_files: list[str] | None = None
    error: str | None = None


def _short_id() -> str:
    return secrets.token_hex(3)


def _short_ref(kind: SourceKind, ident: str) -> str:
    """Ref-safe slug for the source, used in the default branch name."""
    if kind == "pr":
        return f"pr-{ident}"
    if kind == "commit":
        return ident[:8]
    safe = "".join(c if (c.isalnum() or c in "._-") else "-" for c in ident)
    return f"tag-{safe[:32]}"


def _default_branch_name(kind: SourceKind, ident: str) -> str:
    return f"releasy/port/{_short_ref(kind, ident)}-{_short_id()}"


def _git_show_subject_body(repo_path: Path, sha: str) -> tuple[str, str]:
    """``(subject, body)`` of the commit at ``sha``; empty strings on failure."""
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
    """A :class:`PRInfo` (``number=0``) standing in for a commit / tag source."""
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
    """Cherry-pick a PR's merge commit (or PR merge ref) with ``-m 1``; return ``(result, pr)``."""
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
    """Fetch and cherry-pick a commit without ``-m`` (merge commits need the PR URL)."""
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
    """Resolve ``tag`` on the source repo and cherry-pick its commit; return ``(result, sha)``."""
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
) -> tuple[bool, str | None, list[str]]:
    """Invoke Claude on a conflicted cherry-pick; return ``(ok, error, warnings)``."""
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
        return True, None, list(result.warnings)

    reason = result.error or (
        "timed out" if result.timed_out else "unknown failure"
    )
    return False, reason, []


_HEADER_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*$")
_CHECKBOX_RE = re.compile(r"^\s*[-*+]\s*\[[ xX]\]")


def _extract_markdown_section(body: str, header_text: str) -> str | None:
    """Section from ``header_text`` to EOF, cut after the last checkbox; ``None`` if absent."""
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
    """``(section, error)`` from a same-origin PR's body; exactly one is non-None."""
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


def _read_pr_template(repo_path: Path, target_ref: str) -> str | None:
    res = run_git(
        ["show", f"{target_ref}:.github/PULL_REQUEST_TEMPLATE.md"],
        repo_path, check=False,
    )
    return res.stdout if res.returncode == 0 else None


def _ci_options_section(template_text: str | None) -> str:
    """The template's ``CI/CD Options`` section to EOF, else the bundled default block."""
    from releasy.pipeline import _DEFAULT_CI_CD_OPTIONS_BLOCK

    if template_text:
        lines = template_text.splitlines()
        target = FORMATTING_SECTION_HEADER.strip().lower()
        for i, line in enumerate(lines):
            m = _HEADER_RE.match(line)
            if m and m.group(2).strip().lower() == target:
                return "\n".join(lines[i:]).rstrip()
    return _DEFAULT_CI_CD_OPTIONS_BLOCK.rstrip()


def _build_changelog_block_for_pr(pr_info: PRInfo | None) -> str | None:
    """Source PR's changelog category + entry with attribution, or ``None``."""
    if pr_info is None:
        return None
    from releasy.pipeline import (
        _extract_changelog_category,
        _extract_changelog_entry,
        render_changelog_block,
    )

    body = pr_info.body or ""
    return render_changelog_block(
        _extract_changelog_category(body),
        _extract_changelog_entry(body),
        [pr_info],
    )


def _pr_body(
    source_url: str, pr_info: PRInfo | None, ci_section: str,
) -> str:
    """Provenance line, changelog block, then ``ci_section`` (upstream body is not copied)."""
    lines: list[str] = [f"Cherry-picked from {source_url}."]
    changelog = _build_changelog_block_for_pr(pr_info)
    if changelog:
        lines.append("")
        lines.append(changelog)
    if ci_section:
        lines.append("")
        lines.append(ci_section)
    return "\n".join(lines).rstrip() + "\n"


def _cleanup_failed(
    repo_path: Path, branch: str, target_ref: str,
) -> None:
    if is_operation_in_progress(repo_path):
        abort_in_progress_op(repo_path)
    if local_branch_exists(repo_path, branch):
        run_git(["checkout", "--detach", target_ref], repo_path, check=False)
        run_git(["branch", "-D", branch], repo_path, check=False)


def run_stateless_cherry_pick(opts: StatelessOptions) -> StatelessResult:
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
        ai_backend=opts.ai_backend,
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
    template_text = _read_pr_template(repo_path, target_ref)
    branch = opts.branch_name or _default_branch_name(kind, ident)
    console.print(
        f"\nBranching [cyan]{branch}[/cyan] off [cyan]{target_ref}[/cyan]"
    )
    stash_and_clean(repo_path)
    create_branch_from_ref(repo_path, branch, target_ref)

    pr_info: PRInfo | None = None
    picked_sha: str | None = None
    cp_result: OperationResult
    resolve_warnings: list[str] = []

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
            _cleanup_failed(repo_path, branch, target_ref)
            return StatelessResult(
                success=False, branch_name=branch,
                error=msg,
            )

        if opts.resolve_conflicts and pr_info is not None:
            ok, err, resolve_warnings = _try_ai_resolve(
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
            ci_section = _ci_options_section(template_text)
            if opts.formatting_example_url:
                override, err = _fetch_formatting_section(
                    config, opts.formatting_example_url,
                )
                if err:
                    console.print(
                        f"[yellow]![/yellow] {err} — using target template's "
                        f"{FORMATTING_SECTION_HEADER!r} instead"
                    )
                else:
                    ci_section = override
                    console.print(
                        f"[green]✓[/green] Copied "
                        f"{FORMATTING_SECTION_HEADER!r} section from "
                        f"[link={opts.formatting_example_url}]"
                        f"formatting example[/link]"
                    )
            title = _pr_title(kind, slug, ident, pr_info)
            body = _pr_body(opts.source_url, pr_info, ci_section)
            pr_url = create_pull_request(
                config, branch, opts.target, title, body,
            )
            if pr_url:
                console.print(
                    f"[green]✓[/green] PR opened: [link={pr_url}]{pr_url}[/link]"
                )
                if resolve_warnings:
                    from releasy.ai_resolve import (
                        flag_resolution_warnings_on_pr,
                    )

                    flag_resolution_warnings_on_pr(
                        config, pr_url, resolve_warnings,
                    )
            else:
                console.print(
                    "[yellow]![/yellow] Could not open PR (see warnings above). "
                    "The branch is pushed; open the PR manually."
                )

    return StatelessResult(
        success=True, branch_name=branch, pr_url=pr_url,
    )
