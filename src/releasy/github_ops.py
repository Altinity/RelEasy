"""GitHub operations: project board sync, PR creation, and PR search."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path

import requests
from releasy.termlog import console

from releasy.config import Config, get_github_token
from releasy.state import FeatureState, PipelineState

log = logging.getLogger(__name__)

GRAPHQL_URL = "https://api.github.com/graphql"


# ---------------------------------------------------------------------------
# Remote URL parsing
# ---------------------------------------------------------------------------


def parse_remote_url(url: str) -> tuple[str, str] | None:
    """Extract (owner, repo) from a GitHub remote URL.

    Supports SSH (git@github.com:owner/repo.git) and
    HTTPS (https://github.com/owner/repo.git).
    """
    m = re.match(r"git@github\.com:(.+)/(.+?)(?:\.git)?$", url)
    if m:
        return m.group(1), m.group(2)
    m = re.match(r"https://github\.com/(.+)/(.+?)(?:\.git)?$", url)
    if m:
        return m.group(1), m.group(2)
    return None


def get_origin_repo_slug(config: Config) -> str | None:
    """Return 'owner/repo' for the origin remote from config."""
    parsed = parse_remote_url(config.origin.remote)
    if not parsed:
        return None
    return f"{parsed[0]}/{parsed[1]}"


def require_origin_repo_slug(config: Config) -> str:
    """Return the origin slug or raise — used by every write path.

    RelEasy *only* writes (create/update/label PRs, push branches) to the
    repo configured as ``origin``. If the origin URL can't be parsed, no
    write should be attempted at all — this is the single chokepoint that
    makes that guarantee enforceable.
    """
    slug = get_origin_repo_slug(config)
    if not slug:
        raise ValueError(
            f"Cannot determine origin repo slug from remote "
            f"{config.origin.remote!r}. Refusing to perform any write "
            "operation (PR create/update/label, push) without a valid origin."
        )
    return slug


def _assert_writes_target_origin(
    config: Config, target_slug: str, action: str,
) -> None:
    """Defense-in-depth: refuse to write to anything other than origin.

    All write paths derive their target slug from origin in the first
    place, so this check is tautological in correct code. It's here to
    catch refactor mistakes loudly instead of silently mutating an
    unintended GitHub repo.
    """
    origin_slug = require_origin_repo_slug(config)
    if target_slug != origin_slug:
        raise ValueError(
            f"CRITICAL: refusing to {action} on {target_slug!r}: "
            f"RelEasy only writes to the configured origin "
            f"({origin_slug!r}). This should never happen — please "
            "report it as a bug."
        )


# ---------------------------------------------------------------------------
# Pull Request creation (PyGithub REST API)
# ---------------------------------------------------------------------------


def create_pull_request(
    config: Config,
    head: str,
    base: str,
    title: str,
    body: str,
    *,
    draft: bool = False,
    labels: list[str] | None = None,
) -> str | None:
    """Create a pull request **on the origin repo**.

    By construction this function only ever creates PRs in the configured
    ``origin``. The slug is derived from origin on every call and
    re-validated via ``_assert_writes_target_origin`` before any GitHub
    write — there is deliberately no parameter for naming a different repo.

    Args:
        config: Config with origin remote URL.
        head: Source branch name (in origin).
        base: Target branch name (in origin).
        title: PR title.
        body: PR body (markdown).
        draft: When True, open the PR in draft state.
        labels: Optional labels to attach right after creation. Labels that
            don't already exist on the repo are not auto-created here — call
            ``ensure_label`` first if you need that.

    Returns the PR URL, or None if creation failed.
    """
    if config.dry_run:
        log.info(
            "[dry-run] would open PR on origin: %s → %s — %r", head, base, title,
        )
        return None

    token = get_github_token()
    if not token:
        log.warning("RELEASY_GITHUB_TOKEN not set — cannot create PR")
        return None

    try:
        slug = require_origin_repo_slug(config)
    except ValueError as exc:
        log.warning("%s", exc)
        return None
    _assert_writes_target_origin(config, slug, "create PR")

    try:
        from github import Github, GithubException

        gh = Github(token)
        repo = gh.get_repo(slug)
        pr = repo.create_pull(
            title=title, body=body, head=head, base=base, draft=draft,
        )
        if labels:
            try:
                pr.add_to_labels(*labels)
            except GithubException as exc:
                log.warning(
                    "Created PR %s but failed to add labels %s: %s",
                    pr.html_url, labels, exc,
                )
        return pr.html_url
    except GithubException as exc:
        log.warning("Failed to create PR on %s: %s", slug, exc)
        return None
    except Exception as exc:
        log.warning("Unexpected error creating PR on %s: %s", slug, exc)
        return None


def update_pull_request(
    config: Config,
    pr_number: int,
    title: str | None = None,
    body: str | None = None,
) -> bool:
    """Edit the title and/or body of an existing PR **on the origin repo**.

    Returns True on success, False on failure. A ``None`` argument means
    "leave that field alone". Like ``create_pull_request``, this only ever
    targets the configured origin — no parameter to point elsewhere.
    """
    if config.dry_run:
        log.info("[dry-run] would update PR #%d", pr_number)
        return True

    token = get_github_token()
    if not token:
        log.warning("RELEASY_GITHUB_TOKEN not set — cannot update PR")
        return False

    try:
        slug = require_origin_repo_slug(config)
    except ValueError as exc:
        log.warning("%s", exc)
        return False
    _assert_writes_target_origin(config, slug, f"update PR #{pr_number}")

    kwargs: dict = {}
    if title is not None:
        kwargs["title"] = title
    if body is not None:
        kwargs["body"] = body
    if not kwargs:
        return True

    try:
        from github import Github, GithubException

        gh = Github(token)
        repo = gh.get_repo(slug)
        pr = repo.get_pull(pr_number)
        pr.edit(**kwargs)
        return True
    except GithubException as exc:
        log.warning("Failed to update PR %s#%d: %s", slug, pr_number, exc)
        return False
    except Exception as exc:
        log.warning("Unexpected error updating PR %s#%d: %s", slug, pr_number, exc)
        return False


def close_pull_request(
    config: Config,
    pr_number: int,
    *,
    comment: str | None = None,
) -> bool:
    """Close an open PR **on the origin repo**, optionally leaving a comment.

    Returns True on success (the PR is closed after the call) or when the
    PR was already closed, False on any GitHub failure. Like the other
    write helpers this only ever targets the configured origin.
    """
    if config.dry_run:
        log.info("[dry-run] would close PR #%d", pr_number)
        return True

    token = get_github_token()
    if not token:
        log.warning("RELEASY_GITHUB_TOKEN not set — cannot close PR")
        return False

    try:
        slug = require_origin_repo_slug(config)
    except ValueError as exc:
        log.warning("%s", exc)
        return False
    _assert_writes_target_origin(config, slug, f"close PR #{pr_number}")

    try:
        from github import Github, GithubException

        gh = Github(token)
        repo = gh.get_repo(slug)
        pr = repo.get_pull(pr_number)
        if comment:
            try:
                pr.create_issue_comment(comment)
            except GithubException as exc:
                log.warning(
                    "Failed to post superseded-by comment on %s#%d: %s",
                    slug, pr_number, exc,
                )
        if pr.state == "closed":
            return True
        pr.edit(state="closed")
        return True
    except GithubException as exc:
        log.warning("Failed to close PR %s#%d: %s", slug, pr_number, exc)
        return False
    except Exception as exc:
        log.warning("Unexpected error closing PR %s#%d: %s", slug, pr_number, exc)
        return False


# ---------------------------------------------------------------------------
# Issue I/O (used by `releasy graph discover --open-issue` / `graph update`)
# ---------------------------------------------------------------------------


def create_issue(
    config: Config,
    title: str,
    body: str,
    *,
    labels: list[str] | None = None,
) -> tuple[int, str] | None:
    """Create an issue on origin; return (number, html_url) or None.

    Only writes to the configured origin. Labels must already exist on the
    repo (call ensure_label first). Returns None on failure / dry-run.
    """
    if config.dry_run:
        log.info("[dry-run] would open issue on origin: %r", title)
        return None

    token = get_github_token()
    if not token:
        log.warning("RELEASY_GITHUB_TOKEN not set — cannot create issue")
        return None

    try:
        slug = require_origin_repo_slug(config)
    except ValueError as exc:
        log.warning("%s", exc)
        return None
    _assert_writes_target_origin(config, slug, "create issue")

    try:
        from github import Github, GithubException

        gh = Github(token)
        repo = gh.get_repo(slug)
        issue = repo.create_issue(title=title, body=body)
        if labels:
            try:
                issue.add_to_labels(*labels)
            except GithubException as exc:
                log.warning(
                    "Created issue %s but failed to add labels %s: %s",
                    issue.html_url, labels, exc,
                )
        return issue.number, issue.html_url
    except GithubException as exc:
        log.warning("Failed to create issue on %s: %s", slug, exc)
        return None
    except Exception as exc:
        log.warning("Unexpected error creating issue on %s: %s", slug, exc)
        return None


def update_issue(
    config: Config,
    issue_number: int,
    *,
    title: str | None = None,
    body: str | None = None,
) -> bool | None:
    """Edit an issue's title/body on origin. None args are left alone.

    Returns True on success, False on a transient/other failure, and None
    when the issue no longer exists (HTTP 404) so the caller can recreate it.
    """
    if config.dry_run:
        log.info("[dry-run] would update issue #%d", issue_number)
        return True

    token = get_github_token()
    if not token:
        log.warning("RELEASY_GITHUB_TOKEN not set — cannot update issue")
        return False

    try:
        slug = require_origin_repo_slug(config)
    except ValueError as exc:
        log.warning("%s", exc)
        return False
    _assert_writes_target_origin(config, slug, f"update issue #{issue_number}")

    kwargs: dict = {}
    if title is not None:
        kwargs["title"] = title
    if body is not None:
        kwargs["body"] = body
    if not kwargs:
        return True

    try:
        from github import Github, GithubException

        gh = Github(token)
        repo = gh.get_repo(slug)
        issue = repo.get_issue(issue_number)
        issue.edit(**kwargs)
        return True
    except GithubException as exc:
        if getattr(exc, "status", None) == 404:
            log.warning("Issue %s#%d not found (404)", slug, issue_number)
            return None
        log.warning("Failed to update issue %s#%d: %s", slug, issue_number, exc)
        return False
    except Exception as exc:
        log.warning(
            "Unexpected error updating issue %s#%d: %s", slug, issue_number, exc,
        )
        return False


def add_issue_comment(config: Config, issue_number: int, body: str) -> bool:
    """Post a comment on an origin issue. Returns True on success."""
    if config.dry_run:
        log.info("[dry-run] would comment on issue #%d", issue_number)
        return True

    token = get_github_token()
    if not token:
        log.warning("RELEASY_GITHUB_TOKEN not set — cannot comment on issue")
        return False

    try:
        slug = require_origin_repo_slug(config)
    except ValueError as exc:
        log.warning("%s", exc)
        return False
    _assert_writes_target_origin(config, slug, f"comment on issue #{issue_number}")

    try:
        from github import Github, GithubException

        gh = Github(token)
        repo = gh.get_repo(slug)
        issue = repo.get_issue(issue_number)
        issue.create_comment(body)
        return True
    except GithubException as exc:
        log.warning(
            "Failed to comment on issue %s#%d: %s", slug, issue_number, exc,
        )
        return False
    except Exception as exc:
        log.warning(
            "Unexpected error commenting on issue %s#%d: %s",
            slug, issue_number, exc,
        )
        return False


def create_draft_release(
    config: Config,
    *,
    tag_name: str,
    name: str,
    body: str,
    target_commitish: str | None = None,
) -> str | None:
    """Create a draft GitHub release on the origin repo.

    Returns the release HTML URL on success, ``None`` on failure. The
    release is always created as a draft (``draft=True``); GitHub will
    not create the tag until the draft is published.
    """
    token = get_github_token()
    if not token:
        log.warning("RELEASY_GITHUB_TOKEN not set — cannot create release")
        return None

    try:
        slug = require_origin_repo_slug(config)
    except ValueError as exc:
        log.warning("%s", exc)
        return None
    _assert_writes_target_origin(config, slug, f"create draft release {tag_name!r}")

    payload: dict = {
        "tag_name": tag_name,
        "name": name,
        "body": body,
        "draft": True,
        "prerelease": False,
    }
    if target_commitish:
        payload["target_commitish"] = target_commitish

    status, data = _rest_api(
        "POST", f"/repos/{slug}/releases", payload,
        expected_statuses=(201,),
    )
    if status != 201 or not isinstance(data, dict):
        log.warning("Failed to create draft release %s on %s (status %s)",
                    tag_name, slug, status)
        return None
    return data.get("html_url")


# ---------------------------------------------------------------------------
# PR search by label
# ---------------------------------------------------------------------------


@dataclass
class PRInfo:
    number: int
    title: str
    body: str
    state: str  # "open", "merged", or "closed" (unmerged; see include_closed fetches)
    merge_commit_sha: str | None
    head_sha: str
    url: str
    repo_slug: str  # "owner/repo" — may differ from origin for include_prs / groups
    merged_at: str | None = None  # ISO timestamp of merge
    labels: list[str] = None  # type: ignore[assignment]
    author: str | None = None  # GitHub login of the PR author
    # GitHub's ``mergeable_state`` for OPEN PRs. Set on the lookups used
    # by ``releasy project pull`` so we can decide whether a rebase PR is
    # currently clean or conflicting without a trial merge. Values seen
    # in the wild: "clean", "dirty" (= conflicting), "unstable" (CI red
    # but no conflicts), "blocked" (branch-protection / review wait),
    # "behind" (base moved — needs update), "draft", "unknown" (GitHub
    # still computing). ``None`` when we didn't bother to look it up.
    mergeable_state: str | None = None

    def __post_init__(self) -> None:
        if self.labels is None:
            self.labels = []

    def ref(self) -> tuple[str, str, int]:
        """``(owner, repo, number)`` — the canonical cross-repo identity."""
        owner, repo = self.repo_slug.split("/", 1)
        return owner, repo, self.number


def _pr_author(pr) -> str | None:  # noqa: ANN001 — PyGithub PullRequest
    """Best-effort extraction of the author's GitHub login."""
    try:
        user = pr.user
        if user is not None and getattr(user, "login", None):
            return user.login
    except Exception:  # pragma: no cover — network / permissions
        pass
    return None


def parse_pr_url(url: str) -> tuple[str, str, int] | None:
    """Extract ``(owner, repo, number)`` from a GitHub PR URL.

    Accepts URLs like https://github.com/owner/repo/pull/123 (with an
    optional trailing ``.git`` on the repo segment).
    """
    m = re.match(
        r"https://github\.com/([^/]+)/([^/]+?)(?:\.git)?/pull/(\d+)\b", url,
    )
    if not m:
        return None
    return m.group(1), m.group(2), int(m.group(3))


def parse_source_url(
    url: str,
) -> tuple[str, str, str, str] | None:
    """Classify a GitHub URL as a PR, commit, or tag reference.

    Returns ``(kind, owner, repo, identifier)`` where:

    - ``kind == "pr"``     → identifier is the PR number as a string
    - ``kind == "commit"`` → identifier is the commit SHA (any length)
    - ``kind == "tag"``    → identifier is the tag name

    Recognised URL shapes (trailing ``.git`` on the repo segment is
    tolerated, query strings / fragments are ignored):

    - ``https://github.com/<owner>/<repo>/pull/<N>``
    - ``https://github.com/<owner>/<repo>/commit/<sha>``
    - ``https://github.com/<owner>/<repo>/releases/tag/<tag>``
    - ``https://github.com/<owner>/<repo>/tree/<tag>``     (only when the
      ref is a tag — caller resolves it via ``git ls-remote`` and
      decides if it points at a commit)

    Returns ``None`` if the URL doesn't match any of the above.
    """
    cleaned = url.split("?", 1)[0].split("#", 1)[0]

    m = re.match(
        r"https://github\.com/([^/]+)/([^/]+?)(?:\.git)?/pull/(\d+)\b",
        cleaned,
    )
    if m:
        return "pr", m.group(1), m.group(2), m.group(3)

    m = re.match(
        r"https://github\.com/([^/]+)/([^/]+?)(?:\.git)?/commit/([0-9a-fA-F]{4,40})\b",
        cleaned,
    )
    if m:
        return "commit", m.group(1), m.group(2), m.group(3).lower()

    m = re.match(
        r"https://github\.com/([^/]+)/([^/]+?)(?:\.git)?/releases/tag/(.+?)/?$",
        cleaned,
    )
    if m:
        return "tag", m.group(1), m.group(2), m.group(3)

    m = re.match(
        r"https://github\.com/([^/]+)/([^/]+?)(?:\.git)?/tree/(.+?)/?$",
        cleaned,
    )
    if m:
        return "tag", m.group(1), m.group(2), m.group(3)

    return None


def slug_to_https_url(slug: str) -> str:
    """Build the canonical HTTPS git URL for a ``owner/repo`` slug."""
    return f"https://github.com/{slug}.git"


def fetch_pr_by_number(
    config: Config,
    number: int,
    merged_only: bool = False,
    slug: str | None = None,
    *,
    include_closed: bool = False,
) -> PRInfo | None:
    """Fetch a single PR by number.

    By default fetches from the origin repo. Pass ``slug`` (``"owner/repo"``)
    to fetch a PR from any other public GitHub repo — used for cross-repo
    PR references in ``pr_sources.include_prs`` and ``pr_sources.groups[].prs``.
    """
    token = get_github_token()
    if not token:
        log.warning("RELEASY_GITHUB_TOKEN not set — cannot fetch PR")
        return None

    if slug is None:
        slug = get_origin_repo_slug(config)
        if not slug:
            log.warning("Could not parse origin remote URL: %s", config.origin.remote)
            return None

    try:
        from github import Github, GithubException

        gh = Github(token)
        repo = gh.get_repo(slug)
        pr = repo.get_pull(number)

        if pr.merged:
            pr_state = "merged"
        elif pr.state == "open":
            if merged_only:
                return None
            pr_state = "open"
        elif include_closed and pr.state == "closed":
            pr_state = "closed"
        else:
            return None

        return PRInfo(
            number=pr.number,
            title=pr.title,
            body=pr.body or "",
            state=pr_state,
            merge_commit_sha=pr.merge_commit_sha if pr.merged else None,
            head_sha=pr.head.sha,
            url=pr.html_url,
            repo_slug=slug,
            merged_at=pr.merged_at.isoformat() if pr.merged_at else None,
            labels=[lbl.name for lbl in pr.labels],
            author=_pr_author(pr),
        )
    except GithubException as exc:
        log.warning("Failed to fetch PR %s#%d: %s", slug, number, exc)
        return None
    except Exception as exc:
        log.warning("Unexpected error fetching PR %s#%d: %s", slug, number, exc)
        return None


def fetch_pr_by_url(
    config: Config,
    url: str,
    merged_only: bool = False,
    *,
    include_closed: bool = False,
) -> PRInfo | None:
    """Fetch a PR identified by its full GitHub URL (any public repo)."""
    parsed = parse_pr_url(url)
    if parsed is None:
        log.warning("Could not parse PR URL: %s", url)
        return None
    owner, repo, number = parsed
    return fetch_pr_by_number(
        config,
        number,
        merged_only=merged_only,
        slug=f"{owner}/{repo}",
        include_closed=include_closed,
    )


@dataclass
class PRComment:
    """One comment on a pull request, flattened across GitHub's three APIs.

    ``kind`` is one of:
      - ``"issue"``  — top-level conversation comment (`/issues/<n>/comments`).
      - ``"review"`` — the summary body of a review (`/pulls/<n>/reviews`).
                        Approvals/change-requests with no body text are
                        dropped at fetch time; only reviews with actual
                        prose reach this list.
      - ``"inline"`` — line-level comment on a diff
                        (`/pulls/<n>/comments`), carrying ``path`` +
                        ``line`` + ``diff_hunk``.

    All timestamps are ISO-8601 UTC strings (matching what PyGithub
    emits). ``author`` is the commenter's GitHub login — missing / bot
    authors are represented as an empty string so the trusted-reviewer
    filter simply drops them.

    The last four fields are populated by a follow-up GraphQL fetch
    (REST doesn't expose ``isResolved`` / ``isMinimized``). They default
    to "unknown / visible / not part of a thread" so callers that skip
    the enrichment step still get sensible behaviour.
    """
    id: int
    kind: str
    author: str
    created_at: str
    updated_at: str
    url: str
    body: str
    # GitHub's ``author_association`` for this comment, upper-case
    # (``OWNER``, ``MEMBER``, ``COLLABORATOR``, ``CONTRIBUTOR``,
    # ``FIRST_TIME_CONTRIBUTOR``, ``MANNEQUIN``, ``NONE``, ``""``).
    # Set by GitHub at post time — commenters can't forge it — so it
    # doubles as a trust signal: "is this person a maintainer of the
    # repo / org or just a passer-by?"
    author_association: str = ""
    path: str | None = None
    line: int | None = None
    commit_id: str | None = None
    diff_hunk: str | None = None
    in_reply_to_id: int | None = None
    review_state: str | None = None
    is_minimized: bool = False
    is_resolved: bool | None = None  # None when comment isn't on a thread
    is_outdated: bool = False
    thread_id: str | None = None
    node_id: str | None = None  # GraphQL global id (for minimizeComment)


@dataclass
class PRCommentsResult:
    """Bundle returned by :func:`fetch_pr_comments`.

    ``pr_author`` is the GitHub login of whoever opened the PR — needed
    by the "no PR-author reply" gate when filtering top-level comments
    (we drop a comment if the PR author has posted any later comment,
    treating that as the response).
    """
    comments: list[PRComment]
    pr_author: str
    error: str | None = None


def _safe_iso(value) -> str:  # noqa: ANN001 — PyGithub returns datetime
    """Format a PyGithub-returned datetime as ISO-8601, or ``""`` if missing."""
    if value is None:
        return ""
    try:
        return value.isoformat()
    except Exception:  # pragma: no cover
        return ""


def _comment_author_login(obj) -> str:  # noqa: ANN001
    """Best-effort extraction of a comment author's login (``""`` on miss)."""
    try:
        user = obj.user
        if user is not None and getattr(user, "login", None):
            return user.login
    except Exception:  # pragma: no cover
        pass
    return ""


def _author_association(obj) -> str:  # noqa: ANN001
    """Pull ``author_association`` off a PyGithub comment, upper-cased.

    The value is set by GitHub when the comment is posted and reflects
    the author's relationship to the repo at that moment — ``OWNER``,
    ``MEMBER`` (of the repo's org), ``COLLABORATOR``, ``CONTRIBUTOR``,
    ``FIRST_TIME_CONTRIBUTOR``, ``MANNEQUIN``, or ``NONE``. We
    upper-case for safety (the docs list upper-case constants but the
    API has historically returned a few mixed-case variants) and
    return ``""`` if PyGithub didn't surface the field.

    PyGithub does not expose ``author_association`` as an attribute on
    :class:`PullRequestReview` even though the REST payload includes
    it. Fall back to ``raw_data`` so reviews authored by org members
    don't silently fail the trust gate.
    """
    try:
        v = getattr(obj, "author_association", None)
        if isinstance(v, str) and v:
            return v.upper()
        raw = getattr(obj, "raw_data", None)
        if isinstance(raw, dict):
            v2 = raw.get("author_association")
            if isinstance(v2, str) and v2:
                return v2.upper()
    except Exception:  # pragma: no cover
        pass
    return ""


@dataclass
class _CommentMeta:
    """GraphQL-derived metadata for one comment, keyed by REST databaseId.

    Populated by :func:`_fetch_pr_comment_metadata_gql`. ``is_resolved``
    is ``None`` for comments that aren't part of a review thread (issue
    comments, review-body comments) since the concept doesn't apply to
    them.
    """
    is_minimized: bool = False
    is_resolved: bool | None = None
    is_outdated: bool = False
    thread_id: str | None = None


def _fetch_pr_comment_metadata_gql(
    slug: str, number: int,
) -> tuple[dict[int, _CommentMeta], str]:
    """Pull ``isMinimized`` / ``isResolved`` / ``isOutdated`` + PR author.

    Returns ``({databaseId -> _CommentMeta}, pr_author_login)``. On any
    GraphQL transport failure (no token, API down, missing repo) we
    return ``({}, "")`` — callers degrade to "no metadata, no PR-author
    cross-check", which means the new resolved/no-reply gate doesn't
    apply but the run doesn't crash either.

    We paginate review threads + issue comments; each thread's inner
    comments are capped at 100, which is far above any real PR. If
    you ever see a thread with more comments, GraphQL truncates
    silently and those extra comments stay with default metadata
    (``is_minimized=False``, ``is_resolved=None``) — the worst-case
    failure mode is "consider too many comments", not "miss a
    minimised one".
    """
    out: dict[int, _CommentMeta] = {}
    pr_author = ""
    owner, _, repo = slug.partition("/")

    query = """
    query($owner: String!, $repo: String!, $number: Int!,
          $threadCursor: String, $issueCursor: String) {
      repository(owner: $owner, name: $repo) {
        pullRequest(number: $number) {
          author { login }
          reviewThreads(first: 50, after: $threadCursor) {
            pageInfo { hasNextPage endCursor }
            nodes {
              id
              isResolved
              isOutdated
              comments(first: 100) {
                nodes {
                  databaseId
                  isMinimized
                }
              }
            }
          }
          comments(first: 100, after: $issueCursor) {
            pageInfo { hasNextPage endCursor }
            nodes {
              databaseId
              isMinimized
            }
          }
        }
      }
    }
    """

    thread_cursor: str | None = None
    issue_cursor: str | None = None
    while True:
        data = _gql(query, {
            "owner": owner,
            "repo": repo,
            "number": number,
            "threadCursor": thread_cursor,
            "issueCursor": issue_cursor,
        })
        if not data:
            return {}, ""
        pr = (data.get("repository") or {}).get("pullRequest") or {}
        if not pr_author:
            pr_author = ((pr.get("author") or {}).get("login") or "")

        threads = pr.get("reviewThreads") or {}
        for thread in (threads.get("nodes") or []):
            tid = thread.get("id")
            resolved = bool(thread.get("isResolved"))
            outdated = bool(thread.get("isOutdated"))
            for tc in ((thread.get("comments") or {}).get("nodes") or []):
                db_id = tc.get("databaseId")
                if db_id is None:
                    continue
                out[int(db_id)] = _CommentMeta(
                    is_minimized=bool(tc.get("isMinimized")),
                    is_resolved=resolved,
                    is_outdated=outdated,
                    thread_id=tid,
                )

        issue_comments = pr.get("comments") or {}
        for ic in (issue_comments.get("nodes") or []):
            db_id = ic.get("databaseId")
            if db_id is None:
                continue
            out[int(db_id)] = _CommentMeta(
                is_minimized=bool(ic.get("isMinimized")),
            )

        thread_pi = (threads.get("pageInfo") or {})
        issue_pi = (issue_comments.get("pageInfo") or {})
        # Advance whichever page still has more; stop when both are done.
        next_thread = (
            thread_pi.get("endCursor") if thread_pi.get("hasNextPage")
            else None
        )
        next_issue = (
            issue_pi.get("endCursor") if issue_pi.get("hasNextPage")
            else None
        )
        if not next_thread and not next_issue:
            break
        thread_cursor = next_thread or thread_cursor
        issue_cursor = next_issue or issue_cursor

    return out, pr_author


def fetch_pr_comments(
    config: Config, pr_url: str,
) -> PRCommentsResult:
    """Fetch every comment on ``pr_url`` from the three GitHub APIs.

    Returns a :class:`PRCommentsResult` whose ``error`` field is set on
    any failure (missing token, unparseable URL, GitHub API error); the
    ``comments`` list is empty in that case. The three sources are:

      1. Issue comments (`/issues/<n>/comments`)  — general PR discussion.
      2. Review comments (`/pulls/<n>/comments`)  — inline on diff.
      3. Reviews (`/pulls/<n>/reviews`)           — only those with a
         non-empty body (pure approvals contribute no text).

    Comments are sorted by ``created_at`` (stable) so consumers always
    see them in the order they were posted. After the REST pass we run
    a GraphQL enrichment pass to attach ``is_minimized`` /
    ``is_resolved`` / ``is_outdated`` / ``thread_id`` — REST simply
    doesn't expose those fields, but the unresolved / hidden filters
    in ``releasy refresh --address-review`` need them. GraphQL also
    yields the PR author's login (returned alongside) used by the
    "no PR-author reply" gate.

    The function is read-only — no filtering by author / time / trust
    applied here; that's the caller's responsibility.
    """
    token = get_github_token()
    if not token:
        return PRCommentsResult(
            comments=[],
            pr_author="",
            error="RELEASY_GITHUB_TOKEN not set — cannot fetch PR comments",
        )

    parsed = parse_pr_url(pr_url)
    if parsed is None:
        return PRCommentsResult(
            comments=[],
            pr_author="",
            error=f"Could not parse PR URL: {pr_url!r}",
        )
    owner, repo, number = parsed
    slug = f"{owner}/{repo}"

    try:
        from github import Github, GithubException

        gh = Github(token)
        ghrepo = gh.get_repo(slug)
        pr = ghrepo.get_pull(number)

        out: list[PRComment] = []

        for ic in pr.get_issue_comments():
            out.append(PRComment(
                id=ic.id,
                kind="issue",
                author=_comment_author_login(ic),
                author_association=_author_association(ic),
                created_at=_safe_iso(ic.created_at),
                updated_at=_safe_iso(ic.updated_at),
                url=ic.html_url,
                body=ic.body or "",
            ))

        for rc in pr.get_review_comments():
            # PyGithub exposes ``line`` (file line) and ``original_line``
            # (line in the original diff). Prefer ``line`` when present
            # so outdated threads still resolve to something useful.
            line_num = getattr(rc, "line", None) or getattr(rc, "original_line", None)
            out.append(PRComment(
                id=rc.id,
                kind="inline",
                author=_comment_author_login(rc),
                author_association=_author_association(rc),
                created_at=_safe_iso(rc.created_at),
                updated_at=_safe_iso(rc.updated_at),
                url=rc.html_url,
                body=rc.body or "",
                path=getattr(rc, "path", None),
                line=line_num,
                commit_id=getattr(rc, "commit_id", None),
                diff_hunk=getattr(rc, "diff_hunk", None),
                in_reply_to_id=getattr(rc, "in_reply_to_id", None),
            ))

        for rv in pr.get_reviews():
            body = (rv.body or "").strip()
            if not body:
                # Pure approval / change-request with no prose — nothing
                # to feed the resolver.
                continue
            # PyGithub's Review exposes ``submitted_at`` rather than
            # ``created_at``; fall back to whichever is present.
            created = (
                _safe_iso(getattr(rv, "submitted_at", None))
                or _safe_iso(getattr(rv, "created_at", None))
            )
            out.append(PRComment(
                id=rv.id,
                kind="review",
                author=_comment_author_login(rv),
                author_association=_author_association(rv),
                created_at=created,
                updated_at=created,
                url=rv.html_url,
                body=body,
                review_state=getattr(rv, "state", None),
            ))

        out.sort(key=lambda c: (c.created_at, c.id))

        meta_map, pr_author = _fetch_pr_comment_metadata_gql(slug, number)
        for c in out:
            meta = meta_map.get(c.id)
            if meta is None:
                continue
            c.is_minimized = meta.is_minimized
            c.is_resolved = meta.is_resolved
            c.is_outdated = meta.is_outdated
            c.thread_id = meta.thread_id

        return PRCommentsResult(comments=out, pr_author=pr_author)
    except GithubException as exc:
        return PRCommentsResult(
            comments=[],
            pr_author="",
            error=f"GitHub API error fetching {slug}#{number} comments: {exc}",
        )
    except Exception as exc:
        return PRCommentsResult(
            comments=[],
            pr_author="",
            error=f"Unexpected error fetching {slug}#{number} comments: {exc}",
        )


@dataclass
class IssueCommentsResult:
    """Result of :func:`fetch_issue_comments`. ``error`` set on failure."""
    comments: list[PRComment]
    error: str | None = None


def fetch_issue_comments(
    config: Config, issue_number: int,
) -> IssueCommentsResult:
    """Fetch an origin issue's comments, sorted by created_at. Read-only;
    no trust/time filtering (the caller does that). Reuses PRComment."""
    token = get_github_token()
    if not token:
        return IssueCommentsResult(
            comments=[],
            error="RELEASY_GITHUB_TOKEN not set — cannot fetch issue comments",
        )

    try:
        slug = require_origin_repo_slug(config)
    except ValueError as exc:
        return IssueCommentsResult(comments=[], error=str(exc))

    try:
        from github import Github, GithubException

        gh = Github(token)
        repo = gh.get_repo(slug)
        issue = repo.get_issue(issue_number)

        out: list[PRComment] = []
        for ic in issue.get_comments():
            out.append(PRComment(
                id=ic.id,
                kind="issue",
                author=_comment_author_login(ic),
                author_association=_author_association(ic),
                created_at=_safe_iso(ic.created_at),
                updated_at=_safe_iso(ic.updated_at),
                url=ic.html_url,
                body=ic.body or "",
                node_id=(ic.raw_data or {}).get("node_id"),
            ))
        out.sort(key=lambda c: (c.created_at, c.id))
        return IssueCommentsResult(comments=out)
    except GithubException as exc:
        return IssueCommentsResult(
            comments=[],
            error=f"GitHub API error fetching {slug}#{issue_number} comments: {exc}",
        )
    except Exception as exc:
        return IssueCommentsResult(
            comments=[],
            error=f"Unexpected error fetching {slug}#{issue_number} comments: {exc}",
        )


def pr_ref_label(pr_slug: str, number: int, origin_slug: str | None) -> str:
    """Format a PR reference as ``#N`` for origin and ``owner/repo#N`` otherwise."""
    if origin_slug and pr_slug == origin_slug:
        return f"#{number}"
    return f"{pr_slug}#{number}"


def search_prs_by_labels(
    config: Config,
    labels: list[str],
    merged_only: bool = False,
) -> list[PRInfo]:
    """Search the origin repo for PRs that have ALL specified labels.

    Returns PRs sorted by merge date (earliest first), with open PRs last.
    Skips closed-but-not-merged PRs.
    When merged_only is True, only merged PRs are returned.
    """
    token = get_github_token()
    if not token:
        log.warning("RELEASY_GITHUB_TOKEN not set — cannot search PRs")
        return []

    slug = get_origin_repo_slug(config)
    if not slug:
        log.warning("Could not parse origin remote URL: %s", config.origin.remote)
        return []

    if not labels:
        return []

    try:
        from github import Github, GithubException

        gh = Github(token)
        repo = gh.get_repo(slug)
        results: list[PRInfo] = []

        # GitHub API filters by all labels when given a list, so this
        # already implements AND semantics.
        for issue in repo.get_issues(labels=labels, state="all"):
            if issue.pull_request is None:
                continue
            pr = repo.get_pull(issue.number)
            if pr.merged:
                pr_state = "merged"
            elif pr.state == "open":
                if merged_only:
                    continue
                pr_state = "open"
            else:
                continue  # closed but not merged — skip

            results.append(PRInfo(
                number=pr.number,
                title=pr.title,
                body=pr.body or "",
                state=pr_state,
                merge_commit_sha=pr.merge_commit_sha if pr.merged else None,
                head_sha=pr.head.sha,
                url=pr.html_url,
                repo_slug=slug,
                merged_at=pr.merged_at.isoformat() if pr.merged_at else None,
                labels=[lbl.name for lbl in pr.labels],
                author=_pr_author(pr),
            ))

        # Merged PRs first in merge order, then open PRs by number.
        results.sort(key=lambda p: (p.merged_at or "9999", p.number))
        return results
    except GithubException as exc:
        log.warning("Failed to search PRs by labels %s: %s", labels, exc)
        return []
    except Exception as exc:
        log.warning("Unexpected error searching PRs: %s", exc)
        return []


def build_merged_base_query(
    slug: str,
    base_branch: str,
    *,
    merged_from: str | None = None,
    merged_to: str | None = None,
    exclude_labels: list[str] | None = None,
) -> str:
    """Build the Search-API query for merged PRs whose base is ``base_branch``.

    ``merged_from`` / ``merged_to`` are ISO timestamps bounding ``merged:``.
    The lower bound is EXCLUSIVE (``merged:>from``) so a PR merged at exactly
    the ``--from`` ref's timestamp isn't double-counted with the prior
    release; the upper bound is inclusive. ``exclude_labels`` adds
    ``-label:"<l>"`` terms (forward-ports are dropped here so they're never
    even fetched).
    """
    q = f"repo:{slug} is:pr is:merged base:{base_branch}"
    if merged_from:
        q += f" merged:>{merged_from}"
    if merged_to:
        q += f" merged:<={merged_to}"
    for lbl in exclude_labels or []:
        # Strip embedded quotes so a label can't break out of the term.
        q += f' -label:"{lbl.replace(chr(34), "")}"'
    return q


def search_merged_prs_by_base(
    config: Config,
    base_branch: str,
    *,
    merged_from: str | None = None,
    merged_to: str | None = None,
    exclude_labels: list[str] | None = None,
) -> list[PRInfo]:
    """Merged PRs whose base branch is ``base_branch``, via one Search query.

    Bodies come back inline (no per-PR fetch). ``merged_from`` /
    ``merged_to`` are ISO timestamps bounding ``merged:``. Returns PRInfo
    (``head_sha`` / ``merge_commit_sha`` unset) sorted by number ascending.
    """
    token = get_github_token()
    if not token:
        log.warning("RELEASY_GITHUB_TOKEN not set — cannot search PRs")
        return []
    slug = get_origin_repo_slug(config)
    if not slug:
        log.warning("Could not parse origin remote URL: %s", config.origin.remote)
        return []

    query = build_merged_base_query(
        slug, base_branch,
        merged_from=merged_from, merged_to=merged_to,
        exclude_labels=exclude_labels,
    )
    try:
        from github import Github, GithubException

        gh = Github(token)
        results: list[PRInfo] = []
        for issue in gh.search_issues(query, sort="created", order="asc"):
            if issue.pull_request is None:
                continue
            results.append(PRInfo(
                number=issue.number,
                title=issue.title or "",
                body=issue.body or "",
                state="merged",
                merge_commit_sha=None,
                head_sha="",
                url=issue.html_url,
                repo_slug=slug,
                merged_at=None,
                labels=[lbl.name for lbl in issue.labels],
                author=issue.user.login if issue.user else None,
            ))
        results.sort(key=lambda p: p.number)
        return results
    except GithubException as exc:
        log.warning("Failed to search merged PRs (base %s): %s", base_branch, exc)
        return []
    except Exception as exc:
        log.warning("Unexpected error searching merged PRs: %s", exc)
        return []


# ---------------------------------------------------------------------------
# Labels + PR lookup helpers (REST)
# ---------------------------------------------------------------------------


def ensure_label(
    config: Config,
    name: str,
    color: str = "8B5CF6",
    description: str = "",
) -> bool:
    """Ensure a label exists **on the origin repo**. Idempotent.

    Returns True if the label exists (pre-existing or freshly created).
    """
    if config.dry_run:
        log.info("[dry-run] would ensure label %r on origin", name)
        return True

    token = get_github_token()
    if not token:
        log.warning("RELEASY_GITHUB_TOKEN not set \u2014 cannot ensure label %s", name)
        return False

    try:
        slug = require_origin_repo_slug(config)
    except ValueError as exc:
        log.warning("%s", exc)
        return False
    _assert_writes_target_origin(config, slug, f"ensure label {name!r}")

    try:
        from github import Github, GithubException

        gh = Github(token)
        repo = gh.get_repo(slug)
        try:
            repo.get_label(name)
            return True
        except GithubException as exc:
            if exc.status != 404:
                log.warning("Failed to check label %s: %s", name, exc)
                return False
        try:
            repo.create_label(name=name, color=color, description=description)
            return True
        except GithubException as exc:
            # 422 == already exists (race); treat as success.
            if exc.status == 422:
                return True
            log.warning("Failed to create label %s: %s", name, exc)
            return False
    except Exception as exc:
        log.warning("Unexpected error ensuring label %s: %s", name, exc)
        return False


def add_label_to_pr(config: Config, pr_number: int, label: str) -> bool:
    """Attach a label to a PR **on the origin repo**. Idempotent on repeated calls."""
    if config.dry_run:
        log.info("[dry-run] would add label %r to PR #%d", label, pr_number)
        return True

    token = get_github_token()
    if not token:
        log.warning("RELEASY_GITHUB_TOKEN not set \u2014 cannot label PR #%d", pr_number)
        return False

    try:
        slug = require_origin_repo_slug(config)
    except ValueError as exc:
        log.warning("%s", exc)
        return False
    _assert_writes_target_origin(config, slug, f"label PR #{pr_number}")

    try:
        from github import Github, GithubException

        gh = Github(token)
        repo = gh.get_repo(slug)
        issue = repo.get_issue(pr_number)
        issue.add_to_labels(label)
        return True
    except GithubException as exc:
        log.warning(
            "Failed to label PR %s#%d with %s: %s", slug, pr_number, label, exc,
        )
        return False
    except Exception as exc:
        log.warning(
            "Unexpected error labelling PR %s#%d: %s", slug, pr_number, exc,
        )
        return False


def pr_has_label(config: Config, pr_number: int, label: str) -> bool:
    """Return whether PR ``pr_number`` currently carries ``label`` on origin.

    Returns ``False`` (silently) when we can't even talk to GitHub — a
    missing token / unresolvable origin slug / API error all collapse
    to ``False`` so callers can treat "label is definitely not there"
    and "we don't know" the same way: don't take the
    label-was-present-side-effect.

    Used by the recovery path to decide whether a previously-conflicted
    PR carried ``ai-needs-attention`` (and therefore deserves to be
    promoted to ``ai-resolved`` after a successful retry, mirroring the
    appearance of PRs that landed cleanly on their first run).
    """
    label_lc = label.lower()
    token = get_github_token()
    if not token:
        return False

    slug = get_origin_repo_slug(config)
    if not slug:
        return False

    try:
        from github import Github, GithubException

        gh = Github(token)
        repo = gh.get_repo(slug)
        issue = repo.get_issue(pr_number)
        for lbl in issue.labels:
            if lbl.name.lower() == label_lc:
                return True
        return False
    except GithubException as exc:
        log.warning(
            "Failed to read labels for PR %s#%d: %s", slug, pr_number, exc,
        )
        return False
    except Exception as exc:
        log.warning(
            "Unexpected error reading labels for PR %s#%d: %s",
            slug, pr_number, exc,
        )
        return False


def remove_label_from_pr(config: Config, pr_number: int, label: str) -> bool:
    """Strip a label from a PR **on the origin repo**.

    Returns True when the label is gone after the call (whether we
    removed it or it was already absent), False on any unexpected
    GitHub error. Used to clean up state markers like
    ``ai-needs-attention`` once a previously-conflicted port has been
    re-resolved.
    """
    if config.dry_run:
        log.info(
            "[dry-run] would remove label %r from PR #%d", label, pr_number,
        )
        return True

    token = get_github_token()
    if not token:
        log.warning(
            "RELEASY_GITHUB_TOKEN not set \u2014 cannot remove label "
            "from PR #%d", pr_number,
        )
        return False

    try:
        slug = require_origin_repo_slug(config)
    except ValueError as exc:
        log.warning("%s", exc)
        return False
    _assert_writes_target_origin(
        config, slug, f"remove label from PR #{pr_number}",
    )

    try:
        from github import Github, GithubException

        gh = Github(token)
        repo = gh.get_repo(slug)
        issue = repo.get_issue(pr_number)
        try:
            issue.remove_from_labels(label)
        except GithubException as exc:
            # 404 = the label wasn't on the PR; treat as success.
            if exc.status == 404:
                return True
            raise
        return True
    except GithubException as exc:
        log.warning(
            "Failed to remove label %s from PR %s#%d: %s",
            label, slug, pr_number, exc,
        )
        return False
    except Exception as exc:
        log.warning(
            "Unexpected error removing label from PR %s#%d: %s",
            slug, pr_number, exc,
        )
        return False


def mark_pr_ready_for_review(
    config: Config, pr_number: int,
) -> bool | None:
    """Flip a draft PR to ready-for-review **on the origin repo**.

    Returns:
      * ``True``  — the PR is ready-for-review after this call (we flipped
        it, or it was already non-draft).
      * ``False`` — GitHub rejected the change (label not gone? token
        scope?) — caller may want to log it.
      * ``None``  — couldn't even talk to GitHub (no token, slug
        unresolvable). Distinguished from ``False`` so the caller can
        stay quiet on transient setup gaps without misreporting failure.

    PyGithub's ``PullRequest.mark_ready_for_review()`` wraps the
    GraphQL ``markPullRequestReadyForReview`` mutation and is available
    on the versions we already depend on.
    """
    token = get_github_token()
    if not token:
        return None

    try:
        slug = require_origin_repo_slug(config)
    except ValueError:
        return None
    _assert_writes_target_origin(
        config, slug, f"mark PR #{pr_number} ready for review",
    )

    try:
        from github import Github, GithubException

        gh = Github(token)
        repo = gh.get_repo(slug)
        pr = repo.get_pull(pr_number)
        if not getattr(pr, "draft", False):
            return True
        pr.mark_ready_for_review()
        return True
    except GithubException as exc:
        log.warning(
            "Failed to mark PR %s#%d ready for review: %s",
            slug, pr_number, exc,
        )
        return False
    except Exception as exc:
        log.warning(
            "Unexpected error marking PR %s#%d ready for review: %s",
            slug, pr_number, exc,
        )
        return False


def find_pr_for_branch(
    config: Config, head_branch: str, base: str | None = None,
) -> PRInfo | None:
    """Find the most recent open PR from ``head_branch`` (optionally \u2192 base)."""
    token = get_github_token()
    if not token:
        return None

    slug = get_origin_repo_slug(config)
    if not slug:
        return None

    owner = slug.split("/")[0]

    try:
        from github import Github, GithubException

        gh = Github(token)
        repo = gh.get_repo(slug)
        kwargs: dict = {"state": "open", "head": f"{owner}:{head_branch}"}
        if base:
            kwargs["base"] = base
        for pr in repo.get_pulls(**kwargs):
            return PRInfo(
                number=pr.number,
                title=pr.title,
                body=pr.body or "",
                state="open" if not pr.merged else "merged",
                merge_commit_sha=pr.merge_commit_sha if pr.merged else None,
                head_sha=pr.head.sha,
                url=pr.html_url,
                repo_slug=slug,
                merged_at=pr.merged_at.isoformat() if pr.merged_at else None,
                labels=[lbl.name for lbl in pr.labels],
                author=_pr_author(pr),
                mergeable_state=getattr(pr, "mergeable_state", None),
            )
        return None
    except GithubException as exc:
        log.warning("Failed to look up PR for branch %s: %s", head_branch, exc)
        return None
    except Exception as exc:
        log.warning("Unexpected error looking up PR for branch %s: %s", head_branch, exc)
        return None


def find_open_backport_pr(
    config: Config,
    base_branch: str,
    upstream_number: int,
    upstream_url: str | None = None,
) -> str | None:
    """URL of an open origin backport PR for an upstream PR, else None.

    Secondary idempotency for ``project-backport`` (the deterministic
    branch name is the primary, immediately-consistent check). Matches an
    open PR into ``base_branch`` by the ``Backport of #<n>`` title
    fingerprint, or by an ``upstream_url`` body reference whose title also
    contains "backport" (the title guard rejects umbrella/tracking PRs
    that merely list the URL). GitHub Search is eventually consistent.
    """
    token = get_github_token()
    if not token:
        return None
    slug = get_origin_repo_slug(config)
    if not slug:
        return None

    title_query = (
        f'repo:{slug} is:pr is:open base:{base_branch} '
        f'in:title "Backport of #{upstream_number}"'
    )
    body_query = (
        f'repo:{slug} is:pr is:open base:{base_branch} in:body "{upstream_url}"'
        if upstream_url else None
    )

    try:
        from github import Github, GithubException

        gh = Github(token)
        # Title fingerprint: trust any matching PR (it's the title we mint).
        # Body match: require a "backport"-y title so a tracking PR that
        # merely links the upstream URL doesn't suppress a real port.
        for query, require_backport_title in (
            (title_query, False), (body_query, True),
        ):
            if not query:
                continue
            try:
                for issue in gh.search_issues(query):
                    if issue.pull_request is None:
                        continue
                    if require_backport_title and "backport" not in (issue.title or "").lower():
                        continue
                    return issue.html_url
            except GithubException as exc:
                log.warning("Backport-PR search failed (%s): %s", query, exc)
        return None
    except Exception as exc:
        log.warning("Unexpected error searching for existing backport PR: %s", exc)
        return None


def find_latest_pr_for_branch(
    config: Config, head_branch: str, base: str | None = None,
) -> PRInfo | None:
    """Return the most recent PR (any state) from ``head_branch`` → ``base``.

    Unlike :func:`find_pr_for_branch` which is scoped to open PRs, this
    looks across ``state="all"`` and returns whichever PR was updated
    most recently. ``releasy project pull`` uses it so a rebase PR that's
    already been merged (or closed in favour of a replacement) still
    gets surfaced in reconstructed state — otherwise the feature would
    silently reappear as "never ported" on a fresh checkout.
    """
    token = get_github_token()
    if not token:
        return None

    slug = get_origin_repo_slug(config)
    if not slug:
        return None

    owner = slug.split("/")[0]

    try:
        from github import Github, GithubException

        gh = Github(token)
        repo = gh.get_repo(slug)
        kwargs: dict = {
            "state": "all", "head": f"{owner}:{head_branch}",
            "sort": "updated", "direction": "desc",
        }
        if base:
            kwargs["base"] = base
        for pr in repo.get_pulls(**kwargs):
            pr_state = (
                "merged" if pr.merged
                else ("open" if pr.state == "open" else "closed")
            )
            return PRInfo(
                number=pr.number,
                title=pr.title,
                body=pr.body or "",
                state=pr_state,
                merge_commit_sha=pr.merge_commit_sha if pr.merged else None,
                head_sha=pr.head.sha,
                url=pr.html_url,
                repo_slug=slug,
                merged_at=pr.merged_at.isoformat() if pr.merged_at else None,
                labels=[lbl.name for lbl in pr.labels],
                author=_pr_author(pr),
                mergeable_state=getattr(pr, "mergeable_state", None) if pr_state == "open" else None,
            )
        return None
    except GithubException as exc:
        log.warning("Failed to look up PR history for branch %s: %s", head_branch, exc)
        return None
    except Exception as exc:
        log.warning("Unexpected error looking up PR history for branch %s: %s", head_branch, exc)
        return None


# ---------------------------------------------------------------------------
# Supersede detection helpers
# ---------------------------------------------------------------------------


# Standard footer left by ``git cherry-pick -x`` — always a full 40-char SHA.
CHERRY_PICK_FROM_RE = re.compile(
    r"\(cherry picked from commit ([0-9a-f]{40})\)",
    re.IGNORECASE,
)


def fetch_open_prs_with_commits_to_base(
    config: Config, base_branch: str,
) -> list[tuple[str, list[str]]]:
    """Open PRs on origin targeting ``base_branch`` + each PR's commit messages.

    Single GraphQL round-trip (paginated by 50 PRs / 250 commits per page)
    instead of the N+1 of ``get_pulls`` + per-PR ``get_commits``. Used by
    the supersede sweep, which scans commit messages for ``(cherry picked
    from commit <sha>)`` footers.

    Returns ``[(pr_html_url, [commit_msg, ...]), ...]``. Empty on missing
    token / API failure — supersede detection is best-effort.
    """
    slug = get_origin_repo_slug(config)
    if not slug:
        return []
    owner, _, name = slug.partition("/")
    if not owner or not name:
        return []

    query = """
    query($owner: String!, $name: String!, $base: String!, $after: String) {
      repository(owner: $owner, name: $name) {
        pullRequests(
          first: 50,
          states: OPEN,
          baseRefName: $base,
          after: $after
        ) {
          pageInfo { hasNextPage endCursor }
          nodes {
            url
            commits(first: 250) {
              nodes { commit { message } }
            }
          }
        }
      }
    }
    """
    out: list[tuple[str, list[str]]] = []
    cursor: str | None = None
    while True:
        data = _gql(query, {
            "owner": owner, "name": name,
            "base": base_branch, "after": cursor,
        })
        if not data:
            return out
        try:
            page = data["repository"]["pullRequests"]
        except (KeyError, TypeError):
            return out
        for pr_node in page.get("nodes") or []:
            url = pr_node.get("url") or ""
            commit_nodes = (
                ((pr_node.get("commits") or {}).get("nodes")) or []
            )
            msgs = [
                (cn.get("commit") or {}).get("message") or ""
                for cn in commit_nodes
            ]
            if url:
                out.append((url, msgs))
        page_info = page.get("pageInfo") or {}
        if not page_info.get("hasNextPage"):
            return out
        cursor = page_info.get("endCursor")
        if not cursor:
            return out


# ---------------------------------------------------------------------------
# GitHub Projects v2 integration (GraphQL API)
# ---------------------------------------------------------------------------


def _gql(query: str, variables: dict | None = None) -> dict | None:
    """Execute a GitHub GraphQL query.

    Returns the ``data`` block on success, ``None`` on transport failure or
    when the response carried any GraphQL errors. Returning ``None`` in the
    error case prevents callers from silently treating partial / invalid
    payloads as successes (e.g. a mutation that the API rejected).
    """
    token = get_github_token()
    if not token:
        return None

    try:
        resp = requests.post(
            GRAPHQL_URL,
            json={"query": query, "variables": variables or {}},
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
            timeout=30,
        )
    except requests.RequestException as exc:
        log.warning("GitHub GraphQL transport error: %s", exc)
        return None
    if resp.status_code != 200:
        log.warning("GitHub GraphQL request failed: %s %s", resp.status_code, resp.text)
        return None

    data = resp.json()
    if "errors" in data:
        log.warning("GitHub GraphQL errors: %s", data["errors"])
        return None
    return data.get("data")


def minimize_comment(node_id: str, classifier: str = "OUTDATED") -> bool:
    """Collapse a comment via GraphQL ``minimizeComment``. Returns success.

    ``classifier`` is a ReportedContentClassifiers value (OUTDATED,
    RESOLVED, OFF_TOPIC, DUPLICATE, …). No-op-safe: a comment already
    minimized returns a GraphQL error → ``False`` (logged, not fatal).
    """
    if not node_id:
        return False
    mutation = """
    mutation($id: ID!, $classifier: ReportedContentClassifiers!) {
      minimizeComment(input: {subjectId: $id, classifier: $classifier}) {
        minimizedComment { isMinimized }
      }
    }
    """
    data = _gql(mutation, {"id": node_id, "classifier": classifier})
    if not data:
        return False
    # ``.get(k, {})`` returns None when the key is present with a null value,
    # so chain through ``or {}`` to stay no-op-safe on a null payload.
    payload = (data.get("minimizeComment") or {}).get("minimizedComment") or {}
    return bool(payload.get("isMinimized"))


def _parse_project_url(url: str) -> tuple[str, int, bool] | None:
    """Parse a GitHub Project URL into (owner, number, is_org)."""
    m = re.match(r"https://github\.com/orgs/([^/]+)/projects/(\d+)", url)
    if m:
        return m.group(1), int(m.group(2)), True
    m = re.match(r"https://github\.com/users/([^/]+)/projects/(\d+)", url)
    if m:
        return m.group(1), int(m.group(2)), False
    return None


def _get_project_id(owner: str, number: int, is_org: bool) -> str | None:
    if is_org:
        query = """
        query($owner: String!, $number: Int!) {
          organization(login: $owner) {
            projectV2(number: $number) { id }
          }
        }
        """
    else:
        query = """
        query($owner: String!, $number: Int!) {
          user(login: $owner) {
            projectV2(number: $number) { id }
          }
        }
        """
    data = _gql(query, {"owner": owner, "number": number})
    if not data:
        return None
    try:
        key = "organization" if is_org else "user"
        return data[key]["projectV2"]["id"]
    except (KeyError, TypeError):
        return None


def _list_project_fields(project_id: str) -> list[dict]:
    """Return every field node on a project (any data type).

    Single-select fields carry their ``options``; non-single-select fields
    (NUMBER, TEXT, DATE, …) just have ``id``/``name``/``dataType``. Empty
    list on lookup failure — callers fall back gracefully.
    """
    query = """
    query($projectId: ID!) {
      node(id: $projectId) {
        ... on ProjectV2 {
          fields(first: 50) {
            nodes {
              __typename
              ... on ProjectV2FieldCommon { id name dataType }
              ... on ProjectV2SingleSelectField {
                options { id name color description }
              }
            }
          }
        }
      }
    }
    """
    data = _gql(query, {"projectId": project_id})
    if not data:
        return []
    try:
        return data["node"]["fields"]["nodes"] or []
    except (KeyError, TypeError):
        return []


def _get_status_field(project_id: str) -> tuple[str, dict[str, str], list[dict]] | None:
    """Return (field_id, {lowercase_name: option_id}, [raw_options]) or None.

    Looks for a single-select field named "Status" (case-insensitive). A
    field with that name but a different data type (TEXT / NUMBER /
    DATE) is ignored — RelEasy can only drive a single-select Status.
    """
    for field_node in _list_project_fields(project_id):
        if field_node.get("name", "").lower() != "status":
            continue
        if field_node.get("dataType") and field_node["dataType"] != "SINGLE_SELECT":
            continue
        raw_options = field_node.get("options") or []
        options = {
            opt["name"].lower(): opt["id"]
            for opt in raw_options
        }
        return field_node["id"], options, raw_options
    return None


# Project field names RelEasy owns. Hard-coded to keep board layout stable
# and to make the "find or create" lookups trivial.
AI_COST_FIELD_NAME = "AI Cost"
ASSIGNEE_DEV_FIELD_NAME = "Assignee Dev"
ASSIGNEE_QA_FIELD_NAME = "Assignee QA"


def _find_field_by_name(
    project_id: str, name: str, data_type: str | None = None,
) -> str | None:
    """Return the field id for ``name`` on ``project_id`` (case-insensitive).

    When ``data_type`` is given, also requires the field to be of that
    type — protects against accidentally wiring up to a same-named field
    of the wrong shape (e.g. a TEXT "AI Cost" left over from a hand-edit).
    """
    target = name.lower()
    for f in _list_project_fields(project_id):
        if (f.get("name") or "").lower() != target:
            continue
        if data_type and (f.get("dataType") or "") != data_type:
            continue
        return f.get("id")
    return None


def _create_number_field(project_id: str, name: str) -> str | None:
    """Create a NUMBER field on a project. Returns the field id."""
    mutation = """
    mutation($projectId: ID!, $name: String!) {
      createProjectV2Field(input: {
        projectId: $projectId
        dataType: NUMBER
        name: $name
      }) {
        projectV2Field { ... on ProjectV2Field { id } }
      }
    }
    """
    data = _gql(mutation, {"projectId": project_id, "name": name})
    if not data:
        return None
    try:
        return data["createProjectV2Field"]["projectV2Field"]["id"]
    except (KeyError, TypeError):
        return None


def _get_single_select_field(
    project_id: str, name: str,
) -> tuple[str, dict[str, str], list[dict]] | None:
    """Look up a single-select field by name. Returns ``(field_id,
    {lowercase_name: option_id}, raw_options)`` or ``None``.

    Generalises ``_get_status_field`` for any single-select field (used
    for ``Assignee Dev`` / ``Assignee QA``). A field with the right name
    but a different ``dataType`` is rejected — protects against an
    accidental TEXT field shadowing a SINGLE_SELECT one.
    """
    target = name.lower()
    for field_node in _list_project_fields(project_id):
        if (field_node.get("name") or "").lower() != target:
            continue
        dt = field_node.get("dataType")
        if dt and dt != "SINGLE_SELECT":
            continue
        raw_options = field_node.get("options") or []
        options = {
            opt["name"].lower(): opt["id"] for opt in raw_options
        }
        return field_node["id"], options, raw_options
    return None


# Color used for newly-provisioned Assignee Dev / Assignee QA options.
# GRAY keeps the UI neutral (no implied semantics like "good"/"bad").
_ASSIGNEE_OPTION_COLOR = "GRAY"


def _ensure_assignee_field(
    project_id: str, field_name: str, configured_options: list[str],
) -> tuple[str, dict[str, str]] | None:
    """Find or create a single-select assignee field on the project.

    On first creation the field is provisioned with exactly the
    ``configured_options`` list (each option coloured GRAY). On
    subsequent runs the field is left untouched — RelEasy never
    rewrites the option list, so any options the user added in the
    GitHub UI (and any value assigned to a card on a since-removed
    option) are preserved.

    Returns ``(field_id, {lowercase_option_name: option_id})`` for the
    live field, or ``None`` if the field could neither be found nor
    created (e.g. the token can't write to the project).
    """
    existing = _get_single_select_field(project_id, field_name)
    if existing:
        field_id, options_by_name, _ = existing
        return field_id, options_by_name

    if not configured_options:
        log.warning(
            "Cannot create %r field: no options configured "
            "(notifications.assignee_*_options is empty)", field_name,
        )
        return None

    options_payload = [
        {
            "name": opt,
            "color": _ASSIGNEE_OPTION_COLOR,
            "description": "",
        }
        for opt in configured_options
    ]
    field_id = _create_single_select_field(
        project_id, field_name, options_payload,
    )
    if not field_id:
        return None
    # Re-read so we get the option ids GitHub assigned.
    refreshed = _get_single_select_field(project_id, field_name)
    if not refreshed:
        log.warning(
            "Created field %r but could not re-read its options",
            field_name,
        )
        return field_id, {}
    return refreshed[0], refreshed[1]


def _ensure_ai_cost_field(project_id: str) -> str | None:
    """Find or create the ``AI Cost`` NUMBER field on a project.

    Idempotent: returns the existing field id when one is already present
    with the right type, creates one otherwise. Returns ``None`` only on
    a hard GraphQL failure — callers degrade gracefully (skip the cost
    sync rather than abort the whole project sync).
    """
    existing = _find_field_by_name(project_id, AI_COST_FIELD_NAME, data_type="NUMBER")
    if existing:
        return existing
    return _create_number_field(project_id, AI_COST_FIELD_NAME)


@dataclass
class ProjectBoardCard:
    """One card we read back from the GitHub Project board.

    Produced by :func:`fetch_project_board_snapshot`. Used by ``releasy
    import`` to promote the board to source-of-truth for the two fields
    local state can't recover from PRs alone: the ``Skipped`` decision
    (set by humans via ``releasy skip``) and the cumulative ``AI Cost``
    (billed to Claude and mirrored to the board on every sync).
    """
    item_id: str
    # When the card is a real PR attachment: ``url`` + ``number`` are
    # populated. For DraftIssue fallback cards (features that have no PR
    # yet — e.g. dropped-singleton conflicts) only ``draft_title`` is set.
    pr_url: str | None
    pr_number: int | None
    draft_title: str | None
    # Status option as configured on the board ("Needs Review", "Skipped",
    # …). ``None`` if the card has no Status value or the field is missing.
    status: str | None
    # ``AI Cost`` number field. ``None`` means "never billed" (distinct
    # from 0.0 which RelEasy writes on sync for cards that ran the
    # resolver but paid nothing, though GitHub itself reports unset as
    # None — we preserve that distinction).
    ai_cost_usd: float | None


def _list_project_items_with_fields(project_id: str) -> list[dict]:
    """Paginate every item on a project with its field values attached.

    Companion to :func:`_list_project_items` — same shape, plus a
    ``fieldValues`` node carrying each item's Status option and AI Cost
    number. Broken out as a separate query so we don't slow down the hot
    write-path in :func:`sync_project` (which doesn't need field values
    to match items). ``releasy project pull`` is the sole caller today.
    """
    query = """
    query($projectId: ID!, $cursor: String) {
      node(id: $projectId) {
        ... on ProjectV2 {
          items(first: 100, after: $cursor) {
            pageInfo { hasNextPage endCursor }
            nodes {
              id
              content {
                __typename
                ... on DraftIssue { id title }
                ... on Issue { id number url }
                ... on PullRequest { id number url }
              }
              fieldValues(first: 20) {
                nodes {
                  __typename
                  ... on ProjectV2ItemFieldSingleSelectValue {
                    name
                    field {
                      ... on ProjectV2FieldCommon { name }
                    }
                  }
                  ... on ProjectV2ItemFieldNumberValue {
                    number
                    field {
                      ... on ProjectV2FieldCommon { name }
                    }
                  }
                }
              }
            }
          }
        }
      }
    }
    """
    return _paginate_project_v2_items(project_id, query)


def _paginate_project_v2_items(project_id: str, query: str) -> list[dict]:
    """Run a ProjectV2 ``items(first: 100, after: $cursor)`` query to
    exhaustion and return the raw item nodes. ``query`` must shape its
    ``items`` connection with ``pageInfo { hasNextPage endCursor }``.
    """
    nodes: list[dict] = []
    cursor: str | None = None
    while True:
        data = _gql(query, {"projectId": project_id, "cursor": cursor})
        if not data:
            return nodes
        try:
            page = data["node"]["items"]
        except (KeyError, TypeError):
            return nodes
        nodes.extend(page.get("nodes") or [])
        info = page.get("pageInfo") or {}
        if not info.get("hasNextPage"):
            return nodes
        cursor = info.get("endCursor")
        if not cursor:
            return nodes


def list_project_items_for_backport(project_id: str) -> list[dict]:
    """Project items with content repo slug + named field values.

    Like :func:`_list_project_items_with_fields` but also returns each
    content item's repository slug (upstream vs origin) and reads TEXT
    field values (``Port Versions`` may be TEXT). Each dict: ``item_id``,
    ``content_typename``, ``pr_number``, ``pr_url``, ``repo_slug``,
    ``field_values`` ({lowercased field name: value-as-str}).
    """
    # first: 50 matches the fields(first: 50) cap in _list_project_fields,
    # so every field's value is reachable (no silent truncation of the
    # Port Versions value the caller filters on).
    query = """
    query($projectId: ID!, $cursor: String) {
      node(id: $projectId) {
        ... on ProjectV2 {
          items(first: 100, after: $cursor) {
            pageInfo { hasNextPage endCursor }
            nodes {
              id
              content {
                __typename
                ... on PullRequest { number url repository { nameWithOwner } }
                ... on Issue { number url repository { nameWithOwner } }
              }
              fieldValues(first: 50) {
                nodes {
                  __typename
                  ... on ProjectV2ItemFieldTextValue {
                    text field { ... on ProjectV2FieldCommon { name } }
                  }
                  ... on ProjectV2ItemFieldSingleSelectValue {
                    name field { ... on ProjectV2FieldCommon { name } }
                  }
                }
              }
            }
          }
        }
      }
    }
    """
    items: list[dict] = []
    for node in _paginate_project_v2_items(project_id, query):
        content = node.get("content") or {}
        tn = content.get("__typename")
        repo_slug = (content.get("repository") or {}).get("nameWithOwner")
        field_values: dict[str, str] = {}
        for fv in (node.get("fieldValues") or {}).get("nodes", []) or []:
            fname = ((fv.get("field") or {}).get("name") or "").lower()
            if not fname:
                continue
            fvtn = fv.get("__typename")
            if fvtn == "ProjectV2ItemFieldTextValue":
                val = fv.get("text")
            elif fvtn == "ProjectV2ItemFieldSingleSelectValue":
                val = fv.get("name")
            else:
                continue
            if val is not None:
                field_values[fname] = val
        items.append({
            "item_id": node.get("id") or "",
            "content_typename": tn,
            "pr_number": content.get("number") if tn == "PullRequest" else None,
            "pr_url": content.get("url") if tn == "PullRequest" else None,
            "repo_slug": repo_slug,
            "field_values": field_values,
        })
    return items


def fetch_project_board_snapshot(
    config: Config,
) -> list[ProjectBoardCard] | None:
    """Read every card off the configured GitHub Project.

    Returns ``None`` when sync can't even start — no project configured,
    no token, unparseable URL, or GraphQL couldn't resolve the project
    id. Returns an empty list for an empty (but valid) board.

    Consumers (``releasy project pull``) match returned cards against local
    features by ``pr_url`` first and fall back to ``draft_title``
    parsing — see :func:`_project_item_body`'s title convention
    (``"<branch_name> (<feature_id>)"``).
    """
    project_url = config.notifications.github_project
    if not project_url:
        return None
    if not get_github_token():
        return None
    parsed = _parse_project_url(project_url)
    if not parsed:
        return None
    owner, number, is_org = parsed
    project_id = _get_project_id(owner, number, is_org)
    if not project_id:
        return None

    raw = _list_project_items_with_fields(project_id)
    cards: list[ProjectBoardCard] = []
    for item in raw:
        content = item.get("content") or {}
        kind = content.get("__typename")
        pr_url: str | None = None
        pr_number: int | None = None
        draft_title: str | None = None
        if kind == "PullRequest":
            pr_url = content.get("url")
            pr_number = content.get("number")
        elif kind == "DraftIssue":
            draft_title = content.get("title")
        else:
            # Issue or some other content — not something RelEasy
            # itself creates. Skip (we won't be able to map it back to
            # a feature anyway).
            continue

        status: str | None = None
        ai_cost: float | None = None
        for fv in (item.get("fieldValues") or {}).get("nodes", []) or []:
            field = (fv.get("field") or {}).get("name") or ""
            fname = field.lower()
            tn = fv.get("__typename")
            if tn == "ProjectV2ItemFieldSingleSelectValue" and fname == "status":
                status = fv.get("name")
            elif (
                tn == "ProjectV2ItemFieldNumberValue"
                and fname == AI_COST_FIELD_NAME.lower()
            ):
                raw_num = fv.get("number")
                if raw_num is not None:
                    try:
                        ai_cost = float(raw_num)
                    except (TypeError, ValueError):
                        ai_cost = None

        cards.append(ProjectBoardCard(
            item_id=item.get("id") or "",
            pr_url=pr_url,
            pr_number=pr_number,
            draft_title=draft_title,
            status=status,
            ai_cost_usd=ai_cost,
        ))
    return cards


def _list_project_items(project_id: str) -> list[dict]:
    """Return every item in a project (paginated).

    Each entry is the raw item node ``{id, content: {...}}``. ``content``
    can be a ``DraftIssue``, ``Issue``, or ``PullRequest`` — callers
    distinguish by the ``__typename`` field.
    """
    query = """
    query($projectId: ID!, $cursor: String) {
      node(id: $projectId) {
        ... on ProjectV2 {
          items(first: 100, after: $cursor) {
            pageInfo { hasNextPage endCursor }
            nodes {
              id
              content {
                __typename
                ... on DraftIssue { id title }
                ... on Issue { id number url }
                ... on PullRequest { id number url state merged }
              }
            }
          }
        }
      }
    }
    """
    return _paginate_project_v2_items(project_id, query)


def _is_closed_unmerged_pr(content: dict) -> bool:
    """True iff ``content`` is a PullRequest closed without merge.

    Merged PRs intentionally stay on the board — they're the record of
    work that landed. Only closed-without-merge cards are pollution.
    """
    if content.get("__typename") != "PullRequest":
        return False
    return content.get("state") == "CLOSED" and not content.get("merged")


def _prune_closed_pr_items(
    project_id: str, items: list[dict],
) -> int:
    """Delete project items whose PR was closed without merging.

    Mutates ``items`` in place so subsequent lookups (e.g. via
    :func:`_find_item_by_pr_url`) don't see the removed entries.
    Returns the count actually deleted.
    """
    removed = 0
    survivors: list[dict] = []
    for item in items:
        content = item.get("content") or {}
        if _is_closed_unmerged_pr(content):
            if _delete_item(project_id, item["id"]):
                removed += 1
                log.info(
                    "Removed closed PR %s from project board",
                    content.get("url"),
                )
                continue
            log.warning(
                "Failed to remove closed PR %s from project board",
                content.get("url"),
            )
        survivors.append(item)
    items[:] = survivors
    return removed


def _prune_orphan_items(
    project_id: str, items: list[dict], kept_ids: set[str],
) -> int:
    """Delete DraftIssue / PullRequest items not in ``kept_ids``.

    ``kept_ids`` holds the project item IDs the current sync claims as
    still backed by local state. Anything else of those two kinds is
    removed. Bare ``Issue`` items are left alone — releasy doesn't
    create them, so they're assumed user-added. Mutates ``items`` in
    place. Returns the count actually deleted.
    """
    removed = 0
    survivors: list[dict] = []
    for item in items:
        if item["id"] in kept_ids:
            survivors.append(item)
            continue
        content = item.get("content") or {}
        if content.get("__typename") not in ("DraftIssue", "PullRequest"):
            survivors.append(item)
            continue
        if _delete_item(project_id, item["id"]):
            removed += 1
            label = (
                content.get("title")
                or content.get("url")
                or item["id"]
            )
            log.info(
                "Removed orphan project item %r (not in local state)", label,
            )
            continue
        log.warning(
            "Failed to remove orphan project item %s", item["id"],
        )
        survivors.append(item)
    items[:] = survivors
    return removed


def _find_draft_item_by_title(
    items: list[dict], title: str,
) -> tuple[str, str] | None:
    """Find a draft-issue item by title in a pre-fetched item list.

    Returns ``(item_id, draft_issue_id)`` or ``None``.
    """
    for item in items:
        content = item.get("content") or {}
        if content.get("__typename") != "DraftIssue":
            continue
        if content.get("title") == title:
            return item["id"], content["id"]
    return None


def _find_item_by_pr_url(items: list[dict], pr_url: str) -> str | None:
    """Find a PR-content item by URL in a pre-fetched item list."""
    for item in items:
        content = item.get("content") or {}
        if content.get("__typename") != "PullRequest":
            continue
        if content.get("url") == pr_url:
            return item["id"]
    return None


def _add_draft_issue(project_id: str, title: str, body: str) -> str | None:
    mutation = """
    mutation($projectId: ID!, $title: String!, $body: String!) {
      addProjectV2DraftIssue(input: {projectId: $projectId, title: $title, body: $body}) {
        projectItem { id }
      }
    }
    """
    data = _gql(mutation, {"projectId": project_id, "title": title, "body": body})
    if not data:
        return None
    try:
        return data["addProjectV2DraftIssue"]["projectItem"]["id"]
    except (KeyError, TypeError):
        return None


def _get_pr_node_id(slug: str, number: int) -> tuple[str, str, bool] | None:
    """Return ``(node_id, state, merged)`` for a PR.

    ``state`` is GitHub's PR state (``OPEN`` / ``CLOSED`` / ``MERGED``),
    ``merged`` is the explicit boolean — together they let callers
    distinguish *closed without merge* (pollution) from *merged*
    (kept on the board as a record).
    """
    owner, name = slug.split("/", 1)
    query = """
    query($owner: String!, $name: String!, $number: Int!) {
      repository(owner: $owner, name: $name) {
        pullRequest(number: $number) { id state merged }
      }
    }
    """
    data = _gql(query, {"owner": owner, "name": name, "number": number})
    if not data:
        return None
    try:
        pr = data["repository"]["pullRequest"]
        return pr["id"], pr["state"], bool(pr["merged"])
    except (KeyError, TypeError):
        return None


def _add_item_by_content_id(project_id: str, content_id: str) -> str | None:
    """Add an Issue or PullRequest to a project by its node id.

    Idempotent: GitHub returns the existing project-item id if the content
    was already added to this project.
    """
    mutation = """
    mutation($projectId: ID!, $contentId: ID!) {
      addProjectV2ItemById(input: {projectId: $projectId, contentId: $contentId}) {
        item { id }
      }
    }
    """
    data = _gql(mutation, {"projectId": project_id, "contentId": content_id})
    if not data:
        return None
    try:
        return data["addProjectV2ItemById"]["item"]["id"]
    except (KeyError, TypeError):
        return None


def _update_draft_issue(
    draft_issue_id: str,
    title: str | None = None,
    body: str | None = None,
) -> bool:
    """Update an existing draft issue's title and/or body.

    Note: the ``UpdateProjectV2DraftIssueInput`` type takes ``draftIssueId``
    only — there is no ``projectId`` field on it. Passing one makes the
    GraphQL endpoint reject the whole mutation.
    """
    mutation = """
    mutation($draftIssueId: ID!, $title: String, $body: String) {
      updateProjectV2DraftIssue(input: {
        draftIssueId: $draftIssueId,
        title: $title, body: $body
      }) {
        draftIssue { id }
      }
    }
    """
    variables: dict = {"draftIssueId": draft_issue_id}
    if title is not None:
        variables["title"] = title
    if body is not None:
        variables["body"] = body
    data = _gql(mutation, variables)
    return data is not None


def _set_item_field(project_id: str, item_id: str, field_id: str, option_id: str) -> bool:
    mutation = """
    mutation($projectId: ID!, $itemId: ID!, $fieldId: ID!, $optionId: String!) {
      updateProjectV2ItemFieldValue(input: {
        projectId: $projectId
        itemId: $itemId
        fieldId: $fieldId
        value: { singleSelectOptionId: $optionId }
      }) {
        projectV2Item { id }
      }
    }
    """
    data = _gql(mutation, {
        "projectId": project_id,
        "itemId": item_id,
        "fieldId": field_id,
        "optionId": option_id,
    })
    return data is not None


def _set_item_number_field(
    project_id: str, item_id: str, field_id: str, value: float,
) -> bool:
    """Set a NUMBER field on a project item to ``value``.

    GitHub's GraphQL API takes a ``Float`` for ``value.number``; passing
    a Python float is fine — the JSON encoder serialises it correctly
    and integers are accepted too.

    GitHub rejects values with more than 8 decimal places (``VALIDATION``
    error). Accumulated AI costs (``$4.5365`` becoming
    ``4.536499999999999`` after summing several Claude usage entries)
    routinely trip this, so the value is rounded to 8 fractional digits
    before being sent. Eight is the API ceiling — well above the
    cents-level precision we actually care about.
    """
    safe_value = round(float(value), 8)
    mutation = """
    mutation($projectId: ID!, $itemId: ID!, $fieldId: ID!, $value: Float!) {
      updateProjectV2ItemFieldValue(input: {
        projectId: $projectId
        itemId: $itemId
        fieldId: $fieldId
        value: { number: $value }
      }) {
        projectV2Item { id }
      }
    }
    """
    data = _gql(mutation, {
        "projectId": project_id,
        "itemId": item_id,
        "fieldId": field_id,
        "value": safe_value,
    })
    return data is not None


def _set_item_text_field(
    project_id: str, item_id: str, field_id: str, value: str,
) -> bool:
    """Set a TEXT field on a project item to ``value``.

    Companion to :func:`_set_item_number_field` / :func:`_set_item_field`
    for the one field type those don't cover. Used by ``project-backport``
    when ``Port Versions`` is a free-text field.
    """
    mutation = """
    mutation($projectId: ID!, $itemId: ID!, $fieldId: ID!, $value: String!) {
      updateProjectV2ItemFieldValue(input: {
        projectId: $projectId
        itemId: $itemId
        fieldId: $fieldId
        value: { text: $value }
      }) {
        projectV2Item { id }
      }
    }
    """
    data = _gql(mutation, {
        "projectId": project_id,
        "itemId": item_id,
        "fieldId": field_id,
        "value": value,
    })
    return data is not None


def _delete_item(project_id: str, item_id: str) -> bool:
    mutation = """
    mutation($projectId: ID!, $itemId: ID!) {
      deleteProjectV2Item(input: {projectId: $projectId, itemId: $itemId}) {
        deletedItemId
      }
    }
    """
    data = _gql(mutation, {"projectId": project_id, "itemId": item_id})
    return data is not None


STATUS_OPTIONS = [
    "Needs Review",
    "Branch Created",
    "Conflict",
    "Blocked",
    "Skipped",
    "Merged",
    "Closed",
    "Superseded",
]

STATUS_COLORS = {
    "Needs Review": "BLUE",
    "Branch Created": "YELLOW",
    "Conflict": "RED",
    "Blocked": "ORANGE",
    "Skipped": "YELLOW",
    "Merged": "GREEN",
    "Closed": "GRAY",
    "Superseded": "GRAY",
}


def _get_owner_id(owner: str, is_org: bool) -> str | None:
    if is_org:
        query = "query($login: String!) { organization(login: $login) { id } }"
    else:
        query = "query($login: String!) { user(login: $login) { id } }"
    data = _gql(query, {"login": owner})
    if not data:
        return None
    try:
        key = "organization" if is_org else "user"
        return data[key]["id"]
    except (KeyError, TypeError):
        return None


def _create_project(owner_id: str, title: str) -> tuple[str, int] | None:
    """Create a new GitHub Project v2. Returns (project_id, project_number)."""
    mutation = """
    mutation($ownerId: ID!, $title: String!) {
      createProjectV2(input: {ownerId: $ownerId, title: $title}) {
        projectV2 { id number }
      }
    }
    """
    data = _gql(mutation, {"ownerId": owner_id, "title": title})
    if not data:
        return None
    try:
        p = data["createProjectV2"]["projectV2"]
        return p["id"], p["number"]
    except (KeyError, TypeError):
        return None


def _create_single_select_field(
    project_id: str, name: str, options: list[dict],
) -> str | None:
    """Create a single-select field on a project. Returns field ID."""
    mutation = """
    mutation($projectId: ID!, $name: String!, $options: [ProjectV2SingleSelectFieldOptionInput!]!) {
      createProjectV2Field(input: {
        projectId: $projectId
        dataType: SINGLE_SELECT
        name: $name
        singleSelectOptions: $options
      }) {
        projectV2Field { ... on ProjectV2SingleSelectField { id } }
      }
    }
    """
    data = _gql(mutation, {
        "projectId": project_id,
        "name": name,
        "options": options,
    })
    if not data:
        return None
    try:
        return data["createProjectV2Field"]["projectV2Field"]["id"]
    except (KeyError, TypeError):
        return None


def _update_single_select_options(
    field_id: str, options: list[dict],
) -> tuple[bool, str | None]:
    """Replace all options on an existing single-select field.

    Returns ``(success, error_message_or_None)`` so the caller can
    surface the GitHub-side reason for a failure (commonly: missing
    ``project`` token scope, or per-API-version constraints on which
    fields ``updateProjectV2Field`` permits editing).
    """
    mutation = """
    mutation($fieldId: ID!, $options: [ProjectV2SingleSelectFieldOptionInput!]!) {
      updateProjectV2Field(input: {
        fieldId: $fieldId
        singleSelectOptions: $options
      }) {
        projectV2Field { ... on ProjectV2SingleSelectField { id } }
      }
    }
    """
    token = get_github_token()
    if not token:
        return False, "RELEASY_GITHUB_TOKEN not set"
    try:
        resp = requests.post(
            GRAPHQL_URL,
            json={
                "query": mutation,
                "variables": {"fieldId": field_id, "options": options},
            },
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
            timeout=30,
        )
    except requests.RequestException as exc:
        return False, f"network error: {exc}"
    if resp.status_code != 200:
        snippet = resp.text[:300]
        return False, f"HTTP {resp.status_code}: {snippet}"
    payload = resp.json()
    if "errors" in payload:
        msgs = "; ".join(
            e.get("message", "?") for e in payload["errors"]
        )
        return False, f"GraphQL errors: {msgs}"
    if not payload.get("data", {}).get("updateProjectV2Field"):
        return False, f"unexpected response shape: {payload}"
    return True, None


def setup_project(config: Config) -> str | None:
    """Create a GitHub Project with the Status field, or verify an existing one.

    If notifications.github_project is set, verifies the Status field exists.
    If not set, creates a new project and returns the URL.
    """
    token = get_github_token()
    if not token:
        log.warning("RELEASY_GITHUB_TOKEN not set")
        return None

    slug = get_origin_repo_slug(config)
    if not slug:
        return None
    owner = slug.split("/")[0]

    project_url = config.notifications.github_project

    if project_url:
        parsed = _parse_project_url(project_url)
        if not parsed:
            log.warning("Could not parse project URL: %s", project_url)
            return None
        p_owner, p_number, is_org = parsed
        project_id = _get_project_id(p_owner, p_number, is_org)
        if not project_id:
            log.warning("Could not find project: %s", project_url)
            return None
    else:
        is_org = True
        owner_id = _get_owner_id(owner, is_org)
        if not owner_id:
            is_org = False
            owner_id = _get_owner_id(owner, is_org)
        if not owner_id:
            log.warning("Could not resolve owner ID for %s", owner)
            return None

        title = f"RelEasy: {config.project_name}"
        result = _create_project(owner_id, title)
        if not result:
            log.warning("Failed to create project")
            return None
        project_id, p_number = result
        if is_org:
            project_url = f"https://github.com/orgs/{owner}/projects/{p_number}"
        else:
            project_url = f"https://github.com/users/{owner}/projects/{p_number}"

    status_info = _get_status_field(project_id)
    if status_info:
        field_id, existing_options, raw_options = status_info
        canonical_lower = {opt.lower() for opt in STATUS_OPTIONS}
        existing_names = [o["name"] for o in raw_options]
        missing = [
            opt for opt in STATUS_OPTIONS
            if opt.lower() not in existing_options
        ]
        extra = [
            o["name"] for o in raw_options
            if o["name"].lower() not in canonical_lower
        ]
        console.print(
            "  [dim]Status field options found:[/dim] "
            f"{', '.join(existing_names) or '(none)'}"
        )
        console.print(
            f"  [dim]Canonical:[/dim] {', '.join(STATUS_OPTIONS)}"
        )
        if missing or extra:
            console.print(
                f"  [yellow]→[/yellow] reconciling: "
                f"add={missing or '—'}, remove={extra or '—'}"
            )
            # Replace, don't merge: the Status field is fully owned by
            # RelEasy. Orphan options (e.g. legacy ``Ok`` / ``Resolved``
            # from older RelEasy versions, or anything else hand-added to
            # the field) get dropped. Items that were sitting on a
            # dropped option lose their Status value momentarily — the
            # next ``sync_project`` call re-assigns them based on
            # ``fs.status``, which the load-time migration has already
            # collapsed to the new vocabulary.
            canonical = [
                {
                    "name": opt,
                    "color": STATUS_COLORS.get(opt, "GRAY"),
                    "description": "",
                }
                for opt in STATUS_OPTIONS
            ]
            ok, err = _update_single_select_options(field_id, canonical)
            if ok:
                console.print(
                    "  [green]✓[/green] Status field options reconciled"
                )
            else:
                console.print(
                    f"  [red]✗[/red] Could not update Status field "
                    f"options: [yellow]{err}[/yellow]"
                )
                console.print(
                    "    [dim]Most common cause: token is missing the "
                    "[cyan]project[/cyan] scope (classic PAT) or "
                    "[cyan]Projects: Read & write[/cyan] permission "
                    "(fine-grained PAT). Fix the token and re-run, or "
                    "edit the options manually in the project "
                    "settings.[/dim]"
                )
        else:
            console.print(
                "  [dim]Status field options already canonical, "
                "nothing to do.[/dim]"
            )
    else:
        console.print(
            "  [dim]No Status field found on the project, creating "
            "one...[/dim]"
        )
        options = [
            {
                "name": opt,
                "color": STATUS_COLORS.get(opt, "GRAY"),
                "description": "",
            }
            for opt in STATUS_OPTIONS
        ]
        field_id = _create_single_select_field(project_id, "Status", options)
        if field_id:
            console.print("  [green]✓[/green] Status field created")
        else:
            console.print(
                "  [red]✗[/red] Failed to create Status field "
                "(see warnings above)"
            )

    # Ensure the AI Cost (NUMBER) field exists. Created lazily here so an
    # already-running project picks it up the next time the user runs
    # ``releasy setup-project``; ``sync_project`` also creates it on the
    # fly so cards always carry the value.
    ai_cost_field_id = _ensure_ai_cost_field(project_id)
    if ai_cost_field_id:
        console.print(
            f"  [green]\u2713[/green] {AI_COST_FIELD_NAME} field present"
        )
    else:
        console.print(
            f"  [yellow]![/yellow] Could not create {AI_COST_FIELD_NAME} "
            "field (token missing project scope?). Cost will not be "
            "synced to the board."
        )

    # Provision the Assignee Dev / Assignee QA single-select fields. On
    # first creation each is populated with the option list from
    # ``notifications.assignee_*_options``. On subsequent runs we leave
    # the options alone — adding/removing options would risk wiping
    # values the user manually set on the board.
    for field_name, options in (
        (ASSIGNEE_DEV_FIELD_NAME, config.notifications.assignee_dev_options),
        (ASSIGNEE_QA_FIELD_NAME, config.notifications.assignee_qa_options),
    ):
        existed = _get_single_select_field(project_id, field_name) is not None
        ensured = _ensure_assignee_field(project_id, field_name, options)
        if not ensured:
            console.print(
                f"  [yellow]![/yellow] Could not create [cyan]{field_name}"
                "[/cyan] field (token missing project scope?). Add it "
                "manually in the project settings to enable assignee "
                "tracking."
            )
            continue
        if existed:
            console.print(
                f"  [dim]\u2713 {field_name} field already exists "
                "(option list left untouched — edit in the GitHub UI to "
                "add or remove people).[/dim]"
            )
        else:
            console.print(
                f"  [green]\u2713[/green] {field_name} field created "
                f"with {len(options)} option(s): {', '.join(options) or '\u2014'}"
            )

    return project_url


STATUS_MAP = {
    "needs_review": "Needs Review",
    "branch_created": "Branch Created",
    "conflict": "Conflict",
    "blocked": "Blocked",
    "skipped": "Skipped",
    "merged": "Merged",
    "closed": "Closed",
    "superseded": "Superseded",
}

REST_API_URL = "https://api.github.com"


def _rest_api(
    method: str, path: str, json_data: dict | None = None,
    *, expected_statuses: tuple[int, ...] = (200, 201),
    log_on_error: bool = True,
) -> tuple[int, dict | list | None]:
    """Make a GitHub REST API call.

    Returns ``(status_code, json_body_or_None)``. Emits a warning on
    unexpected status codes unless ``log_on_error`` is False.
    """
    token = get_github_token()
    if not token:
        return 0, None
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    resp = requests.request(
        method, f"{REST_API_URL}{path}",
        json=json_data, headers=headers, timeout=30,
    )
    if resp.status_code not in expected_statuses:
        if log_on_error:
            log.warning(
                "REST API %s %s: %s %s",
                method, path, resp.status_code, resp.text,
            )
        return resp.status_code, None
    try:
        return resp.status_code, (resp.json() if resp.text else None)
    except ValueError:
        return resp.status_code, None


# Projects Classic uses the REST `/projects/{n}/views` endpoint; Projects V2
# (the only kind still being created) serves that URL as 404. We probe once
# per run, cache the result, and stop spamming the log for V2 projects.
_PROJECT_IS_V2: set[tuple[str, int]] = set()


def _project_v2_marker(owner: str, project_number: int) -> tuple[str, int]:
    return (owner.lower(), int(project_number))


def _get_project_views(owner: str, project_number: int, is_org: bool) -> list[dict]:
    """List existing views on a Projects Classic project. Empty list for V2."""
    marker = _project_v2_marker(owner, project_number)
    if marker in _PROJECT_IS_V2:
        return []
    prefix = "orgs" if is_org else "users"
    status, data = _rest_api(
        "GET",
        f"/{prefix}/{owner}/projects/{project_number}/views",
        log_on_error=False,
    )
    if status == 404:
        _PROJECT_IS_V2.add(marker)
        log.info(
            "Project %s/#%d appears to be a Projects V2 board; "
            "REST view management is unavailable. Skipping view creation.",
            owner, project_number,
        )
        return []
    if status not in (200,):
        log.warning(
            "GET /%s/%s/projects/%d/views: %s",
            prefix, owner, project_number, status,
        )
        return []
    if isinstance(data, list):
        return data
    return []


def _create_project_view(
    owner: str, project_number: int, is_org: bool,
    name: str, layout: str = "table",
) -> dict | None:
    """Create a new view (tab) on a Projects Classic project.

    No-op for Projects V2 (already detected by _get_project_views).
    """
    marker = _project_v2_marker(owner, project_number)
    if marker in _PROJECT_IS_V2:
        return None
    prefix = "orgs" if is_org else "users"
    status, data = _rest_api(
        "POST",
        f"/{prefix}/{owner}/projects/{project_number}/views",
        {"name": name, "layout": layout},
        log_on_error=False,
    )
    if status == 404:
        _PROJECT_IS_V2.add(marker)
        return None
    if status not in (200, 201):
        log.warning(
            "POST /%s/%s/projects/%d/views: %s",
            prefix, owner, project_number, status,
        )
        return None
    return data if isinstance(data, dict) else None


def _ensure_project_view(
    owner: str, project_number: int, is_org: bool, view_name: str,
) -> bool:
    """Create a view for this rebase if it doesn't exist yet."""
    marker = _project_v2_marker(owner, project_number)
    if marker in _PROJECT_IS_V2:
        return False
    views = _get_project_views(owner, project_number, is_org)
    if marker in _PROJECT_IS_V2:
        return False
    for v in views:
        if v.get("name") == view_name:
            return True
    result = _create_project_view(owner, project_number, is_org, view_name)
    return result is not None


@dataclass
class ProjectSyncSummary:
    """Outcome of a single ``sync_project`` call.

    ``added`` counts cards that didn't exist on the board before this call
    (newly attached PRs + newly created draft issues). ``updated`` counts
    pre-existing cards we refreshed (body / Status field). ``errors`` is
    the number of features we wanted to sync but couldn't.

    ``skipped`` is True when sync didn't run at all (no project URL
    configured, missing token, unparseable URL, …) — that's not an error,
    just a no-op.
    """
    added: int = 0
    updated: int = 0
    removed: int = 0
    errors: int = 0
    skipped: bool = False
    skipped_reason: str | None = None

    @property
    def changed(self) -> int:
        return self.added + self.updated + self.removed

    def summary_line(self) -> str:
        if self.skipped:
            return f"skipped ({self.skipped_reason or 'no project configured'})"
        parts: list[str] = []
        if self.added:
            parts.append(f"{self.added} added")
        if self.updated:
            parts.append(f"{self.updated} updated")
        if self.removed:
            parts.append(f"{self.removed} removed")
        if self.errors:
            parts.append(f"{self.errors} error(s)")
        if not parts:
            return "already up to date"
        return ", ".join(parts)


def sync_project(
    config: Config, state: PipelineState,
    *,
    prune_orphans: bool = False,
) -> ProjectSyncSummary:
    """Sync pipeline state to a GitHub Project board.

    Iterates every feature actually present in ``state.features`` (ports
    discovered via ``pr_sources`` are only known here, never in
    ``config.features``) and any static feature from ``config.features``
    that has not produced state yet (so unstarted/disabled features still
    show up as cards).

    Each feature becomes one item on the project:

    * If a PR exists for it (``fs.rebase_pr_url``), the *real* PR is
      attached to the project via ``addProjectV2ItemById`` — that's the
      whole point of the Projects v2 API. The mutation is idempotent so
      re-running is safe.
    * Otherwise (pending / disabled / dropped singleton) we fall back to a
      DraftIssue carrying the same status info, so the board still
      reflects the run.

    When ``prune_orphans`` is set, DraftIssue / PullRequest items on the
    board that aren't backed by a state entry in this run are deleted.
    Off by default so per-run incremental sync calls (after every state
    save) don't reap items mid-mutation; only the standalone
    ``releasy project push`` opts in.

    The returned ``ProjectSyncSummary`` reports how many cards were added
    vs. refreshed, so reconciliation passes (e.g. at the end of ``releasy
    continue``) can tell the user "added 3 missing items" without parsing
    log output.
    """
    if config.dry_run:
        return ProjectSyncSummary(
            skipped=True, skipped_reason="dry-run",
        )

    project_url = config.notifications.github_project
    if not project_url:
        return ProjectSyncSummary(
            skipped=True,
            skipped_reason="notifications.github_project not set",
        )

    token = get_github_token()
    if not token:
        log.warning("RELEASY_GITHUB_TOKEN not set — skipping project sync")
        return ProjectSyncSummary(
            skipped=True, skipped_reason="RELEASY_GITHUB_TOKEN not set",
        )

    parsed = _parse_project_url(project_url)
    if not parsed:
        log.warning("Could not parse project URL: %s", project_url)
        return ProjectSyncSummary(
            skipped=True,
            skipped_reason=f"unparseable project URL {project_url!r}",
        )

    owner, number, is_org = parsed
    project_id = _get_project_id(owner, number, is_org)
    if not project_id:
        log.warning(
            "Could not resolve project ID for %s — check that the URL is "
            "correct and that RELEASY_GITHUB_TOKEN has the 'project' scope",
            project_url,
        )
        return ProjectSyncSummary(
            skipped=True,
            skipped_reason=(
                f"could not resolve project {project_url!r} "
                "(token missing 'project' scope?)"
            ),
        )

    if state.base_branch:
        _ensure_project_view(owner, number, is_org, state.base_branch)

    status_info = _get_status_field(project_id)
    status_field_id = None
    status_options: dict[str, str] = {}
    if status_info:
        status_field_id, status_options, _ = status_info

    # Auto-provision the AI Cost field. Best-effort: if the token can't
    # create it, we just skip the cost sync — every other part of the
    # board still updates.
    ai_cost_field_id = _ensure_ai_cost_field(project_id)

    # Look up (do NOT auto-create here — that's setup_project's job) the
    # Assignee Dev field. We only set its default value on freshly created
    # cards; missing field => default-seeding is silently skipped.
    assignee_dev_field_id: str | None = None
    assignee_dev_options: dict[str, str] = {}
    dev_field = _get_single_select_field(project_id, ASSIGNEE_DEV_FIELD_NAME)
    if dev_field:
        assignee_dev_field_id, assignee_dev_options, _ = dev_field
    # Lower-case the configured login → option-label map at the call
    # site so we can compare PR-author logins case-insensitively without
    # mutating the live config.
    login_map_lc = {
        k.lower(): v
        for k, v in config.notifications.assignee_dev_login_map.items()
    }

    origin_slug = get_origin_repo_slug(config)

    # Collect every feature we know about. State wins; static config
    # features merely provide a row for things that haven't run yet.
    rows: list[tuple[str, str, str, list[str], FeatureState | None]] = []
    seen: set[str] = set()

    for feat_id, fs in state.features.items():
        feat = config.get_feature(feat_id)
        label = (
            fs.branch_name
            or (feat.source_branch if feat else None)
            or feat_id
        )
        title = f"{label} ({feat_id})"
        rows.append((feat_id, title, fs.status, fs.conflict_files, fs))
        seen.add(feat_id)

    # Static config.features that haven't run yet are deliberately
    # skipped: they have no real status, no branch, and nothing to track.
    # They appear on the board only after a run produces a state entry.

    summary = ProjectSyncSummary()
    if not rows:
        log.info("No features to sync to GitHub Project")
        return summary

    existing_items = _list_project_items(project_id)
    summary.removed = _prune_closed_pr_items(project_id, existing_items)
    kept_item_ids: set[str] = set()

    for feat_id, title, status, conflict_files, fs in rows:
        body = _project_item_body(
            state, status, conflict_files, fs, origin_slug,
        )

        item_id: str | None = None
        was_existing = False
        attached_real_pr = False
        # Prefer attaching the real PR — that's what Projects v2 is for.
        pr_url = fs.rebase_pr_url if fs else None
        if pr_url:
            item_id = _find_item_by_pr_url(existing_items, pr_url)
            if item_id:
                was_existing = True
                attached_real_pr = True
                kept_item_ids.add(item_id)
            else:
                pr_ref = parse_pr_url(pr_url)
                pr_slug = (
                    f"{pr_ref[0]}/{pr_ref[1]}"
                    if pr_ref else (origin_slug or "")
                )
                pr_number = pr_ref[2] if pr_ref else None
                if pr_slug and pr_number is not None:
                    pr_meta = _get_pr_node_id(pr_slug, pr_number)
                    if pr_meta:
                        pr_node_id, pr_state, pr_merged = pr_meta
                        if pr_state == "CLOSED" and not pr_merged:
                            log.info(
                                "Skipping closed PR %s — not adding to "
                                "project board", pr_url,
                            )
                            continue
                        item_id = _add_item_by_content_id(
                            project_id, pr_node_id,
                        )
                        if item_id:
                            attached_real_pr = True
                        else:
                            log.warning(
                                "Failed to add PR %s to project — falling "
                                "back to draft issue", pr_url,
                            )
                    else:
                        log.warning(
                            "Could not resolve PR node id for %s — falling "
                            "back to draft issue", pr_url,
                        )

        if not item_id:
            existing_draft = _find_draft_item_by_title(existing_items, title)
            if existing_draft:
                item_id, draft_id = existing_draft
                _update_draft_issue(draft_id, body=body)
                was_existing = True
                kept_item_ids.add(item_id)
            else:
                item_id = _add_draft_issue(project_id, title, body)
                if not item_id:
                    log.warning("Failed to create project item for %s", title)
                    summary.errors += 1
                    continue
        elif attached_real_pr:
            # Real PR is now the project item. Remove any leftover draft stub
            # created on a prior run when no PR existed yet — otherwise the
            # board ends up with two cards for the same feature.
            stale_draft = _find_draft_item_by_title(existing_items, title)
            if stale_draft:
                stale_item_id, _ = stale_draft
                if _delete_item(project_id, stale_item_id):
                    existing_items[:] = [
                        i for i in existing_items if i["id"] != stale_item_id
                    ]
                    log.info(
                        "Removed stale draft project item %r (replaced by "
                        "PR %s)", title, pr_url,
                    )
                else:
                    log.warning(
                        "Could not remove stale draft project item %r — "
                        "board may show duplicate cards", title,
                    )

        if status_field_id and status_options:
            mapped = STATUS_MAP.get(status, "Pending")
            option_id = status_options.get(mapped.lower())
            if option_id:
                _set_item_field(project_id, item_id, status_field_id, option_id)

        # AI Cost: always write a number so the column is never blank
        # (cards that never invoked the resolver land at 0.0). Skipped
        # only when the field couldn't be provisioned at all.
        if ai_cost_field_id:
            cost_value = float(fs.ai_cost_usd) if (fs and fs.ai_cost_usd is not None) else 0.0
            _set_item_number_field(
                project_id, item_id, ai_cost_field_id, cost_value,
            )

        # Assignee Dev: seed once per card, on first creation only. We
        # never overwrite an existing card's value so anything a human
        # set manually (or any later reassignment) is preserved across
        # re-runs. Assignee QA is left strictly untouched — it has no
        # automatic default.
        if (
            not was_existing
            and assignee_dev_field_id
            and assignee_dev_options
            and fs is not None
            and fs.pr_author
        ):
            mapped_label = login_map_lc.get(fs.pr_author.lower())
            if mapped_label:
                option_id = assignee_dev_options.get(mapped_label.lower())
                if option_id:
                    _set_item_field(
                        project_id, item_id,
                        assignee_dev_field_id, option_id,
                    )
                else:
                    log.info(
                        "Assignee Dev default %r for PR author %r is not "
                        "an option on the project field — leaving the "
                        "field empty for manual assignment",
                        mapped_label, fs.pr_author,
                    )

        if was_existing:
            summary.updated += 1
        else:
            summary.added += 1

    if prune_orphans:
        summary.removed += _prune_orphan_items(
            project_id, existing_items, kept_item_ids,
        )

    log.info(
        "Synced %d items to GitHub Project "
        "(%d added, %d updated, %d removed, %d errors)",
        summary.added + summary.updated + summary.removed,
        summary.added, summary.updated, summary.removed, summary.errors,
    )
    return summary


def _format_pr_url_as_link(url: str) -> str:
    """Render a GitHub PR URL as a markdown link ``[owner/repo#N](url)``.

    Falls back to the bare URL when parsing fails — keeps the body
    readable without ever throwing on a malformed string.
    """
    m = re.match(
        r"https?://github\.com/([^/]+)/([^/]+?)(?:\.git)?/pull/(\d+)",
        url,
    )
    if not m:
        return url
    owner, repo, num = m.group(1), m.group(2), m.group(3)
    return f"[{owner}/{repo}#{num}]({url})"


def _project_item_body(
    state: PipelineState,
    status: str,
    conflict_files: list[str],
    fs: FeatureState | None,
    origin_slug: str | None = None,
) -> str:
    """Render the body for a draft-issue project card."""
    body_parts = [f"**Status:** {status}"]
    if state.onto:
        body_parts.append(f"**Onto:** `{state.onto}`")
    if state.base_branch:
        body_parts.append(f"**Base:** `{state.base_branch}`")
    if (
        status == "branch_created" and fs is not None
        and fs.branch_name and origin_slug
    ):
        repo_url = f"https://github.com/{origin_slug}"
        branch_url = f"{repo_url}/tree/{fs.branch_name}"
        # GitHub /compare/<base>...<head>?expand=1 lands on the
        # "Open a pull request" form pre-populated with the diff, so the
        # user can create the PR manually with one click.
        compare_url = (
            f"{repo_url}/compare/{state.base_branch or 'main'}..."
            f"{fs.branch_name}?expand=1"
        )
        body_parts.append(
            "**Branch pushed, no PR opened yet.**\n"
            f"- Branch: [`{fs.branch_name}`]({branch_url})\n"
            f"- [Open a pull request manually]({compare_url})"
        )
    # An "AI gave up" conflict (legacy ``needs_resolution``) is now just
    # a regular ``conflict`` — but the ``failed_step_index`` /
    # ``partial_pr_count`` / ``rebase_pr_url`` fields, when set, identify
    # this flavour and let the body explain what happened. Plain
    # cherry-pick conflicts (no AI involvement) just fall through to the
    # ``conflict_files`` block below.
    if (
        status == "conflict" and fs is not None and (
            fs.partial_pr_count is not None
            or fs.failed_step_index is not None
            or fs.rebase_pr_url
        )
    ):
        note_lines = [
            "**Needs manual intervention.**",
            "Releasy could not resolve a conflict automatically.",
        ]
        if fs.partial_pr_count is not None and fs.partial_pr_count > 0:
            note_lines.append(
                f"Partial group: {fs.partial_pr_count} PR(s) applied "
                "before the failure. A draft PR was opened — see "
                "`rebase_pr_url` below."
            )
        elif fs.failed_step_index is not None:
            note_lines.append(
                "Local port branch was dropped (nothing to keep). "
                "Resolve the source PR manually and re-run."
            )
        if fs.failed_step_index is not None:
            note_lines.append(
                f"Failed at cherry-pick step #{fs.failed_step_index + 1}."
            )
        if fs.rebase_pr_url:
            note_lines.append(f"Draft PR: {fs.rebase_pr_url}")
        body_parts.append("\n".join(note_lines))

    # Prereq-detection blocks. These are independent of the conflict-files
    # block above — a unit can both have unresolved conflict files AND a
    # missing-prereq trail (e.g. detection-only mode left both populated).
    if fs is not None:
        prereq_block = _render_prereq_body_block(fs)
        if prereq_block:
            body_parts.append(prereq_block)

    if conflict_files:
        files_str = "\n".join(f"- `{f}`" for f in conflict_files)
        body_parts.append(f"**Conflict files:**\n{files_str}")
    return "\n\n".join(body_parts)


def _render_prereq_body_block(fs: FeatureState) -> str | None:
    """Render the prereq-detection / auto-recovery block(s) for ``fs``.

    Selects the right variant based on which fields are populated:
    * ``queued_prereq_units`` set → "already queued elsewhere"
    * ``prereq_recovery_exhausted`` set → "depth limit / cycle"
    * ``dynamic_prereq_urls`` set with no exhaust + status != conflict
      → "auto-ported prerequisites" (success)
    * ``missing_prereq_prs`` set otherwise → detection-only block

    Returns ``None`` when no prereq-related state is present.
    """
    if fs.queued_prereq_units:
        lines = ["**Prerequisite already queued.**"]
        lines.append(
            "Releasy detected that this PR depends on PR(s) which are "
            "already known to releasy and being ported elsewhere. "
            "Merge those first; this unit will succeed on the next "
            "`releasy run`."
        )
        for entry in fs.queued_prereq_units:
            url = entry.get("prereq_url", "")
            queued_in = entry.get("queued_in", "?")
            queued_pr = entry.get("queued_in_pr_url")
            link = _format_pr_url_as_link(url) if url else "(unknown PR)"
            extra = f" — already-open PR: {queued_pr}" if queued_pr else ""
            lines.append(f"- {link} (queued in `{queued_in}`){extra}")
        return "\n".join(lines)

    if fs.prereq_recovery_exhausted and fs.prereq_trail:
        max_depth = max(
            (entry.get("at_depth", 0) for entry in fs.prereq_trail),
            default=0,
        )
        # Last trail entry's `discovered` is the prereq we *would* have
        # dived into when we hit the cap (or the cycle).
        last = fs.prereq_trail[-1]
        next_prereqs = last.get("discovered", []) or []
        next_link_str = ", ".join(
            _format_pr_url_as_link(u) for u in next_prereqs
        ) or "(none recorded)"
        lines = [
            f"**Auto-prereq recovery hit a hard limit (depth {max_depth}).**",
            "Dependency trail (each line = one dive, in discovery order):",
        ]
        for i, entry in enumerate(fs.prereq_trail, start=1):
            trig = entry.get("triggering_pr") or "(unknown)"
            disc = entry.get("discovered", []) or []
            reason = entry.get("reason") or ""
            disc_str = ", ".join(
                _format_pr_url_as_link(u) for u in disc
            ) or "(none)"
            trig_link = _format_pr_url_as_link(trig)
            line = f"{i}. {trig_link} → needed {disc_str}"
            if reason:
                line += f" — _{reason}_"
            lines.append(line)
        lines.append(
            f"**Next prereq that exceeded the limit:** {next_link_str}"
        )
        lines.append(
            "All dynamic prereqs were rolled back. Resolve manually or "
            "bump `ai_resolve.auto_add_prerequisite_prs.max_prereq_depth`."
        )
        return "\n".join(lines)

    if (
        fs.dynamic_prereq_urls
        and not fs.prereq_recovery_exhausted
        and fs.status != "conflict"
    ):
        lines = ["**Auto-ported prerequisites.**"]
        lines.append(
            "Releasy detected one or more missing prerequisite PR(s) and "
            "ported them automatically before the requested PR. The "
            "combined PR includes:"
        )
        for url in fs.dynamic_prereq_urls:
            lines.append(f"- {_format_pr_url_as_link(url)}")
        if fs.prereq_trail:
            trail_chain = " → ".join(
                _format_pr_url_as_link(
                    entry.get("triggering_pr") or "(unknown)"
                )
                for entry in fs.prereq_trail
            )
            if trail_chain:
                lines.append(f"_Detection trail:_ {trail_chain}")
        return "\n".join(lines)

    if fs.missing_prereq_prs:
        lines = ["**Missing prerequisites.**"]
        lines.append(
            "Claude judged this conflict to be caused by upstream PR(s) "
            "that have not yet been ported to the target branch:"
        )
        for url in fs.missing_prereq_prs:
            lines.append(f"- {_format_pr_url_as_link(url)}")
        if fs.missing_prereq_note:
            lines.append(f"**Analysis:** {fs.missing_prereq_note}")
        return "\n".join(lines)

    return None
