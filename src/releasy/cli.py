"""CLI entry point using Click."""

from __future__ import annotations

import secrets
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

import click

from releasy import __version__
from releasy.config import (
    Config,
    default_session_stem,
    state_file_path,
    state_root,
    validate_project_name,
)

# (session path, warning) pairs already printed in this process.
_PRINTED_LOAD_WARNINGS: set[tuple[str, str]] = set()


def _load_config_or_exit(config_path: str | None = None) -> Config:
    from releasy.config import load_config

    path = Path(config_path) if config_path else None
    try:
        return load_config(path)
    except FileNotFoundError as e:
        raise click.ClickException(str(e))
    except Exception as e:
        raise click.ClickException(f"Failed to load config: {e}")


def _attach_session(
    config: Config,
    session_file_override: str | None,
    *,
    required: bool,
) -> None:
    """Load the session into ``config.session``.

    A missing file is an error when ``required`` or when ``--session-file``
    named it explicitly; otherwise ``config.session`` is left ``None``.
    """
    from releasy.config import load_session

    override = Path(session_file_override) if session_file_override else None
    if override is not None and not override.exists():
        raise click.ClickException(f"Session file not found: {override}")
    try:
        config.session = load_session(config, override)
    except FileNotFoundError as e:
        if required:
            raise click.ClickException(str(e))
        config.session = None
    except Exception as e:
        raise click.ClickException(f"Failed to load session: {e}")

    if config.session is not None:
        session_key = (
            str(config.session.session_path)
            if config.session.session_path else ""
        )
        for w in config.session.load_warnings:
            key = (session_key, w)
            if key in _PRINTED_LOAD_WARNINGS:
                continue
            _PRINTED_LOAD_WARNINGS.add(key)
            click.echo(f"warning: {w}", err=True)


def _load_and_verify(
    ctx: click.Context, *, session: str = "optional",
) -> Config:
    """Load config, verify state ownership, attach session.

    ``session``: ``"required"``, ``"optional"`` or ``"skip"``.
    """
    from releasy.state import OwnershipCollisionError, verify_ownership

    config = _load_config_or_exit(ctx.obj["config_path"])
    try:
        verify_ownership(config)
    except OwnershipCollisionError as e:
        raise click.ClickException(str(e))
    if session != "skip":
        _attach_session(
            config, ctx.obj.get("session_file"),
            required=(session == "required"),
        )
    return config


@contextmanager
def _locked_config(
    ctx: click.Context, *, session: str = "optional",
) -> Iterator[Config]:
    """Like :func:`_load_and_verify`, but holds the project lock while in use."""
    from releasy.locks import project_lock

    config = _load_and_verify(ctx, session=session)
    with project_lock(config):
        yield config


def _parse_walk_only(only: str | None, pr_url: str | None, stateless: bool):
    """Validate and parse ``--only`` for commands that walk tracked PRs."""
    from releasy.pipeline import parse_only

    if only is None:
        return None
    if pr_url is not None:
        raise click.UsageError(
            "--only and --pr are mutually exclusive: --only filters "
            "the multi-PR walk; --pr already names a single PR."
        )
    if stateless:
        raise click.UsageError(
            "--only is incompatible with --stateless: stateless mode "
            "always operates on a single PR (--pr) without a state "
            "file to filter against."
        )
    try:
        return parse_only(only)
    except ValueError as e:
        raise click.UsageError(str(e))


def _load_config_if_present(ctx: click.Context) -> Config | None:
    from releasy.config import load_config

    config_path = ctx.obj.get("config_path")
    try:
        return load_config(Path(config_path) if config_path else None)
    except FileNotFoundError:
        return None
    except Exception as e:
        raise click.ClickException(f"Failed to load config: {e}")


def _origin_from_pr_url(pr_url: str) -> str:
    """https URL of the repo hosting ``pr_url``."""
    from releasy.github_ops import parse_pr_url, slug_to_https_url

    parsed = parse_pr_url(pr_url)
    if parsed is None:
        raise click.ClickException(f"Could not parse --pr URL: {pr_url!r}")
    owner, repo, _ = parsed
    return slug_to_https_url(f"{owner}/{repo}")


def _short_id() -> str:
    return secrets.token_hex(3)


def _render_template(text: str, **vars: str) -> str:
    """Substitute ``{{ key }}`` / ``{{key}}`` placeholders."""
    out = text
    for key, value in vars.items():
        for placeholder in (f"{{{{ {key} }}}}", f"{{{{{key}}}}}"):
            out = out.replace(placeholder, value)
    return out


# Click caps help width at 80 by default, truncating short_help in `releasy --help`.
_CLI_CONTEXT_SETTINGS = {"max_content_width": 120}


@click.group(context_settings=_CLI_CONTEXT_SETTINGS)
@click.version_option(version=__version__, prog_name="releasy")
@click.option(
    "--config",
    "--config-file",
    "config_path",
    default=None,
    help="Path to config.yaml (defaults to ./config.yaml in the current directory)",
)
@click.option(
    "--session-file",
    "session_file",
    default=None,
    help="Path to the session file (features + pr_sources). Overrides "
         "the session_file: key in config.yaml. Defaults to "
         "<config-dir>/<target_branch>.session.yaml (or <name> when "
         "target_branch is unset).",
)
@click.pass_context
def cli(
    ctx: click.Context, config_path: str | None, session_file: str | None,
) -> None:
    """RelEasy — manage port branches and release construction."""
    ctx.ensure_object(dict)
    ctx.obj["config_path"] = config_path
    ctx.obj["session_file"] = session_file


@cli.command(short_help="Discover + port new PRs (cherry-pick + open PR).")
@click.option(
    "--onto",
    default=None,
    help="Version label used to derive the base branch name "
         "(<project>-<version>). Just a string — never resolved as a git "
         "ref; the base branch must already exist on origin. Not needed "
         "when 'target_branch' is set in config.",
)
@click.option("--work-dir", default=None, help="Working directory for git operations")
@click.option(
    "--resolve-conflicts/--no-resolve-conflicts",
    default=True,
    help="Invoke the AI resolver on conflicts (requires ai_resolve.enabled in config). "
         "Default: on.",
)
@click.option(
    "--retry-failed/--no-retry-failed",
    default=None,
    help="Re-attempt PR units whose previous run ended in `conflict` "
         "status: discard the existing branch and re-run the cherry-pick "
         "from base. With --no-retry-failed those entries are left "
         "exactly as-is. Defaults to the `pr_policy.retry_failed` value "
         "in config (true unless overridden).",
)
@click.option(
    "--only",
    "only",
    default=None,
    help="Restrict this run to a single PR (URL) or to a single group / "
         "feature ID. URL form: full GitHub PR link "
         "(https://github.com/owner/repo/pull/N) — only the matching "
         "discovered unit is processed. Name form: a `pr_sources.groups[].id` "
         "from the session file, or a singleton feature id like `pr-123` "
         "(`<owner>-<repo>-pr-N` for cross-repo PRs). Other discovered "
         "units are dropped before any side-effects. Exits non-zero if "
         "nothing matches.",
)
@click.option(
    "--pr",
    "pr_url",
    default=None,
    help="Restrict this run to a single PR by URL. Like --only with a "
         "URL, but exits cleanly (no-op) when the PR isn't in this "
         "session's scope. Use this from webhook/cron callers that "
         "don't know in advance whether the PR belongs to this "
         "project. Mutually exclusive with --only.",
)
@click.option(
    "--merge-target/--no-merge-target",
    "merge_target",
    default=False,
    help="For units whose rebase PR is already open: push a merge "
         "commit of the latest target tip into the PR branch even when "
         "there are no conflicts. Without this, RelEasy only touches an "
         "existing PR's branch when target conflicts with it (and AI "
         "resolves them). Never force-pushes — only fast-forward / "
         "merge-commit pushes. Default: off.",
)
@click.option(
    "--dry-run",
    "dry_run",
    is_flag=True,
    default=False,
    help="Show what the run would do without changing anything: no "
         "state writes, no git mutations (cherry-pick / push / branch), "
         "no GitHub writes (PR / labels / project board). Read-only "
         "GitHub fetches still happen so the plan reflects current "
         "reality. Cannot predict cherry-pick conflicts — shows "
         "intended actions only.",
)
@click.option(
    "--ignore-stalls",
    "ignore_stalls",
    is_flag=True,
    default=False,
    help="Re-attempt units parked on a stall that cannot clear by itself "
         "(waiting for another unit's PR to merge, on a prereq nobody "
         "ports, or a resolution dead end whose "
         "ai_resolve.max_dead_end_attempts are spent). Those are skipped "
         "by default because re-resolving "
         "them reaches the same verdict at full token price — see "
         "pr_policy.honor_stall_reasons.",
)
@click.option(
    "--redo",
    is_flag=True,
    default=False,
    help="Re-port the --only / --pr unit from scratch: drop its state, "
         "close its open port PR (if any) and rebuild on a renumbered "
         "branch with a fresh PR. Without a port PR, its branch is "
         "rebuilt from base in place. Refused for a merged port. Requires "
         "--only or --pr; not supported in sequential mode.",
)
@click.pass_context
def run(
    ctx: click.Context,
    onto: str | None,
    work_dir: str | None,
    resolve_conflicts: bool,
    retry_failed: bool | None,
    only: str | None,
    pr_url: str | None,
    merge_target: bool,
    dry_run: bool,
    ignore_stalls: bool,
    redo: bool,
) -> None:
    """Discover and port new PRs onto the base branch (cherry-pick + open PR)."""
    from releasy.pipeline import (
        parse_only, parse_pr_url_filter, run_pipeline, run_sequential,
    )

    if only is not None and pr_url is not None:
        raise click.UsageError(
            "--only and --pr are mutually exclusive: --only fails on "
            "no-match while --pr exits cleanly. Pick one."
        )

    try:
        only_filter = parse_only(only) or parse_pr_url_filter(pr_url)
    except ValueError as e:
        raise click.UsageError(str(e))
    if redo and only_filter is None:
        raise click.UsageError("--redo needs --only or --pr.")

    with _locked_config(ctx, session="required") as config:
        if not onto:
            if not config.target_branch:
                raise click.ClickException(
                    "Either pass --onto <ref> or set 'target_branch:' in config.yaml."
                )
            onto = config.target_branch

        wd = Path(work_dir) if work_dir else None
        effective_retry_failed = (
            config.pr_policy.retry_failed
            if retry_failed is None else retry_failed
        )
        config.dry_run = dry_run
        config.ignore_stalls = ignore_stalls

        if config.sequential:
            if redo:
                raise click.UsageError(
                    "--redo is not supported in sequential mode."
                )
            run_sequential(
                config, onto, wd,
                resolve_conflicts=resolve_conflicts,
                retry_failed=effective_retry_failed,
                only=only_filter,
                force_merge=merge_target,
            )
            _maybe_sync_graph_progress(config, onto)
            return

        state = run_pipeline(
            config, onto, wd,
            resolve_conflicts=resolve_conflicts,
            retry_failed=effective_retry_failed,
            only=only_filter,
            force_merge=merge_target,
            redo=redo,
        )
        _maybe_sync_graph_progress(config, onto)

        # Only conflicts within the --only / --pr scope affect the exit code.
        if only_filter is not None:
            has_conflicts = any(
                fs.status == "conflict"
                for fid, fs in state.features.items()
                if only_filter.matches_state(fid, fs)
            )
        else:
            has_conflicts = any(
                fs.status == "conflict" for fs in state.features.values()
            )
        if has_conflicts:
            raise SystemExit(1)


@cli.command(
    name="cherry-pick",
    short_help="One-off cross-repo cherry-pick (no config / state file).",
)
@click.option(
    "--origin",
    "origin",
    required=True,
    help="Origin remote URL (ssh or https) — the repo to clone, push to, "
         "and open the PR against. e.g. git@github.com:owner/repo.git",
)
@click.option(
    "--target",
    "target",
    required=True,
    help="Branch on origin to base the port on and (optionally) open a "
         "PR against. Must already exist on the origin remote.",
)
@click.option(
    "--commit",
    "source_url",
    required=True,
    help="GitHub URL of the source to cherry-pick. Accepts a PR "
         "(.../pull/N — uses the merge commit with -m 1), a commit "
         "(.../commit/<sha>), or a tag (.../releases/tag/<tag> or "
         ".../tree/<tag>). May reference any public repo (e.g. a fork).",
)
@click.option("--work-dir", default=None, help="Working directory for git operations")
@click.option(
    "--branch-name",
    "branch_name",
    default=None,
    help="Name of the port branch. Defaults to "
         "releasy/port/<short-id>-<6hex>.",
)
@click.option(
    "--push/--no-push",
    default=True,
    help="Push the resulting branch to origin. Default: on.",
)
@click.option(
    "--with-pr",
    "with_pr",
    is_flag=True,
    default=False,
    help="Open a PR from the port branch back to --target on origin. "
         "Requires --push (implied) and RELEASY_GITHUB_TOKEN.",
)
@click.option(
    "--resolve-conflicts",
    is_flag=True,
    default=False,
    help="On conflict, invoke Claude (or whatever --claude-command points "
         "at) to resolve. Requires --build-command so Claude can verify "
         "the resolution compiles.",
)
@click.option(
    "--mode",
    "mode",
    type=click.Choice(["backport", "forward_port"]),
    default="backport",
    show_default=True,
    help="Port direction for the AI resolver. 'backport' lets it adapt "
         "code (adjust signatures, drop non-crucial upstream functionality) "
         "and only declares a prerequisite when the PR truly can't stand "
         "without it; 'forward_port' is strict (reports MISSING_PREREQS).",
)
@click.option(
    "--build-command",
    "build_command",
    default="",
    help="Shell command Claude runs to verify the resolution compiles. "
         "Required when --resolve-conflicts is set. Example: "
         "'cd build && ninja'.",
)
@click.option(
    "--claude-command",
    "claude_command",
    default="claude",
    show_default=True,
    help="Executable used to invoke Claude.",
)
@click.option(
    "--ai-backend",
    "ai_backend",
    type=click.Choice(["cli", "api"]),
    default="cli",
    show_default=True,
    help="How to reach the model: 'cli' spawns --claude-command, 'api' "
         "talks to the Anthropic API directly using $ANTHROPIC_API_KEY.",
)
@click.option(
    "--prompt-file",
    "prompt_file",
    default=None,
    help="Path to the AI-resolve prompt template. Defaults to the "
         "prompt bundled with the releasy package.",
)
@click.option(
    "--timeout",
    "timeout_seconds",
    type=int,
    default=7200,
    show_default=True,
    help="Per-attempt Claude timeout (seconds).",
)
@click.option(
    "--max-iterations",
    "max_iterations",
    type=int,
    default=5,
    show_default=True,
    help="Maximum build attempts Claude may make per resolve invocation.",
)
@click.option(
    "--formatting-example",
    "formatting_example_url",
    default=None,
    help="Override the 'CI/CD Options' section with the one from this "
         "origin-repo PR instead of the target branch's PR template. The "
         "rest of that PR's body is ignored. Requires --with-pr.",
)
def cherry_pick_cmd(
    origin: str,
    target: str,
    source_url: str,
    work_dir: str | None,
    branch_name: str | None,
    push: bool,
    with_pr: bool,
    resolve_conflicts: bool,
    mode: str,
    build_command: str,
    claude_command: str,
    ai_backend: str,
    prompt_file: str | None,
    timeout_seconds: int,
    max_iterations: int,
    formatting_example_url: str | None,
) -> None:
    """One-off cross-repo cherry-pick — no config file, no state file.

    Cherry-picks a PR / commit / tag from any public GitHub repo onto a
    fresh branch off ``--target`` in ``--origin``, optionally resolving
    conflicts with Claude, pushing, and opening a PR against ``--target``.
    Nothing is persisted; each run makes a new branch.
    """
    if resolve_conflicts and not build_command.strip():
        raise click.UsageError(
            "--resolve-conflicts requires --build-command (the shell "
            "command Claude will run to verify the resolution compiles). "
            "Pass --build-command 'cd build && ninja' (or similar)."
        )
    if with_pr and not push:
        raise click.UsageError(
            "--with-pr requires --push; cannot open a PR for an "
            "unpushed branch."
        )
    if formatting_example_url and not with_pr:
        raise click.UsageError(
            "--formatting-example only applies when opening a PR; "
            "pass --with-pr or drop --formatting-example."
        )

    from releasy.stateless import StatelessOptions, run_stateless_cherry_pick

    opts = StatelessOptions(
        origin=origin,
        target=target,
        source_url=source_url,
        work_dir=Path(work_dir) if work_dir else None,
        branch_name=branch_name,
        push=push,
        open_pr=with_pr,
        resolve_conflicts=resolve_conflicts,
        mode=mode,
        build_command=build_command,
        claude_command=claude_command,
        ai_backend=ai_backend,
        prompt_file=prompt_file,
        timeout_seconds=timeout_seconds,
        max_iterations=max_iterations,
        formatting_example_url=formatting_example_url,
    )

    result = run_stateless_cherry_pick(opts)
    if not result.success:
        if result.error:
            raise click.ClickException(result.error)
        raise SystemExit(1)


@cli.command(
    name="project-backport",
    short_help="Batch-create backport PRs for a version from a GitHub Project.",
)
@click.option(
    "--project",
    "project_url",
    required=True,
    help="GitHub ProjectV2 URL, e.g. "
         "https://github.com/orgs/Altinity/projects/26.",
)
@click.option(
    "--version",
    "version",
    required=True,
    help="Target version, e.g. 24.8. Items whose 'Port Versions' field "
         "includes this are backported; also used as the PR label, the "
         "title prefix, and the 'Port Versions' value set on the new card.",
)
@click.option(
    "--target",
    "target",
    required=True,
    help="Origin branch to cherry-pick onto and open PRs against (e.g. "
         "customizations/24.8.14). Must already exist on origin.",
)
@click.option(
    "--origin",
    "origin",
    default=None,
    help="Origin remote URL to clone / push / open PRs against. Defaults "
         "to git@github.com:Altinity/ClickHouse.git.",
)
@click.option(
    "--work-dir",
    default=None,
    help="Working directory for git operations. If omitted, a stable cache "
         "clone is created/reused under $XDG_CACHE_HOME/releasy.",
)
@click.option(
    "--resolve-conflicts",
    is_flag=True,
    default=False,
    help="On cherry-pick conflict, invoke the AI resolver in backport mode "
         "(same machinery as `cherry-pick`). Requires --build-command.",
)
@click.option(
    "--build-command",
    "build_command",
    default="",
    help="Shell command Claude runs to verify a conflict resolution "
         "compiles. Required when --resolve-conflicts is set.",
)
@click.option("--claude-command", "claude_command", default="claude", show_default=True)
@click.option(
    "--ai-backend",
    "ai_backend",
    type=click.Choice(["cli", "api"]),
    default="cli",
    show_default=True,
    help="How to reach the model: 'cli' spawns --claude-command, 'api' "
         "talks to the Anthropic API directly using $ANTHROPIC_API_KEY.",
)
@click.option("--prompt-file", "prompt_file", default=None)
@click.option("--timeout", "timeout_seconds", type=int, default=7200, show_default=True)
@click.option("--max-iterations", "max_iterations", type=int, default=5, show_default=True)
@click.option(
    "--limit",
    "limit",
    type=int,
    default=None,
    help="Process at most this many items (newest upstream PR first).",
)
@click.option(
    "--dry-run",
    "dry_run",
    is_flag=True,
    default=False,
    help="Plan only: list qualifying items and what would be created / "
         "skipped. No clone, no cherry-pick, no pushes, no GitHub writes.",
)
def project_backport_cmd(
    project_url: str,
    version: str,
    target: str,
    origin: str | None,
    work_dir: str | None,
    resolve_conflicts: bool,
    build_command: str,
    claude_command: str,
    ai_backend: str,
    prompt_file: str | None,
    timeout_seconds: int,
    max_iterations: int,
    limit: int | None,
    dry_run: bool,
) -> None:
    """Batch-backport upstream PRs queued in a GitHub Project — no state file.

    For every upstream (ClickHouse/ClickHouse) PR item whose 'Port Versions'
    includes ``--version``, opens a Backport PR into ``--target`` on origin
    and adds it to the project with 'Port Versions' set.

    Idempotent: items that already have a backport PR are skipped. Only
    ever opens PRs into origin — never upstream.
    """
    if not version.strip():
        raise click.UsageError("--version must not be empty.")
    if resolve_conflicts and not build_command.strip():
        raise click.UsageError(
            "--resolve-conflicts requires --build-command (the shell "
            "command Claude runs to verify the resolution compiles). "
            "Pass --build-command 'cd build && ninja' (or similar)."
        )

    from releasy.project_backport import (
        DEFAULT_ORIGIN,
        ProjectBackportOptions,
        run_project_backport,
    )

    opts = ProjectBackportOptions(
        project_url=project_url,
        version=version,
        target=target,
        origin=origin or DEFAULT_ORIGIN,
        work_dir=Path(work_dir) if work_dir else None,
        resolve_conflicts=resolve_conflicts,
        build_command=build_command,
        claude_command=claude_command,
        ai_backend=ai_backend,
        prompt_file=prompt_file,
        timeout_seconds=timeout_seconds,
        max_iterations=max_iterations,
        dry_run=dry_run,
        limit=limit,
    )

    result = run_project_backport(opts)
    if result.fatal:
        raise click.ClickException(result.fatal)
    if result.had_failures:
        raise SystemExit(1)


@cli.command(
    name="continue",
    short_help="Reconcile state after manual fixes.",
)
@click.option(
    "--branch",
    default=None,
    help="Branch or feature ID to mark resolved. If omitted, reconciles "
         "every port in state: opens PRs for any clean branch that lacks "
         "one (e.g. previous run had auto_pr off), pushes + opens PRs for "
         "newly-resolved conflicts, highlights any still-unresolved ones, "
         "and refreshes the GitHub Project board.",
)
@click.option("--work-dir", default=None, help="Working directory for git operations")
@click.option(
    "--dry-run",
    "dry_run",
    is_flag=True,
    default=False,
    help="Show what would happen without changing anything: no state "
         "writes, no pushes, no PR opens, no project sync. Read-only "
         "GitHub fetches still happen.",
)
@click.pass_context
def continue_cmd(
    ctx: click.Context,
    branch: str | None,
    work_dir: str | None,
    dry_run: bool,
) -> None:
    """Reconcile state after a manual fix (push + open any missing PRs)."""
    from releasy.pipeline import continue_all, continue_branch, run_sequential

    with _locked_config(ctx, session="optional") as config:
        config.dry_run = dry_run
        wd = Path(work_dir) if work_dir else None
        if branch:
            if not continue_branch(config, branch):
                raise SystemExit(1)
            return

        if config.sequential:
            if not config.target_branch:
                raise click.ClickException(
                    "Sequential mode requires 'target_branch:' to be set in config.yaml."
                )
            run_sequential(
                config, config.target_branch, wd, resolve_conflicts=True,
            )
            return

        if not continue_all(config, wd):
            raise SystemExit(1)


@cli.command(short_help="Mark a port as skipped (state-only).")
@click.option("--branch", required=True, help="Branch name or feature ID to skip")
@click.pass_context
def skip(ctx: click.Context, branch: str) -> None:
    """Mark a port branch as skipped (state-only, branch + PR untouched)."""
    from releasy.pipeline import skip_branch

    with _locked_config(ctx, session="skip") as config:
        if not skip_branch(config, branch):
            raise SystemExit(1)


@cli.command(
    name="mark-reverted",
    short_help="Record that a merged port was reverted on target (state-only).",
)
@click.option(
    "--branch", required=True,
    help="Branch name or feature ID whose port was reverted",
)
@click.option(
    "--reason", default=None,
    help="Why it was reverted (shown in `status` and on the graph issue). "
         "Cite the revert PR here.",
)
@click.pass_context
def mark_reverted_cmd(
    ctx: click.Context, branch: str, reason: str | None,
) -> None:
    """Mark a merged port as reverted on the target branch (state-only).

    The entry becomes terminal: `run` and `refresh` leave it alone and
    `graph sync` lists it as reverted. Only
    `pr_policy.recreate_reverted_prs` (off by default) re-ports it.
    Git and the PR are not touched.
    """
    from releasy.pipeline import mark_reverted

    with _locked_config(ctx, session="skip") as config:
        if not mark_reverted(config, branch, reason):
            raise SystemExit(1)


@cli.command(short_help="Park a PR without vetoing it (session-level).")
@click.argument("url")
@click.option(
    "--reason", default=None,
    help="What it is waiting on. Shown on the graph issue and by "
         "`releasy pr list`.",
)
@click.pass_context
def hold(ctx: click.Context, url: str, reason: str | None) -> None:
    """Put a PR on hold — appends it to ``pr_sources.on_hold``.

    Not a veto: the PR keeps its graph place and any port branch / PR;
    `run` skips its unit, and units depending on it report as blocked.
    Holding any PR of a group holds the whole group. Use `releasy unhold`
    to resume. Refused for a PR in ``exclude_prs``.
    """
    from releasy.pr_membership import hold_pr

    with _locked_config(ctx, session="required") as config:
        if not hold_pr(config, url, reason or ""):
            raise SystemExit(1)


@cli.command(short_help="Take a PR off hold (session-level).")
@click.argument("url")
@click.pass_context
def unhold(ctx: click.Context, url: str) -> None:
    """Take a PR off hold — drops it from ``pr_sources.on_hold``.

    The unit ports on the next `releasy run`. No-op if not on hold.
    """
    from releasy.pr_membership import unhold_pr

    with _locked_config(ctx, session="required") as config:
        if not unhold_pr(config, url):
            raise SystemExit(1)


@cli.command(short_help="Persist state and exit (nothing rolled back).")
@click.pass_context
def abort(ctx: click.Context) -> None:
    """Persist current state and exit (no rollback; branches/PRs untouched)."""
    from releasy.pipeline import abort_run

    with _locked_config(ctx, session="skip") as config:
        abort_run(config)


@cli.command(
    short_help="Wipe local-only port artifacts (aborted cherry-picks, "
               "damaged branches with no PR).",
)
@click.argument("identifier", required=False)
@click.option(
    "--work-dir", default=None,
    help="Working directory for git operations (defaults to config).",
)
@click.option(
    "--dry-run", is_flag=True, default=False,
    help="Show what would be cleaned without changing anything.",
)
@click.option(
    "--yes", "-y", "assume_yes", is_flag=True, default=False,
    help="Skip the interactive confirmation in multi-clear mode.",
)
@click.pass_context
def clear(
    ctx: click.Context,
    identifier: str | None,
    work_dir: str | None,
    dry_run: bool,
    assume_yes: bool,
) -> None:
    """Wipe local-only port artifacts that never made it to a PR.

    With IDENTIFIER (feature ID, branch name, source-PR number or URL),
    clears that feature. Without it, clears every ``conflict`` /
    ``branch_created`` feature with no rebase PR, after confirmation.

    Clearing aborts any in-progress cherry-pick / merge / rebase, deletes
    the local port branch, and drops the state entry. Features with an
    open rebase PR are refused.
    """
    from releasy.pipeline import clear_all_dirty, clear_branch

    with _locked_config(ctx, session="skip") as config:
        wd = Path(work_dir) if work_dir else None
        if identifier:
            ok = clear_branch(config, identifier, wd, dry_run=dry_run)
        else:
            ok = clear_all_dirty(
                config, wd, dry_run=dry_run, assume_yes=assume_yes,
            )
        if not ok:
            raise SystemExit(1)


@cli.command(short_help="Print current pipeline state (read-only).")
@click.pass_context
def status(ctx: click.Context) -> None:
    """Print current pipeline state (read-only, no git/network)."""
    from releasy.pipeline import print_status

    config = _load_and_verify(ctx, session="skip")
    print_status(config)


@cli.group(name="graph")
def graph_cmd() -> None:
    """Discover the PR dependency graph and track it as a GitHub issue.

    ``discover`` trial-picks PRs to build the graph (``--open-issue`` opens
    an issue for it); ``update`` refines it from trusted member comments on
    the issue; ``sync`` refreshes the issue's progress checkboxes.
    """


@graph_cmd.command(
    name="discover",
    short_help="Auto-discover PR groups from trial cherry-picks.",
)
@click.option(
    "--onto", default=None,
    help="Base branch (defaults to config.target_branch).",
)
@click.option(
    "--work-dir", default=None,
    help="Working directory for git operations.",
)
@click.option(
    "--output", "-o", "output_path", default=None, type=click.Path(),
    help="Diagnostic report path. Default: <config-dir>/graph.<base>.yaml.",
)
@click.option(
    "--deps-file", "deps_file_override", default=None, type=click.Path(),
    help="Override `pr_sources.deps_file` for this run — write the deps "
         "overlay here instead of the configured path. Useful for "
         "previews / A-B comparisons (e.g. /tmp/preview.yaml).",
)
@click.option(
    "--no-write", is_flag=True, default=False,
    help="Skip writing the deps overlay even when `pr_sources.deps_file` "
         "is set. Diagnostic report still lands.",
)
@click.option(
    "--no-ai", is_flag=True, default=False,
    help="Skip ALL Claude calls — both the lightweight refinement of "
         "deterministic candidates and the heavyweight AI-resolver "
         "fallback that runs when the deterministic mapping yields no "
         "candidates. Use deterministic git-graph deps as-is.",
)
@click.option(
    "--max-depth", default=None, type=int,
    help="Cap for upstream-backport prereq recursion. Default: "
         "ai_resolve.auto_add_prerequisite_prs.max_prereq_depth.",
)
@click.option(
    "--limit", "pr_limit", default=None, type=int,
    help="Cap units scanned (most-recent N). Default: unlimited.",
)
@click.option(
    "--include-already-merged", is_flag=True, default=False,
    help="Include units already in target as zero-edge nodes in the report.",
)
@click.option(
    "--redo", is_flag=True, default=False,
    help="Re-discover everything from scratch. By default a re-run reuses "
         "the units already in the prior graph (standalone PRs AND groups) "
         "and only trial-picks PRs new since the last run.",
)
@click.option(
    "--open-issue/--no-open-issue", default=False,
    help="Open (or refresh) a GitHub issue on origin carrying the graph. "
         "Re-running updates the same issue instead of opening a duplicate. "
         "Members can then comment and `releasy graph update` ingests them.",
)
@click.option(
    "--issue-title", default=None,
    help="Title for the graph issue (default: 'Port graph for <base>').",
)
@click.pass_context
def graph_discover_cmd(
    ctx: click.Context,
    onto: str | None,
    work_dir: str | None,
    output_path: str | None,
    deps_file_override: str | None,
    no_write: bool,
    no_ai: bool,
    max_depth: int,
    pr_limit: int | None,
    include_already_merged: bool,
    redo: bool,
    open_issue: bool,
    issue_title: str | None,
) -> None:
    """Auto-discover PR groups from trial cherry-picks.

    Trial-cherry-picks ``pr_sources`` candidates (oldest-merged first) onto
    the target tip in a scratch worktree. A real conflict groups a PR with
    its prerequisite; connected PRs collapse into one unit. Writes a
    diagnostic YAML report (checkpointed per unit, so interrupted runs
    resume) and a deps overlay to ``pr_sources.deps_file`` for the next
    ``releasy run`` (``--no-write`` skips, ``--deps-file`` redirects).

    Does not touch ``state.yaml`` or the main worktree.
    """
    if no_write and deps_file_override:
        raise click.UsageError(
            "--no-write and --deps-file are mutually exclusive: --no-write "
            "skips the overlay entirely, --deps-file requests one at a "
            "specific path. Pick one."
        )
    if open_issue and output_path:
        raise click.UsageError(
            "--open-issue and -o/--output are mutually exclusive: the "
            "issue-tracking report must live at the default path so "
            "`graph update` can find it. Drop -o (or drop --open-issue)."
        )

    from releasy.dag_discovery import run_discover_deps
    from releasy.config import resolve_deps_file_path

    with _locked_config(ctx, session="required") as config:
        deps_overlay_path: Path | None
        if no_write:
            deps_overlay_path = None
        elif deps_file_override:
            override = Path(deps_file_override)
            if not override.is_absolute():
                # Relative to cwd, not the session dir.
                override = override.resolve()
            deps_overlay_path = override
        else:
            session_path = (
                config.session.session_path
                if config.session and config.session.session_path
                else None
            )
            if session_path is None:
                deps_overlay_path = None
            else:
                deps_file_value = (
                    config.session.pr_sources.deps_file
                    if config.session else None
                )
                deps_overlay_path = resolve_deps_file_path(
                    session_path, deps_file_value,
                )

        try:
            report = run_discover_deps(
                config,
                onto=onto,
                work_dir=Path(work_dir) if work_dir else None,
                output_path=Path(output_path) if output_path else None,
                deps_overlay_path=deps_overlay_path,
                use_ai=not no_ai,
                max_depth=max_depth,
                pr_limit=pr_limit,
                include_already_merged=include_already_merged,
                redo=redo,
                open_issue=open_issue,
                issue_title=issue_title,
            )
        except (ValueError, RuntimeError) as e:
            raise click.ClickException(str(e))

    _print_discovery_summary(report)


def _print_discovery_summary(report) -> None:  # noqa: ANN001 — DiscoveryReport
    group_units = sum(1 for n in report.nodes if n.is_user_group)
    method_counts: dict[str, int] = {}
    for n in report.nodes:
        method_counts[n.discovery_method] = method_counts.get(n.discovery_method, 0) + 1
    in_target = len(report.skipped_already_in_target)
    to_pick = report.candidate_unit_count - in_target

    click.echo("")
    click.echo(f"graph discover · base={report.base_branch}")
    click.echo(
        f"  candidates: {report.candidate_unit_count} unit(s) "
        f"covering {report.candidate_pr_count} PR(s)"
        + (
            f" — including {group_units} user-declared group(s)"
            if group_units else ""
        )
    )
    click.echo(
        f"  status: {in_target} already in target · {to_pick} to trial-pick"
    )
    if method_counts:
        ordered_keys = [
            "trial-clean", "git-graph", "git-graph+claude",
            "ai-resolve", "ai-resolve-clean", "depth-cutoff",
        ]
        bits = []
        for k in ordered_keys:
            if method_counts.get(k):
                bits.append(f"{method_counts[k]} {k}")
        for k, v in method_counts.items():
            if k not in ordered_keys:
                bits.append(f"{v} {k}")
        click.echo(f"  trial-picked: {' · '.join(bits)}")
    cached_count = sum(1 for n in report.nodes if n.cached)
    if cached_count:
        click.echo(
            f"  cached: {cached_count} port branch(es) preserved at "
            f"feature/<base>/<unit_id> for `releasy run` to reuse"
        )
    if to_pick == 0 and report.candidate_unit_count > 0:
        click.echo(
            "  (every candidate is already in target — deps overlay "
            "carries no new entries)"
        )

    if report.refresh_removed or report.refresh_added:
        bits: list[str] = []
        if report.refresh_removed:
            sample = ", ".join(report.refresh_removed[:5])
            extra = f" (+{len(report.refresh_removed) - 5} more)" if len(report.refresh_removed) > 5 else ""
            bits.append(
                f"{len(report.refresh_removed)} removed since last run "
                f"[{sample}{extra}]"
            )
        if report.refresh_added:
            sample = ", ".join(report.refresh_added[:5])
            extra = f" (+{len(report.refresh_added) - 5} more)" if len(report.refresh_added) > 5 else ""
            bits.append(
                f"{len(report.refresh_added)} added [{sample}{extra}]"
            )
        click.echo(f"  refresh: {' · '.join(bits)}")
    groups = [
        n for n in report.nodes
        if not n.is_user_group and len(n.pr_urls) > 1
    ]
    if groups:
        click.echo(f"  groups: {len(groups)} combined port(s)")
        for g in groups:
            click.echo(f"    {g.unit_id}: {len(g.pr_urls)} PR(s)")
    if report.components:
        click.echo(f"  dependency components: {len(report.components)}")
        for comp in report.components:
            arrows = " → ".join(comp.unit_ids)
            click.echo(f"    {comp.component_id}: {arrows}")
    if report.singletons:
        click.echo(f"  standalone PRs ({len(report.singletons)}): "
                   f"{', '.join(report.singletons[:8])}"
                   f"{' …' if len(report.singletons) > 8 else ''}")
    if report.warnings:
        click.echo(f"  warnings ({len(report.warnings)}):")
        for w in report.warnings[:10]:
            click.echo(f"    • {w}")
        if len(report.warnings) > 10:
            click.echo(f"    … and {len(report.warnings) - 10} more")


@graph_cmd.command(
    name="update",
    short_help="Refine the graph from trusted member comments on its issue.",
)
@click.option(
    "--onto", default=None,
    help="Base branch (defaults to config.target_branch). Must match the "
         "value used for `graph discover`.",
)
@click.option(
    "--since", default=None,
    help="Only ingest comments created after this ISO-8601 timestamp. "
         "Default: the last-ingested watermark stored in the report.",
)
@click.option(
    "--work-dir", default=None,
    help="Working directory (used to locate config/report; no git ops).",
)
@click.option(
    "--post-comment/--no-post-comment", default=None,
    help="Post a summary comment on the issue after applying changes "
         "(default: graph.post_comment in config).",
)
@click.option(
    "--dry-run", is_flag=True, default=False,
    help="Show the rebuilt graph + intended session edits; write nothing.",
)
@click.pass_context
def graph_update_cmd(
    ctx: click.Context,
    onto: str | None,
    since: str | None,
    work_dir: str | None,
    post_comment: bool | None,
    dry_run: bool,
) -> None:
    """Refine the saved graph from trusted member comments on its issue.

    Feeds Claude the graph from ``graph discover --open-issue`` plus new
    comments from trusted members (``graph.trusted_associations``) and
    rebuilds the graph from its reply. No git, no trial-picks. Adds /
    vetoes go into the session (``include_prs`` / ``exclude_prs``); the
    issue body is refreshed in place.
    """
    from releasy.dag_discovery import run_graph_update

    with _locked_config(ctx, session="required") as config:
        if dry_run:
            config.dry_run = True
        resolved_post = (
            config.graph.post_comment if post_comment is None else post_comment
        )
        code = run_graph_update(
            config,
            onto=onto,
            since=since,
            work_dir=Path(work_dir) if work_dir else None,
            dry_run=dry_run,
            post_comment=resolved_post,
        )
    if code != 0:
        raise SystemExit(code)


@graph_cmd.command(
    name="sync",
    short_help="Refresh the graph issue's progress checkboxes from state.",
)
@click.option(
    "--onto", default=None,
    help="Base branch (defaults to config.target_branch). Must match the "
         "value used for `graph discover`.",
)
@click.option(
    "--dry-run", is_flag=True, default=False,
    help="Show what would be posted; don't touch the issue.",
)
@click.option(
    "--open-issue", is_flag=True, default=False,
    help="Open the issue from the saved graph when it has none — for a "
         "`graph discover` run that forgot --open-issue. Reads the report "
         "on disk; no trial-picks, no AI, no re-discovery.",
)
@click.pass_context
def graph_sync_cmd(
    ctx: click.Context, onto: str | None, dry_run: bool, open_issue: bool,
) -> None:
    """Refresh the graph issue's progress checkboxes from pipeline state.

    Each unit's box is ticked once its port PR is open (a partially-applied
    group's draft PR counts), annotated with status and a PR link. No git,
    no AI. ``--open-issue`` opens the issue from the saved graph if
    ``discover`` didn't.

    ``releasy run`` and ``releasy refresh`` do this automatically unless
    ``graph.sync_progress: false``.
    """
    from releasy.dag_discovery import sync_graph_progress

    with _locked_config(ctx, session="optional") as config:
        if dry_run:
            config.dry_run = True
        code = sync_graph_progress(config, onto=onto, open_issue=open_issue)
    if code != 0:
        raise SystemExit(code)


def _maybe_sync_graph_progress(
    config: Config, onto: str | None = None,
) -> None:
    """Best-effort graph issue refresh after ``run`` / ``refresh``; never fails."""
    if not config.graph.sync_progress:
        return
    from releasy.dag_discovery import sync_graph_progress

    try:
        sync_graph_progress(config, onto=onto, quiet=True)
    except Exception as e:  # noqa: BLE001 — never fail the caller
        click.echo(f"warning: graph issue sync failed: {e}", err=True)


@cli.command(
    short_help=(
        "Sync status; optionally merge / analyze CI / address review."
    ),
)
@click.option(
    "--pr",
    "pr_url",
    default=None,
    help="GitHub URL of a single PR to merge target into and AI-resolve "
         "conflicts on. When omitted, walks every PR currently tracked "
         "in the project state file. Required when --stateless is set.",
)
@click.option(
    "--work-dir", default=None, help="Working directory for git operations",
)
@click.option(
    "--resolve-conflicts/--no-resolve-conflicts",
    "ai_resolve_flag",
    default=True,
    help="Invoke the AI resolver on merge conflicts (requires "
         "ai_resolve.enabled in config — automatically flipped on with "
         "--stateless). With --no-resolve-conflicts, conflicts are "
         "flagged but no automatic fix is attempted. Default: on.",
)
@click.option(
    "--stateless",
    is_flag=True,
    default=False,
    help="Skip the session and state files: no per-project lock, no "
         "ownership check, no state mutations. config.yaml IS still "
         "loaded (with the usual --config override) when present, so AI "
         "settings, origin, etc. are inherited from it. The --origin / "
         "--build-command / --claude-command / --prompt-file / --timeout "
         "/ --max-iterations overrides below apply only with --stateless. "
         "When no config.yaml is present in cwd, a synthetic config is "
         "built from the flags; --origin defaults to the PR's host repo "
         "as an https URL in that case. Requires --pr.",
)
@click.option(
    "--origin",
    "origin_url",
    default=None,
    help="(stateless only) Origin remote URL to push to. Use this if "
         "you need an ssh-form URL (e.g. git@github.com:owner/repo.git) "
         "instead of https. When config.yaml is present its origin is "
         "used unless this flag overrides.",
)
@click.option(
    "--build-command",
    "build_command_cli",
    default=None,
    help="(stateless only) Shell command the AI may run inside the "
         "repo to verify its conflict resolution compiles. Empty means "
         "'no build — AI skips verification'. Overrides "
         "ai_resolve.build_command in config.",
)
@click.option(
    "--claude-command",
    "claude_command",
    default=None,
    help="(stateless only) Executable used to invoke Claude. "
         "Overrides ai_resolve.command in config.",
)
@click.option(
    "--ai-backend",
    "ai_backend_cli",
    type=click.Choice(["cli", "codex", "api"]),
    default=None,
    help="How to reach the model: 'cli' spawns the claude binary, 'codex' "
         "spawns 'codex exec', 'api' talks to the Anthropic API directly "
         "using $ANTHROPIC_API_KEY. Overrides ai_backend in config.",
)
@click.option(
    "--prompt-file",
    "prompt_file_cli",
    default=None,
    help="(stateless only) Path to the merge-conflict prompt template. "
         "Overrides ai_resolve.merge_prompt_file in config. Defaults to "
         "the prompt bundled with the releasy package.",
)
@click.option(
    "--timeout",
    "timeout_seconds",
    type=int,
    default=None,
    help="(stateless only) Per-invocation Claude timeout in seconds. "
         "Overrides ai_resolve.timeout_seconds in config.",
)
@click.option(
    "--max-iterations",
    "max_iterations_cli",
    type=int,
    default=None,
    help="(stateless only) Hard cap on build attempts per resolve. "
         "Overrides ai_resolve.max_iterations in config.",
)
@click.option(
    "--only",
    "only",
    default=None,
    help="Restrict the multi-PR walk to a single tracked PR (URL — "
         "source or rebase) or a single feature / group ID. "
         "Mutually exclusive with --pr and --stateless. "
         "Exits non-zero if nothing matches.",
)
@click.option(
    "--merge-target/--no-merge-target",
    "merge_target",
    default=False,
    help="Merge the latest target branch into each in-scope PR's "
         "branch (with AI-resolved conflicts) and push. Without this, "
         "refresh only runs status-sync — no branch is touched. Never "
         "force-pushes; only fast-forward / merge-commit pushes. "
         "Default: off.",
)
@click.option(
    "--address-review/--no-address-review",
    "address_review_flag",
    default=False,
    help="Address review feedback on each in-scope PR via the AI. "
         "Trust gate: by default keeps comments whose "
         "author_association is OWNER/MEMBER/COLLABORATOR/CONTRIBUTOR "
         "(configurable via review_response.trusted_associations); "
         "review_response.trusted_reviewers adds extra logins. "
         "Comments are then further narrowed by --since + drop hidden "
         "(minimized/outdated) + keep only inline comments on "
         "unresolved threads OR top-level comments with no later "
         "PR-author reply. Combine with --merge-target to merge first, "
         "then address review; PRs in conflict are skipped. "
         "Default: off.",
)
@click.option(
    "--analyze-fails/--no-analyze-fails",
    "analyze_fails_flag",
    default=False,
    help="Investigate failed CI on each in-scope PR via the AI: walks "
         "failed praktika status entries on the PR head, bundles the "
         "failing tests per shard, and lets Claude run the iterative "
         "fix-build-rerun loop. Combine with --merge-target / "
         "--address-review; phase order in one run is merge-target → "
         "analyze-fails → address-review (analyze-fails needs a "
         "non-stale CI report against the current head SHA; "
         "address-review's commits would invalidate it). PRs left in "
         "conflict by --merge-target are skipped. Default: off.",
)
@click.option(
    "--no-flaky-check",
    "no_flaky_check",
    is_flag=True,
    default=False,
    help="(--analyze-fails only) Skip the flaky-elsewhere assessment "
         "that cross-references failures against other tracked PRs.",
)
@click.option(
    "--no-baseline-check",
    "no_baseline_check",
    is_flag=True,
    default=False,
    help="(--analyze-fails only) Skip the pre-change comparison "
         "against the last CI run on the target branch that predates "
         "the PR. Overrides analyze_fails.baseline_check.",
)
@click.option(
    "--post-comment/--no-post-comment",
    "post_comment_flag",
    default=None,
    help="(--analyze-fails only) Post a top-level summary comment on "
         "each processed PR. Defaults to the "
         "`analyze_fails.post_comment_to_pr` config value.",
)
@click.option(
    "--dry-run",
    "dry_run",
    is_flag=True,
    default=False,
    help="Show what would happen without changing anything: no state "
         "writes, no merges, no pushes, no GitHub writes. Read-only "
         "GitHub fetches still happen. Cannot predict merge conflicts.",
)
@click.pass_context
def refresh(
    ctx: click.Context,
    pr_url: str | None,
    work_dir: str | None,
    ai_resolve_flag: bool,
    stateless: bool,
    origin_url: str | None,
    build_command_cli: str | None,
    claude_command: str | None,
    ai_backend_cli: str | None,
    prompt_file_cli: str | None,
    timeout_seconds: int | None,
    max_iterations_cli: int | None,
    only: str | None,
    merge_target: bool,
    address_review_flag: bool,
    analyze_fails_flag: bool,
    no_flaky_check: bool,
    no_baseline_check: bool,
    post_comment_flag: bool | None,
    dry_run: bool,
) -> None:
    """Maintenance pass over tracked PRs (or one PR by URL).

    Three modes:

    \b
    1. (no flags)                — walk every tracked PR in state.
    2. ``--pr <url>``            — operate on one PR by URL (state
                                   updated if the URL matches a tracked
                                   rebase PR).
    3. ``--stateless --pr <url>``— pure standalone: no session, no
                                   state, no lock. Only RELEASY_GITHUB_TOKEN
                                   and the PR URL are required; config.yaml
                                   is read if present, otherwise a synthetic
                                   config is built from the stateless flags.

    Never opens new PRs, creates branches, or discovers PR sources.
    Status sync (externally merged PRs, supersede sweep, labels) always
    runs. The three branch-mutating passes are opt-in:

    \b
    - ``--merge-target``    — merge ``origin/<base>`` into each PR
      branch and AI-resolve any conflicts, then push.
    - ``--analyze-fails``   — bundle each PR's failing CI tests per
      shard and let the AI run the iterative fix-build-rerun loop.
    - ``--address-review``  — fetch trusted review feedback, drop
      hidden / addressed comments, and let the AI add fix commits.

    Phases run in the order ``merge-target → analyze-fails →
    address-review``; PRs left in conflict by ``--merge-target`` skip the
    rest.

    Exit code is 1 if any PR ended up in conflict, any address-review
    run failed, or any analyze-fails per-PR run errored — 0 otherwise.
    """
    from releasy.refresh import (
        refresh_tracked_prs,
        resolve_conflicts_for_pr,
    )

    wd = Path(work_dir) if work_dir else None
    only_filter = _parse_walk_only(only, pr_url, stateless)

    stateless_only_set: list[str] = []
    if origin_url is not None:
        stateless_only_set.append("--origin")
    if build_command_cli is not None:
        stateless_only_set.append("--build-command")
    if claude_command is not None:
        stateless_only_set.append("--claude-command")
    if prompt_file_cli is not None:
        stateless_only_set.append("--prompt-file")
    if timeout_seconds is not None:
        stateless_only_set.append("--timeout")
    if max_iterations_cli is not None:
        stateless_only_set.append("--max-iterations")

    if not stateless and stateless_only_set:
        raise click.UsageError(
            f"{', '.join(stateless_only_set)} only apply with "
            "--stateless. Drop the flags, or add --stateless to skip "
            "the session/state/lock layer."
        )

    if stateless:
        if pr_url is None:
            raise click.UsageError(
                "--stateless requires --pr <url>: there is no state "
                "file to enumerate tracked PRs from."
            )
        from releasy.config import make_stateless_config

        config = _load_config_if_present(ctx)
        if config is None:
            config = make_stateless_config(
                origin_url or _origin_from_pr_url(pr_url),
                work_dir=wd,
                push=True,
                auto_pr=False,
                ai_enabled=ai_resolve_flag,
                ai_command=claude_command or "claude",
                ai_backend=ai_backend_cli or "cli",
                ai_build_command=build_command_cli or "",
                ai_prompt_file=None,
                ai_timeout_seconds=(
                    timeout_seconds if timeout_seconds is not None else 7200
                ),
                ai_max_iterations=(
                    max_iterations_cli
                    if max_iterations_cli is not None else 5
                ),
            )
            if prompt_file_cli is not None:
                config.ai_resolve.merge_prompt_file = prompt_file_cli
            else:
                bundled = (
                    Path(__file__).parent / "prompts"
                    / "resolve_merge_conflict.md"
                ).resolve()
                config.ai_resolve.merge_prompt_file = str(bundled)
        else:
            if origin_url:
                config.origin.remote = origin_url
            if claude_command is not None:
                config.ai_resolve.command = claude_command
            if ai_backend_cli is not None:
                config.ai_backend = ai_backend_cli
            if build_command_cli is not None:
                config.ai_resolve.build_command = build_command_cli
            if prompt_file_cli is not None:
                config.ai_resolve.merge_prompt_file = prompt_file_cli
            if timeout_seconds is not None:
                config.ai_resolve.timeout_seconds = timeout_seconds
            if max_iterations_cli is not None:
                config.ai_resolve.max_iterations = max_iterations_cli
            if ai_resolve_flag:
                config.ai_resolve.enabled = True

        config.session = None
        config.stateless = True
        config.dry_run = dry_run
        if no_baseline_check:
            config.analyze_fails.baseline_check = False
        if not resolve_conflicts_for_pr(
            config, pr_url, wd, resolve_conflicts=ai_resolve_flag,
            force_merge=merge_target,
            address_review=address_review_flag,
            analyze_fails=analyze_fails_flag,
            no_flaky_check=no_flaky_check,
            post_comment=post_comment_flag,
        ):
            raise SystemExit(1)
        return

    # Session is "optional" (not "skip") so session.pr_labels can be reconciled.
    if pr_url is None:
        with _locked_config(ctx, session="optional") as config:
            config.dry_run = dry_run
            if ai_backend_cli is not None:
                config.ai_backend = ai_backend_cli
            if no_baseline_check:
                config.analyze_fails.baseline_check = False
            ok = refresh_tracked_prs(
                config, wd, resolve_conflicts=ai_resolve_flag,
                only=only_filter, force_merge=merge_target,
                address_review=address_review_flag,
                analyze_fails=analyze_fails_flag,
                no_flaky_check=no_flaky_check,
                post_comment=post_comment_flag,
            )
            # Sync even on partial failure: some ports still moved forward.
            _maybe_sync_graph_progress(config)
            if not ok:
                raise SystemExit(1)
        return

    with _locked_config(ctx, session="optional") as config:
        config.dry_run = dry_run
        if ai_backend_cli is not None:
            config.ai_backend = ai_backend_cli
        if no_baseline_check:
            config.analyze_fails.baseline_check = False
        if not resolve_conflicts_for_pr(
            config, pr_url, wd, resolve_conflicts=ai_resolve_flag,
            force_merge=merge_target,
            address_review=address_review_flag,
            analyze_fails=analyze_fails_flag,
            no_flaky_check=no_flaky_check,
            post_comment=post_comment_flag,
        ):
            raise SystemExit(1)


@cli.command(
    name="analyze-fails",
    short_help="Investigate failed CI tests on a PR (or every tracked PR).",
)
@click.option(
    "--pr",
    "pr_url",
    default=None,
    help="GitHub URL of the PR to analyse. When omitted, every PR "
         "RelEasy currently tracks in state (with a rebase_pr_url) is "
         "processed in turn.",
)
@click.option("--work-dir", default=None, help="Working directory for git operations")
@click.option(
    "--dry-run",
    is_flag=True,
    default=False,
    help="Discover failed tests, build the flaky-elsewhere map, and "
         "print what would happen — without invoking Claude or pushing.",
)
@click.option(
    "--push/--no-push",
    default=True,
    help="Push commits the AI appended to each PR's head branch. "
         "Default: on.",
)
@click.option(
    "--no-flaky-check",
    is_flag=True,
    default=False,
    help="Skip the flaky-elsewhere assessment (don't fetch reports for "
         "other tracked PRs to corroborate flake signals). Faster, "
         "but Claude has to judge unrelated-vs-related from the diff "
         "alone.",
)
@click.option(
    "--no-baseline-check",
    is_flag=True,
    default=False,
    help="Skip the pre-change comparison (don't read the last CI run "
         "on the target branch that predates the PR). Faster, but "
         "Claude loses the strongest unrelated-vs-related signal "
         "there is. Overrides analyze_fails.baseline_check.",
)
@click.option(
    "--post-comment/--no-post-comment",
    "post_comment",
    default=None,
    help="Post a top-level summary comment on each processed PR "
         "(per-shard outcomes, AI narration, commit + push status). "
         "Defaults to the `analyze_fails.post_comment_to_pr` config "
         "value (default: on). Use --no-post-comment for silent "
         "runs that only narrate to local stdout.",
)
@click.option(
    "--stateless",
    is_flag=True,
    default=False,
    help="Skip the session and state files: no per-project lock, no "
         "ownership check, no state mutations. config.yaml IS still "
         "loaded (with the usual --config override) so AI settings, "
         "origin, etc. are inherited. Required if --pr points at a "
         "repo that's not the project origin, or when no project "
         "config exists in cwd.",
)
@click.option(
    "--origin",
    "origin_url",
    default=None,
    help="(stateless only) Origin remote URL to push to. Use this if "
         "you need an ssh-form URL instead of https. When config.yaml "
         "is present its origin is used unless this flag overrides.",
)
@click.option(
    "--build-command",
    "build_command_cli",
    default=None,
    help="(stateless only) Shell command the AI may run inside the "
         "repo to verify its changes compile and to (re)build "
         "ClickHouse before reproducing the failing test. Empty means "
         "'no build'. Overrides ai_resolve.build_command in config.",
)
@click.option(
    "--claude-command",
    "claude_command",
    default=None,
    help="(stateless only) Executable used to invoke Claude. "
         "Overrides analyze_fails.command in config.",
)
@click.option(
    "--ai-backend",
    "ai_backend_cli",
    type=click.Choice(["cli", "codex", "api"]),
    default=None,
    help="How to reach the model: 'cli' spawns the claude binary, 'codex' "
         "spawns 'codex exec', 'api' talks to the Anthropic API directly "
         "using $ANTHROPIC_API_KEY. Overrides ai_backend in config.",
)
@click.option(
    "--prompt-file",
    "prompt_file_cli",
    default=None,
    help="(stateless only) Path to the analyze-fails prompt template. "
         "Overrides analyze_fails.prompt_file in config. Defaults to "
         "the prompt bundled with the releasy package.",
)
@click.option(
    "--timeout",
    "timeout_seconds",
    type=int,
    default=None,
    help="(stateless only) Per-invocation Claude timeout in seconds.",
)
@click.option(
    "--max-iterations",
    "max_iterations_cli",
    type=int,
    default=None,
    help="(stateless only) Hard cap on build attempts per failed test.",
)
@click.option(
    "--max-prs",
    "max_prs_cli",
    type=int,
    default=None,
    help="(stateless only) Cap on how many tracked PRs to process when "
         "--pr is omitted (0 = no cap). Overrides "
         "analyze_fails.max_prs_per_run.",
)
@click.option(
    "--only",
    "only",
    default=None,
    help="Restrict the multi-PR walk to a single tracked PR (URL — "
         "source or rebase) or a single feature / group ID. "
         "Mutually exclusive with --pr and --stateless. "
         "Exits non-zero if nothing matches.",
)
@click.pass_context
def analyze_fails_cmd(
    ctx: click.Context,
    pr_url: str | None,
    work_dir: str | None,
    dry_run: bool,
    push: bool,
    no_flaky_check: bool,
    no_baseline_check: bool,
    post_comment: bool | None,
    stateless: bool,
    origin_url: str | None,
    build_command_cli: str | None,
    claude_command: str | None,
    ai_backend_cli: str | None,
    prompt_file_cli: str | None,
    timeout_seconds: int | None,
    max_iterations_cli: int | None,
    max_prs_cli: int | None,
    only: str | None,
) -> None:
    """Walk failed CI on a PR (or every tracked PR), debug + fix per test.

    Reads every failed commit status (praktika reports and TestFlows
    suites). For each failed test, Claude:

    \b
    1. Reads the failure excerpt and the PR's diff.
    2. Decides "related to this PR" vs "unrelated flake on master".
    3. If related, reproduces the failure locally, fixes the test or
       the code under test, and commits.

    Tests failing in enough other tracked PRs are flagged to Claude as
    likely flaky (a hint, not a cutoff); --no-flaky-check disables this.

    Exit code is 1 on any per-PR failure; 0 on success even if every
    test was UNRELATED.
    """
    from releasy.analyze_fails import analyze_fails

    wd = Path(work_dir) if work_dir else None
    only_filter = _parse_walk_only(only, pr_url, stateless)

    stateless_only_set: list[str] = []
    if origin_url is not None:
        stateless_only_set.append("--origin")
    if build_command_cli is not None:
        stateless_only_set.append("--build-command")
    if claude_command is not None:
        stateless_only_set.append("--claude-command")
    if prompt_file_cli is not None:
        stateless_only_set.append("--prompt-file")
    if timeout_seconds is not None:
        stateless_only_set.append("--timeout")
    if max_iterations_cli is not None:
        stateless_only_set.append("--max-iterations")
    if max_prs_cli is not None:
        stateless_only_set.append("--max-prs")

    if not stateless and stateless_only_set:
        raise click.UsageError(
            f"{', '.join(stateless_only_set)} only apply with "
            "--stateless. Drop the flags, or add --stateless to skip "
            "the session/state/lock layer."
        )

    if stateless:
        from releasy.config import (
            build_stateless_analyze_fails_config,
            overlay_analyze_fails_overrides,
        )

        config = _load_config_if_present(ctx)
        if config is None:
            effective_origin = origin_url
            if not effective_origin:
                if not pr_url:
                    raise click.ClickException(
                        "Stateless run without config.yaml requires "
                        "either --origin or --pr (so the origin can be "
                        "derived from the PR URL)."
                    )
                effective_origin = _origin_from_pr_url(pr_url)
            config = build_stateless_analyze_fails_config(
                origin_url=effective_origin,
                work_dir=wd,
                claude_command=claude_command or "claude",
                ai_backend=ai_backend_cli or "cli",
                build_command=build_command_cli or "",
                prompt_file=prompt_file_cli,
                timeout_seconds=(
                    timeout_seconds if timeout_seconds is not None else 7200
                ),
                max_iterations=(
                    max_iterations_cli
                    if max_iterations_cli is not None else 6
                ),
                max_prs_per_run=(
                    max_prs_cli if max_prs_cli is not None else 0
                ),
            )
        else:
            if origin_url:
                config.origin.remote = origin_url
            overlay_analyze_fails_overrides(
                config,
                claude_command=claude_command,
                ai_backend=ai_backend_cli,
                build_command=build_command_cli,
                prompt_file=prompt_file_cli,
                timeout_seconds=timeout_seconds,
                max_iterations=max_iterations_cli,
                max_prs_per_run=max_prs_cli,
            )

        config.session = None
        config.stateless = True
        config.dry_run = dry_run
        if no_baseline_check:
            config.analyze_fails.baseline_check = False
        result = analyze_fails(
            config, pr_url=pr_url, work_dir=wd, dry_run=dry_run,
            push=push, no_flaky_check=no_flaky_check,
            post_comment=post_comment,
        )
        if not result.success:
            if result.error:
                raise click.ClickException(result.error)
            raise SystemExit(1)
        for r in result.runs:
            if r.comment_url:
                click.echo(f"PR comment: {r.comment_url}")
        return

    if pr_url is None:
        with _locked_config(ctx, session="optional") as config:
            config.dry_run = dry_run
            if ai_backend_cli is not None:
                config.ai_backend = ai_backend_cli
            if no_baseline_check:
                config.analyze_fails.baseline_check = False
            result = analyze_fails(
                config, pr_url=None, work_dir=wd, dry_run=dry_run,
                push=push, no_flaky_check=no_flaky_check,
                post_comment=post_comment, only=only_filter,
            )
            if not result.success:
                if result.error:
                    raise click.ClickException(result.error)
                raise SystemExit(1)
        return

    with _locked_config(ctx, session="skip") as config:
        config.dry_run = dry_run
        if ai_backend_cli is not None:
            config.ai_backend = ai_backend_cli
        if no_baseline_check:
            config.analyze_fails.baseline_check = False
        result = analyze_fails(
            config, pr_url=pr_url, work_dir=wd, dry_run=dry_run,
            push=push, no_flaky_check=no_flaky_check,
            post_comment=post_comment,
        )
        if not result.success:
            if result.error:
                raise click.ClickException(result.error)
            raise SystemExit(1)


@cli.command(
    name="rebase",
    short_help="Port an existing rebase PR onto a different target branch.",
)
@click.option(
    "--pr",
    "pr_url",
    default=None,
    help="GitHub URL of the rebase PR to port. When omitted, RelEasy "
         "walks every tracked rebase PR in the project state file and "
         "rebases each one (skipping any that already target --target).",
)
@click.option(
    "--target",
    "target_branch",
    required=True,
    help="Branch on origin to rebase the PR(s) onto (e.g. antalya-26.3). "
         "Must already exist on the origin remote.",
)
@click.option("--work-dir", default=None, help="Working directory for git operations")
@click.option(
    "--resolve-conflicts/--no-resolve-conflicts",
    default=True,
    help="Invoke the AI resolver on cherry-pick conflicts (requires "
         "ai_resolve.enabled in config). Default: on.",
)
@click.option(
    "--only",
    "only",
    default=None,
    help="Restrict the multi-PR walk to a single tracked PR (URL — "
         "source or rebase) or a single feature / group ID. "
         "Mutually exclusive with --pr. Exits non-zero if nothing matches.",
)
@click.option(
    "--dry-run",
    "dry_run",
    is_flag=True,
    default=False,
    help="Show what would happen without changing anything: no "
         "branches, no cherry-picks, no pushes, no PR opens / closes. "
         "Read-only GitHub fetches still happen.",
)
@click.pass_context
def rebase_cmd(
    ctx: click.Context,
    pr_url: str | None,
    target_branch: str,
    work_dir: str | None,
    resolve_conflicts: bool,
    only: str | None,
    dry_run: bool,
) -> None:
    """Re-port a rebase PR onto a different target branch.

    For each PR in scope:

    \b
    1. Skip when the PR already targets ``--target``.
    2. Branch off origin/<target>, cherry-pick the PR's commits one
       at a time (AI-resolving conflicts as they appear). If the
       cherry-pick path can't be made to apply, fall back to a single
       squashed ``git merge --squash`` of the PR's head onto the new
       target.
    3. Push the new branch and open a fresh PR (same title and body,
       prefixed with a ``Port of <old PR> onto <target>`` reference).
    4. Close the original PR with a ``superseded by <new PR>`` comment.

    Without ``--pr`` every tracked rebase PR in state is processed. The
    state file is never mutated.
    """
    from releasy.rebase import rebase_all_tracked, rebase_single

    wd = Path(work_dir) if work_dir else None
    only_filter = _parse_walk_only(only, pr_url, stateless=False)

    with _locked_config(ctx, session="skip") as config:
        config.dry_run = dry_run
        if pr_url is not None:
            summary = rebase_single(
                config, pr_url, target_branch,
                work_dir=wd, resolve_conflicts=resolve_conflicts,
            )
        else:
            summary = rebase_all_tracked(
                config, target_branch,
                work_dir=wd, resolve_conflicts=resolve_conflicts,
                only=only_filter,
            )
        if not summary.all_succeeded:
            raise SystemExit(1)


@cli.command(short_help="Build a release branch from a tag.")
@click.option(
    "--base-tag", "base_tag", required=True,
    help="Tag/ref to base the release on (must be present locally or "
         "fetchable from origin)",
)
@click.option("--name", required=True, help="Release branch name")
@click.option("--strict", is_flag=True, help="Abort if any enabled feature is not ok")
@click.option("--include-skipped", is_flag=True, help="Include skipped features in release")
@click.option("--work-dir", default=None, help="Working directory for git operations")
@click.pass_context
def release(
    ctx: click.Context,
    base_tag: str,
    name: str,
    strict: bool,
    include_skipped: bool,
    work_dir: str | None,
) -> None:
    """Build a release branch from a tag, merging finished ports onto it."""
    from releasy.release import build_release

    with _locked_config(ctx, session="optional") as config:
        wd = Path(work_dir) if work_dir else None
        if not build_release(config, base_tag, name, strict, include_skipped, wd):
            raise SystemExit(1)


@cli.command(
    name="draft-release",
    short_help="Generate a release changelog from merged PRs.",
)
@click.option(
    "--from", "from_ref", required=True,
    help="Lower bound of the release window (NON-inclusive). PRs merged "
         "AT/BEFORE this ref's date are excluded. Typically the previous "
         "release tag, or the upstream tag the branch forked from "
         "(e.g. v26.1.6.6-stable).",
)
@click.option(
    "--to", "to_ref", required=True,
    help="Upper bound of the release window (inclusive) and the draft "
         "release commitish. Usually the tip of the release branch or "
         "the tag you're cutting.",
)
@click.option(
    "--base", "base_branch", default=None,
    help="Branch whose merged PRs are collected. Defaults to the "
         "configured target branch, falling back to --to.",
)
@click.option(
    "--prs", "prs", multiple=True,
    help="Explicit PR URL(s) to include, bypassing discovery. Repeatable. "
         "Combine with --prs-file. When given, --base / the merge window "
         "are ignored.",
)
@click.option(
    "--prs-file", "prs_file", default=None,
    type=click.Path(dir_okay=False, exists=True, path_type=Path),
    help="File of PR URLs (one per line, '#' comments allowed) to include, "
         "merged with any --prs values.",
)
@click.option(
    "--name", "release_name", default=None,
    help="GitHub release tag (e.g. v26.1.6.20001.altinityantalya). "
         "Defaults to --to when --to is itself a tag; if --to is a "
         "commit / branch and --name is omitted, the draft release's "
         "tag field is left blank (no new tag is created). Used "
         "verbatim as the tag on the draft release; see --title for "
         "the human-readable heading.",
)
@click.option(
    "--title", "release_title", default=None,
    help="Human-readable title used in the changelog heading and as the "
         "draft release's display name. Defaults to a prettified --name "
         "(e.g. v26.1.6.20001.altinityantalya → "
         "'26.1.6.20001 Altinity Antalya').",
)
@click.option(
    "-o", "--output", "output_file",
    default=None,
    type=click.Path(dir_okay=False, file_okay=True, path_type=Path),
    help="Write the rendered markdown to this file instead of creating "
         "a draft release on GitHub.",
)
@click.option(
    "--work-dir", default=None,
    help="Existing clone used to resolve --from / --to refs and dates. "
         "When given, no config.yaml is needed: origin (and upstream, if "
         "the clone has one) are read from its git remotes. Without it, "
         "config.yaml supplies the origin remote and the work dir. An "
         "explicit --config always wins.",
)
@click.option(
    "--docker-image-url", default=None,
    help="Full URL to the Docker image (typically the SHA-pinned "
         "hub.docker.com/layers/... link). When omitted, RelEasy emits "
         "a placeholder ending in `sha256-TBD` so the digest can be "
         "filled in mechanically after the image is pushed.",
)
@click.option(
    "--build-report-url", default=None,
    help="Full URL to the CI build report (ci_run_report.html). When "
         "omitted, RelEasy resolves the workflow-run id from the "
         "build-artifacts bucket, falling back to a `RUN-ID-TBD` "
         "placeholder if CI has not published for the ref yet.",
)
@click.option(
    "--release-notes-url", default=None,
    help="Full URL to the docs.altinity.com release notes. When omitted, "
         "it is derived from the tag for projects whose docs are split by "
         "version (antalya, stable); other projects get no link.",
)
@click.pass_context
def draft_release_cmd(
    ctx: click.Context,
    from_ref: str,
    to_ref: str,
    base_branch: str | None,
    prs: tuple[str, ...],
    prs_file: Path | None,
    release_name: str | None,
    release_title: str | None,
    output_file: Path | None,
    work_dir: str | None,
    docker_image_url: str | None,
    build_report_url: str | None,
    release_notes_url: str | None,
) -> None:
    """Build a categorised release changelog from merged PRs.

    Collects PRs merged into ``--base`` in the ``--from``..``--to`` window
    (or the explicit ``--prs`` / ``--prs-file`` set), drops forward-ports,
    groups them by Changelog category, and renders Altinity release notes.

    With ``-o`` the markdown is written to disk. Otherwise a DRAFT GitHub
    release is created on origin (tag = ``--name``, commitish = ``--to``)
    and its URL printed.

    With ``--work-dir`` (and no ``--config``) remotes come from that clone
    and no ``config.yaml`` is read.
    """
    from releasy.changelog import emit_changelog
    from releasy.config import make_stateless_config_for_repo

    wd = Path(work_dir) if work_dir else None
    if wd is not None and ctx.obj.get("config_path") is None:
        try:
            config = make_stateless_config_for_repo(wd)
        except ValueError as exc:
            raise click.ClickException(str(exc))
    else:
        config = _load_and_verify(ctx, session="skip")

    explicit_prs: list[str] = list(prs)
    if prs_file is not None:
        for raw in prs_file.read_text().splitlines():
            line = raw.split("#", 1)[0].strip()
            if line:
                explicit_prs.append(line)

    if not emit_changelog(
        config,
        from_ref=from_ref,
        to_ref=to_ref,
        release_name=release_name,
        display_title=release_title,
        output_file=output_file,
        work_dir=wd,
        docker_image_url=docker_image_url,
        build_report_url=build_report_url,
        release_notes_url=release_notes_url,
        base_branch=base_branch,
        explicit_prs=explicit_prs or None,
    ):
        raise SystemExit(1)


@cli.command(
    name="setup-project",
    short_help="Create or verify the GitHub Project board.",
)
@click.pass_context
def setup_project_cmd(ctx: click.Context) -> None:
    """Create or verify a GitHub Project for status tracking.

    If notifications.github_project is set, verifies the project and its
    Status field, drops non-canonical Status options, and re-syncs cards
    from local state. Otherwise creates a new project and prints the URL
    to add to config.
    """
    from releasy.github_ops import setup_project
    from releasy.pipeline import sync_to_project

    with _locked_config(ctx, session="skip") as config:
        url = setup_project(config)
        if not url:
            raise SystemExit(1)
        click.echo(f"Project ready: {url}")
        if not config.notifications.github_project:
            click.echo(
                f"\nAdd this to your config.yaml:\n\n"
                f"notifications:\n"
                f"  github_project: {url}\n"
            )
            return
        sync_to_project(config)


@cli.group(
    name="project",
    short_help="Sync local state with the GitHub Project board.",
)
def project_cmd() -> None:
    """Sync local state with the GitHub Project board.

    ``push`` writes local state to the board (the board mirrors state).
    ``pull`` rebuilds local state from the board + GitHub PRs (state
    mirrors the world).
    """


@project_cmd.command(
    name="push",
    short_help="Push local state to the Project board.",
)
@click.pass_context
def project_push_cmd(ctx: click.Context) -> None:
    """Push the current local state to the GitHub Project board.

    Adds missing PR cards, refreshes existing ones and their Status, and
    deletes cards not backed by local state. No git, no PRs.
    """
    from releasy.pipeline import sync_to_project

    with _locked_config(ctx, session="optional") as config:
        if not sync_to_project(config):
            raise SystemExit(1)


@project_cmd.command(
    name="pull",
    short_help="Rebuild local state from GitHub + the project board.",
)
@click.pass_context
def project_pull_cmd(ctx: click.Context) -> None:
    """Rebuild the per-project state file from GitHub + the project board.

    For when local state is missing or stale (fresh machine, CI runner).
    GitHub API only — no git. Merges into any existing state file:
    local-only fields are kept, the board wins for `Skipped` and
    `AI Cost`, everything else is refreshed from GitHub.

    Requires notifications.github_project in config.
    """
    from releasy.import_state import import_from_github

    with _locked_config(ctx, session="optional") as config:
        if not import_from_github(config):
            raise SystemExit(1)


@cli.command(name="new", short_help="Scaffold a fresh config from the bundled template.")
@click.option(
    "--name",
    "name_opt",
    default=None,
    help="Project name (slug). Auto-generated from --target-branch + a 6-hex "
         "id when omitted.",
)
@click.option(
    "--target-branch",
    "target_branch",
    default=None,
    help="Target/base branch this project will port onto (e.g. antalya-26.3). "
         "Used to seed `target_branch:` in the config and, when --name is "
         "omitted, to derive the auto-generated name (`<target-branch>-<6hex>`).",
)
@click.option(
    "--project",
    "project_opt",
    default="",
    help="Short project identifier used in derived branch names "
         "(e.g. antalya). Left blank when not given so you can fill it in.",
)
@click.option(
    "--out",
    "out_path",
    default=None,
    type=click.Path(dir_okay=False, file_okay=True, path_type=Path),
    help="Where to write the new config. Defaults to ./config.yaml; refuses "
         "to overwrite an existing file.",
)
def new_cmd(
    name_opt: str | None,
    target_branch: str | None,
    project_opt: str,
    out_path: Path | None,
) -> None:
    """Scaffold a new releasy config and print its absolute path.

    Prints ONLY the absolute path on stdout, so it composes:

        cd $(dirname "$(releasy new --target-branch antalya-25.8)")
    """
    if name_opt is None:
        suffix = _short_id()
        if target_branch:
            name_opt = f"{target_branch}-{suffix}"
        else:
            name_opt = f"releasy-{suffix}"
    try:
        validate_project_name(name_opt)
    except ValueError as e:
        raise click.ClickException(str(e))

    if out_path is None:
        out_path = Path.cwd() / "config.yaml"
    out_path = out_path.expanduser().resolve()
    if out_path.exists():
        raise click.ClickException(
            f"{out_path} already exists. Pass --out to write somewhere else "
            f"or remove the existing file first."
        )

    templates_dir = Path(__file__).parent / "templates"
    config_tmpl = templates_dir / "config.yaml.tmpl"
    session_tmpl = templates_dir / "session.yaml.tmpl"
    if not config_tmpl.exists() or not session_tmpl.exists():
        raise click.ClickException(
            f"Bundled templates missing under {templates_dir}. This is a "
            "packaging bug — please report it."
        )

    session_stem = default_session_stem(name_opt, target_branch)
    session_path = out_path.parent / f"{session_stem}.session.yaml"
    if session_path.exists():
        raise click.ClickException(
            f"{session_path} already exists. Remove it or pass --out to "
            "write somewhere else."
        )

    rendered_config = _render_template(
        config_tmpl.read_text(),
        name=name_opt,
        target_branch=target_branch or "",
        project=project_opt or "",
    )
    rendered_session = session_tmpl.read_text()

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(rendered_config)
    session_path.write_text(rendered_session)

    # stdout carries only the path, for `cd $(dirname $(releasy new …))`.
    click.echo(str(out_path))
    click.echo(
        f"Created config for project {name_opt!r}.\n"
        f"  Config : {out_path}\n"
        f"  Session: {session_path}\n"
        f"  State  : {state_file_path(name_opt)}",
        err=True,
    )


@cli.command(name="list", short_help="List every releasy project on this machine.")
def list_cmd() -> None:
    """List every project found under the user's state dir.

    Outputs one row per project: ``name | phase | features | last_run | config``.
    """
    from rich.table import Table

    from releasy.termlog import console

    from releasy.state import _read_raw_state

    root = state_root()
    state_files = sorted(root.glob("*.state.yaml"))
    if not state_files:
        click.echo(f"No projects found under {root}.")
        click.echo(
            "Use `releasy new` to scaffold one, then `releasy run` to "
            "kick off the pipeline."
        )
        return

    table = Table(title=f"RelEasy projects ({root})", title_justify="left")
    table.add_column("Name", style="cyan")
    table.add_column("Phase")
    table.add_column("Features")
    table.add_column("Last run")
    table.add_column("Config")

    for path in state_files:
        name = path.name[: -len(".state.yaml")]
        raw = _read_raw_state(path)
        run_blob = raw.get("last_run") or {}
        phase = run_blob.get("phase") or "—"
        features = (run_blob.get("features") or {})
        ok = sum(
            1 for f in features.values()
            if (f or {}).get("status") in ("needs_review", "branch_created")
        )
        conflict = sum(
            1 for f in features.values()
            if (f or {}).get("status") == "conflict"
        )
        skipped = sum(
            1 for f in features.values()
            if (f or {}).get("status") == "skipped"
        )
        feat_summary = (
            f"{ok} ok / {conflict} conflict"
            + (f" / {skipped} skipped" if skipped else "")
        )
        table.add_row(
            name,
            phase,
            feat_summary,
            run_blob.get("started_at") or "—",
            raw.get("config_path") or "—",
        )
    console.print(table)


@cli.command(name="ls", short_help="Alias for `releasy list`.")
def ls_cmd() -> None:
    """Alias for `releasy list`."""
    list_cmd.callback()  # type: ignore[misc]


@cli.command(short_help="Print the state file path for the resolved config.")
@click.pass_context
def where(ctx: click.Context) -> None:
    """Print the absolute path of this project's state file."""
    config = _load_config_or_exit(ctx.obj["config_path"])
    click.echo(str(config.state_path))


@cli.command(short_help="Rebind state to the current config (use after moving config).")
@click.pass_context
def adopt(ctx: click.Context) -> None:
    """Rewrite the state file's stored config_path to the current config.

    Use after moving / renaming config.yaml to avoid the
    ownership-collision check. Creates an empty state file if none exists.
    """
    from releasy.locks import project_lock
    from releasy.state import adopt_ownership

    config = _load_config_or_exit(ctx.obj["config_path"])
    with project_lock(config):
        previous, state_path = adopt_ownership(config)
    if previous is None:
        click.echo(
            f"Project {config.name!r} is now bound to "
            f"{config.config_path}.\nState file: {state_path}"
        )
    else:
        click.echo(
            f"Rebound project {config.name!r} from {previous} to "
            f"{config.config_path}.\nState file: {state_path}"
        )


@cli.group()
def feature() -> None:
    """Manage the static `features:` list in the session file."""


@feature.command(name="add")
@click.option("--id", "feature_id", required=True, help="Feature identifier")
@click.option("--source-branch", required=True, help="Existing branch with feature commits")
@click.option("--description", required=True, help="Feature description")
@click.pass_context
def feature_add(
    ctx: click.Context, feature_id: str, source_branch: str, description: str,
) -> None:
    """Add a new feature branch."""
    from releasy.feature import add_feature

    with _locked_config(ctx, session="required") as config:
        if not add_feature(config, feature_id, source_branch, description):
            raise SystemExit(1)


@feature.command(name="enable")
@click.option("--id", "feature_id", required=True, help="Feature identifier")
@click.pass_context
def feature_enable(ctx: click.Context, feature_id: str) -> None:
    """Enable a feature branch."""
    from releasy.feature import enable_feature

    with _locked_config(ctx, session="required") as config:
        if not enable_feature(config, feature_id):
            raise SystemExit(1)


@feature.command(name="disable")
@click.option("--id", "feature_id", required=True, help="Feature identifier")
@click.pass_context
def feature_disable(ctx: click.Context, feature_id: str) -> None:
    """Disable a feature branch."""
    from releasy.feature import disable_feature

    with _locked_config(ctx, session="required") as config:
        if not disable_feature(config, feature_id):
            raise SystemExit(1)


@feature.command(name="remove")
@click.option("--id", "feature_id", required=True, help="Feature identifier")
@click.pass_context
def feature_remove(ctx: click.Context, feature_id: str) -> None:
    """Remove a feature branch."""
    from releasy.feature import remove_feature

    with _locked_config(ctx, session="required") as config:
        if not remove_feature(config, feature_id):
            raise SystemExit(1)


@feature.command(name="list")
@click.pass_context
def feature_list(ctx: click.Context) -> None:
    """List all configured features."""
    from releasy.feature import list_features

    config = _load_and_verify(ctx, session="required")
    list_features(config)


@cli.group()
def pr() -> None:
    """Add, remove, and list PR membership in the session."""


@pr.command(name="add")
@click.argument("url")
@click.option(
    "--group", "group_id", default=None,
    help="Add to this group's prs list instead of top-level include_prs. "
         "The group must already exist in the session.",
)
@click.option(
    "--context", "context", default=None,
    help="Per-PR ai_context note attached to this URL. Surfaced to the "
         "conflict resolver alongside any unit-level ai_context.",
)
@click.pass_context
def pr_add(
    ctx: click.Context, url: str, group_id: str | None, context: str | None,
) -> None:
    """Add a PR URL to the session.

    Adds to ``pr_sources.include_prs``, or to a group's ``prs`` with
    ``--group``. Validates the URL via GitHub, is idempotent, and removes
    it from ``exclude_prs``.
    """
    from releasy.pr_membership import add_pr

    with _locked_config(ctx, session="required") as config:
        if not add_pr(config, url, group_id=group_id, context=context):
            raise SystemExit(1)


@pr.command(name="remove")
@click.argument("url")
@click.option(
    "--keep-discovery", is_flag=True,
    help="Don't append the URL to exclude_prs. Without this flag, "
         "removal adds the URL to exclude_prs so label-driven discovery "
         "doesn't re-add it on the next refresh.",
)
@click.pass_context
def pr_remove(
    ctx: click.Context, url: str, keep_discovery: bool,
) -> None:
    """Remove a PR URL from session and state.

    Drops the URL from ``include_prs``, every group's ``prs``, and its
    state entry. Refused for a PR in a multi-PR group in state.
    """
    from releasy.pr_membership import remove_pr

    with _locked_config(ctx, session="required") as config:
        if not remove_pr(config, url, keep_discovery=keep_discovery):
            raise SystemExit(1)


@pr.command(name="list")
@click.pass_context
def pr_list(ctx: click.Context) -> None:
    """List every PR URL the session references."""
    from releasy.pr_membership import list_prs

    config = _load_and_verify(ctx, session="required")
    list_prs(config)


