# Claude skill: resolve a RelEasy PR-update merge conflict

You are an autonomous agent resolving a `git merge` conflict in
`{repo_slug}`.

The repository at `{cwd}` is already prepared:

- Current branch: `{port_branch}` (behind an open rebase PR).
- A merge of `{base_branch}` into `{port_branch}` is in progress and
  has hit conflict markers.
- Rebase PR being kept current: [{rebase_pr_url}]({rebase_pr_url}).
- Branch originally ports source PR
  [{source_pr_url}]({source_pr_url}) — "{source_pr_title}".
- Original cherry-picked commit SHA: `{source_pr_merge_sha}`.

> **NOTE:** One step of a larger pipeline. Done when the conflict is
> resolved, the build succeeds, and the merge has been committed
> locally. RelEasy pushes / updates the PR. **Do not push, do not run
> `gh pr ...` mutations.**

## Conflicted files

{conflict_files}

## Source PR body (context, may be empty)

{source_pr_body}
{user_context_section}
---

## The single most important rule

A merge here means two legitimate change sets must coexist:

1. **The port's own changes** — what source PR
   `#{source_pr_number}` added, now on `{port_branch}` as "ours".
2. **The base branch's recent changes** — what `{base_branch}`
   ("theirs") accumulated since the last time this rebase PR was
   current.

You must keep **both** sets. The two failure modes: (i) dropping one
(regression / undoing base-branch work), or (ii) **inventing a third
set** (pulling in unrelated code that "looks related"). Don't.

Every kept line on either side of any marker must fit exactly one
bucket:

- **(a) In the source PR's diff.** Bar: ≥99% sure it's there.
- **(b) In `git diff <MB> MERGE_HEAD`** (where `<MB>` is the
  merge-base from Step 1). The base branch's recent contribution.
  Bar: ≥99% sure it's there.
- **(c) Minimal mechanical adaptation bridging (a) and (b)** — the
  source PR called `Foo::serialize(out)`, the base branch changed
  the signature to `Foo::serialize(ctx, out)`, so the merged code
  needs `Foo::serialize(ctx, out)`. Allowed ONLY when you can name
  the specific change in `git diff <MB> MERGE_HEAD` (or
  `git log <MB>..MERGE_HEAD`) that forces it, the adaptation is a
  token-level translation, and it adds no new behaviour / helpers /
  tests.

Anything outside the three buckets is out of scope — that's how past
bad PRs leaked extra `ProfileEvents` / unrelated `SettingsChanges` /
helper methods / integration tests into the merge.

---

## Special case: `src/Core/SettingsChangesHistory.cpp`

When one side adds settings that already exist on the other side as
commented-out lines for the same key:

- Do NOT keep both new uncommented rows and the old `// ...` block.
- DO uncomment the existing `// ...` rows **in place**, align their
  content with the contributing side's intent (same key, same
  semantics), then drop the duplicates from the other side.
- If the commented block is obsolete or wrong for the merged result,
  resolve on substance (update or remove) — don't blindly accept a
  hunk that only adds fresh lines.

---

## Task — execute these steps in order

### Step 1 — Establish ground truth (both diffs)

Compute the previous merge-base:

```bash
git merge-base HEAD MERGE_HEAD
```

Call its SHA `<MB>`. Then read both authoritative diffs:

(a) What the source PR was supposed to add:

```bash
git show -m --first-parent --no-color {source_pr_merge_sha}
gh pr diff {source_pr_url}                    # cross-check
```

(b) What the base branch is bringing in:

```bash
git diff --no-color <MB> MERGE_HEAD
```

Per-file:

```bash
git diff --no-color <MB> MERGE_HEAD -- <file>
git show -m --first-parent --no-color {source_pr_merge_sha} -- <file>
```

Anything outside these two diffs needs an explicit bucket-(c)
justification.

### Step 2 — Inspect what git left behind

```bash
git status
git diff -- <file>            # working tree with markers
git diff --base   -- <file>   # <MB> → working tree
git diff --ours   -- <file>   # ours (port's view)
git diff --theirs -- <file>   # theirs (base branch's view)
```

In a merge:

- **"ours"** = `{port_branch}` (`{base_branch}` at `<MB>` PLUS the
  source PR's port). Anything new here not in the source PR's diff
  is suspect.
- **"theirs"** = `{base_branch}` at its current tip. Anything here
  not in `git diff <MB> MERGE_HEAD` is suspect.

### Step 3 — Resolve each conflict, hunk by hunk

For every `<<<<<<< ... ======= ... >>>>>>>` block:

1. For each line differing from the merge-base on either side:
   - **In (a)** → keep (port's contribution).
   - **In (b)** → keep (base branch's contribution).
   - **In both** → trivial; both sides made the same change.
   - **Bucket (c)** → see point 2, all conditions must hold.
   - **None of the above** → drop. No "looks like it belongs",
     "matches style", "another file does this".
2. **Bucket-(c) adaptation is OK when ALL of:**
   1. minimal mechanical translation needed because (a) and (b)
      collide on a shared symbol (renamed call, added required arg,
      moved import, split struct field, …);
   2. you can point to the specific commit/symbol in
      `git log <MB>..MERGE_HEAD` that forces it;
   3. no new behaviour, logging, error handling, tests, or helpers.
      Need a new helper? → `UNRESOLVED`.

   Mention each bucket-(c) path briefly in your final narration.
3. **Append-only registries** (changelog, `SettingsChangesHistory`,
   `ProfileEvents`, error-code tables): keep exactly the union of
   rows added by (a) and (b). Nothing else. No bucket-(c) carve-out.

### Hard prohibitions

- **No inventions.** No new functions / classes / settings / metrics
  / errors / tests / imports unless they fit (a), (b), or a named
  bucket-(c) adaptation. Registries get NO bucket-(c) carve-out.
- **No reading other refs.** Only `{source_pr_merge_sha}`,
  `{port_branch}`, `{base_branch}`, `MERGE_HEAD`, and `<MB>` matter.
- **No `git add -A`.** Stage only files you touched.
- **No drive-by lint / refactor / typo fixes.**

### Step 4 — Verify scope before committing

Before `git commit --no-edit`:

```bash
git diff -- <file>
```

Classify each `+` line:

- **Bucket (a)?** In `git show -m --first-parent {source_pr_merge_sha}`?
- **Bucket (b)?** In `git diff <MB> MERGE_HEAD`?
- **Bucket (c)?** You can fill in:

  > "Line needed because commit `<sha>` (or symbol `<name>`) on
  > `{base_branch}` `<renamed | moved | re-signatured | split | removed>`
  > `<exact thing>`, colliding with the source PR's use of it.
  > Minimal translation: `<token swap | extra arg | new include path | …>`."

  Vague answers ("API looks different", "matches style", "seems
  consistent") DO NOT count.
- **None → remove the line.** If removing it breaks the build,
  you misidentified (c): name the cause or drop the line.

Can't decide a hunk, or bucket-(c) needs more than a token swap →
`UNRESOLVED`. Clean abort beats over-eager guess.

### Step 5 — Stage and conclude the merge

```bash
git add <file> <file> ...
git commit --no-edit
```

Git has already prepared the merge commit message; just seal it.

### Step 6 — Build

```bash
bash {build_script}
```

Rules:

- Verbatim line. No subshells, no `&&`, no `bash -c`. No `cmake` /
  `ninja` / `make` directly. No output redirection — it already tees
  into `{build_log}`.
- On success: do not read the log.
- On failure: cause is at the END of the log.
  - Start `tail -n 200 {build_log}`; double as needed.
  - `Grep "error:"` / `"^FAILED:"` fallback if tail hasn't surfaced
    the cause within ~2k lines.
  - Never `Read` the whole log (>25k tokens, rejected).
- Same scope rule on fixes: bucket-(a) or (b) or named bucket-(c).
  Don't "fix" the build with code from other PRs.
- Amend: `git add -u && git commit --amend --no-edit`. Rerun the
  build.
- Max **{max_iterations}** build attempts.

### Step 7 — Final clean-tree check

```bash
git status --porcelain
```

Must produce no output. If it does, repeat Step 4 on what's left,
then `git add -u && git commit --amend --no-edit`.

---

## Hard rules (non-negotiable)

- Only `{port_branch}` may be touched. No other branch checkout/push/
  delete.
- Never push, never `gh pr create / edit / merge`. RelEasy pushes.
  Read-only `gh pr diff` etc. is fine.
- Never force-push to `{base_branch}` or any protected branch.
- Never amend commits already on `origin/{base_branch}` or
  `origin/{port_branch}` (only your new merge commit is yours to
  seal/amend).
- Never `git reset --hard` against any remote ref. No
  `git merge --abort`, no `git reset --hard HEAD~` — if you can't
  resolve, exit `UNRESOLVED` and let RelEasy clean up.
- Only read `{build_log}`. Never write logs.
- Only allowed build invocation: `bash {build_script}`. No direct
  `cmake`/`ninja`/`make`.
- No compound Bash (`&&`, `||`, `;`, `(...)`, `bash -c`). One command
  per call.
- After **{max_iterations}** failed builds: `BUILD FAILED` and exit.
- If you cannot resolve a hunk with ≥99% confidence every kept line
  is bucket-(a), (b), or named bucket-(c): `UNRESOLVED`. Don't guess.
- On success, final line must be `DONE`.
