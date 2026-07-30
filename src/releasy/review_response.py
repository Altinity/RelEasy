"""``releasy refresh --address-review`` — let the AI address PR review feedback.

Given a pull-request URL, this module:

1. Fetches every comment GitHub knows about the PR (issue comments,
   inline review comments, review bodies) via the three REST endpoints
   wrapped in ``github_ops.fetch_pr_comments``, then enriches each with
   GraphQL-only flags (``isMinimized``, ``isResolved``, …) and the PR
   author's login.
2. **Filters those comments before the AI ever sees them.** Only
   comments whose author is in the trusted allowlist
   (``review_response.trusted_reviewers`` in config) survive — and
   only when they aren't hidden, aren't already resolved (inline
   threads) or already-replied-to (top-level by the PR author).
   Optionally narrows further with ``--since <iso>``. Empty
   allowlist → the run refuses to start.
3. Renders a prompt embedding only the surviving comments, inside
   clearly-delimited blocks — so the AI is reading structured data, not
   live PR context.
4. Checks out the PR's head branch locally and invokes Claude, reusing
   the streaming / cost / error-retry machinery from :mod:`ai_resolve`.
5. Verifies the resolver kept history **linear** (new commits only, no
   rewrites) before pushing: HEAD must be a descendant of the pre-run
   tip. Anything else aborts without pushing.
6. Plain (non-force) push — races are a user problem, we refuse to
   clobber someone else's work.

The PR does not need to be tracked in the project's state file. When
it IS, ``last_review_addressed_at`` is stamped on success and used as
an implicit exclusive ``--since`` default on the next run.

Injection-safe by construction: every piece of text Claude sees is
either (a) configuration / CLI input you control, or (b) the body of a
comment authored by someone you listed as trusted. Untrusted comments
are dropped at fetch time, before the prompt is rendered.
"""

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
    _find_transient_api_error,
    _resolve_backend,
    _spawn_claude,
    _write_build_script,
)
from releasy.config import Config, get_github_token
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
    get_origin_repo_slug,
    parse_pr_url,
)


# ---------------------------------------------------------------------------
# Allowlist / time filtering
# ---------------------------------------------------------------------------


def _build_trusted_set(
    config: Config, cli_reviewers: tuple[str, ...],
) -> set[str]:
    """Build the explicit-login allowlist (additive over the association gate).

    All logins are lower-cased so the author comparison is
    case-insensitive (GitHub treats logins as case-insensitive at the
    UI level; the API returns whatever casing the user registered
    with). Empty strings are dropped. May be empty — the
    ``trusted_associations`` gate handles the common case on its own,
    so we no longer require this list to be non-empty before the run.
    """
    out: set[str] = set()
    for login in list(config.review_response.trusted_reviewers) + list(cli_reviewers):
        s = (login or "").strip().lower()
        if s:
            out.add(s)
    return out


def _build_trusted_associations(config: Config) -> set[str]:
    """Normalise the configured trusted ``author_association`` values.

    Returns an upper-cased set of associations whose authors we trust
    (default: ``{OWNER, MEMBER, COLLABORATOR}``). GitHub sets the
    field server-side, so it's a tamper-proof "is this person a
    maintainer of this repo / org?" signal — much sturdier than a
    hand-maintained login list.
    """
    out: set[str] = set()
    for a in config.review_response.trusted_associations:
        up = (a or "").strip().upper()
        if up:
            out.add(up)
    return out


@dataclass
class _SinceFilter:
    """Resolved ``--since`` cutoff: timestamp + comparison mode.

    ``exclusive=True`` means the filter keeps comments with
    ``created_at > cutoff`` (used for comment-URL input — intent is
    "everything after *this* comment"). ``exclusive=False`` keeps
    ``created_at >= cutoff`` (used for ISO input — the literal boundary
    the user named).
    """
    cutoff: str
    exclusive: bool


# GitHub's three fragment shapes for the three comment kinds. Order
# doesn't matter — we only extract the numeric id; which API the
# comment originated from is irrelevant because we already fetched all
# three kinds for this PR and match by id alone.
_COMMENT_FRAGMENT_RE = re.compile(
    r"#(?:issuecomment-|discussion_r|pullrequestreview-)(\d+)\b",
    re.IGNORECASE,
)


def _parse_since_spec(since: str | None) -> tuple[str, str] | None:
    """Classify ``--since`` input as either a comment URL or an ISO string.

    Returns ``("url", <url>)`` or ``("iso", <iso>)`` — the URL form is
    resolved to a concrete timestamp later (via :func:`_resolve_since`)
    once the PR's comments have been fetched. Raises ``ValueError`` on
    garbage input so the CLI can surface a clean message.

    URL form requires a fragment GitHub actually emits for comments
    (``#issuecomment-…``, ``#discussion_r…``, ``#pullrequestreview-…``);
    without a fragment we can't tell which comment the user meant.
    """
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
    """Turn a parsed spec into a concrete cutoff + comparison mode.

    URL specs look up the referenced comment in ``comments`` (the
    already-fetched list for the PR) and use its ``created_at`` as an
    **exclusive** lower bound: "everything strictly after this one". If
    the referenced comment isn't in the list, we fail loud — it
    probably means the URL points at a different PR.

    ISO specs use the given string as an **inclusive** lower bound,
    since that matches the literal meaning of "since <timestamp>".
    """
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
    """Drop untrusted / too-old / hidden / already-answered comments.

    Filtering is applied in this order so the stats accurately reflect
    which gate dropped each comment:

      1. Untrusted author              → ``"untrusted"``
         (kept only when the author's ``author_association`` is in
          ``trusted_associations`` OR their login is in
          ``trusted_logins``; the two sources combine additively.)
      2. Older than ``--since``        → ``"too_old"``
      3. Hidden (minimized / outdated) → ``"hidden"``
      4. Already addressed             → ``"addressed"``
         (inline: thread ``isResolved=true``; issue / review-body:
          a later comment by ``pr_author`` exists)

    The "addressed" gate is the key change for ``refresh
    --address-review``: we don't want the AI to re-think comments the
    author already replied to. ``pr_author`` is matched
    case-insensitively; when empty we skip the issue/review side of the
    check (treat them as still needing attention) so we never drop a
    comment based on missing data.
    """
    kept: list[PRComment] = []
    dropped = {"untrusted": 0, "too_old": 0, "hidden": 0, "addressed": 0}

    author_lc = (pr_author or "").lower()
    # Pre-compute "earliest created_at of a PR-author comment AFTER this
    # one" by scanning once: ascending iteration means once we see a PR
    # author comment we know every earlier non-author comment is
    # answered by it. We use ``id`` as a tiebreaker because GitHub
    # records second-resolution timestamps and bursts can share one.
    pr_author_marks: list[tuple[str, int]] = []
    if author_lc:
        for c in comments:
            if (c.author or "").lower() == author_lc:
                pr_author_marks.append((c.created_at or "", c.id))

    def _has_later_pr_author_reply(c: PRComment) -> bool:
        if not author_lc:
            return False
        # A "reply" is any later top-level comment by the PR author.
        # Comparing on (created_at, id) keeps the ordering stable when
        # GitHub returns the same timestamp for two adjacent posts.
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
            # GitHub's per-thread resolution is the source of truth here:
            # if a maintainer ticked "Resolve conversation" the AI has
            # nothing to do.
            if c.is_resolved is True:
                dropped["addressed"] += 1
                continue
        else:
            # Top-level discussion has no thread, so we approximate
            # "answered" as "PR author posted anything after it".
            if _has_later_pr_author_reply(c):
                dropped["addressed"] += 1
                continue
        kept.append(c)
    return kept, dropped


# ---------------------------------------------------------------------------
# Opportunistic state tracking (stateful mode only)
# ---------------------------------------------------------------------------


def _same_pr_url(a: str | None, b: str | None) -> bool:
    """Compare two GitHub PR URLs for "same PR" regardless of cosmetic diffs.

    GitHub emits PR URLs without trailing slashes, but hand-copied links
    sometimes carry ``#foo`` fragments, ``?diff=split`` query strings,
    or the literal ``.git`` suffix on the repo segment. We strip all of
    that and compare the canonical ``owner/repo#number`` tuple.
    """
    if not a or not b:
        return False
    pa = parse_pr_url(a)
    pb = parse_pr_url(b)
    if pa is None or pb is None:
        return False
    return (pa[0].lower(), pa[1].lower(), pa[2]) == (
        pb[0].lower(), pb[1].lower(), pb[2],
    )


def _load_tracking_state(
    config: Config, pr_url: str,
) -> tuple[PipelineState | None, str | None]:
    """Try to find the feature that owns ``pr_url`` in the state file.

    Returns ``(state, feature_id)``:

    - ``(state, fid)`` when the PR is tracked (its URL matches a
      feature's ``rebase_pr_url``). Callers mutate
      ``state.features[fid]`` and save.
    - ``(state, None)`` when state loaded but the PR isn't tracked.
    - ``(None, None)`` when state couldn't be loaded. Either way,
      :func:`address_review` silently falls back to stateless.

    Never raises — an unreadable / collision-tripped state file is not
    a reason to fail a stateless-by-design command.
    """
    # ``--stateless`` callers construct a Config via make_stateless_config
    # specifically to skip persistence. Honour that here so we don't
    # accidentally read a stale ``_stateless.state.yaml`` from a previous
    # run.
    from releasy.config import is_stateless
    if is_stateless(config):
        return None, None
    try:
        state = load_state(config)
    except Exception:
        return None, None
    for fid, fs in state.features.items():
        if _same_pr_url(fs.rebase_pr_url, pr_url):
            return state, fid
    return state, None


def _utc_now_iso() -> str:
    """``datetime.now(UTC).isoformat()`` — kept here so the import only
    happens in stateful runs."""
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()


def _record_review_addressed(
    config: Config,
    state: PipelineState,
    feature_id: str,
    when_iso: str,
) -> None:
    """Stamp ``last_review_addressed_at`` on ``feature_id`` and persist.

    Best-effort: any persistence failure is logged but doesn't fail the
    run. The address-review outcome (commits pushed) is the source of
    truth; state timestamps are just a re-run ergonomics aid.
    """
    fs = state.features.get(feature_id)
    if fs is None:
        return
    fs.last_review_addressed_at = when_iso
    if config.dry_run:
        return
    try:
        save_state(state, config)
    except Exception as exc:  # pragma: no cover — defensive
        console.print(
            f"  [yellow]![/yellow] failed to persist "
            f"last_review_addressed_at for {feature_id}: {exc}"
        )


# ---------------------------------------------------------------------------
# Prompt rendering
# ---------------------------------------------------------------------------


def _render_comment_block(c: PRComment, index: int) -> str:
    """One comment → one markdown block for the prompt.

    Bodies are fenced with a marker (``---BEGIN…---END``) so an AI
    reading the prompt has an unambiguous delimiter for "where the
    reviewer's text starts and ends" — makes it harder for a malicious
    comment body to smuggle prompt instructions that look like section
    headers.
    """
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
            # Fence the diff hunk separately so the reviewer's body
            # below it retains its own fence. Strip a possible trailing
            # newline to keep the block tight.
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
    raw = config.review_response.prompt_file
    prompt_path = Path(raw)
    if not prompt_path.is_absolute():
        prompt_path = (config.repo_dir / prompt_path).resolve()
    if not prompt_path.exists():
        raise FileNotFoundError(
            f"review_response prompt template not found: {prompt_path}. "
            "Set review_response.prompt_file in config, or copy the "
            "bundled prompts/address_review.md alongside config.yaml."
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
        "build_log": ".releasy/build.log",
        "build_command": config.ai_resolve.build_command,
        "reply_section": reply_section,
        "summary_section": summary_section,
    }

    def _replace(match: re.Match[str]) -> str:
        key = match.group(1)
        return placeholders.get(key, match.group(0))

    return re.sub(r"\{([a-zA-Z_][a-zA-Z0-9_]*)\}", _replace, template)


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


@dataclass
class AddressReviewResult:
    success: bool
    error: str | None = None
    comments_considered: int = 0
    commits_added: int = 0
    pushed: bool = False
    cost_usd: float | None = None


def _fetch_pr_head(pr_url: str) -> tuple[str, str, str, str, int] | None:
    """Look up the PR's head branch / head repo / base branch / head sha.

    Returns ``(head_ref, head_repo_slug, base_ref, head_sha, number)`` or
    ``None`` if the lookup fails — caller surfaces the error.
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
    """Drive one address-review run end-to-end for a single PR.

    Called by :mod:`releasy.refresh` when ``--address-review`` is set
    (per-tracked-PR in the multi-PR walk, or once for a single PR via
    ``refresh --pr <url>``). Returns an :class:`AddressReviewResult`
    describing what happened so the CLI can pick an exit code. Never
    raises; all failure modes collapse into ``success=False`` with a
    human-readable ``error``.
    """
    trusted_logins = _build_trusted_set(config, cli_reviewers)
    trusted_associations = _build_trusted_associations(config)
    if not trusted_logins and not trusted_associations:
        # Both gates empty would feed *every* commenter to the AI —
        # exactly the injection surface we're trying to avoid. Refuse.
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

    # Opportunistic state lookup: if this PR is a rebase PR RelEasy
    # itself opened, the matching FeatureState carries
    # ``last_review_addressed_at`` from the last run and we use it as an
    # implicit (exclusive) --since default when the CLI didn't pass one.
    # Misses (no state file, no matching feature) are silent — the
    # command is stateless by design and simply falls back to "consider
    # every comment" when we can't find a prior timestamp.
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
        return AddressReviewResult(
            success=True, comments_considered=0,
        )

    _print_comment_summary(filtered)

    if dry_run:
        console.print(
            "\n[yellow]--dry-run: skipping AI invocation "
            "and push.[/yellow]"
        )
        return AddressReviewResult(
            success=True, comments_considered=len(filtered),
        )

    head = _fetch_pr_head(pr_url)
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

    # Late import to avoid a circular dep: pipeline imports nothing
    # from here but owns the canonical work-repo setup helper we want
    # to reuse (identical fetch / submodule logic as `refresh`).
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

    # Refresh the remote pointer so we pick up any in-flight updates
    # the PR author pushed after our initial _setup_repo fetch.
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
        # Not fatal — the PR author may have pushed between fetches —
        # but worth surfacing so the user knows the AI is operating on
        # a slightly newer tip than GitHub showed at --pr lookup time.
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

    # Build wrapper script is harmless even if the AI doesn't build —
    # the prompt tells it `bash .releasy/build.sh` is available, so we
    # must materialise the file. Reuses ai_resolve's writer verbatim.
    try:
        _write_build_script(repo_path, config.ai_resolve.build_command)
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

    # Build the claude argv with the review-response flavour of
    # allowed_tools / extra_args. We reuse ``_build_claude_argv`` by
    # swapping the AIResolveConfig-shaped view it consumes.
    class _ResolveShim:
        """Feed ``_build_claude_argv`` the review-response command / tools.

        That helper reads ``config.ai_resolve.{command,allowed_tools,extra_args}``
        to compose the argv; constructing a tiny shim here avoids
        mutating the real config for one call while keeping the
        streaming / tool-allow-list wiring identical to the
        conflict-resolve path.
        """
        command = config.review_response.command
        allowed_tools = config.review_response.allowed_tools
        extra_args = config.review_response.extra_args

    class _ConfigShim:
        ai_resolve = _ResolveShim
        ai_model = config.ai_model
        ai_effort = config.ai_effort
        ai_backend = config.ai_backend
        ai_api = config.ai_api

    argv = _build_claude_argv(_ConfigShim)  # type: ignore[arg-type]
    api = _build_api_spec(_ConfigShim)  # type: ignore[arg-type]

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

    if timed_out:
        return AddressReviewResult(
            success=False,
            error=(
                f"claude timed out after "
                f"{config.review_response.timeout_seconds}s"
            ),
            comments_considered=len(filtered),
            cost_usd=cost_usd,
        )

    assistant_text = _extract_assistant_text(output)
    tail = assistant_text.strip().splitlines()[-40:] if assistant_text.strip() else []

    if any(line.strip() == "UNRESOLVED" for line in tail):
        return AddressReviewResult(
            success=False,
            error="claude reported UNRESOLVED",
            comments_considered=len(filtered),
            cost_usd=cost_usd,
        )

    if exit_code != 0:
        transient = _find_transient_api_error(output)
        suffix = f" (transient API error: {transient})" if transient else ""
        return AddressReviewResult(
            success=False,
            error=f"claude exited with code {exit_code}{suffix}",
            comments_considered=len(filtered),
            cost_usd=cost_usd,
        )

    # --- Postcondition: working tree clean -----------------------------------
    if is_operation_in_progress(repo_path):
        return AddressReviewResult(
            success=False,
            error=(
                "git operation still in progress after claude exited — "
                "nothing pushed."
            ),
            comments_considered=len(filtered),
            cost_usd=cost_usd,
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
            comments_considered=len(filtered),
            cost_usd=cost_usd,
        )

    # --- Postcondition: linear history ---------------------------------------
    new_head = run_git(
        ["rev-parse", "--verify", "HEAD"], repo_path, check=False,
    )
    if new_head.returncode != 0:
        return AddressReviewResult(
            success=False,
            error="could not resolve HEAD after claude exited",
            comments_considered=len(filtered),
            cost_usd=cost_usd,
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
        return AddressReviewResult(
            success=True,
            comments_considered=len(filtered),
            commits_added=0,
            cost_usd=cost_usd,
        )

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
            comments_considered=len(filtered),
            cost_usd=cost_usd,
        )

    count_res = run_git(
        ["rev-list", "--count", f"{start_sha}..{new_sha}"],
        repo_path, check=False,
    )
    try:
        commits_added = int((count_res.stdout or "0").strip())
    except ValueError:
        commits_added = 0

    # --- Push ----------------------------------------------------------------
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
            comments_considered=len(filtered),
            commits_added=commits_added,
            cost_usd=cost_usd,
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

    return AddressReviewResult(
        success=True,
        comments_considered=len(filtered),
        commits_added=commits_added,
        pushed=True,
        cost_usd=cost_usd,
    )
