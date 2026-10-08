"""``releasy refresh --address-review``: let the AI address trusted PR review
comments with new commits only (untrusted comments never reach the prompt)."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from releasy.termlog import console

from releasy.ai_resolve import (
    _backend_label,
    _build_api_spec,
    _build_claude_argv,
    _exhaustion_kwargs,
    _extract_assistant_text,
    _extract_cost_usd,
    _fill_placeholders,
    _find_transient_api_error,
    _prompt_path,
    _resolve_backend,
    _section_config,
    _spawn_claude,
    _write_build_script,
    build_log_path,
)
from releasy.config import Config
from releasy.state import PipelineState, load_state, save_state
from releasy.git_ops import (
    fetch_remote,
    is_ancestor,
    is_operation_in_progress,
    remote_branch_exists,
    run_git,
    stash_and_clean,
)
from releasy.github_ops import (
    PRComment,
    fetch_pr_comments,
    fetch_pr_head,
    get_origin_repo_slug,
    parse_pr_url,
    same_pr_url,
)


def _build_trusted_set(
    config: Config, cli_reviewers: tuple[str, ...],
) -> set[str]:
    """Lower-cased trusted logins from config + CLI (may be empty)."""
    out: set[str] = set()
    for login in list(config.review_response.trusted_reviewers) + list(cli_reviewers):
        s = (login or "").strip().lower()
        if s:
            out.add(s)
    return out


def _build_trusted_associations(config: Config) -> set[str]:
    """Upper-cased trusted ``author_association`` values from config."""
    out: set[str] = set()
    for a in config.review_response.trusted_associations:
        up = (a or "").strip().upper()
        if up:
            out.add(up)
    return out


@dataclass
class _SinceFilter:
    """Resolved ``--since`` cutoff; ``exclusive`` keeps only ``created_at > cutoff``."""
    cutoff: str
    exclusive: bool


# GitHub's comment-link fragments for issue, inline and review comments.
_COMMENT_FRAGMENT_RE = re.compile(
    r"#(?:issuecomment-|discussion_r|pullrequestreview-)(\d+)\b",
    re.IGNORECASE,
)


def _parse_since_spec(since: str | None) -> tuple[str, str] | None:
    """Classify ``--since`` as ``("url", url)`` or ``("iso", iso)``; ValueError on garbage."""
    if not since:
        return None
    s = since.strip()
    if s.lower().startswith(("http://", "https://")):
        if not _COMMENT_FRAGMENT_RE.search(s):
            raise ValueError(
                f"--since URL {since!r} has no comment fragment. Expected "
                "something like `…/pull/123#issuecomment-456`, "
                "`…#discussion_r456`, or `…#pullrequestreview-456`. "
                "Copy the link from the comment's timestamp on GitHub."
            )
        return ("url", s)

    from datetime import datetime

    normalised = s[:-1] + "+00:00" if s.endswith("Z") else s
    try:
        datetime.fromisoformat(normalised)
    except ValueError as exc:
        raise ValueError(
            f"--since value {since!r} is neither a GitHub comment URL "
            "nor a valid ISO-8601 timestamp (e.g. 2026-04-24T10:00:00Z). "
            "Reason: " + str(exc)
        )
    return ("iso", s)


def _resolve_since(
    spec: tuple[str, str] | None,
    comments: list[PRComment],
) -> _SinceFilter | None:
    """Comment-URL specs become an exclusive cutoff at that comment; ISO ones inclusive."""
    if spec is None:
        return None
    kind, value = spec
    if kind == "iso":
        return _SinceFilter(cutoff=value, exclusive=False)

    m = _COMMENT_FRAGMENT_RE.search(value)
    if not m:  # pragma: no cover — already validated in _parse_since_spec
        raise ValueError(
            f"--since URL {value!r} has no recognisable comment fragment."
        )
    target_id = int(m.group(1))
    for c in comments:
        if c.id == target_id:
            if not c.created_at:
                raise ValueError(
                    f"--since URL {value!r} matches comment id "
                    f"{target_id} but GitHub reported no created_at for "
                    "it — refusing to use an unknown cutoff."
                )
            return _SinceFilter(cutoff=c.created_at, exclusive=True)
    raise ValueError(
        f"--since URL {value!r} points at comment id {target_id}, but "
        "no such comment was found on this PR. Double-check the URL "
        "belongs to the same PR as --pr, and that the comment still "
        "exists."
    )


def _filter_comments(
    comments: list[PRComment],
    trusted_logins: set[str],
    trusted_associations: set[str],
    since: _SinceFilter | None,
    pr_author: str | None = None,
) -> tuple[list[PRComment], dict[str, int]]:
    """Drop untrusted / too-old / hidden / already-addressed comments.

    Returns the kept comments and per-gate drop counts. A top-level
    comment counts as addressed when ``pr_author`` posted after it.
    """
    kept: list[PRComment] = []
    dropped = {"untrusted": 0, "too_old": 0, "hidden": 0, "addressed": 0}

    author_lc = (pr_author or "").lower()
    pr_author_marks: list[tuple[str, int]] = []
    if author_lc:
        for c in comments:
            if (c.author or "").lower() == author_lc:
                pr_author_marks.append((c.created_at or "", c.id))

    def _has_later_pr_author_reply(c: PRComment) -> bool:
        if not author_lc:
            return False
        # ``id`` breaks ties: GitHub timestamps have second resolution.
        c_key = (c.created_at or "", c.id)
        return any(mark > c_key for mark in pr_author_marks)

    for c in comments:
        is_trusted = (
            (c.author or "").lower() in trusted_logins
            or (c.author_association or "").upper() in trusted_associations
        )
        if not is_trusted:
            dropped["untrusted"] += 1
            continue
        if since is not None and c.created_at:
            cmp_ok = (
                c.created_at > since.cutoff if since.exclusive
                else c.created_at >= since.cutoff
            )
            if not cmp_ok:
                dropped["too_old"] += 1
                continue
        if c.is_minimized:
            dropped["hidden"] += 1
            continue
        if c.kind == "inline":
            if c.is_resolved is True:
                dropped["addressed"] += 1
                continue
        else:
            if _has_later_pr_author_reply(c):
                dropped["addressed"] += 1
                continue
        kept.append(c)
    return kept, dropped


def _load_tracking_state(
    config: Config, pr_url: str,
) -> tuple[PipelineState | None, str | None]:
    """``(state, fid)`` of the feature whose rebase PR is ``pr_url``; never raises.

    ``fid`` is None when untracked; ``state`` is None when stateless or unreadable.
    """
    from releasy.config import is_stateless
    if is_stateless(config):
        return None, None
    try:
        state = load_state(config)
    except Exception:
        return None, None
    for fid, fs in state.features.items():
        if same_pr_url(fs.rebase_pr_url, pr_url):
            return state, fid
    return state, None


def _utc_now_iso() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()


def _record_review_addressed(
    config: Config,
    state: PipelineState,
    feature_id: str,
    when_iso: str,
) -> None:
    """Stamp ``last_review_addressed_at`` on ``feature_id`` and persist (best-effort)."""
    fs = state.features.get(feature_id)
    if fs is None:
        return
    fs.last_review_addressed_at = when_iso
    _persist_best_effort(config, state, feature_id, "last_review_addressed_at")


def _record_review_cost(
    config: Config,
    state: PipelineState,
    feature_id: str,
    cost_usd: float,
) -> None:
    """Add ``cost_usd`` to ``feature_id``'s ``ai_cost_usd`` and persist (best-effort)."""
    fs = state.features.get(feature_id)
    if fs is None:
        return
    fs.ai_cost_usd = (fs.ai_cost_usd or 0.0) + cost_usd
    _persist_best_effort(config, state, feature_id, "ai_cost_usd")


def _persist_best_effort(
    config: Config, state: PipelineState, feature_id: str, what: str,
) -> None:
    if config.dry_run:
        return
    try:
        save_state(state, config)
    except Exception as exc:  # pragma: no cover — defensive
        console.print(
            f"  [yellow]![/yellow] failed to persist "
            f"{what} for {feature_id}: {exc}"
        )


def _render_comment_block(c: PRComment, index: int) -> str:
    """One comment as a prompt block; the body is fenced with BEGIN/END markers."""
    header = f"### Comment #{index} — {c.kind}"
    lines = [header, ""]
    lines.append(f"- Author: @{c.author or 'unknown'}")
    lines.append(f"- Posted: {c.created_at or '?'}")
    lines.append(f"- URL: {c.url}")
    if c.kind == "inline":
        if c.path:
            loc = c.path + (f":{c.line}" if c.line else "")
            lines.append(f"- File: `{loc}`")
        if c.in_reply_to_id:
            lines.append(f"- Reply to comment id: {c.in_reply_to_id}")
        if c.diff_hunk:
            lines.append("- Diff hunk:")
            lines.append("```diff")
            lines.append(c.diff_hunk.rstrip())
            lines.append("```")
    if c.kind == "review" and c.review_state:
        lines.append(f"- Review state: {c.review_state}")
    lines.append("")
    lines.append(f"---BEGIN COMMENT #{index} BODY---")
    lines.append(c.body.rstrip())
    lines.append(f"---END COMMENT #{index} BODY---")
    return "\n".join(lines)


def _render_prompt(
    config: Config,
    repo_path: Path,
    pr_url: str,
    pr_number: int,
    pr_branch: str,
    base_branch: str,
    comments: list[PRComment],
    reply_to_non_addressable: bool,
    post_summary_comment: bool,
) -> str:
    """Load the prompt template and substitute the per-run placeholders."""
    prompt_path = _prompt_path(config, config.review_response.prompt_file)
    if not prompt_path.exists():
        raise FileNotFoundError(
            f"review_response prompt template not found: {prompt_path}. "
            "Set review_response.prompt_file in config."
        )
    template = prompt_path.read_text(encoding="utf-8")

    comment_blocks = "\n\n".join(
        _render_comment_block(c, i + 1) for i, c in enumerate(comments)
    ) or "_(no comments — this should have been caught earlier)_"

    repo_slug = get_origin_repo_slug(config) or "<unknown>"

    reply_section = (
        "**Enabled.** For every comment you classify as ALREADY DONE, "
        "OUT OF SCOPE, or MISUNDERSTANDING, post a reply — see "
        '"Replying to non-actionable comments" below for the exact '
        "commands and body format. ADDRESSABLE comments are answered "
        "by the commit that fixes them (mention the comment URL in "
        "the commit message) — do not post a reply for them too."
        if reply_to_non_addressable else
        "**Disabled** for this run (the operator passed --no-reply or "
        "turned off review_response.reply_to_non_addressable). Do "
        "**not** post any per-comment replies; list declined comments "
        "in the final stdout narration only."
    )

    summary_section = (
        "After your per-comment work, post exactly **one** summary "
        "comment via "
        "`gh pr comment {pr_url} --body '<text>'` describing what you "
        "changed and which comments you declined (and why)."
        if post_summary_comment else
        "Do not post a separate summary comment — the narration in "
        "your stdout (and the per-comment replies, if any) is enough."
    )
    summary_section = summary_section.replace("{pr_url}", pr_url)

    placeholders = {
        "repo_slug": repo_slug,
        "cwd": str(repo_path),
        "pr_url": pr_url,
        "pr_number": str(pr_number),
        "pr_branch": pr_branch,
        "base_branch": base_branch,
        "comment_blocks": comment_blocks,
        "max_iterations": str(config.review_response.max_iterations),
        "build_script": ".releasy/build.sh",
        "build_log": build_log_path(pr_branch),
        "build_command": config.ai_resolve.build_command,
        "reply_section": reply_section,
        "summary_section": summary_section,
    }

    return _fill_placeholders(template, placeholders)


def _print_comment_summary(comments: list[PRComment]) -> None:
    console.print(
        f"\n[bold]Addressing {len(comments)} trusted comment(s):[/bold]",
    )
    for i, c in enumerate(comments, start=1):
        locator = ""
        if c.kind == "inline" and c.path:
            locator = f" [dim]{c.path}"
            if c.line:
                locator += f":{c.line}"
            locator += "[/dim]"
        snippet = c.body.strip().splitlines()[0] if c.body.strip() else ""
        if len(snippet) > 100:
            snippet = snippet[:99] + "…"
        console.print(
            f"  [cyan]#{i}[/cyan] [magenta]{c.kind}[/magenta] "
            f"@{c.author or 'unknown'} [dim]{c.created_at}[/dim]{locator}"
        )
        if snippet:
            console.print(f"      [dim]> {snippet}[/dim]")


@dataclass
class AddressReviewResult:
    success: bool
    error: str | None = None


def address_review(
    config: Config,
    pr_url: str,
    *,
    cli_reviewers: tuple[str, ...] = (),
    since_iso: str | None = None,
    work_dir: Path | None = None,
    dry_run: bool = False,
    reply_override: bool | None = None,
) -> AddressReviewResult:
    """Run one address-review pass on a PR; failures return ``success=False``."""
    trusted_logins = _build_trusted_set(config, cli_reviewers)
    trusted_associations = _build_trusted_associations(config)
    if not trusted_logins and not trusted_associations:
        return AddressReviewResult(
            success=False,
            error=(
                "No trust gate configured. Set "
                "review_response.trusted_associations (defaults to "
                "OWNER/MEMBER/COLLABORATOR) and/or "
                "review_response.trusted_reviewers. RelEasy refuses to "
                "process comments without any trust signal."
            ),
        )

    try:
        since_spec = _parse_since_spec(since_iso)
    except ValueError as exc:
        return AddressReviewResult(success=False, error=str(exc))

    parsed = parse_pr_url(pr_url)
    if parsed is None:
        return AddressReviewResult(
            success=False, error=f"Could not parse PR URL: {pr_url!r}",
        )

    origin_slug = get_origin_repo_slug(config)
    if not origin_slug:
        return AddressReviewResult(
            success=False,
            error=(
                "Cannot determine origin repo slug from config — check "
                f"origin.remote ({config.origin.remote!r})."
            ),
        )

    owner, repo, number = parsed
    if f"{owner}/{repo}".lower() != origin_slug.lower():
        return AddressReviewResult(
            success=False,
            error=(
                f"--pr points at {owner}/{repo}#{number} but this "
                f"project's origin is {origin_slug}. RelEasy only pushes "
                "to origin, so addressing review on a non-origin PR "
                "would be half-useful at best. Bail."
            ),
        )

    # A tracked PR's last_review_addressed_at is the default (exclusive) --since.
    state, tracked_feature_id = _load_tracking_state(config, pr_url)

    state_auto_since: _SinceFilter | None = None
    if since_spec is None and tracked_feature_id is not None:
        fs = state.features[tracked_feature_id]  # type: ignore[union-attr]
        if fs.last_review_addressed_at:
            state_auto_since = _SinceFilter(
                cutoff=fs.last_review_addressed_at, exclusive=True,
            )
            console.print(
                f"[dim]Auto --since from state: "
                f"{fs.last_review_addressed_at} (exclusive) — previous "
                f"address-review run on feature {tracked_feature_id}. "
                "Pass --since explicitly to override.[/dim]"
            )

    console.print(
        f"\n[bold]Fetching comments on[/bold] [cyan]{pr_url}[/cyan]…",
    )
    fetched = fetch_pr_comments(config, pr_url)
    if fetched.error:
        return AddressReviewResult(success=False, error=fetched.error)
    comments = fetched.comments

    try:
        since_filter = (
            _resolve_since(since_spec, comments)
            if since_spec is not None
            else state_auto_since
        )
    except ValueError as exc:
        return AddressReviewResult(success=False, error=str(exc))

    filtered, dropped = _filter_comments(
        comments, trusted_logins, trusted_associations, since_filter,
        pr_author=fetched.pr_author,
    )
    console.print(
        f"  [dim]{len(comments)} total, "
        f"{len(filtered)} actionable, "
        f"{dropped['untrusted']} untrusted, "
        f"{dropped['too_old']} too old (--since), "
        f"{dropped['hidden']} hidden, "
        f"{dropped['addressed']} already addressed.[/dim]"
    )
    if not filtered:
        console.print("[green]Nothing to address — exiting cleanly.[/green]")
        return AddressReviewResult(success=True)

    _print_comment_summary(filtered)

    if dry_run:
        console.print(
            "\n[yellow]--dry-run: skipping AI invocation "
            "and push.[/yellow]"
        )
        return AddressReviewResult(success=True)

    head = fetch_pr_head(pr_url)
    if head is None:
        return AddressReviewResult(
            success=False,
            error=(
                "Could not look up PR head / base refs — check token "
                "scope or the PR URL."
            ),
        )
    head_ref, head_repo, base_ref, head_sha_expected, pr_number = head

    if head_repo.lower() != origin_slug.lower():
        return AddressReviewResult(
            success=False,
            error=(
                f"PR head branch lives on {head_repo}, but RelEasy only "
                f"pushes to origin ({origin_slug}). Can't address review "
                "on a PR from a fork."
            ),
        )

    from releasy.pipeline import _setup_repo

    repo_path = _setup_repo(config, work_dir, base_ref)

    if is_operation_in_progress(repo_path):
        return AddressReviewResult(
            success=False,
            error=(
                f"A git operation (cherry-pick/merge/rebase) is already "
                f"in progress in {repo_path}. Resolve or abort it before "
                "running address-review."
            ),
        )

    remote = config.origin.remote_name
    if not remote_branch_exists(repo_path, head_ref, remote):
        return AddressReviewResult(
            success=False,
            error=(
                f"PR head branch {head_ref!r} is not visible on "
                f"{remote} (already fetched). Was the branch deleted?"
            ),
        )

    fetch_remote(repo_path, remote)

    stash_and_clean(repo_path)
    co = run_git(
        ["checkout", "-B", head_ref, f"{remote}/{head_ref}"],
        repo_path, check=False,
    )
    if co.returncode != 0:
        return AddressReviewResult(
            success=False,
            error=f"Could not check out {head_ref}: {co.stderr.strip()}",
        )

    start_head = run_git(
        ["rev-parse", "--verify", "HEAD"], repo_path, check=False,
    )
    if start_head.returncode != 0:
        return AddressReviewResult(
            success=False, error="Could not resolve HEAD after checkout",
        )
    start_sha = start_head.stdout.strip()

    if head_sha_expected and start_sha != head_sha_expected:
        console.print(
            f"  [yellow]Note: local tip {start_sha[:10]} differs from "
            f"PR head {head_sha_expected[:10]} reported by GitHub "
            "(PR branch moved since fetch).[/yellow]"
        )

    _api, backend_error = _resolve_backend(
        config, config.review_response.command,
        list(config.review_response.allowed_tools),
    )
    if backend_error:
        return AddressReviewResult(
            success=False,
            error=(
                f"{backend_error} — install Claude Code, adjust "
                "review_response.command, or fix the ai_api settings."
            ),
        )

    # The prompt advertises .releasy/build.sh, so it must exist.
    try:
        _write_build_script(
            repo_path, config.ai_resolve.build_command,
            build_log_path(head_ref),
        )
    except OSError as exc:
        return AddressReviewResult(
            success=False,
            error=f"Could not write build wrapper: {exc}",
        )

    reply_enabled = (
        reply_override
        if reply_override is not None
        else config.review_response.reply_to_non_addressable
    )

    try:
        prompt = _render_prompt(
            config, repo_path, pr_url, pr_number, head_ref, base_ref,
            filtered,
            reply_to_non_addressable=reply_enabled,
            post_summary_comment=config.review_response.post_summary_comment,
        )
    except FileNotFoundError as exc:
        return AddressReviewResult(success=False, error=str(exc))

    section = _section_config(
        config,
        config.review_response.command,
        config.review_response.allowed_tools,
        config.review_response.extra_args,
    )
    argv = _build_claude_argv(section)  # type: ignore[arg-type]
    api = _build_api_spec(section)  # type: ignore[arg-type]

    console.print(
        f"\n[magenta]\U0001f916 invoking "
        f"{_backend_label(config, config.review_response.command)} "
        f"(timeout {config.review_response.timeout_seconds}s, "
        f"max {config.review_response.max_iterations} iterations)"
        "[/magenta]"
    )

    exit_code, output, timed_out = _spawn_claude(
        argv, repo_path, config.review_response.timeout_seconds,
        prompt=prompt, api=api, **_exhaustion_kwargs(config),
    )
    cost_usd = _extract_cost_usd(output)
    if cost_usd and state is not None and tracked_feature_id is not None:
        _record_review_cost(config, state, tracked_feature_id, cost_usd)

    if timed_out:
        return AddressReviewResult(
            success=False,
            error=(
                f"claude timed out after "
                f"{config.review_response.timeout_seconds}s"
            ),
        )

    assistant_text = _extract_assistant_text(output)
    tail = assistant_text.strip().splitlines()[-40:] if assistant_text.strip() else []

    if any(line.strip() == "UNRESOLVED" for line in tail):
        return AddressReviewResult(
            success=False,
            error="claude reported UNRESOLVED",
        )

    if exit_code != 0:
        transient = _find_transient_api_error(output)
        suffix = f" (transient API error: {transient})" if transient else ""
        return AddressReviewResult(
            success=False,
            error=f"claude exited with code {exit_code}{suffix}",
        )

    if is_operation_in_progress(repo_path):
        return AddressReviewResult(
            success=False,
            error=(
                "git operation still in progress after claude exited — "
                "nothing pushed."
            ),
        )

    porc = run_git(
        ["status", "--porcelain", "--untracked-files=no"],
        repo_path, check=False,
    )
    if porc.stdout.strip():
        dirty = ", ".join(
            line[3:] for line in porc.stdout.splitlines()[:5]
        )
        return AddressReviewResult(
            success=False,
            error=f"working tree not clean after claude: {dirty}",
        )

    new_head = run_git(
        ["rev-parse", "--verify", "HEAD"], repo_path, check=False,
    )
    if new_head.returncode != 0:
        return AddressReviewResult(
            success=False,
            error="could not resolve HEAD after claude exited",
        )
    new_sha = new_head.stdout.strip()

    if new_sha == start_sha:
        console.print(
            "\n[yellow]AI made no commits — nothing to push. "
            "See its narration above for what it decided (or didn't)."
            "[/yellow]"
        )
        if state is not None and tracked_feature_id is not None:
            _record_review_addressed(
                config, state, tracked_feature_id, _utc_now_iso(),
            )
        return AddressReviewResult(success=True)

    ancestor = is_ancestor(repo_path, start_sha, new_sha)
    if ancestor is not True:
        return AddressReviewResult(
            success=False,
            error=(
                f"Non-linear history: start {start_sha[:10]} is not an "
                f"ancestor of new HEAD {new_sha[:10]} — the AI "
                "rewrote/amended something it wasn't supposed to. "
                "Refusing to push; local branch left at the rewritten "
                "state for inspection."
            ),
        )

    count_res = run_git(
        ["rev-list", "--count", f"{start_sha}..{new_sha}"],
        repo_path, check=False,
    )
    try:
        commits_added = int((count_res.stdout or "0").strip())
    except ValueError:
        commits_added = 0

    push = run_git(["push", remote, head_ref], repo_path, check=False)
    if push.returncode != 0:
        for line in (push.stderr or "").strip().splitlines()[:5]:
            console.print(f"    [dim]{line}[/dim]")
        return AddressReviewResult(
            success=False,
            error=(
                "push failed (origin moved? auth?). The resolved commits "
                f"are kept locally at HEAD={new_sha[:10]} — re-run to "
                "retry."
            ),
        )

    cost_note = (
        f" [dim](cost: ${cost_usd:.4f})[/dim]"
        if cost_usd is not None else ""
    )
    console.print(
        f"\n[green]✓[/green] Pushed {commits_added} new commit(s) to "
        f"[cyan]{head_ref}[/cyan]{cost_note}"
    )

    if state is not None and tracked_feature_id is not None:
        _record_review_addressed(
            config, state, tracked_feature_id, _utc_now_iso(),
        )

    return AddressReviewResult(success=True)
