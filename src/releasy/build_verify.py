"""Deterministic build + test verification for ported branches.

RelEasy builds (exit code is truth); on failure a fresh Claude invocation
fixes the build from the log tail (up to ``max_build_attempts``); on a green
build Claude runs the PR's own tests. A test fix re-enters the build loop;
the two-level loop is bounded by ``max_verify_iterations``. Failures stay on
the local branch as ``build_failed`` and resume next ``releasy run``.
"""
from __future__ import annotations

import fnmatch
import os
import re
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from releasy.termlog import console

from releasy.config import Config, get_ssh_key_path
from releasy.github_ops import PRInfo
from releasy.git_ops import run_git
# Reuse the resolve module's build wrapper + Claude-invocation primitives.
from releasy.ai_resolve import (
    _BUILD_LOG,
    _BUILD_SCRIPT,
    _extract_assistant_text,
    _invoke_claude_with_retries,
    _write_build_script,
)
from releasy.analyze_fails import _CATEGORY_RUNNER_HINTS


VerifyOutcome = Literal[
    "passed", "build_failed", "tests_failed", "timed_out", "error",
]


@dataclass
class VerifyResult:
    """Outcome of one ``verify_build_and_tests`` pass."""
    success: bool
    outcome: VerifyOutcome
    build_attempts: int = 0  # consecutive build-fix attempts spent
    iterations: int = 0  # total build↔test iterations
    error: str | None = None
    cost_usd: float | None = None
    new_head: str | None = None


# ---------------------------------------------------------------------------
# Deterministic build (RelEasy-owned; exit code is truth)
# ---------------------------------------------------------------------------


def _build_env() -> dict[str, str]:
    """Build-subprocess env with the same SSH-key handling as git_ops."""
    env = os.environ.copy()
    ssh_key = get_ssh_key_path()
    if ssh_key:
        env["GIT_SSH_COMMAND"] = f"ssh -i {ssh_key} -o StrictHostKeyChecking=no"
    return env


def run_build(config: Config, repo_path: Path) -> tuple[int, bool]:
    """Run ``.releasy/build.sh`` (submodule update + build_command, tees to
    the log) and return ``(exit_code, timed_out)``."""
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
            env=_build_env(),
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


# The excerpt is embedded into the fix-build prompt, which is passed as a
# single argv string to `claude`; Linux caps one arg at 128 KiB
# (MAX_ARG_STRLEN). Keep the excerpt well under that — compiler lines can be
# huge (template types, full paths), so cap both per-line and total bytes.
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


def _build_log_excerpt(repo_path: Path, tail_lines: int) -> str:
    """Grepped error/FAILED lines + the tail of the build log, byte-bounded.

    ninja compiles past the first error, so the real cause can sit above a
    plain tail — surface the grepped lines too. Bounded to stay under the OS
    arg limit (see ``_MAX_EXCERPT_BYTES``).
    """
    log_path = repo_path / _BUILD_LOG
    try:
        text = log_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return "(build log unavailable)"
    raw_lines = text.splitlines()
    # Grep on the uncapped lines (so a long line still matches), then cap each
    # selected line for display.
    error_lines = [
        ln for ln in raw_lines
        if re.search(r"\berror:|^FAILED:|^ninja: build stopped", ln)
    ]
    extra = len(error_lines) - 60
    error_lines = [_cap_line(ln) for ln in error_lines[:60]]
    if extra > 0:
        error_lines.append(f"... (+{extra} more)")

    # The grepped errors are the cause — keep them first, capped to half the
    # budget; the tail fills whatever's left.
    err_block = ""
    if error_lines:
        err_block = "# Grepped error: / FAILED: lines\n" + "\n".join(error_lines)
        err_block, _ = _tail_bytes(err_block, _MAX_EXCERPT_BYTES // 2)

    budget = _MAX_EXCERPT_BYTES - len(err_block.encode("utf-8"))
    tail = [_cap_line(ln) for ln in raw_lines[-tail_lines:]]
    tail_text, cut = _tail_bytes("\n".join(tail), budget)
    header = f"# Tail of {_BUILD_LOG}" + (" (truncated)" if cut else "")

    parts = [p for p in (err_block, f"{header}\n{tail_text}") if p.strip()]
    return "\n\n".join(parts)


# ---------------------------------------------------------------------------
# Test detection + runner hints
# ---------------------------------------------------------------------------


def _changed_files(repo_path: Path, base_sha: str) -> list[str]:
    """Files the port adds/changes: ``git diff --name-only base_sha..HEAD``."""
    res = run_git(
        ["diff", "--name-only", f"{base_sha}..HEAD"], repo_path, check=False,
    )
    if res.returncode != 0:
        return []
    return [ln.strip() for ln in res.stdout.splitlines() if ln.strip()]


def _touched_test_files(changed: list[str], globs: list[str]) -> list[str]:
    """Subset of ``changed`` matching any of the test ``globs``."""
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
    """Runner guidance for the touched categories, reusing
    ``analyze_fails._CATEGORY_RUNNER_HINTS``."""
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


# ---------------------------------------------------------------------------
# Prompt rendering
# ---------------------------------------------------------------------------


def _render(template_path: Path, mapping: dict[str, str]) -> str:
    text = template_path.read_text(encoding="utf-8")

    def _replace(m: re.Match[str]) -> str:
        return mapping.get(m.group(1), m.group(0))

    return re.sub(r"\{([a-zA-Z_][a-zA-Z0-9_]*)\}", _replace, text)


def _prompt_path(config: Config, rel: str) -> Path:
    p = Path(rel)
    return p if p.is_absolute() else (config.repo_dir / p).resolve()


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


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _head_sha(repo_path: Path) -> str | None:
    res = run_git(["rev-parse", "--verify", "HEAD"], repo_path, check=False)
    return res.stdout.strip() if res.returncode == 0 else None


def _last_marker(text: str, markers: tuple[str, ...]) -> str | None:
    """Last line that starts with one of ``markers`` (case-sensitive)."""
    for line in reversed(text.strip().splitlines()):
        s = line.strip().strip("`").strip()
        for mk in markers:
            if s == mk or s.startswith(mk + ":"):
                return s
    return None


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------


def verify_build_and_tests(
    config: Config,
    repo_path: Path,
    source_pr: PRInfo,
    *,
    port_branch: str,
    base_branch: str,
    base_sha: str,
    max_build_attempts: int | None = None,
) -> VerifyResult:
    """Build the branch deterministically and run the PR's tests.

    ``base_sha`` (the branch's fork point) scopes test detection to the
    port's own files. Returns a :class:`VerifyResult`; the caller opens the
    PR or parks as ``build_failed``. All work stays on ``port_branch``.
    """
    max_build = max_build_attempts or config.ai_resolve.max_build_attempts
    max_iters = max(1, config.ai_resolve.max_verify_iterations)

    pre_resolve_sha = ""
    res = run_git(["rev-parse", "--verify", "HEAD~1"], repo_path, check=False)
    if res.returncode == 0:
        pre_resolve_sha = res.stdout.strip()

    # Write the build wrapper once from current config (idempotent).
    try:
        _write_build_script(repo_path, config.ai_resolve.build_command)
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

        # --- Build (deterministic) ---------------------------------------
        rc, timed_out = run_build(config, repo_path)
        if timed_out:
            return VerifyResult(
                success=False, outcome="timed_out",
                build_attempts=consecutive_build_failures, iterations=iterations,
                error="build timed out", cost_usd=cost_total,
                new_head=_head_sha(repo_path),
            )

        if rc != 0:
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
                repo_path, config.ai_resolve.build_log_tail_lines,
            )
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
            continue  # rebuild

        # --- Build green -------------------------------------------------
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

        # --- Tests (Claude runs; HEAD-change drives re-verify) -----------
        head_before = _head_sha(repo_path)
        mapping = _common_placeholders(
            config, repo_path, source_pr, port_branch, base_branch,
            pre_resolve_sha,
        )
        mapping["test_files"] = "\n".join(f"- `{f}`" for f in test_files)
        mapping["runner_hints"] = _runner_hints(test_files)
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
            allowed_tools=config.analyze_fails.allowed_tools,
        )
        _add_cost(cost)
        if to:
            return VerifyResult(
                success=False, outcome="timed_out", iterations=iterations,
                error="run-tests timed out", cost_usd=cost_total,
                new_head=_head_sha(repo_path),
            )

        head_after = _head_sha(repo_path)
        if head_after and head_after != head_before:
            # A fix was committed — may affect the build; rebuild + re-verify.
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
        return VerifyResult(
            success=False, outcome="tests_failed", iterations=iterations,
            error=f"PR tests did not pass: {marker or 'no verdict'}",
            cost_usd=cost_total, new_head=head_after,
        )

    # Overall iteration budget exhausted (build↔test ping-pong).
    return VerifyResult(
        success=False, outcome="build_failed",
        build_attempts=consecutive_build_failures, iterations=iterations,
        error=f"verify exceeded max_verify_iterations ({max_iters})",
        cost_usd=cost_total, new_head=_head_sha(repo_path),
    )
