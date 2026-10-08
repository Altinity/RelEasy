"""``releasy changelog``: render release notes for the PRs in ``--from``..``--to``
to a file or a draft GitHub release."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from xml.etree import ElementTree

import requests

from releasy.config import Config, get_github_token
from releasy.git_ops import (
    commit_date,
    ensure_remote,
    ensure_work_repo,
    first_parent_pr_numbers,
    is_ancestor,
    is_tag_ref,
    resolve_ref_prefer_remote,
    run_git,
)
from releasy.github_ops import (
    PRInfo,
    create_draft_release,
    fetch_pr_by_number,
    fetch_pr_by_url,
    fetch_prs_by_numbers,
    get_origin_repo_slug,
    search_merged_prs_by_base,
)
from releasy.termlog import console

log = logging.getLogger(__name__)


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

SECTION_NOT_FOR_CHANGELOG = "__not_for_changelog__"


# Substring patterns over the lowercased category; first match wins, so
# more specific needles precede the ones they contain.
_CATEGORY_PATTERNS: list[tuple[str, str]] = [
    ("not for changelog", SECTION_NOT_FOR_CHANGELOG),
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

_FWDPORT_TITLE_RE = re.compile(r"forward[\s\-]?port", re.IGNORECASE)
_FWDPORT_LABELS = {"forwardport", "forward-port", "forward port"}

_CROSSREPO_REF_RE = re.compile(r"\b([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)#(\d+)\b")
_PR_URL_RE = re.compile(
    r"https?://github\.com/([^/\s)]+)/([^/\s)]+?)(?:\.git)?/pull/(\d+)\b",
)
# "Cherry-picked from …" line, continued up to the next blank line.
_CHERRY_PICKED_FROM_RE = re.compile(
    r"^Cherry-picked from\s+([^\n]+(?:\n(?!\s*$)[^\n]+)*)",
    re.IGNORECASE | re.MULTILINE,
)


@dataclass
class ChangelogEntry:
    pr: PRInfo
    description: str
    section: str
    upstream_prs: list[PRInfo] = field(default_factory=list)


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
    """Canonical section for a category, or ``None`` when no category was given."""
    if not category_text:
        return None
    text = re.sub(r"<!--.*?-->", "", category_text, flags=re.DOTALL).lower()
    for needle, section in _CATEGORY_PATTERNS:
        if needle in text:
            return section
    return SECTION_IMPROVEMENTS


def _description_for_pr(pr: PRInfo) -> str | None:
    """Text of the body's ``Changelog entry`` section (never the title), or ``None``."""
    section = _section_text(pr.body or "", "changelog entry")
    if not section:
        return None
    cleaned = _strip_template_chrome(section)
    if not cleaned:
        return None
    return cleaned


def _strip_template_chrome(section: str) -> str | None:
    """Strip comments, bullets and template placeholders; join lines with spaces."""
    if not section:
        return None
    text = re.sub(r"<!--.*?-->", "", section, flags=re.DOTALL)
    keep: list[str] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        line = re.sub(r"^[\-\*\+]\s+", "", line)
        if not line:
            continue
        low = line.lower()
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
    """``(start_index, inner_text)`` of the balanced ``(...)`` ending ``text``, or None."""
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


def _iter_balanced_parens(text: str):
    """Yield ``(start, end, inner)`` for each top-level balanced ``(...)`` group."""
    depth = 0
    start = -1
    for i, ch in enumerate(text):
        if ch == "(":
            if depth == 0:
                start = i
            depth += 1
        elif ch == ")" and depth > 0:
            depth -= 1
            if depth == 0:
                yield start, i + 1, text[start + 1:i]


def _paren_names_upstream(inner: str, upstream_prs: list[PRInfo]) -> bool:
    """True if ``inner`` names any of ``upstream_prs`` by URL or ``slug#N`` (never bare ``#N``)."""
    for pr in upstream_prs:
        if pr.url and pr.url in inner:
            return True
        if f"{pr.repo_slug}#{pr.number}" in inner:
            return True
    return False


def _attribution_from_text(
    inner: str, origin_slug: str,
) -> list[tuple[str, int, str | None]]:
    """Cross-repo ``(slug, number, author)`` refs in an attribution paren.

    Each ref is credited to the next ``by @handle``, else to the sole author.
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


def _split_inline_entries(
    section: str, origin_slug: str,
) -> list[tuple[str, list[tuple[str, int, str | None]]]] | None:
    """Split a section inlining ≥2 ``desc (<upstream-url> by @author)`` entries.

    Returns ``(description, refs)`` per entry, or ``None`` for fewer than two.
    """
    if not section:
        return None
    text = re.sub(r"<!--.*?-->", "", section, flags=re.DOTALL)
    marks: list[tuple[int, int, list[tuple[str, int, str | None]]]] = []
    for start, end, inner in _iter_balanced_parens(text):
        refs = _attribution_from_text(inner, origin_slug)
        if refs:
            marks.append((start, end, refs))
    if len(marks) < 2:
        return None

    chunks: list[list] = []  # [description, refs]
    cursor = 0
    for start, end, refs in marks:
        desc = _strip_template_chrome(text[cursor:start])
        cursor = end
        if desc:
            chunks.append([desc, list(refs)])
        elif chunks:
            # Attribution with no preceding description → a further link for
            # the previous entry (e.g. "Fix X (url1) (url2)").
            chunks[-1][1].extend(refs)
    if len(chunks) < 2:
        return None
    return [(desc, refs) for desc, refs in chunks]


def _strip_redundant_upstream_parens(
    description: str, upstream_prs: list[PRInfo],
) -> str:
    """Drop a trailing parenthetical naming an upstream PR (rendering re-adds it)."""
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
    """Non-origin ``(slug, number)`` refs from the ``Cherry-picked from`` line only."""
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
    """``(section, description)`` from a PR's body, or ``None`` to drop it."""
    section = _classify_category(_section_text(src_pr.body or "", "changelog category"))
    if section == SECTION_NOT_FOR_CHANGELOG:
        console.print(f"  [dim]not for changelog: skipping {label}[/dim]")
        return None
    description = _description_for_pr(src_pr)
    if not description:
        console.print(f"  [dim]no changelog entry: skipping {label}[/dim]")
        return None
    return (section if section is not None else SECTION_IMPROVEMENTS), description


def _stub_upstream_pr(slug: str, number: int, author: str | None) -> PRInfo:
    """Render-only PRInfo (url, slug, author) for an upstream ref found in entry text."""
    return PRInfo(
        number=number, title="", body="", state="merged",
        merge_commit_sha=None, head_sha="",
        url=f"https://github.com/{slug}/pull/{number}",
        repo_slug=slug, author=author,
    )


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

    upstream_prs = list(upstream_prs)
    # No ``Cherry-picked from`` line: recover upstream refs from the trailing attribution.
    if not upstream_prs:
        grp = _trailing_paren_group(description)
        if grp is not None:
            for slug, number, author in _attribution_from_text(grp[1], origin_slug):
                upstream_prs.append(_stub_upstream_pr(slug, number, author))

    description = _strip_redundant_upstream_parens(description, upstream_prs)
    return ChangelogEntry(
        pr=pr, description=description, section=section, upstream_prs=upstream_prs,
    )


def _entries_from_inline_split(
    pr: PRInfo, origin_slug: str,
) -> list[ChangelogEntry] | None:
    """One bullet per inlined entry (see :func:`_split_inline_entries`), or ``None``."""
    chunks = _split_inline_entries(
        _section_text(pr.body or "", "changelog entry") or "", origin_slug,
    )
    if not chunks:
        return None
    category = _classify_category(
        _section_text(pr.body or "", "changelog category")
    )
    if category == SECTION_NOT_FOR_CHANGELOG:
        console.print(f"  [dim]not for changelog: skipping #{pr.number}[/dim]")
        return []
    section = category if category is not None else SECTION_IMPROVEMENTS
    return [
        ChangelogEntry(
            pr=pr, description=desc, section=section,
            upstream_prs=[_stub_upstream_pr(s, n, a) for s, n, a in refs],
        )
        for desc, refs in chunks
    ]


def _entries_for_pr(
    config: Config,
    pr: PRInfo,
    origin_slug: str,
    upstream_cache: dict[tuple[str, int], PRInfo | None],
) -> list[ChangelogEntry]:
    """Changelog bullets for one merged PR: one per bundled backport, else one."""
    if _is_forward_port(pr):
        console.print(f"  [dim]forward-port: skipping #{pr.number}[/dim]")
        return []

    upstream_prs = _fetch_upstream_prs(config, pr, origin_slug, upstream_cache)

    if len(upstream_prs) >= 2:
        entries = [e for e in (_entry_from_upstream(pr, u) for u in upstream_prs) if e]
        if entries:
            return entries

    if len(upstream_prs) < 2:
        split = _entries_from_inline_split(pr, origin_slug)
        if split is not None:
            return split

    e = _entry_from_altinity(pr, origin_slug, upstream_prs)
    return [e] if e is not None else []


def _author_handle(author: str | None) -> str:
    if not author:
        return ""
    handle = author.lstrip("@")
    return f"@{handle}"


def _render_entry(entry: ChangelogEntry) -> str:
    """``* desc (url by @a)``, or ``* desc (upstream urls by @a via url)``."""
    altinity_url = entry.pr.url
    altinity_author = _author_handle(entry.pr.author)

    if not entry.upstream_prs:
        author_part = f" by {altinity_author}" if altinity_author else ""
        return f"* {entry.description} ({altinity_url}{author_part})"

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
    """``v26.1.6.20001.altinityantalya`` → ``26.1.6.20001 Altinity Antalya``."""
    if not tag:
        return tag
    m = _DISPLAY_TITLE_RE.match(tag.strip())
    if m:
        return f"{m.group('ver')} Altinity {m.group('proj').capitalize()}"
    if tag.startswith("v") and len(tag) > 1 and tag[1].isdigit():
        return tag[1:]
    return tag


def render_packages_block(tag: str, docker_image_url: str | None = None) -> str | None:
    """Packages + Docker images sections for an Altinity tag, or ``None``."""
    m = _DISPLAY_TITLE_RE.match((tag or "").strip())
    if not m:
        return None
    ver = m.group("ver")
    proj_suffix = m.group("proj").lower()
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


# CI artefacts live under ``REFs/<ref>/<sha>/<workflow-run-id>/`` in this bucket.
_BUILD_ARTIFACTS_BASE = "https://s3.amazonaws.com/altinity-build-artifacts"
_S3_NS = "{http://s3.amazonaws.com/doc/2006-03-01/}"
_RUN_ID_PLACEHOLDER = "RUN-ID-TBD"


def _lookup_ci_run_id(ref: str, sha: str, *, timeout: int = 30) -> str | None:
    """Highest CI workflow-run id published under ``REFs/<ref>/<sha>/``, or None.

    Not paginated: S3 lists prefixes lexicographically, so the all-digit
    run ids come before task-name prefixes on the first page.
    """
    try:
        resp = requests.get(
            f"{_BUILD_ARTIFACTS_BASE}/",
            params={
                "list-type": "2",
                "prefix": f"REFs/{ref}/{sha}/",
                "delimiter": "/",
            },
            timeout=timeout,
        )
    except Exception as exc:
        log.debug("build-report lookup failed: %s", exc)
        return None
    if resp.status_code != 200:
        log.debug("build-report lookup -> HTTP %s", resp.status_code)
        return None
    try:
        root = ElementTree.fromstring(resp.content)
    except ElementTree.ParseError as exc:
        log.debug("build-report listing is not valid XML: %s", exc)
        return None

    run_ids: list[str] = []
    for node in root.iter(f"{_S3_NS}CommonPrefixes"):
        prefix = (node.findtext(f"{_S3_NS}Prefix") or "").rstrip("/")
        leaf = prefix.rsplit("/", 1)[-1]
        if leaf.isdigit():
            run_ids.append(leaf)
    if not run_ids:
        return None
    return max(run_ids, key=int)


def render_build_report_block(
    ref: str, sha: str, build_report_url: str | None = None,
) -> str | None:
    """Build report section; ``None`` for non-Altinity refs without an explicit URL."""
    if build_report_url is None:
        if not _DISPLAY_TITLE_RE.match((ref or "").strip()):
            return None
        run_id = _lookup_ci_run_id(ref, sha)
        if run_id is None:
            run_id = _RUN_ID_PLACEHOLDER
            console.print(
                f"[yellow]No CI run published under REFs/{ref}/{sha[:11]}"
                f"[/yellow] — build report link left as {_RUN_ID_PLACEHOLDER}."
            )
        build_report_url = (
            f"{_BUILD_ARTIFACTS_BASE}/REFs/{ref}/{sha}/"
            f"{run_id}/ci_run_report.html"
        )
    return f"## [Build report]({build_report_url})"


# Projects whose docs.altinity.com release notes are split by <major>.<minor>.
_RELEASE_NOTES_BASE = "https://docs.altinity.com/releasenotes"
_RELEASE_NOTES_PROJECTS = {"antalya", "stable"}


def render_release_notes_block(
    tag: str, release_notes_url: str | None = None,
) -> str | None:
    """Release notes section; ``None`` when no URL is given or derivable."""
    if release_notes_url is None:
        m = _DISPLAY_TITLE_RE.match((tag or "").strip())
        if not m:
            return None
        proj = m.group("proj").lower()
        if proj not in _RELEASE_NOTES_PROJECTS:
            return None
        parts = m.group("ver").split(".")
        if len(parts) < 2:
            return None
        release_notes_url = (
            f"{_RELEASE_NOTES_BASE}/altinity-{proj}-release-notes/"
            f"{parts[0]}.{parts[1]}/"
        )
    return f"## [Release notes]({release_notes_url})"


def render_markdown(
    *,
    display_title: str,
    to_sha: str,
    from_ref_label: str,
    from_sha: str | None,
    from_url: str | None,
    entries: list[ChangelogEntry],
    full_changelog_url: str | None = None,
    build_report_block: str | None = None,
    release_notes_block: str | None = None,
    packages_block: str | None = None,
) -> str:
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

    if build_report_block:
        lines.append(build_report_block.rstrip())
        lines.append("")

    if release_notes_block:
        lines.append(release_notes_block.rstrip())
        lines.append("")

    if packages_block:
        lines.append(packages_block.rstrip())
        lines.append("")

    if full_changelog_url:
        lines.append(f"**Full Changelog**: {full_changelog_url}")
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def _looks_like_tag(ref: str) -> bool:
    return bool(re.match(r"^v?\d+\.\d+", ref))


def _resolve_compared_to(
    config: Config,
    from_ref: str,
    sha: str,
) -> tuple[str, str | None]:
    """``(label, url)``: upstream release page for tags, else origin commit page."""
    if config.upstream and _looks_like_tag(from_ref):
        upstream_remote = config.upstream.remote
        m = re.match(
            r"(?:git@github\.com:|https://github\.com/)([^/]+)/([^/\s]+?)(?:\.git)?/?$",
            upstream_remote,
        )
        if m:
            slug = f"{m.group(1)}/{m.group(2)}"
            return (
                from_ref,
                f"https://github.com/{slug}/releases/tag/{from_ref}",
            )

    origin_slug = get_origin_repo_slug(config)
    if origin_slug:
        return from_ref, f"https://github.com/{origin_slug}/commit/{sha}"

    return from_ref, None


def build_changelog(
    config: Config,
    *,
    from_ref: str,
    to_ref: str,
    release_name: str,
    display_title: str | None = None,
    work_dir: Path | None = None,
    docker_image_url: str | None = None,
    build_report_url: str | None = None,
    release_notes_url: str | None = None,
    base_branch: str | None = None,
    explicit_prs: list[str] | None = None,
) -> tuple[str, str, bool] | None:
    """Render the changelog for ``from_ref``..``to_ref``.

    Returns ``(markdown, to_sha, to_is_tag)``, or ``None`` on failure.
    """
    origin_slug = get_origin_repo_slug(config)
    if not origin_slug:
        console.print(
            f"[red]Could not parse origin remote URL: "
            f"{config.origin.remote}[/red]"
        )
        return None

    if not get_github_token():
        console.print(
            "[red]RELEASY_GITHUB_TOKEN not set[/red] — cannot fetch PRs."
        )
        return None

    work_dir = config.resolve_work_dir(work_dir)
    repo_path, _ = ensure_work_repo(config, work_dir)
    console.print(f"[dim]Repo: {repo_path}[/dim]")

    def _try_fetch(remote: str, *, with_tags: bool) -> bool:
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

    # A --tags fetch can fail on moved-tag collisions; then fetch branches
    # only and force-refresh the --from / --to tags below.
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
            console.print("[yellow]skipped[/yellow]")

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

    # Prefer origin: a stale local branch of the same name must never win.
    remote_name = config.origin.remote_name
    resolved: dict[str, str] = {}
    for flag, ref in (("--from", from_ref), ("--to", to_ref)):
        hit = resolve_ref_prefer_remote(repo_path, ref, remote_name)
        if hit is None:
            console.print(
                f"[red]Could not resolve {flag} {ref!r} in the repo.[/red] "
                "Pass a tag/branch/SHA that exists on origin or upstream "
                "(or configure ``upstream:`` in config.yaml)."
            )
            return None
        sha, kind = hit
        if kind == "local-branch":
            console.print(
                f"[yellow]{flag} {ref!r} is a local branch not on "
                f"{remote_name}[/yellow] — using it as-is ({sha[:11]})."
            )
        resolved[flag] = sha
    from_sha = resolved["--from"]
    to_sha = resolved["--to"]

    title = display_title or format_display_title(release_name)
    packages_block = render_packages_block(release_name, docker_image_url)
    build_report_block = render_build_report_block(
        release_name, to_sha, build_report_url,
    )
    release_notes_block = render_release_notes_block(
        release_name, release_notes_url,
    )
    to_is_tag = is_tag_ref(repo_path, to_ref)

    # PR set: explicit list; else the first-parent PRs of from..to when
    # from is an ancestor; else a date-window search by base branch.
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
                console.print(
                    f"  [yellow]![/yellow] {url} is {pr.state}, not merged "
                    "— skipping"
                )
                continue
            prs.append(pr)
    elif ancestry := is_ancestor(repo_path, from_sha, to_sha):
        if base_branch:
            console.print("  [dim]--base ignored — walking the commit range[/dim]")
        numbers = first_parent_pr_numbers(repo_path, from_sha, to_sha)
        console.print(
            f"Walking [cyan]{from_ref}..{to_ref}[/cyan] — "
            f"[cyan]{len(numbers)}[/cyan] PR(s) merged onto the branch..."
        )
        fetched = fetch_prs_by_numbers(
            config, numbers, slug=origin_slug, include_closed=True,
        )
        # value None = transient fetch failure (refuse — would ship incomplete);
        # key absent = 404 / bad subject parse (skip). See fetch_prs_by_numbers.
        failed = [n for n in numbers if n in fetched and fetched[n] is None]
        if failed:
            console.print(
                f"[red]Could not fetch {len(failed)} PR(s) in the range: "
                f"{', '.join(f'#{n}' for n in failed)}.[/red] "
                "Refusing to draft an incomplete changelog."
            )
            return None
        for n in numbers:
            pr = fetched.get(n)
            if pr is None:
                console.print(
                    f"  [yellow]![/yellow] #{n} is not a PR on {origin_slug} "
                    "— skipping"
                )
                continue
            if pr.state != "merged":
                console.print(
                    f"  [yellow]![/yellow] #{n} is {pr.state}, not merged "
                    "— skipping"
                )
                continue
            prs.append(pr)
    else:
        anc_unknown = ancestry is None
        base = base_branch or config.target_branch or to_ref
        from_date = commit_date(repo_path, from_sha)
        to_date = commit_date(repo_path, to_sha)
        if from_date is None or to_date is None:
            missing = from_ref if from_date is None else to_ref
            console.print(
                f"[red]Could not read the commit date of {missing!r}.[/red] "
                "Cannot bound the release window."
            )
            return None
        if datetime.fromisoformat(from_date) > datetime.fromisoformat(to_date):
            console.print(
                f"[red]--from {from_ref!r} is newer than --to {to_ref!r}[/red]; "
                "the release window is reversed."
            )
            return None
        reason = (
            "could not determine --from/--to ancestry"
            if anc_unknown else "--from is not an ancestor of --to"
        )
        console.print(
            f"[yellow]{reason}[/yellow]; querying PRs merged into "
            f"[cyan]{base}[/cyan] in [cyan]{from_ref}..{to_ref}[/cyan] by date..."
        )
        prs = search_merged_prs_by_base(
            config, base,
            merged_from=from_date, merged_to=to_date,
            exclude_labels=sorted(_FWDPORT_LABELS),
        )
        if len(prs) >= 1000:
            console.print(
                "  [yellow]warning:[/yellow] hit GitHub Search's 1000-result "
                "cap; some PRs may be missing. Narrow the --from..--to window."
            )
    console.print(f"  [dim]Considering {len(prs)} PR(s)[/dim]")

    if not prs:
        console.print(
            f"[red]No PRs found in {from_ref}..{to_ref}.[/red] "
            "Nothing to draft — check the --from / --to range."
        )
        return None

    entries: list[ChangelogEntry] = []
    for pr in prs:
        entries.extend(_entries_for_pr(config, pr, origin_slug, upstream_cache))

    from_label, from_url = _resolve_compared_to(config, from_ref, from_sha)
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
        build_report_block=build_report_block,
        release_notes_block=release_notes_block,
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
    docker_image_url: str | None = None,
    build_report_url: str | None = None,
    release_notes_url: str | None = None,
    base_branch: str | None = None,
    explicit_prs: list[str] | None = None,
) -> bool:
    """Build the changelog and write it to ``output_file`` or a draft release.

    Without ``release_name`` the draft's tag is ``to_ref`` only when it is a tag.
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
        docker_image_url=docker_image_url,
        build_report_url=build_report_url,
        release_notes_url=release_notes_url,
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
    print(url)  # bare URL on stdout for scripts
    return True
