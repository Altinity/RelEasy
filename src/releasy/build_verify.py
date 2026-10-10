"""Deterministic build + test verification for ported branches.

RelEasy builds; Claude fixes build failures and runs the PR's own tests.
"""
from __future__ import annotations

import fnmatch
import re
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from releasy.termlog import console

from releasy.config import Config
from releasy.github_ops import PRInfo
from releasy.git_ops import _git_env, run_git
from releasy.ai_resolve import (
    _BUILD_SCRIPT,
    _extract_assistant_text,
    _fill_placeholders,
    _invoke_claude_with_retries,
    _prompt_path,
    _write_build_script,
    build_log_path,
)
from releasy.analyze_fails import _CATEGORY_RUNNER_HINTS, _resolve_tool_paths


VerifyOutcome = Literal[
    "passed", "build_failed", "tests_failed", "timed_out", "error",
]


@dataclass
class VerifyResult:
    success: bool
    outcome: VerifyOutcome
    build_attempts: int = 0  # consecutive build-fix attempts spent
    iterations: int = 0  # total build↔test iterations
    error: str | None = None
    cost_usd: float | None = None
    new_head: str | None = None


def run_build(config: Config, repo_path: Path) -> tuple[int, bool]:
    """Run ``.releasy/build.sh``; return ``(exit_code, timed_out)``."""
    script = repo_path / _BUILD_SCRIPT
    console.print(
        f"    [cyan]\U0001f528 building[/cyan] [dim]($ bash {_BUILD_SCRIPT}, "
        f"timeout {config.ai_resolve.build_timeout_seconds}s)[/dim]"
    )
    start = time.monotonic()
    try:
        proc = subprocess.run(
            ["bash", str(script)],
            cwd=repo_path,
            env=_git_env(),
            timeout=config.ai_resolve.build_timeout_seconds,
            check=False,
        )
    except subprocess.TimeoutExpired:
        console.print("    [red]✗ build timed out[/red]")
        return (-1, True)
    elapsed = int(time.monotonic() - start)
    if proc.returncode == 0:
        console.print(f"    [green]✓ build ok[/green] [dim]({elapsed}s)[/dim]")
    else:
        console.print(
            f"    [red]✗ build failed[/red] [dim](exit {proc.returncode}, "
            f"{elapsed}s)[/dim]"
        )
    return (proc.returncode, False)


# The excerpt goes into one `claude` argv string; Linux caps an arg at 128 KiB.
_MAX_EXCERPT_BYTES = 48 * 1024
_MAX_LINE_CHARS = 2000


def _cap_line(ln: str) -> str:
    return ln if len(ln) <= _MAX_LINE_CHARS else ln[:_MAX_LINE_CHARS] + " …(truncated)"


def _tail_bytes(text: str, budget: int) -> tuple[str, bool]:
    """Last ``budget`` bytes of ``text`` (whole UTF-8 chars), and whether cut."""
    if budget <= 0:
        return "", True
    raw = text.encode("utf-8")
    if len(raw) <= budget:
        return text, False
    return raw[-budget:].decode("utf-8", errors="ignore"), True


# A red build whose log holds none of these never reached the compiler.
_COMPILE_ERROR_RE = re.compile(r"\berror:|^FAILED:|^ninja: build stopped")


def build_reached_compiler(repo_path: Path, log_path: str) -> bool:
    """True when the build log holds a real compile/link failure."""
    try:
        text = (repo_path / log_path).read_text(
            encoding="utf-8", errors="replace",
        )
    except OSError:
        return False
    return any(_COMPILE_ERROR_RE.search(ln) for ln in text.splitlines())


def _build_log_excerpt(
    repo_path: Path, log_path: str, tail_lines: int,
) -> str:
    """Grepped error/FAILED lines + the tail of the build log, byte-bounded."""
    try:
        text = (repo_path / log_path).read_text(
            encoding="utf-8", errors="replace",
        )
    except OSError:
        return "(build log unavailable)"
    raw_lines = text.splitlines()
    error_lines = [ln for ln in raw_lines if _COMPILE_ERROR_RE.search(ln)]
    extra = len(error_lines) - 60
    error_lines = [_cap_line(ln) for ln in error_lines[:60]]
    if extra > 0:
        error_lines.append(f"... (+{extra} more)")

    # Errors first, capped to half the budget; the tail fills the rest.
    err_block = ""
    if error_lines:
        err_block = "# Grepped error: / FAILED: lines\n" + "\n".join(error_lines)
        err_block, _ = _tail_bytes(err_block, _MAX_EXCERPT_BYTES // 2)

    budget = _MAX_EXCERPT_BYTES - len(err_block.encode("utf-8"))
    tail = [_cap_line(ln) for ln in raw_lines[-tail_lines:]]
    tail_text, cut = _tail_bytes("\n".join(tail), budget)
    header = f"# Tail of {log_path}" + (" (truncated)" if cut else "")

    parts = [p for p in (err_block, f"{header}\n{tail_text}") if p.strip()]
    return "\n\n".join(parts)


def _changed_files(repo_path: Path, base_sha: str) -> list[str]:
    res = run_git(
        ["diff", "--name-only", f"{base_sha}..HEAD"], repo_path, check=False,
    )
    if res.returncode != 0:
        return []
    return [ln.strip() for ln in res.stdout.splitlines() if ln.strip()]


def _touched_test_files(changed: list[str], globs: list[str]) -> list[str]:
    out: list[str] = []
    for path in changed:
        if any(fnmatch.fnmatch(path, g) for g in globs):
            out.append(path)
    return out


def _categorise(test_files: list[str]) -> set[str]:
    """Map touched test paths to ``_CATEGORY_RUNNER_HINTS`` categories."""
    cats: set[str] = set()
    for p in test_files:
        if p.startswith("tests/queries/"):
            cats.add("stateless")
        elif p.startswith("tests/integration/"):
            cats.add("integration")
    return cats


def _runner_hints(test_files: list[str]) -> str:
    cats = _categorise(test_files)
    if not cats:
        return (
            "_(No known ClickHouse test family matched — find the right "
            "invocation under `ci/jobs/` or the repo's test docs.)_"
        )
    blocks: list[str] = []
    for cat in sorted(cats):
        template = _CATEGORY_RUNNER_HINTS.get(cat)
        if not template:
            continue
        block = (
            template
            .replace("{tests_arg}", "<the tests listed above>")
            .replace(
                "{shard_context}",
                "(infer the right flags from `ci/jobs/` if a test needs them)",
            )
        )
        blocks.append(f"**{cat} tests**\n\n{block}")
    return "\n\n".join(blocks)


def _previous_attempt_section(previous_error: str | None) -> str:
    if not previous_error:
        return ""
    return (
        "## Previous attempt\n\n"
        "An earlier run tested this same resolution and stopped with:\n\n"
        f"> {previous_error}\n\n"
        "Re-running and reaching the same verdict is not acceptable. Treat "
        "this as the problem to solve now: fix the failure, amend HEAD, and "
        "re-run the tests.\n"
    )


def _render(template_path: Path, mapping: dict[str, str]) -> str:
    return _fill_placeholders(template_path.read_text(encoding="utf-8"), mapping)


def _common_placeholders(
    config: Config, repo_path: Path, source_pr: PRInfo,
    port_branch: str, base_branch: str, pre_resolve_sha: str,
) -> dict[str, str]:
    from releasy.github_ops import get_origin_repo_slug
    return {
        "repo_slug": get_origin_repo_slug(config) or "<unknown>",
        "cwd": str(repo_path),
        "port_branch": port_branch,
        "base_branch": base_branch,
        "pre_resolve_sha": pre_resolve_sha,
        "source_pr_url": source_pr.url,
        "source_pr_title": source_pr.title,
        "source_pr_number": str(source_pr.number),
        "build_command": config.ai_resolve.build_command,
    }


def _head_sha(repo_path: Path) -> str | None:
    res = run_git(["rev-parse", "--verify", "HEAD"], repo_path, check=False)
    return res.stdout.strip() if res.returncode == 0 else None


def _last_marker(text: str, markers: tuple[str, ...]) -> str | None:
    for line in reversed(text.strip().splitlines()):
        s = line.strip().strip("`").strip()
        for mk in markers:
            if s == mk or s.startswith(mk + ":"):
                return s
    return None


def verify_build_and_tests(
    config: Config,
    repo_path: Path,
    source_pr: PRInfo,
    *,
    port_branch: str,
    base_branch: str,
    base_sha: str,
    max_build_attempts: int | None = None,
    previous_error: str | None = None,
) -> VerifyResult:
    """Build the branch and run the PR's tests (``base_sha`` scopes test detection).

    ``previous_error``: an earlier run's verdict, handed to the first test session to fix.
    """
    max_build = max_build_attempts or config.ai_resolve.max_build_attempts
    max_iters = max(1, config.ai_resolve.max_verify_iterations)
    log_path = build_log_path(port_branch)

    pre_resolve_sha = ""
    res = run_git(["rev-parse", "--verify", "HEAD~1"], repo_path, check=False)
    if res.returncode == 0:
        pre_resolve_sha = res.stdout.strip()

    try:
        _write_build_script(
            repo_path, config.ai_resolve.build_command, log_path,
        )
    except OSError as exc:
        return VerifyResult(
            success=False, outcome="error",
            error=f"could not write build wrapper: {exc}",
        )

    fix_prompt_path = _prompt_path(config, config.ai_resolve.fix_build_prompt_file)
    test_prompt_path = _prompt_path(config, config.ai_resolve.run_tests_prompt_file)

    cost_total: float | None = None
    consecutive_build_failures = 0
    iterations = 0

    def _add_cost(c: float | None) -> None:
        nonlocal cost_total
        if c is not None:
            cost_total = (cost_total or 0.0) + c

    while iterations < max_iters:
        iterations += 1

        rc, timed_out = run_build(config, repo_path)
        if timed_out:
            return VerifyResult(
                success=False, outcome="timed_out",
                build_attempts=consecutive_build_failures, iterations=iterations,
                error="build timed out", cost_usd=cost_total,
                new_head=_head_sha(repo_path),
            )

        if rc != 0:
            if not build_reached_compiler(repo_path, log_path):
                # Environment fault: no code fix can help, don't spend an attempt.
                console.print(
                    f"    [red]✗ build never reached the compiler[/red] "
                    f"[dim](no error:/FAILED: line in {log_path} — "
                    "environment fault, not a code fix)[/dim]"
                )
                return VerifyResult(
                    success=False, outcome="error",
                    build_attempts=consecutive_build_failures,
                    iterations=iterations,
                    error=(
                        "build never reached the compiler (no compile error "
                        f"in {log_path}) — check the build environment "
                        "(ai_resolve.build_command, build dir, toolchain)"
                    ),
                    cost_usd=cost_total, new_head=_head_sha(repo_path),
                )
            consecutive_build_failures += 1
            if consecutive_build_failures > max_build:
                return VerifyResult(
                    success=False, outcome="build_failed",
                    build_attempts=consecutive_build_failures - 1,
                    iterations=iterations,
                    error=(
                        f"build still failing after {max_build} fix "
                        f"attempt(s)"
                    ),
                    cost_usd=cost_total, new_head=_head_sha(repo_path),
                )

            mapping = _common_placeholders(
                config, repo_path, source_pr, port_branch, base_branch,
                pre_resolve_sha,
            )
            mapping["build_log_excerpt"] = _build_log_excerpt(
                repo_path, log_path, config.ai_resolve.build_log_tail_lines,
            )
            mapping["build_log"] = log_path
            mapping["attempt"] = str(consecutive_build_failures)
            mapping["max_build_attempts"] = str(max_build)
            try:
                prompt = _render(fix_prompt_path, mapping)
            except OSError as exc:
                return VerifyResult(
                    success=False, outcome="error",
                    error=f"fix-build prompt not found: {exc}",
                    cost_usd=cost_total,
                )

            console.print(
                f"    [magenta]\U0001f916 fix-build attempt "
                f"{consecutive_build_failures}/{max_build}[/magenta]"
            )
            ec, out, to, cost = _invoke_claude_with_retries(
                config, repo_path, prompt,
                timeout=config.ai_resolve.timeout_seconds,
            )
            _add_cost(cost)
            if to:
                return VerifyResult(
                    success=False, outcome="timed_out",
                    build_attempts=consecutive_build_failures,
                    iterations=iterations, error="fix-build timed out",
                    cost_usd=cost_total, new_head=_head_sha(repo_path),
                )
            marker = _last_marker(
                _extract_assistant_text(out), ("FIXED", "CANNOT FIX"),
            )
            if marker and marker.startswith("CANNOT FIX"):
                return VerifyResult(
                    success=False, outcome="build_failed",
                    build_attempts=consecutive_build_failures,
                    iterations=iterations,
                    error=f"claude could not fix the build: {marker}",
                    cost_usd=cost_total, new_head=_head_sha(repo_path),
                )
            continue

        consecutive_build_failures = 0

        if not config.ai_resolve.run_pr_tests:
            return VerifyResult(
                success=True, outcome="passed", iterations=iterations,
                cost_usd=cost_total, new_head=_head_sha(repo_path),
            )

        test_files = _touched_test_files(
            _changed_files(repo_path, base_sha),
            config.ai_resolve.test_file_globs,
        )
        if not test_files:
            console.print(
                "    [dim]no test files touched by the port — skipping tests"
                "[/dim]"
            )
            return VerifyResult(
                success=True, outcome="passed", iterations=iterations,
                cost_usd=cost_total, new_head=_head_sha(repo_path),
            )

        head_before = _head_sha(repo_path)
        mapping = _common_placeholders(
            config, repo_path, source_pr, port_branch, base_branch,
            pre_resolve_sha,
        )
        mapping["test_files"] = "\n".join(f"- `{f}`" for f in test_files)
        mapping["runner_hints"] = _runner_hints(test_files)
        mapping["previous_attempt"] = _previous_attempt_section(previous_error)
        previous_error = None
        try:
            prompt = _render(test_prompt_path, mapping)
        except OSError as exc:
            return VerifyResult(
                success=False, outcome="error",
                error=f"run-tests prompt not found: {exc}",
                cost_usd=cost_total,
            )

        console.print(
            f"    [magenta]\U0001f9ea running {len(test_files)} PR test "
            f"file(s)[/magenta]"
        )
        ec, out, to, cost = _invoke_claude_with_retries(
            config, repo_path, prompt,
            timeout=config.ai_resolve.test_timeout_seconds,
            allowed_tools=_resolve_tool_paths(
                list(config.analyze_fails.allowed_tools), repo_path,
            ),
        )
        _add_cost(cost)
        tests_log = log_path.removesuffix(".log") + ".tests.log"
        try:
            (repo_path / tests_log).parent.mkdir(parents=True, exist_ok=True)
            (repo_path / tests_log).write_text(out, encoding="utf-8")
        except OSError as exc:
            console.print(
                f"    [yellow]![/yellow] could not write {tests_log} "
                f"[dim]({exc})[/dim]"
            )
        else:
            console.print(
                f"    [dim]run-tests output (exit {ec}): {tests_log}[/dim]"
            )
        if to:
            return VerifyResult(
                success=False, outcome="timed_out", iterations=iterations,
                error="run-tests timed out", cost_usd=cost_total,
                new_head=_head_sha(repo_path),
            )

        head_after = _head_sha(repo_path)
        if head_after and head_after != head_before:
            # A test fix was committed; rebuild and re-verify.
            console.print(
                "    [dim]test step amended the resolution — rebuilding[/dim]"
            )
            continue

        marker = _last_marker(
            _extract_assistant_text(out), ("TESTS PASSED", "TESTS FAILED"),
        )
        if marker == "TESTS PASSED":
            return VerifyResult(
                success=True, outcome="passed", iterations=iterations,
                cost_usd=cost_total, new_head=head_after,
            )
        # No verdict, or tests never ran: the code was not judged.
        could_not_run = not marker or marker.startswith(
            "TESTS FAILED: could not run tests",
        )
        return VerifyResult(
            success=False,
            outcome="error" if could_not_run else "tests_failed",
            iterations=iterations,
            error=f"PR tests did not pass: {marker or 'no verdict'}",
            cost_usd=cost_total, new_head=head_after,
        )

    return VerifyResult(
        success=False, outcome="build_failed",
        build_attempts=consecutive_build_failures, iterations=iterations,
        error=f"verify exceeded max_verify_iterations ({max_iters})",
        cost_usd=cost_total, new_head=_head_sha(repo_path),
    )
