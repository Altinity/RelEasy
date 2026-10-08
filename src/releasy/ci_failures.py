"""Discover failed CI checks on a PR and parse their praktika / TestFlows reports.

Read-only: GitHub statuses API and the S3 artefact bucket; no git, no Claude, no state."""

from __future__ import annotations

import json
import re
import textwrap
import urllib.parse
from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Iterable

import requests

from releasy.config import Config, get_github_token
from releasy.github_ops import parse_pr_url


# Mirrors the praktika viewer's JS so we hit the same S3 keys.
def _normalize_task_name(name: str) -> str:
    s = name.lower()
    s = re.sub(r"[^a-z0-9]", "_", s)
    s = re.sub(r"_+", "_", s)
    return s.rstrip("_")


@dataclass
class ArtifactLocator:
    """Coordinates of a praktika ``result_*.json`` artefact (keyed by ``pr`` or ``ref``)."""
    base_url: str
    pr: str | None
    sha: str
    name_0: str
    name_1: str | None  # the leaf task name, e.g. "Stateless tests (...)"
    ref: str | None = None

    def result_json_url(self) -> str:
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
    """Parse a praktika ``json.html?...`` target URL; ``None`` if it isn't one."""
    try:
        parts = urllib.parse.urlsplit(url)
    except ValueError:
        return None
    if not parts.scheme or not parts.netloc:
        return None
    if not parts.path.endswith("json.html"):
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
        # The viewer falls back to the page's origin + dirname: the bucket origin.
        base_url = f"{parts.scheme}://{parts.netloc}"
    return ArtifactLocator(
        base_url=base_url, pr=pr, sha=sha, name_0=name_0, name_1=name_1,
        ref=ref,
    )


@dataclass
class TestFlowsLocator:
    """Directory of a TestFlows ``report.html`` (the ``Regression …`` checks)."""
    report_dir: str  # absolute URL, no trailing slash

    def fails_log_url(self) -> str:
        return f"{self.report_dir}/fails.log.txt"

    def report_url(self) -> str:
        return f"{self.report_dir}/report.html"


def _testflows_locator_from_target_url(url: str) -> TestFlowsLocator | None:
    """Parse a TestFlows ``…/report.html`` target URL into its directory."""
    try:
        parts = urllib.parse.urlsplit(url)
    except ValueError:
        return None
    if not parts.scheme or not parts.netloc:
        return None
    if not parts.path.endswith("/report.html"):
        return None
    directory = parts.path[: -len("/report.html")]
    return TestFlowsLocator(
        report_dir=f"{parts.scheme}://{parts.netloc}{directory}",
    )


def locator_from_target_url(
    url: str,
) -> ArtifactLocator | TestFlowsLocator | None:
    """Locator of the report a status ``target_url`` points at; ``None`` if unreadable."""
    return (
        _artifact_locator_from_target_url(url)
        or _testflows_locator_from_target_url(url)
    )


TestCategory = str

CATEGORY_OTHER: TestCategory = "other"


_NAME_CATEGORY_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("fasttest", re.compile(r"^Fast\s*test\b", re.IGNORECASE)),
    ("stateless", re.compile(r"^Stateless\s*tests?\b", re.IGNORECASE)),
    ("integration", re.compile(r"^Integration\s*tests?\b", re.IGNORECASE)),
    ("regression", re.compile(r"^Regression\b", re.IGNORECASE)),
    (
        "quick_functional",
        re.compile(r"^Quick\s*functional\s*tests?\b", re.IGNORECASE),
    ),
)


# Cheap-and-broad first, regression (slowest to reproduce) last.
CATEGORY_ORDER: dict[str, int] = {
    "fasttest": 0,
    "quick_functional": 1,
    "stateless": 2,
    "integration": 3,
    "regression": 4,
    CATEGORY_OTHER: 5,
}


def category_from_name(name: str) -> TestCategory:
    """Test category of a status context name; :data:`CATEGORY_OTHER` if unrecognised."""
    for cat, pat in _NAME_CATEGORY_PATTERNS:
        if pat.search(name):
            return cat
    return CATEGORY_OTHER


@dataclass
class FailedStatus:
    """One CI status; ``locator`` is ``None`` when its report is unreadable."""
    context: str
    state: str  # "failure" | "error"
    target_url: str
    description: str
    category: TestCategory
    locator: ArtifactLocator | TestFlowsLocator | None
    updated_at: str | None = None

    @property
    def is_aggregate(self) -> bool:
        """True for the workflow-level rolled-up report, which duplicates per-job ones."""
        return (
            isinstance(self.locator, ArtifactLocator)
            and not self.locator.name_1
        )


def _gh_headers(token: str) -> dict[str, str]:
    return {
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {token}",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def _fetch_combined_statuses(
    owner: str, repo: str, sha: str, token: str,
) -> list[dict[str, Any]]:
    """All raw entries of the commit-statuses endpoint, newest first."""
    out: list[dict[str, Any]] = []
    url = (
        f"https://api.github.com/repos/{owner}/{repo}/commits/{sha}/statuses"
        f"?per_page=100"
    )
    headers = _gh_headers(token)
    seen_pages = 0
    while url and seen_pages < 50:  # 5000 statuses ought to be enough
        resp = requests.get(url, headers=headers, timeout=30)
        if resp.status_code != 200:
            raise RuntimeError(
                f"GitHub statuses API returned HTTP {resp.status_code}: "
                f"{resp.text[:300]}"
            )
        out.extend(resp.json() or [])
        url = _next_link(resp.headers.get("Link", ""))
        seen_pages += 1
    return out


_NEXT_LINK_RE = re.compile(r'<([^>]+)>;\s*rel="next"')


def _next_link(link_header: str) -> str | None:
    if not link_header:
        return None
    m = _NEXT_LINK_RE.search(link_header)
    return m.group(1) if m else None


def fetch_statuses(
    owner: str, repo: str, sha: str, *, failed_only: bool = True,
) -> tuple[list[FailedStatus], str | None]:
    """CI statuses on ``sha``, latest entry per context. Returns ``(statuses, error)``."""
    token = get_github_token()
    if not token:
        return [], (
            "RELEASY_GITHUB_TOKEN not set — cannot fetch CI statuses"
        )
    try:
        raw = _fetch_combined_statuses(owner, repo, sha, token)
    except Exception as exc:
        return [], f"GitHub statuses lookup failed: {exc}"

    # Newest first, so the first entry per context is authoritative.
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
        if failed_only and state not in ("failure", "error"):
            continue
        target_url = entry.get("target_url") or ""
        locator = locator_from_target_url(target_url)
        out.append(FailedStatus(
            context=ctx,
            state=state,
            target_url=target_url,
            description=(entry.get("description") or "").strip(),
            category=category_from_name(ctx),
            locator=locator,
            updated_at=entry.get("updated_at"),
        ))
    out.sort(key=lambda s: (
        CATEGORY_ORDER.get(s.category, 99),
        s.context,
    ))
    return out, None


def fetch_failed_statuses(
    owner: str, repo: str, sha: str,
) -> tuple[list[FailedStatus], str | None]:
    """Every failed/errored CI status on ``sha`` (latest per context)."""
    return fetch_statuses(owner, repo, sha, failed_only=True)


def fetch_report_json(
    locator: ArtifactLocator, *, timeout: int = 60,
) -> tuple[dict[str, Any] | None, str | None]:
    """Fetch the praktika ``result_*.json``. Returns ``(json, error)``."""
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


# Per praktika's ``Result.is_failure()`` / ``is_error()``; ``BROKEN`` and
# ``XFAIL`` are muted results (``is_ok()``). ``TIMEOUT`` is upper-cased
# ``Timeout``. ``FAILURE`` is what older praktika writes on step nodes.
_FAILED_LEAF_STATUSES = frozenset({
    "FAIL",
    "FAILURE",
    "ERROR",
    "XPASS",
    "TIMEOUT",
})


# Runner-level pseudo-leaves mirroring the umbrella failure; passing one to
# the runner would re-run the entire suite.
_META_LEAF_NAMES = frozenset({"clickhouse-test"})


@dataclass
class FailedTest:
    """One failed test (or, with ``job_level``, one failed check)."""
    name: str
    status: str
    category: TestCategory
    shard_context: str
    target_url: str
    info_excerpt: str = ""
    files: list[str] = field(default_factory=list)
    links: list[str] = field(default_factory=list)
    job_level: bool = False  # ``name`` is a job name: never pass it to a test runner


def _iter_failed_leaves(
    node: dict[str, Any], *, depth: int = 0,
) -> Iterable[tuple[dict[str, Any], int]]:
    """Yield ``(leaf, depth)`` for every failing childless node, skipping meta-leaves."""
    children = node.get("results") or []
    status = (node.get("status") or "").upper()
    name = (node.get("name") or "").strip()
    if status in _FAILED_LEAF_STATUSES and not children:
        if name in _META_LEAF_NAMES:
            return
        yield node, depth
        return
    for child in children:
        if not isinstance(child, dict):
            continue
        yield from _iter_failed_leaves(child, depth=depth + 1)


_INFO_EXCERPT_MAX = 4000

# 0 is the job, 1 its steps; tests hang off a grouping node at 2+.
_FIRST_TEST_DEPTH = 2


def extract_failed_tests(
    report: dict[str, Any],
    *,
    category: TestCategory,
    shard_context: str,
    target_url: str,
) -> list[FailedTest]:
    """Walk the praktika tree and collect failed leaves as ``FailedTest``."""
    out: list[FailedTest] = []
    for leaf, depth in _iter_failed_leaves(report):
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
            job_level=depth < _FIRST_TEST_DEPTH,
        ))
    return out


def fetch_testflows_fails_log(
    locator: TestFlowsLocator, *, timeout: int = 60,
) -> tuple[str | None, str | None]:
    """Fetch the TestFlows ``fails.log.txt``. Returns ``(text, error)``."""
    url = locator.fails_log_url()
    try:
        resp = requests.get(url, timeout=timeout)
    except Exception as exc:
        return None, f"GET {url} failed: {exc}"
    if resp.status_code in (403, 404):
        return None, (
            f"No fails.log.txt at {url}. The suite likely died before it "
            "could write a report (infra / build failure, or the job was "
            "still running), or the artefact has been pruned."
        )
    if resp.status_code != 200:
        return None, (
            f"GET {url} → HTTP {resp.status_code}; first 200 chars: "
            f"{resp.text[:200]!r}"
        )
    return resp.text, None


# ``X``-prefixed results are expected (known-broken) failures and stay muted.
_FAILED_TESTFLOWS_STATUSES = frozenset({"FAIL", "ERROR", "NULL"})


# ``fails.log.txt`` lists the same tests twice, in two shapes.
#
# Detail section — duration *before* the status bracket, bare path, then
# an indented assertion / traceback block:
#     ``✘ 1m 53s    [  Fail  ] /swarms/feature/node failure``
_TF_DETAIL_RE = re.compile(
    r"^✘\s+(?P<duration>\S.*?)\s+\[\s*(?P<status>\w+)\s*\]\s+(?P<path>/.*)$",
)
# Summary sections (``Known`` / ``Failing``) — status first, path quoted:
#     ``✘ [ Fail ] '/swarms/feature/node failure' (11m 34s)``
_TF_SUMMARY_RE = re.compile(
    r"^✘\s+\[\s*(?P<status>\w+)\s*\]\s+'(?P<path>.+)'"
    r"\s+\((?P<duration>[^)]*)\)\s*$",
)


@dataclass
class TestFlowsEntry:
    """One node of a TestFlows run as reported by ``fails.log.txt``."""
    path: str
    status: str
    detail: str = ""


def parse_testflows_fails_log(text: str) -> dict[str, TestFlowsEntry]:
    """Parse a TestFlows ``fails.log.txt`` into ``{test path: entry}``."""
    entries: dict[str, TestFlowsEntry] = {}
    lines = text.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]
        i += 1
        # Summary first: the detail pattern would misparse a path containing ``[``.
        summary = _TF_SUMMARY_RE.match(line)
        if summary is not None:
            path = summary.group("path")
            entries.setdefault(path, TestFlowsEntry(
                path=path, status=summary.group("status").upper(),
            ))
            continue
        detail_match = _TF_DETAIL_RE.match(line)
        if detail_match is None:
            continue
        path = detail_match.group("path").rstrip()
        status = detail_match.group("status").upper()
        # The indented (or blank) lines below belong to this entry.
        block: list[str] = []
        while i < len(lines):
            nxt = lines[i]
            if nxt.strip() and not nxt[:1].isspace():
                break
            block.append(nxt)
            i += 1
        detail = textwrap.dedent("\n".join(block)).strip()
        entry = entries.get(path)
        if entry is None:
            entries[path] = TestFlowsEntry(
                path=path, status=status, detail=detail,
            )
        elif detail and not entry.detail:
            entry.status = status
            entry.detail = detail
    return entries


# Shorter leaf details trigger the ancestor-traceback lookup.
_TF_THIN_DETAIL = 200


def _nearest_detailed_ancestor(
    path: str, entries: dict[str, TestFlowsEntry],
) -> TestFlowsEntry | None:
    """Deepest ancestor of ``path`` carrying a substantial detail block."""
    parts = path.split("/")
    for cut in range(len(parts) - 1, 0, -1):
        candidate = entries.get("/".join(parts[:cut]))
        if candidate is not None and len(candidate.detail) >= _TF_THIN_DETAIL:
            return candidate
    return None


def extract_regression_failures(
    fails_log: str,
    *,
    category: TestCategory,
    shard_context: str,
    target_url: str,
) -> list[FailedTest]:
    """Deepest failing nodes from a TestFlows ``fails.log.txt``.

    A thin leaf borrows its nearest detailed ancestor's traceback; only the
    first leaf under that ancestor gets it, the rest get a pointer.
    """
    entries = parse_testflows_fails_log(fails_log)
    failing = {
        path: entry for path, entry in entries.items()
        if entry.status in _FAILED_TESTFLOWS_STATUSES
    }
    leaves = [
        entry for path, entry in failing.items()
        if not any(other.startswith(path + "/") for other in failing)
    ]

    out: list[FailedTest] = []
    borrowed: set[str] = set()
    for entry in sorted(leaves, key=lambda e: e.path):
        info = entry.detail
        if len(info) < _TF_THIN_DETAIL:
            ancestor = _nearest_detailed_ancestor(entry.path, entries)
            if ancestor is None:
                pass
            elif ancestor.path in borrowed:
                info = (
                    f"{info}\n\n[releasy] no per-test detail; see the "
                    f"enclosing node {ancestor.path!r} shown with the "
                    "first test under it."
                ).strip()
            else:
                borrowed.add(ancestor.path)
                info = (
                    f"{info}\n\n[releasy] detail reported on the enclosing "
                    f"node {ancestor.path!r}. TestFlows prints one "
                    "representative traceback there rather than one per "
                    "leaf, so this may be a sibling scenario's failure:"
                    f"\n{ancestor.detail}"
                ).strip()
        if len(info) > _INFO_EXCERPT_MAX:
            info = info[:_INFO_EXCERPT_MAX] + "\n…(truncated)"
        out.append(FailedTest(
            name=entry.path,
            status=entry.status,
            category=category,
            shard_context=shard_context,
            target_url=target_url,
            info_excerpt=info,
        ))
    return out


def job_level_failure(status: FailedStatus, reason: str) -> FailedTest:
    """Stand-in record for a failed check that reported no failing tests."""
    parts = [reason.strip()]
    if status.description:
        parts.append(f"CI status description: {status.description}")
    if status.target_url:
        parts.append(f"Report / log: {status.target_url}")
    return FailedTest(
        name=status.context,
        status=status.state.upper(),
        category=status.category,
        shard_context=status.context,
        target_url=status.target_url,
        info_excerpt="\n\n".join(p for p in parts if p),
        job_level=True,
    )


def decompose_statuses(
    statuses: list[FailedStatus],
    *,
    categories: tuple[TestCategory, ...] | None = None,
    job_level: bool = True,
    pr_number: int | None = None,
) -> tuple[list[FailedTest], list[str]]:
    """Turn failed statuses into per-test records (no dedupe). Returns ``(tests, warnings)``."""
    cat_set = set(categories) if categories else None
    failed_tests: list[FailedTest] = []
    warnings: list[str] = []
    aggregates: list[str] = []
    non_aggregate = 0

    def _undecomposable(st: FailedStatus, reason: str) -> None:
        if job_level:
            failed_tests.append(job_level_failure(st, reason))
        else:
            warnings.append(f"{st.context}: {reason}")

    for st in statuses:
        if cat_set is not None and st.category not in cat_set:
            continue
        if st.is_aggregate:
            aggregates.append(st.context)
            continue
        non_aggregate += 1
        if st.locator is None:
            _undecomposable(st, (
                "this check published no machine-readable report — its "
                f"target_url ({st.target_url or 'none'}) is neither a "
                "praktika result JSON nor a TestFlows report, so RelEasy "
                "could not decompose it into individual tests."
            ))
            continue
        if isinstance(st.locator, TestFlowsLocator):
            fails_log, ferr = fetch_testflows_fails_log(st.locator)
            if ferr or fails_log is None:
                _undecomposable(st, ferr or "empty fails.log.txt")
                continue
            leaves = extract_regression_failures(
                fails_log,
                category=st.category,
                shard_context=st.context,
                target_url=st.target_url,
            )
        else:
            if st.locator.pr is None and pr_number is not None:
                st.locator.pr = str(pr_number)
            report, ferr = fetch_report_json(st.locator)
            if ferr or report is None:
                _undecomposable(st, ferr or "empty report")
                continue
            leaves = extract_failed_tests(
                report,
                category=st.category,
                shard_context=st.context,
                target_url=st.target_url,
            )
        if not leaves:
            _undecomposable(st, (
                f"the check reported {st.state} but its report holds no "
                "failing test leaf — an infrastructure, build, packaging "
                "or image failure rather than a per-test one."
            ))
            continue
        failed_tests.extend(leaves)

    for ctx in aggregates:
        warnings.append(
            f"{ctx}: workflow-level rolled-up report — the per-job "
            "statuses cover the same failures; skipping"
            if non_aggregate else
            f"{ctx}: workflow-level rolled-up report, and the only "
            "failed status on this commit — no per-job check went red, "
            "so there is nothing to decompose. Jobs dropped or "
            "cancelled before they ran surface only here."
        )
    return failed_tests, warnings


@dataclass
class BaselineRun:
    """One CI run on the target branch, taken before the PR's diff."""
    sha: str
    committed_at: str
    checks_total: int
    checks_failed: int
    failing: dict[tuple[str, str], str]  # (category, test) → shard that reported it
    categories_run: set[str]
    warnings: list[str] = field(default_factory=list)
    skipped_newer: int = 0  # newer runs passed over for lacking the needed checks

    def verdict_for(self, category: str, name: str) -> str:
        """``"failed"`` / ``"passed"`` / ``"not covered"`` for one test."""
        if (category, name) in self.failing:
            return "failed"
        return "passed" if category in self.categories_run else "not covered"


def merge_base_sha(
    owner: str, repo: str, base_ref: str, head_sha: str,
) -> tuple[str | None, str | None]:
    """SHA where ``head_sha`` diverged from ``base_ref``."""
    token = get_github_token()
    if not token:
        return None, "RELEASY_GITHUB_TOKEN not set — cannot compare refs"
    url = (
        f"https://api.github.com/repos/{owner}/{repo}/compare/"
        f"{urllib.parse.quote(base_ref, safe='')}...{head_sha}"
    )
    headers = _gh_headers(token)
    try:
        resp = requests.get(url, headers=headers, timeout=30)
    except Exception as exc:
        return None, f"compare {base_ref}...{head_sha[:10]} failed: {exc}"
    if resp.status_code != 200:
        return None, (
            f"compare {base_ref}...{head_sha[:10]} → HTTP "
            f"{resp.status_code}"
        )
    sha = ((resp.json() or {}).get("merge_base_commit") or {}).get("sha")
    if not sha:
        return None, "compare response carried no merge_base_commit"
    return sha, None


def _list_commits(
    owner: str, repo: str, sha: str, limit: int,
) -> tuple[list[tuple[str, str]], str | None]:
    """``[(sha, committed_at)]`` for ``sha`` and its ancestors, newest first."""
    token = get_github_token()
    if not token:
        return [], "RELEASY_GITHUB_TOKEN not set — cannot list commits"
    url = (
        f"https://api.github.com/repos/{owner}/{repo}/commits"
        f"?sha={urllib.parse.quote(sha, safe='')}"
        f"&per_page={max(1, min(limit, 100))}"
    )
    headers = _gh_headers(token)
    try:
        resp = requests.get(url, headers=headers, timeout=30)
    except Exception as exc:
        return [], f"commit listing failed: {exc}"
    if resp.status_code != 200:
        return [], f"commit listing → HTTP {resp.status_code}"
    out: list[tuple[str, str]] = []
    for entry in resp.json() or []:
        csha = entry.get("sha")
        when = (
            ((entry.get("commit") or {}).get("committer") or {})
            .get("date") or ""
        )
        if csha:
            out.append((csha, when))
    return out[:limit], None


def baseline_run_before(
    owner: str,
    repo: str,
    from_sha: str,
    *,
    max_commits: int = 25,
    categories: tuple[TestCategory, ...] | None = None,
    job_level: bool = True,
    exclude_sha: str | None = None,
    require_categories: frozenset[str] | None = None,
) -> tuple[BaselineRun | None, str | None]:
    """Decompose the newest CI run at or before ``from_sha``.

    Prefers runs covering ``require_categories``, falling back to the newest
    run found. Returns ``(run, None)`` or ``(None, reason)``.
    """
    commits, err = _list_commits(owner, repo, from_sha, max_commits)
    if err:
        return None, err

    def _build(
        csha: str, when: str, statuses: list[FailedStatus], skipped: int,
    ) -> BaselineRun:
        failed = [s for s in statuses if s.state in ("failure", "error")]
        tests, warnings = decompose_statuses(
            failed, categories=categories, job_level=job_level,
        )
        return BaselineRun(
            sha=csha,
            committed_at=when,
            checks_total=len(statuses),
            checks_failed=len(failed),
            failing={
                (t.category, t.name): t.shard_context for t in tests
            },
            categories_run={s.category for s in statuses},
            warnings=warnings,
            skipped_newer=skipped,
        )

    fallback: tuple[str, str, list[FailedStatus]] | None = None
    skipped = 0
    for csha, when in commits:
        if exclude_sha and csha == exclude_sha:
            continue
        statuses, serr = fetch_statuses(owner, repo, csha, failed_only=False)
        if serr or not statuses:
            continue
        if require_categories and not require_categories <= {
            s.category for s in statuses
        }:
            if fallback is None:
                fallback = (csha, when, statuses)
            skipped += 1
            continue
        return _build(csha, when, statuses, skipped), None

    if fallback is not None:
        return _build(*fallback, 0), None
    return None, (
        f"no CI run found within {len(commits)} commit(s) at or before "
        f"{from_sha[:10]}"
    )


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
    # Checks whose failures were all deduped into another check's shard.
    covered_elsewhere: list[str] = field(default_factory=list)


def discover_pr_failures(
    config: Config,
    pr_url: str,
    *,
    head_sha: str | None = None,
    head_ref: str | None = None,
    base_ref: str | None = None,
    categories: tuple[TestCategory, ...] | None = None,
    job_level: bool = True,
) -> tuple[PRFailures | None, str | None]:
    """Resolve a PR's head, then decompose and dedupe its failed CI statuses.

    ``categories=None`` processes every failed check. With ``job_level=False``
    checks without failing tests go to ``skipped_status_warnings``.
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
            from github import Github

            gh = Github(token)
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

    failed_tests, warnings = decompose_statuses(
        statuses, categories=categories, job_level=job_level,
        pr_number=number,
    )

    # One record per (category, name); other shards are noted in info_excerpt.
    seen: dict[tuple[str, str], FailedTest] = {}
    extra_shards: dict[tuple[str, str], list[str]] = {}
    contributed: Counter[str] = Counter()
    absorbed: Counter[str] = Counter()
    absorbed_into: dict[str, list[str]] = {}
    for ft in failed_tests:
        contributed[ft.shard_context] += 1
        key = (ft.category, ft.name)
        if key in seen:
            extra_shards.setdefault(key, []).append(ft.shard_context)
            absorbed[ft.shard_context] += 1
            into = absorbed_into.setdefault(ft.shard_context, [])
            if seen[key].shard_context not in into:
                into.append(seen[key].shard_context)
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

    covered: list[str] = []
    for ctx, count in absorbed.items():
        into = absorbed_into.get(ctx) or []
        into_str = ", ".join(into[:3]) + ("…" if len(into) > 3 else "")
        if count == contributed[ctx]:
            covered.append(
                f"{ctx}: all {count} failure(s) are the same test(s) as "
                f"{into_str} — investigated there, no shard of its own."
            )
        else:
            covered.append(
                f"{ctx}: {count} of {contributed[ctx]} failure(s) "
                f"duplicate {into_str}; the remaining "
                f"{contributed[ctx] - count} form this shard."
            )

    return (
        PRFailures(
            pr_url=pr_url,
            head_sha=head_sha,
            head_ref=head_ref,
            base_ref=base_ref,
            statuses=statuses,
            failed_tests=deduped,
            skipped_status_warnings=warnings,
            covered_elsewhere=covered,
        ),
        None,
    )
