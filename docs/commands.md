# Command reference

`releasy <command> --help` is authoritative. This page is the quick map.

For the model behind these commands, see [concepts.md](concepts.md). For
config they read, see [configuration.md](configuration.md).

## Contents

- [Global options](#global-options)
- [At a glance: which command does what](#at-a-glance-which-command-does-what)
- Pipeline:
  [`run`](#releasy-run) ·
  [`refresh`](#releasy-refresh) ·
  [`graph discover`](#releasy-graph-discover) ·
  [`graph update`](#releasy-graph-update) ·
  [`analyze-fails`](#releasy-analyze-fails) ·
  [`continue`](#releasy-continue) ·
  [Sequential mode](#sequential-mode) ·
  [`skip`](#releasy-skip) ·
  [`abort`](#releasy-abort) ·
  [`clear`](#releasy-clear)
- One-off porting:
  [`cherry-pick`](#releasy-cherry-pick) ·
  [`project-backport`](#releasy-project-backport) ·
  [`rebase`](#releasy-rebase)
- Inspection: [`status`](#releasy-status)
- Multi-project:
  [`new`](#releasy-new) ·
  [`list`](#releasy-list) ·
  [`where`](#releasy-where) ·
  [`adopt`](#releasy-adopt)
- Project board:
  [`setup-project`](#releasy-setup-project) ·
  [`project push`](#releasy-project-push) ·
  [`project pull`](#releasy-project-pull)
- Release:
  [`release`](#releasy-release) ·
  [`draft-release`](#releasy-draft-release)
- Features: [`feature *`](#feature-management)
- PR membership: [`pr *`](#pr-membership)

## Global options

| Option | Description | Default |
|--------|-------------|---------|
| `--config <path>` | Path to `config.yaml` | `./config.yaml` |
| `--session-file <path>` | Path to session file. Overrides `session_file:` in config. | `<config-dir>/<target_branch>.session.yaml` (or `<name>` when unset) |
| `--version` | Print version and exit | — |

## At a glance: which command does what

| | `run` | `continue` | `refresh` | `refresh --merge-target` | `refresh --analyze-fails` | `refresh --address-review` | `analyze-fails` |
|--|:-----:|:----------:|:---------:|:------------------------:|:-------------------------:|:--------------------------:|:---------------:|
| Discovers new PRs | ✅ | — | — | — | — | — | — |
| Creates new port branches | ✅ | — | — | — | — | — | — |
| Opens new rebase PRs | ✅ new | ✅ missed | — | — | — | — | — |
| AI-resolves cherry-pick conflicts | ✅ | — | — | — | — | — | — |
| AI-resolves merge conflicts (target moved on) | — | — | — | ✅ | — | — | — |
| AI-investigates failing CI | — | — | — | — | ✅ | — | ✅ |
| AI-addresses reviewer comments | — | — | — | — | — | ✅ | — |
| Refreshes merged/superseded state | — | — | ✅ | ✅ | ✅ | ✅ | — |
| Iterates state entries | only skip / ensure-PR | ✅ all | ✅ all tracked | ✅ all tracked | ✅ all tracked | ✅ all tracked | ✅ all tracked |
| Mutates work-dir | ✅ cherry-picks | ✅ push only | — | ✅ merges | ✅ commits | ✅ commits | ✅ commits |
| Pushes to origin | ✅ | ✅ | — | ✅ (merges only) | ✅ (plain) | ✅ (plain) | ✅ (plain) |

One-liners:

- **`run`** — *do new work.* Discover, cherry-pick, push, open PRs.
- **`continue`** — *I fixed something by hand; reconcile state.* Push/open
  what's pending. No git ops beyond push + status checks.
- **`refresh`** — *re-sync status across tracked PRs* (merged-from-upstream
  sweep, supersede detection, label reconciliation). With
  `--merge-target` it also merges target in and AI-resolves conflicts;
  with `--analyze-fails` it triages failing CI; with `--address-review`
  it lets the AI act on reviewer feedback. The three flags compose;
  inside one invocation they run in the fixed order
  *merge-target → analyze-fails → address-review*.
- **`analyze-fails`** — *CI is red; let AI triage.* Iterative per-shard
  fix loop. Also available as `refresh --analyze-fails` when you want
  to bundle it with the other refresh passes under one lock and one
  status-sync.

> **Why both `run` and `continue`?** `run` only acts on PRs it's
> cherry-picking right now. If you fix a conflict by hand on a branch
> with **no rebase PR yet**, `run` either skips it (`if_exists: skip`)
> or rebuilds from base (`recreate`). `continue` preserves your manual
> fix and just pushes + opens the PR.

[`graph discover`](#releasy-graph-discover) is a read-only diagnostic
sibling of `run` — see its section. [`graph update`](#releasy-graph-update)
refines that graph from issue comments (no git).

The rest ([`skip`](#releasy-skip), [`abort`](#releasy-abort),
[`status`](#releasy-status), board-sync, release, feature) never touch git
history.

## Pipeline

### `releasy run`

*Port PRs onto the base branch.*

Discovers PRs from `pr_sources`, creates port branches from
`origin/<base>`, cherry-picks, opens PRs. AI-resolves cherry-pick
conflicts when `ai_resolve.enabled` is on. Unresolved → singleton dropped
or partial-group draft PR with `ai-needs-attention`. See
[Conflict resolution](concepts.md#conflict-resolution).

A **partial-group draft PR** is auto-resumed on the next `run` (with
`retry_failed` on): the not-yet-applied PRs are appended and re-resolved,
up to `pr_policy.max_partial_continue_attempts` (default 2) before it's
left for manual help. No need to set `if_exists: append` by hand.

Resuming takes precedence over `if_exists: recreate` — an exhausted or
timed-out resolver is not a reason to discard the PRs that did land. The
redo cases are the terminal ones: a rebase PR **closed without merging**
becomes `status: closed` and is rebuilt from base on a renumbered branch
(`pr_policy.recreate_closed_prs`), and a conflict on the *first*
cherry-pick has nothing to keep. Set
`pr_policy.max_partial_continue_attempts: 0` to opt out and get plain
`if_exists` handling back. A `build_failed` branch (resolution landed, the
build/test loop didn't pass) resumes on the same terms, bounded by
`ai_resolve.max_verify_resume_attempts` — unless it has fallen more than
`ai_resolve.max_resume_base_drift` commits behind base, which re-ports it
from base rather than building stale code. A build that never reaches the
compiler (missing build dir, broken toolchain) is an environment fault: it
spends no fix attempt and no resume.

For PRs with an existing rebase PR, `run` doesn't rebuild — it routes
through the same merge-target flow [`refresh`](#releasy-refresh) uses:
clean merge → leave alone; conflict → AI-resolve and plain push (never
force). `if_exists: append` is the only setting that cherry-picks new
commits on top of an existing PR.

```bash
releasy run [--onto <ver>] [--work-dir <path>]
            [--resolve-conflicts | --no-resolve-conflicts]
            [--retry-failed | --no-retry-failed]
            [--merge-target | --no-merge-target]
            [--only <url-or-id> | --pr <URL>]
            [--dry-run]
```

| Option | Description | Default |
|--------|-------------|---------|
| `--onto <ver>` | Version label for derived base name. Naming-only — never resolved as a git ref. | from `target_branch` |
| `--work-dir <path>` | Working dir for git ops. | config / cwd |
| `--resolve-conflicts` / `--no-resolve-conflicts` | Kill-switch for AI resolver. AI runs only if both this and `ai_resolve.enabled` are true. | on |
| `--retry-failed` / `--no-retry-failed` | Re-attempt entries in `conflict` status. No-PR-yet: rebuild from base (only when `if_exists: recreate`). PR open: merge-target flow (PR always preserved). | `pr_policy.retry_failed` |
| `--merge-target` / `--no-merge-target` | Push a merge commit on PRs even without conflicts. Never force-pushes. | off |
| `--only <url-or-id>` | Single PR URL **or** group/singleton id. Drops everything else. **Non-zero** if nothing matches. Mutex with `--pr`. | — |
| `--pr <URL>` | Single PR by URL. Exits **cleanly (0)** when the PR isn't in session scope. Use from webhook/cron callers. Mutex with `--only`. | — |
| `--dry-run` | No writes anywhere (state / git / GitHub). Read-only fetches still happen; cannot predict cherry-pick conflicts. | off |

Exit: `1` on any `conflict` (in scope), else `0`.

### `releasy refresh`

*Maintenance pass over tracked PRs.*

**Never opens PRs, never discovers, never cherry-picks.** Status sync
always runs (catch merges/closes upstream, supersede sweep,
merged-label apply, session-label reconcile). The three branch-mutating
passes are opt-in via flags — bare `refresh` only re-syncs state.

**`--merge-target`** — for each tracked PR, merge `origin/<base>`
into the PR branch via `git merge --no-ff`:

- **clean** → push the merge commit (you opted in)
- **conflict + AI resolves** → push, restore status, set `ai_resolved`
- **conflict + AI gives up** → reset local, mark `conflict`

**`--analyze-fails`** — for each tracked PR, walk failed praktika
status entries on the PR's head SHA, bundle the failing tests per
shard, and let Claude run the iterative fix-build-rerun loop. Same
machinery as the standalone [`analyze-fails`](#releasy-analyze-fails)
command — see that section for outcome classifications, flaky-elsewhere
heuristic, and config. Per-PR sub-flags: `--no-flaky-check`,
`--post-comment` / `--no-post-comment`.

**`--address-review`** — for each tracked PR, fetch comments and let
the AI append fix commits. Filters compose: trust gate
(`trusted_associations` by default, plus any `trusted_reviewers`)
+ `--since` + dropped if hidden (minimized/outdated) +
kept only when the inline thread is unresolved or the top-level
comment has no later reply by the PR author. Linear history only —
append commits, never amend/rebase/force-push. Stateful
`last_review_addressed_at` stamp drives implicit re-run `--since` on
tracked PRs.

All three flags compose. Inside one invocation the phase order is
fixed:

```
status sync → merge-target → analyze-fails → address-review
```

PRs left in `conflict` by the merge phase skip both subsequent passes.
The ordering exists because `analyze-fails` reads commit statuses
tied to the *current* head SHA — any push that lands first
(merge-target, address-review) would invalidate the CI report it
consumes.

Uses `ai_resolve.merge_prompt_file` for conflicts,
`analyze_fails.prompt_file` for CI triage, and
`review_response.prompt_file` for review feedback. Suitable for cron.
Note that [`run`](#releasy-run) also applies the merge flow to PRs
with its own `--merge-target` — explicit `refresh` is mainly for
cron cadence, CI triage, and the review pass.

```bash
releasy refresh [--pr <URL>]
                [--work-dir <path>]
                [--resolve-conflicts | --no-resolve-conflicts]
                [--merge-target | --no-merge-target]
                [--analyze-fails | --no-analyze-fails]
                [--no-flaky-check]
                [--post-comment | --no-post-comment]
                [--address-review | --no-address-review]
                [--only <url-or-id>]
                [--dry-run]
                [--stateless ...]
```

| Option | Description | Default |
|--------|-------------|---------|
| `--pr <URL>` | Operate on one PR by URL. **Stateful mode** silently exits (0) when the URL isn't tracked in this session. **Stateless mode** (`--stateless`) acts on any PR. Required with `--stateless`. | walk every tracked PR |
| `--work-dir <path>` | Working dir. | config / cwd |
| `--resolve-conflicts` / `--no-resolve-conflicts` | Toggle AI resolver (only meaningful with `--merge-target`). | on |
| `--merge-target` / `--no-merge-target` | Merge `origin/<base>` into PR branches + push. | off |
| `--analyze-fails` / `--no-analyze-fails` | Run the AI CI-triage pass on each in-scope PR. | off |
| `--no-flaky-check` | (with `--analyze-fails`) skip flaky-elsewhere cross-check. | off |
| `--post-comment` / `--no-post-comment` | (with `--analyze-fails`) post per-PR summary comment. | `analyze_fails.post_comment_to_pr` |
| `--address-review` / `--no-address-review` | Run the AI review-feedback pass. Needs at least one trust source — `review_response.trusted_associations` (defaults to OWNER/MEMBER/COLLABORATOR/CONTRIBUTOR) or `review_response.trusted_reviewers`. Refuses only if both are empty. | off |
| `--only <url-or-id>` | Single tracked PR (URL — source or rebase) or feature/group id. | — |
| `--dry-run` | No writes anywhere; print intended actions. | off |
| `--stateless` | Skip session/state. Requires `--pr`. | off |

Stateless-only overrides: `--origin`, `--build-command`,
`--claude-command`, `--prompt-file`, `--timeout`, `--max-iterations`.
Rejected without `--stateless`.

Exit: `1` if any PR ended up in `conflict`, any address-review run
failed, or any analyze-fails per-PR run errored — else `0`.

### `releasy graph discover`

*Auto-discover PR groups (and optionally post them as an issue).*

Walks candidates **oldest-merged first**, trial-cherry-picking each onto the
target in a scratch worktree. A real (non-cosmetic) conflict means the PR
continues an earlier one, so the two are **grouped**: every connected set of
such PRs collapses into one combined unit, cherry-picked together in
**apply order** (prerequisite first). Cosmetic conflicts (whitespace, comment
drift, independent regions) are *not* grouped — they port standalone and are
resolved trivially at run time. Writes a deps overlay at
`<session-stem>.deps.yaml` (multi-PR `auto_discovered` groups, `sort: listed`)
that [`run`](#releasy-run) picks up. Main session is never modified.

With `--open-issue` it posts the result as a GitHub issue on origin — each
group shown as a numbered apply sequence — so the team can review and steer it
via [`graph update`](#releasy-graph-update). Re-running `--open-issue` updates
the same issue, not a duplicate.

Declared `pr_sources.groups[]` are treated as **single super-nodes** —
discovery never subdivides or auto-merges them (it only warns if one shares a
dependency with other PRs).

```bash
releasy graph discover [--onto <ver>] [--work-dir <path>]
                       [-o <path>] [--deps-file <path> | --no-write]
                       [--no-ai] [--max-depth <N>] [--limit <N>]
                       [--include-already-merged] [--redo]
                       [--open-issue] [--issue-title <text>]
```

| Option | Description | Default |
|--------|-------------|---------|
| `--redo` | Re-discover from scratch, ignoring the prior graph. By default a re-run reuses every already-discovered unit and only trial-picks PRs new since the last run (see **Incremental re-runs**). | off |

| Output | Where | Override |
|--------|-------|----------|
| Diagnostic report (always written) | `<config-dir>/graph.<base>.yaml` | `-o <path>` |
| Deps overlay (consumed by `run`) | `<session-stem>.deps.yaml` | `pr_sources.deps_file:` in session, or `--deps-file <path>`, or `--no-write` to skip |
| Graph issue (with `--open-issue`) | a new/updated issue on origin | `--issue-title <text>`; labels = `graph.issue_labels` + target-branch name |

`--no-write` and `--deps-file` are mutually exclusive. So are `-o` and
`--open-issue` — the tracked report must stay at the default path so
`graph update` can find it.

**Hybrid AI flow per conflict:**

1. **Deterministic** — `git log target..source -- <file>` →
   `Source-PR:` trailers + merge-containment → candidate unit IDs.
2. **Candidates found** → ask Claude (text-only, no tools) to
   confirm/refine. `discovery_method: git-graph+claude`.
3. **No candidates** → invoke full AI resolver (tools, builds). Outcomes:
   `MISSING_PREREQS:` → those become deps (`ai-resolve`); resolver
   succeeds → no deps needed (`ai-resolve-clean`); resolver fails →
   empty deps + warning (`git-graph`).
4. **Always reset** the scratch worktree.

`--no-ai` skips both AI steps. Trade-off: fast/free but the deterministic
mapping misses semantic dependencies.

**Port-branch caching:** trial-pick results are preserved as
`feature/<base>/<unit_id>` and reused by [`run`](#releasy-run) via
`if_exists: skip` — no re-cherry-pick. This covers **standalone** PRs and
**auto-discovered groups**: a group's combined branch `feature/<base>/<group-id>`
is built (members cherry-picked in apply order, cross-repo commits fetched as
needed) and cached when it applies cleanly. A group that conflicts combined — or
whose commits can't be fetched — isn't cached (`run` rebuilds and resolves it).
User-declared `pr_sources.groups[]` are not combined-cached. `--no-write`
disables caching (true dry-run).

**Upstream-backport recursion** (opt-in): when a **cross-repo** PR
(`repo_slug` ≠ origin) conflicts on a prerequisite that isn't among the
candidates, and `ai_resolve.auto_add_prerequisite_prs.enabled` is set with an
`upstream` remote configured, the prerequisite PR is fetched from `upstream`,
added to the candidate set, trial-picked, and folded into the same group
(prerequisite first). Recurses on the prereq's own prereqs, bounded by
`--max-depth` (default `max_prereq_depth`). Without those settings — or if the
upstream commit can't be fetched — it's flagged `missing-prerequisites`.

**Round-trip notes:**

- Groups are emitted as multi-PR `auto_discovered` entries (`sort: listed`,
  prerequisite first). Move an entry into the main session (drop
  `auto_discovered:`) to make it permanent.
- An `auto_discovered` group read back on a later run stays auto-owned (it is
  re-emitted to the overlay, and may grow if a new PR traces into it). Session
  groups are never rewritten here; `graph update` reconciles those in place,
  creating the session entry if it's missing.
- Re-running rewrites the deps file from scratch — hand-edits are lost; use
  `--no-write` / `--deps-file <path>` to redirect.

**Re-scanning for new PRs:** just re-run `graph discover` — there is no
`graph refresh`. It re-queries `pr_sources` (labels etc.) fresh each run, so
PRs that newly match are picked up and PRs that landed in target drop out; the
summary prints a refresh-diff (`refresh: N removed [...] · M added [...]`).

**Incremental re-runs (default):** a re-run **reuses every unit already in the
prior `graph.<base>.yaml`** — standalone PRs *and* groups — and only trial-picks
PRs that are new in `pr_sources` since the last run (skipping their re-pick,
prereq-tracing, and AI). A reused group is rebuilt from its recorded members; a
brand-new PR that conflicts into a member **attaches** to that group. A PR that
was re-merged (same URL, new SHA) or that dropped out of the candidate set is
re-discovered, not reused.

Pass **`--redo`** to ignore the prior graph and trial-pick everything from
scratch.

When `origin/<base>` has **moved** since the last run, discovery still reuses
the prior groupings but treats cached branches as stale: reused units are
emitted `cached: false` (so `run` re-picks them onto the new tip) and a warning
nudges you toward `--redo`. The refresh-diff (`refresh: N removed · M added`)
still reports what changed either way.

Exit: `0` regardless of conflicts found — read-only diagnostic.

### `releasy graph update`

*Refine the discovered graph from trusted member comments on its issue.*

Feeds new trusted comments on the graph issue (from
[`graph discover --open-issue`](#releasy-graph-discover)) to Claude with the
prior graph and rebuilds the graph from the reply. **No git, no trial-picks.**

A member can ask for any change in prose; Claude decides:

- **add** a PR ("also port #2000");
- **veto** a PR ("don't port #1010") → recorded, and (unless
  `graph.apply_exclusions: false`) added to `exclude_prs` so
  [`run`](#releasy-run) skips it;
- **regroup** into an atomic unit, or **reorder** via `depends_on`.

```bash
releasy graph update [--onto <ver>] [--since <iso>] [--work-dir <path>]
                     [--post-comment | --no-post-comment] [--dry-run]
```

| Option | Description | Default |
|--------|-------------|---------|
| `--onto <ver>` | Base branch; must match the `graph discover` value. | `target_branch` |
| `--since <iso>` | Only ingest comments after this ISO-8601 timestamp. | stored last-ingest watermark |
| `--work-dir <path>` | Locate config / report; no git ops run. | config / cwd |
| `--post-comment` / `--no-post-comment` | Summary comment on the issue after applying. | `graph.post_comment` |
| `--dry-run` | Show the rebuilt graph + intended session edits; write nothing. | off |

**Trust gate.** Only comments whose `author_association` is in
`graph.trusted_associations` (default `OWNER, MEMBER, COLLABORATOR`) or whose
login is in `graph.trusted_reviewers` reach Claude. RelEasy's own comments
are skipped.

**Addressed comments** are collapsed as **Outdated** (unless
`graph.minimize_addressed_comments: false`); comments Claude ignored or
couldn't action (questions, 👍) stay visible so a human sees what's pending.

Rewrites the report, deps overlay, and session (`include_prs` /
`exclude_prs`), and refreshes the issue. Deps here are AI/human-asserted, not
trial-pick-verified — re-run `graph discover` for verified deps.

Exit: `0` on success or clean no-op; `1` on fetch error, malformed reply,
dependency cycle, or a session edit that couldn't be applied.

### `releasy analyze-fails`

*Investigate red CI on a PR (or every tracked PR).*

> Also available as **[`refresh --analyze-fails`](#releasy-refresh)** — same
> per-shard fix loop, runs alongside `--merge-target` / `--address-review`
> under a single project lock. Prefer the refresh form when you want a
> bundled cron pass; reach for standalone `analyze-fails` when CI triage
> is the only thing you're doing.

Walks failed commit-status entries on the PR's head SHA whose `target_url`
points at the praktika JSON viewer (GitHub-Actions job logs are
deliberately ignored). Per failed shard, bundles all failures into a
single Claude invocation that runs iteratively: triage → pick highest-
leverage root cause → fix → build → re-run still-failing tests in one
batch → repeat (up to `max_iterations`).

A **flaky-elsewhere map** cross-references failures across other tracked
PRs (`flaky_elsewhere_threshold` default 2) so master-side flakes get
classified `UNRELATED` instead of fix attempts.

Per-shard outcomes:

| Outcome | Meaning |
|---------|---------|
| `DONE` | Every test now passes (or confirmed flake). |
| `PARTIAL` | Some fixed; some still failing or unexplored. Common. |
| `UNRELATED` | Whole shard is master-side flake. No code changes. |
| `UNRESOLVED` | Couldn't make progress. |

The failed-test list lands at `.releasy/failed-tests.txt` for the AI to
read. Anthropic spend rolls into `ai_cost_usd` (same field as `run` /
`refresh`) and surfaces on the board's
[`AI Cost`](configuration.md#what-gets-synced) column.

```bash
releasy analyze-fails [--pr <URL>] [--work-dir <path>]
                      [--dry-run]
                      [--push | --no-push]
                      [--no-flaky-check]
                      [--post-comment | --no-post-comment]
                      [--only <url-or-id>]
                      [--stateless ...]
```

| Option | Description | Default |
|--------|-------------|---------|
| `--pr <URL>` | PR on origin. Omit to iterate every tracked PR with a `rebase_pr_url`. | — |
| `--work-dir <path>` | Working dir. | config / cwd |
| `--dry-run` | List failed tests + flake counts, exit. No Claude, no push. | off |
| `--push` / `--no-push` | Push AI commits. Plain push, no force; race aborts. | on |
| `--no-flaky-check` | Skip flaky-elsewhere assessment. | off |
| `--post-comment` / `--no-post-comment` | Per-PR summary comment with outcomes + commit list. | `analyze_fails.post_comment_to_pr` |
| `--only <url-or-id>` | Single tracked PR / feature / group. Mutex with `--pr` and `--stateless`. | — |
| `--stateless` | Skip session/state/lock; act on `--pr` alone. `config.yaml` still loaded if present. | off |

Stateless-only overrides: `--origin`, `--build-command`, `--claude-command`,
`--prompt-file`, `--timeout`, `--max-iterations`, `--max-prs`.

Custom Claude allowlists for test runners go in `config.yaml`. Use
`{work_dir}` (alias `{repo_dir}`, `{cwd}`) so paths aren't hard-coded:

```yaml
analyze_fails:
  allowed_tools:
    - Read
    - Bash(git:*)
    - Bash(tests/clickhouse-test:*)
    - Bash({work_dir}/build/programs/clickhouse:*)
```

Exit: `1` on any per-PR failure (fetch / push race / non-linear history);
`0` otherwise even if everything is `UNRELATED`.

> **Linear history only** — same as `refresh --address-review`. Append
> commits only. To retract: `git revert <sha>`.

### `releasy continue`

*Reconcile state after a manual fix.*

Walks every port in state. Doesn't discover, doesn't cherry-pick, doesn't
merge. Per entry:

| State | Action |
|-------|--------|
| `skipped` | leave |
| `conflict`, AI gave up (`failed_step_index` set) | highlight; user must act |
| `conflict`, branch clean (manually resolved) | push, open PR (if `auto_pr`), flip to `needs_review` |
| `conflict`, still unresolved | highlight with conflict files + `git status` hint |
| `branch_created` (branch on origin, no PR) | push (if needed) + open PR |
| `needs_review` | leave |

Always finishes with a project-board reconcile.

```bash
releasy continue [--branch <branch-or-feature-id>] [--work-dir <path>]
                 [--dry-run]
```

| Option | Description | Default |
|--------|-------------|---------|
| `--branch <name>` | Operate on one entry — flip from `conflict` to `needs_review`. | full pass |
| `--work-dir <path>` | Working dir. | config / cwd |
| `--dry-run` | Show what would happen — no state writes, pushes, PR opens, or project sync. Read-only GitHub fetches still happen. | off |

Exit: `1` if any conflict remains (full pass) or the branch couldn't be
marked resolved.

### Sequential mode

When `sequential: true` is in `config.yaml`, both [`run`](#releasy-run)
and [`continue`](#releasy-continue) (without `--branch`) process **one
PR per invocation**. Queue is sorted by `merged_at`.

1. **First invocation** → port earliest PR, push, open rebase PR, exit.
2. **You** review, approve, merge that PR on GitHub.
3. **Next invocation** → checks GitHub:
   - Previous PR **merged** → mark `merged`, re-fetch, port the next.
   - Previous PR **not merged** → exit `1`, change nothing.
4. Repeat. AI-unresolvable conflict → stops; resolve manually + run
   `releasy continue --branch <id>`.

Constraints:
- Incompatible with `pr_sources.groups` (session load fails).
- Requires `target_branch:` in config.
- Re-run [`setup-project`](#releasy-setup-project) once to provision the
  new `merged` Status option.

```bash
releasy run
# (review, approve, merge on GitHub, then:)
releasy continue
```

### `releasy skip`

*Drop a conflicted port from this run.*

Marks `skipped` so subsequent passes ignore it. Doesn't touch git.

```bash
releasy skip --branch <branch-or-feature-id>
```

### `releasy abort`

*Stop tracking this run as in-progress.*

Persists state. No undo for ports already pushed — branches and PRs stay
exactly as they are.

```bash
releasy abort
```

### `releasy clear`

*Wipe local-only port artifacts that never reached a PR.*

With an identifier, clears that one feature. Without it, scans state for
every feature stuck in a damaged local-only state (`conflict` or
`branch_created` with no rebase PR), lists them, and clears after a
confirmation prompt. For each: aborts any in-progress
cherry-pick / merge / rebase, force-deletes the local port branch, and
drops the state entry so the next `run` starts fresh. **Refuses** any
feature whose rebase PR is already open — those live on GitHub and are
out of scope.

```bash
releasy clear [<identifier>] [--work-dir <path>] [--dry-run] [--yes]
```

| Option | Description | Default |
|--------|-------------|---------|
| `<identifier>` | Feature ID / branch / source-PR number / source-PR URL. Omit to sweep all damaged local-only entries. | sweep all |
| `--work-dir <path>` | Working dir for git ops. | config / cwd |
| `--dry-run` | Show what would be cleaned; change nothing. | off |
| `--yes` / `-y` | Skip the confirmation prompt in sweep mode. | off |

## One-off porting

### `releasy cherry-pick`

*One-off cross-repo cherry-pick — no config, no state.*

Cherry-picks a PR (`.../pull/N`, merge commit with `-m 1`), commit
(`.../commit/<sha>`), or tag from any public GitHub repo onto a fresh
branch off `--target` in `--origin`; optionally AI-resolves conflicts,
pushes, and opens a PR back to `--target`. **Persists nothing** — no
config / state / lock / board. Re-running makes a brand-new branch each
time (pin it with `--branch-name`).

The opened PR **respects the target branch's PR template**: its body is a
provenance line, the source PR's `Changelog category` + `Changelog entry`
with a ` (<source-PR-url> by @<author>)` attribution suffix, and the
`CI/CD Options` section copied verbatim from the target branch's
`.github/PULL_REQUEST_TEMPLATE.md` (falling back to a bundled default block
if the template lacks one). The raw upstream body is not pasted in — it
would carry upstream's own template. `--formatting-example` overrides just
the `CI/CD Options` section.

```bash
releasy cherry-pick --origin <url> --target <branch> --commit <github-url>
                    [--branch-name <name>] [--push | --no-push] [--with-pr]
                    [--resolve-conflicts --build-command <cmd>]
                    [--mode backport|forward_port]
                    [--claude-command <exe>] [--prompt-file <path>]
                    [--timeout <s>] [--max-iterations <n>]
                    [--formatting-example <pr-url>] [--work-dir <path>]
```

| Option | Description | Default |
|--------|-------------|---------|
| `--origin <url>` (required) | Origin remote (ssh/https) to clone, push to, and open the PR against. | — |
| `--target <branch>` (required) | Existing origin branch to base the port on / open the PR against. | — |
| `--commit <url>` (required) | GitHub URL to cherry-pick: PR, commit, or tag; any public repo (incl. forks). | — |
| `--branch-name <name>` | Port branch name. | `releasy/port/<id>-<6hex>` |
| `--push` / `--no-push` | Push the branch to origin. | on |
| `--with-pr` | Open a PR from the branch back to `--target` (implies `--push`; needs `RELEASY_GITHUB_TOKEN`). | off |
| `--resolve-conflicts` | On conflict, invoke Claude. Requires `--build-command`. | off |
| `--mode backport\|forward_port` | Port direction for the resolver. `backport` adapts code (adjust signatures, drop non-crucial upstream functionality) and declares a prerequisite only when the PR truly can't stand without it; `forward_port` is strict (reports `MISSING_PREREQS`). | `backport` |
| `--build-command <cmd>` | Shell command Claude runs to verify the resolution compiles. Required with `--resolve-conflicts`. | — |
| `--claude-command <exe>` | Claude executable. | `claude` |
| `--prompt-file <path>` | AI-resolve prompt template. | bundled |
| `--timeout <s>` | Per-attempt Claude timeout (seconds). | `7200` |
| `--max-iterations <n>` | Max build attempts per resolve. | `5` |
| `--formatting-example <pr-url>` | Override the "CI/CD Options" section with this origin-repo PR's instead of the target template's (needs `--with-pr`). | target PR template |
| `--work-dir <path>` | Working dir for git ops. | cwd |

### `releasy project-backport`

*Batch backport upstream PRs queued in a GitHub Project — no config, no state.*

For "Stable" releases. Walks a GitHub Project (one view per version), and
for every item whose **content is an upstream `ClickHouse/ClickHouse` PR**
whose **`Port Versions` field includes `--version`**, opens a Backport PR
into `--target` on origin (Altinity/ClickHouse): cherry-picks the upstream
merge commit (`-m 1`), optionally AI-resolves conflicts in backport mode,
pushes, and opens the PR. The PR title is `<version> Backport of #<n> -
<upstream title>`; the body carries the upstream PR's changelog
category + entry (entry text with ` (<upstream url> by @<author>)`
appended) followed by the `CI/CD Options` section taken verbatim from the
target branch's `PULL_REQUEST_TEMPLATE.md`; a `<version>` label is added.
After creating the PR it is **added back to the same project** with its
`Port Versions` set to `<version>` (nothing is ever deleted).

**Persists nothing** — the GitHub Project + open origin PRs are the only
source of truth. Re-running is **idempotent**: any item that already has a
backport PR into `--target` (matched by branch name, then by an open PR
titled `Backport of #<n>` / referencing the upstream URL) is skipped. Only
ever opens PRs into origin — never upstream. Needs `RELEASY_GITHUB_TOKEN`
with the `project` scope.

```bash
releasy project-backport --project <project-url> --version <ver> --target <branch>
                         [--origin <url>] [--work-dir <path>]
                         [--resolve-conflicts --build-command <cmd>]
                         [--claude-command <exe>] [--prompt-file <path>]
                         [--timeout <s>] [--max-iterations <n>]
                         [--limit <n>] [--dry-run]
```

| Option | Description | Default |
|--------|-------------|---------|
| `--project <url>` (required) | GitHub ProjectV2 URL, e.g. `https://github.com/orgs/Altinity/projects/26`. | — |
| `--version <ver>` (required) | Target version (e.g. `24.8`). Filters items by `Port Versions`; also the PR label, title prefix, and `Port Versions` value set on the new card. | — |
| `--target <branch>` (required) | Existing origin branch to cherry-pick onto and open PRs against (e.g. `customizations/24.8.14`). | — |
| `--origin <url>` | Origin remote to clone / push / open PRs against. | `git@github.com:Altinity/ClickHouse.git` |
| `--work-dir <path>` | Working dir for git ops. If omitted, a stable cache clone is created/reused. | `$XDG_CACHE_HOME/releasy/Altinity-ClickHouse` |
| `--resolve-conflicts` | On conflict, invoke the AI resolver (backport mode). Requires `--build-command`. | off |
| `--build-command <cmd>` | Shell command Claude runs to verify the resolution compiles. Required with `--resolve-conflicts`. | — |
| `--claude-command <exe>` | Claude executable. | `claude` |
| `--prompt-file <path>` | AI-resolve prompt template. | bundled |
| `--timeout <s>` | Per-attempt Claude timeout (seconds). | `7200` |
| `--max-iterations <n>` | Max build attempts per resolve. | `5` |
| `--limit <n>` | Process at most `n` items (newest upstream PR first). | all |
| `--dry-run` | Plan only: list qualifying items and what would be created / skipped. No clone, cherry-pick, push, or GitHub writes. | off |

### `releasy rebase`

*Re-port an existing rebase PR onto a different target branch.*

For each PR in scope: skips if it already targets `--target`; otherwise
branches off `origin/<target>`, cherry-picks its commits one at a time
(AI-resolving conflicts; falls back to a single squashed
`git merge --squash` if the cherry-pick path won't apply), pushes a fresh
branch, opens a new PR (referencing `Port of <old PR> onto <target>`),
and closes the original with a `superseded by <new PR>` comment. With
`--pr` only that PR; without it, every tracked rebase PR in state.
**Never mutates the state file** — it's a one-way porter, not a migration.

```bash
releasy rebase --target <branch> [--pr <url>] [--only <url-or-id>]
               [--resolve-conflicts | --no-resolve-conflicts]
               [--work-dir <path>] [--dry-run]
```

| Option | Description | Default |
|--------|-------------|---------|
| `--target <branch>` (required) | Existing origin branch to rebase onto. | — |
| `--pr <url>` | The rebase PR to port. Omit to walk every tracked rebase PR. | all tracked |
| `--only <url-or-id>` | Restrict the walk to one tracked PR (source or rebase URL) or feature / group ID. Mutex with `--pr`; non-zero if no match. | — |
| `--resolve-conflicts` / `--no-resolve-conflicts` | AI resolver on conflicts (needs `ai_resolve.enabled`). | on |
| `--work-dir <path>` | Working dir for git ops. | config / cwd |
| `--dry-run` | No branches / cherry-picks / pushes / PR changes. Read-only fetches still happen. | off |

## Inspection

### `releasy status`

*Print current pipeline state.*

Rich-text per-status sub-tables, ordered with conflicts first (see
`STATUS_DISPLAY_ORDER` in [`src/releasy/state.py`](../src/releasy/state.py)).
Reads state only — no git, no network.

```bash
releasy status
```

## Multi-project

See [concepts.md → Multiple projects](concepts.md#multiple-projects-in-parallel).

### `releasy new`

*Scaffold a fresh project.*

Writes `config.yaml` (at `--out`) + sibling `<target_branch>.session.yaml`
(falls back to `<name>` when `--target-branch` is omitted). Refuses
to overwrite. Prints config's absolute path on stdout (everything else on
stderr) so it composes:

```bash
cd $(dirname "$(releasy new --target-branch antalya-25.8 --project antalya)")
```

```bash
releasy new [--name <slug>] [--target-branch <branch>] [--project <id>] [--out <path>]
```

| Option | Description | Default |
|--------|-------------|---------|
| `--name <slug>` | `[A-Za-z0-9._-]{1,64}`. | auto: `<target-branch>-<6hex>` |
| `--target-branch <branch>` | Seeds `target_branch:` + auto-name. | empty |
| `--project <id>` | Seeds `project:`. | empty |
| `--out <path>` | Config path. Refuses to overwrite. | `./config.yaml` |

Auto-generated names get a 6-hex CSPRNG suffix so back-to-back calls don't
collide.

### `releasy list`

*Every project on this machine.* Alias: `releasy ls`.

One row per project: name, phase, feature counts, last-run timestamp,
owning config path.

```bash
releasy list
```

### `releasy where`

*Print the state-file path for the current config.*

```bash
releasy where
# /home/<you>/.local/state/releasy/antalya-26.3.state.yaml
```

### `releasy adopt`

*Rebind state to the current config.*

After moving/renaming a `config.yaml`, the next mutating command trips an
ownership-collision check. Run `adopt` from the new location to rebind;
the old path is appended to a history list for audit.

If no state exists yet, creates an empty one — doubles as "register this
config without doing anything else".

```bash
releasy adopt
```

## Project board sync

No-ops unless `notifications.github_project` is set and
`RELEASY_GITHUB_TOKEN` has `project` scope. UI setup:
[configuration.md → GitHub Project board](configuration.md#github-project-board).

### `releasy setup-project`

*Create / verify the GitHub Project.*

If configured: verifies project, reconciles Status options to the
canonical set, provisions `AI Cost`. If unset: creates a new project,
prints the URL, runs an initial sync.

```bash
releasy setup-project
```

> **Destructive:** drops non-canonical Status options. Cards on dropped
> options are re-synced based on local state immediately after.

### `releasy project push`

*Push local state to the project board.*

Reconciles every known feature: attaches missing PR cards, refreshes
existing, updates Status, and deletes cards no longer backed by local
state. No git, no PRs — only the board. Use after hand-editing state,
rotating tokens, or wiring up a new project URL.

```bash
releasy project push
```

Exit: `1` if sync was skipped (no project / no token / bad URL) or any
item failed, `0` otherwise.

### `releasy project pull`

*Rebuild local state from GitHub + the project board.*

Use when local state is missing or stale (fresh machine, teammate
takeover, throwaway CI runner) but the world outside is intact. Read-only
on git — only the GitHub APIs are hit. Merges into any existing state
file; the board wins for `Skipped` and `AI Cost`, GitHub wins for PR
status, local-only fields (`ai_iterations`, `failed_step_index`,
`partial_pr_count`) are preserved.

```bash
releasy project pull
```

Requires `notifications.github_project` in config and
`RELEASY_GITHUB_TOKEN` with `project` scope.

## Release construction

### `releasy release`

*Build a release branch from a tag.*

Creates a release base branch from `--base-tag` and merges every finished
port (`needs_review`, optionally `skipped`) onto it.

```bash
releasy release --base-tag <tag> --name <branch> [--strict] [--include-skipped] [--work-dir <path>]
```

| Option | Description | Default |
|--------|-------------|---------|
| `--base-tag <tag>` (required) | Tag/ref to base on. Must be local or fetchable from origin. | — |
| `--name <branch>` (required) | Release branch name. | — |
| `--strict` | Abort if any enabled feature isn't `needs_review`. | off |
| `--include-skipped` | Include `skipped` features. | off |
| `--work-dir <path>` | Working dir. | config / cwd |

### `releasy draft-release`

*Generate a release changelog from the PRs in a `--from`..`--to` range.*

When `--from` is an **ancestor** of `--to` (the usual same-branch release),
walks the commit range and collects its **first-parent** PRs — the exact
delta. `--base` is not consulted in this mode. When `--from` is **not** an
ancestor (e.g. an upstream fork tag not on the branch), falls back to a
**date-window Search call** for PRs whose base is `--base` (the target
branch) that merged in the `--from`..`--to` window. Either way, forward-ports
(`forwardport` / `forward-port` label or title) are dropped, each PR is
classified by its Changelog category, and the result is rendered as
Altinity's release-notes markdown. `--prs` / `--prs-file` supply an explicit
PR set instead, bypassing discovery. With `-o` writes to disk and publishes
nothing; without it, creates a **draft** GitHub release on origin (tag =
`--name`, commitish = `--to`) and prints its URL.

The walk reads PR numbers from GitHub's merge-commit / squash-merge subjects,
so rebase-merged PRs (which leave no PR reference) aren't detected — origin
uses squash / merge-commit. `--from` / `--to` are resolved against a local
clone.

```bash
releasy draft-release --from <ref> --to <ref> [--base <branch>]
                      [--prs <url> ...] [--prs-file <path>]
                      [--name <tag>] [--title <text>] [-o <file>]
                      [--compared-to-url <url>] [--docker-image-url <url>]
                      [--work-dir <path>]
```

| Option | Description | Default |
|--------|-------------|---------|
| `--from <ref>` (required) | Range lower bound, **exclusive** (usually the previous release / upstream fork tag). | — |
| `--to <ref>` (required) | Range upper bound (inclusive) and draft commitish (release-branch tip or the tag being cut). | — |
| `--base <branch>` | Branch whose merged PRs are collected — **only used in the date-window fallback** (when `--from` isn't an ancestor of `--to`). | target branch → `--to` |
| `--prs <url>` | Explicit PR URL to include (repeatable); bypasses discovery. | — |
| `--prs-file <path>` | File of PR URLs (one per line, `#` comments ok); merged with `--prs`. | — |
| `--name <tag>` | Release tag. Defaults to `--to` when it's a tag; left blank if `--to` is a commit / branch. | — |
| `--title <text>` | Changelog heading / draft display name. | prettified `--name` |
| `-o` / `--output <file>` | Write markdown to file instead of creating a draft release. | — |
| `--compared-to-url <url>` | Override the "as compared to" header link. | auto |
| `--docker-image-url <url>` | Docker image URL; placeholder `sha256-TBD` if omitted. | — |
| `--work-dir <path>` | Local clone for resolving `--from` / `--to`. | config / cwd |

## Feature management

Manages the static `features:` list in the session file (the dynamic
counterpart is `pr_sources.*`). Schema:
[configuration.md](configuration.md#target_branchsessionyaml-per-effort-source-data).

```bash
releasy feature add --id <id> --source-branch <branch> --description <desc>
releasy feature enable --id <id>
releasy feature disable --id <id>
releasy feature remove --id <id>
releasy feature list
```

| Subcommand | Description |
|------------|-------------|
| `add` | Append entry. Requires `--id`, `--source-branch`, `--description`. |
| `enable` | Set `enabled: true`. Requires `--id`. |
| `disable` | Set `enabled: false`. Requires `--id`. |
| `remove` | Delete from session. Doesn't touch branches. Requires `--id`. |
| `list` | Print features grouped by enabled/disabled. |

## PR membership

Add, remove, and list individual PR URLs in the session so you never
hand-edit `pr_sources.include_prs`, `pr_sources.exclude_prs`, or
`pr_sources.groups[].prs`. Schema:
[configuration.md](configuration.md#target_branchsessionyaml-per-effort-source-data).

```bash
releasy pr add <PR-URL> [--group <id>] [--context <text>]
releasy pr remove <PR-URL> [--keep-discovery]
releasy pr list
```

| Subcommand | Description |
|------------|-------------|
| `add` | Append URL to `pr_sources.include_prs` (or `groups[<id>].prs` with `--group`). Validates the URL via the GitHub API, idempotent on re-add, and clears the URL from `exclude_prs` if it was previously excluded. Optional `--context` sets the per-PR `ai_context` note. |
| `remove` | Drop the URL from every session list (`include_prs`, every group's `prs`, both `ai_context` dicts) and purge the matching `FeatureState`. By default also appends the URL to `exclude_prs` so label-driven discovery doesn't re-add it on the next refresh; pass `--keep-discovery` to skip that step. Refuses if the URL is part of a multi-PR group still in state (groups are atomic — use `releasy clear <identifier>` to wipe the whole group). |
| `list` | Print every URL the session references — top-level `include_prs`, each group's `prs`, and `exclude_prs` — with their `ai_context` notes. |

Exit: `1` on a malformed URL, an unreachable PR, a group id that doesn't
exist, or any cross-list collision (URL already in `include_prs` when
adding with `--group`, etc.); `0` otherwise. All mutating subcommands
take the project lock; `list` is read-only.
