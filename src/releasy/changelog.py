"""Generate a release changelog from PRs merged into the target branch.

Queries the origin repo (one Search-API call) for PRs whose base is the
target branch and that merged within the ``--from``..``--to`` window,
drops forward-ports, and renders a categorised markdown changelog
matching the Altinity release-notes convention. An explicit PR set can
be supplied instead, bypassing discovery.

Output goes either to a file (``-o``) or to a draft GitHub release on
the origin repo.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path

from releasy.config import Config
from releasy.git_ops import (
    commit_date,
    ensure_remote,
    ensure_work_repo,
    is_tag_ref,
    resolve_ref,
    run_git,
)
from releasy.github_ops import (
    PRInfo,
    create_draft_release,
    fetch_pr_by_number,
    fetch_pr_by_url,
    get_origin_repo_slug,
    search_merged_prs_by_base,
)
from releasy.termlog import console

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------


# Section headings, in the order they appear in the rendered output.
# Each ClickHouse "Changelog category" maps 1:1 to a section here —
# Documentation goes to Documentation, Build/Testing/Packaging gets its
# own section, etc.
SECTION_BACKWARD_INCOMPAT = "Backward Incompatible Change"
SECTION_NEW_FEATURES = "New Features"
SECTION_PERFORMANCE = "Performance Improvements"
SECTION_IMPROVEMENTS = "Improvements"
SECTION_BUG_FIXES = "Bug Fixes (user-visible misbehavior in an official stable release)"
SECTION_BUILD = "Build/Testing/Packaging Improvements"
SECTION_CI = "CI Fixes or Improvements"
SECTION_DOCS = "Documentation"

SECTION_ORDER = (
    SECTION_BACKWARD_INCOMPAT,
    SECTION_NEW_FEATURES,
    SECTION_PERFORMANCE,
    SECTION_IMPROVEMENTS,
    SECTION_BUG_FIXES,
    SECTION_BUILD,
    SECTION_CI,
    SECTION_DOCS,
)

# Sentinel returned by ``_classify_category`` for the only category
# that's an explicit opt-out from release notes.
SECTION_NOT_FOR_CHANGELOG = "__not_for_changelog__"


# Maps lowercased "Changelog category" text to a canonical section.
# Pattern fragments are matched as substrings (case-insensitive) against
# the category text the PR author wrote. Order matters: the "Not for
# changelog" drop rule comes first, then more-specific patterns before
# their substring-shadowing siblings (e.g. "performance improvement"
# before "improvement", "ci fix" before "build/testing/packaging").
_CATEGORY_PATTERNS: list[tuple[str, str]] = [
    # Drop category — explicit opt-out.
    ("not for changelog", SECTION_NOT_FOR_CHANGELOG),
    # Real sections.
    ("backward incompatible", SECTION_BACKWARD_INCOMPAT),
    ("new feature", SECTION_NEW_FEATURES),
    ("performance improvement", SECTION_PERFORMANCE),
    ("ci fix", SECTION_CI),
    ("ci improvement", SECTION_CI),
    ("build/testing/packaging", SECTION_BUILD),
    ("build / testing / packaging", SECTION_BUILD),
    ("documentation", SECTION_DOCS),
    ("bug fix", SECTION_BUG_FIXES),
    ("improvement", SECTION_IMPROVEMENTS),
]

# Forward-port detection.
_FWDPORT_TITLE_RE = re.compile(r"forward[\s\-]?port", re.IGNORECASE)
_FWDPORT_LABELS = {"forwardport", "forward-port", "forward port"}

# "ClickHouse/ClickHouse#12345" or "owner/repo#N" cross-repo refs in body.
_CROSSREPO_REF_RE = re.compile(r"\b([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)#(\d+)\b")
# Markdown link to a github.com/.../pull/N
_PR_URL_RE = re.compile(
    r"https?://github\.com/([^/\s)]+)/([^/\s)]+?)(?:\.git)?/pull/(\d+)\b",
)
# "Cherry-picked from …" line that RelEasy adds to every port PR body.
# Anchored at line start, matches up to (and excluding) the next blank
# line so a comma-separated multi-PR list survives wrapping.
_CHERRY_PICKED_FROM_RE = re.compile(
    r"^Cherry-picked from\s+([^\n]+(?:\n(?!\s*$)[^\n]+)*)",
    re.IGNORECASE | re.MULTILINE,
)


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass
class ChangelogEntry:
    pr: PRInfo
    description: str
    section: str
    upstream_prs: list[PRInfo] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Markdown / body parsing
# ---------------------------------------------------------------------------


_MD_HEADING_RE = re.compile(r"^(#{1,6})[ \t]+(.+?)[ \t]*$", re.MULTILINE)


def _section_text(body: str, keyword: str) -> str | None:
    """Return text under the first heading containing ``keyword``."""
    if not body:
        return None
    key = keyword.lower()
    matches = list(_MD_HEADING_RE.finditer(body))
    for i, m in enumerate(matches):
        if key in m.group(2).lower():
            start = m.end()
            end = matches[i + 1].start() if i + 1 < len(matches) else len(body)
            text = body[start:end].strip()
            return text or None
    return None


def _classify_category(category_text: str | None) -> str | None:
    """Return the canonical section, ``SECTION_NOT_FOR_CHANGELOG`` for
    drop categories, or ``None`` when no category was provided at all.

    A ``None`` return lets the caller distinguish "PR has no template
    section" from "PR explicitly opted out" — the former still falls
    back to the default Improvements section when a description exists.
    """
    if not category_text:
        return None
    # Strip markdown bullets and template comments before matching so a
    # body like "- Documentation" is recognised the same as plain text.
    text = re.sub(r"<!--.*?-->", "", category_text, flags=re.DOTALL).lower()
    for needle, section in _CATEGORY_PATTERNS:
        if needle in text:
            return section
    return SECTION_IMPROVEMENTS


def _description_for_pr(pr: PRInfo) -> str | None:
    """Extract the user-visible changelog description, or ``None``.

    Strict: pulls text from the body's ``Changelog entry`` section. PRs
    without that section, or whose section is empty / only template
    placeholders, return ``None``. PR title is **not** used as a
    fallback — per project rule, PRs without an explicit changelog
    entry are dropped from the release notes entirely.

    Multi-paragraph entries are joined with a single space so the entry
    keeps its full prose (e.g. "X. Previously …") on one bullet line.
    """
    section = _section_text(pr.body or "", "changelog entry")
    if not section:
        return None
    cleaned = _strip_template_chrome(section)
    if not cleaned:
        return None
    return cleaned


def _strip_template_chrome(section: str) -> str | None:
    """Drop template comments / placeholder lines and return the rest.

    Removes ``<!-- … -->`` HTML comments, lines that are pure template
    cruft (``...``, ``Description.``, ``no entry``, ``n/a``), and any
    leading bullet markers. Joins surviving lines with a single space so
    a multi-paragraph entry collapses to one prose line. Returns
    ``None`` when nothing meaningful is left.
    """
    if not section:
        return None
    text = re.sub(r"<!--.*?-->", "", section, flags=re.DOTALL)
    keep: list[str] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        # Drop leading bullet/list markers.
        line = re.sub(r"^[\-\*\+]\s+", "", line)
        if not line:
            continue
        low = line.lower()
        # ClickHouse template placeholders.
        if line == "..." or low in ("description.", "no entry", "n/a"):
            continue
        keep.append(line)
    if not keep:
        return None
    return " ".join(keep)


def _is_forward_port(pr: PRInfo) -> bool:
    if pr.title and _FWDPORT_TITLE_RE.search(pr.title):
        return True
    labels = {(lbl or "").lower() for lbl in (pr.labels or [])}
    if labels & _FWDPORT_LABELS:
        return True
    return False


# "by @handle" attribution inside a parenthetical. The ``@`` is required so
# prose like "fixed by hand" is not read as an author.
_BY_AUTHOR_RE = re.compile(r"by\s+@([A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?)")


def _trailing_paren_group(text: str) -> tuple[int, str] | None:
    """Locate the balanced ``(...)`` group that ends ``text``.

    Returns ``(start_index, inner_text)`` or ``None``. Unlike a simple
    ``\\(([^()]+)\\)$`` regex, this spans nested parens — so a markdown
    attribution like ``([#101272](url) by @x)`` is matched whole.
    """
    t = text.rstrip()
    if not t.endswith(")"):
        return None
    depth = 0
    for i in range(len(t) - 1, -1, -1):
        if t[i] == ")":
            depth += 1
        elif t[i] == "(":
            depth -= 1
            if depth == 0:
                return i, t[i + 1:len(t) - 1]
    return None  # unbalanced


def _paren_names_upstream(inner: str, upstream_prs: list[PRInfo]) -> bool:
    """True if ``inner`` references any of ``upstream_prs`` by url or slug#N.

    Only the two unambiguous forms count — the full PR url (which is also a
    substring of a ``[#N](url)`` markdown link) and the ``owner/repo#N``
    shorthand. A bare ``#N`` is NOT matched: it would strip a benign
    parenthetical whose number happens to equal an upstream PR's.
    """
    for pr in upstream_prs:
        if pr.url and pr.url in inner:
            return True
        if f"{pr.repo_slug}#{pr.number}" in inner:
            return True
    return False


def _attribution_from_text(
    inner: str, origin_slug: str,
) -> list[tuple[str, int, str | None]]:
    """Parse cross-repo ``(slug, number, author)`` from an attribution paren.

    Only full PR URLs and ``owner/repo#N`` shorthands count (a bare
    ``#N`` or ``(see notes)`` is ignored), so this won't misread an
    ordinary trailing parenthetical as an upstream link. Each ref is
    credited to the ``by @handle`` that follows it (so a paren naming two
    PRs by different authors attributes each correctly), falling back to
    the sole author when there's exactly one.
    """
    authors = [(m.start(), m.group(1)) for m in _BY_AUTHOR_RE.finditer(inner)]

    def _author_for(end_pos: int) -> str | None:
        for apos, a in authors:
            if apos >= end_pos:
                return a
        return authors[0][1] if len(authors) == 1 else None

    refs: list[tuple[int, int, str, int]] = []  # (start, end, slug, number)
    for m in _PR_URL_RE.finditer(inner):
        refs.append((m.start(), m.end(), f"{m.group(1)}/{m.group(2)}", int(m.group(3))))
    for m in _CROSSREPO_REF_RE.finditer(inner):
        refs.append((m.start(), m.end(), m.group(1), int(m.group(2))))
    refs.sort()

    out: list[tuple[str, int, str | None]] = []
    seen: set[tuple[str, int]] = set()
    for _start, end, slug, num in refs:
        if slug.lower() == origin_slug.lower():
            continue
        key = (slug.lower(), num)
        if key in seen:
            continue
        seen.add(key)
        out.append((slug, num, _author_for(end)))
    return out


def _strip_redundant_upstream_parens(
    description: str, upstream_prs: list[PRInfo],
) -> str:
    """Drop a trailing "(<upstream-url> by @author)" already in the entry.

    Altinity port PRs often copy the upstream PR's own changelog entry
    verbatim — including its trailing "(<url> by @author)" parenthetical.
    We re-derive the same info and append " via <altinity-url>", so strip
    the trailing parenthetical when it names one of the upstream PRs to
    avoid double-printing. Balanced-paren aware (handles markdown links).
    """
    if not description or not upstream_prs:
        return description
    grp = _trailing_paren_group(description)
    if grp is None:
        return description
    start, inner = grp
    if _paren_names_upstream(inner, upstream_prs):
        return description[:start].rstrip()
    return description


def _extract_upstream_refs(
    pr_body: str,
    origin_slug: str,
) -> list[tuple[str, int]]:
    """Return cross-repo PR refs listed in the body's ``Cherry-picked from`` line.

    RelEasy stamps every port PR body with a single authoritative
    ``Cherry-picked from <refs>.`` line. We extract upstream PRs only
    from that line — never from anywhere else in the body. That
    eliminates false positives from incidental URLs (e.g.
    ``Followup to: <url>``, links inside the long description, or PR
    references inside a "Changelog entry" parenthetical that we already
    handle separately).

    PRs without a ``Cherry-picked from`` line aren't ports as far as
    RelEasy is concerned — they render as direct origin entries with
    no upstream "via" suffix.

    Returns ``(slug, number)`` pairs for every PR in the line that
    lives in a repo OTHER than ``origin_slug``, preserving first-seen
    order.
    """
    if not pr_body:
        return []
    seen: set[tuple[str, int]] = set()
    out: list[tuple[str, int]] = []

    for chunk_match in _CHERRY_PICKED_FROM_RE.finditer(pr_body):
        chunk = chunk_match.group(1)
        for m in _PR_URL_RE.finditer(chunk):
            slug = f"{m.group(1)}/{m.group(2)}"
            if slug.lower() == origin_slug.lower():
                continue
            key = (slug, int(m.group(3)))
            if key in seen:
                continue
            seen.add(key)
            out.append(key)
        for m in _CROSSREPO_REF_RE.finditer(chunk):
            slug = m.group(1)
            if slug.lower() == origin_slug.lower():
                continue
            key = (slug, int(m.group(2)))
            if key in seen:
                continue
            seen.add(key)
            out.append(key)
    return out


# ---------------------------------------------------------------------------
# Git: ref dates + PR → entry
# ---------------------------------------------------------------------------


def _fetch_upstream_prs(
    config: Config,
    pr: PRInfo,
    origin_slug: str,
    upstream_cache: dict[tuple[str, int], PRInfo | None],
) -> list[PRInfo]:
    """Cross-repo backport PRs (with bodies) named in pr's Cherry-picked-from line."""
    out: list[PRInfo] = []
    for slug, number in _extract_upstream_refs(pr.body or "", origin_slug):
        key = (slug.lower(), number)
        if key in upstream_cache:
            u = upstream_cache[key]
        else:
            u = fetch_pr_by_number(config, number, slug=slug, include_closed=True)
            upstream_cache[key] = u
        if u is not None:
            out.append(u)
    return out


def _classify_and_describe(
    src_pr: PRInfo, *, label: str,
) -> tuple[str, str] | None:
    """``(section, description)`` from a PR's body, or ``None`` to drop it.

    Drops ``Not for Changelog`` PRs and PRs with no usable ``Changelog
    entry`` (PR title is never a fallback); folds a missing category into
    Improvements. ``label`` names the PR in the skip log.
    """
    section = _classify_category(_section_text(src_pr.body or "", "changelog category"))
    if section == SECTION_NOT_FOR_CHANGELOG:
        console.print(f"  [dim]not for changelog: skipping {label}[/dim]")
        return None
    description = _description_for_pr(src_pr)
    if not description:
        console.print(f"  [dim]no changelog entry: skipping {label}[/dim]")
        return None
    return (section if section is not None else SECTION_IMPROVEMENTS), description


def _entry_from_upstream(altinity_pr: PRInfo, u: PRInfo) -> ChangelogEntry | None:
    """One bullet sourced from an upstream backport PR's own changelog entry."""
    cd = _classify_and_describe(u, label=f"upstream #{u.number}")
    if cd is None:
        return None
    section, description = cd
    description = _strip_redundant_upstream_parens(description, [u])
    return ChangelogEntry(
        pr=altinity_pr, description=description, section=section, upstream_prs=[u],
    )


def _entry_from_altinity(
    pr: PRInfo,
    origin_slug: str,
    upstream_prs: list[PRInfo],
) -> ChangelogEntry | None:
    """One bullet from the port PR's own entry (single / zero-backport case)."""
    cd = _classify_and_describe(pr, label=f"#{pr.number}")
    if cd is None:
        return None
    section, description = cd

    # Copy: never mutate the caller's list (it owns the fetched upstreams).
    upstream_prs = list(upstream_prs)
    # No ``Cherry-picked from`` line (non-RelEasy / older port): recover the
    # upstream link + author from the entry's own trailing attribution so we
    # still render the "via" form instead of crediting the porter.
    if not upstream_prs:
        grp = _trailing_paren_group(description)
        if grp is not None:
            for slug, number, author in _attribution_from_text(grp[1], origin_slug):
                upstream_prs.append(PRInfo(
                    number=number, title="", body="", state="merged",
                    merge_commit_sha=None, head_sha="",
                    url=f"https://github.com/{slug}/pull/{number}",
                    repo_slug=slug, author=author,
                ))

    description = _strip_redundant_upstream_parens(description, upstream_prs)
    return ChangelogEntry(
        pr=pr, description=description, section=section, upstream_prs=upstream_prs,
    )


def _entries_for_pr(
    config: Config,
    pr: PRInfo,
    origin_slug: str,
    upstream_cache: dict[tuple[str, int], PRInfo | None],
) -> list[ChangelogEntry]:
    """Changelog bullets for one merged PR (0, 1, or N).

    A bundle of ≥2 upstream backports yields one bullet per upstream PR —
    each from its own entry, attributed ``via`` this port PR. Otherwise a
    single bullet from this PR's own entry. Drops forward-ports,
    ``Not for Changelog`` PRs, and entries with no ``Changelog entry``.
    """
    if _is_forward_port(pr):
        console.print(f"  [dim]forward-port: skipping #{pr.number}[/dim]")
        return []

    upstream_prs = _fetch_upstream_prs(config, pr, origin_slug, upstream_cache)

    if len(upstream_prs) >= 2:
        # One bullet per upstream PR that carries its own entry. An upstream
        # PR with no ``Changelog entry`` is dropped (logged) — same as a
        # standalone PR: a missing entry is the author's opt-out, not a bug.
        entries = [e for e in (_entry_from_upstream(pr, u) for u in upstream_prs) if e]
        if entries:
            return entries
        # None had a usable entry → fall back to the bundle's own entry.

    e = _entry_from_altinity(pr, origin_slug, upstream_prs)
    return [e] if e is not None else []


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def _author_handle(author: str | None) -> str:
    if not author:
        return ""
    handle = author.lstrip("@")
    return f"@{handle}"


def _render_entry(entry: ChangelogEntry) -> str:
    """Render one bullet line.

    Layout cases:
      - No upstream refs:     "* {desc} ({altinity-pr-url} by @author)"
      - Upstream refs found:  groups upstream PRs by author, then appends
        " via {altinity-pr-url}".
    """
    altinity_url = entry.pr.url
    altinity_author = _author_handle(entry.pr.author)

    if not entry.upstream_prs:
        author_part = f" by {altinity_author}" if altinity_author else ""
        return f"* {entry.description} ({altinity_url}{author_part})"

    # Group upstream PRs by author, preserving first-seen order.
    grouped: list[tuple[str, list[PRInfo]]] = []
    by_author: dict[str, list[PRInfo]] = {}
    for u in entry.upstream_prs:
        key = u.author or ""
        if key not in by_author:
            by_author[key] = []
            grouped.append((key, by_author[key]))
        by_author[key].append(u)

    chunks: list[str] = []
    for author, prs in grouped:
        urls = ", ".join(p.url for p in prs)
        if author:
            chunks.append(f"{urls} by {_author_handle(author)}")
        else:
            chunks.append(urls)
    upstream_part = ", ".join(chunks)
    return (
        f"* {entry.description} ({upstream_part} via {altinity_url})"
    )


_DISPLAY_TITLE_RE = re.compile(
    r"^v?(?P<ver>\d[\w.\-]*?)\.altinity(?P<proj>[a-z]+)$",
    re.IGNORECASE,
)


def format_display_title(tag: str) -> str:
    """Turn a release tag into a human-readable heading.

    ``v26.1.6.20001.altinityantalya`` → ``26.1.6.20001 Altinity Antalya``.
    Tags that don't match the ``…altinity<project>`` shape just lose a
    leading ``v`` and are returned otherwise as-is.
    """
    if not tag:
        return tag
    m = _DISPLAY_TITLE_RE.match(tag.strip())
    if m:
        return f"{m.group('ver')} Altinity {m.group('proj').capitalize()}"
    if tag.startswith("v") and len(tag) > 1 and tag[1].isdigit():
        return tag[1:]
    return tag


def render_packages_block(tag: str, docker_image_url: str | None = None) -> str | None:
    """Render the Packages + Docker images sections for an Altinity tag.

    Returns ``None`` for tags that don't fit the ``…altinity<project>``
    convention; the caller leaves these sections out of the changelog.

    ``docker_image_url`` overrides the default placeholder URL. The
    default keeps the canonical
    ``hub.docker.com/layers/altinity/clickhouse-server/<tag>/images/sha256-TBD``
    shape so the SHA-256 digest can be filled in mechanically after the
    image is pushed.
    """
    m = _DISPLAY_TITLE_RE.match((tag or "").strip())
    if not m:
        return None
    ver = m.group("ver")
    proj_suffix = m.group("proj").lower()  # e.g. "antalya"
    docker_tag = f"{ver}.altinity{proj_suffix}"
    builds_anchor = f"altinity{proj_suffix}"
    if docker_image_url is None:
        docker_image_url = (
            f"https://hub.docker.com/layers/altinity/clickhouse-server/"
            f"{docker_tag}/images/sha256-TBD"
        )
    return (
        "## Packages\n"
        f"Available for both AMD64 and Aarch64 from "
        f"https://builds.altinity.cloud/#{builds_anchor} as either "
        f"`.deb`, `.rpm`, or `.tgz`\n"
        "\n"
        "## Docker images\n"
        f"Available for both AMD64 and Aarch64: "
        f"[altinity/clickhouse-server:{docker_tag}]({docker_image_url})"
    )


def render_markdown(
    *,
    display_title: str,
    to_sha: str,
    from_ref_label: str,
    from_sha: str | None,
    from_url: str | None,
    entries: list[ChangelogEntry],
    full_changelog_url: str | None = None,
    packages_block: str | None = None,
) -> str:
    """Build the changelog markdown body.

    ``display_title`` is the human-friendly form used in the H3 heading
    (e.g. ``26.1.6.20001 Altinity Antalya``). The GitHub release tag is
    chosen separately by the caller.
    """
    if from_url:
        if from_sha:
            compared_to = (
                f"[`{from_ref_label} ({from_sha})`]({from_url})"
            )
        else:
            compared_to = f"[`{from_ref_label}`]({from_url})"
    else:
        suffix = f" ({from_sha})" if from_sha else ""
        compared_to = f"`{from_ref_label}{suffix}`"

    lines: list[str] = []
    lines.append(
        f"### {display_title} ({to_sha}) as compared to {compared_to}"
    )
    lines.append("")

    by_section: dict[str, list[ChangelogEntry]] = {s: [] for s in SECTION_ORDER}
    for e in entries:
        by_section.setdefault(e.section, []).append(e)

    rendered_any = False
    for section in SECTION_ORDER:
        bucket = by_section.get(section) or []
        if not bucket:
            continue
        rendered_any = True
        lines.append(f"#### {section}")
        for e in bucket:
            lines.append(_render_entry(e))
        lines.append("")

    if not rendered_any:
        lines.append("_No user-visible changes since the previous release._")
        lines.append("")

    if packages_block:
        lines.append(packages_block.rstrip())
        lines.append("")

    if full_changelog_url:
        lines.append(f"**Full Changelog**: {full_changelog_url}")
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


# ---------------------------------------------------------------------------
# Compared-to URL resolution
# ---------------------------------------------------------------------------


def _looks_like_tag(ref: str) -> bool:
    return bool(re.match(r"^v?\d+\.\d+", ref))


def _resolve_compared_to(
    config: Config,
    repo_path: Path,
    from_ref: str,
    explicit_url: str | None,
) -> tuple[str, str | None, str | None]:
    """Return (label, sha_or_none, url_or_none) for the comparison anchor.

    If ``explicit_url`` was supplied, it wins. Otherwise, when the
    upstream remote is configured and ``from_ref`` resolves to a tag on
    upstream, link to the upstream release page; failing that, link to
    the origin commit page.
    """
    sha = resolve_ref(repo_path, from_ref)

    if explicit_url:
        return from_ref, sha, explicit_url

    # Try upstream tag link.
    if config.upstream and _looks_like_tag(from_ref):
        upstream_remote = config.upstream.remote
        # Resolve owner/repo from the upstream URL to build a release page link.
        m = re.match(
            r"(?:git@github\.com:|https://github\.com/)([^/]+)/([^/\s]+?)(?:\.git)?/?$",
            upstream_remote,
        )
        if m:
            slug = f"{m.group(1)}/{m.group(2)}"
            return (
                from_ref, sha,
                f"https://github.com/{slug}/releases/tag/{from_ref}",
            )

    # Origin commit link.
    origin_slug = get_origin_repo_slug(config)
    if origin_slug and sha:
        return from_ref, sha, f"https://github.com/{origin_slug}/commit/{sha}"

    return from_ref, sha, None


# ---------------------------------------------------------------------------
# Top-level entry point
# ---------------------------------------------------------------------------


def build_changelog(
    config: Config,
    *,
    from_ref: str,
    to_ref: str,
    release_name: str,
    display_title: str | None = None,
    work_dir: Path | None = None,
    compared_to_url: str | None = None,
    docker_image_url: str | None = None,
    base_branch: str | None = None,
    explicit_prs: list[str] | None = None,
) -> tuple[str, str, bool] | None:
    """Collect PRs merged into the target branch and render the changelog.

    By default queries origin for PRs whose base is ``base_branch`` (the
    target branch) and that merged in the ``from_ref``..``to_ref`` window.
    ``explicit_prs`` (URLs) bypasses discovery and uses exactly that set.

    ``release_name`` is the GitHub release **tag** (e.g.
    ``v26.1.6.20001.altinityantalya``). ``display_title`` is the
    human-friendly heading (e.g. ``26.1.6.20001 Altinity Antalya``);
    when omitted it's auto-derived from ``release_name`` via
    :func:`format_display_title`.

    Returns ``(markdown, to_sha, to_is_tag)`` on success, ``None`` on
    failure. ``to_is_tag`` is True iff ``to_ref`` resolves to an actual
    git tag in the repo (vs. a branch / commit SHA).
    """
    origin_slug = get_origin_repo_slug(config)
    if not origin_slug:
        console.print(
            f"[red]Could not parse origin remote URL: "
            f"{config.origin.remote}[/red]"
        )
        return None

    work_dir = config.resolve_work_dir(work_dir)
    repo_path, _ = ensure_work_repo(config, work_dir)
    console.print(f"[dim]Repo: {repo_path}[/dim]")

    def _try_fetch(remote: str, *, with_tags: bool) -> bool:
        """Run ``git fetch [--tags] <remote>``; surface stderr on failure.

        Returns True on clean fetch, False otherwise. We don't raise on
        failure: a tag-collision or auth blip on one remote is not fatal
        — the resolve_ref / per-tag fallback below still has a chance.
        """
        argv = ["fetch"]
        if with_tags:
            argv.append("--tags")
        argv.append(remote)
        result = run_git(argv, repo_path, check=False)
        if result.returncode == 0:
            return True
        stderr = (result.stderr or "").strip()
        console.print(f"[yellow]fetch {remote} failed[/yellow]")
        if stderr:
            console.print(f"[dim]{stderr}[/dim]")
        return False

    # ``--tags`` so tags-only-on-the-remote (e.g. upstream stable tags
    # like v26.1.6.6-stable) are pulled in — both --from and --to commonly
    # refer to those. Tag fetches occasionally fail on an active fork
    # because of moved-tag collisions; if they do, fall back to a plain
    # branch fetch and force-refresh the specific tags we need below.
    origin_tags_fresh = False
    console.print(f"Fetching [cyan]{config.origin.remote_name}[/cyan]...", end=" ")
    if _try_fetch(config.origin.remote_name, with_tags=True):
        console.print("[green]done[/green]")
        origin_tags_fresh = True
    elif _try_fetch(config.origin.remote_name, with_tags=False):
        console.print(
            "  [dim]retried without --tags; will force-refresh --from / "
            "--to tags individually below[/dim]"
        )
    else:
        console.print(
            f"[red]Could not fetch origin ({config.origin.remote_name}).[/red] "
            "See git output above."
        )
        return None

    upstream_tags_fresh = False
    if config.upstream:
        ensure_remote(
            repo_path, config.upstream.remote_name, config.upstream.remote,
        )
        console.print(
            f"Fetching [cyan]{config.upstream.remote_name}[/cyan]...", end=" ",
        )
        if _try_fetch(config.upstream.remote_name, with_tags=True):
            console.print("[green]done[/green]")
            upstream_tags_fresh = True
        elif _try_fetch(config.upstream.remote_name, with_tags=False):
            console.print(
                "  [dim]retried without --tags; will force-refresh "
                "--from / --to tags individually below[/dim]"
            )
        else:
            # Upstream is optional — only used for resolving compared-to tags.
            console.print("[yellow]skipped[/yellow]")

    # Force-refresh the specific --from / --to tags from each configured
    # remote URL whenever the bulk ``--tags`` fetch was skipped on that
    # remote. This handles the moved-tag-collision case where the local
    # clone has a stale tag pointing at a different SHA than the remote:
    # ``+refs/tags/X:refs/tags/X`` is a forced refspec, so the local tag
    # gets overwritten.
    candidates: list[tuple[str, bool]] = [
        (config.origin.remote, origin_tags_fresh),
    ]
    if config.upstream:
        candidates.append((config.upstream.remote, upstream_tags_fresh))
    for ref in (from_ref, to_ref):
        for url, already_fresh in candidates:
            if already_fresh:
                continue
            run_git(
                ["fetch", "--no-tags", url, f"+refs/tags/{ref}:refs/tags/{ref}"],
                repo_path, check=False,
            )

    to_sha = resolve_ref(repo_path, to_ref)
    if to_sha is None:
        console.print(
            f"[red]Could not resolve --to {to_ref!r} in the repo.[/red] "
            "Pass a tag/branch/SHA that exists on origin or upstream "
            "(or configure ``upstream:`` in config.yaml)."
        )
        return None

    if resolve_ref(repo_path, from_ref) is None:
        console.print(
            f"[red]Could not resolve --from {from_ref!r} in the repo.[/red] "
            "Pass a tag/branch/SHA that exists on origin or upstream "
            "(or configure ``upstream:`` in config.yaml)."
        )
        return None

    title = display_title or format_display_title(release_name)
    packages_block = render_packages_block(release_name, docker_image_url)
    to_is_tag = is_tag_ref(repo_path, to_ref)

    # Collect the PR set: an explicit override, or the PRs merged into
    # the target branch within the from..to window (one Search query).
    upstream_cache: dict[tuple[str, int], PRInfo | None] = {}
    prs: list[PRInfo] = []
    if explicit_prs:
        console.print(
            f"Using [cyan]{len(explicit_prs)}[/cyan] PR(s) from "
            f"--prs / --prs-file..."
        )
        for url in explicit_prs:
            pr = fetch_pr_by_url(config, url, include_closed=True)
            if pr is None:
                console.print(
                    f"  [yellow]![/yellow] could not fetch {url} — skipping"
                )
                continue
            if pr.state != "merged":
                # Release notes list what shipped; an open / unmerged-closed
                # PR doesn't belong even when explicitly named.
                console.print(
                    f"  [yellow]![/yellow] {url} is {pr.state}, not merged "
                    "— skipping"
                )
                continue
            prs.append(pr)
    else:
        base = base_branch or config.target_branch or to_ref
        from_date = commit_date(repo_path, from_ref)
        to_date = commit_date(repo_path, to_ref)
        if from_date is None:
            # Without a lower bound the query would scan the branch's entire
            # history; refuse rather than emit a bogus whole-history changelog.
            console.print(
                f"[red]Could not read the commit date of --from {from_ref!r}.[/red] "
                "Cannot bound the release window."
            )
            return None
        console.print(
            f"Querying PRs merged into [cyan]{base}[/cyan] in "
            f"[cyan]{from_ref}..{to_ref}[/cyan]..."
        )
        prs = search_merged_prs_by_base(
            config, base,
            merged_from=from_date, merged_to=to_date,
            exclude_labels=sorted(_FWDPORT_LABELS),
        )
        if len(prs) >= 1000:
            # GitHub Search caps at 1000 results — never silently truncate.
            console.print(
                "  [yellow]warning:[/yellow] hit GitHub Search's 1000-result "
                "cap; some PRs may be missing. Narrow the --from..--to window."
            )
    console.print(f"  [dim]Considering {len(prs)} PR(s)[/dim]")

    entries: list[ChangelogEntry] = []
    for pr in prs:
        entries.extend(_entries_for_pr(config, pr, origin_slug, upstream_cache))

    from_label, from_sha, from_url = _resolve_compared_to(
        config, repo_path, from_ref, compared_to_url,
    )
    full_changelog_url = None
    if from_sha and origin_slug:
        full_changelog_url = (
            f"https://github.com/{origin_slug}/compare/{from_sha}...{to_sha}"
        )
    md = render_markdown(
        display_title=title,
        to_sha=to_sha,
        from_ref_label=from_label,
        from_sha=from_sha,
        from_url=from_url,
        entries=entries,
        full_changelog_url=full_changelog_url,
        packages_block=packages_block,
    )
    return md, to_sha, to_is_tag


def emit_changelog(
    config: Config,
    *,
    from_ref: str,
    to_ref: str,
    release_name: str | None,
    output_file: Path | None,
    display_title: str | None = None,
    work_dir: Path | None = None,
    compared_to_url: str | None = None,
    docker_image_url: str | None = None,
    base_branch: str | None = None,
    explicit_prs: list[str] | None = None,
) -> bool:
    """Run the changelog build and either write to file or open a draft release.

    ``release_name`` is the GitHub release tag. When ``None``, it
    defaults to ``to_ref`` for display purposes only — and on the
    GitHub draft release the tag field is left blank if ``to_ref`` is
    not an actual tag (so we don't mint a tag from a commit SHA).

    Returns True on success.
    """
    name_explicit = release_name is not None
    effective_name = release_name or to_ref
    title = display_title or format_display_title(effective_name)
    result = build_changelog(
        config,
        from_ref=from_ref,
        to_ref=to_ref,
        release_name=effective_name,
        display_title=title,
        work_dir=work_dir,
        compared_to_url=compared_to_url,
        docker_image_url=docker_image_url,
        base_branch=base_branch,
        explicit_prs=explicit_prs,
    )
    if result is None:
        return False
    markdown, to_sha, to_is_tag = result

    if output_file is not None:
        output_file = output_file.expanduser().resolve()
        output_file.parent.mkdir(parents=True, exist_ok=True)
        output_file.write_text(markdown)
        console.print(f"[green]Wrote changelog to[/green] {output_file}")
        return True

    # The release name on GitHub gets the prettified title; the tag
    # stays as the raw ref so it round-trips with origin and
    # ``git fetch``. But when --name was not provided and --to is a
    # commit / branch (not a tag), leave the tag field blank — we don't
    # want to mint a brand-new tag from a commit SHA.
    if name_explicit or to_is_tag:
        tag_name = effective_name
    else:
        tag_name = ""
        console.print(
            f"[dim]--to {to_ref!r} is not a tag and --name was not "
            f"provided; leaving the draft release's tag field blank.[/dim]"
        )

    url = create_draft_release(
        config,
        tag_name=tag_name,
        name=title,
        body=markdown,
        target_commitish=to_sha,
    )
    if not url:
        console.print(
            "[red]Failed to create draft release[/red] (token, network, or "
            "permissions issue — see logs above)."
        )
        return False
    console.print(f"[green]Draft release created:[/green] {url}")
    # Plain stdout for scripting.
    print(url)
    return True
