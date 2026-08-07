"""Claude-driven autonomous conflict resolution.

Renders a prompt template, spawns ``claude -p``, streams its output to the
console, enforces a timeout, and verifies the post-conditions (branch pushed,
PR opened, AI label attached) using the existing GitHub helpers.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import signal
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from rich.markup import escape

from releasy.termlog import console

from releasy.config import AIApiConfig, Config, PortMode
from releasy.git_ops import (
    is_operation_in_progress,
    run_git,
)
from releasy.github_ops import PRInfo

if TYPE_CHECKING:
    from releasy.api_agent import ApiAgentSpec


# What kind of conflicted git operation Claude is being asked to drive to
# completion. Picks the prompt template (``ai_resolve.prompt_file`` for
# cherry-picks, ``ai_resolve.merge_prompt_file`` for merges) and shapes
# the placeholders rendered into it.
OperationKind = Literal["cherry-pick", "merge"]


@dataclass
class AIResolveContext:
    port_branch: str
    base_branch: str
    # The PR most relevant to interpreting the conflict:
    # - cherry-pick: the upstream PR being ported (source of the commits).
    # - merge:       the upstream source PR the rebase branch ports — the
    #                conflict is between its changes and the moved-on
    #                base branch, so its intent is what Claude must
    #                preserve.
    source_pr: PRInfo
    conflict_files: list[str] = field(default_factory=list)
    # SHA of the port branch tip BEFORE the conflicting operation was
    # attempted. Used to verify Claude actually committed something, and
    # to reset to a known-good state on failure.
    start_sha: str | None = None
    # Which kind of conflict Claude is resolving. Selects the prompt
    # template and tweaks the postcondition narrative.
    operation: OperationKind = "cherry-pick"
    # Merge-only context: the URL of the rebase PR whose branch is being
    # kept current. Rendered into the merge prompt so Claude can name it
    # in commit messages / log lines if it wants. Ignored for cherry-pick.
    rebase_pr_url: str | None = None
    # Free-form note the user attached to this PR / group / feature in
    # the session file via ``ai_context:``. Surfaced verbatim in a
    # dedicated section of the rendered prompt. Empty string ⇒ no
    # section is rendered.
    user_context: str = ""
    # Cherry-pick split-commit mode (see ``ai_resolve.split_conflict_commit``):
    # when True, RelEasy has already concluded the cherry-pick by committing
    # the conflict markers as a stand-alone "with conflicts" commit, and
    # Claude's job is to make a SECOND commit on top with the resolution.
    # Selects ``ai_resolve.split_prompt_file`` instead of ``prompt_file``
    # and tightens postcondition checks (HEAD must have advanced past
    # ``pre_resolve_sha``, not just ``start_sha``). Ignored for merge.
    split_mode: bool = False
    # SHA of HEAD AFTER the "with conflicts" commit but BEFORE Claude
    # ran. Used in split-mode postcondition checks: a successful resolve
    # must add at least one new commit on top of this SHA.
    pre_resolve_sha: str | None = None
    # Backport unlocks bucket-0 (drop optional missing-prereq surfaces);
    # forward-port keeps the MISSING_PREREQS-only flow.
    mode: PortMode = "forward_port"
    # Resolve + commit but do NOT build (no-build prompt); RelEasy builds +
    # tests afterwards via ``build_verify``. Set by the cherry-pick port path
    # when ``deterministic_build`` is on; merge/rebase leave it False (legacy).
    skip_build: bool = False


@dataclass
class AIResolveResult:
    success: bool
    iterations: int | None = None
    error: str | None = None
    timed_out: bool = False
    new_head: str | None = None  # branch tip after Claude's commit
    # Total USD cost reported by Claude across every attempt of this
    # resolve invocation (sum of ``total_cost_usd`` from each
    # ``result``-typed event in the stream-json transcript, including
    # transient-API-error retries). ``None`` when Claude reported no cost
    # at all (e.g. failed before producing a result event).
    cost_usd: float | None = None
    # When Claude reported ``MISSING_PREREQS: <url1> <url2>`` in its
    # output (followed by ``UNRESOLVED``), these are the discovered PR
    # URLs and the one-line REASON. Empty list / None when the run was
    # not classified as a missing-prereq situation. Always paired with
    # ``success=False`` and a non-None ``error`` ("claude reported
    # MISSING_PREREQS"); the pipeline reads ``missing_prereq_prs`` to
    # branch into detection-only labelling or auto-recovery.
    missing_prereq_prs: list[str] = field(default_factory=list)
    missing_prereq_note: str | None = None
    # True when the run died before the model could work on the conflict —
    # a transient API error that outlived the retries, an unusable backend,
    # a missing prompt file. The resolver reached no verdict, so callers
    # must not spend a retry budget (``max_partial_continue_attempts``) on
    # it. A timeout is NOT this: that one burned the whole wall-clock.
    api_aborted: bool = False


# ---------------------------------------------------------------------------
# Build wrapper script
# ---------------------------------------------------------------------------
#
# Claude Code's Bash tool matcher refuses compound commands (subshells,
# `&&`, `;`, `bash -c '…'`). The user's build_command is intrinsically
# multi-step (`cd build` + `cmake …` + `ninja`) and its output is too
# large to fit in a single Bash tool result. We solve both problems at
# once by writing the build commands to a wrapper script inside the repo
# (`.releasy/build.sh`) that internally tees full output to
# `.releasy/build.log`. Claude then only needs to run the single
# command  `bash .releasy/build.sh`, and can `Read` the log on failure.

_BUILD_DIR = ".releasy"
_BUILD_SCRIPT = f"{_BUILD_DIR}/build.sh"
_BUILD_LOG = f"{_BUILD_DIR}/build.log"


def _write_build_script(repo_path: Path, build_command: str) -> None:
    """Materialise the build wrapper inside the repo.

    Overwrites any previous copy so config changes take effect.
    """
    target = repo_path / _BUILD_SCRIPT
    target.parent.mkdir(parents=True, exist_ok=True)
    body = build_command.rstrip() + "\n"
    # Refresh submodules before every build: when the port branch
    # ingests newer commits (cherry-pick / merge-target), submodule
    # pointers can advance — building against stale checkouts then
    # produces confusing compile or runtime failures. No-op when the
    # repo has none.
    script = (
        "#!/usr/bin/env bash\n"
        "# Auto-generated by RelEasy. Do not edit by hand — regenerated\n"
        "# on every AI-resolve invocation from ai_resolve.build_command.\n"
        "set -euo pipefail\n"
        "cd \"$(git rev-parse --show-toplevel)\"\n"
        f"mkdir -p {_BUILD_DIR}\n"
        f"exec > >(tee {_BUILD_LOG}) 2>&1\n"
        "echo \"[releasy] build started at $(date -u +%FT%TZ)\"\n"
        "echo \"[releasy] git submodule update --init --recursive\"\n"
        "git submodule update --init --recursive --jobs 8\n"
        "(\n"
        f"{body}"
        ")\n"
        "echo \"[releasy] build finished at $(date -u +%FT%TZ)\"\n"
    )
    target.write_text(script, encoding="utf-8")
    target.chmod(0o755)

    gitignore = repo_path / _BUILD_DIR / ".gitignore"
    if not gitignore.exists():
        gitignore.write_text("*\n", encoding="utf-8")


# ---------------------------------------------------------------------------
# Prompt rendering
# ---------------------------------------------------------------------------


def _resolve_prompt_template(config: Config, ctx: AIResolveContext) -> Path:
    """Pick the prompt file path for ``ctx.operation`` and resolve it.

    Cherry-pick uses ``ai_resolve.prompt_file`` for the legacy single-
    commit flow (cherry-pick still in progress, Claude resolves and
    runs ``git cherry-pick --continue``), or ``ai_resolve.split_prompt_file``
    in split-commit mode (cherry-pick already concluded as a "with
    conflicts" commit, Claude makes a second resolution commit on top).
    Merge uses ``ai_resolve.merge_prompt_file``. The three templates
    share the same placeholder vocabulary but differ in flow narrative.
    """
    if ctx.operation == "merge":
        raw = config.ai_resolve.merge_prompt_file
    elif ctx.skip_build:
        # Resolve-only: same split-commit contract as split_prompt_file, minus
        # the Build step (RelEasy builds afterwards).
        raw = config.ai_resolve.resolve_only_prompt_file
    elif ctx.split_mode:
        raw = config.ai_resolve.split_prompt_file
    else:
        raw = config.ai_resolve.prompt_file
    prompt_path = Path(raw)
    if not prompt_path.is_absolute():
        prompt_path = (config.repo_dir / prompt_path).resolve()
    return prompt_path


def _render_prompt(config: Config, repo_path: Path, ctx: AIResolveContext) -> str:
    """Load the prompt template and fill in placeholders."""
    prompt_path = _resolve_prompt_template(config, ctx)

    if not prompt_path.exists():
        if ctx.operation == "merge":
            which = "ai_resolve.merge_prompt_file"
        elif ctx.skip_build:
            which = "ai_resolve.resolve_only_prompt_file"
        elif ctx.split_mode:
            which = "ai_resolve.split_prompt_file"
        else:
            which = "ai_resolve.prompt_file"
        raise FileNotFoundError(
            f"AI prompt template not found: {prompt_path}. "
            f"Set {which} in config."
        )

    template = prompt_path.read_text(encoding="utf-8")

    from releasy.github_ops import get_origin_repo_slug
    repo_slug = get_origin_repo_slug(config) or "<unknown>"

    conflict_files_md = "\n".join(f"- `{f}`" for f in ctx.conflict_files) or "- (none)"

    body = (ctx.source_pr.body or "").strip()
    if not body:
        body = "_(empty)_"
    elif len(body) > 4000:
        body = body[:4000] + "\n\n_(truncated)_"

    # SHA Claude can use to inspect the EXACT diff being applied (cherry-pick
    # of a merge commit uses the first-parent diff, which is what `git show -m
    # --first-parent` prints). For open PRs we fall back to head_sha so Claude
    # still has a concrete ref; the prompt also tells it to use `gh pr diff`
    # as a cross-check.
    source_pr_merge_sha = (
        ctx.source_pr.merge_commit_sha or ctx.source_pr.head_sha or ""
    )

    # Origin remote / default branch — referenced by the prereq-detection
    # section of the cherry-pick prompt so commands stay literal-copy
    # ready (not "git log -S … origin/master" hard-coded when the user
    # configured a different remote_name).
    origin_remote_name = config.origin.remote_name
    # The "default" branch on origin to search prereq history against.
    # We don't have a config knob for this today (target_branch is the
    # *port target*, which is exactly the branch where the prereq is
    # missing — not the right thing to search). Default to ``master``,
    # the convention for the upstream-mirror repos RelEasy targets.
    origin_branch_default = "master"

    if config.upstream is not None:
        upstream_remote_name = config.upstream.remote_name
        upstream_branch = config.upstream.branch
        upstream_fetch_section = (
            "Also search the upstream remote (fetch it first):\n\n"
            "```bash\n"
            f"git fetch {upstream_remote_name} {upstream_branch} --depth=500\n"
            f"git log -S '<identifier>' --oneline {upstream_remote_name}/"
            f"{upstream_branch} -- <file>\n"
            "```\n"
        )
    else:
        upstream_remote_name = ""
        upstream_branch = ""
        upstream_fetch_section = (
            "_(no upstream remote is configured; only the origin history "
            "above is searched)_\n"
        )

    # The user can attach a per-PR / per-group note to the resolver via
    # ``ai_context:`` in the session file. We render it as a dedicated
    # section so it's prominent without us having to edit the template
    # for every consumer; when the user supplied nothing, the placeholder
    # collapses to an empty string so the prompt simply skips the
    # section. Leading newline keeps an empty render flush against the
    # surrounding content (no stray blank lines), and the trailing
    # newlines keep the populated render from running into whatever
    # follows in the template (notably a `---` separator that would
    # otherwise be parsed as a setext heading underline for the last
    # line of the user's note).
    user_context_text = (ctx.user_context or "").strip()
    if user_context_text:
        user_context_section = (
            "\n## User-supplied context (from session.yaml)\n\n"
            "> The operator attached this note to this PR / group when "
            "configuring the run. Treat it as authoritative guidance from "
            "the human driving the port — but it does NOT override the "
            "diff-fidelity rule below: the source PR's diff is still the "
            "only authoritative list of what the port wants to add.\n\n"
            f"{user_context_text}\n"
        )
    else:
        user_context_section = ""

    placeholders = {
        "repo_slug": repo_slug,
        "cwd": str(repo_path),
        "port_branch": ctx.port_branch,
        "base_branch": ctx.base_branch,
        "source_pr_url": ctx.source_pr.url,
        "source_pr_title": ctx.source_pr.title,
        "source_pr_number": str(ctx.source_pr.number),
        "source_pr_body": body,
        "source_pr_merge_sha": source_pr_merge_sha,
        "conflict_files": conflict_files_md,
        "build_command": config.ai_resolve.build_command,
        "build_script": _BUILD_SCRIPT,
        "build_log": _BUILD_LOG,
        "max_iterations": str(config.ai_resolve.max_iterations),
        "label": config.ai_resolve.label,
        # Merge-only — rendered as a literal placeholder for cherry-pick
        # prompts that don't reference it (no-op there). For merge prompts
        # this lets Claude link the rebase PR in narration if helpful.
        "rebase_pr_url": ctx.rebase_pr_url or "",
        # Prereq-detection placeholders (cherry-pick prompt only — the
        # merge prompt doesn't reference them, so empty values are safe).
        "origin_remote_name": origin_remote_name,
        "origin_branch": origin_branch_default,
        "upstream_remote_name": upstream_remote_name,
        "upstream_branch": upstream_branch,
        "upstream_fetch_section": upstream_fetch_section,
        # User-supplied per-PR / per-group note from the session file.
        # Either an empty string (no note ⇒ section skipped) or a fully
        # rendered "## User-supplied context" markdown block.
        "user_context_section": user_context_section,
        # Split-mode only: the SHA of the "with conflicts" commit that
        # holds the conflict markers verbatim. Claude must NOT amend it;
        # the resolution lives in a new commit on top. Empty for non-split
        # invocations so the placeholder collapses cleanly.
        "pre_resolve_sha": ctx.pre_resolve_sha or "",
        # Port direction. Either ``"backport"`` or ``"forward_port"``.
        # Drives the "Port direction" section in the resolver prompt:
        # backport-mode templates activate bucket-0; forward-port-mode
        # templates keep the original MISSING_PREREQS-only flow.
        "port_direction": ctx.mode,
        # Comma-separated list of source PR labels for the model to
        # sanity-check the detected ``port_direction``. Empty when no
        # labels were fetched (still acceptable — the ladder doesn't
        # require labels to commit to a mode).
        "source_pr_labels": ", ".join(
            l for l in (ctx.source_pr.labels or []) if l
        ),
    }

    def _replace(match: re.Match[str]) -> str:
        key = match.group(1)
        return placeholders.get(key, match.group(0))

    return re.sub(r"\{([a-zA-Z_][a-zA-Z0-9_]*)\}", _replace, template)


# ---------------------------------------------------------------------------
# Claude invocation
# ---------------------------------------------------------------------------


def _model_effort_args(config: Config) -> list[str]:
    """Global --model / --effort flags, appended to every claude invocation."""
    args: list[str] = []
    if config.ai_model:
        args += ["--model", config.ai_model]
    if config.ai_effort:
        args += ["--effort", config.ai_effort]
    return args


# A single argv string is capped by the OS (Linux MAX_ARG_STRLEN = 128 KiB).
# Prompts at/under this go inline as `-p <prompt>` (proven path); larger ones
# are fed via stdin (`claude -p` reads the prompt from stdin) so there is no
# size limit. Kept well under 128 KiB for headroom.
_PROMPT_ARG_MAX_BYTES = 96 * 1024


def _build_claude_argv(
    config: Config, allowed_tools: list[str] | None = None,
) -> list[str]:
    """Base print-mode argv WITHOUT the prompt — the prompt is supplied at
    spawn time (inline for small prompts, via stdin for large ones)."""
    cmd = [
        config.ai_resolve.command,
        "-p",
        "--output-format", "stream-json",
        "--verbose",
    ]
    tools = allowed_tools if allowed_tools is not None else config.ai_resolve.allowed_tools
    if tools:
        cmd += ["--allowedTools", ",".join(tools)]
    cmd += _model_effort_args(config)
    cmd += list(config.ai_resolve.extra_args)
    return cmd


def _argv_with_inline_prompt(base_argv: list[str], prompt: str) -> list[str]:
    """Insert ``prompt`` right after the ``-p`` flag (small-prompt path)."""
    return base_argv[:2] + [prompt] + base_argv[2:]


# ---------------------------------------------------------------------------
# Backend selection (CLI subprocess vs Anthropic API token)
# ---------------------------------------------------------------------------


def _api_backend_selected(config: Config) -> bool:
    return str(getattr(config, "ai_backend", "cli") or "cli").lower() == "api"


def _build_api_spec(
    config: Config, allowed_tools: list[str] | None = None,
) -> "ApiAgentSpec | None":
    """API-backend spec for this invocation, or ``None`` in CLI mode.

    Counterpart of :func:`_build_claude_argv`: it reads the same
    ``config.ai_resolve.allowed_tools`` view, so the per-command shims in
    ``analyze_fails`` / ``review_response`` keep selecting their own tool
    set, plus the global ``ai_api`` block.
    """
    if not _api_backend_selected(config):
        return None

    from releasy.api_agent import DEFAULT_MODEL, ApiAgentSpec

    api = getattr(config, "ai_api", None) or AIApiConfig()
    tools = (
        list(allowed_tools) if allowed_tools is not None
        else list(config.ai_resolve.allowed_tools)
    )
    return ApiAgentSpec(
        model=api.model or config.ai_model or DEFAULT_MODEL,
        effort=config.ai_effort,
        max_tokens=api.max_tokens,
        max_turns=api.max_turns,
        thinking=api.thinking,
        allowed_tools=tools,
        api_key=(os.environ.get(api.api_key_env) or api.api_key or None),
        base_url=api.base_url,
        max_retries=api.max_retries,
        request_timeout_seconds=api.request_timeout_seconds,
        bash_timeout_seconds=api.bash_timeout_seconds,
        tool_output_max_chars=api.tool_output_max_chars,
        system_prompt_extra=api.system_prompt_extra,
    )


def _resolve_backend(
    config: Config, command: str, allowed_tools: list[str] | None = None,
) -> tuple["ApiAgentSpec | None", str | None]:
    """Pick the backend and check it can run.

    Returns ``(api_spec, error)``: ``api_spec`` is ``None`` in CLI mode,
    ``error`` is a user-facing reason the backend is unusable (missing
    binary / missing token / missing SDK) or ``None`` when it's good to go.
    """
    spec = _build_api_spec(config, allowed_tools)
    if spec is not None:
        from releasy.api_agent import check_available
        return spec, check_available(spec)
    if shutil.which(command) is None:
        return None, f"'{command}' not found on PATH"
    return None, None


def _backend_label(config: Config, command: str) -> str:
    """How to name the backend in progress output."""
    spec = _build_api_spec(config, allowed_tools=[])
    if spec is None:
        return command
    return f"anthropic api ({spec.resolved_model()})"


def _fmt_elapsed(seconds: float) -> str:
    s = int(seconds)
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m{s % 60:02d}s"
    return f"{s // 3600}h{(s % 3600) // 60:02d}m"


def _flatten(text: str) -> str:
    return text.strip().replace("\n", " ⏎ ")


def _render_event(line: str, start: float) -> str | None:
    """Turn one stream-json event into a human-readable status line.

    Returns ``None`` when the event has nothing user-visible to report.
    """
    try:
        ev = json.loads(line)
    except json.JSONDecodeError:
        stripped = line.strip()
        if not stripped:
            return None
        # ``escape`` everything that originates in claude's stream: its
        # text routinely contains ``[…]`` (diff hunks, doc snippets, paths
        # like ``[/<sub-path>]``) that Rich would otherwise parse as markup
        # and throw ``MarkupError`` on — which used to tear down the whole
        # stream and SIGTERM a healthy claude. Our own ``[dim]`` etc. tags
        # are added *outside* the escaped segments so they still render.
        return f"[dim]│[/dim] {escape(stripped)}"

    elapsed = _fmt_elapsed(time.monotonic() - start)
    etype = ev.get("type")

    if etype == "system":
        sub = ev.get("subtype", "")
        model = ev.get("model") or ""
        if sub == "init":
            return f"[dim]│ [{elapsed}][/dim] [magenta]session start[/magenta] {escape(model)}".rstrip()
        return None

    if etype == "assistant":
        msg = ev.get("message") or {}
        parts = []
        for block in msg.get("content") or []:
            btype = block.get("type")
            if btype == "text":
                txt = _flatten(block.get("text", ""))
                if txt:
                    parts.append(f"[dim]│ [{elapsed}][/dim] [cyan]💬[/cyan] {escape(txt)}")
            elif btype == "thinking":
                txt = _flatten(block.get("thinking", ""))
                if txt:
                    parts.append(f"[dim]│ [{elapsed}] 🧠 {escape(txt)}[/dim]")
            elif btype == "tool_use":
                name = block.get("name", "tool")
                inp = block.get("input") or {}
                summary = ""
                if name == "Bash":
                    summary = _flatten(str(inp.get("command", "")))
                elif name in ("Read", "Write", "Edit"):
                    summary = str(inp.get("file_path") or inp.get("path") or "")
                elif name == "Glob":
                    summary = str(inp.get("pattern") or inp.get("glob_pattern") or "")
                elif name == "Grep":
                    summary = _flatten(str(inp.get("pattern", "")))
                else:
                    keys = ", ".join(list(inp)[:3])
                    summary = f"({keys})" if keys else ""
                line_str = f"[dim]│ [{elapsed}][/dim] [yellow]🔧 {escape(str(name))}[/yellow]"
                if summary:
                    line_str += f" {escape(summary)}"
                parts.append(line_str)
        return "\n".join(parts) if parts else None

    if etype == "user":
        msg = ev.get("message") or {}
        for block in msg.get("content") or []:
            if block.get("type") == "tool_result":
                content = block.get("content")
                if isinstance(content, list):
                    text = " ".join(
                        c.get("text", "") for c in content if isinstance(c, dict)
                    )
                else:
                    text = str(content or "")
                is_err = block.get("is_error")
                marker = "[red]✗[/red]" if is_err else "[green]✓[/green]"
                summary = _flatten(text)
                if not summary:
                    return None
                return f"[dim]│ [{elapsed}][/dim] {marker} [dim]{escape(summary)}[/dim]"
        return None

    if etype == "result":
        sub = ev.get("subtype", "")
        cost = ev.get("total_cost_usd")
        turns = ev.get("num_turns")
        bits = [f"result={escape(sub)}"]
        if turns is not None:
            bits.append(f"turns={turns}")
        if cost is not None:
            bits.append(f"cost=${cost:.3f}")
        return f"[dim]│ [{elapsed}] {' '.join(bits)}[/dim]"

    return None


def _kill_proc_tree(proc: subprocess.Popen) -> None:
    """SIGTERM (then SIGKILL) the entire process group of ``proc``.

    Claude spawns child tools (ninja, gcc, gh, …); without targeting the
    whole group those survive after we kill claude itself. ``proc`` is
    started with ``start_new_session=True`` so it owns its own process
    group whose pgid equals the child pid.
    """
    if proc.poll() is not None:
        return
    try:
        pgid = os.getpgid(proc.pid)
    except (ProcessLookupError, OSError):
        return
    try:
        os.killpg(pgid, signal.SIGTERM)
    except (ProcessLookupError, OSError):
        pass
    try:
        proc.wait(timeout=5)
        return
    except subprocess.TimeoutExpired:
        pass
    try:
        os.killpg(pgid, signal.SIGKILL)
    except (ProcessLookupError, OSError):
        pass
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        pass


# Session-exhaustion wait defaults — baked in so every _spawn_claude caller
# waits by default; config-aware callers override via _exhaustion_kwargs.
_DEFAULT_EXHAUSTION_MAX_WAIT_SECONDS = 60 * 3600  # 60h
_DEFAULT_EXHAUSTION_POLL_SECONDS = 30 * 60  # 30m


def _exhaustion_kwargs(config: Config) -> dict:
    """``_spawn_claude`` session-exhaustion kwargs derived from config."""
    ai = config.ai_resolve
    return {
        "exhaustion_wait": ai.wait_on_session_exhaustion,
        "exhaustion_max_wait_seconds": ai.session_exhaustion_max_wait_hours * 3600,
        "exhaustion_poll_seconds": ai.session_exhaustion_poll_minutes * 60,
        "exhaustion_extra_patterns": tuple(ai.session_exhaustion_extra_patterns),
    }


def _interruptible_sleep(seconds: float) -> None:
    """Sleep ``seconds`` in small chunks (Ctrl-C responsive, 5-min heartbeat)."""
    end = time.monotonic() + seconds
    next_beat = time.monotonic() + 300.0
    while True:
        remaining = end - time.monotonic()
        if remaining <= 0:
            return
        time.sleep(min(15.0, remaining))
        if time.monotonic() >= next_beat:
            mins_left = int((end - time.monotonic()) / 60) + 1
            console.print(
                f"    [dim]│ …waiting for Claude session (~{mins_left}m "
                "left this poll)[/dim]"
            )
            next_beat += 300.0


def _spawn_claude(
    argv: list[str], repo_path: Path, timeout: int,
    *,
    prompt: str,
    api: "ApiAgentSpec | None" = None,
    exhaustion_wait: bool = True,
    exhaustion_max_wait_seconds: int = _DEFAULT_EXHAUSTION_MAX_WAIT_SECONDS,
    exhaustion_poll_seconds: int = _DEFAULT_EXHAUSTION_POLL_SECONDS,
    exhaustion_extra_patterns: tuple[str, ...] = (),
) -> tuple[int, str, bool]:
    """Run the agent, waiting out an exhausted usage session.

    Wraps :func:`_spawn_claude_once` (or :func:`_run_api_agent_once` when
    ``api`` is set — the API backend produces the same stream-json
    transcript, so the wait / retry ladder is backend-agnostic): on a
    session-exhaustion failure (not a transient API error — those retry one
    level up), sleep and re-prompt until it works or the wait cap is hit,
    then return the last result.
    """
    # A non-positive poll interval would busy-loop; treat it as "disabled".
    wait_enabled = (
        exhaustion_wait
        and exhaustion_poll_seconds > 0
        and exhaustion_max_wait_seconds > 0
    )
    waited = 0.0
    while True:
        if api is not None:
            exit_code, output, timed_out = _run_api_agent_once(
                api, repo_path, timeout, prompt,
            )
        else:
            exit_code, output, timed_out = _spawn_claude_once(
                argv, repo_path, timeout, prompt,
            )
        if not wait_enabled or timed_out or exit_code == 0:
            return exit_code, output, timed_out
        reason = _find_session_exhausted(output, exhaustion_extra_patterns)
        if reason is None:
            return exit_code, output, timed_out
        if waited + exhaustion_poll_seconds > exhaustion_max_wait_seconds:
            console.print(
                f"    [red]✗ Claude session still exhausted after "
                f"{_fmt_elapsed(waited)} of waiting "
                f"(cap {_fmt_elapsed(exhaustion_max_wait_seconds)}); giving up"
                f"[/red] [dim]({reason})[/dim]"
            )
            return exit_code, output, timed_out
        console.print(
            f"    [yellow]⏳ Claude session exhausted[/yellow] "
            f"[dim]({reason})[/dim] — sleeping "
            f"{_fmt_elapsed(exhaustion_poll_seconds)}, then retrying "
            f"[dim](waited {_fmt_elapsed(waited)} / "
            f"{_fmt_elapsed(exhaustion_max_wait_seconds)}; Ctrl-C to abort)[/dim]"
        )
        _interruptible_sleep(exhaustion_poll_seconds)
        waited += exhaustion_poll_seconds


def _run_api_agent_once(
    api: "ApiAgentSpec", repo_path: Path, timeout: int, prompt: str,
) -> tuple[int, str, bool]:
    """API-backend twin of :func:`_spawn_claude_once`.

    Runs the agent loop in-process against the Messages API and renders its
    stream-json events through :func:`_render_event`, so the console output
    and the returned transcript are indistinguishable from the CLI path.
    """
    from releasy.api_agent import run_agent

    start = time.monotonic()
    console.print(
        f"    [dim]$ anthropic api {escape(api.resolved_model())}"
        f"{' effort=' + api.effort if api.effort else ''} "
        f"<prompt via messages>[/dim]"
    )
    console.print("    [dim](press Ctrl-C to abort)[/dim]")

    def _render(line: str) -> None:
        rendered = _render_event(line, start)
        if not rendered:
            return
        try:
            console.print(f"    {rendered}")
        except Exception:
            # Same belt-and-suspenders as the CLI path: a Rich markup
            # error must never abort a healthy run.
            console.print(f"    {rendered}", markup=False, highlight=False)

    return run_agent(api, repo_path, timeout, prompt, _render)


def _spawn_claude_once(
    argv: list[str], repo_path: Path, timeout: int, prompt: str,
) -> tuple[int, str, bool]:
    """Run claude as a subprocess, streaming stdout/stderr to the console.

    Parses Claude's stream-json events and pretty-prints each tool call,
    message, and tool result. Emits a heartbeat when claude is quiet so
    the user can tell it's still working. Ctrl-C kills the whole claude
    process group (claude + ninja + whatever else it spawned) and
    re-raises ``KeyboardInterrupt``.

    The prompt goes inline as ``-p <prompt>`` when small, or via stdin when it
    would overflow the OS arg limit (``_PROMPT_ARG_MAX_BYTES``). ``argv`` is
    the base print-mode argv (no prompt) from :func:`_build_claude_argv`.

    Returns (exit_code, combined_output, timed_out).
    """
    use_stdin = len(prompt.encode("utf-8")) > _PROMPT_ARG_MAX_BYTES
    full_argv = argv if use_stdin else _argv_with_inline_prompt(argv, prompt)

    console.print(
        f"    [dim]$ {escape(shlex.join(argv))}"
        f"{' <prompt via stdin>' if use_stdin else ' <prompt…>'}[/dim]"
    )
    console.print("    [dim](press Ctrl-C to abort claude)[/dim]")

    env = os.environ.copy()

    # start_new_session=True puts claude in its own process group so:
    #   1. it does NOT receive the terminal's Ctrl-C (we control it),
    #   2. we can kill the entire tree (claude + nested tools) at once.
    proc = subprocess.Popen(
        full_argv,
        cwd=repo_path,
        env=env,
        stdin=subprocess.PIPE if use_stdin else subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
        start_new_session=True,
    )

    # Feed a large prompt via stdin in a thread so a >pipe-buffer write can't
    # deadlock against us reading stdout. Broken pipe (claude exited early /
    # was killed) is benign.
    if use_stdin:
        def _feed() -> None:
            try:
                assert proc.stdin is not None
                proc.stdin.write(prompt)
                proc.stdin.close()
            except (BrokenPipeError, OSError, ValueError):
                pass
        threading.Thread(target=_feed, daemon=True).start()

    collected: list[str] = []
    start = time.monotonic()
    last_output = start
    timed_out = False
    interrupted = False
    stop_heartbeat = threading.Event()

    def _heartbeat() -> None:
        idle_threshold = 30.0
        while not stop_heartbeat.wait(15.0):
            idle = time.monotonic() - last_output
            if idle >= idle_threshold:
                console.print(
                    f"    [dim]│ [{_fmt_elapsed(time.monotonic() - start)}] "
                    f"…still working (idle {int(idle)}s)[/dim]"
                )

    hb = threading.Thread(target=_heartbeat, daemon=True)
    hb.start()

    try:
        assert proc.stdout is not None
        while True:
            if (time.monotonic() - start) > timeout:
                timed_out = True
                break
            try:
                line = proc.stdout.readline()
            except KeyboardInterrupt:
                interrupted = True
                break
            if not line:
                if proc.poll() is not None:
                    break
                continue
            collected.append(line)
            last_output = time.monotonic()
            rendered = _render_event(line, start)
            if rendered:
                try:
                    console.print(f"    {rendered}")
                except Exception:
                    # Belt-and-suspenders: _render_event already escapes
                    # claude-originated text, but a console-rendering error
                    # must NEVER tear down the stream loop — that would kill
                    # an otherwise-healthy claude (exit 143). Fall back to a
                    # raw, markup-free print of this one line.
                    console.print(
                        f"    {rendered}", markup=False, highlight=False,
                    )
    except KeyboardInterrupt:
        interrupted = True
    except Exception as exc:
        collected.append(f"[runner] error streaming claude output: {exc}\n")
    finally:
        stop_heartbeat.set()
        if proc.poll() is None:
            reason = (
                "interrupted by user"
                if interrupted
                else f"timed out after {timeout}s"
                if timed_out
                else "stream ended; cleaning up"
            )
            console.print(
                f"    [yellow]✗ killing claude ({reason})…[/yellow]"
            )
            _kill_proc_tree(proc)

    if interrupted:
        raise KeyboardInterrupt

    exit_code = proc.returncode if proc.returncode is not None else -1
    return exit_code, "".join(collected), timed_out


# ---------------------------------------------------------------------------
# Post-condition verification
# ---------------------------------------------------------------------------


def _extract_assistant_text(output: str) -> str:
    """Pull all assistant text out of a stream-json transcript.

    Falls back to the raw output when nothing parses as JSON.
    """
    chunks: list[str] = []
    parsed_any = False
    for line in output.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            chunks.append(line)
            continue
        parsed_any = True
        if ev.get("type") == "assistant":
            for block in (ev.get("message") or {}).get("content") or []:
                if block.get("type") == "text":
                    chunks.append(block.get("text", ""))
        elif ev.get("type") == "result":
            res = ev.get("result")
            if isinstance(res, str):
                chunks.append(res)
    if not parsed_any:
        return output
    return "\n".join(chunks)


# Heuristics for transient Anthropic streaming-API failures that abort
# the turn before Claude can do any real work. We retry on these; we do
# NOT retry on real resolver errors (build failed, UNRESOLVED, etc.).
_TRANSIENT_API_ERROR_RES = [
    re.compile(r"API Error:\s*Stream idle timeout", re.IGNORECASE),
    re.compile(r"API Error:\s*Overloaded", re.IGNORECASE),
    re.compile(r"API Error:\s*5\d\d", re.IGNORECASE),
    re.compile(r"API Error:\s*Connection (?:reset|closed)", re.IGNORECASE),
    re.compile(r"API Error:\s*Request timed out", re.IGNORECASE),
    re.compile(r"API Error:\s*ECONNRESET", re.IGNORECASE),
    re.compile(r"API Error:\s*fetch failed", re.IGNORECASE),
    re.compile(r"partial response received", re.IGNORECASE),
]


def _find_transient_api_error(output: str) -> str | None:
    """Return a short human-readable reason if the turn was aborted by a
    transient Anthropic API error, else ``None``.

    We look both in raw lines and in the assistant text extracted from
    the stream-json transcript.
    """
    assistant = _extract_assistant_text(output)
    for haystack in (assistant, output):
        for pat in _TRANSIENT_API_ERROR_RES:
            m = pat.search(haystack)
            if m:
                return m.group(0)
    return None


# Signals that the Claude usage session is spent (warrants the scheduled
# wait, not a 15s retry). Distinct from the transient "API Error: …" patterns
# above. Only consulted on a non-zero exit. NB: the "monthly spend limit ·
# run /usage-credits" wording is the CLI mislabeling a session limit that
# resets — correct to wait out, not a real billing cap.
_SESSION_EXHAUSTED_RES = [
    re.compile(r"\blimit reached\b", re.IGNORECASE),
    re.compile(r"reached your (?:usage |5-hour |weekly |daily )?limit", re.IGNORECASE),
    re.compile(r"\bhit your\b[^\n]*?\blimit\b", re.IGNORECASE),
    re.compile(r"\bspend limit\b", re.IGNORECASE),
    re.compile(r"/usage-credits", re.IGNORECASE),
    re.compile(r"\b(?:usage|rate|spend|\d+-hour|weekly|daily|monthly)\s+limit\b", re.IGNORECASE),
    re.compile(r"\blimit (?:will )?reset", re.IGNORECASE),
    re.compile(r"(?:upgrade|ask your admin)[^\n]*(?:higher|increase|raise)[^\n]*limit", re.IGNORECASE),
    re.compile(r"API Error:\s*429", re.IGNORECASE),
]


def _find_session_exhausted(
    output: str, extra_patterns: tuple[str, ...] = (),
) -> str | None:
    """Short reason if the run failed on an exhausted usage limit, else None.

    Scans the raw transcript only (the limit notice is a CLI message; skipping
    model text avoids false positives). ``extra_patterns`` are user regexes
    OR-ed with the built-ins.
    """
    for pat in _SESSION_EXHAUSTED_RES:
        m = pat.search(output)
        if m:
            return m.group(0)
    for raw in extra_patterns:
        try:
            m = re.search(raw, output, re.IGNORECASE)
        except re.error:
            continue  # a malformed user pattern must not crash the run
        if m:
            return m.group(0)
    return None


def _extract_cost_usd(output: str) -> float | None:
    """Sum ``total_cost_usd`` across every ``result`` event in the transcript.

    Claude emits one ``result``-typed stream-json event per session
    summarising the run. When ``resolve_with_claude`` retries after a
    transient API error, each attempt produces its own result event and
    the costs need to be added together to reflect the full bill for the
    invocation. Returns ``None`` if no usable cost field was found.
    """
    total: float | None = None
    for line in output.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        if ev.get("type") != "result":
            continue
        cost = ev.get("total_cost_usd")
        if isinstance(cost, (int, float)):
            total = (total or 0.0) + float(cost)
    return total


_MISSING_PREREQS_RE = re.compile(
    r"^MISSING_PREREQS:\s*(.+?)\s*$",
    re.MULTILINE,
)
_REASON_RE = re.compile(r"^REASON:\s*(.+?)\s*$", re.MULTILINE)
# A token that *looks like* a GitHub PR URL — broad on purpose so we
# accept variants like ``https://github.com/owner/repo/pull/123#whatever``
# without dropping them. Validation happens downstream via
# ``parse_pr_url``; the parser's job is just to peel them off the
# MISSING_PREREQS line.
_PR_URL_TOKEN_RE = re.compile(
    r"https?://github\.com/[^/\s]+/[^/\s]+/pull/\d+[\w/#?=&.\-]*",
    re.IGNORECASE,
)


def _parse_missing_prereqs(output: str) -> tuple[list[str], str | None]:
    """Extract MISSING_PREREQS PR URLs (and the one-line REASON) from the
    transcript.

    The prompt's contract: Claude prints ``MISSING_PREREQS: url1 url2 ...``
    on one line and ``REASON: <one-liner>`` on the next, followed by
    ``UNRESOLVED`` on its own line. We accept any whitespace separator
    between URLs (space, tab, comma) and trim trailing punctuation.

    Returns ``([], None)`` when no MISSING_PREREQS line was emitted.
    """
    text = _extract_assistant_text(output)
    urls: list[str] = []
    seen: set[str] = set()
    # Walk every MISSING_PREREQS occurrence — there should normally only
    # be one, but if Claude restated it (e.g. once mid-narration and once
    # at the tail) we union them rather than picking arbitrarily.
    for m in _MISSING_PREREQS_RE.finditer(text):
        for tok in _PR_URL_TOKEN_RE.finditer(m.group(1)):
            url = tok.group(0).rstrip(".,;:)]\"'")
            if url not in seen:
                seen.add(url)
                urls.append(url)
    if not urls:
        return [], None
    reason_match = _REASON_RE.search(text)
    reason = reason_match.group(1).strip() if reason_match else None
    return urls, reason


def _count_iterations(output: str) -> int | None:
    """Best-effort: count how many build attempts Claude ran.

    Looks for patterns in its own narration. Returns None when unknown.
    """
    text = _extract_assistant_text(output)
    patterns = [
        r"build attempt[s]?:?\s*(\d+)",
        r"iteration\s*(\d+)\s*/\s*\d+",
    ]
    best: int | None = None
    for pat in patterns:
        for m in re.finditer(pat, text, re.IGNORECASE):
            try:
                n = int(m.group(1))
            except ValueError:
                continue
            best = n if best is None else max(best, n)
    return best


# ``src/Core/SettingsChangesHistory.cpp`` is an append-only registry of
# ClickHouse settings changes. Every conflict-resolve invocation must
# leave the port branch's diff of this file as a **subset** of the
# rows the source PR itself added — extra rows are essentially always
# bad: they're context lines the AI accidentally uncommented or stale
# entries from "ours" that bracketed a real source-PR edit. The rule
# is in the prompt (see resolve_conflict.md → "Append-only registries"),
# but PR #1812 showed prompt-only enforcement isn't reliable, so we
# also verify mechanically here.
_SETTINGS_HISTORY_FILE = "src/Core/SettingsChangesHistory.cpp"

# Matches ``+    {"setting_name", ...`` — the canonical row shape of
# this registry. Captures the setting name. We anchor on ``+`` so we
# only see additions; deletions and context lines never contribute.
_SETTINGS_HISTORY_ROW_RE = re.compile(r'^\+\s*\{"([^"]+)"')


def _settings_history_added_names(stdout: str) -> set[str]:
    """Pull setting names out of a unified-diff stdout snippet."""
    return {
        m.group(1) for line in stdout.splitlines()
        if (m := _SETTINGS_HISTORY_ROW_RE.match(line))
    }


def _check_settings_history_whitelist(
    repo_path: Path, ctx: AIResolveContext, new_head: str,
) -> tuple[bool, str | None]:
    """Verify the port's SettingsChangesHistory.cpp adds ⊆ source PR's adds.

    No-op when we can't compute either side (missing start / source SHA,
    file untouched by the port). On violation, returns a specific error
    naming the unauthorized setting(s); the caller surfaces it like any
    other postcondition failure and resets to ``ctx.start_sha``.
    """
    source_sha = ctx.source_pr.merge_commit_sha or ctx.source_pr.head_sha
    if not source_sha or not ctx.start_sha:
        return True, None

    on_port = run_git(
        ["diff", "--no-color", f"{ctx.start_sha}..{new_head}",
         "--", _SETTINGS_HISTORY_FILE],
        repo_path, check=False,
    )
    if on_port.returncode != 0 or not on_port.stdout.strip():
        return True, None  # file untouched by the port

    added_on_port = _settings_history_added_names(on_port.stdout)
    if not added_on_port:
        return True, None

    # ``-m --first-parent`` matches how the prompt itself tells Claude
    # to read the source PR's diff — see resolve_conflict.md:352.
    src = run_git(
        ["show", "-m", "--first-parent", "--no-color", source_sha,
         "--", _SETTINGS_HISTORY_FILE],
        repo_path, check=False,
    )
    allowed = (
        _settings_history_added_names(src.stdout) if src.returncode == 0
        else set()
    )

    extra = added_on_port - allowed
    if not extra:
        return True, None

    sample = ", ".join(sorted(extra)[:5])
    more = f" (+{len(extra) - 5} more)" if len(extra) > 5 else ""
    return False, (
        f"{_SETTINGS_HISTORY_FILE}: {len(extra)} unauthorized setting "
        f"row(s) added during resolution: {sample}{more} — these names "
        "are not in the source PR's diff for this file. This registry "
        "is append-only: only rows the source PR itself adds may land "
        "in the port. Re-resolve and drop the extras (likely context "
        "lines from \"ours\" that got swept in alongside a real edit)."
    )


# Postcondition failures whose ``err_kind`` may be handed back to Claude for
# an in-place correction (bounded by ``ai_resolve.postcondition_retries``)
# instead of discarding the whole resolution. These are blemishes on an
# otherwise-good resolve — never "claude didn't finish" signals.
_CORRECTABLE_POSTCONDITIONS = {"settings_history"}


# Focused follow-up prompt for the ``settings_history`` postcondition. The
# resolution is already committed; Claude only trims the unauthorized rows
# and amends. Placeholders are filled with the same ``\{ident\}`` re.sub used
# by :func:`_render_prompt`; literal ``{"..."}`` registry rows are left alone
# because the char after ``{`` isn't an identifier.
_SETTINGS_HISTORY_FIX_PROMPT = """\
You are fixing ONE specific problem in an ALREADY-COMPLETED cherry-pick. Do not start over.

## State
- Repo: {cwd}
- Port branch `{port_branch}` is checked out; the working tree is clean.
- The cherry-pick of {source_pr_url} is ALREADY resolved and committed at HEAD.
  Do NOT run `git cherry-pick`, `git reset`, `git rebase`, `git merge`, or check out any other ref.
- Source PR merge/commit SHA: `{source_pr_merge_sha}`
- Port base (HEAD before the pick): `{start_sha}`

## Problem
RelEasy's post-resolution check rejected the landed resolution:

> {err}

`src/Core/SettingsChangesHistory.cpp` is an APPEND-ONLY registry. The rows your
port ADDS to this file must be a SUBSET of the rows the SOURCE PR's own diff
adds. The flagged rows above are almost always context / "ours" lines that got
swept in alongside a real edit — they are NOT part of this port.

## Fix
1. List the rows the SOURCE PR legitimately adds to this file:
   ```
   git show -m --first-parent --no-color {source_pr_merge_sha} -- src/Core/SettingsChangesHistory.cpp
   ```
2. List the rows your port currently adds:
   ```
   git diff --no-color {start_sha}..HEAD -- src/Core/SettingsChangesHistory.cpp
   ```
3. Edit `src/Core/SettingsChangesHistory.cpp` and DELETE every added
   {"setting_name", ...} row whose setting name is NOT in the source PR's
   additions from step 1. Keep the legitimately-added rows exactly where they
   belong (in the correct version block). Change NOTHING else; touch NO other file.
4. Keep the file valid C++ (balanced braces, no dangling/missing commas in the
   initializer). A full rebuild is NOT required for this registry-only edit.
5. Fold the fix into the existing resolution commit — do not create a new one:
   ```
   git add -- src/Core/SettingsChangesHistory.cpp
   git commit --amend --no-edit
   ```

When done, print `DONE`. If the complaint is genuinely wrong (every flagged row
really is in the source PR's diff from step 1), print `DONE` without editing and
RelEasy will re-verify.
"""


def _render_correction_prompt(
    config: Config, repo_path: Path, ctx: AIResolveContext,
    err_kind: str, err: str | None,
) -> str:
    """Render the focused follow-up prompt for a correctable postcondition.

    Raises ``ValueError`` for an ``err_kind`` we have no correction prompt
    for, so :func:`resolve_with_claude` falls back to plain failure.
    """
    if err_kind != "settings_history":
        raise ValueError(f"no correction prompt for postcondition {err_kind!r}")

    source_sha = (
        ctx.source_pr.merge_commit_sha or ctx.source_pr.head_sha or ""
    )
    placeholders = {
        "cwd": str(repo_path),
        "port_branch": ctx.port_branch,
        "source_pr_url": ctx.source_pr.url,
        "source_pr_merge_sha": source_sha,
        "start_sha": ctx.start_sha or "",
        "err": (err or "").strip(),
    }

    def _replace(match: re.Match[str]) -> str:
        return placeholders.get(match.group(1), match.group(0))

    return re.sub(
        r"\{([a-zA-Z_][a-zA-Z0-9_]*)\}", _replace, _SETTINGS_HISTORY_FIX_PROMPT,
    )


def _verify_postconditions(
    config: Config, repo_path: Path, ctx: AIResolveContext,
) -> tuple[bool, str | None, str | None, str | None]:
    """Step-mode check: cherry-pick fully concluded, tree clean, HEAD moved.

    RelEasy itself owns push / PR / label, so we only verify Claude's
    local-repo invariants:

    - no cherry-pick / merge / rebase still in progress,
    - working tree is clean (no unstaged conflict-resolution leftovers),
    - the port branch advanced past ``ctx.start_sha`` (i.e. at least one
      commit was actually made),
    - in split mode, additionally HEAD has advanced past
      ``ctx.pre_resolve_sha`` so the resolution lives in its own commit
      on top of the "with conflicts" commit (Claude must not have
      amended that one), and HEAD is not a fixup of HEAD~1 — there
      really is a separate resolution commit,
    - any additions to ``src/Core/SettingsChangesHistory.cpp`` are a
      subset of the rows the source PR's own diff adds — see
      :func:`_check_settings_history_whitelist`.

    Returns ``(ok, new_head_sha, error_message, err_kind)``. ``err_kind`` is
    a stable tag for the failure mode — ``None`` on success or for failures
    that aren't worth re-prompting Claude about, and a member of
    :data:`_CORRECTABLE_POSTCONDITIONS` (e.g. ``"settings_history"``) when
    the resolution is otherwise good but trips a fixable content check that
    :func:`resolve_with_claude` can hand back to Claude.
    """
    if is_operation_in_progress(repo_path):
        return False, None, (
            "cherry-pick/merge/rebase still in progress after claude exited"
        ), None

    # Look only for **unmerged paths** — the unambiguous signal that the
    # cherry-pick wasn't finished. Other dirt (modified/staged/deleted
    # tracked files, untracked scratch in tmp/ or build/) is noise from
    # build steps, generated headers, server runtime data, etc. — it
    # does not invalidate a cherry-pick that was already committed
    # (and the HEAD-advanced check below independently confirms the
    # commit actually happened). Failing on dirty tmp files would
    # reject legitimate resolutions, which is exactly the bug we hit on
    # 2026-05-19 with the Iceberg PR #90740 port.
    porc = run_git(
        ["status", "--porcelain", "--untracked-files=no"],
        repo_path, check=False,
    )
    unmerged = [
        line for line in porc.stdout.splitlines()
        if len(line) >= 2 and (
            line[0] == "U" or line[1] == "U"
            or line[:2] in {"AA", "DD"}
        )
    ]
    if unmerged:
        files = ", ".join(line[3:] for line in unmerged[:5])
        return False, None, f"unmerged paths after claude: {files}", None

    head = run_git(["rev-parse", "--verify", "HEAD"], repo_path, check=False)
    if head.returncode != 0:
        return False, None, "could not read HEAD", None
    new_head = head.stdout.strip()

    if ctx.start_sha and new_head == ctx.start_sha:
        return False, new_head, (
            "no new commits — cherry-pick was not concluded"
        ), None

    if ctx.split_mode and ctx.pre_resolve_sha and new_head == ctx.pre_resolve_sha:
        return False, new_head, (
            "no resolution commit on top of the 'with conflicts' commit "
            "— claude did not produce a second commit"
        ), None

    ok, sh_err = _check_settings_history_whitelist(repo_path, ctx, new_head)
    if not ok:
        return False, new_head, sh_err, "settings_history"

    return True, new_head, None, None


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def _invoke_claude_with_retries(
    config: Config, repo_path: Path, prompt: str,
    *, timeout: int | None = None, allowed_tools: list[str] | None = None,
) -> tuple[int, str, bool, float | None]:
    """Run claude on ``prompt``, retrying on transient Anthropic API errors.

    Returns ``(exit_code, output, timed_out, cost_usd)`` for the final
    attempt. Cost is summed across attempts — each retry is a separately
    billed turn even when only the last one succeeds. Shared by the main
    resolve and the postcondition-correction passes so both honour
    ``api_retries`` / backoff identically.

    ``timeout`` / ``allowed_tools`` override the resolve defaults (used by
    ``build_verify`` for the run-tests step).
    """
    spawn_timeout = (
        timeout if timeout is not None else config.ai_resolve.timeout_seconds
    )
    argv = _build_claude_argv(config, allowed_tools)
    api = _build_api_spec(config, allowed_tools)
    max_attempts = max(1, config.ai_resolve.api_retries + 1)
    backoff = max(0, config.ai_resolve.api_retry_backoff_seconds)

    last_exit_code = -1
    last_output = ""
    last_timed_out = False
    cost_usd_total: float | None = None

    for attempt in range(1, max_attempts + 1):
        if attempt > 1:
            console.print(
                f"    [yellow]↻ retrying claude after transient API error "
                f"(attempt {attempt}/{max_attempts}, sleeping {backoff}s)"
                f"[/yellow]"
            )
            try:
                time.sleep(backoff)
            except KeyboardInterrupt:
                raise

        exit_code, output, timed_out = _spawn_claude(
            argv, repo_path, spawn_timeout, prompt=prompt, api=api,
            **_exhaustion_kwargs(config),
        )
        last_exit_code, last_output, last_timed_out = exit_code, output, timed_out

        attempt_cost = _extract_cost_usd(output)
        if attempt_cost is not None:
            cost_usd_total = (cost_usd_total or 0.0) + attempt_cost

        if timed_out:
            break

        transient = _find_transient_api_error(output)
        if transient and exit_code != 0 and attempt < max_attempts:
            console.print(
                f"    [yellow]⚠ transient API error: {transient}"
                f" — will retry[/yellow]"
            )
            continue

        break

    return last_exit_code, last_output, last_timed_out, cost_usd_total


def resolve_with_claude(
    config: Config, repo_path: Path, ctx: AIResolveContext,
) -> AIResolveResult:
    """Render the prompt, run claude, and verify post-conditions."""
    _api, backend_error = _resolve_backend(config, config.ai_resolve.command)
    if backend_error:
        return AIResolveResult(
            success=False, error=backend_error, api_aborted=True,
        )

    # In resolve-only mode build_verify owns the build wrapper.
    if not ctx.skip_build:
        try:
            _write_build_script(repo_path, config.ai_resolve.build_command)
        except OSError as exc:
            return AIResolveResult(
                success=False, error=f"could not write build wrapper: {exc}",
                api_aborted=True,
            )

    try:
        prompt = _render_prompt(config, repo_path, ctx)
    except FileNotFoundError as exc:
        return AIResolveResult(success=False, error=str(exc), api_aborted=True)

    build_note = (
        "resolve only, no build" if ctx.skip_build
        else f"max {config.ai_resolve.max_iterations} build attempts"
    )
    console.print(
        f"    [magenta]\U0001f916 invoking "
        f"{_backend_label(config, config.ai_resolve.command)} "
        f"(timeout {config.ai_resolve.timeout_seconds}s, {build_note}, "
        f"up to {config.ai_resolve.api_retries} API-error retries)[/magenta]"
    )

    max_attempts = max(1, config.ai_resolve.api_retries + 1)
    exit_code, output, timed_out, cost_usd_total = _invoke_claude_with_retries(
        config, repo_path, prompt,
    )

    iterations = _count_iterations(output)
    assistant_text = _extract_assistant_text(output)

    if timed_out:
        return AIResolveResult(
            success=False, timed_out=True, iterations=iterations,
            error=f"claude timed out after {config.ai_resolve.timeout_seconds}s",
            cost_usd=cost_usd_total,
        )

    tail_lines = assistant_text.strip().splitlines()[-40:] if assistant_text.strip() else []
    tail_str = "\n".join(tail_lines)

    # Check MISSING_PREREQS *before* the generic UNRESOLVED check: the prompt
    # contract is "MISSING_PREREQS: ... ; REASON: ... ; UNRESOLVED" together,
    # so an UNRESOLVED tail line by itself doesn't disambiguate the two.
    # Scan the full transcript (not just the tail) because Claude sometimes
    # emits the structured marker mid-narration before its closing summary.
    missing_prereq_prs, missing_prereq_note = _parse_missing_prereqs(output)
    if missing_prereq_prs:
        return AIResolveResult(
            success=False, iterations=iterations,
            error="claude reported MISSING_PREREQS",
            cost_usd=cost_usd_total,
            missing_prereq_prs=missing_prereq_prs,
            missing_prereq_note=missing_prereq_note,
        )
    if any(line.strip() == "UNRESOLVED" for line in tail_lines):
        return AIResolveResult(
            success=False, iterations=iterations,
            error="claude reported UNRESOLVED",
            cost_usd=cost_usd_total,
        )
    if any(line.strip() == "BUILD FAILED" for line in tail_lines):
        return AIResolveResult(
            success=False, iterations=iterations,
            error="claude reported BUILD FAILED",
            cost_usd=cost_usd_total,
        )

    if exit_code != 0:
        transient = _find_transient_api_error(output)
        suffix = (
            f" (transient API error after {max_attempts} attempt(s): {transient})"
            if transient
            else ""
        )
        no_billed_work = not iterations and not cost_usd_total
        return AIResolveResult(
            success=False, iterations=iterations,
            error=f"claude exited with code {exit_code}{suffix}\n{tail_str}",
            cost_usd=cost_usd_total,
            api_aborted=bool(transient) or no_billed_work,
        )

    ok, new_head, err, err_kind = _verify_postconditions(config, repo_path, ctx)

    # Corrective re-resolution: a content-correctable postcondition (today
    # only the append-only SettingsChangesHistory.cpp whitelist) is a
    # fixable blemish on an otherwise-good resolution. Rather than discard
    # the whole resolve (the caller hard-resets to start_sha on failure),
    # hand the exact error back to Claude and let it trim the offending file
    # in place. The resolution is still committed on the branch here, so the
    # follow-up amends it. Bounded by ``postcondition_retries``.
    fix_passes = max(0, config.ai_resolve.postcondition_retries)
    pass_no = 0
    while (
        not ok
        and err_kind in _CORRECTABLE_POSTCONDITIONS
        and pass_no < fix_passes
    ):
        try:
            fix_prompt = _render_correction_prompt(
                config, repo_path, ctx, err_kind, err,
            )
        except ValueError:
            break  # no correction prompt for this kind — fail as usual

        # Count (and announce) only passes that actually invoke Claude, so
        # ``pass_no`` is an accurate attempt count for the diagnostics below.
        pass_no += 1
        console.print(
            f"    [yellow]↻ postcondition '{err_kind}' failed — asking "
            f"{_backend_label(config, config.ai_resolve.command)} "
            "to correct it in place "
            f"(pass {pass_no}/{fix_passes})[/yellow]"
        )

        fc, fout, fto, fcost = _invoke_claude_with_retries(
            config, repo_path, fix_prompt,
        )
        if fcost is not None:
            cost_usd_total = (cost_usd_total or 0.0) + fcost
        if fto:
            return AIResolveResult(
                success=False, timed_out=True, iterations=iterations,
                new_head=new_head, cost_usd=cost_usd_total,
                error=(
                    f"claude timed out after {config.ai_resolve.timeout_seconds}s "
                    f"correcting postcondition '{err_kind}' (pass {pass_no})"
                ),
            )
        # A transient API error means the turn was dropped before Claude
        # could act — re-prompting just burns the rest of the budget, so
        # bail now with a diagnostic that names the real cause (the main
        # resolve path does the same on a non-zero exit). For a non-transient
        # exit we still re-verify: Claude may have committed the fix and then
        # exited non-zero on an unrelated late step.
        if fc != 0:
            transient = _find_transient_api_error(fout)
            if transient:
                return AIResolveResult(
                    success=False, iterations=iterations, new_head=new_head,
                    cost_usd=cost_usd_total,
                    error=(
                        f"transient API error correcting postcondition "
                        f"'{err_kind}' (pass {pass_no}): {transient}"
                    ),
                )
        ok, new_head, err, err_kind = _verify_postconditions(
            config, repo_path, ctx,
        )

    if not ok:
        # Make it clear the corrective loop ran, so the surfaced error isn't
        # mistaken for an un-attempted first-pass failure.
        if pass_no:
            err = f"{err}\n(still failing after {pass_no} correction pass(es))"
        return AIResolveResult(
            success=False, iterations=iterations, new_head=new_head, error=err,
            cost_usd=cost_usd_total,
        )

    return AIResolveResult(
        success=True, iterations=iterations, new_head=new_head, error=err,
        cost_usd=cost_usd_total,
    )


# ---------------------------------------------------------------------------
# High-level wrapper used by every caller (cherry-pick + merge resolvers)
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Stand-alone text generation (changelog entry synthesis)
# ---------------------------------------------------------------------------


@dataclass
class AITextResult:
    """Outcome of a one-shot Claude text-generation call.

    Used by callers (e.g. CHANGELOG entry synthesis) that just want a
    short text back without any tool use or post-condition checks.
    """
    success: bool
    text: str | None = None
    error: str | None = None
    timed_out: bool = False
    cost_usd: float | None = None


def _resolve_changelog_prompt_path(config: Config) -> Path:
    """Resolve the changelog-synthesis prompt template path.

    Same convention as :func:`_resolve_prompt_template`: relative paths
    are anchored at ``config.repo_dir`` so per-project overrides drop
    into a ``prompts/`` directory next to ``config.yaml``.
    """
    raw = config.ai_changelog.prompt_file
    p = Path(raw)
    if not p.is_absolute():
        p = (config.repo_dir / p).resolve()
    return p


def synthesize_text(
    config: Config,
    prompt: str,
    *,
    label: str,
    timeout_seconds: int,
    command: str,
) -> AITextResult:
    """Run Claude on ``prompt`` and return the assistant's reply text.

    No tools are made available — this is pure text generation. Cost is
    extracted from the stream-json transcript when present; the call is
    routed through :func:`_spawn_claude` so the user sees the same
    streaming heartbeat / Ctrl-C semantics as the conflict resolver.

    The CWD passed to Claude is a throwaway temp dir so it has nowhere
    interesting to write into even if the model misinterprets the
    no-tools constraint.
    """
    # Empty allow-list keeps this call in pure text-generation mode on both
    # backends (``--allowedTools ""`` for the CLI, no tool definitions for
    # the API).
    api, backend_error = _resolve_backend(config, command, allowed_tools=[])
    if backend_error:
        return AITextResult(success=False, error=backend_error)

    argv = [
        command,
        "-p",
        "--output-format", "stream-json",
        "--verbose",
        "--allowedTools", "",
    ]
    argv += _model_effort_args(config)

    console.print(
        f"    [magenta]\U0001f916 synthesizing text via "
        f"{_backend_label(config, command)} for [cyan]{label}[/cyan] "
        f"(timeout {timeout_seconds}s)[/magenta]"
    )

    import tempfile

    with tempfile.TemporaryDirectory(prefix="releasy-ai-text-") as td:
        try:
            exit_code, output, timed_out = _spawn_claude(
                argv, Path(td), timeout_seconds, prompt=prompt, api=api,
                **_exhaustion_kwargs(config),
            )
        except KeyboardInterrupt:
            raise

    cost = _extract_cost_usd(output)

    if timed_out:
        return AITextResult(
            success=False, timed_out=True, cost_usd=cost,
            error=f"claude timed out after {timeout_seconds}s",
        )

    if exit_code != 0:
        transient = _find_transient_api_error(output)
        suffix = f" ({transient})" if transient else ""
        return AITextResult(
            success=False, cost_usd=cost,
            error=f"claude exited with code {exit_code}{suffix}",
        )

    text = _extract_assistant_text(output).strip()
    if not text:
        return AITextResult(
            success=False, cost_usd=cost,
            error="claude returned empty output",
        )

    return AITextResult(success=True, text=text, cost_usd=cost)


def synthesize_changelog_entry(
    config: Config,
    *,
    unit_label: str,
    pr_blocks: str,
    n_prs: int,
    base_branch: str,
    source_repo: str,
) -> AITextResult:
    """Render the changelog-synthesis prompt and ask Claude to fill it.

    Caller (``releasy.pipeline``) is responsible for building
    ``pr_blocks`` — a markdown chunk containing each source PR's
    title + body in cherry-pick order, already truncated to a
    reasonable size — so the prompt template stays decoupled from the
    project-specific PR-info dataclass.
    """
    prompt_path = _resolve_changelog_prompt_path(config)
    if not prompt_path.exists():
        return AITextResult(
            success=False,
            error=(
                "changelog-synthesis prompt template not found: "
                f"{prompt_path}. Set ai_changelog.prompt_file in config "
                "to point at a real file (or copy the bundled "
                "prompts/synthesize_changelog.md alongside config.yaml)."
            ),
        )

    template = prompt_path.read_text(encoding="utf-8")
    placeholders = {
        "n_prs": str(n_prs),
        "base_branch": base_branch,
        "source_repo": source_repo,
        "pr_blocks": pr_blocks,
    }

    def _replace(match: re.Match[str]) -> str:
        key = match.group(1)
        return placeholders.get(key, match.group(0))

    rendered = re.sub(r"\{([a-zA-Z_][a-zA-Z0-9_]*)\}", _replace, template)

    return synthesize_text(
        config, rendered,
        label=unit_label,
        timeout_seconds=config.ai_changelog.timeout_seconds,
        command=config.ai_changelog.command,
    )


def attempt_ai_resolve(
    config: Config, repo_path: Path, ctx: AIResolveContext,
) -> AIResolveResult:
    """Render the prompt, run claude, and clean up on failure.

    Wraps :func:`resolve_with_claude` with the cleanup contract every
    caller needs: on a failed resolve the in-progress git operation
    (cherry-pick / merge / rebase) is aborted and HEAD is hard-reset to
    ``ctx.start_sha`` so the working tree is back to a known-good state.
    Callers (the cherry-pick step, the PR-merge updater) only need to
    decide *what to do* on success/failure, not *how to clean up*.

    If ``ctx.start_sha`` isn't set the helper fills it from the current
    HEAD before invoking Claude, so PR-conflict-resolution callers don't
    have to remember to capture it themselves.
    """
    if ctx.start_sha is None:
        head = run_git(["rev-parse", "--verify", "HEAD"], repo_path, check=False)
        ctx.start_sha = head.stdout.strip() if head.returncode == 0 else None

    result = resolve_with_claude(config, repo_path, ctx)

    if result.success:
        return result

    # Reset the worktree so the caller can decide what to do next without
    # tripping over half-baked merge / cherry-pick state.
    if is_operation_in_progress(repo_path):
        run_git(["cherry-pick", "--abort"], repo_path, check=False)
        run_git(["merge", "--abort"], repo_path, check=False)
        run_git(["rebase", "--abort"], repo_path, check=False)
    if ctx.start_sha:
        run_git(["reset", "--hard", ctx.start_sha], repo_path, check=False)

    return result


# Post-resolve verification (advisory): a read-only second Claude pass
# that diffs the landed resolution against the source PR. Findings drive
# a label + PR comment; never rolls back.

# Read-only allowlist; Edit/Write would defeat the audit's purpose.
_VERIFY_ALLOWED_TOOLS = (
    "Read", "Glob", "Grep",
    "Bash(git:*)", "Bash(gh:*)",
    "Bash(ls:*)", "Bash(cat:*)", "Bash(head:*)", "Bash(tail:*)",
    "Bash(rg:*)", "Bash(wc:*)",
)


VerifyVerdict = Literal["ok", "needs_attention", "unknown"]


@dataclass
class VerifyContext:
    port_branch: str
    base_branch: str
    source_pr: PRInfo
    # The AI's work lives in start_sha..new_head (1 or 2 commits).
    start_sha: str
    new_head: str
    conflict_files: list[str] = field(default_factory=list)
    user_context: str = ""
    mode: PortMode = "forward_port"


@dataclass
class VerifyResult:
    # ``success`` is True iff the verifier RAN cleanly (any verdict);
    # False = timeout / exit-code error / malformed transcript.
    success: bool
    verdict: VerifyVerdict = "unknown"
    summary: str = ""
    findings: list[str] = field(default_factory=list)
    error: str | None = None
    timed_out: bool = False
    cost_usd: float | None = None


def _resolve_verify_prompt_path(config: Config) -> Path:
    raw = config.ai_resolve.verify_prompt_file
    p = Path(raw)
    if not p.is_absolute():
        p = (config.repo_dir / p).resolve()
    return p


def _render_verify_prompt(config: Config, repo_path: Path, ctx: VerifyContext) -> str:
    prompt_path = _resolve_verify_prompt_path(config)
    if not prompt_path.exists():
        raise FileNotFoundError(
            f"Verifier prompt template not found: {prompt_path}. "
            "Set ai_resolve.verify_prompt_file in config."
        )

    template = prompt_path.read_text(encoding="utf-8")

    from releasy.github_ops import get_origin_repo_slug
    repo_slug = get_origin_repo_slug(config) or "<unknown>"

    conflict_files_md = "\n".join(f"- `{f}`" for f in ctx.conflict_files) or "- (none)"

    body = (ctx.source_pr.body or "").strip()
    if not body:
        body = "_(empty)_"
    elif len(body) > 4000:
        body = body[:4000] + "\n\n_(truncated)_"

    source_pr_merge_sha = (
        ctx.source_pr.merge_commit_sha or ctx.source_pr.head_sha or ""
    )

    user_context_text = (ctx.user_context or "").strip()
    if user_context_text:
        user_context_section = (
            "\n## User-supplied context (from session.yaml)\n\n"
            "> The operator attached this note to this PR / group when "
            "configuring the run. Treat it as a hint about the resolver's "
            "intent, not a license to relax the in-scope rule.\n\n"
            f"{user_context_text}\n"
        )
    else:
        user_context_section = ""

    placeholders = {
        "repo_slug": repo_slug,
        "cwd": str(repo_path),
        "port_branch": ctx.port_branch,
        "base_branch": ctx.base_branch,
        "source_pr_url": ctx.source_pr.url,
        "source_pr_title": ctx.source_pr.title,
        "source_pr_number": str(ctx.source_pr.number),
        "source_pr_body": body,
        "source_pr_merge_sha": source_pr_merge_sha,
        "start_sha": ctx.start_sha,
        "new_head": ctx.new_head,
        "conflict_files": conflict_files_md,
        "user_context_section": user_context_section,
        "port_direction": ctx.mode,
        "source_pr_labels": ", ".join(
            l for l in (ctx.source_pr.labels or []) if l
        ),
    }

    def _replace(match: re.Match[str]) -> str:
        key = match.group(1)
        return placeholders.get(key, match.group(0))

    return re.sub(r"\{([a-zA-Z_][a-zA-Z0-9_]*)\}", _replace, template)


# Match each verdict line; last occurrence wins so mid-run rephrasings
# can't pin us to a stale answer.
_VERIFY_VERDICT_RE = re.compile(
    r"^\s*VERDICT\s*:\s*(OK|NEEDS_ATTENTION)\s*$",
    re.IGNORECASE | re.MULTILINE,
)
_VERIFY_SUMMARY_RE = re.compile(
    r"^\s*SUMMARY\s*:\s*(.+?)\s*$", re.IGNORECASE | re.MULTILINE,
)
_VERIFY_END_RE = re.compile(r"^\s*END_VERIFY\s*$", re.IGNORECASE | re.MULTILINE)


def _parse_verify_output(output: str) -> tuple[VerifyVerdict, str, list[str]]:
    """Parse VERDICT / SUMMARY / FINDINGS from a transcript.

    Returns ``("unknown", "", [])`` on a malformed transcript so the
    caller can downgrade to advisory-only.
    """
    text = _extract_assistant_text(output)

    verdict: VerifyVerdict = "unknown"
    verdict_matches = list(_VERIFY_VERDICT_RE.finditer(text))
    if verdict_matches:
        raw = verdict_matches[-1].group(1).strip().upper()
        if raw == "OK":
            verdict = "ok"
        elif raw == "NEEDS_ATTENTION":
            verdict = "needs_attention"

    summary = ""
    summary_matches = list(_VERIFY_SUMMARY_RE.finditer(text))
    if summary_matches:
        summary = summary_matches[-1].group(1).strip()

    findings: list[str] = []
    # Scan the last FINDINGS: block before END_VERIFY (or EOF).
    findings_block = ""
    findings_idx = text.lower().rfind("findings:")
    if findings_idx >= 0:
        tail = text[findings_idx + len("findings:"):]
        end_match = _VERIFY_END_RE.search(tail)
        findings_block = tail[: end_match.start()] if end_match else tail
    for line in findings_block.splitlines():
        line = line.strip()
        if not line:
            continue
        for prefix in ("- ", "* ", "• "):
            if line.startswith(prefix):
                line = line[len(prefix):].strip()
                break
        else:
            m = re.match(r"^\d+\.\s+(.+)$", line)
            if m:
                line = m.group(1).strip()
            else:
                # First non-bullet line ends the block (stops capturing
                # trailing narration as findings).
                break
        if not line or line.lower() == "(none)":
            continue
        findings.append(line)

    return verdict, summary, findings


def verify_ai_resolution(
    config: Config, repo_path: Path, ctx: VerifyContext,
) -> VerifyResult:
    """Run the advisory verifier; never mutates the repo or remote."""
    api, backend_error = _resolve_backend(
        config, config.ai_resolve.command, list(_VERIFY_ALLOWED_TOOLS),
    )
    if backend_error:
        return VerifyResult(success=False, error=backend_error)

    try:
        prompt = _render_verify_prompt(config, repo_path, ctx)
    except FileNotFoundError as exc:
        return VerifyResult(success=False, error=str(exc))

    argv = [
        config.ai_resolve.command,
        "-p",
        "--output-format", "stream-json",
        "--verbose",
        "--allowedTools", ",".join(_VERIFY_ALLOWED_TOOLS),
    ]
    argv += _model_effort_args(config)
    argv += list(config.ai_resolve.extra_args)

    console.print(
        "    [magenta]\U0001f50e verifying AI resolution "
        f"(timeout {config.ai_resolve.verify_timeout_seconds}s, read-only)[/magenta]"
    )

    try:
        exit_code, output, timed_out = _spawn_claude(
            argv, repo_path, config.ai_resolve.verify_timeout_seconds,
            prompt=prompt, api=api, **_exhaustion_kwargs(config),
        )
    except KeyboardInterrupt:
        raise

    cost = _extract_cost_usd(output)

    if timed_out:
        return VerifyResult(
            success=False, timed_out=True, cost_usd=cost,
            error=f"verifier timed out after {config.ai_resolve.verify_timeout_seconds}s",
        )

    if exit_code != 0:
        transient = _find_transient_api_error(output)
        suffix = f" ({transient})" if transient else ""
        return VerifyResult(
            success=False, cost_usd=cost,
            error=f"verifier exited with code {exit_code}{suffix}",
        )

    verdict, summary, findings = _parse_verify_output(output)
    if verdict == "unknown":
        return VerifyResult(
            success=False, cost_usd=cost, verdict=verdict,
            summary=summary, findings=findings,
            error="verifier did not emit a parsable VERDICT line",
        )

    return VerifyResult(
        success=True, verdict=verdict, summary=summary,
        findings=findings, cost_usd=cost,
    )
