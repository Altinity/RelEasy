"""Git operations via subprocess."""

from __future__ import annotations

import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

from releasy.config import Config, get_ssh_key_path
from releasy.termlog import console


@dataclass
class OperationResult:
    success: bool
    conflict_files: list[str]
    error_message: str | None = None
    # Cherry-pick was empty: the patch is already in target.
    already_applied: bool = False


def _git_env() -> dict[str, str]:
    env = os.environ.copy()
    ssh_key = get_ssh_key_path()
    if ssh_key:
        env["GIT_SSH_COMMAND"] = f"ssh -i {ssh_key} -o StrictHostKeyChecking=no"
    return env


def run_git(
    args: list[str],
    work_dir: Path,
    *,
    check: bool = True,
    capture: bool = True,
) -> subprocess.CompletedProcess[str]:
    cmd = ["git"] + args
    return subprocess.run(
        cmd,
        cwd=work_dir,
        env=_git_env(),
        capture_output=capture,
        text=True,
        check=check,
    )


def ensure_work_repo(config: Config, work_dir: Path) -> tuple[Path, bool]:
    """Use ``work_dir`` if it is a repo, else clone into ``work_dir/repo``.

    Returns ``(repo_path, freshly_cloned)``.
    """
    if (work_dir / ".git").exists():
        repo_path = work_dir
    else:
        repo_path = work_dir / "repo"

    freshly_cloned = False
    if not (repo_path / ".git").exists():
        work_dir.mkdir(parents=True, exist_ok=True)
        run_git(["clone", config.origin.remote, "repo"], work_dir)
        freshly_cloned = True
    else:
        result = run_git(
            ["remote", "get-url", config.origin.remote_name], repo_path, check=False,
        )
        if result.returncode != 0:
            run_git(
                ["remote", "add", config.origin.remote_name, config.origin.remote],
                repo_path, check=False,
            )

    return repo_path, freshly_cloned


def update_submodules(repo_path: Path, jobs: int = 8) -> None:
    run_git(
        ["submodule", "update", "--init", "--recursive", "--jobs", str(jobs)],
        repo_path,
    )


def fetch_remote(repo_path: Path, remote_name: str) -> None:
    run_git(["fetch", remote_name], repo_path)


def ensure_remote(repo_path: Path, name: str, url: str) -> bool:
    """Add remote ``name`` or update its URL; return True iff config changed."""
    existing = run_git(
        ["remote", "get-url", name], repo_path, check=False,
    )
    if existing.returncode == 0:
        if existing.stdout.strip() == url:
            return False
        run_git(["remote", "set-url", name, url], repo_path, check=False)
        return True
    run_git(["remote", "add", name, url], repo_path, check=False)
    return True


def is_ancestor(repo_path: Path, ancestor: str, descendant: str) -> bool | None:
    """``git merge-base --is-ancestor``; ``None`` when git errors."""
    result = run_git(
        ["merge-base", "--is-ancestor", ancestor, descendant],
        repo_path,
        check=False,
    )
    if result.returncode == 0:
        return True
    if result.returncode == 1:
        return False
    return None


def stash_and_clean(repo_path: Path) -> None:
    run_git(["checkout", "--force", "HEAD"], repo_path, check=False)
    run_git(["clean", "-fd"], repo_path, check=False)


def _resolved_git_dir(repo_path: Path) -> Path:
    """Actual gitdir (worktree-aware: per-worktree op state lives there, not in ``.git``)."""
    result = run_git(
        ["rev-parse", "--absolute-git-dir"], repo_path, check=False,
    )
    if result.returncode != 0:
        return repo_path / ".git"
    return Path(result.stdout.strip())


def is_operation_in_progress(repo_path: Path) -> bool:
    """Check if a cherry-pick, merge, or rebase is still in progress."""
    git_dir = _resolved_git_dir(repo_path)
    return (
        (git_dir / "CHERRY_PICK_HEAD").exists()
        or (git_dir / "MERGE_HEAD").exists()
        or (git_dir / "rebase-merge").exists()
        or (git_dir / "rebase-apply").exists()
    )


def abort_in_progress_op(repo_path: Path) -> str | None:
    """Abort the in-progress cherry-pick/merge/rebase; return its kind or ``None``."""
    git_dir = _resolved_git_dir(repo_path)
    if (git_dir / "CHERRY_PICK_HEAD").exists():
        run_git(["cherry-pick", "--abort"], repo_path, check=False)
        kind = "cherry-pick"
    elif (git_dir / "MERGE_HEAD").exists():
        run_git(["merge", "--abort"], repo_path, check=False)
        kind = "merge"
    elif (git_dir / "rebase-merge").exists() or (git_dir / "rebase-apply").exists():
        run_git(["rebase", "--abort"], repo_path, check=False)
        kind = "rebase"
    else:
        return None
    run_git(["reset", "--hard", "HEAD"], repo_path, check=False)
    run_git(["clean", "-fd"], repo_path, check=False)
    return kind


def create_branch_from_ref(repo_path: Path, branch_name: str, ref: str) -> None:
    """Create (or recreate) a branch from a given ref and check it out."""
    # Detach HEAD first so we can delete the branch even if we're on it
    run_git(["checkout", "--detach"], repo_path, check=False)
    run_git(["branch", "-D", branch_name], repo_path, check=False)
    run_git(["checkout", "-b", branch_name, ref], repo_path)


def branch_exists(repo_path: Path, branch: str, remote: str | None = None) -> bool:
    """True if the branch exists locally or (when given) on ``remote``."""
    candidates = [f"refs/heads/{branch}"]
    if remote:
        candidates.append(f"refs/remotes/{remote}/{branch}")
    for ref in candidates:
        result = run_git(["rev-parse", "--verify", ref], repo_path, check=False)
        if result.returncode == 0:
            return True
    return False


def local_branch_exists(repo_path: Path, branch: str) -> bool:
    result = run_git(
        ["rev-parse", "--verify", f"refs/heads/{branch}"], repo_path, check=False,
    )
    return result.returncode == 0


def remote_branch_exists(repo_path: Path, branch: str, remote: str) -> bool:
    result = run_git(
        ["rev-parse", "--verify", f"refs/remotes/{remote}/{branch}"],
        repo_path, check=False,
    )
    return result.returncode == 0


def ref_exists_locally(repo_path: Path, ref: str) -> bool:
    """Check if a ref (tag, branch, SHA) is already available locally."""
    result = run_git(["rev-parse", "--verify", ref], repo_path, check=False)
    return result.returncode == 0


def force_push(repo_path: Path, branch: str, config: Config) -> None:
    """Force-push a branch to the origin remote (the only push path for the work repo)."""
    if config.dry_run:
        console.print(
            f"    [magenta]dry-run:[/magenta] would force-push "
            f"[cyan]{branch}[/cyan] to "
            f"[cyan]{config.origin.remote_name}[/cyan]"
        )
        return
    run_git(["push", "--force", config.origin.remote_name, branch], repo_path)


def get_branch_tip(repo_path: Path, ref: str) -> str:
    result = run_git(["rev-parse", ref], repo_path)
    return result.stdout.strip()


def find_merge_base(repo_path: Path, ref_a: str, ref_b: str) -> str | None:
    result = run_git(["merge-base", ref_a, ref_b], repo_path, check=False)
    if result.returncode != 0:
        return None
    return result.stdout.strip()


def count_commits(repo_path: Path, base_ref: str, tip_ref: str) -> int:
    result = run_git(
        ["rev-list", "--count", f"{base_ref}..{tip_ref}"],
        repo_path,
        check=False,
    )
    if result.returncode != 0:
        return 0
    return int(result.stdout.strip())


def get_conflict_files(repo_path: Path) -> list[str]:
    result = run_git(["diff", "--name-only", "--diff-filter=U"], repo_path, check=False)
    if result.returncode == 0 and result.stdout.strip():
        return result.stdout.strip().splitlines()

    result = run_git(["status", "--porcelain"], repo_path, check=False)
    conflicts = []
    for line in result.stdout.splitlines():
        if line[:2] in ("UU", "AA", "DD"):
            conflicts.append(line[3:].strip())
    return conflicts


def fetch_pr_ref(repo_path: Path, remote_or_url: str, pr_number: int) -> bool:
    """Fetch ``refs/pull/<n>/merge`` from a remote name or URL; True on success."""
    result = run_git(
        ["fetch", remote_or_url, f"refs/pull/{pr_number}/merge"],
        repo_path,
        check=False,
    )
    return result.returncode == 0


def resolve_remote_tag(
    repo_path: Path, remote_or_url: str, tag: str,
) -> str | None:
    """Resolve a tag on a remote (or URL) to a commit SHA via ``ls-remote``, or ``None``.

    Prefers the dereferenced ``^{}`` line so annotated tags yield the commit, not the tag object.
    """
    result = run_git(
        ["ls-remote", "--tags", remote_or_url, tag, f"{tag}^{{}}"],
        repo_path,
        check=False,
    )
    if result.returncode != 0 or not result.stdout.strip():
        return None
    sha_for_tag: str | None = None
    sha_dereferenced: str | None = None
    for line in result.stdout.splitlines():
        parts = line.split("\t", 1)
        if len(parts) != 2:
            continue
        sha, ref = parts[0].strip(), parts[1].strip()
        if ref.endswith("^{}"):
            sha_dereferenced = sha
        else:
            sha_for_tag = sha
    return sha_dereferenced or sha_for_tag


def fetch_commit(repo_path: Path, remote_or_url: str, sha: str) -> bool:
    """Fetch a single commit by SHA from a remote name or URL."""
    result = run_git(
        ["fetch", remote_or_url, sha], repo_path, check=False,
    )
    return result.returncode == 0


def cherry_pick_sha(
    repo_path: Path,
    commit: str,
    *,
    mainline: int | None = None,
    abort_on_conflict: bool = True,
) -> OperationResult:
    """Cherry-pick a commit onto HEAD.

    ``mainline`` is the merge parent for merge commits; ``None`` for non-merge
    commits (git rejects ``-m`` there). With ``abort_on_conflict=False`` the
    conflicted tree is left for the caller to resolve.
    """
    argv = ["cherry-pick"]
    if mainline is not None:
        argv += ["-m", str(mainline)]
    argv += ["--no-edit", commit]
    result = run_git(argv, repo_path, check=False)
    if result.returncode == 0:
        return OperationResult(success=True, conflict_files=[])

    conflict_files = get_conflict_files(repo_path)

    # "previous cherry-pick is now empty": patch already present in target.
    stderr = result.stderr or ""
    if not conflict_files and "is now empty" in stderr:
        run_git(["cherry-pick", "--skip"], repo_path, check=False)
        return OperationResult(
            success=False,
            conflict_files=[],
            already_applied=True,
            error_message=stderr.strip(),
        )

    if abort_on_conflict:
        run_git(["cherry-pick", "--abort"], repo_path, check=False)
    return OperationResult(
        success=False,
        conflict_files=conflict_files,
        error_message=result.stderr.strip() if result.stderr else None,
    )


def cherry_pick_merge_commit(
    repo_path: Path, commit: str, *, abort_on_conflict: bool = True,
) -> OperationResult:
    """Cherry-pick a merge commit using its first-parent diff."""
    return cherry_pick_sha(
        repo_path, commit, mainline=1, abort_on_conflict=abort_on_conflict,
    )


def append_commit_trailer(repo_path: Path, key: str, value: str) -> bool:
    """Append a ``Key: value`` trailer to HEAD's commit message (Git 2.32+)."""
    result = run_git(
        ["commit", "--amend", "--no-edit", "--trailer", f"{key}: {value}"],
        repo_path,
        check=False,
    )
    return result.returncode == 0


def _stage_unmerged_paths(repo_path: Path) -> None:
    """Stage only the unmerged paths of an in-progress cherry-pick.

    Never ``git add -A``: work trees hold untracked build/runtime scratch.
    """
    result = run_git(
        ["diff", "--name-only", "--diff-filter=U"],
        repo_path, check=False,
    )
    if result.returncode != 0:
        return
    paths = [p for p in result.stdout.splitlines() if p.strip()]
    if not paths:
        return
    run_git(["add", "--"] + paths, repo_path, check=False)


def commit_cherry_pick_conflict_as_is(
    repo_path: Path, source_pr_url: str | None = None,
) -> tuple[bool, str | None]:
    """Commit an in-progress cherry-pick with its markers; return ``(success, head_sha)``."""
    _stage_unmerged_paths(repo_path)

    msg_file = _resolved_git_dir(repo_path) / "MERGE_MSG"
    original_msg = ""
    if msg_file.exists():
        try:
            original_msg = msg_file.read_text(encoding="utf-8").strip()
        except OSError:
            original_msg = ""

    header = "Cherry-pick with unresolved conflict markers (resolution in next commit)"
    if source_pr_url:
        header = (
            f"Cherry-pick of {source_pr_url} with unresolved conflict markers "
            "(resolution in next commit)"
        )

    if original_msg:
        full_msg = f"{header}\n\n---\nOriginal cherry-pick message follows:\n\n{original_msg}"
    else:
        full_msg = header

    result = run_git(
        ["commit", "--no-edit", "-m", full_msg],
        repo_path,
        check=False,
    )
    if result.returncode != 0:
        return False, None

    head = run_git(["rev-parse", "--verify", "HEAD"], repo_path, check=False)
    if head.returncode != 0:
        return True, None
    return True, head.stdout.strip() or None


def squash_commits(repo_path: Path, base_ref: str, message: str) -> OperationResult:
    mb = find_merge_base(repo_path, "HEAD", base_ref)
    if mb is None:
        return OperationResult(
            success=False, conflict_files=[],
            error_message=f"Could not find merge-base between HEAD and {base_ref}",
        )

    if count_commits(repo_path, mb, "HEAD") == 0:
        return OperationResult(success=True, conflict_files=[])

    run_git(["reset", "--soft", mb], repo_path)
    result = run_git(["commit", "-m", message], repo_path, check=False)

    if result.returncode != 0:
        return OperationResult(
            success=False, conflict_files=[],
            error_message=result.stderr.strip() if result.stderr else None,
        )
    return OperationResult(success=True, conflict_files=[])


def resolve_ref(repo_path: Path, ref: str) -> str | None:
    """Resolve a ref or tag name to a full SHA, or ``None``."""
    for candidate in [ref, f"refs/tags/{ref}"]:
        result = run_git(["rev-parse", candidate], repo_path, check=False)
        if result.returncode == 0:
            return result.stdout.strip()
    return None


def resolve_ref_prefer_remote(
    repo_path: Path, ref: str, remote_name: str = "origin",
) -> tuple[str, str] | None:
    """Resolve ``ref`` trying remote-tracking, then tag, then ``ref`` itself.

    Returns ``(sha, kind)``; kind is ``remote``, ``tag``, ``local-branch`` or ``other``.
    """
    for candidate, kind in (
        (f"refs/remotes/{remote_name}/{ref}", "remote"),
        (f"refs/tags/{ref}", "tag"),
        (ref, "other"),
    ):
        result = run_git(
            ["rev-parse", "--verify", "--quiet", candidate],
            repo_path, check=False,
        )
        sha = (result.stdout or "").strip()
        if result.returncode != 0 or not sha:
            continue
        if kind == "other":
            full = run_git(
                ["rev-parse", "--symbolic-full-name", ref],
                repo_path, check=False,
            )
            name = (full.stdout or "").strip()
            if name.startswith("refs/remotes/"):
                kind = "remote"
            elif name.startswith("refs/heads/"):
                kind = "local-branch"
        return sha, kind
    return None


def is_tag_ref(repo_path: Path, ref: str) -> bool:
    result = run_git(
        ["rev-parse", "--verify", "--quiet", f"refs/tags/{ref}"],
        repo_path, check=False,
    )
    return result.returncode == 0


def commit_date(repo_path: Path, ref: str) -> str | None:
    """Committer date of ``ref`` as strict ISO 8601, or ``None``."""
    result = run_git(
        ["show", "-s", "--format=%cI", ref], repo_path, check=False,
    )
    if result.returncode != 0:
        return None
    lines = [ln for ln in (result.stdout or "").splitlines() if ln.strip()]
    return lines[0].strip() if lines else None


# GitHub-generated mainline commit subjects: a merge-commit
# ("Merge pull request #N from …") or a squash-merge ("<title> (#N)").
_MERGE_PR_RE = re.compile(r"^Merge pull request #(\d+)\b")
_SQUASH_PR_RE = re.compile(r"\(#(\d+)\)\s*$")


def pr_number_from_subject(subject: str) -> int | None:
    """Origin PR number from a mainline commit subject, or ``None``."""
    m = _MERGE_PR_RE.match(subject) or _SQUASH_PR_RE.search(subject)
    return int(m.group(1)) if m else None


def first_parent_pr_numbers(
    repo_path: Path, from_ref: str, to_ref: str,
) -> list[int]:
    """Sorted unique PR numbers on the first-parent chain of ``from_ref..to_ref``."""
    result = run_git(
        ["log", "--first-parent", "--format=%s", f"{from_ref}..{to_ref}"],
        repo_path, check=False,
    )
    if result.returncode != 0:
        return []
    seen: set[int] = set()
    for line in (result.stdout or "").splitlines():
        n = pr_number_from_subject(line)
        if n is not None:
            seen.add(n)
    return sorted(seen)
