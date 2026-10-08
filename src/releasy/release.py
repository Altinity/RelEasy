"""Release branch construction: base branch from a tag, then one squashed PR per feature."""

from __future__ import annotations

from pathlib import Path

from releasy.termlog import console

from releasy.config import Config, FeatureConfig
from releasy.git_ops import (
    get_conflict_files,
    run_git,
    count_commits,
    create_branch_from_ref,
    ensure_work_repo,
    force_push,
    get_branch_tip,
    resolve_ref,
    squash_commits,
    stash_and_clean,
)
from releasy.github_ops import create_pull_request
from releasy.state import load_state


def _feature_pr_branch(release_name: str, feature_id: str) -> str:
    return f"{release_name}/feat/{feature_id}"


def _build_pr_body(
    feat: FeatureConfig,
    n_commits: int,
    release_name: str,
    original_pr_body: str | None = None,
) -> str:
    lines = [
        f"## {feat.description}",
        "",
        f"Feature `{feat.id}` squashed into a single commit ({n_commits} original commit(s)).",
        "",
    ]

    if original_pr_body:
        lines.append("### Original PR Description")
        lines.append("")
        lines.append(original_pr_body)
        lines.append("")

    if feat.depends_on:
        dep_links = []
        for dep_id in feat.depends_on:
            dep_branch = _feature_pr_branch(release_name, dep_id)
            dep_links.append(f"`{dep_branch}`")
        lines.append(f"**Merge after:** {', '.join(dep_links)}")
        lines.append("")

    lines.append("---")
    lines.append("*Created automatically by RelEasy.*")
    return "\n".join(lines)


def build_release(
    config: Config,
    base_tag: str,
    branch_name: str,
    strict: bool = False,
    include_skipped: bool = False,
    work_dir: Path | None = None,
) -> bool:
    state = load_state(config)

    existing_ids = {f.id for f in config.features}
    for fid, fs in state.features.items():
        if fs.pr_number and fid not in existing_ids:
            config.features.append(FeatureConfig(
                id=fid,
                description=fs.pr_title or f"PR #{fs.pr_number}",
                source_branch="",
                enabled=True,
            ))

    warnings = []
    for feat in config.enabled_features:
        fs = state.features.get(feat.id)
        if fs is None:
            warnings.append(f"  {feat.id}: no state (never run)")
            continue
        if fs.status == "conflict":
            warnings.append(f"  {feat.id}: [red]conflict[/red]")
        elif fs.status == "skipped" and not include_skipped:
            warnings.append(
                f"  {feat.id}: [yellow]skipped[/yellow] (use --include-skipped to include)"
            )

    if warnings:
        console.print("[yellow]Warnings:[/yellow]")
        for w in warnings:
            console.print(w)
        if strict:
            console.print("\n[red]Aborting due to --strict flag.[/red]")
            return False
        console.print()

    features_to_include: list[FeatureConfig] = []
    for feat in config.enabled_features:
        fs = state.features.get(feat.id)
        if fs is None:
            continue
        if fs.status == "conflict":
            console.print(f"[yellow]Skipping {feat.id} (conflict)[/yellow]")
            continue
        if fs.status == "skipped" and not include_skipped:
            console.print(f"[yellow]Skipping {feat.id} (skipped)[/yellow]")
            continue
        features_to_include.append(feat)

    work_dir = config.resolve_work_dir(work_dir)
    console.print(f"[dim]Working directory: {work_dir}[/dim]")

    console.print("[dim]Setting up repository...[/dim]")
    repo_path, _freshly_cloned = ensure_work_repo(config, work_dir)
    console.print(f"[dim]Repo: {repo_path}[/dim]")

    from releasy.git_ops import fetch_remote, ref_exists_locally
    console.print(f"Fetching [cyan]{config.origin.remote_name}[/cyan]...", end=" ")
    fetch_remote(repo_path, config.origin.remote_name)
    console.print("[green]done[/green]")

    console.print(
        f"\n[bold]Creating release branch[/bold] [cyan]{branch_name}[/cyan] "
        f"from tag [yellow]{base_tag}[/yellow]"
    )

    tag_sha = resolve_ref(repo_path, base_tag)
    if tag_sha is None:
        console.print(
            f"[red]Tag {base_tag} not found[/red] — fetch it into "
            f"[cyan]{config.origin.remote_name}[/cyan] first."
        )
        return False
    if not ref_exists_locally(repo_path, base_tag):
        console.print(f"[dim]{base_tag} resolved from {config.origin.remote_name}[/dim]")

    create_branch_from_ref(repo_path, branch_name, tag_sha)
    console.print(f"  [green]✓[/green] Branch created at {tag_sha[:12]}")

    if config.push:
        console.print(f"\n[bold]Pushing base branch[/bold] [cyan]{branch_name}[/cyan]")
        force_push(repo_path, branch_name, config)
        console.print("  [green]✓[/green] Pushed")
    else:
        console.print("\n[dim]Skipping push of base branch (push not enabled)[/dim]")

    console.print("\n[bold]Creating per-feature branches and PRs[/bold]")

    pr_results: list[tuple[str, str, str | None]] = []

    for feat in features_to_include:
        fs = state.features.get(feat.id)
        if not fs or not fs.branch_name or not fs.base_commit:
            console.print(f"\n  [yellow]Skipping {feat.id} — no branch state[/yellow]")
            continue

        feat_pr_branch = _feature_pr_branch(branch_name, feat.id)
        console.print(f"\n  [cyan]{feat_pr_branch}[/cyan] ({feat.id}: {feat.description})")

        feat_remote_ref = f"{config.origin.remote_name}/{fs.branch_name}"
        feat_tip = get_branch_tip(repo_path, feat_remote_ref)

        n_feat = count_commits(repo_path, fs.base_commit, feat_tip)
        if n_feat == 0:
            console.print("    [dim]No unique commits — skipping[/dim]")
            continue

        stash_and_clean(repo_path)
        create_branch_from_ref(repo_path, feat_pr_branch, branch_name)

        tmp = f"_releasy_squash_{feat.id}"
        run_git(["checkout", "-b", tmp, feat_tip], repo_path, check=False)

        squash_result = squash_commits(
            repo_path,
            fs.base_commit,
            f"[releasy] {feat.id}: {feat.description}",
        )

        if not squash_result.success:
            console.print(
                f"    [red]✗[/red] Failed to squash: {squash_result.error_message}"
            )
            stash_and_clean(repo_path)
            run_git(["checkout", branch_name], repo_path)
            run_git(["branch", "-D", tmp], repo_path, check=False)
            run_git(["branch", "-D", feat_pr_branch], repo_path, check=False)
            continue

        squashed_sha = get_branch_tip(repo_path, "HEAD")

        run_git(["checkout", feat_pr_branch], repo_path)
        cp = run_git(["cherry-pick", squashed_sha], repo_path, check=False)
        run_git(["branch", "-D", tmp], repo_path, check=False)

        if cp.returncode != 0:
            console.print("    [red]✗[/red] Conflict applying squashed feature!")
            for f in get_conflict_files(repo_path):
                console.print(f"      [red]•[/red] {f}")
            run_git(["cherry-pick", "--abort"], repo_path, check=False)
            stash_and_clean(repo_path)
            run_git(["checkout", branch_name], repo_path)
            run_git(["branch", "-D", feat_pr_branch], repo_path, check=False)
            continue

        pr_url = None
        if config.push:
            force_push(repo_path, feat_pr_branch, config)
            console.print(f"    [green]✓[/green] Pushed ({n_feat} commits squashed into 1)")

            original_body = fs.pr_body if fs.pr_body else None
            pr_title = f"[releasy] {feat.id}: {feat.description}"
            pr_body = _build_pr_body(feat, n_feat, branch_name, original_body)
            pr_url = create_pull_request(config, feat_pr_branch, branch_name, pr_title, pr_body)

            if pr_url:
                console.print(
                    f"    [green]✓[/green] PR opened: "
                    f"[link={pr_url}]{pr_url}[/link]"
                )
            else:
                console.print(
                    "    [yellow]![/yellow] Branch pushed but PR not created "
                    "(set RELEASY_GITHUB_TOKEN to enable)"
                )
        else:
            console.print("    [dim]Skipping push (push not enabled)[/dim]")

        pr_results.append((feat.id, feat_pr_branch, pr_url))

        stash_and_clean(repo_path)
        run_git(["checkout", branch_name], repo_path)

    console.print(f"\n[bold]Release summary for [cyan]{branch_name}[/cyan][/bold]")
    console.print(f"  Base: {base_tag}")

    if pr_results:
        console.print("  Feature PRs:")
        for feat_id, pr_branch, pr_url in pr_results:
            if pr_url:
                console.print(f"    [green]✓[/green] {feat_id}: {pr_url}")
            else:
                console.print(f"    [yellow]![/yellow] {feat_id}: {pr_branch} (no PR created)")

        has_deps = any(
            f.depends_on
            for fid, _, _ in pr_results
            if (f := config.get_feature(fid))
        )
        if has_deps:
            console.print(
                "\n  [yellow]Note:[/yellow] Some features have dependencies — "
                "merge PRs in the order listed."
            )
    else:
        console.print("  [dim]No feature PRs created[/dim]")

    return True
