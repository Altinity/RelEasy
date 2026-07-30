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
from releasy.ci_failures import (
    FailedTest,
    PRFailures,
    discover_pr_failures,
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
    commits_added: int = 0
    pushed: bool = False
    cost_usd: float = 0.0
    comment_url: str | None = None
    outcomes: list[ShardOutcome] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
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
        if fs.status in ("merged", "skipped", "closed", "superseded"):
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

    for url in pr_urls:
        failures, err = discover_pr_failures(config, url)
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
#   - Stateless / Integration tests hit real storage, docker, network
#     — genuine flakes are common and flaky-elsewhere annotations are
#     load-bearing UNRELATED evidence.
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
}


def _category_prior_section(category: str) -> str:
    """Render the category-specific scoping prior, or a no-op fallback."""
    prior = _CATEGORY_PRIORS.get(category)
    if prior is None:
        return (
            f"_(no category-specific prior for {category!r}; apply the "
            "generic scoping rule above as-is.)_"
        )
    return prior


# Each entry is a small markdown block telling Claude how to invoke the
# right test runner for the category. ``{tests_arg}`` is substituted
# with a space-separated quoted list of the failing test names; Claude
# is told that this is the ground truth for "what was failing" and that
# it must re-invoke the runner with a (possibly shrinking) subset on
# every iteration of the fix-build-rerun loop.
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
}


def _quote_for_shell(name: str) -> str:
    """Single-quote ``name`` for safe inclusion in a shell command line."""
    if not name:
        return "''"
    if all(c.isalnum() or c in "_-./:[]=+@" for c in name):
        return name
    return "'" + name.replace("'", "'\\''") + "'"


def _category_runner_section(
    category: str, test_names: list[str], shard_context: str,
    *, max_inline: int = 25,
) -> str:
    template = _CATEGORY_RUNNER_HINTS.get(category)
    if template is None:
        return (
            f"_(no per-category runner hint for category {category!r}; "
            "find the right invocation under `ci/jobs/` or the "
            "equivalent docs in the repo.)_"
        )
    if len(test_names) <= max_inline:
        tests_arg = " ".join(_quote_for_shell(n) for n in test_names)
    else:
        # Too many to fit on one shell command line cleanly; tell
        # Claude to use a temp file. Inline the first few as a teaser
        # so the prompt reads sensibly without the file detour.
        head = " ".join(_quote_for_shell(n) for n in test_names[:max_inline])
        tests_arg = (
            f"$(cat .releasy/failed-tests.txt)  # the full list lives "
            "in `.releasy/failed-tests.txt` (one test name per line); "
            f"the first {max_inline} are: {head}"
        )
    return (
        template
        .replace("{tests_arg}", tests_arg)
        .replace("{shard_context}", shard_context)
    )


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


def _render_failure_block(
    test: FailedTest,
    index: int,
    flaky_map: dict[str, list[str]],
    threshold: int,
    current_pr_url: str,
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

    lines = [
        f"### {index}. `{test.name}`  ({test.status})",
        "",
        f"- {flaky}",
        "",
    ]
    if info:
        lines.append(f"---BEGIN FAILURE EXCERPT #{index}---")
        lines.append(info)
        lines.append(f"---END FAILURE EXCERPT #{index}---")
    else:
        lines.append("_(no per-test info captured by the praktika report)_")
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
        _render_failure_block(t, i + 1, flaky_map, threshold, pr_url)
        for i, t in enumerate(inline)
    )
    if extra:
        blocks += (
            f"\n\n_(+{len(extra)} more failing test(s) in this shard — "
            "the canonical list lives in `.releasy/failed-tests.txt`, "
            "one test name per line. Use that file as the ground truth "
            "for the test arguments in the runner command.)_"
        )

    runner_section = _category_runner_section(
        category, [t.name for t in tests], shard_context,
    )
    category_prior = _category_prior_section(category)

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
    one.
    """
    target = repo_path / ".releasy" / "failed-tests.txt"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        "\n".join(t.name for t in tests) + "\n", encoding="utf-8",
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
    in stable order: fasttest first (single shard, broad blast radius),
    then stateless, then integration, then anything else; within a
    category, alphabetical by shard so the output is reproducible.
    """
    groups: dict[
        tuple[str, str, str], list[FailedTest],
    ] = {}
    for ft in failed_tests:
        key = (ft.category, ft.shard_context, ft.target_url)
        groups.setdefault(key, []).append(ft)

    _category_order = {"fasttest": 0, "stateless": 1, "integration": 2}
    return sorted(
        (
            (cat, ctx, url, tests)
            for (cat, ctx, url), tests in groups.items()
        ),
        key=lambda x: (_category_order.get(x[0], 99), x[1]),
    )


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
        f"{len(shards)} shard(s)"
    )
    for w in result.warnings:
        console.print(f"  [yellow]![/yellow] {w}")

    if dry_run:
        for category, shard_ctx, _, tests in shards:
            flaky_count = sum(
                1 for t in tests
                if [u for u in flaky_map.get(
                    _flaky_key(t.category, t.name), [],
                ) if u != pr_url]
            )
            console.print(
                f"  [cyan]{category}[/cyan] {shard_ctx}: "
                f"{len(tests)} test(s)"
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
            prompt = _render_shard_prompt(
                config, repo_path, pr_url, pr_number,
                head_ref, base_ref, shard_ctx, target_url, category,
                tests, flaky_map,
            )
        except FileNotFoundError as exc:
            result.error = str(exc)
            return result
        except OSError as exc:
            result.error = f"Could not stage shard manifest: {exc}"
            return result

        exit_code, output, timed_out, cost = _invoke_claude(
            config, repo_path, prompt,
        )
        if cost is not None:
            result.cost_usd += cost
        narration = _extract_assistant_text(output)

        if timed_out:
            console.print(
                f"    [red]✗[/red] timed out after "
                f"{config.analyze_fails.timeout_seconds}s"
            )
            result.outcomes.append(ShardOutcome(
                category=category, shard_context=shard_ctx,
                target_url=target_url, test_count=len(tests),
                classification="UNRESOLVED",
                summary="claude timed out",
                narration=narration,
                cost_usd=cost,
            ))
            result.shards_unresolved += 1
            result.shards_processed += 1
            continue

        if exit_code != 0:
            transient = _find_transient_api_error(output)
            console.print(
                f"    [red]✗[/red] claude exited {exit_code}"
                + (f" — transient: {transient}" if transient else "")
            )
            result.outcomes.append(ShardOutcome(
                category=category, shard_context=shard_ctx,
                target_url=target_url, test_count=len(tests),
                classification="UNRESOLVED",
                summary=(narration.strip().splitlines() or ["<empty>"])[-1],
                narration=narration,
                cost_usd=cost,
            ))
            result.shards_unresolved += 1
            result.shards_processed += 1
            continue

        clean_err = _verify_post_run_cleanliness(repo_path)
        if clean_err:
            console.print(f"    [red]✗[/red] {clean_err}")
            result.outcomes.append(ShardOutcome(
                category=category, shard_context=shard_ctx,
                target_url=target_url, test_count=len(tests),
                classification="UNRESOLVED",
                summary=clean_err,
                narration=narration,
                cost_usd=cost,
            ))
            result.shards_unresolved += 1
            result.shards_processed += 1
            continue

        new_head = run_git(
            ["rev-parse", "--verify", "HEAD"], repo_path, check=False,
        )
        if new_head.returncode != 0:
            console.print(
                "    [red]✗[/red] could not read HEAD post-run"
            )
            result.outcomes.append(ShardOutcome(
                category=category, shard_context=shard_ctx,
                target_url=target_url, test_count=len(tests),
                classification="UNRESOLVED",
                summary="git rev-parse HEAD failed",
                narration=narration,
                cost_usd=cost,
            ))
            result.shards_unresolved += 1
            result.shards_processed += 1
            continue
        new_sha = new_head.stdout.strip()

        if new_sha != start_sha:
            ancestor = is_ancestor(repo_path, start_sha, new_sha)
            if ancestor is not True:
                msg = (
                    "non-linear history detected (HEAD is not a "
                    f"descendant of {start_sha[:10]} — refusing to push)"
                )
                console.print(f"    [red]✗[/red] {msg}")
                result.outcomes.append(ShardOutcome(
                    category=category, shard_context=shard_ctx,
                    target_url=target_url, test_count=len(tests),
                    classification="UNRESOLVED",
                    summary=msg,
                    narration=narration,
                    cost_usd=cost,
                ))
                result.shards_unresolved += 1
                result.shards_processed += 1
                # Stop — local branch state is unsafe for further shards.
                break

        token, summary = _classify_outcome(narration)
        commits_added_this_shard = 0
        if new_sha != start_sha:
            rev_count = run_git(
                ["rev-list", "--count", f"{start_sha}..{new_sha}"],
                repo_path, check=False,
            )
            try:
                commits_added_this_shard = int(
                    (rev_count.stdout or "0").strip()
                )
            except ValueError:
                commits_added_this_shard = 0
        result.outcomes.append(ShardOutcome(
            category=category, shard_context=shard_ctx,
            target_url=target_url, test_count=len(tests),
            classification=token, summary=summary,
            narration=narration,
            cost_usd=cost,
            commits_added=commits_added_this_shard,
        ))
        result.shards_processed += 1
        marker = {
            "DONE": ("[green]✓[/green]", "shards_done"),
            "PARTIAL": ("[yellow]◐[/yellow]", "shards_partial"),
            "UNRELATED": ("[yellow]→[/yellow]", "shards_unrelated"),
            "UNRESOLVED": ("[red]✗[/red]", "shards_unresolved"),
        }.get(token, ("[red]✗[/red]", "shards_unresolved"))
        setattr(result, marker[1], getattr(result, marker[1]) + 1)
        cost_note = f" [dim](cost ${cost:.4f})[/dim]" if cost else ""
        commit_note = (
            f" [dim]+{commits_added_this_shard} commit(s)[/dim]"
            if commits_added_this_shard else ""
        )
        console.print(
            f"    {marker[0]} {token}{commit_note}{cost_note}"
        )

        if new_sha != start_sha and token == "UNRELATED":
            result.warnings.append(
                f"{shard_ctx}: AI declared UNRELATED but moved HEAD "
                f"from {start_sha[:10]} to {new_sha[:10]}; commits "
                "kept locally."
            )

        # Walk the linear-history baseline forward so later shards
        # baseline against the now-extended branch tip.
        start_sha = new_sha

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

    if result.commits_added > 0 and push:
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
        f"- **Tests considered:** {run.tests_total} across "
        f"{run.shards_total} CI shard(s)",
        f"- **Outcomes:** "
        f"{run.shards_done} done · "
        f"{run.shards_partial} partial · "
        f"{run.shards_unrelated} unrelated · "
        f"{run.shards_unresolved} unresolved",
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
            commit_note = (
                f" — **+{o.commits_added} commit(s)**"
                if o.commits_added else ""
            )
            cost_per = (
                f" — cost ${o.cost_usd:.4f}"
                if o.cost_usd else ""
            )
            lines.append(
                f"### {badge} — `{o.shard_context}`"
            )
            lines.append("")
            lines.append(
                f"_{o.test_count} failed test(s) considered{commit_note}{cost_per}_"
            )
            lines.append(
                f"[full report]({o.target_url})"
            )
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
