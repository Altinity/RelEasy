"""Discover failed CI checks on a PR and parse the human-readable JSON reports.

Altinity ClickHouse CI surfaces test results in two ways:

1. **GitHub Actions check-runs** — opaque job logs (e.g. ``PR / Fast test
   (pull_request)``). These are the raw workflow output; we deliberately
   ignore them.
2. **GitHub commit statuses** — one entry per logical task whose
   ``target_url`` points at a hosted ``json.html`` viewer (the
   ``praktika`` report). The viewer fetches a sibling ``result_<task>.json``
   from the same S3 bucket and renders it; that JSON is the structured,
   machine-readable form of "which tests passed / failed".

This module is the bridge between (2) and a list of failed-test records
``analyze-fails`` can hand to Claude one by one. The shapes of the JSON
report are not formally documented anywhere — what's encoded here is the
result of reverse-engineering the live viewer code.

Pure functions only. No git, no Claude, no state. Network access is
limited to the GitHub statuses API and an S3 bucket holding the
artefacts; both are read-only.
"""

from __future__ import annotations

import json
import re
import urllib.parse
from dataclasses import dataclass, field
from typing import Any, Iterable

import requests

from releasy.config import Config, get_github_token
from releasy.github_ops import parse_pr_url


# ---------------------------------------------------------------------------
# Status / target_url parsing
# ---------------------------------------------------------------------------


# The viewer normalises a task display name into a filename slug by
# lower-casing, mapping every non-alphanumeric run to ``_``, and stripping
# trailing underscores. Mirrored from the JS so we hit the same S3 keys.
def _normalize_task_name(name: str) -> str:
    s = name.lower()
    s = re.sub(r"[^a-z0-9]", "_", s)
    s = re.sub(r"_+", "_", s)
    return s.rstrip("_")


@dataclass
class ArtifactLocator:
    """Coordinates pinning a single ``result_*.json`` artefact in S3.

    ``base_url`` is the bucket host (with no trailing slash) — the same
    origin that served ``json.html``. ``pr`` / ``ref`` are mutually
    exclusive: GitHub PR runs key by ``PRs/<number>/``, while branch /
    ref runs key by ``REFs/<refname>/``. We only ever construct the PR
    flavour here, but the field is kept so the dataclass mirrors the
    shape the viewer accepts.
    """
    base_url: str
    pr: str | None
    sha: str
    name_0: str
    name_1: str | None  # the leaf task name, e.g. "Stateless tests (...)"
    ref: str | None = None

    def result_json_url(self) -> str:
        """Compose the S3 URL of the JSON artefact for this locator's leaf task."""
        leaf = self.name_1 if self.name_1 else self.name_0
        if self.pr:
            suffix = f"PRs/{urllib.parse.quote(self.pr, safe='')}"
        elif self.ref:
            suffix = f"REFs/{urllib.parse.quote(self.ref, safe='')}"
        else:
            raise ValueError("ArtifactLocator needs either pr or ref set")
        slug = _normalize_task_name(leaf)
        return (
            f"{self.base_url.rstrip('/')}/{suffix}/"
            f"{urllib.parse.quote(self.sha, safe='')}/result_{slug}.json"
        )


def _artifact_locator_from_target_url(url: str) -> ArtifactLocator | None:
    """Parse a ``json.html?...`` target URL into the artefact coordinates.

    Returns ``None`` for anything that isn't a recognisable praktika
    viewer URL (e.g. a GitHub Actions job log) — callers use this as a
    classifier for "is this status a parsed-report status, or just a
    GitHub job log?".
    """
    try:
        parts = urllib.parse.urlsplit(url)
    except ValueError:
        return None
    if not parts.scheme or not parts.netloc:
        return None
    if not parts.path.endswith("/json.html") and not parts.path.endswith(
        "json.html",
    ):
        return None

    qs = urllib.parse.parse_qs(parts.query, keep_blank_values=False)
    pr = (qs.get("PR") or [None])[0]
    ref = (qs.get("REF") or [None])[0]
    sha = (qs.get("sha") or [None])[0]
    name_0 = (qs.get("name_0") or [None])[0]
    name_1 = (qs.get("name_1") or [None])[0]
    base_url_qs = (qs.get("base_url") or [None])[0]
    if not (pr or ref) or not sha or not name_0:
        return None

    if base_url_qs:
        base_url = base_url_qs.rstrip("/")
    else:
        # The viewer falls back to ``window.location.origin + dirname``
        # when no base_url is supplied. For the canonical
        # ``…/json.html`` URL the dirname is ``/``, so the bucket origin
        # is the right base.
        base_url = f"{parts.scheme}://{parts.netloc}"
    return ArtifactLocator(
        base_url=base_url, pr=pr, sha=sha, name_0=name_0, name_1=name_1,
        ref=ref,
    )


# Categories worth running per-test analysis on. Anything else with a
# parseable artefact URL gets reported but skipped (e.g. Build statuses
# expose a JSON report too, but a "Build failed" doesn't decompose into
# per-test diagnostics).
TestCategory = str  # "fasttest" | "stateless" | "integration"


_NAME_CATEGORY_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("fasttest", re.compile(r"^Fast\s*test\b", re.IGNORECASE)),
    ("stateless", re.compile(r"^Stateless\s*tests?\b", re.IGNORECASE)),
    ("integration", re.compile(r"^Integration\s*tests?\b", re.IGNORECASE)),
)


def category_from_name(name: str) -> TestCategory | None:
    """Classify a status context name into a known test category.

    ``None`` for builds, regression suites, GitHub-Actions check-runs,
    etc. — anything we don't have a per-test analyser for.
    """
    for cat, pat in _NAME_CATEGORY_PATTERNS:
        if pat.search(name):
            return cat
    return None


# ---------------------------------------------------------------------------
# Failed-status discovery via GitHub commit statuses
# ---------------------------------------------------------------------------


@dataclass
class FailedStatus:
    """One failed CI status with enough context to fetch its parsed report.

    ``locator`` is ``None`` when the status's ``target_url`` doesn't point
    at a praktika report (e.g. a raw GitHub Actions job log) — those are
    surfaced for the operator to see but don't drive per-test analysis.
    """
    context: str
    state: str  # "failure" | "error"
    target_url: str
    description: str
    category: TestCategory | None
    locator: ArtifactLocator | None
    updated_at: str | None = None


def _fetch_combined_statuses(
    owner: str, repo: str, sha: str, token: str,
) -> list[dict[str, Any]]:
    """Page through the commit-statuses endpoint and return the raw entries.

    The endpoint returns the most-recent status per page in descending
    ``updated_at`` order; we collect every page so we can dedupe by
    ``context`` to the latest update across the whole list.
    """
    out: list[dict[str, Any]] = []
    url = (
        f"https://api.github.com/repos/{owner}/{repo}/commits/{sha}/statuses"
        f"?per_page=100"
    )
    headers = {
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {token}",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    seen_pages = 0
    while url and seen_pages < 50:  # 5000 statuses ought to be enough
        resp = requests.get(url, headers=headers, timeout=30)
        if resp.status_code != 200:
            raise RuntimeError(
                f"GitHub statuses API returned HTTP {resp.status_code}: "
                f"{resp.text[:300]}"
            )
        out.extend(resp.json() or [])
        # Pagination is exposed via the Link header.
        nxt = _next_link(resp.headers.get("Link", ""))
        url = nxt
        seen_pages += 1
    return out


_NEXT_LINK_RE = re.compile(r'<([^>]+)>;\s*rel="next"')


def _next_link(link_header: str) -> str | None:
    if not link_header:
        return None
    m = _NEXT_LINK_RE.search(link_header)
    return m.group(1) if m else None


def fetch_failed_statuses(
    owner: str, repo: str, sha: str,
) -> tuple[list[FailedStatus], str | None]:
    """Return every failed/errored CI status on ``sha`` (latest per context).

    Errors return ``(partial_list_or_empty, message)``. Successful runs
    return ``([…], None)``.
    """
    token = get_github_token()
    if not token:
        return [], (
            "RELEASY_GITHUB_TOKEN not set — cannot fetch CI statuses"
        )
    try:
        raw = _fetch_combined_statuses(owner, repo, sha, token)
    except Exception as exc:
        return [], f"GitHub statuses lookup failed: {exc}"

    # Latest entry per context wins. The endpoint returns
    # most-recent-first, so the first occurrence is authoritative.
    seen: dict[str, dict[str, Any]] = {}
    for entry in raw:
        ctx = entry.get("context") or ""
        if not ctx:
            continue
        if ctx in seen:
            continue
        seen[ctx] = entry

    out: list[FailedStatus] = []
    for ctx, entry in seen.items():
        state = (entry.get("state") or "").lower()
        if state not in ("failure", "error"):
            continue
        target_url = entry.get("target_url") or ""
        locator = _artifact_locator_from_target_url(target_url)
        out.append(FailedStatus(
            context=ctx,
            state=state,
            target_url=target_url,
            description=(entry.get("description") or "").strip(),
            category=category_from_name(ctx),
            locator=locator,
            updated_at=entry.get("updated_at"),
        ))
    # Stable display order: failed Fast → Stateless → Integration → others.
    _category_order = {"fasttest": 0, "stateless": 1, "integration": 2}
    out.sort(key=lambda s: (
        _category_order.get(s.category or "", 99),
        s.context,
    ))
    return out, None


# ---------------------------------------------------------------------------
# JSON-report fetching + walking
# ---------------------------------------------------------------------------


def fetch_report_json(
    locator: ArtifactLocator, *, timeout: int = 60,
) -> tuple[dict[str, Any] | None, str | None]:
    """Fetch and decode the praktika ``result_*.json`` for ``locator``.

    Returns ``(json, None)`` on success, ``(None, message)`` on failure.
    The bucket gzip-encodes responses regardless of extension; we let
    ``requests`` transparently decompress.
    """
    url = locator.result_json_url()
    try:
        resp = requests.get(url, timeout=timeout)
    except Exception as exc:
        return None, f"GET {url} failed: {exc}"
    if resp.status_code == 403:
        return None, (
            f"Report not yet uploaded or expired ({url}). The CI run may "
            "still be in progress, or the artefact has been pruned."
        )
    if resp.status_code != 200:
        return None, (
            f"GET {url} → HTTP {resp.status_code}; first 200 chars: "
            f"{resp.text[:200]!r}"
        )
    try:
        return resp.json(), None
    except json.JSONDecodeError as exc:
        return None, f"Could not parse JSON from {url}: {exc}"


# Statuses that mean "this leaf is broken and worth handing to Claude".
#
# Grounded in upstream ``ci/praktika/result.py``:
#
#   - ``Result.is_failure()`` →  ``FAILED``/``FAIL``/``XPASS``
#   - ``Result.is_error()``   →  ``ERROR`` (both ``Status`` and ``StatusExtended``)
#   - ``Result.is_ok()``      →  ``OK``/``SUCCESS``/``SKIPPED``/``BROKEN``/``XFAIL``
#
# ``BROKEN`` looks like a failure but isn't: upstream classifies it as
# OK (``ci/jobs/integration_test_job.py`` actively *downgrades* a FAIL
# to BROKEN when the test matches the known-broken rules — that's the
# project's way of muting expected-broken results). Same for ``XFAIL``
# (expected-failure annotation, also is_ok). ``UNKNOWN`` (set in
# functional_tests_results.py after a server crash to mute noise) is
# also deliberately excluded.
#
# ``Timeout`` (note: literal mixed case, written by
# ``functional_tests_results.py`` for the ``Timeout!`` marker) is a
# real failure — counted in ``failed`` upstream. We upper-case before
# comparing, so the lookup key is ``TIMEOUT``.
#
# ``XPASS`` (pytest "unexpected pass") IS a failure per praktika's
# ``is_failure()`` — included so integration tests' xpassed leaves
# don't slip through silently.
_FAILED_LEAF_STATUSES = frozenset({
    "FAIL",
    "ERROR",
    "XPASS",
    "TIMEOUT",
})


# Praktika fasttest reports bundle a runner-level pseudo-leaf named
# ``clickhouse-test`` alongside the real per-test leaves. Emitted by
# ``ci/jobs/fast_test.py`` when the ``clickhouse-test`` invocation
# itself errors out (status=FAIL, info="clickhouse-test error"). It
# mirrors the umbrella failure rather than carrying independent
# diagnostic value, so feeding it to Claude would just have it re-run
# the entire suite. Skipped at extraction time.
_META_LEAF_NAMES = frozenset({"clickhouse-test"})


@dataclass
class FailedTest:
    """One failed individual test extracted from a praktika report.

    ``shard_context`` is the commit-status context that surfaced the
    failure (e.g. ``Stateless tests (arm_asan, azure, parallel, 2/4)``).
    ``info_excerpt`` is the parsed report's per-test info string trimmed
    so the prompt stays compact — Claude can still hit the artefact URL
    if it needs the full thing.
    """
    name: str
    status: str
    category: TestCategory
    shard_context: str
    target_url: str
    info_excerpt: str = ""
    files: list[str] = field(default_factory=list)
    links: list[str] = field(default_factory=list)


def _iter_failed_leaves(
    node: dict[str, Any], *, depth: int = 0,
) -> Iterable[dict[str, Any]]:
    """Yield every leaf in the report tree whose status is FAIL/ERROR/etc.

    The praktika report is recursive: ``results`` may hold further
    ``results`` nodes. We treat a node as a "leaf failure" when its
    status is in ``_FAILED_LEAF_STATUSES`` AND it has no ``results`` of
    its own — that filters out aggregate "Tests" failure rows that just
    summarise per-test failures we'd otherwise count twice.

    Praktika meta-leaves listed in :data:`_META_LEAF_NAMES` (e.g.
    ``clickhouse-test`` in Fast test / Stateless tests reports) are
    skipped — they mirror the rolled-up status, not an independent
    failure, so handing them to Claude would only widen the runner
    invocation pointlessly.
    """
    children = node.get("results") or []
    status = (node.get("status") or "").upper()
    name = (node.get("name") or "").strip()
    if status in _FAILED_LEAF_STATUSES and not children:
        if name in _META_LEAF_NAMES:
            return
        yield node
        return
    for child in children:
        if not isinstance(child, dict):
            continue
        yield from _iter_failed_leaves(child, depth=depth + 1)


_INFO_EXCERPT_MAX = 4000


def extract_failed_tests(
    report: dict[str, Any],
    *,
    category: TestCategory,
    shard_context: str,
    target_url: str,
) -> list[FailedTest]:
    """Walk the praktika tree and collect failed leaves as ``FailedTest``."""
    out: list[FailedTest] = []
    for leaf in _iter_failed_leaves(report):
        info = (leaf.get("info") or "").rstrip()
        if len(info) > _INFO_EXCERPT_MAX:
            info = info[:_INFO_EXCERPT_MAX] + "\n…(truncated)"
        files = list(leaf.get("files") or []) if isinstance(
            leaf.get("files"), list,
        ) else []
        links = list(leaf.get("links") or []) if isinstance(
            leaf.get("links"), list,
        ) else []
        out.append(FailedTest(
            name=str(leaf.get("name") or "<unnamed>"),
            status=str(leaf.get("status") or "FAIL").upper(),
            category=category,
            shard_context=shard_context,
            target_url=target_url,
            info_excerpt=info,
            files=files,
            links=links,
        ))
    return out


# ---------------------------------------------------------------------------
# High-level: discover failures for one PR
# ---------------------------------------------------------------------------


@dataclass
class PRFailures:
    """All actionable CI failures on a single PR's head commit."""
    pr_url: str
    head_sha: str
    head_ref: str
    base_ref: str
    statuses: list[FailedStatus]
    failed_tests: list[FailedTest]
    skipped_status_warnings: list[str] = field(default_factory=list)


def discover_pr_failures(
    config: Config,
    pr_url: str,
    *,
    head_sha: str | None = None,
    head_ref: str | None = None,
    base_ref: str | None = None,
    categories: tuple[TestCategory, ...] = (
        "fasttest", "stateless", "integration",
    ),
) -> tuple[PRFailures | None, str | None]:
    """Resolve a PR's head, list failed statuses, and parse each report.

    Lookups are best-effort per status — a single broken artefact URL
    surfaces as a string in ``skipped_status_warnings`` rather than
    failing the whole call.
    """
    parsed = parse_pr_url(pr_url)
    if parsed is None:
        return None, f"Could not parse PR URL: {pr_url!r}"
    owner, repo, number = parsed

    if head_sha is None or head_ref is None or base_ref is None:
        token = get_github_token()
        if not token:
            return None, "RELEASY_GITHUB_TOKEN not set — cannot fetch PR head"
        try:
            from github import Github  # noqa: F401  — type-check that it imports

            from github import Github as _Github
            gh = _Github(token)
            ghrepo = gh.get_repo(f"{owner}/{repo}")
            pr = ghrepo.get_pull(number)
            head_sha = head_sha or pr.head.sha
            head_ref = head_ref or pr.head.ref
            base_ref = base_ref or pr.base.ref
        except Exception as exc:
            return None, f"PR head lookup failed: {exc}"

    statuses, err = fetch_failed_statuses(owner, repo, head_sha)
    if err:
        return None, err

    cat_set = set(categories)
    failed_tests: list[FailedTest] = []
    warnings: list[str] = []
    for st in statuses:
        if st.category not in cat_set:
            continue
        if st.locator is None:
            warnings.append(
                f"{st.context}: target_url is not a praktika report "
                f"({st.target_url or 'no target_url'}) — skipping"
            )
            continue
        if st.locator.pr is None and pr_url:
            # Replace any missing PR coordinate with the one we know.
            st.locator.pr = str(number)
        report, ferr = fetch_report_json(st.locator)
        if ferr or report is None:
            warnings.append(f"{st.context}: {ferr or 'empty report'}")
            continue
        leaves = extract_failed_tests(
            report,
            category=st.category,
            shard_context=st.context,
            target_url=st.target_url,
        )
        if not leaves:
            # Status said failure but no per-test failures came through —
            # could be an infrastructure failure (e.g. job killed before
            # the test phase). Surface as a warning so the operator
            # knows we won't act on it.
            warnings.append(
                f"{st.context}: status is {st.state} but report has no "
                "FAIL leaves — likely an infrastructure / build issue, "
                "not a per-test failure."
            )
            continue
        failed_tests.extend(leaves)

    # Dedupe: the same test name commonly fails in multiple shards of the
    # same suite. Keep the first occurrence so callers get one record per
    # (category, name) pair, but remember the other shards in
    # ``info_excerpt`` so Claude knows it's not shard-specific.
    seen: dict[tuple[str, str], FailedTest] = {}
    extra_shards: dict[tuple[str, str], list[str]] = {}
    for ft in failed_tests:
        key = (ft.category, ft.name)
        if key in seen:
            extra_shards.setdefault(key, []).append(ft.shard_context)
            continue
        seen[key] = ft
    deduped: list[FailedTest] = []
    for key, ft in seen.items():
        extras = extra_shards.get(key) or []
        if extras:
            note = (
                "\n\n[releasy] also failed in shards: "
                + ", ".join(extras[:5])
                + ("…" if len(extras) > 5 else "")
            )
            ft.info_excerpt = (ft.info_excerpt + note).strip()
        deduped.append(ft)

    return (
        PRFailures(
            pr_url=pr_url,
            head_sha=head_sha,
            head_ref=head_ref,
            base_ref=base_ref,
            statuses=statuses,
            failed_tests=deduped,
            skipped_status_warnings=warnings,
        ),
        None,
    )
