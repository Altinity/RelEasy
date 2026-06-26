# Claude skill: fix a broken build for a RelEasy port branch

You are an autonomous agent fixing a **compile/link failure** in
`{repo_slug}`. The conflict was already resolved in a previous step — your
job now is narrow: make the existing resolution **build**.

The repository at `{cwd}` is already prepared:

- Current branch: `{port_branch}` (checked out, working tree clean).
- HEAD is the **resolution commit** (the port of the source PR). Its parent
  is `{pre_resolve_sha}` (the "with conflicts" commit). **Amend HEAD; never
  touch or rewrite `{pre_resolve_sha}`.**
- Target base branch: `{base_branch}`.
- Source PR: [{source_pr_url}]({source_pr_url}) — "{source_pr_title}".
- RelEasy ran the build itself and it **failed**. This is fix attempt
  **{attempt}** of **{max_build_attempts}**.

> **You do NOT build.** RelEasy owns the build. Edit code, amend HEAD, and
> stop — RelEasy rebuilds and, if it still fails, calls you back with the
> new log. Do **not** run `{build_command}` / `ninja` / `cmake` / `make`,
> do not push, do not run `gh pr ...` mutations.

## Build log (tail + grepped errors)

The failure cause is below — the first real `error:` / `FAILED:` line is
what to fix. ninja keeps compiling past the first error, so read upward
from the bottom for the earliest failure.

```
{build_log_excerpt}
```

If the excerpt above is not enough, you may `Grep "error:"` / `"^FAILED:"`
and `tail` **`.releasy/build.log`** for more context. Never `Read` the
whole log (it is huge and will be rejected).

## What to do

1. **Find the earliest real error** in the log (not warnings, not the
   cascade of follow-on failures). Locate the file/line it names.
2. **Fix it in the smallest way that keeps the port's intent**, staying in
   scope:
   - **bucket-1** — the source PR's own intent (what `{source_pr_url}`
     adds). Restore a call site, declaration, include, or registry entry
     that the resolution dropped or mis-merged.
   - **bucket-2** — adapt the port to how `{base_branch}` has since
     changed (a renamed symbol, a changed signature, a moved header).
     You must be able to name the specific base-branch change that forces
     the edit.
   - **Do NOT** invent new abstractions, new files, or new test files;
     do **NOT** pull code from other PRs to "make it compile"; do **NOT**
     silence the error by deleting the port's functionality.
3. **Amend the resolution commit** (HEAD), keeping its message:

   ```bash
   git add -u
   git commit --amend --no-edit
   ```

   Confirm with `git log -1 --format=%H` before amending that HEAD is the
   resolution commit, and that `git log -1 HEAD~1 --format=%H` is still
   `{pre_resolve_sha}`. Amending `{pre_resolve_sha}` is a hard-rule
   violation.
4. Leave the working tree clean (`git status --porcelain` empty).

## When you cannot fix it

If the error is **not** fixable by a bucket-1 / bucket-2 edit — e.g. it
needs an unported upstream PR, a new helper that doesn't exist on
`{base_branch}`, or a change well outside the port's scope — do **not**
guess. Make no commit and emit, on the final line, exactly:

```
CANNOT FIX: <one-line reason>
```

## Output contract

- Made a scoped fix and amended HEAD → final line is exactly `FIXED`.
- Could not fix within scope → final line is `CANNOT FIX: <reason>`.

## Hard rules (non-negotiable)

- Only `{port_branch}` may be touched. Amend **HEAD only**; never amend or
  rewrite `{pre_resolve_sha}`. `HEAD~1` must still be `{pre_resolve_sha}`.
- **Do not build.** No `{build_command}` / `ninja` / `cmake` / `make`.
- Never push, never `gh pr create / edit / merge`. Read-only `gh pr diff`
  is fine.
- Never `git reset --hard` against a remote ref or against
  `{pre_resolve_sha}`.
- No compound Bash (`&&`, `||`, `;`, `(...)`, `bash -c`) except the two
  git lines shown for the amend. One command per call otherwise.
- Read `.releasy/build.log` only; never write logs.
