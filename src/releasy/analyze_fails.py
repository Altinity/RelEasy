"""``releasy analyze-fails`` — investigate failed CI tests on a PR.

Per **failed CI shard** (e.g. one ``Stateless tests (arm_asan, azure,
parallel, 2/4)`` row, or the single ``Fast test`` row), Claude is given
the full bundled list of failures and asked to run the iterative loop:

1. Read every failure, classify each as RELATED or LIKELY-UNRELATED.
2. Group by likely root cause and pick the highest-leverage fix.
3. Make the smallest possible change.
4. Build.
5. Re-run **all** the failed tests in this shard (one go, not one by
   one).
6. See what remains failing.
7. Repeat 2–6 until everything is fixed, the rest is UNRELATED, or the
   build budget is exhausted.

This is dramatically cheaper than per-test Claude invocations when many
tests share a root cause — fixing one regression frequently flips
dozens of tests green at once. The iterative shape is encoded in the
prompt; the orchestrator just bundles, invokes, and tallies.

This module owns: discover failures via :mod:`releasy.ci_failures`,
group them per shard, render the bundled prompt, invoke Claude
(reusing the streaming machinery from :mod:`ai_resolve`), and push at
the end if Claude appended commits.
"""

from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from releasy.pipeline import OnlyFilter

from releasy.ai_resolve import (
    _VERIFY_ALLOWED_TOOLS,
    _build_api_spec,
    _build_claude_argv,
    _exhaustion_kwargs,
    _extract_assistant_text,
    _extract_cost_usd,
    _find_transient_api_error,
    _parse_verify_output,
    _resolve_backend,
    _spawn_claude,
    _write_build_script,
)
from releasy.ci_failures import (
    CATEGORY_ORDER,
    CATEGORY_OTHER,
    BaselineRun,
    FailedTest,
    PRFailures,
    baseline_run_before,
    discover_pr_failures,
    merge_base_sha,
)
from releasy.config import Config, get_github_token, is_stateless
from releasy.git_ops import (
    fetch_remote,
    is_ancestor,
    is_operation_in_progress,
    remote_branch_exists,
    run_git,
    stash_and_clean,
)
from releasy.github_ops import (
    add_label_to_pr,
    ensure_label,
    fetch_pr_by_url,
    get_origin_repo_slug,
    parse_pr_url,
)
from releasy.state import PipelineState, load_state, save_state
from releasy.termlog import console


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------


@dataclass
class ShardOutcome:
    """How one shard's bundled investigation ended."""
    category: str
    shard_context: str
    target_url: str
    test_count: int
    classification: str  # "DONE" | "PARTIAL" | "UNRELATED" | "UNRESOLVED"
    summary: str = ""
    # Full assistant-prose narration captured from the streaming
    # transcript — used for the PR comment so the operator has the
    # whole investigation transcript without scrolling the cropped
    # local terminal output. Empty for shards that bailed before
    # claude produced text (timeout / spawn error).
    narration: str = ""
    cost_usd: float | None = None
    commits_added: int = 0
    # Independent second-session audit. ``verify_reason`` is why this
    # shard was picked for one (``None`` = it wasn't in doubt, so no
    # session ran); ``verify_verdict`` is "ok" / "needs_attention" /
    # "unknown" (unknown = the audit itself failed, advisory only).
    verify_reason: str | None = None
    verify_verdict: str | None = None
    verify_summary: str = ""
    verify_findings: list[str] = field(default_factory=list)
    # 1 for the first investigation, 2+ for a redo the audit triggered.
    # ``superseded`` marks a round a later one replaced — kept for the
    # record, but it isn't this shard's verdict any more.
    round_index: int = 1
    superseded: bool = False

    @property
    def disputed(self) -> bool:
        return self.verify_verdict == "needs_attention"


@dataclass
class RedoContext:
    """What a re-investigation is told about the round it replaces."""
    round_index: int
    classification: str
    commits_added: int
    commit_range: str
    audit_summary: str
    audit_findings: list[str]


@dataclass
class PRRunResult:
    pr_url: str
    head_sha: str
    head_ref: str
    statuses_failed: int = 0
    tests_total: int = 0
    shards_total: int = 0
    shards_processed: int = 0
    shards_done: int = 0
    shards_partial: int = 0
    shards_unrelated: int = 0
    shards_unresolved: int = 0
    shards_audited: int = 0
    shards_disputed: int = 0
    # The read-only auditor touched the repo — a contract violation, so
    # what it observed no longer describes what we'd push.
    audit_mutated_repo: bool = False

    @property
    def open_disputes(self) -> int:
        """Disputes still standing — a redo that fixed one doesn't count."""
        return sum(
            1 for o in self.outcomes if o.disputed and not o.superseded
        )
    commits_added: int = 0
    pushed: bool = False
    cost_usd: float = 0.0
    comment_url: str | None = None
    outcomes: list[ShardOutcome] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    # Failed checks whose failures the cross-shard dedupe folded into
    # another check's shard — they get no shard of their own, so this is
    # the only place they're accounted for.
    covered_elsewhere: list[str] = field(default_factory=list)
    # The pre-change CI run the failures were compared against, and how
    # the comparison came out.
    baseline_sha: str | None = None
    baseline_committed_at: str | None = None
    baseline_note: str | None = None
    tests_pre_existing: int = 0
    error: str | None = None


@dataclass
class AnalyzeFailsResult:
    success: bool
    error: str | None = None
    runs: list[PRRunResult] = field(default_factory=list)
    flaky_elsewhere_map: dict[str, list[str]] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Tracked-PR enumeration & flaky-elsewhere map
# ---------------------------------------------------------------------------


def tracked_pr_urls(
    state: PipelineState | None,
    only: OnlyFilter | None = None,
) -> list[str]:
    """Every PR URL ``releasy run`` has opened that's still in state.

    Skips entries without a ``rebase_pr_url`` (those are still pending
    a PR — nothing to analyse), entries the local state already marks
    ``merged`` or ``skipped`` (no point poking dead PRs), and
    de-duplicates while preserving insertion order. Returns ``[]`` when
    ``state`` is ``None``.

    The authoritative open/merged/closed check still happens per-PR
    inside :func:`_process_pr` against GitHub, so a stale local
    ``needs_review`` for a PR merged externally is caught there. This
    prefilter just avoids one round-trip per obvious dead entry.

    ``only`` (optional) restricts the result to the single tracked
    feature whose URL or feature-id matches the filter.
    """
    if state is None:
        return []
    seen: set[str] = set()
    out: list[str] = []
    for fid, fs in state.features.items():
        url = fs.rebase_pr_url
        if not url or url in seen:
            continue
        if fs.status in (
            "merged", "skipped", "closed", "superseded", "reverted",
        ):
            continue
        if only is not None and not only.matches_state(fid, fs):
            continue
        seen.add(url)
        out.append(url)
    return out


def flaky_scan_extra_for(
    state: PipelineState | None, primary_pr_urls: list[str],
) -> list[str] | None:
    """Pick the flaky-elsewhere scan set for a single-PR-scope run.

    The flaky-elsewhere heuristic needs *other* PRs as evidence; if the
    primary is one specific PR, the scan must reach beyond it. Returns
    the every-other-tracked-PR list, or ``None`` when state is missing
    (caller falls back to ``primary_pr_urls`` — which is correct for
    multi-PR walks where the primary list itself provides enough
    cross-evidence).
    """
    if state is None:
        return None
    primary_set = set(primary_pr_urls)
    return [u for u in tracked_pr_urls(state) if u not in primary_set]


def _build_flaky_elsewhere_map(
    config: Config,
    pr_urls: list[str],
) -> tuple[dict[str, list[str]], list[str]]:
    """Return ``{(category, test_name): [pr_url, …]}`` across ``pr_urls``.

    Every PR contributes its full failed-test list — there is no
    primary/other distinction at build time. Per-PR exclusion is left
    to the lookup site (so a test failing on the PR being analysed
    doesn't count as "elsewhere" evidence about itself).
    """
    flaky_map: dict[str, list[str]] = defaultdict(list)
    warnings: list[str] = []

    cap = config.analyze_fails.flaky_check_prs
    if cap > 0:
        pr_urls = pr_urls[:cap]

    categories = _configured_categories(config)
    for url in pr_urls:
        failures, err = discover_pr_failures(
            config, url, categories=categories,
            job_level=config.analyze_fails.job_level_failures,
        )
        if err or failures is None:
            warnings.append(f"flaky-elsewhere: {url}: {err}")
            continue
        for ft in failures.failed_tests:
            key = _flaky_key(ft.category, ft.name)
            if url not in flaky_map[key]:
                flaky_map[key].append(url)

    return dict(flaky_map), warnings


def _flaky_key(category: str, test_name: str) -> str:
    return f"{category}::{test_name}"


def _configured_categories(config: Config) -> tuple[str, ...] | None:
    """Category allowlist from config; ``None`` means every failed check."""
    return tuple(config.analyze_fails.categories) or None


# ---------------------------------------------------------------------------
# Per-shard reproduction commands + category-specific priors
# ---------------------------------------------------------------------------


# Each entry is a small markdown block that biases Claude's triage step
# in favour of (or against) classifying a failure as caused-by-this-PR.
# The runtime characteristics of each category give different priors:
#
#   - Fast test runs inline on every PR against a debug-built binary
#     against a deterministic set of cheap tests. Flakes are rare —
#     when something there fails, the prior is overwhelmingly "this
#     PR broke it", and flaky-elsewhere evidence is more likely
#     "several rebased PRs share a broken baseline" than "master-side
#     flake".
#   - Stateless / Integration / Regression tests hit real storage,
#     docker, network — genuine flakes are common and flaky-elsewhere
#     annotations are load-bearing UNRELATED evidence.
#
# These priors *bias* the triage step; they don't override the
# scoping rule ("only fix tests this PR broke" still stands). The
# generic rule applies when a category has no specific entry.
_CATEGORY_PRIORS: dict[str, str] = {
    "fasttest": (
        "**Fast test** runs inline on every PR against a deterministic, "
        "fast suite. Genuine master-side flakes here are **vanishingly "
        "rare** — the prior for any Fast test failure is "
        "**~99% that this PR caused it**.\n\n"
        "Apply the flaky-elsewhere annotation with extra scepticism in "
        "this shard: when several tracked rebase PRs all see the same "
        "Fast test failure, the usual cause is that *all of them share "
        "the same broken baseline* (e.g. a recently-rebased master or a "
        "common backport ancestor) — **not** master-side flake. Default "
        "to CAUSED-BY-THIS-PR unless you can produce a concrete, "
        "non-PR explanation (infra crash, build broken before your diff, "
        "the same exact assertion firing on a freshly-rebased master "
        "branch you actually checked). When in doubt in this shard, "
        "**investigate and fix**."
    ),
    "stateless": (
        "**Stateless tests** run against real storage backends (S3, "
        "Azure, replicated DBs) under docker. Flakes from load / docker "
        "/ network jitter / image races are common. The flaky-elsewhere "
        "annotation is **strong UNRELATED evidence** here — trust it. "
        "Default to CAN'T-TELL → NOT-THIS-PR for failures with no "
        "concrete diff link, especially when corroborated by other "
        "tracked PRs."
    ),
    "integration": (
        "**Integration tests** bring up multi-node clusters via docker "
        "and exercise full networking, kafka, replication. Flakes are "
        "routine (image pulls, port contention, startup races). The "
        "flaky-elsewhere annotation is **strong UNRELATED evidence** "
        "here — trust it. Default to CAN'T-TELL → NOT-THIS-PR when the "
        "diff has no plausible link to the failure surface."
    ),
    "regression": (
        "**Regression suites** are TestFlows scenario trees from the "
        "separate `Altinity/clickhouse-regression` repo, run against "
        "minio / localstack / multi-node clusters for hours. Two "
        "consequences for triage:\n\n"
        "- One broken step reports as dozens of failing scenarios. The "
        "enclosing feature/module nodes have already been stripped from "
        "the list below, but sibling scenarios failing in lockstep "
        "almost always share one root cause — find the cause, don't "
        "work the list.\n"
        "- Storage / network jitter makes flakes common, so the "
        "flaky-elsewhere annotation is **strong UNRELATED evidence** — "
        "trust it.\n\n"
        "These suites assert Altinity-specific behaviour (Iceberg "
        "export, swarms, S3 export) that a rebase can genuinely break, "
        "so do check the diff against the failing scenario's subject "
        "before writing it off."
    ),
    "quick_functional": (
        "**Quick functional tests** run a small deterministic query set "
        "against a debug binary with no external storage. Flakes are "
        "**rare** — treat a failure here much like Fast test: the prior "
        "is that this PR caused it. Default to CAUSED-BY-THIS-PR unless "
        "you can name a concrete non-PR cause."
    ),
    CATEGORY_OTHER: (
        "This is a check RelEasy has no reproduction recipe for (stress "
        "test, fuzzer, sqllogic, compatibility, packaging, …). Work out "
        "what it actually exercises *before* judging relatedness — the "
        "job definition and its script are in the repo (see the runner "
        "section below). If you can't reproduce it locally, don't "
        "guess: reason from the failure excerpt plus the PR diff, and "
        "classify honestly."
    ),
}


# Used instead of the category prior when the check failed as a whole
# and published no per-test results — the category says nothing useful
# about a job that never reached its test phase.
_JOB_LEVEL_PRIOR = (
    "This check failed **as a whole**, without per-test results. "
    "Typical causes: the job died before its test phase (runner OOM, "
    "docker / network failure, cancellation), a build or packaging step "
    "failed, or the check has nothing per-test to report (image build, "
    "vulnerability scan, install check).\n\n"
    "Judge relatedness from the job's own log plus the diff. A compile, "
    "link or packaging error naming files this PR touches is "
    "**CAUSED-BY-THIS-PR**. A CVE in a base image, a registry timeout, "
    "a killed runner or an artefact that never uploaded is "
    "**NOT-THIS-PR** — report it, never edit code to silence it."
)


def _category_prior_section(category: str, *, job_level: bool = False) -> str:
    """Render the category-specific scoping prior, or a no-op fallback."""
    if job_level:
        return _JOB_LEVEL_PRIOR
    prior = _CATEGORY_PRIORS.get(category)
    if prior is None:
        return (
            f"_(no category-specific prior for {category!r}; apply the "
            "generic scoping rule above as-is.)_"
        )
    return prior


# Each entry is a small markdown block telling Claude how to invoke the
# right test runner for the category. ``{tests_arg}`` is substituted
# with a space-separated quoted list of the failing test names (for
# regression: TestFlows ``--only`` patterns), ``{shard_context}`` with
# the CI status context, and ``{repo_dir}`` with the absolute repo path.
# Claude is told that the test list is the ground truth for "what was
# failing" and that it must re-invoke the runner with a (possibly
# shrinking) subset on every iteration of the fix-build-rerun loop.
_CATEGORY_RUNNER_HINTS: dict[str, str] = {
    "fasttest": (
        "Fast test runs the bulk of stateless tests. Locally, the "
        "canonical way to run an explicit list of tests is:\n\n"
        "```bash\n"
        "rm -rf ci/tmp\n"
        "tests/clickhouse-test {tests_arg}\n"
        "```\n\n"
        "On the very first iteration, run the full list above. After "
        "every fix attempt, rerun whichever subset is still expected "
        "to fail (i.e. not yet confirmed passing) plus a couple of "
        "previously-passing tests as a regression spot-check."
    ),
    "stateless": (
        "Stateless tests run via `tests/clickhouse-test`. The shard "
        "name (`{shard_context}`) tells you the storage backend; "
        "translate it into the runner flags as follows:\n\n"
        "- `azure` → pass `--azure`\n"
        "- `s3 storage` → pass `--s3-storage`\n"
        "- `db disk` → pass `--db-engine=Replicated` (only when the "
        "  shard says \"db disk\"; harmless to omit otherwise)\n"
        "- `distributed plan` → pass "
        "  `--distributed-plan` (the runner flag varies between "
        "  ClickHouse forks; use whatever the existing CI scripts "
        "  under `ci/jobs/` invoke for this shard)\n\n"
        "Run all the failing tests in one go:\n\n"
        "```bash\n"
        "rm -rf ci/tmp\n"
        "tests/clickhouse-test <shard-flags> {tests_arg}\n"
        "```\n\n"
        "If you can't infer the right flags, look at the shell "
        "snippet under `ci/jobs/<job>.sh` (or the equivalent file in "
        "the ClickHouse fork) — that file is what CI uses to invoke "
        "this exact shard."
    ),
    "integration": (
        "Integration tests are pytest-driven and run via "
        "`tests/integration/runner`. Each test name is in the form "
        "`<dir>/<file>.py::<test>[<params>]`. To run the full failing "
        "list in one go:\n\n"
        "```bash\n"
        "rm -rf ci/tmp\n"
        "cd tests/integration\n"
        "./runner --binary $(pwd)/../../build/programs/clickhouse "
        "{tests_arg}\n"
        "```\n\n"
        "If the runner pulls docker images on first invocation, that "
        "is expected — wait it out, it's not the failure under "
        "investigation."
    ),
    "regression": (
        "**These tests are not in this repository.** The regression "
        "suites live in `Altinity/clickhouse-regression`, which CI "
        "clones fresh per run; `.github/workflows/regression.yml` and "
        "`.github/workflows/regression-reusable-suite.yml` in this "
        "checkout are the ground truth for how each suite is invoked.\n\n"
        "The first path segment of each failing test is the suite "
        "directory (`/swarms/…` → `swarms`, `/s3/…` → `s3`). Clone into "
        "`ci/tmp/` (gitignored, so it can't dirty this branch), build "
        "ClickHouse here first, then:\n\n"
        "```bash\n"
        "git clone --depth 1 https://github.com/Altinity/clickhouse-regression "
        "{repo_dir}/ci/tmp/clickhouse-regression\n"
        "cd {repo_dir}/ci/tmp/clickhouse-regression\n"
        "python3 -u <suite>/regression.py \\\n"
        "  --clickhouse {repo_dir}/build/programs/clickhouse \\\n"
        "  --only {tests_arg} \\\n"
        "  --test-to-end --no-colors --local --collect-service-logs \\\n"
        "  --log raw.log\n"
        "```\n\n"
        "Test paths contain spaces — always quote each `--only` "
        "pattern. Storage-flavoured shards need the extra arguments CI "
        "passes (`--storage minio`, `--gcs-uri …`, `--use-keeper`, …); "
        "look up `{shard_context}` in `regression.yml` for the exact "
        "set. The report's own `fails.log.txt` also ends with a "
        "**Debugging** section quoting a working `--only` for its first "
        "failure — trust that over any guess.\n\n"
        "Richer logs sit next to the report at `{report_dir}/`, and you "
        "can fetch any of them directly:\n\n"
        "- `fails.log.txt` — the failure list this bundle was built "
        "from, ending in a working `--only` for its first failure.\n"
        "- `nice-new-fails.log.txt` — full verbose log of the *new* "
        "failures. This is where per-test detail lives when the "
        "excerpts above only say `AssertionError`.\n"
        "- `raw.log` — the complete TestFlows message stream (large).\n"
        "- `_service_logs/` and `*/_instances/*/logs/` — ClickHouse, "
        "keeper, minio and localstack server logs.\n\n"
        "Standing this environment up is expensive (docker, minio, "
        "localstack, an external repo). If you can't, say so plainly "
        "and diagnose from the logs above plus the diff — that is a "
        "valid outcome here. And note **fixes belong in this "
        "repository**: if the real fix is in the regression suite "
        "itself, do not try to commit it here; report it and classify "
        "the shard."
    ),
    "quick_functional": (
        "Quick functional tests run `ci/jobs/clickhouse_light.py` over "
        "the queries in `ci/jobs/queries/` — **not** "
        "`tests/clickhouse-test`:\n\n"
        "```bash\n"
        "python3 ./ci/jobs/clickhouse_light.py "
        "--path {repo_dir}/build/programs/clickhouse\n"
        "```\n\n"
        "(CI points `--path` at the binary it downloaded into "
        "`ci/tmp`; locally point it at your own build.) The script runs "
        "the whole set — there is no per-test argument — so the failing "
        "tests to watch for are:\n\n"
        "```\n"
        "{tests_arg}\n"
        "```"
    ),
}


# Used for a check that published no per-test failures at all. There is
# no test list to re-run, so the category recipe must NOT be used —
# interpolating an empty test list into `tests/clickhouse-test` would
# run the entire suite.
_JOB_LEVEL_RUNNER_HINT = (
    "**There is no test list for this check** — it failed as a whole, so "
    "`{failed_tests_file}` is empty and there is nothing to re-run. Do "
    "**not** invoke a test runner without arguments; that runs the whole "
    "suite and tells you nothing.\n\n"
    "Read the evidence first:\n\n"
    "- The job's report / log is at {target_url} — fetch it (`WebFetch`, "
    "or `curl` if the artefact is plain text). The failure reason is "
    "there.\n"
    "- Strip the parenthesised parameters from `{shard_context}` and look "
    "the job name up in `ci/defs/job_configs.py`: its `command=` is "
    "verbatim what CI ran, and the script it names lives under "
    "`ci/jobs/`. For a `Regression …` check, look in "
    "`.github/workflows/regression.yml` instead.\n\n"
    "Reproduce only the single failing step, and only if it is cheap and "
    "local (a build, a script under `ci/jobs/`). If it needs CI "
    "infrastructure you don't have — a docker registry, S3 credentials, "
    "a vulnerability database — don't attempt it: diagnose from the log "
    "plus the diff and classify honestly."
)


# Fallback for a failed check with no recipe of its own. Deliberately
# points at the job definition instead of guessing an invocation: in
# praktika, ``Job.Config.command`` is verbatim what CI ran.
_GENERIC_RUNNER_HINT = (
    "RelEasy has no runner recipe for this check, so find the "
    "invocation before touching any code. Strip the parenthesised "
    "parameters from `{shard_context}` and look that job name up in "
    "`ci/defs/job_configs.py` — its `command=` is exactly what CI ran, "
    "and the script it names lives under `ci/jobs/`.\n\n"
    "The failing tests in this shard are:\n\n"
    "```\n"
    "{tests_arg}\n"
    "```\n\n"
    "If the check has no per-test runner at all (a build, a packaging "
    "or an image check), reproducing means running that whole command "
    "locally. If you can't, diagnose from the excerpts plus the diff "
    "and classify — do not guess at a fix."
)


def _quote_for_shell(name: str) -> str:
    """Single-quote ``name`` for safe inclusion in a shell command line."""
    if not name:
        return "''"
    if all(c.isalnum() or c in "_-./:[]=+@" for c in name):
        return name
    return "'" + name.replace("'", "'\\''") + "'"


def _tests_arg(category: str, test_names: list[str]) -> str:
    """Render failing test names as the runner's test arguments.

    TestFlows selects by path pattern rather than by exact name, so
    regression tests get the trailing ``/*`` its ``--only`` expects.
    """
    if category == "regression":
        return " ".join(_quote_for_shell(f"{n}/*") for n in test_names)
    return " ".join(_quote_for_shell(n) for n in test_names)


# Appended when a regression shard has more failing paths than fit on a
# command line. The other categories inline ``$(cat …)`` instead, which
# only works because their test names contain no spaces — TestFlows
# paths do, so they have to go through an array.
_REGRESSION_OVERFLOW_NOTE = (
    "_This shard has {count} failing test paths — more than fit on one "
    "command line. All of them are in `.releasy/failed-tests.txt`, one "
    "per line. Build the pattern list from that file rather than "
    "retyping it:_\n\n"
    "```bash\n"
    "mapfile -t PATTERNS < <(sed 's|$|/*|' .releasy/failed-tests.txt)\n"
    "# … --only \"${PATTERNS[@]}\" …\n"
    "```"
)


def _category_runner_section(
    category: str, test_names: list[str], shard_context: str,
    repo_path: Path, target_url: str = "",
    *, max_inline: int = 25, job_level: bool = False,
) -> str:
    if job_level:
        return (
            _JOB_LEVEL_RUNNER_HINT
            .replace("{shard_context}", shard_context)
            .replace("{target_url}", target_url or "(no report URL)")
            .replace("{failed_tests_file}", ".releasy/failed-tests.txt")
        )
    template = _CATEGORY_RUNNER_HINTS.get(category, _GENERIC_RUNNER_HINT)
    overflow_note = ""
    if len(test_names) <= max_inline:
        tests_arg = _tests_arg(category, test_names)
    elif category == "regression":
        tests_arg = _tests_arg(category, test_names[:max_inline])
        overflow_note = _REGRESSION_OVERFLOW_NOTE.replace(
            "{count}", str(len(test_names)),
        )
    else:
        # Too many to fit on one shell command line cleanly; tell
        # Claude to use a temp file. Inline the first few as a teaser
        # so the prompt reads sensibly without the file detour.
        head = _tests_arg(category, test_names[:max_inline])
        tests_arg = (
            f"$(cat .releasy/failed-tests.txt)  # the full list lives "
            "in `.releasy/failed-tests.txt` (one test name per line); "
            f"the first {max_inline} are: {head}"
        )
    section = (
        template
        .replace("{tests_arg}", tests_arg)
        .replace("{shard_context}", shard_context)
        .replace("{repo_dir}", str(repo_path))
        # TestFlows artefacts are siblings of the report the status
        # links to, so the report's directory is where to look.
        .replace("{report_dir}", target_url.rsplit("/", 1)[0])
    )
    if overflow_note:
        section = f"{section}\n\n{overflow_note}"
    return section


# ---------------------------------------------------------------------------
# Bundled-failure prompt rendering
# ---------------------------------------------------------------------------


# Per-test info excerpts can be massive; we trim each one before
# bundling so the prompt stays readable. Claude can always fetch the
# full report via the shard's `target_url` if it needs more.
_PER_TEST_EXCERPT_MAX = 1000

# When a shard has more failures than this, the bundled list is split
# into "first N (verbatim)" + "remaining count" — Claude is told the
# canonical list lives in ``.releasy/failed-tests.txt`` and is
# encouraged to consult it.
_INLINE_FAILURE_LIMIT = 30


def _baseline_line(test: FailedTest, baseline: BaselineRun | None) -> str:
    """The one-line pre-change verdict shown under a failure block."""
    if baseline is None:
        return "baseline: no pre-change run available."
    verdict = baseline.verdict_for(test.category, test.name)
    short = baseline.sha[:10]
    if verdict == "failed":
        where = baseline.failing.get((test.category, test.name), "")
        return (
            f"**pre-existing:** already failing before this PR — "
            f"baseline commit `{short}` ({baseline.committed_at}), in "
            f"`{where}`. **This PR did not break it.**"
        )
    if verdict == "passed":
        return (
            f"**new since baseline:** did NOT fail at `{short}` "
            f"({baseline.committed_at}), where its category did run — "
            "this PR is the prime suspect."
        )
    return (
        f"baseline: `{short}` never ran a {test.category} check, so it "
        "says nothing about this failure."
    )


def _render_failure_block(
    test: FailedTest,
    index: int,
    flaky_map: dict[str, list[str]],
    threshold: int,
    current_pr_url: str,
    baseline: BaselineRun | None = None,
) -> str:
    info = (test.info_excerpt or "").strip()
    if len(info) > _PER_TEST_EXCERPT_MAX:
        info = info[:_PER_TEST_EXCERPT_MAX] + "\n…(truncated)"
    others = [
        u for u in flaky_map.get(_flaky_key(test.category, test.name), [])
        if u != current_pr_url
    ]
    if others and threshold > 0 and len(others) >= threshold:
        flaky = (
            f"**flaky-elsewhere:** failing on {len(others)} other "
            f"tracked PR(s) — strong prior for UNRELATED."
        )
    elif others:
        flaky = (
            f"flaky-elsewhere: {len(others)} other tracked PR(s)."
        )
    else:
        flaky = "flaky-elsewhere: none."

    if test.job_level:
        heading = (
            f"### {index}. Job-level failure: `{test.name}` "
            f"({test.status}) — not a test, no per-test results"
        )
    else:
        heading = f"### {index}. `{test.name}`  ({test.status})"

    lines = [
        heading,
        "",
        f"- {_baseline_line(test, baseline)}",
        f"- {flaky}",
        "",
    ]
    if info:
        lines.append(f"---BEGIN FAILURE EXCERPT #{index}---")
        lines.append(info)
        lines.append(f"---END FAILURE EXCERPT #{index}---")
    else:
        lines.append("_(no per-test info captured by the CI report)_")
    return "\n".join(lines)


def _baseline_section(
    tests: list[FailedTest], baseline: BaselineRun | None,
    base_branch: str, unavailable_reason: str | None = None,
) -> str:
    """Render the shard's pre-change comparison for the prompt."""
    if baseline is None:
        return (
            "RelEasy found **no CI run on "
            f"`{base_branch}` predating this PR** to compare against"
            + (f" ({unavailable_reason})" if unavailable_reason else "")
            + ". Triage from the diff and the flaky-elsewhere "
            "annotations alone, and be correspondingly careful before "
            "calling a failure this PR's fault."
        )

    pre_existing = [
        t for t in tests
        if baseline.verdict_for(t.category, t.name) == "failed"
    ]
    fresh = [
        t for t in tests
        if baseline.verdict_for(t.category, t.name) == "passed"
    ]
    unknown = len(tests) - len(pre_existing) - len(fresh)

    lines = [
        f"The last CI run on `{base_branch}` **without this PR's diff** "
        f"is commit `{baseline.sha[:10]}` ({baseline.committed_at}), "
        f"where {baseline.checks_failed} of {baseline.checks_total} "
        f"checks were already red. Comparing this shard against it:",
        "",
    ]
    if baseline.skipped_newer:
        lines += [
            f"_({baseline.skipped_newer} newer run(s) on `{base_branch}` "
            "were passed over — they never ran these checks. So a "
            "`new since baseline` verdict here can also mean 'broken by "
            "something merged into the target branch after the "
            "baseline'; confirm against the diff before fixing.)_",
            "",
        ]
    lines += [
        f"- **{len(pre_existing)} of {len(tests)} already failed there** "
        "— pre-existing breakage, NOT this PR's to fix. Classify as "
        "`[unrelated]` and do not edit code for them.",
        f"- **{len(fresh)} did not fail there** while their category "
        "did run — these are the failures worth your build budget.",
    ]
    if unknown:
        lines.append(
            f"- {unknown} could not be compared (the baseline run never "
            "exercised their check)."
        )
    lines += [
        "",
        "Each failure below carries its own verdict. The baseline is "
        "**stronger evidence than any reasoning from the diff**: a test "
        "that was already red on the target branch cannot have been "
        "broken by a change that isn't in it yet.",
    ]
    if fresh and len(fresh) < len(tests):
        lines += [
            "",
            "So start with the new-since-baseline failures. If they turn "
            "out to be unrelated too, say so and finish — do not spend "
            "the budget re-litigating the pre-existing ones.",
        ]
    elif not fresh:
        lines += [
            "",
            "**Nothing in this shard is new since the baseline.** Verify "
            "the comparison holds (spot-check one or two failures "
            "against the baseline report), then report `UNRELATED` "
            "without building anything.",
        ]
    return "\n".join(lines)


def _redo_section(redo: RedoContext | None, pr_branch: str) -> str:
    """Tell a re-investigation what the audit objected to, and to fix it."""
    if redo is None:
        return (
            "_(First look at this shard — no prior round to correct.)_"
        )
    lines = [
        f"**This is attempt {redo.round_index + 1}.** Round "
        f"{redo.round_index} concluded `{redo.classification}`"
        + (
            f" and committed {redo.commits_added} change(s) "
            f"(`{redo.commit_range}`)"
            if redo.commits_added else " and committed nothing"
        )
        + ". An independent audit rejected that outcome:",
        "",
    ]
    if redo.audit_summary:
        lines += [f"> {redo.audit_summary}", ""]
    for f in redo.audit_findings[:10]:
        lines.append(f"- {f}")
    if redo.audit_findings:
        lines.append("")
    lines += [
        "Those commits are **still on the branch** — you start from the "
        "tip that includes them. Act on the findings:",
        "",
        f"- If a finding says a commit silences a test rather than "
        f"fixing it (assertion weakened or deleted, reference output "
        f"rewritten, tolerance widened, test skipped), `git revert "
        f"--no-edit <sha>` it first, then either fix the cause properly "
        f"or classify the failure honestly. A reverted bad fix plus an "
        f"accurate `[unrelated]` is a **better** outcome than a fix that "
        f"hides a regression.\n"
        f"- If a finding says an edit is out of scope, revert that "
        f"commit.\n"
        f"- If a finding says a verdict contradicts the evidence, "
        f"re-triage that failure specifically — do not restate the "
        f"previous conclusion without new evidence.\n"
        f"- If you still believe the previous conclusion after checking, "
        f"say so explicitly and give the evidence that answers the "
        f"finding. Standing your ground is allowed; ignoring the "
        f"finding is not.",
        "",
        f"Revert, never rewrite: history on `{pr_branch}` stays "
        "append-only (see the linear-history section below).",
    ]
    return "\n".join(lines)


def _render_shard_prompt(
    config: Config,
    repo_path: Path,
    pr_url: str,
    pr_number: int,
    pr_branch: str,
    base_branch: str,
    shard_context: str,
    target_url: str,
    category: str,
    tests: list[FailedTest],
    flaky_map: dict[str, list[str]],
    baseline: BaselineRun | None = None,
    baseline_note: str | None = None,
    redo: RedoContext | None = None,
) -> str:
    raw = config.analyze_fails.prompt_file
    prompt_path = Path(raw)
    if not prompt_path.is_absolute():
        prompt_path = (config.repo_dir / prompt_path).resolve()
    if not prompt_path.exists():
        raise FileNotFoundError(
            f"analyze_fails prompt template not found: {prompt_path}. "
            "Set analyze_fails.prompt_file in config, or copy the "
            "bundled prompts/analyze_fails.md alongside config.yaml."
        )
    template = prompt_path.read_text(encoding="utf-8")

    repo_slug = get_origin_repo_slug(config) or "<unknown>"
    threshold = config.analyze_fails.flaky_elsewhere_threshold

    inline = tests[:_INLINE_FAILURE_LIMIT]
    extra = tests[_INLINE_FAILURE_LIMIT:]
    blocks = "\n\n".join(
        _render_failure_block(
            t, i + 1, flaky_map, threshold, pr_url, baseline,
        )
        for i, t in enumerate(inline)
    )
    if extra:
        blocks += (
            f"\n\n_(+{len(extra)} more failing test(s) in this shard — "
            "the canonical list lives in `.releasy/failed-tests.txt`, "
            "one test name per line. Use that file as the ground truth "
            "for the test arguments in the runner command.)_"
        )

    # Job-level records name a job or one of its steps, never a test —
    # they must not reach the runner command line. A shard left with no
    # real test name is a job-level shard.
    runnable = [t.name for t in tests if not t.job_level]
    job_level = not runnable
    runner_section = _category_runner_section(
        category, runnable, shard_context, repo_path,
        target_url, job_level=job_level,
    )
    category_prior = _category_prior_section(category, job_level=job_level)

    placeholders = {
        "repo_slug": repo_slug,
        "cwd": str(repo_path),
        "pr_url": pr_url,
        "pr_number": str(pr_number),
        "pr_branch": pr_branch,
        "base_branch": base_branch,
        "shard_context": shard_context,
        "target_url": target_url,
        "test_category": category,
        "category_prior": category_prior,
        "baseline_section": _baseline_section(
            tests, baseline, base_branch, baseline_note,
        ),
        "redo_section": _redo_section(redo, pr_branch),
        "failure_count": str(len(tests)),
        "failure_blocks": blocks,
        "runner_section": runner_section,
        "max_iterations": str(config.analyze_fails.max_iterations),
        "build_script": ".releasy/build.sh",
        "build_log": ".releasy/build.log",
        "build_command": config.ai_resolve.build_command,
        "failed_tests_file": ".releasy/failed-tests.txt",
    }

    def _replace(match: re.Match[str]) -> str:
        key = match.group(1)
        return placeholders.get(key, match.group(0))

    return re.sub(r"\{([a-zA-Z_][a-zA-Z0-9_]*)\}", _replace, template)


def _write_failed_tests_manifest(
    repo_path: Path, tests: list[FailedTest],
) -> None:
    """Drop the canonical failed-test list as a sibling of build.sh.

    Claude reads this file when the test list is too long to embed
    cleanly in the runner command line. Unconditional write so the file
    always reflects the *current* shard's failure set, not the previous
    one. Job-level records name a CI job, not a test, so they leave the
    file empty — feeding one to a runner would select nothing (or, with
    ``clickhouse-test``, everything).
    """
    names = [t.name for t in tests if not t.job_level]
    target = repo_path / ".releasy" / "failed-tests.txt"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        ("\n".join(names) + "\n") if names else "", encoding="utf-8",
    )


# ---------------------------------------------------------------------------
# Claude invocation
# ---------------------------------------------------------------------------


_TERMINAL_TOKENS = ("DONE", "PARTIAL", "UNRELATED", "UNRESOLVED")


def _classify_outcome(text: str) -> tuple[str, str]:
    """Inspect the AI's tail output and pick a terminal classification.

    Returns ``(token, summary)``. Falls back to ``UNRESOLVED`` when no
    recognised terminal line is found. ``DONE`` / ``PARTIAL`` /
    ``UNRELATED`` / ``UNRESOLVED`` mirror the prompt's contract.
    """
    if not text.strip():
        return "UNRESOLVED", "(no narration captured)"
    tail = text.strip().splitlines()[-30:]
    found = None
    for line in reversed(tail):
        s = line.strip()
        if s in _TERMINAL_TOKENS:
            found = s
            break
    summary = "\n".join(tail[-15:])
    if found is None:
        return "UNRESOLVED", summary
    return found, summary


# Placeholders accepted inside ``analyze_fails.allowed_tools`` /
# ``extra_args`` entries so users don't have to hardcode their absolute
# work-dir path. Resolved per-invocation against the live repo path.
_TOOL_PATH_PLACEHOLDERS = ("{work_dir}", "{repo_dir}", "{cwd}")


def _resolve_tool_paths(items: list[str], repo_path: Path) -> list[str]:
    """Substitute ``{work_dir}``-style placeholders in tool/arg specs.

    Lets ``config.yaml`` carry a portable allowlist like::

        allowed_tools:
          - Bash({work_dir}/build/programs/clickhouse:*)

    and have it resolve to the actual repo path each invocation, even
    when ``work_dir`` is overridden via CLI or differs between
    machines. Aliases (``{repo_dir}``, ``{cwd}``) all resolve to the
    same path — callers can pick whichever name reads most natural for
    their entry.
    """
    repo_str = str(repo_path)
    out: list[str] = []
    for entry in items:
        s = entry
        for ph in _TOOL_PATH_PLACEHOLDERS:
            if ph in s:
                s = s.replace(ph, repo_str)
        out.append(s)
    return out


def _verification_reason(
    classification: str,
    commits_added: int,
    tests: list[FailedTest],
    baseline: BaselineRun | None,
    flaky_map: dict[str, list[str]],
    threshold: int,
    pr_url: str,
) -> str | None:
    """Why this shard's outcome needs a second opinion — ``None`` if not.

    Two things count as doubt:

    * **Code landed.** A commit is about to be pushed to someone's PR
      on the strength of one session's judgement.
    * **The verdict contradicts the evidence.** "Nothing here is mine"
      over a failure that passed at the baseline and fails on no other
      tracked PR is exactly the call that must not go unchallenged.

    Everything else is left alone: a shard that changed nothing and
    whose failures all predate the PR is already evidenced, and
    UNRESOLVED without commits has no conclusion to audit — a human is
    needed either way.
    """
    if commits_added > 0:
        return (
            f"the session committed {commits_added} change(s) to the PR "
            "branch"
        )
    if classification not in ("UNRELATED", "DONE"):
        return None
    if baseline is None:
        return None
    unexplained = [
        t for t in tests
        if baseline.verdict_for(t.category, t.name) == "passed"
        and len([
            u for u in flaky_map.get(_flaky_key(t.category, t.name), [])
            if u != pr_url
        ]) < max(threshold, 1)
    ]
    if unexplained:
        names = ", ".join(t.name for t in unexplained[:3])
        return (
            f"the session called this shard {classification}, but "
            f"{len(unexplained)} failure(s) passed at the baseline and "
            f"fail on no other tracked PR ({names}"
            + ("…" if len(unexplained) > 3 else "") + ")"
        )
    return None


def _render_verify_prompt(
    config: Config,
    repo_path: Path,
    pr_url: str,
    pr_number: int,
    pr_branch: str,
    base_branch: str,
    shard_context: str,
    target_url: str,
    category: str,
    tests: list[FailedTest],
    baseline: BaselineRun | None,
    outcome: ShardOutcome,
    commit_range: str,
) -> str:
    raw = config.analyze_fails.verify_prompt_file
    prompt_path = Path(raw)
    if not prompt_path.is_absolute():
        prompt_path = (config.repo_dir / prompt_path).resolve()
    if not prompt_path.exists():
        raise FileNotFoundError(
            f"analyze_fails verify prompt not found: {prompt_path}. Set "
            "analyze_fails.verify_prompt_file, copy the bundled "
            "prompts/verify_analysis.md alongside config.yaml, or turn "
            "the audit off with analyze_fails.verify_outcome: false."
        )
    template = prompt_path.read_text(encoding="utf-8")

    verdict_lines = []
    for t in tests[:_INLINE_FAILURE_LIMIT]:
        mark = (
            baseline.verdict_for(t.category, t.name) if baseline
            else "no baseline"
        )
        label = {
            "failed": "pre-existing at baseline",
            "passed": "NEW since baseline",
            "not covered": "baseline did not run this check",
        }.get(mark, mark)
        verdict_lines.append(f"- [{label}] `{t.name}`")
    if len(tests) > _INLINE_FAILURE_LIMIT:
        verdict_lines.append(
            f"- …(+{len(tests) - _INLINE_FAILURE_LIMIT} more; full list "
            "in `.releasy/failed-tests.txt`)"
        )

    placeholders = {
        "repo_slug": get_origin_repo_slug(config) or "<unknown>",
        "cwd": str(repo_path),
        "pr_url": pr_url,
        "pr_number": str(pr_number),
        "pr_branch": pr_branch,
        "base_branch": base_branch,
        "shard_context": shard_context,
        "target_url": target_url,
        "test_category": category,
        "failure_count": str(len(tests)),
        "failure_verdicts": "\n".join(verdict_lines),
        "baseline_sha": baseline.sha if baseline else "(none)",
        "baseline_committed_at": (
            baseline.committed_at if baseline else "(none)"
        ),
        "classification": outcome.classification,
        "doubt_reason": outcome.verify_reason or "(unspecified)",
        "commit_range": commit_range or "(no commits)",
        "commit_count": str(outcome.commits_added),
        "claimed_summary": _trim_narration_for_comment(outcome.narration),
        "failed_tests_file": ".releasy/failed-tests.txt",
    }

    def _replace(match: re.Match[str]) -> str:
        return placeholders.get(match.group(1), match.group(0))

    return re.sub(r"\{([a-zA-Z_][a-zA-Z0-9_]*)\}", _replace, template)


def _invoke_verifier(
    config: Config, repo_path: Path, prompt: str,
) -> tuple[int, str, bool, float | None]:
    """Spawn the read-only auditor in a fresh session."""

    class _ResolveShim:
        command = config.analyze_fails.command
        allowed_tools = list(_VERIFY_ALLOWED_TOOLS)
        extra_args = list(config.analyze_fails.extra_args)

    class _ConfigShim:
        ai_resolve = _ResolveShim
        ai_model = config.ai_model
        ai_effort = config.ai_effort
        ai_backend = config.ai_backend
        ai_api = config.ai_api

    argv = _build_claude_argv(_ConfigShim)  # type: ignore[arg-type]
    api = _build_api_spec(_ConfigShim)  # type: ignore[arg-type]
    exit_code, output, timed_out = _spawn_claude(
        argv, repo_path, config.analyze_fails.verify_timeout_seconds,
        prompt=prompt, api=api, **_exhaustion_kwargs(config),
    )
    return exit_code, output, timed_out, _extract_cost_usd(output)


def _invoke_claude(
    config: Config, repo_path: Path, prompt: str,
) -> tuple[int, str, bool, float | None]:
    resolved_tools = _resolve_tool_paths(
        list(config.analyze_fails.allowed_tools), repo_path,
    )
    resolved_extra = _resolve_tool_paths(
        list(config.analyze_fails.extra_args), repo_path,
    )

    class _ResolveShim:
        command = config.analyze_fails.command
        allowed_tools = resolved_tools
        extra_args = resolved_extra

    class _ConfigShim:
        ai_resolve = _ResolveShim
        ai_model = config.ai_model
        ai_effort = config.ai_effort
        ai_backend = config.ai_backend
        ai_api = config.ai_api

    argv = _build_claude_argv(_ConfigShim)  # type: ignore[arg-type]
    api = _build_api_spec(_ConfigShim)  # type: ignore[arg-type]
    exit_code, output, timed_out = _spawn_claude(
        argv, repo_path, config.analyze_fails.timeout_seconds,
        prompt=prompt, api=api, **_exhaustion_kwargs(config),
    )
    cost = _extract_cost_usd(output)
    return exit_code, output, timed_out, cost


# ---------------------------------------------------------------------------
# Per-PR / per-shard flow
# ---------------------------------------------------------------------------


def _fetch_pr_meta(
    pr_url: str,
) -> tuple[str, str, str, str, int] | None:
    """Resolve PR head ref / head repo / base ref / head SHA / number."""
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
        head_repo = (
            pr.head.repo.full_name if pr.head.repo is not None
            else f"{owner}/{repo}"
        )
        return pr.head.ref, head_repo, pr.base.ref, pr.head.sha, pr.number
    except Exception:
        return None


def _checkout_pr_head(
    config: Config, repo_path: Path, head_ref: str,
) -> tuple[bool, str | None, str | None]:
    """Refresh remote, switch to ``head_ref``, return (ok, start_sha, error)."""
    remote = config.origin.remote_name
    if not remote_branch_exists(repo_path, head_ref, remote):
        return False, None, (
            f"PR head branch {head_ref!r} is not visible on {remote} "
            "after fetch — was the branch deleted?"
        )
    fetch_remote(repo_path, remote)
    stash_and_clean(repo_path)
    co = run_git(
        ["checkout", "-B", head_ref, f"{remote}/{head_ref}"],
        repo_path, check=False,
    )
    if co.returncode != 0:
        return False, None, (
            f"Could not check out {head_ref}: {co.stderr.strip()}"
        )
    rev = run_git(
        ["rev-parse", "--verify", "HEAD"], repo_path, check=False,
    )
    if rev.returncode != 0:
        return False, None, "Could not resolve HEAD after checkout"
    return True, rev.stdout.strip(), None


def _verify_post_run_cleanliness(repo_path: Path) -> str | None:
    if is_operation_in_progress(repo_path):
        return (
            "git operation still in progress after claude exited — "
            "nothing pushed."
        )
    porc = run_git(
        ["status", "--porcelain", "--untracked-files=no"],
        repo_path, check=False,
    )
    if porc.stdout.strip():
        dirty = ", ".join(line[3:] for line in porc.stdout.splitlines()[:5])
        return f"working tree not clean after claude: {dirty}"
    return None


def _group_failures_by_shard(
    failed_tests: list[FailedTest],
) -> list[tuple[str, str, str, list[FailedTest]]]:
    """Bucket failures by ``(category, shard_context, target_url)``.

    Returns a list of ``(category, shard_context, target_url, tests)``
    in :data:`CATEGORY_ORDER` — fasttest first (single shard, broad
    blast radius), regression last (external repo, slowest to
    reproduce) — and within a category alphabetical by shard, so the
    output is reproducible.
    """
    groups: dict[
        tuple[str, str, str], list[FailedTest],
    ] = {}
    for ft in failed_tests:
        key = (ft.category, ft.shard_context, ft.target_url)
        groups.setdefault(key, []).append(ft)

    return sorted(
        (
            (cat, ctx, url, tests)
            for (cat, ctx, url), tests in groups.items()
        ),
        key=lambda x: (CATEGORY_ORDER.get(x[0], 99), x[1]),
    )


# Baseline runs are decomposed once per merge base, not once per PR:
# a batch of rebase PRs cut from the same target-branch commit shares
# one, and decoding a run means re-fetching every failed shard's report.
# Keyed by everything that changes the result; process-lifetime only.
_BASELINE_CACHE: dict[
    tuple[str, str, tuple[str, ...] | None, bool, frozenset[str]],
    tuple[BaselineRun | None, str | None],
] = {}


def _tally_outcome(result: PRRunResult, outcome: ShardOutcome) -> None:
    """Count a shard's *final* outcome and print its marker."""
    field_name = {
        "DONE": "shards_done",
        "PARTIAL": "shards_partial",
        "UNRELATED": "shards_unrelated",
        "UNRESOLVED": "shards_unresolved",
    }.get(outcome.classification, "shards_unresolved")
    setattr(result, field_name, getattr(result, field_name) + 1)


def _run_investigation_round(
    config: Config,
    repo_path: Path,
    prompt: str,
    start_sha: str,
    *,
    category: str,
    shard_ctx: str,
    target_url: str,
    test_count: int,
) -> tuple[ShardOutcome, str, bool]:
    """One investigator session plus the checks that make it pushable.

    Returns ``(outcome, head_sha_after, unsafe)``. ``unsafe`` means the
    branch is no longer append-only, so the caller must stop touching
    this PR — nothing after that point can be trusted or pushed.
    """
    def _failed(summary: str, narration: str, cost: float | None) -> ShardOutcome:
        return ShardOutcome(
            category=category, shard_context=shard_ctx,
            target_url=target_url, test_count=test_count,
            classification="UNRESOLVED", summary=summary,
            narration=narration, cost_usd=cost,
        )

    exit_code, output, timed_out, cost = _invoke_claude(
        config, repo_path, prompt,
    )
    narration = _extract_assistant_text(output)

    if timed_out:
        console.print(
            f"    [red]✗[/red] timed out after "
            f"{config.analyze_fails.timeout_seconds}s"
        )
        return _failed("claude timed out", narration, cost), start_sha, False

    if exit_code != 0:
        transient = _find_transient_api_error(output)
        console.print(
            f"    [red]✗[/red] claude exited {exit_code}"
            + (f" — transient: {transient}" if transient else "")
        )
        return _failed(
            (narration.strip().splitlines() or ["<empty>"])[-1],
            narration, cost,
        ), start_sha, False

    clean_err = _verify_post_run_cleanliness(repo_path)
    if clean_err:
        console.print(f"    [red]✗[/red] {clean_err}")
        return _failed(clean_err, narration, cost), start_sha, False

    new_head = run_git(
        ["rev-parse", "--verify", "HEAD"], repo_path, check=False,
    )
    if new_head.returncode != 0:
        console.print("    [red]✗[/red] could not read HEAD post-run")
        return _failed(
            "git rev-parse HEAD failed", narration, cost,
        ), start_sha, False
    new_sha = new_head.stdout.strip()

    if new_sha != start_sha and is_ancestor(repo_path, start_sha, new_sha) is not True:
        msg = (
            "non-linear history detected (HEAD is not a descendant of "
            f"{start_sha[:10]} — refusing to push)"
        )
        console.print(f"    [red]✗[/red] {msg}")
        return _failed(msg, narration, cost), new_sha, True

    token, summary = _classify_outcome(narration)
    commits_added = 0
    if new_sha != start_sha:
        rev_count = run_git(
            ["rev-list", "--count", f"{start_sha}..{new_sha}"],
            repo_path, check=False,
        )
        try:
            commits_added = int((rev_count.stdout or "0").strip())
        except ValueError:
            commits_added = 0

    marker = {
        "DONE": "[green]✓[/green]",
        "PARTIAL": "[yellow]◐[/yellow]",
        "UNRELATED": "[yellow]→[/yellow]",
        "UNRESOLVED": "[red]✗[/red]",
    }.get(token, "[red]✗[/red]")
    console.print(
        f"    {marker} {token}"
        + (f" [dim]+{commits_added} commit(s)[/dim]" if commits_added else "")
        + (f" [dim](cost ${cost:.4f})[/dim]" if cost else "")
    )
    return ShardOutcome(
        category=category, shard_context=shard_ctx,
        target_url=target_url, test_count=test_count,
        classification=token, summary=summary, narration=narration,
        cost_usd=cost, commits_added=commits_added,
    ), new_sha, False


def _audit_shard_outcome(
    config: Config,
    repo_path: Path,
    result: PRRunResult,
    outcome: ShardOutcome,
    *,
    pr_url: str,
    pr_number: int,
    pr_branch: str,
    base_branch: str,
    category: str,
    tests: list[FailedTest],
    baseline: BaselineRun | None,
    flaky_map: dict[str, list[str]],
    commit_range: str,
) -> None:
    """Second opinion on one shard's outcome, when the shard is in doubt.

    Advisory throughout: findings are recorded on ``outcome`` and drive
    the PR comment and label. Nothing is reverted, and the push is not
    blocked — the operator decides what to do with a dispute. A failed
    audit (timeout, unparsable verdict) is likewise never fatal; it
    just leaves ``verify_verdict`` at ``"unknown"``.
    """
    if not config.analyze_fails.verify_outcome:
        return
    reason = _verification_reason(
        outcome.classification, outcome.commits_added, tests, baseline,
        flaky_map, config.analyze_fails.flaky_elsewhere_threshold, pr_url,
    )
    if reason is None:
        return
    outcome.verify_reason = reason
    head = run_git(["rev-parse", "--verify", "HEAD"], repo_path, check=False)
    head_before = head.stdout.strip() if head.returncode == 0 else ""

    console.print(
        f"    [magenta]🔎 second opinion[/magenta] [dim]({reason})[/dim]"
    )
    try:
        prompt = _render_verify_prompt(
            config, repo_path, pr_url, pr_number, pr_branch, base_branch,
            outcome.shard_context, outcome.target_url, category, tests,
            baseline, outcome, commit_range,
        )
    except FileNotFoundError as exc:
        outcome.verify_verdict = "unknown"
        result.warnings.append(f"audit skipped: {exc}")
        console.print(f"      [yellow]![/yellow] {exc}")
        return

    exit_code, output, timed_out, cost = _invoke_verifier(
        config, repo_path, prompt,
    )
    if cost:
        result.cost_usd += cost
        outcome.cost_usd = (outcome.cost_usd or 0.0) + cost
    result.shards_audited += 1

    if timed_out or exit_code != 0:
        outcome.verify_verdict = "unknown"
        why = (
            f"timed out after {config.analyze_fails.verify_timeout_seconds}s"
            if timed_out else f"exited {exit_code}"
        )
        result.warnings.append(
            f"{outcome.shard_context}: second opinion {why} — the "
            "first session's verdict stands unaudited."
        )
        console.print(f"      [yellow]![/yellow] audit {why}")
        return

    # The auditor is told it is read-only, and its allowlist has no
    # editing tools — but `Bash(git:*)` is broad enough to commit, so
    # confirm rather than assume. A verifier that wrote to the repo has
    # invalidated what we were about to push.
    after = run_git(["rev-parse", "--verify", "HEAD"], repo_path, check=False)
    moved = after.returncode != 0 or after.stdout.strip() != head_before
    if moved or _verify_post_run_cleanliness(repo_path):
        result.audit_mutated_repo = True
        msg = (
            f"{outcome.shard_context}: the read-only auditor modified the "
            "repository — nothing will be pushed for this PR. Inspect the "
            "work-dir by hand."
        )
        result.warnings.append(msg)
        console.print(f"      [red]✗[/red] {msg}")

    verdict, summary, findings = _parse_verify_output(output)
    outcome.verify_verdict = verdict
    outcome.verify_summary = summary
    outcome.verify_findings = findings

    if verdict == "needs_attention":
        result.shards_disputed += 1
        console.print(
            f"      [yellow]⚠ disputed[/yellow] {summary or '(no summary)'}"
        )
        for f in findings[:5]:
            console.print(f"        [dim]- {f}[/dim]")
    elif verdict == "ok":
        console.print("      [green]✓[/green] audit agrees")
    else:
        result.warnings.append(
            f"{outcome.shard_context}: second opinion returned no "
            "parsable verdict — treated as unaudited."
        )
        console.print("      [yellow]![/yellow] audit gave no verdict")


def _resolve_baseline(
    config: Config,
    origin_slug: str,
    base_ref: str,
    head_sha: str,
    needed_categories: frozenset[str],
) -> tuple[BaselineRun | None, str | None]:
    """Fetch the pre-change CI run for this PR, or explain why not.

    ``needed_categories`` are the check families the PR actually failed
    in — a run that never exercised them is skipped in favour of an
    older one that did.

    Never fatal: a PR whose target branch has no reachable run is
    triaged the old way, from the diff and the flaky-elsewhere map.
    """
    if not config.analyze_fails.baseline_check:
        return None, "disabled (analyze_fails.baseline_check)"
    owner, _, repo = origin_slug.partition("/")
    if not owner or not repo:
        return None, f"cannot parse origin slug {origin_slug!r}"

    categories = _configured_categories(config)
    job_level = config.analyze_fails.job_level_failures
    mb, err = merge_base_sha(owner, repo, base_ref, head_sha)
    if err or not mb:
        return None, err or "no merge base"

    key = (
        origin_slug.lower(), mb, categories, job_level, needed_categories,
    )
    if key not in _BASELINE_CACHE:
        _BASELINE_CACHE[key] = baseline_run_before(
            owner, repo, mb,
            max_commits=config.analyze_fails.baseline_scan_commits,
            categories=categories, job_level=job_level,
            exclude_sha=head_sha,
            require_categories=needed_categories or None,
        )
    return _BASELINE_CACHE[key]


def _process_pr(
    config: Config,
    repo_path: Path,
    pr_url: str,
    flaky_map: dict[str, list[str]],
    *,
    push: bool,
    dry_run: bool,
) -> PRRunResult:
    """Drive the per-shard Claude loop + push for ONE PR."""
    # Authoritative open-state check. analyze-fails pushes commits to
    # the PR's head branch; doing that on a merged or closed PR is
    # either pointless (merged — branch is no longer the source of
    # truth) or harmful (closed — the author already decided not to
    # land it). Skip with a one-line explanation so the operator sees
    # which PRs were excluded.
    pr_info = fetch_pr_by_url(config, pr_url, include_closed=True)
    if pr_info is None:
        return PRRunResult(
            pr_url=pr_url, head_sha="", head_ref="",
            error=(
                "Could not fetch PR metadata — check the URL and "
                "RELEASY_GITHUB_TOKEN."
            ),
        )
    if pr_info.state != "open":
        console.print(
            f"  [dim]{pr_url}: {pr_info.state} — skipping "
            "(analyze-fails only operates on open PRs)[/dim]"
        )
        return PRRunResult(
            pr_url=pr_url, head_sha=pr_info.head_sha, head_ref="",
        )

    head = _fetch_pr_meta(pr_url)
    if head is None:
        return PRRunResult(
            pr_url=pr_url, head_sha="", head_ref="",
            error="Could not look up PR head/base — token / URL?",
        )
    head_ref, head_repo, base_ref, head_sha, pr_number = head

    origin_slug = get_origin_repo_slug(config) or ""
    if head_repo.lower() != origin_slug.lower():
        return PRRunResult(
            pr_url=pr_url, head_sha=head_sha, head_ref=head_ref,
            error=(
                f"PR head branch lives on {head_repo}, but RelEasy only "
                f"pushes to origin ({origin_slug}). Skipping."
            ),
        )

    failures, err = discover_pr_failures(
        config, pr_url,
        head_sha=head_sha, head_ref=head_ref, base_ref=base_ref,
        categories=_configured_categories(config),
        job_level=config.analyze_fails.job_level_failures,
    )
    if err or failures is None:
        return PRRunResult(
            pr_url=pr_url, head_sha=head_sha, head_ref=head_ref,
            error=err or "discover_pr_failures returned no data",
        )

    result = PRRunResult(
        pr_url=pr_url, head_sha=head_sha, head_ref=head_ref,
        statuses_failed=len(failures.statuses),
        tests_total=len(failures.failed_tests),
        warnings=list(failures.skipped_status_warnings),
        covered_elsewhere=list(failures.covered_elsewhere),
    )

    if not failures.failed_tests:
        console.print(
            f"  [green]✓[/green] {pr_url}: no parsed-report test "
            "failures to act on."
        )
        return result

    shards = _group_failures_by_shard(failures.failed_tests)
    result.shards_total = len(shards)

    console.print(
        f"\n[bold]{pr_url}[/bold] — "
        f"{len(failures.failed_tests)} failing test(s) across "
        f"{len(shards)} shard(s) "
        f"[dim](from {len(failures.statuses)} failed check(s))[/dim]"
    )

    baseline, baseline_note = _resolve_baseline(
        config, origin_slug, base_ref, head_sha,
        frozenset(t.category for t in failures.failed_tests),
    )
    if baseline is not None:
        result.baseline_sha = baseline.sha
        result.baseline_committed_at = baseline.committed_at
        result.tests_pre_existing = sum(
            1 for t in failures.failed_tests
            if baseline.verdict_for(t.category, t.name) == "failed"
        )
        console.print(
            f"  [dim]baseline {baseline.sha[:10]} "
            f"({baseline.committed_at}) on {base_ref}: "
            f"{baseline.checks_failed}/{baseline.checks_total} check(s) "
            f"already red[/dim] — [bold]{result.tests_pre_existing} of "
            f"{len(failures.failed_tests)} failure(s) predate this "
            "PR[/bold]"
        )
    elif baseline_note:
        result.baseline_note = baseline_note
        # An operator who turned the pass off doesn't need warning about
        # it on every PR.
        if config.analyze_fails.baseline_check:
            console.print(f"  [yellow]![/yellow] baseline: {baseline_note}")
        else:
            console.print(f"  [dim]baseline: {baseline_note}[/dim]")
    for w in result.warnings:
        console.print(f"  [yellow]![/yellow] {w}")
    for c in result.covered_elsewhere:
        console.print(f"  [dim]≡ {c}[/dim]")

    if dry_run:
        for category, shard_ctx, _, tests in shards:
            flaky_count = sum(
                1 for t in tests
                if [u for u in flaky_map.get(
                    _flaky_key(t.category, t.name), [],
                ) if u != pr_url]
            )
            fresh = (
                sum(
                    1 for t in tests
                    if baseline.verdict_for(t.category, t.name) != "failed"
                ) if baseline is not None else None
            )
            console.print(
                f"  [cyan]{category}[/cyan] {shard_ctx}: "
                f"{len(tests)} test(s)"
                + (
                    f" [green]({fresh} new since baseline)[/green]"
                    if fresh else
                    " [dim](all predate this PR)[/dim]"
                    if fresh == 0 else ""
                )
                + (
                    f" [yellow]({flaky_count} also fail elsewhere)[/yellow]"
                    if flaky_count else ""
                )
            )
        result.shards_processed = len(shards)
        return result

    ok, start_sha, cerr = _checkout_pr_head(config, repo_path, head_ref)
    if not ok or start_sha is None:
        result.error = cerr
        return result

    try:
        _write_build_script(repo_path, config.ai_resolve.build_command)
    except OSError as exc:
        result.error = f"Could not write build wrapper: {exc}"
        return result

    _api, backend_error = _resolve_backend(
        config, config.analyze_fails.command,
        list(config.analyze_fails.allowed_tools),
    )
    if backend_error:
        result.error = (
            f"{backend_error} — install Claude Code, adjust "
            "analyze_fails.command, or fix the ai_api settings."
        )
        return result

    branch_starting_sha = start_sha

    for shard_idx, (category, shard_ctx, target_url, tests) in enumerate(
        shards, start=1,
    ):
        console.print(
            f"\n  [magenta]→ shard {shard_idx}/{len(shards)}[/magenta] "
            f"[cyan]{category}[/cyan] {shard_ctx} "
            f"[dim]({len(tests)} test(s))[/dim]"
        )

        try:
            _write_failed_tests_manifest(repo_path, tests)
        except OSError as exc:
            result.error = f"Could not stage shard manifest: {exc}"
            return result

        # Investigate, audit, and — when the audit disputes what came
        # out — hand the findings to a fresh investigator and try once
        # more. Bounded by max_investigation_rounds; each round starts
        # from the previous round's tip, so a redo can revert what the
        # audit objected to.
        redo: RedoContext | None = None
        outcome: ShardOutcome | None = None
        unsafe = False
        for round_index in range(
            1, max(1, config.analyze_fails.max_investigation_rounds) + 1,
        ):
            if round_index > 1:
                console.print(
                    f"    [magenta]↻ round {round_index}[/magenta] "
                    "[dim](re-investigating with the audit's "
                    "findings)[/dim]"
                )
            try:
                prompt = _render_shard_prompt(
                    config, repo_path, pr_url, pr_number,
                    head_ref, base_ref, shard_ctx, target_url, category,
                    tests, flaky_map, baseline, baseline_note, redo,
                )
            except FileNotFoundError as exc:
                result.error = str(exc)
                return result

            outcome, new_sha, unsafe = _run_investigation_round(
                config, repo_path, prompt, start_sha,
                category=category, shard_ctx=shard_ctx,
                target_url=target_url, test_count=len(tests),
            )
            outcome.round_index = round_index
            result.outcomes.append(outcome)
            if unsafe:
                break

            if new_sha != start_sha and outcome.classification == "UNRELATED":
                result.warnings.append(
                    f"{shard_ctx}: AI declared UNRELATED but moved HEAD "
                    f"from {start_sha[:10]} to {new_sha[:10]}; commits "
                    "kept locally."
                )
            # Walk the linear-history baseline forward: later rounds and
            # later shards baseline against the now-extended tip.
            round_start_sha, start_sha = start_sha, new_sha

            _audit_shard_outcome(
                config, repo_path, result, outcome,
                pr_url=pr_url, pr_number=pr_number, pr_branch=head_ref,
                base_branch=base_ref, category=category, tests=tests,
                baseline=baseline, flaky_map=flaky_map,
                commit_range=(
                    f"{round_start_sha}..{new_sha}"
                    if new_sha != round_start_sha else ""
                ),
            )
            if not outcome.disputed:
                break
            if round_index >= config.analyze_fails.max_investigation_rounds:
                result.warnings.append(
                    f"{shard_ctx}: still disputed after {round_index} "
                    "round(s) — left for a human."
                )
                break
            if result.audit_mutated_repo:
                # The work-dir is no longer trustworthy; a redo would
                # build on it.
                break
            outcome.superseded = True
            redo = RedoContext(
                round_index=round_index,
                classification=outcome.classification,
                commits_added=outcome.commits_added,
                commit_range=(
                    f"{round_start_sha}..{new_sha}"
                    if new_sha != round_start_sha else ""
                ),
                audit_summary=outcome.verify_summary,
                audit_findings=list(outcome.verify_findings),
            )

        if outcome is not None:
            result.shards_processed += 1
            _tally_outcome(result, outcome)
        if unsafe:
            # Stop — local branch state is unsafe for further shards.
            break

    final_head = run_git(
        ["rev-parse", "--verify", "HEAD"], repo_path, check=False,
    )
    if final_head.returncode == 0:
        rev_count = run_git(
            ["rev-list", "--count",
             f"{branch_starting_sha}..{final_head.stdout.strip()}"],
            repo_path, check=False,
        )
        try:
            result.commits_added = int((rev_count.stdout or "0").strip())
        except ValueError:
            result.commits_added = 0

    if result.commits_added > 0 and result.audit_mutated_repo:
        result.error = (
            "the read-only auditor modified the work-dir, so the "
            f"{result.commits_added} new commit(s) on {head_ref} are not "
            "what was audited — refusing to push. Inspect the work-dir."
        )
        console.print(f"  [red]✗[/red] {result.error}")
    elif result.commits_added > 0 and push:
        push_res = run_git(
            ["push", config.origin.remote_name, head_ref],
            repo_path, check=False,
        )
        if push_res.returncode != 0:
            for line in (push_res.stderr or "").strip().splitlines()[:5]:
                console.print(f"      [dim]{line}[/dim]")
            result.error = (
                f"push failed (race? auth?). The {result.commits_added} "
                f"new commit(s) live locally at HEAD on {head_ref}."
            )
        else:
            result.pushed = True
            console.print(
                f"  [green]✓[/green] pushed {result.commits_added} "
                f"new commit(s) to [cyan]{head_ref}[/cyan]"
            )
    elif result.commits_added > 0:
        console.print(
            f"  [yellow]–[/yellow] {result.commits_added} new commit(s) "
            "kept locally (push disabled)"
        )

    return result


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# PR comment formatting + posting
# ---------------------------------------------------------------------------


# Per-shard narration excerpt cap for the PR comment. Long enough to
# carry Claude's investigation summary verbatim (which routinely runs
# 1-3k chars), short enough that a noisy 6-shard PR doesn't blow the
# comment past GitHub's 65k-char limit.
_NARRATION_CAP_PER_SHARD = 6000


def _utc_now_iso() -> str:
    from datetime import datetime, timezone
    return (
        datetime.now(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def _trim_narration_for_comment(narration: str) -> str:
    """Trim a Claude transcript to a comment-safe excerpt.

    Strategy:
      * Strip leading/trailing whitespace.
      * Cap to ``_NARRATION_CAP_PER_SHARD`` chars; when over, keep the
        last 3/4 of the budget (the conclusion is more useful than the
        thinking-out-loud preamble) and prepend an ``…(truncated)``
        marker.
    """
    text = (narration or "").strip()
    if not text:
        return "_(no narration captured)_"
    if len(text) <= _NARRATION_CAP_PER_SHARD:
        return text
    keep = int(_NARRATION_CAP_PER_SHARD * 0.75)
    return f"…(narration truncated; last {keep} chars)\n\n" + text[-keep:]


def _format_pr_comment(run: PRRunResult) -> str:
    """Build the markdown body posted to the PR after a per-PR run."""
    overall = (
        # A shard whose audit still stands rejected is never a clean
        # result, whatever the session concluded.
        "DISPUTED" if run.open_disputes else
        "DONE" if run.shards_unresolved == 0 and run.shards_partial == 0
        and run.shards_done > 0 and run.shards_processed > 0 else
        "PARTIAL" if run.shards_done > 0 or run.shards_partial > 0 else
        "UNRELATED" if run.shards_unrelated == run.shards_processed
        and run.shards_processed > 0 else
        "UNRESOLVED"
    )
    pushed_note = (
        "✅ pushed" if run.pushed
        else "⚠️ NOT pushed" if run.commits_added > 0
        else "—"
    )
    cost_note = f"${run.cost_usd:.4f}" if run.cost_usd else "—"

    lines = [
        f"## RelEasy `analyze-fails` — {overall}",
        "",
        f"_run completed at {_utc_now_iso()}_",
        "",
        f"- **Head SHA:** `{run.head_sha[:10]}` (`{run.head_ref}`)",
        f"- **Failed CI checks:** {run.statuses_failed}",
        f"- **Tests considered:** {run.tests_total} across "
        f"{run.shards_total} CI shard(s)",
        (
            f"- **Baseline (`{run.baseline_sha[:10]}`, "
            f"{run.baseline_committed_at}, before this PR):** "
            f"{run.tests_pre_existing} of {run.tests_total} failure(s) "
            "were already red there"
            if run.baseline_sha else
            f"- **Baseline:** none available — {run.baseline_note}"
            if run.baseline_note else
            "- **Baseline:** none available"
        ),
        f"- **Outcomes:** "
        f"{run.shards_done} done · "
        f"{run.shards_partial} partial · "
        f"{run.shards_unrelated} unrelated · "
        f"{run.shards_unresolved} unresolved",
        (
            f"- **Second opinion:** {run.shards_audited} audit(s) by an "
            "independent session"
            + (
                f"; {run.shards_disputed - run.open_disputes} dispute(s) "
                "sent back for re-investigation and settled"
                if run.shards_disputed > run.open_disputes else ""
            )
            + (
                f"; **{run.open_disputes} still disputed** — read the "
                "findings below before trusting this run."
                if run.open_disputes else "; nothing left disputed."
            )
            if run.shards_audited else
            "- **Second opinion:** no shard was in doubt (nothing "
            "committed, and the evidence backs each verdict)."
        ),
        f"- **Commits added by AI:** {run.commits_added} ({pushed_note})",
        f"- **Anthropic cost:** {cost_note}",
    ]
    if run.error:
        lines.append(f"- **Error:** {run.error}")
    if run.warnings:
        lines.append("- **Warnings:**")
        for w in run.warnings[:5]:
            lines.append(f"  - {w}")
        if len(run.warnings) > 5:
            lines.append(f"  - …(+{len(run.warnings) - 5} more)")
    if run.covered_elsewhere:
        lines.append(
            "- **Checks covered by another shard** (same failures, "
            "investigated once):"
        )
        for c in run.covered_elsewhere[:10]:
            lines.append(f"  - {c}")
        if len(run.covered_elsewhere) > 10:
            lines.append(
                f"  - …(+{len(run.covered_elsewhere) - 10} more)"
            )
    lines.append("")

    if not run.outcomes:
        lines.append(
            "_No CI shards had parsed-report failures to act on._"
        )
        lines.append("")
    else:
        lines.append("## Per-shard outcomes")
        lines.append("")
        for o in run.outcomes:
            badge = {
                "DONE": "✅ DONE",
                "PARTIAL": "🟡 PARTIAL",
                "UNRELATED": "⏭️ UNRELATED",
                "UNRESOLVED": "❌ UNRESOLVED",
            }.get(o.classification, f"❓ {o.classification}")
            if o.disputed:
                badge = f"⚠️ {o.classification} — DISPUTED"
            if o.superseded:
                badge = f"↻ {o.classification} — REDONE"
            commit_note = (
                f" — **+{o.commits_added} commit(s)**"
                if o.commits_added else ""
            )
            cost_per = (
                f" — cost ${o.cost_usd:.4f}"
                if o.cost_usd else ""
            )
            round_note = (
                f" (round {o.round_index})" if o.round_index > 1
                or o.superseded else ""
            )
            lines.append(
                f"### {badge} — `{o.shard_context}`{round_note}"
            )
            lines.append("")
            if o.superseded:
                lines.append(
                    "_Superseded: the audit rejected this round, and a "
                    "fresh investigator was given its findings. The "
                    "shard's verdict is the round below._"
                )
                lines.append("")
            lines.append(
                f"_{o.test_count} failed test(s) considered{commit_note}{cost_per}_"
            )
            lines.append(
                f"[full report]({o.target_url})"
            )
            lines.append("")
            if o.verify_reason:
                verdict_note = {
                    "needs_attention": "⚠️ **disputes it**",
                    "ok": "✅ agrees",
                }.get(
                    o.verify_verdict or "",
                    "❓ reached no verdict (advisory only)",
                )
                lines.append(
                    f"**Second opinion** — audited because "
                    f"{o.verify_reason}; an independent session "
                    f"{verdict_note}."
                )
                lines.append("")
                if o.verify_summary:
                    lines.append(f"> {o.verify_summary}")
                    lines.append("")
                for f in o.verify_findings[:10]:
                    lines.append(f"- {f}")
                if len(o.verify_findings) > 10:
                    lines.append(
                        f"- …(+{len(o.verify_findings) - 10} more)"
                    )
                if o.verify_findings:
                    lines.append("")
            lines.append("<details><summary>AI narration</summary>")
            lines.append("")
            lines.append(_trim_narration_for_comment(o.narration))
            lines.append("")
            lines.append("</details>")
            lines.append("")

    lines.append("---")
    lines.append(
        "🤖 *Posted automatically by `releasy analyze-fails`. "
        "Re-run the command to refresh.*"
    )
    return "\n".join(lines)


def _attribute_cost_to_feature(
    state: PipelineState | None,
    pr_url: str,
    cost_usd: float,
) -> str | None:
    """Add ``cost_usd`` to the matching feature's ``ai_cost_usd`` total.

    Returns the feature id when a match was found and updated, ``None``
    otherwise. Mirrors the ``refresh`` flow's accumulation pattern:

        prior = fs.ai_cost_usd or 0.0
        fs.ai_cost_usd = prior + cost_usd

    so the GitHub Project board's "AI Cost" column shows the
    cumulative spend across cherry-pick resolution, refresh-merge
    resolution, AND analyze-fails investigation on this same feature.
    No-op when state is missing, the PR isn't tracked, or the run
    incurred zero cost.
    """
    if state is None or cost_usd <= 0:
        return None
    parsed = parse_pr_url(pr_url)
    if parsed is None:
        return None
    target = (parsed[0].lower(), parsed[1].lower(), parsed[2])
    for fid, fs in state.features.items():
        if not fs.rebase_pr_url:
            continue
        other = parse_pr_url(fs.rebase_pr_url)
        if other is None:
            continue
        if (other[0].lower(), other[1].lower(), other[2]) == target:
            fs.ai_cost_usd = (fs.ai_cost_usd or 0.0) + cost_usd
            return fid
    return None


def _apply_verify_label(config: Config, pr_url: str) -> None:
    """Best-effort ``verify_label`` on a PR whose audit found something."""
    parsed = parse_pr_url(pr_url)
    if parsed is None:
        return
    label = config.analyze_fails.verify_label
    ensure_label(
        config, label, config.analyze_fails.verify_label_color,
        "An independent audit disputed the AI's CI-failure analysis",
    )
    if add_label_to_pr(config, parsed[2], label):
        console.print(
            f"  [yellow]🔎[/yellow] labelled PR [yellow]{label}[/yellow] "
            "[dim](second opinion disputed a shard)[/dim]"
        )


def _post_pr_comment(
    pr_url: str, body: str,
) -> tuple[str | None, str | None]:
    """POST a top-level comment to ``pr_url``. Returns ``(comment_url, error)``.

    Best-effort: any GitHub API error is captured and returned without
    raising, so a comment-posting failure never breaks the
    investigation flow that's already done its real work.
    """
    token = get_github_token()
    if not token:
        return None, "RELEASY_GITHUB_TOKEN not set — cannot post comment"
    parsed = parse_pr_url(pr_url)
    if parsed is None:
        return None, f"Could not parse PR URL: {pr_url!r}"
    owner, repo, number = parsed
    try:
        from github import Github

        gh = Github(token)
        ghrepo = gh.get_repo(f"{owner}/{repo}")
        pr = ghrepo.get_pull(number)
        # PR-level comments live on the issue endpoint (top-level
        # comments, not inline review comments).
        ic = pr.create_issue_comment(body)
        return ic.html_url, None
    except Exception as exc:
        return None, f"create_issue_comment failed: {exc}"


def run_analyze_fails_pass(
    config: Config,
    state: PipelineState | None,
    repo_path: Path,
    pr_urls: list[str],
    *,
    push: bool,
    dry_run: bool,
    no_flaky_check: bool,
    post_comment: bool | None,
    flaky_scan_extra: list[str] | None = None,
) -> tuple[list[PRRunResult], dict[str, list[str]], list[str], bool]:
    """Drive analyze-fails over an explicit list of PR URLs.

    Caller is responsible for: project lock, state load, ``_setup_repo``,
    in-progress-op guard, and (later) state persistence + project sync.
    Designed so :func:`refresh.refresh_tracked_prs` can fold an
    analyze-fails phase into a single locked refresh invocation without
    re-doing any of that bookkeeping.

    Cost is accumulated onto the matching :class:`FeatureState`'s
    ``ai_cost_usd`` in-place — the caller decides when to persist /
    sync the project board (refresh already does this at the end of
    its own pass).

    ``flaky_scan_extra`` is used by single-PR callers (e.g. ``refresh
    --pr <url> --analyze-fails``) to widen the flaky-elsewhere scan to
    cover every other tracked PR rather than just the one being
    analysed. Pass ``None`` to scan the primary list itself.

    Returns ``(runs, flaky_map, flaky_warnings, cost_attributed_any)``.
    """
    effective_post_comment = (
        config.analyze_fails.post_comment_to_pr
        if post_comment is None else post_comment
    )

    flaky_map: dict[str, list[str]] = {}
    flaky_warnings: list[str] = []
    if not no_flaky_check:
        if flaky_scan_extra is not None:
            scan = list(flaky_scan_extra)
        else:
            scan = list(pr_urls)
        if scan:
            cap = config.analyze_fails.flaky_check_prs
            scanned = min(len(scan), cap) if cap > 0 else len(scan)
            console.print(
                f"\n[dim]Building flaky-elsewhere map across "
                f"{scanned} tracked PR(s)…[/dim]"
            )
            flaky_map, flaky_warnings = _build_flaky_elsewhere_map(
                config, scan,
            )
            console.print(
                f"[dim]flaky map: {len(flaky_map)} test(s) seen failing "
                "elsewhere[/dim]"
            )
        else:
            console.print(
                "[dim]No other tracked PRs — skipping flaky-elsewhere "
                "assessment.[/dim]"
            )

    runs: list[PRRunResult] = []
    cost_attributed_any = False
    for url in pr_urls:
        run = _process_pr(
            config, repo_path, url, flaky_map,
            push=push and not dry_run, dry_run=dry_run,
        )
        runs.append(run)
        if run.open_disputes and not dry_run:
            _apply_verify_label(config, run.pr_url)
        # Accumulate cost on the matching FeatureState so the next
        # state-save (whoever's driving us) reflects the spend. No-op
        # for stateless / dry-run / unmatched PRs.
        if not dry_run and run.cost_usd:
            fid = _attribute_cost_to_feature(state, run.pr_url, run.cost_usd)
            if fid:
                cost_attributed_any = True
                console.print(
                    f"  [dim]+${run.cost_usd:.4f} → feature "
                    f"{fid} ai_cost_usd[/dim]"
                )
        if (
            effective_post_comment
            and not dry_run
            and run.outcomes  # something was processed
        ):
            curl, cerr = _post_pr_comment(
                run.pr_url, _format_pr_comment(run),
            )
            if curl:
                run.comment_url = curl
                console.print(
                    f"  [green]✓[/green] posted summary comment: "
                    f"[cyan]{curl}[/cyan]"
                )
            elif cerr:
                console.print(
                    f"  [yellow]![/yellow] could not post PR comment: "
                    f"{cerr}"
                )

    if flaky_warnings:
        console.print()
        for w in flaky_warnings:
            console.print(f"[dim]{w}[/dim]")

    return runs, flaky_map, flaky_warnings, cost_attributed_any


def analyze_fails(
    config: Config,
    *,
    pr_url: str | None = None,
    work_dir: Path | None = None,
    dry_run: bool = False,
    push: bool = True,
    no_flaky_check: bool = False,
    post_comment: bool | None = None,
    only: OnlyFilter | None = None,
) -> AnalyzeFailsResult:
    """Drive one ``releasy analyze-fails`` run end-to-end.

    ``only`` (optional) restricts the multi-PR walk to a single tracked
    feature (matched by URL or feature / group ID). Mutually exclusive
    with ``pr_url`` at the CLI layer.

    This is the top-level entry point used by the standalone
    ``releasy analyze-fails`` command. ``refresh --analyze-fails`` calls
    :func:`run_analyze_fails_pass` directly instead — it already holds
    the project lock, has state loaded, and the repo prepared.
    """
    if not get_origin_repo_slug(config):
        return AnalyzeFailsResult(
            success=False,
            error=(
                "Cannot determine origin repo slug from config — check "
                f"origin.remote ({config.origin.remote!r})."
            ),
        )

    if not get_github_token():
        return AnalyzeFailsResult(
            success=False,
            error=(
                "RELEASY_GITHUB_TOKEN is not set. analyze-fails needs "
                "it to look up PR head metadata and fetch CI statuses."
            ),
        )

    state: PipelineState | None = None
    if not is_stateless(config):
        try:
            state = load_state(config)
        except Exception as exc:
            console.print(
                f"[yellow]![/yellow] state file unreadable ({exc}); "
                "running without flaky-elsewhere assessment"
            )
            state = None

    if pr_url:
        primary_pr_urls = [pr_url]
    else:
        # Refresh local merge status from GitHub so the tracked_pr_urls
        # prefilter doesn't queue a PR that's already been merged
        # externally. Cheap (parallel GETs); skipped on dry-run / no
        # state since there's nothing to update.
        if state is not None and not dry_run:
            from releasy.pipeline import _refresh_all_merge_status_from_github
            _refresh_all_merge_status_from_github(config, state)
        primary_pr_urls = tracked_pr_urls(state, only=only)
        if not primary_pr_urls:
            if only is not None:
                return AnalyzeFailsResult(
                    success=False,
                    error=(
                        f"--only={only.label!r} matched no tracked PRs. "
                        "Check the URL / group id and re-run."
                    ),
                )
            return AnalyzeFailsResult(
                success=False,
                error=(
                    "No --pr given and no tracked PRs in state. Pass "
                    "--pr <URL> or run inside a project that has at "
                    "least one rebase PR opened."
                ),
            )
        cap = config.analyze_fails.max_prs_per_run
        if cap > 0 and len(primary_pr_urls) > cap:
            primary_pr_urls = primary_pr_urls[:cap]

    flaky_scan_extra: list[str] | None = None
    if pr_url or only is not None:
        flaky_scan_extra = flaky_scan_extra_for(state, primary_pr_urls)

    initial_base = None
    if primary_pr_urls:
        first_meta = _fetch_pr_meta(primary_pr_urls[0])
        if first_meta is None:
            return AnalyzeFailsResult(
                success=False,
                error=(
                    f"Could not look up PR head metadata for "
                    f"{primary_pr_urls[0]} — check the URL and "
                    "RELEASY_GITHUB_TOKEN."
                ),
            )
        initial_base = first_meta[2]

    from releasy.pipeline import _setup_repo

    repo_path = _setup_repo(config, work_dir, initial_base)

    if is_operation_in_progress(repo_path):
        return AnalyzeFailsResult(
            success=False,
            error=(
                f"A git operation (cherry-pick/merge/rebase) is already "
                f"in progress in {repo_path}. Resolve or abort it before "
                "running analyze-fails."
            ),
        )

    runs, flaky_map, _flaky_warnings, cost_attributed_any = (
        run_analyze_fails_pass(
            config, state, repo_path, primary_pr_urls,
            push=push, dry_run=dry_run,
            no_flaky_check=no_flaky_check,
            post_comment=post_comment,
            flaky_scan_extra=flaky_scan_extra,
        )
    )

    print_summary(runs)

    if state is not None:
        try:
            save_state(state, config)
        except Exception as exc:
            console.print(
                f"[yellow]![/yellow] failed to persist state: {exc}"
            )
        # Push the freshly accumulated AI cost(s) to the GitHub Project
        # board. Same trigger as ``releasy refresh``: only when ``push``
        # is on (the project sync is otherwise off-policy) and at least
        # one PR's cost actually landed in state. No-op gracefully if
        # the project isn't configured / token lacks the scope — the
        # state file already has the right value, so the next
        # ``releasy project push`` will catch up.
        if cost_attributed_any and config.push:
            try:
                from releasy.github_ops import sync_project
                console.print(
                    "[dim]Syncing AI cost to GitHub Project board…[/dim]"
                )
                sync_project(config, state)
            except Exception as exc:
                console.print(
                    f"[yellow]![/yellow] project sync failed: {exc} "
                    "(state file is still up to date — re-sync with "
                    "`releasy project push` when convenient)"
                )

    success = all(r.error is None for r in runs)
    return AnalyzeFailsResult(
        success=success,
        runs=runs,
        flaky_elsewhere_map=flaky_map,
    )


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------


def print_summary(runs: list[PRRunResult]) -> None:
    if not runs:
        return
    console.print("\n[bold]Summary:[/bold]")
    overall_cost = 0.0
    for r in runs:
        overall_cost += r.cost_usd
        if r.commits_added > 0 and r.pushed:
            commit_state = (
                f"[green]committed +{r.commits_added}, pushed[/green]"
            )
        elif r.commits_added > 0:
            commit_state = (
                f"[yellow]committed +{r.commits_added}, NOT pushed[/yellow]"
            )
        else:
            commit_state = "[dim]no code changes[/dim]"
        console.print(
            f"  [cyan]{r.pr_url}[/cyan] — "
            f"{r.tests_total} test(s) / {r.shards_total} shard(s): "
            f"done {r.shards_done}, partial {r.shards_partial}, "
            f"unrelated {r.shards_unrelated}, "
            f"unresolved {r.shards_unresolved} — {commit_state}"
            + (f" [dim]${r.cost_usd:.4f}[/dim]" if r.cost_usd else "")
        )
        if r.comment_url:
            console.print(f"    [dim]comment:[/dim] {r.comment_url}")
        if r.error:
            console.print(f"    [red]error:[/red] {r.error}")
    if overall_cost:
        console.print(
            f"\n[dim]Total Anthropic cost across this run: "
            f"${overall_cost:.4f}[/dim]"
        )
