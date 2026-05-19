# Claude skill: verify an AI-resolved RelEasy cherry-pick

You are an independent reviewer auditing an automated conflict
resolution in `{repo_slug}`. A previous Claude invocation resolved the
conflict and committed the result; your job is **only to check the
diff** that landed and decide whether it's a faithful port (within the
latitude mechanical adaptation legitimately requires) or whether the
resolver introduced something it shouldn't have.

**Read-only.** No edits, no commits, no push, no rebase, no reset, no
`gh pr edit`. `git diff` / `git show` / `git log` / `git grep` /
`gh pr diff` / `gh pr view` are fine. Don't try to "fix" — only report.

## Context

- Repo at `{cwd}`, branch `{port_branch}`. Base: `{base_branch}`.
- Source PR: [{source_pr_url}]({source_pr_url}) — "{source_pr_title}".
- Cherry-picked commit SHA: `{source_pr_merge_sha}` (merge commit;
  `git cherry-pick -m 1` replayed its first-parent diff).
- Branch tip BEFORE the cherry-pick: `{start_sha}`.
- Branch tip AFTER the resolution: `{new_head}`.
- Everything in `{start_sha}..{new_head}` is the AI's work — that's
  what you audit. The range may be one commit (legacy mode) or two
  commits (split-commit mode: "with conflicts" commit + resolution on
  top). What matters is the net diff:

  ```bash
  git diff {start_sha}..{new_head}
  ```

- Files reported as conflicted before resolution:

{conflict_files}

{user_context_section}

---

## The single most important rule (same as the resolver was told)

**The source PR's diff is the only authoritative list of what this port
*wants* to add or change.** Every `+`/`-` line in
`git diff {start_sha}..{new_head}` must fit one of:

1. **In the source PR's diff** — faithful port. Verify with `git show`
   or `gh pr diff`.
2. **Minimal mechanical adaptation forced by `{base_branch}`** — the
   source PR called a function / used a type / imported a header that
   `{base_branch}` has since renamed/moved/split/re-signatured;
   acceptable only when you can name the specific base-branch change.

Anything outside the two buckets is out of scope — flag it.

---

## Port direction: `{port_direction}`

Source PR labels: `{source_pr_labels}`

The resolver was run in `{port_direction}` mode, which changes what's
allowed:

- **`backport`** — bucket-0 (drop) was available. The resolver may
  have removed isolated optional surfaces from the source PR rather
  than pulling in a missing-prereq PR. Drops are recorded as
  `Dropped:` trailers in the resolution commit(s). **You must audit
  whether the drops were justified** (see Step 5 below).
- **`forward_port`** — bucket-0 was DISABLED. The resolver was not
  allowed to drop source-PR functionality; any `-` line in the net
  diff that removes source-PR-intended code is **itself a finding**.

---

## Step 1 — Establish ground truth

```bash
git log --oneline {start_sha}..{new_head}       # one or two commits
git diff {start_sha}..{new_head}                # net result
git show -m --first-parent --no-color {source_pr_merge_sha}
gh pr diff {source_pr_url}                      # cross-check
```

Local cherry-pick diff is authoritative; `gh pr diff` is cross-check.
Material divergence between the two is itself worth a note.

## Step 2 — Walk the net diff hunk by hunk

For each `+` line in `git diff {start_sha}..{new_head}`:

- **Bucket 1?** Same line (modulo formatting) in the source PR's diff.
  Faithful. Move on.
- **Bucket 2?** You can fill in:
  > *"Source PR adds `X`; `{base_branch}` has
  > `<renamed | moved | split | re-signatured | type-wrapped | parallel-module>`
  > `<name>`, so it became `<resolver's version>`."*

  In `forward_port` mode the translation must be minimal — no new
  helpers / branches / error handling / logging. In `backport` mode
  the resolver has **adaptation latitude**: type-wrapper unboxing,
  parallel-module routing, small adapter shims (~10–30 LOC), and
  light local base-branch refactor (≤ ~20 LOC) are in scope as long
  as each adaptation is named (`Adapted:` trailer or narration) and
  the total adaptation across the port is ≲ 50 LOC. Move on.
- **Neither → finding.**

Suspicious patterns (historically bad — flag them):

- New function / method / class / template not in the source PR diff
  AND not a named bucket-2 adapter shim in backport mode.
- New log lines / metrics / `ProfileEvents` / error codes the source
  PR didn't add.
- Rows in append-only registries (`SettingsChangesHistory`,
  `ProfileEvents`, changelog tables) for keys the source PR didn't
  touch.
- New comments / TODOs / "fix later" notes the source PR didn't
  write (a single one-line `// adapter for #NNN` next to a shim is
  fine in backport mode).
- Imports added that aren't needed by any kept bucket-1/2 line.
- Removals from `{base_branch}` code that the source PR didn't remove
  AND aren't a named bucket-2 light-refactor.
- New tests / fixtures / test files the source PR didn't add.
- Adaptation cost (count `+` lines that are NOT bucket-1) clearly
  exceeding ~50 LOC — at that scale the resolver is reinventing the
  prereq instead of adapting; should have reported `MISSING_PREREQS`.

Normal — do NOT flag:

- Bucket-1 lines at a slightly different position (surrounding code
  drifted on `{base_branch}`).
- Bucket-2 token swaps (rename / signature / type / header path)
  with a named cause.
- Hunks where the source PR adds N lines and the port adds N lines,
  just relocated as context shifted.
- **Backport mode only:** named adapter shims, type-wrapper
  unboxing at call sites, parallel-module routing, and light
  base-branch refactor — provided each has either an `Adapted:`
  trailer or a one-line narration mention.
- The intermediate "with conflicts" commit in split-commit mode —
  the net diff already cancels its markers out.

## Step 3 — Audit prerequisite-handling judgment

Apparent prereq-equivalent code in the port is necessarily a bucket-2
claim the resolver made (a real missing prereq would have failed, not
produced this diff). For each such adaptation:

1. What did the source PR's diff add? (call / type / include)
2. What does `{base_branch}` actually have? (`git grep`,
   `git log --oneline {base_branch} -- <file>`)
3. Is the substitute genuinely equivalent, or a superficially-named
   look-alike from a parallel module / different stack layer?

In `forward_port` mode any parallel-module substitution is a finding
(strict equivalence required).

In `backport` mode parallel-module routing IS acceptable when:
   * the parallel module covers the source PR's intent (does the same
     thing, just structured differently — inline call vs extracted
     executor, raw `String` vs typed wrapper, etc.);
   * the routing change is named (`Adapted:` trailer or narration);
   * the total adaptation across the port stays within budget
     (≲ 50 LOC, no new top-level abstractions, no re-implementing the
     source PR's feature).

If a real missing prereq was mis-classified as bucket-2, **flag it**:
name the symbol(s) and explain why the `{base_branch}` substitute
isn't equivalent. This is one of the most valuable things this audit
catches.

If the resolver took the right call (genuine equivalent, just renamed/
moved — or in backport mode, a valid parallel-module adaptation), say
so explicitly — gives reviewers confidence.

## Step 4 — Audit scope (other-file edits)

```bash
git diff --name-only {start_sha}..{new_head}
gh pr diff {source_pr_url} --name-only
```

Port-modified files the source PR didn't touch are a strong out-of-
scope signal. Acceptable: bucket-2 `#include` updates forced by a
header move on `{base_branch}`; a one-line `CMakeLists.txt` tweak to
compile. Broader (a new feature in an unrelated file, comments added
to a file the source PR didn't touch, …) → flag.

## Step 5 — Audit bucket-0 drops (backport mode only — skip in forward-port)

List the `Dropped:` trailers the resolver wrote:

```bash
git log --format='%H %(trailers:key=Dropped,unfold=true,valueonly=true)' {start_sha}..{new_head}
```

For each `Dropped:` value, audit:

1. **What is missing from the port relative to the source PR?** Diff
   the source PR's hunks for the named surface against the net port
   diff and identify the lines that didn't carry over.
2. **Was the dropped surface genuinely isolated and optional in the
   source PR?** A feature flag, an integration behind a default-off
   setting, an `if (feature_x) { … }` block — droppable. A core code
   path the source PR's primary feature actually exercises — NOT
   droppable, even if it "compiles without it".
3. **Does the rest of the port still do the source PR's primary job?**
   If the source PR's headline is "Add foo with bar integration" and
   the resolver dropped foo, that's wrong even if bar was the
   excuse for dropping.
4. **Is the trailer description honest?** "Dropped: arrow-flight —
   depends on PR #91170" is honest; "Dropped: refactor that wasn't
   needed" without a missing-prereq justification is suspicious.

Flag a drop when ANY of:

- the dropped surface is the source PR's primary feature, not an
  optional integration;
- the trailer doesn't name a real missing-prereq cause;
- the port no longer fulfils the source PR's main intent;
- code that should have been dropped *along with* the named surface
  was left behind (e.g. registry entry without its implementation).

Also flag when bucket-0 was clearly applicable but the resolver
**didn't** use it — i.e. you can identify an isolated optional
surface in the source PR diff that should have been droppable, but
the resolver instead carried unrelated extra code into the port to
satisfy it.

**In `forward_port` mode:** any `Dropped:` trailer at all is itself a
finding — bucket-0 was disabled, the resolver shouldn't have
produced any.

## Step 6 — Audit `Adapted:` trailers (backport mode only — skip in forward-port)

List the `Adapted:` trailers the resolver wrote:

```bash
git log --format='%H %(trailers:key=Adapted,unfold=true,valueonly=true)' {start_sha}..{new_head}
```

For each `Adapted:` value, audit:

1. **What gap on `{base_branch}` did it bridge?** The trailer should
   name a concrete shape difference (renamed helper, typed wrapper
   vs raw primitive, inline call vs extracted executor, …) — not
   vague ("API differs", "matches style").
2. **Is the adaptation minimal for the gap?** Type-wrapper unboxing
   should touch only call sites; parallel-module routing should
   replace the call, not duplicate the logic; a shim should be
   ≤ ~30 LOC and named for what it does.
3. **Is the adaptation local to the conflicted area?** Spreading
   adapter shims into unrelated files is out-of-scope.
4. **Total budget.** Sum the LOC of all `Adapted:` changes — exceeding
   ~50 LOC means the resolver should have reported `MISSING_PREREQS`
   instead. Flag.

Flag an adaptation when ANY of:

- the trailer doesn't name a concrete base-branch shape difference;
- the change exceeds the minimal-for-the-gap test (e.g. a 200-LOC
  "shim" that re-implements the source PR's actual feature);
- it touches files / abstractions unrelated to the conflict region;
- it introduces new top-level abstractions, files, tests, metrics,
  or settings beyond what the source PR adds.

Also flag missing `Adapted:` trailers: if the port contains
non-trivial bucket-2 work (shim functions, parallel-module routing,
type-wrapper unboxing) with no corresponding trailer or narration
mention, the resolver hid the adaptation from the audit trail.

**In `forward_port` mode:** any `Adapted:` trailer at all is itself
a finding — backport-mode latitude doesn't apply.

---

## Output contract — strict

End your turn with **exactly** this structure (no preamble, no code
fences, nothing after `END_VERIFY`):

```
VERDICT: OK
SUMMARY: <one sentence>
FINDINGS:
- (none)
END_VERIFY
```

OR

```
VERDICT: NEEDS_ATTENTION
SUMMARY: <one sentence — the most important concern>
FINDINGS:
- <concrete concern #1 — file:line or hunk, and which bucket test it fails>
- <concrete concern #2>
- …
END_VERIFY
```

Rules for findings:

- **Specific.** Name a file, a hunk, an added symbol, and the
  bucket-1/2 test the line fails. "Looks fine" / "seems off" is useless.
- **Few high-quality findings** beat many low-confidence ones. Catch
  the regressions that hurt RelEasy in the past (out-of-scope
  additions, invented logic, mis-classified prereqs). Don't pad with
  cosmetic worries.
- Ambiguous hunk → `NEEDS_ATTENTION` with the ambiguity explained.
  False positives are fine (human dismisses). False negatives are
  what this audit exists to prevent.

## Hard rules (non-negotiable)

- Read-only. No `git add / commit / rebase / reset / checkout / push`,
  no `gh pr edit`, no file edits.
- Do not run the build — that's the resolver's job; you audit scope
  and faithfulness.
- Only inspect named refs (`{start_sha}`, `{new_head}`,
  `{source_pr_merge_sha}`, `{port_branch}`, `{base_branch}`) and the
  source PR. No fishing in unrelated branches.
- Final line: `END_VERIFY`. Nothing after it.
