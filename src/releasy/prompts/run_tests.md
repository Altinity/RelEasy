# Claude skill: run a ported PR's own tests

You are an autonomous agent verifying that a ported PR's **own tests** pass
in `{repo_slug}`. The conflict is resolved and the code **builds** — your
job is to run the tests the source PR added or changed, and fix them if
they fail because of the port.

The repository at `{cwd}` is already prepared:

- Current branch: `{port_branch}` (checked out, working tree clean).
- HEAD is the **resolution commit**; its parent is `{pre_resolve_sha}`.
  **Amend HEAD only; never touch `{pre_resolve_sha}`.**
- Target base branch: `{base_branch}`.
- Source PR: [{source_pr_url}]({source_pr_url}) — "{source_pr_title}".
- A built `clickhouse` binary exists under `build/` (RelEasy just built it).

> If you change any code or test fixture and amend HEAD, RelEasy will
> **rebuild and re-run** the tests itself afterwards — so a fix that might
> affect the build is safe. Do not push, do not run `gh pr ...` mutations.

## Tests to run

These are the test files the source PR added or modified. Run **only**
these (plus a couple of obviously-related neighbours if a failure is
ambiguous) — not the whole suite:

{test_files}

## How to run them

{runner_hints}

Run from the repo root. Use the existing built binary under `build/`; do
**not** rebuild. If a runner needs a scratch dir it manages itself (e.g.
`ci/tmp`), that is fine.

## What to do

1. Run the listed tests.
2. **All pass** → you are done. Final line exactly `TESTS PASSED`.
3. **A test fails because of the port** (the resolution dropped/changed
   behaviour the PR intended, or a `.reference` needs the PR's new output):
   - Fix it in scope — **bucket-1** (the PR's own intent) or **bucket-2**
     (adapt to a `{base_branch}` change you can name). Updating a
     `.reference` file to match the PR's intended output is in scope; do
     **not** loosen a test to hide a real regression, and do **not** pull
     code from other PRs.
   - `git add -u && git commit --amend --no-edit` (HEAD must stay the
     resolution commit; `HEAD~1` must stay `{pre_resolve_sha}`).
   - You may re-run the tests to confirm. RelEasy will rebuild + re-run
     regardless once you exit, so stopping after the amend is also fine.
4. **A failure is pre-existing, flaky, or infra (not caused by the port)**
   — do not chase it. Note it and treat the port's own tests as the bar.

## Output contract

- The port's tests pass (after any in-scope fix you committed) → final line
  exactly `TESTS PASSED`.
- A genuine, port-caused failure you cannot fix within scope → final line
  `TESTS FAILED: <one-line reason>`.

## Hard rules (non-negotiable)

- Only `{port_branch}` may be touched. Amend **HEAD only**; `HEAD~1` must
  stay `{pre_resolve_sha}`.
- **Do not rebuild** (`{build_command}` / `ninja` / `cmake` / `make`) —
  RelEasy rebuilds when needed.
- Never push, never `gh pr create / edit / merge`. Read-only `gh pr diff`
  is fine.
- Never `git reset --hard` against a remote ref or `{pre_resolve_sha}`.
- Do not add new top-level abstractions, new source files, or brand-new
  test files the PR didn't introduce.
