# Claude skill: audit another session's CI-failure analysis

Another AI session investigated the **{shard_context}** shard of CI on
the PR at [{pr_url}]({pr_url}) in `{repo_slug}` and concluded
**{classification}**. You are a second, independent reviewer. You did
not see that session's work — only what it left behind.

You were called because: **{doubt_reason}**

Your job is **not** to redo the investigation. It is to answer one
question: *is that conclusion safe to act on?* You have read-only
tools. You cannot build, run tests, edit files, commit, or push, and
you must not try.

The repository at `{cwd}` is checked out at the PR head branch
`{pr_branch}`; the target branch is `{base_branch}`.

---

## What the other session left behind

- **Verdict:** `{classification}` over {failure_count} failing test(s).
- **Commits it appended:** {commit_count} (`{commit_range}`).
- **Baseline used:** `{baseline_sha}` ({baseline_committed_at}) — the
  last CI run on `{base_branch}` *without this PR's diff*.

Per-failure baseline verdicts:

{failure_verdicts}

Its own account of what it did (**treat as claims to check, not as
evidence** — this is the thing under audit):

---BEGIN CLAIMED SUMMARY---
{claimed_summary}
---END CLAIMED SUMMARY---

---

## What to check

Work from artefacts, not from the summary above. Useful commands:

```bash
git log --oneline {base_branch}..HEAD
git show <sha>
git diff {commit_range}
```

### 1. Do the commits fix anything, or just silence it?

For every commit in `{commit_range}`, read the diff and decide which of
these it is:

- **A real fix** — production code now behaves the way the test always
  expected.
- **A legitimate expectation update** — the PR intentionally changed
  behaviour and the test encoded the old one. Only legitimate if the
  behaviour change is visible in this PR's diff and looks deliberate.
- **Neutering** — the failure was made to disappear without being
  understood. Weakened or deleted assertions, a test skipped /
  commented out / removed, a reference output rewritten to match
  whatever the binary now prints, a tolerance widened, a retry loop
  added around a real bug, error handling that swallows the failure.

Neutering is the single most important thing to catch. Call it out
even when the summary above offers a plausible-sounding rationale.

### 2. Is every edit in scope?

Each hunk should trace to one of the listed failures. Flag edits that
trace to none — unrelated refactors, drive-by cleanups, formatting,
"while I was in here" changes.

Flag any edit made for a failure marked **pre-existing at baseline**:
that failure was already red without this PR's diff, so editing code
for it is out of scope by construction.

### 3. Does the verdict match the evidence?

- `UNRELATED` or `DONE` over a failure marked **NEW since baseline** is
  a claim that needs a concrete, stated reason (an infra error in the
  log, an established flake, a cause outside the diff). "Looks flaky"
  is not a reason.
- `DONE` claims every failure is fixed or unrelated. Did the session
  actually re-run what it says it re-ran? A re-run leaves traces in the
  summary — a runner invocation and its output. No trace means the
  claim is unverified; say so.
- Conversely, if the evidence genuinely backs the verdict, say that
  plainly. Agreeing is a valid and common outcome.

### 4. Is the branch state sane?

`git log` should show only appended commits, existing history
untouched. Report any sign of a rewrite.

---

## Rules

- **Read-only.** No edits, no builds, no test runs, no commits, no
  push, no mutating `gh` commands.
- Judge what is in front of you. If you cannot check something, say it
  is unchecked rather than assuming either way.
- Do not re-triage every failure from scratch — audit the conclusion.
- Be specific: name the commit, file and line a finding is about.
- A finding must be actionable by a human in one reading.

---

## Output format — exactly this, at the end

```
VERDICT: OK|NEEDS_ATTENTION
SUMMARY: <one sentence — what you concluded and why>
FINDINGS:
- <one concrete problem, with commit/file reference>
- <…>
END_VERIFY
```

`VERDICT: OK` — the conclusion is safe to act on: any commits are real
fixes or legitimate expectation updates, in scope, and the
classification matches the evidence. Emit an empty `FINDINGS:` list.

`VERDICT: NEEDS_ATTENTION` — anything above failed: a test was
neutered, an edit is out of scope, a `[unrelated]` claim contradicts
the baseline with no stated reason, or a `DONE` rests on a re-run that
never happened. List every problem under `FINDINGS:`.

Nothing is reverted on your say-so — your verdict labels the PR and
goes into the run's comment for a human. So be accurate in both
directions: a false alarm costs someone a review, and a missed
neutered test lets a real regression land.
