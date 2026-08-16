"""Discover failed CI checks on a PR and parse the human-readable reports.

Altinity ClickHouse CI surfaces test results in three ways:

1. **GitHub Actions check-runs** — opaque job logs (e.g. ``PR / Fast test
   (pull_request)``). These are the raw workflow output; we deliberately
   ignore them.
2. **GitHub commit statuses** whose ``target_url`` points at a hosted
   ``json.html`` viewer (the ``praktika`` report). The viewer fetches a
   sibling ``result_<task>.json`` from the same S3 bucket and renders
   it; that JSON is the structured, machine-readable form of "which
   tests passed / failed".
3. **GitHub commit statuses** whose ``target_url`` is a TestFlows
   ``report.html`` — the ``Regression <arch> <suite>`` checks. Those
   suites live in the separate ``Altinity/clickhouse-regression`` repo
   and publish no praktika JSON; their machine-readable failure list is
   the sibling ``fails.log.txt``.

This module is the bridge between (2)/(3) and a list of failed-test
records ``analyze-fails`` can hand to Claude. Neither report shape is
formally documented anywhere — what's encoded here is the result of
reverse-engineering the live viewer code and the published artefacts.

Pure functions only. No git, no Claude, no state. Network access is
limited to the GitHub statuses API and an S3 bucket holding the
artefacts; both are read-only.
"""

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


@dataclass
class TestFlowsLocator:
    """Coordinates of a TestFlows regression report directory in S3.

    The ``Regression <arch> <suite>`` checks publish a rendered
    ``report.html`` plus sibling artefacts, keyed by
    ``REFs/<pr>/merge/<sha>/regression/<arch>/…/<suite>/``. Unlike
    praktika there is no key to compose — the status ``target_url`` is
    the report itself, so we just remember its directory and read
    ``fails.log.txt`` next to it.
    """
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
    """Classify a status ``target_url`` into whichever report it points at.

    ``None`` means we can't read this status's results at all (a raw
    GitHub-Actions job log, an empty ``target_url``, …).
    """
    return (
        _artifact_locator_from_target_url(url)
        or _testflows_locator_from_target_url(url)
    )


# Every failed check gets processed. The category decides which
# reproduction recipe and triage prior :mod:`releasy.analyze_fails`
# hands Claude; checks we have no recipe for land in ``CATEGORY_OTHER``
# and are handed over with instructions to find the runner first.
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


# Processing / display order: cheap-and-broad first (one Fast test fix
# routinely flips the rest green), regression last — it needs an
# external repo and hours of docker to reproduce.
CATEGORY_ORDER: dict[str, int] = {
    "fasttest": 0,
    "quick_functional": 1,
    "stateless": 2,
    "integration": 3,
    "regression": 4,
    CATEGORY_OTHER: 5,
}


def category_from_name(name: str) -> TestCategory:
    """Classify a status context name into a test category.

    Falls back to :data:`CATEGORY_OTHER` rather than ``None``: an
    unrecognised check is still a failed check worth investigating.
    """
    for cat, pat in _NAME_CATEGORY_PATTERNS:
        if pat.search(name):
            return cat
    return CATEGORY_OTHER


# ---------------------------------------------------------------------------
# Failed-status discovery via GitHub commit statuses
# ---------------------------------------------------------------------------


@dataclass
class FailedStatus:
    """One failed CI status with enough context to fetch its report.

    ``locator`` is ``None`` when the status's ``target_url`` points at
    neither a praktika nor a TestFlows report (e.g. a raw GitHub Actions
    job log) — those are surfaced for the operator to see but don't
    drive per-test analysis.
    """
    context: str
    state: str  # "failure" | "error"
    target_url: str
    description: str
    category: TestCategory
    locator: ArtifactLocator | TestFlowsLocator | None
    updated_at: str | None = None

    @property
    def is_aggregate(self) -> bool:
        """True for the workflow-level rolled-up report (the ``PR`` status).

        Praktika publishes one report per job *plus* one for the whole
        workflow; the latter carries no ``name_1`` and its tree contains
        every job's failures. Walking it would duplicate every per-job
        failure we already collect from the individual statuses.
        """
        return (
            isinstance(self.locator, ArtifactLocator)
            and not self.locator.name_1
        )


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


def fetch_statuses(
    owner: str, repo: str, sha: str, *, failed_only: bool = True,
) -> tuple[list[FailedStatus], str | None]:
    """Return the CI statuses on ``sha`` (latest entry per context).

    ``failed_only`` (the default) keeps only ``failure``/``error``.
    Pass ``False`` to get every state — a baseline comparison needs to
    know which checks *ran*, not just which failed, since "absent from
    the failure list" only means "passed" for a check that ran at all.

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
#
# ``FAILURE`` is the GitHub-status vocabulary (``Result.GHStatus``):
# reports produced by older praktika revisions — still what an older
# release branch's CI publishes — write ``failure``/``success`` on the
# job's *step* nodes ("Start ClickHouse Server", "Install ClickHouse").
# Those steps are where a job that died before its test phase records
# what happened, so treat ``FAILURE`` as a failing leaf too.
_FAILED_LEAF_STATUSES = frozenset({
    "FAIL",
    "FAILURE",
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
    # True for the stand-in record of a check that failed as a whole
    # without per-test results (see :func:`job_level_failure`). Callers
    # must not feed its ``name`` to a test runner — it's a job name.
    job_level: bool = False


def _iter_failed_leaves(
    node: dict[str, Any], *, depth: int = 0,
) -> Iterable[tuple[dict[str, Any], int]]:
    """Yield ``(leaf, depth)`` for every failing leaf of the report tree.

    The praktika report is recursive: ``results`` may hold further
    ``results`` nodes. We treat a node as a "leaf failure" when its
    status is in ``_FAILED_LEAF_STATUSES`` AND it has no ``results`` of
    its own — that filters out aggregate "Tests" failure rows that just
    summarise per-test failures we'd otherwise count twice.

    ``depth`` lets the caller tell a per-test leaf from a job-level one.
    The top two levels are the job and its steps ("Install ClickHouse",
    "Build ClickHouse", "Start ClickHouse Server"); real tests hang off
    a grouping node below them. A failing step is what a job that died
    before its test phase leaves behind — worth investigating, but its
    name must never reach a test runner.

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
        yield node, depth
        return
    for child in children:
        if not isinstance(child, dict):
            continue
        yield from _iter_failed_leaves(child, depth=depth + 1)


_INFO_EXCERPT_MAX = 4000

# Report depth at which per-test leaves start. 0 is the job itself, 1 is
# its steps; tests hang off a grouping node at 2 or deeper.
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
            # The job and its steps are not tests — see
            # _iter_failed_leaves.
            job_level=depth < _FIRST_TEST_DEPTH,
        ))
    return out


# ---------------------------------------------------------------------------
# TestFlows (regression suite) report fetching + parsing
# ---------------------------------------------------------------------------


def fetch_testflows_fails_log(
    locator: TestFlowsLocator, *, timeout: int = 60,
) -> tuple[str | None, str | None]:
    """Fetch the TestFlows ``fails.log.txt`` sitting next to a report.

    Returns ``(text, None)`` on success, ``(None, message)`` on failure.
    """
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


# TestFlows result names that mean "this test really failed".
#
# The ``X`` flavours (``XFail`` / ``XError`` / ``XNull``) are *expected*
# failures: the suite annotated them as known-broken, usually with an
# upstream issue link, and the report lists them under its ``Known``
# section rather than ``Failing``. They are the TestFlows counterpart of
# praktika's ``XFAIL``/``BROKEN`` and are muted here for the same
# reason. ``Skip`` never ran at all.
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
    """Parse a TestFlows ``fails.log.txt`` into ``{test path: entry}``.

    Folds both shapes of the file into one entry per path, keeping the
    detail block when the detail section carried one. Test names are
    unambiguous keys: TestFlows escapes ``/`` and quotes inside a test
    name with lookalike codepoints, so a raw ``/`` is always a path
    separator.
    """
    entries: dict[str, TestFlowsEntry] = {}
    lines = text.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]
        i += 1
        # Summary first: its status bracket would otherwise let the
        # detail pattern misparse a path containing a ``[``.
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
        # Everything indented (or blank) underneath belongs to this
        # entry; the next entry, or a section heading, is flush-left.
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


# A leaf whose own detail block is shorter than this is treated as
# uninformative (``AssertionError`` and nothing else), which triggers
# the ancestor-traceback lookup below.
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
    """Collect the leaf failures from a TestFlows ``fails.log.txt``.

    TestFlows reports every node on the path to a failure, so one broken
    scenario surfaces as itself *plus* every enclosing feature and the
    module. We keep only the deepest nodes — a failing path that no
    other failing path extends — because the ancestors carry no
    independent diagnostic value and would multiply the work.

    The ancestors do often carry the *traceback*, though: TestFlows
    prints the full assertion detail on the enclosing node and a bare
    ``AssertionError`` on the leaf. When a leaf's own block is too thin
    to act on, the nearest substantial ancestor block is attached — but
    only to the first leaf under that ancestor, and labelled as
    possibly belonging to a sibling. One enclosing node routinely spans
    hundreds of leaves, so its single traceback is a representative
    sample, not per-test truth, and repeating it verbatim on every leaf
    would both mislead and swamp the prompt.
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


# ---------------------------------------------------------------------------
# Job-level failures (checks with no per-test results)
# ---------------------------------------------------------------------------


def job_level_failure(status: FailedStatus, reason: str) -> FailedTest:
    """Stand-in record for a failed check that reported no failing tests.

    A build, packaging, image or scan check has nothing per-test to
    report; a job killed before its test phase publishes a report with
    no failing leaf; a check whose ``target_url`` is a plain job log
    publishes nothing we can read at all. All three are still red CI on
    the PR, so they become one ``job_level`` record — the shard Claude
    is handed carries the reason, the status description and the report
    URL instead of a test list.
    """
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
    """Turn failed statuses into per-test records. No dedupe.

    Shared by the PR path and the baseline-commit path, so a run on the
    target branch is decomposed exactly like the PR's own — otherwise
    the two failure sets wouldn't be comparable. Returns
    ``(failed_tests, warnings)``.
    """
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
                # Replace any missing PR coordinate with the one we know.
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

    # The rolled-up report duplicates the per-job statuses — unless none
    # of them is red, in which case it's the only evidence there is and
    # saying "covered elsewhere" would be a lie.
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


# ---------------------------------------------------------------------------
# Baseline: the last CI run that predates the change under investigation
# ---------------------------------------------------------------------------


@dataclass
class BaselineRun:
    """One CI run on the target branch, taken before the PR's diff.

    ``failing`` keys are ``(category, test name)`` — the same key the
    PR-side records use, so membership answers "was this already red
    without this PR?". ``categories_run`` records which check families
    that run actually exercised: a test missing from ``failing`` only
    means "did not fail" if its category ran at all.
    """
    sha: str
    committed_at: str
    checks_total: int
    checks_failed: int
    failing: dict[tuple[str, str], str]  # → the shard that reported it
    categories_run: set[str]
    warnings: list[str] = field(default_factory=list)
    # Newer runs that were passed over because they never exercised the
    # checks under investigation. Non-empty means this baseline is older
    # than it had to be, which the prompt says out loud.
    skipped_newer: int = 0

    def verdict_for(self, category: str, name: str) -> str:
        """``"failed"`` / ``"passed"`` / ``"not covered"`` for one test."""
        if (category, name) in self.failing:
            return "failed"
        return "passed" if category in self.categories_run else "not covered"


def merge_base_sha(
    owner: str, repo: str, base_ref: str, head_sha: str,
) -> tuple[str | None, str | None]:
    """SHA where ``head_sha`` diverged from ``base_ref``.

    That commit is the newest state of the branch that does *not*
    contain the PR's diff — the anchor for "before the change".
    """
    token = get_github_token()
    if not token:
        return None, "RELEASY_GITHUB_TOKEN not set — cannot compare refs"
    url = (
        f"https://api.github.com/repos/{owner}/{repo}/compare/"
        f"{urllib.parse.quote(base_ref, safe='')}...{head_sha}"
    )
    headers = {
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {token}",
        "X-GitHub-Api-Version": "2022-11-28",
    }
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
    headers = {
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {token}",
        "X-GitHub-Api-Version": "2022-11-28",
    }
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
    """Find and decompose the newest usable CI run at or before ``from_sha``.

    Walks back until a commit with CI statuses turns up: most commits
    on a release branch are GitHub merge commits that no workflow ever
    ran on, while the merged PR's own head commits carry a full run.

    ``require_categories`` (the check families the comparison needs to
    say anything about) makes the walk skip runs that never exercised
    them — a run with no ``fasttest`` check answers no question about a
    Fast test failure. If nothing within range covers them, the newest
    run found is used anyway and its gaps surface as "not covered"
    verdicts. Only the chosen run's reports are fetched; the others cost
    one status call each.

    Returns ``(run, None)``, or ``(None, reason)`` when no run is
    reachable within ``max_commits`` — a missing baseline is normal
    (fresh branch, pruned artefacts), never fatal.
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
            # Degenerate compare (PR already merged): its own run is not
            # a baseline for itself.
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


def discover_baseline_failures(
    owner: str,
    repo: str,
    base_ref: str,
    head_sha: str,
    *,
    max_commits: int = 25,
    categories: tuple[TestCategory, ...] | None = None,
    job_level: bool = True,
    require_categories: frozenset[str] | None = None,
) -> tuple[BaselineRun | None, str | None]:
    """The last CI run on ``base_ref`` that predates ``head_sha``'s diff.

    Anchors on the merge base — the newest state of the branch without
    the change under investigation — then takes the newest usable run
    at or before it.
    """
    mb, err = merge_base_sha(owner, repo, base_ref, head_sha)
    if err or not mb:
        return None, err or "no merge base"
    return baseline_run_before(
        owner, repo, mb, max_commits=max_commits, categories=categories,
        job_level=job_level, exclude_sha=head_sha,
        require_categories=require_categories,
    )


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
    # One line per failed check whose failures the cross-shard dedupe
    # folded into another check's shard. Without it such a check
    # vanishes from every count — it contributes no shard of its own.
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
    """Resolve a PR's head, list failed statuses, and parse each report.

    ``categories`` restricts which categories are decomposed into
    per-test records; ``None`` (the default) processes **every** failed
    check.

    A check that publishes no failing test — a job log for a
    ``target_url``, an unreadable artefact, a report with no failing
    leaf — still yields one :func:`job_level_failure` record so it gets
    investigated rather than dropped. Pass ``job_level=False`` to have
    those reported in ``skipped_status_warnings`` instead. The only
    status skipped unconditionally is the workflow-level rolled-up
    report, which duplicates the per-job ones.

    Lookups are best-effort per status — a single broken artefact URL
    costs that status its per-test detail, not the whole call.
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

    failed_tests, warnings = decompose_statuses(
        statuses, categories=categories, job_level=job_level,
        pr_number=number,
    )

    # Dedupe: the same test name commonly fails in multiple shards of the
    # same suite. Keep the first occurrence so callers get one record per
    # (category, name) pair, but remember the other shards in
    # ``info_excerpt`` so Claude knows it's not shard-specific.
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

    # A check whose failures all duplicate another's contributes no shard
    # of its own, so it would otherwise disappear from every count.
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
