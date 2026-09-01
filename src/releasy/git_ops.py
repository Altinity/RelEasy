"""Git operations via subprocess.

Uses subprocess for full control over cherry-pick/conflict detection.
"""

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
    # True when ``cherry_pick_sha`` recognised git's "previous cherry-pick
    # is now empty" outcome — the patch is already in target via some
    # other path (sibling backport, prior port, squash, etc.). The
    # caller should treat the PR as a no-op rather than a conflict.
    already_applied: bool = False


def _git_env() -> dict[str, str]:
    """Build env dict with SSH key configuration if set."""
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
    """Run a git command in the given working directory."""
    cmd = ["git"] + args
    return subprocess.run(
        cmd,
        cwd=work_dir,
        env=_git_env(),
        capture_output=capture,
        text=True,
        check=check,
    )


# ---------------------------------------------------------------------------
# Repository setup
# ---------------------------------------------------------------------------


def ensure_work_repo(config: Config, work_dir: Path) -> tuple[Path, bool]:
    """Ensure a local clone of the origin repo exists and has the right remote.

    If work_dir itself is a git repo, it is used directly.
    Otherwise, a clone is created at work_dir/repo.

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
    """Initialise and update all submodules recursively."""
    run_git(
        ["submodule", "update", "--init", "--recursive", "--jobs", str(jobs)],
        repo_path,
    )


# ---------------------------------------------------------------------------
# Fetch / checkout
# ---------------------------------------------------------------------------


def fetch_remote(repo_path: Path, remote_name: str) -> None:
    run_git(["fetch", remote_name], repo_path)


def fetch_all(config: Config, repo_path: Path) -> None:
    fetch_remote(repo_path, config.origin.remote_name)


def ensure_remote(repo_path: Path, name: str, url: str) -> bool:
    """Register a remote alias if missing; update the URL if it drifted.

    Idempotent: a no-op when ``name`` already points at ``url``. Returns
    True iff the local config was changed (added or updated). Used by the
    AI conflict resolver to make the configured ``upstream`` remote
    available for ``git fetch <upstream> <branch>`` and ``git log -S``
    queries during prereq detection.
    """
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
    """Return True iff ``ancestor`` is an ancestor of ``descendant``.

    Wraps ``git merge-base --is-ancestor`` (exit 0 = yes, 1 = no, anything
    else = error). Returns ``None`` on error so callers can skip the
    check rather than treat "git doesn't know" as a definitive answer.
    """
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
    """Ensure the working tree is clean."""
    run_git(["checkout", "--force", "HEAD"], repo_path, check=False)
    run_git(["clean", "-fd"], repo_path, check=False)


def _resolved_git_dir(repo_path: Path) -> Path:
    """Resolve the actual gitdir for ``repo_path``.

    For a regular checkout this is ``<repo>/.git`` (a directory). For a
    *git worktree* it's ``<main-repo>/.git/worktrees/<id>/`` — and that
    matters: per-worktree state files (``CHERRY_PICK_HEAD``,
    ``MERGE_HEAD``, ``rebase-*``) live there, NOT in the main gitdir.

    The previous naive ``repo_path / ".git"`` check returned False in a
    worktree even when a cherry-pick was actually in progress, because
    ``.git`` is a *file* in worktrees that points at the real gitdir.
    """
    result = run_git(
        ["rev-parse", "--absolute-git-dir"], repo_path, check=False,
    )
    if result.returncode != 0:
        # Fallback to the naive path if rev-parse somehow fails — better
        # to keep the old behaviour than crash on unrelated callers.
        return repo_path / ".git"
    return Path(result.stdout.strip())


def is_operation_in_progress(repo_path: Path) -> bool:
    """Check if a cherry-pick, merge, or rebase is still in progress.

    Worktree-aware: resolves the per-worktree gitdir (see
    :func:`_resolved_git_dir`) so checks fire correctly inside
    ``git worktree add``-created worktrees too.
    """
    git_dir = _resolved_git_dir(repo_path)
    return (
        (git_dir / "CHERRY_PICK_HEAD").exists()
        or (git_dir / "MERGE_HEAD").exists()
        or (git_dir / "rebase-merge").exists()
        or (git_dir / "rebase-apply").exists()
    )


def abort_in_progress_op(repo_path: Path) -> str | None:
    """Abort whichever git operation is currently in progress, if any.

    Returns the operation kind (``"cherry-pick"`` / ``"merge"`` /
    ``"rebase"``) when something was aborted, or ``None`` if the working
    tree was already clean. Worktree-aware via :func:`_resolved_git_dir`.
    """
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


# ---------------------------------------------------------------------------
# Branch operations
# ---------------------------------------------------------------------------


def create_branch_from_ref(repo_path: Path, branch_name: str, ref: str) -> None:
    """Create (or recreate) a branch from a given ref and check it out."""
    # Detach HEAD first so we can delete the branch even if we're on it
    run_git(["checkout", "--detach"], repo_path, check=False)
    run_git(["branch", "-D", branch_name], repo_path, check=False)
    run_git(["checkout", "-b", branch_name, ref], repo_path)


def branch_exists(repo_path: Path, branch: str, remote: str | None = None) -> bool:
    """Check if a branch exists locally or on a remote.

    If remote is given, checks refs/remotes/<remote>/<branch>.
    Also checks the local branch. Returns True if either exists.
    """
    candidates = [f"refs/heads/{branch}"]
    if remote:
        candidates.append(f"refs/remotes/{remote}/{branch}")
    for ref in candidates:
        result = run_git(["rev-parse", "--verify", ref], repo_path, check=False)
        if result.returncode == 0:
            return True
    return False


def local_branch_exists(repo_path: Path, branch: str) -> bool:
    """Check if the branch exists locally (refs/heads/<branch>)."""
    result = run_git(
        ["rev-parse", "--verify", f"refs/heads/{branch}"], repo_path, check=False,
    )
    return result.returncode == 0


def remote_branch_exists(repo_path: Path, branch: str, remote: str) -> bool:
    """Check if the branch exists on the given remote (refs/remotes/<remote>/<branch>)."""
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
    """Force-push a branch **to the origin remote**.

    By construction this is the *only* push path RelEasy uses for the
    work repo; there is no parameter to point it at a different remote.
    Any future code that wants to push elsewhere has to be added
    explicitly — it can't happen by accident through this helper.
    """
    if config.dry_run:
        console.print(
            f"    [magenta]dry-run:[/magenta] would force-push "
            f"[cyan]{branch}[/cyan] to "
            f"[cyan]{config.origin.remote_name}[/cyan]"
        )
        return
    run_git(["push", "--force", config.origin.remote_name, branch], repo_path)


def get_branch_tip(repo_path: Path, ref: str) -> str:
    """Get the SHA of a ref (branch, tag, HEAD, etc.)."""
    result = run_git(["rev-parse", ref], repo_path)
    return result.stdout.strip()


def get_short_sha(repo_path: Path, ref: str) -> str:
    """Get the short (8-char) SHA of a ref."""
    result = run_git(["rev-parse", "--short=8", ref], repo_path)
    return result.stdout.strip()


# ---------------------------------------------------------------------------
# Merge-base & commit range
# ---------------------------------------------------------------------------


def find_merge_base(repo_path: Path, ref_a: str, ref_b: str) -> str | None:
    """Find the best common ancestor of two refs."""
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


# ---------------------------------------------------------------------------
# Cherry-pick
# ---------------------------------------------------------------------------


def get_conflict_files(repo_path: Path) -> list[str]:
    """Extract conflicting file paths from a failed operation."""
    result = run_git(["diff", "--name-only", "--diff-filter=U"], repo_path, check=False)
    if result.returncode == 0 and result.stdout.strip():
        return result.stdout.strip().splitlines()

    result = run_git(["status", "--porcelain"], repo_path, check=False)
    conflicts = []
    for line in result.stdout.splitlines():
        if line[:2] in ("UU", "AA", "DD"):
            conflicts.append(line[3:].strip())
    return conflicts


# ---------------------------------------------------------------------------
# PR merge-commit cherry-pick
# ---------------------------------------------------------------------------


def fetch_pr_ref(repo_path: Path, remote_or_url: str, pr_number: int) -> bool:
    """Fetch a PR's merge ref from GitHub (needed for open PRs).

    ``remote_or_url`` can be either a configured remote name or a full
    git URL (e.g. ``https://github.com/owner/repo.git``) — the latter
    is used for cross-repo PR sources.

    Returns True if the merge ref was fetched successfully.
    """
    result = run_git(
        ["fetch", remote_or_url, f"refs/pull/{pr_number}/merge"],
        repo_path,
        check=False,
    )
    return result.returncode == 0


def resolve_remote_tag(
    repo_path: Path, remote_or_url: str, tag: str,
) -> str | None:
    """Resolve a tag name on a remote (or full URL) to a commit SHA.

    Uses ``git ls-remote --tags <remote_or_url> <tag>``. For annotated
    tags this returns the tag-object SHA on the bare line and the
    commit SHA on the dereferenced (``^{}``) line; we prefer the
    dereferenced commit when present so the caller always cherry-picks
    a real commit, never a tag object.

    Returns the commit SHA, or ``None`` if the tag couldn't be found.
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
    """Fetch a single commit by SHA from a remote name or URL.

    Useful when cherry-picking a merged PR from a foreign repo whose
    commit isn't yet in the local clone.
    """
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
    """Cherry-pick a single commit (merge or non-merge) onto HEAD.

    ``mainline`` selects the merge parent for merge commits (1 = the
    "into" side, which is what GitHub's ``Merge pull request`` button
    produces, so cherry-picking a PR uses ``mainline=1``). Pass
    ``None`` for a non-merge commit; ``git cherry-pick`` rejects ``-m``
    on non-merge commits, so we omit the flag entirely.

    When ``abort_on_conflict`` is False the working tree is left in the
    conflicted state so the caller can drive resolution (Claude, manual
    fixup, or commit the markers as WIP).
    """
    argv = ["cherry-pick"]
    if mainline is not None:
        argv += ["-m", str(mainline)]
    argv += ["--no-edit", commit]
    result = run_git(argv, repo_path, check=False)
    if result.returncode == 0:
        return OperationResult(success=True, conflict_files=[])

    conflict_files = get_conflict_files(repo_path)

    # "previous cherry-pick is now empty" — git's signal that the patch
    # is already present in target. No conflict files, just a non-zero
    # exit and that specific stderr fingerprint. Recognise it here so
    # the pipeline can treat the PR as already-applied instead of
    # invoking the AI resolver on a phantom conflict.
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
    """Cherry-pick a merge commit using its first-parent diff.

    Thin wrapper around :func:`cherry_pick_sha` with ``mainline=1``,
    kept as the canonical entry-point for the PR-port flow (which only
    ever cherry-picks GitHub merge commits).
    """
    return cherry_pick_sha(
        repo_path, commit, mainline=1, abort_on_conflict=abort_on_conflict,
    )


def append_commit_trailer(repo_path: Path, key: str, value: str) -> bool:
    """Append a ``Key: value`` trailer to HEAD's commit message.

    Uses ``git commit --amend --no-edit --trailer`` (Git 2.32+). Returns
    True on success.
    """
    result = run_git(
        ["commit", "--amend", "--no-edit", "--trailer", f"{key}: {value}"],
        repo_path,
        check=False,
    )
    return result.returncode == 0


def _stage_unmerged_paths(repo_path: Path) -> None:
    """Stage exactly the conflict-marked files from an in-progress cherry-pick.

    Never use ``git add --all`` / ``git add -A`` here: working trees of
    real C++ repos accumulate scratch outside ``.gitignore`` (ClickHouse
    leaves server runtime data under ``tmp/server_data*``, build
    pipelines spit out generated headers, \u2026). Sweeping everything into
    the cherry-pick commit produces multi-hundred-thousand-LOC PRs.
    Bug seen 2026-05-19 on Altinity/ClickHouse#1812: 696 K additions
    across 19 K files because tmp/ was tracked-and-modified at the time.

    The clean parts of the cherry-pick are already staged by git
    automatically \u2014 we only need to stage the unmerged paths whose
    textual content is the conflict markers themselves.
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
    """Conclude an in-progress cherry-pick with conflict markers as-is.

    Stages exactly the unmerged paths (their textual content is the
    conflict markers verbatim) and creates a commit. The original
    cherry-pick commit message that git prepared in ``.git/MERGE_MSG``
    is preserved as the body, with a header line that flags the commit
    as carrying unresolved markers \u2014 so a human reading the port
    branch's ``git log`` can immediately tell that the next commit is
    the conflict resolution.

    Returns ``(success, head_sha_or_None)``. On success ``head_sha`` is
    the SHA of the new commit (the one with the markers).
    """
    _stage_unmerged_paths(repo_path)

    msg_file = repo_path / ".git" / "MERGE_MSG"
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


# ---------------------------------------------------------------------------
# Squash
# ---------------------------------------------------------------------------


def squash_commits(repo_path: Path, base_ref: str, message: str) -> OperationResult:
    """Squash all commits since base_ref into a single commit."""
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


# ---------------------------------------------------------------------------
# Ref resolution helpers
# ---------------------------------------------------------------------------


def resolve_ref(repo_path: Path, ref: str) -> str | None:
    """Try to resolve a ref (tag, branch, sha). Returns full SHA or None."""
    for candidate in [ref, f"refs/tags/{ref}"]:
        result = run_git(["rev-parse", candidate], repo_path, check=False)
        if result.returncode == 0:
            return result.stdout.strip()
    return None


def resolve_ref_prefer_remote(
    repo_path: Path, ref: str, remote_name: str = "origin",
) -> tuple[str, str] | None:
    """Resolve ``ref`` the remote's way first. Returns ``(sha, kind)``.

    Order: ``refs/remotes/<remote>/<ref>``, ``refs/tags/<ref>``, then ``ref``
    itself — so a bare branch name can't silently resolve to a stale local
    branch when the remote-tracking ref is what the caller meant.

    ``kind`` is ``"remote"``, ``"tag"``, ``"local-branch"`` (resolved via
    ``refs/heads``) or ``"other"`` (SHA, qualified ref, …).
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
    """True if ``ref`` resolves to an actual git tag in the repo."""
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
    """Origin PR number from a mainline commit subject, or ``None``.

    Matches GitHub's ``Merge pull request #N from …`` merge commits and the
    trailing ``(#N)`` of squash-merges. Subjects with no such reference (plain
    ``Merge branch …``, direct pushes) return ``None``.
    """
    m = _MERGE_PR_RE.match(subject) or _SQUASH_PR_RE.search(subject)
    return int(m.group(1)) if m else None


def first_parent_pr_numbers(
    repo_path: Path, from_ref: str, to_ref: str,
) -> list[int]:
    """Origin PR numbers on the first-parent chain of ``from_ref..to_ref``,
    ascending and de-duplicated.

    First-parent so a PR's own merged-in history isn't counted as a top-level
    PR. Rebase-merged PRs carry no subject reference and aren't detected.
    """
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
