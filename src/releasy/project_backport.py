"""Stateless batch backport driven by a GitHub Project.

For each project item whose content is an upstream (ClickHouse/ClickHouse)
PR tagged with ``--version`` in its ``Port Versions`` field, open a backport
PR into ``--target`` on origin (Altinity/ClickHouse) and add the new PR back
to the project. Stateless and idempotent; only ever opens PRs into origin.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from releasy.termlog import console

from releasy.config import Config, UpstreamConfig, make_stateless_config
from releasy.git_ops import (
    abort_in_progress_op,
    branch_exists,
    cherry_pick_sha,
    create_branch_from_ref,
    ensure_work_repo,
    fetch_commit,
    fetch_remote,
    force_push,
    is_operation_in_progress,
    local_branch_exists,
    run_git,
    stash_and_clean,
    update_submodules,
)
from releasy.github_ops import (
    PRInfo,
    _add_item_by_content_id,
    _get_pr_node_id,
    _get_project_id,
    _list_project_fields,
    _parse_project_url,
    _set_item_field,
    _set_item_text_field,
    create_pull_request,
    ensure_label,
    fetch_pr_by_number,
    find_latest_pr_for_branch,
    find_open_backport_pr,
    get_origin_repo_slug,
    list_project_items_for_backport,
    parse_pr_url,
    slug_to_https_url,
)
from releasy.stateless import _try_ai_resolve


# The only repos this command ever reads from / writes to.
UPSTREAM_SLUG = "ClickHouse/ClickHouse"
DEFAULT_ORIGIN = "git@github.com:Altinity/ClickHouse.git"

# Project field the views filter on; also what we set on each new card.
PORT_VERSIONS_FIELD = "Port Versions"
# Heading in the ClickHouse PR template; everything from here down is
# copied verbatim into the backport PR body ("CI/CD options and below").
CI_CD_SECTION_HEADER = "CI/CD Options"


# ---------------------------------------------------------------------------
# Public dataclasses
# ---------------------------------------------------------------------------


@dataclass
class ProjectBackportOptions:
    """All inputs for ``releasy project-backport`` (built from CLI flags)."""
    project_url: str
    version: str
    target: str
    origin: str = DEFAULT_ORIGIN
    work_dir: Path | None = None
    resolve_conflicts: bool = False
    build_command: str = ""
    claude_command: str = "claude"
    prompt_file: str | None = None
    timeout_seconds: int = 7200
    max_iterations: int = 5
    dry_run: bool = False
    limit: int | None = None


@dataclass
class ItemOutcome:
    upstream_number: int
    status: str            # "created" | "skipped" | "failed" | "would_create"
    pr_url: str | None = None
    reason: str | None = None


@dataclass
class ProjectBackportResult:
    outcomes: list[ItemOutcome] = field(default_factory=list)
    # Set when the whole run could not start (bad project URL, unresolved
    # project, missing 'Port Versions' field, …). Distinct from per-item
    # failures; the CLI surfaces it as a hard error.
    fatal: str | None = None

    @property
    def had_failures(self) -> bool:
        return any(o.status == "failed" for o in self.outcomes)


# ---------------------------------------------------------------------------
# Pure helpers (unit-tested without network)
# ---------------------------------------------------------------------------


def _sanitize_ref_component(value: str) -> str:
    """Make ``value`` safe to embed in a git ref."""
    return re.sub(r"[^A-Za-z0-9._-]+", "-", value).strip("-") or "x"


def _backport_branch(version: str, upstream_number: int) -> str:
    """Deterministic branch name so re-runs and PR lookups line up."""
    return f"backport/{_sanitize_ref_component(version)}/{upstream_number}"


def _port_versions_includes(value: str | None, version: str) -> bool:
    """True when a ``Port Versions`` value (TEXT or single-select) lists
    ``version``. Word-boundary match so ``24.8`` matches ``"24.8, 25.3"``
    but not ``"24.80"`` or ``"24.8.14"``.
    """
    if not value:
        return False
    pattern = rf"(?<![\d.]){re.escape(version)}(?![\d.])"
    return re.search(pattern, value) is not None


def _item_qualifies(item: dict, version: str, upstream_slug: str) -> bool:
    """A project item we should back-port: an upstream PR tagged for ``version``."""
    if item.get("content_typename") != "PullRequest":
        return False
    if (item.get("repo_slug") or "").lower() != upstream_slug.lower():
        return False
    pv = (item.get("field_values") or {}).get(PORT_VERSIONS_FIELD.lower())
    return _port_versions_includes(pv, version)


def _pr_title(version: str, pr: PRInfo) -> str:
    return f"{version} Backport of #{pr.number} - {pr.title}"


def _build_changelog_block_for_pr(pr: PRInfo) -> str | None:
    """Upstream PR's changelog category + entry, with attribution appended."""
    from releasy.pipeline import (
        _extract_changelog_category,
        _extract_changelog_entry,
        render_changelog_block,
    )

    return render_changelog_block(
        _extract_changelog_category(pr.body or ""),
        _extract_changelog_entry(pr.body or ""),
        [pr],
    )


def _ci_options_section(template_text: str | None) -> str:
    """The ``CI/CD Options`` heading and everything below it, verbatim.

    Falls back to ``pipeline._DEFAULT_CI_CD_OPTIONS_BLOCK`` when the
    template is missing or has no such section.
    """
    from releasy.pipeline import _DEFAULT_CI_CD_OPTIONS_BLOCK

    if template_text:
        lines = template_text.splitlines()
        target = CI_CD_SECTION_HEADER.strip().lower()
        for i, line in enumerate(lines):
            m = re.match(r"^#{1,6}\s+(.+?)\s*$", line)
            if m and m.group(1).strip().lower() == target:
                return "\n".join(lines[i:]).rstrip()
    return _DEFAULT_CI_CD_OPTIONS_BLOCK.rstrip()


def _build_pr_body(changelog_block: str | None, ci_block: str) -> str:
    parts = [p for p in (changelog_block, ci_block) if p]
    return ("\n\n".join(parts)).strip() + "\n"


# ---------------------------------------------------------------------------
# Port Versions field
# ---------------------------------------------------------------------------


def _find_field_node(project_id: str, name: str) -> dict | None:
    target = name.lower()
    for f in _list_project_fields(project_id):
        if (f.get("name") or "").lower() == target:
            return f
    return None


def _find_option_id(field_node: dict, version: str) -> str | None:
    target = version.lower()
    for opt in field_node.get("options") or []:
        if (opt.get("name") or "").lower() == target:
            return opt.get("id")
    return None


def _set_port_versions(
    project_id: str, field_node: dict, item_id: str, version: str,
) -> tuple[bool, str | None]:
    """Set the new card's ``Port Versions`` to ``version``.

    For a SINGLE_SELECT field the ``version`` option must already exist
    (qualifying items are tagged with it, so it does). We deliberately do
    NOT mutate a user-owned field's option list to create one.
    """
    field_id = field_node.get("id")
    dtype = field_node.get("dataType")
    if not field_id:
        return False, "'Port Versions' field has no id"

    if dtype == "TEXT":
        ok = _set_item_text_field(project_id, item_id, field_id, version)
        return ok, None if ok else "GraphQL set-text failed"

    if dtype == "SINGLE_SELECT":
        option_id = _find_option_id(field_node, version)
        if option_id is None:
            return False, (
                f"'{version}' is not an option on the single-select "
                "'Port Versions' field — add it in the project UI"
            )
        ok = _set_item_field(project_id, item_id, field_id, option_id)
        return ok, None if ok else "GraphQL set-single-select failed"

    return False, f"'Port Versions' is {dtype!r}; expected TEXT or SINGLE_SELECT"


# ---------------------------------------------------------------------------
# Work dir + config
# ---------------------------------------------------------------------------


def _default_work_dir() -> Path:
    """Stable cache clone reused across runs when ``--work-dir`` is omitted."""
    import os

    cache_root = os.environ.get("XDG_CACHE_HOME") or str(Path.home() / ".cache")
    return Path(cache_root) / "releasy" / "Altinity-ClickHouse"


def _build_config(opts: ProjectBackportOptions) -> Config:
    config = make_stateless_config(
        opts.origin,
        work_dir=opts.work_dir or _default_work_dir(),
        push=True,
        auto_pr=True,
        ai_enabled=opts.resolve_conflicts,
        ai_command=opts.claude_command,
        ai_build_command=opts.build_command,
        ai_prompt_file=opts.prompt_file,
        ai_timeout_seconds=opts.timeout_seconds,
        ai_max_iterations=opts.max_iterations,
    )
    # Upstream is fetch-only; declaring it lets the AI resolver detect
    # missing prerequisites against ClickHouse/ClickHouse history.
    config.upstream = UpstreamConfig(remote=slug_to_https_url(UPSTREAM_SLUG))
    config.dry_run = opts.dry_run
    return config


# ---------------------------------------------------------------------------
# Per-item backport
# ---------------------------------------------------------------------------


@dataclass
class _RepoState:
    repo_path: Path
    remote: str
    target_ref: str
    template_text: str | None


def _prepare_repo(config: Config, opts: ProjectBackportOptions) -> _RepoState | None:
    """Clone/reuse the work repo, fetch origin, verify the target branch.

    Returns ``None`` (with a console error) when the target branch is
    missing on origin — there's nothing to base backports on.
    """
    wd = config.resolve_work_dir(opts.work_dir)
    console.print(f"[dim]Working directory: {wd}[/dim]")
    console.print(f"[dim]Origin: {opts.origin}[/dim]")

    repo_path, fresh = ensure_work_repo(config, wd)
    console.print(f"[dim]Repo: {repo_path}{' (freshly cloned)' if fresh else ''}[/dim]")

    remote = config.origin.remote_name
    console.print(f"Fetching [cyan]{remote}[/cyan]...", end=" ")
    fetch_remote(repo_path, remote)
    console.print("[green]done[/green]")

    if is_operation_in_progress(repo_path):
        abort_in_progress_op(repo_path)

    if not branch_exists(repo_path, opts.target, remote):
        console.print(
            f"[red]✗[/red] target branch {opts.target!r} does not exist on "
            f"remote {remote!r} ({opts.origin}). Create + push it first."
        )
        return None

    target_ref = f"{remote}/{opts.target}"
    template_text = _read_pr_template(repo_path, target_ref)
    return _RepoState(repo_path, remote, target_ref, template_text)


def _read_pr_template(repo_path: Path, target_ref: str) -> str | None:
    res = run_git(
        ["show", f"{target_ref}:.github/PULL_REQUEST_TEMPLATE.md"],
        repo_path, check=False,
    )
    return res.stdout if res.returncode == 0 else None


def _cleanup_branch(repo_path: Path, branch: str, target_ref: str) -> None:
    if is_operation_in_progress(repo_path):
        abort_in_progress_op(repo_path)
    if local_branch_exists(repo_path, branch):
        run_git(["checkout", "--detach", target_ref], repo_path, check=False)
        run_git(["branch", "-D", branch], repo_path, check=False)


def _backport_one(
    config: Config,
    opts: ProjectBackportOptions,
    repo: _RepoState,
    project_id: str,
    field_node: dict,
    item: dict,
    upstream: PRInfo,
) -> ItemOutcome:
    """Cherry-pick, push, open the PR, and register it on the project."""
    version = opts.version
    n = upstream.number
    branch = _backport_branch(version, n)

    # Fresh branch off the target tip.
    if is_operation_in_progress(repo.repo_path):
        abort_in_progress_op(repo.repo_path)
    stash_and_clean(repo.repo_path)
    create_branch_from_ref(repo.repo_path, branch, repo.target_ref)
    console.print("    [dim]initialising submodules...[/dim]")
    update_submodules(repo.repo_path)

    # Cherry-pick the merge commit (-m 1).
    fetch_url = slug_to_https_url(UPSTREAM_SLUG)
    if not fetch_commit(repo.repo_path, fetch_url, upstream.merge_commit_sha):
        _cleanup_branch(repo.repo_path, branch, repo.target_ref)
        return ItemOutcome(n, "failed", reason="could not fetch merge commit")

    cp = cherry_pick_sha(
        repo.repo_path, upstream.merge_commit_sha,
        mainline=1, abort_on_conflict=False,
    )
    if cp.already_applied:
        _cleanup_branch(repo.repo_path, branch, repo.target_ref)
        return ItemOutcome(n, "skipped", reason="already present in target")

    if not cp.success:
        if not cp.conflict_files:
            _cleanup_branch(repo.repo_path, branch, repo.target_ref)
            return ItemOutcome(
                n, "failed", reason=cp.error_message or "cherry-pick failed",
            )
        if opts.resolve_conflicts:
            ok, err = _try_ai_resolve(
                config, repo.repo_path, branch, opts.target, upstream,
                cp.conflict_files, "backport",
            )
            if not ok:
                _cleanup_branch(repo.repo_path, branch, repo.target_ref)
                return ItemOutcome(n, "failed", reason=f"AI resolve failed: {err}")
        else:
            _cleanup_branch(repo.repo_path, branch, repo.target_ref)
            return ItemOutcome(
                n, "failed",
                reason=(
                    "cherry-pick conflicted ("
                    + ", ".join(cp.conflict_files[:5])
                    + "); re-run with --resolve-conflicts --build-command"
                ),
            )

    # Push + open the PR.
    force_push(repo.repo_path, branch, config)

    title = _pr_title(version, upstream)
    body = _build_pr_body(
        _build_changelog_block_for_pr(upstream),
        _ci_options_section(repo.template_text),
    )
    ensure_label(config, version)
    pr_url = create_pull_request(
        config, branch, opts.target, title, body, labels=[version],
    )
    if not pr_url:
        return ItemOutcome(
            n, "failed",
            reason="branch pushed but PR creation failed (see warnings)",
        )

    _register_on_project(config, project_id, field_node, item, pr_url, version)
    return ItemOutcome(n, "created", pr_url=pr_url)


def _register_on_project(
    config: Config, project_id: str, field_node: dict,
    item: dict, pr_url: str, version: str,
) -> None:
    """Add the new PR to the project and set its Port Versions (best-effort)."""
    parsed = parse_pr_url(pr_url)
    if not parsed:
        console.print(f"    [yellow]![/yellow] could not parse new PR URL {pr_url}")
        return
    owner, repo, number = parsed
    node = _get_pr_node_id(f"{owner}/{repo}", number)
    if not node:
        console.print("    [yellow]![/yellow] could not resolve new PR node id")
        return
    new_item_id = _add_item_by_content_id(project_id, node[0])
    if not new_item_id:
        console.print(
            "    [yellow]![/yellow] could not add PR to project "
            "(token missing 'project' scope?)"
        )
        return
    ok, err = _set_port_versions(project_id, field_node, new_item_id, version)
    if ok:
        console.print(f"    [green]✓[/green] added to project, {PORT_VERSIONS_FIELD} = {version}")
    else:
        console.print(
            f"    [yellow]![/yellow] added to project but could not set "
            f"{PORT_VERSIONS_FIELD}: {err}"
        )


# ---------------------------------------------------------------------------
# Main entry
# ---------------------------------------------------------------------------


def run_project_backport(opts: ProjectBackportOptions) -> ProjectBackportResult:
    """Execute the batch backport described by ``opts``.

    Never raises for per-item failures (they're collected into the result);
    only returns early for whole-run problems (bad project URL, missing
    field, missing target branch).
    """
    result = ProjectBackportResult()

    parsed = _parse_project_url(opts.project_url)
    if not parsed:
        result.fatal = f"could not parse project URL: {opts.project_url!r}"
        console.print(f"[red]✗[/red] {result.fatal}")
        return result
    owner, number, is_org = parsed

    config = _build_config(opts)
    origin_slug = get_origin_repo_slug(config)
    if not origin_slug:
        result.fatal = f"could not parse origin remote: {opts.origin!r}"
        console.print(f"[red]✗[/red] {result.fatal}")
        return result

    project_id = _get_project_id(owner, number, is_org)
    if not project_id:
        result.fatal = (
            "could not resolve project (missing RELEASY_GITHUB_TOKEN, or "
            "token lacks 'read:project' / 'project' scope)."
        )
        console.print(f"[red]✗[/red] {result.fatal}")
        return result

    field_node = _find_field_node(project_id, PORT_VERSIONS_FIELD)
    if not field_node:
        result.fatal = (
            f"project has no {PORT_VERSIONS_FIELD!r} field — cannot filter "
            "items or tag new cards."
        )
        console.print(f"[red]✗[/red] {result.fatal}")
        return result

    items = list_project_items_for_backport(project_id)
    qualifying = [it for it in items if _item_qualifies(it, opts.version, UPSTREAM_SLUG)]
    # Newest upstream PR first, then apply --limit.
    qualifying.sort(key=lambda it: it.get("pr_number") or 0, reverse=True)
    if opts.limit is not None:
        qualifying = qualifying[: opts.limit]

    console.print(
        f"\n[bold]{len(qualifying)}[/bold] upstream PR(s) tagged "
        f"{PORT_VERSIONS_FIELD}={opts.version} to backport onto "
        f"[cyan]{opts.target}[/cyan]"
        + (" [yellow](dry-run)[/yellow]" if opts.dry_run else "")
    )
    if not qualifying and items:
        # Don't leave the user guessing why a non-empty board produced no
        # work — the filter is upstream-PR content + Port Versions.
        console.print(
            f"[dim]No items matched: need content = a {UPSTREAM_SLUG} PR with "
            f"{PORT_VERSIONS_FIELD} including {opts.version!r}.[/dim]"
        )

    repo: _RepoState | None = None
    for it in qualifying:
        n = it.get("pr_number")
        console.print(f"\n[bold]#{n}[/bold] (upstream {UPSTREAM_SLUG}#{n})")
        try:
            # Read-only resolution first: terminal outcome (skip / would-
            # create / fetch failure) or an upstream PR that needs porting.
            terminal, upstream = _resolve_item(config, opts, it)
            if terminal is not None:
                oc = terminal
            else:
                # Clone/verify the repo lazily on the first real backport,
                # then reuse it. A missing target branch aborts the run.
                if repo is None:
                    repo = _prepare_repo(config, opts)
                    if repo is None:
                        result.fatal = (
                            f"target branch {opts.target!r} does not exist on "
                            f"origin ({opts.origin})"
                        )
                        break
                oc = _backport_one(
                    config, opts, repo, project_id, field_node, it, upstream,
                )
        except Exception as exc:  # never let one item abort the batch
            oc = ItemOutcome(n or 0, "failed", reason=f"unexpected error: {exc}")
        result.outcomes.append(oc)
        _print_outcome(oc)

    _print_summary(result)
    return result


def _resolve_item(
    config: Config,
    opts: ProjectBackportOptions,
    item: dict,
) -> tuple[ItemOutcome | None, PRInfo | None]:
    """Read-only: decide skip / would-create, else hand back the upstream PR.

    Returns ``(terminal_outcome, None)`` when the item needs no porting
    (already backported, couldn't fetch, not merged, or dry-run), or
    ``(None, upstream_pr)`` when the item should be backported.
    """
    n = item.get("pr_number")
    branch = _backport_branch(opts.version, n)

    # Cheapest, immediately-consistent check first: any-state PR on our
    # deterministic branch is unambiguously our backport (covers open,
    # merged, and closed — so a re-run never duplicates it).
    existing = find_latest_pr_for_branch(config, branch, base=opts.target)
    if existing is not None:
        return ItemOutcome(
            n, "skipped", pr_url=existing.url,
            reason=f"backport already exists ({existing.state})",
        ), None

    upstream = fetch_pr_by_number(config, int(n), slug=UPSTREAM_SLUG, include_closed=True)
    if upstream is None:
        return ItemOutcome(n or 0, "failed", reason="could not fetch upstream PR"), None

    if upstream.state != "merged" or not upstream.merge_commit_sha:
        return ItemOutcome(n, "skipped", reason="upstream PR not merged"), None

    # Secondary: an open backport opened from a different branch.
    other = find_open_backport_pr(config, opts.target, n, upstream.url)
    if other:
        return ItemOutcome(
            n, "skipped", pr_url=other, reason="backport already exists",
        ), None

    if opts.dry_run:
        return ItemOutcome(
            n, "would_create",
            reason=f"would open: {_pr_title(opts.version, upstream)}",
        ), None

    return None, upstream


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------


def _print_outcome(oc: ItemOutcome) -> None:
    if oc.status == "created":
        console.print(f"    [green]✓ created[/green] {oc.pr_url}")
    elif oc.status == "would_create":
        console.print(f"    [cyan]→ {oc.reason}[/cyan]")
    elif oc.status == "skipped":
        extra = f" — {oc.pr_url}" if oc.pr_url else ""
        console.print(f"    [yellow]↷ skipped[/yellow] ({oc.reason}){extra}")
    else:
        console.print(f"    [red]✗ failed[/red] — {oc.reason}")


def _print_summary(result: ProjectBackportResult) -> None:
    created = [o for o in result.outcomes if o.status == "created"]
    would = [o for o in result.outcomes if o.status == "would_create"]
    skipped = [o for o in result.outcomes if o.status == "skipped"]
    failed = [o for o in result.outcomes if o.status == "failed"]
    console.print(
        f"\n[bold]Summary:[/bold] "
        f"{len(created)} created, "
        + (f"{len(would)} would-create, " if would else "")
        + f"{len(skipped)} skipped, {len(failed)} failed"
    )
    for o in failed:
        console.print(f"  [red]•[/red] #{o.upstream_number}: {o.reason}")
