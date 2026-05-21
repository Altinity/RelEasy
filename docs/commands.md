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
  [`discover-deps`](#releasy-discover-deps) ·
  [`analyze-fails`](#releasy-analyze-fails) ·
  [`continue`](#releasy-continue) ·
  [Sequential mode](#sequential-mode) ·
  [`skip`](#releasy-skip) ·
  [`abort`](#releasy-abort)
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
- Release: [`release`](#releasy-release)
- Features: [`feature *`](#feature-management)

## Global options

| Option | Description | Default |
|--------|-------------|---------|
| `--config <path>` | Path to `config.yaml` | `./config.yaml` |
| `--session-file <path>` | Path to session file. Overrides `session_file:` in config. | `<config-dir>/<name>.session.yaml` |
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

[`discover-deps`](#releasy-discover-deps) is a read-only diagnostic
sibling of `run` — see its section.

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
the AI append fix commits. Filters compose: trusted-reviewer
allowlist + `--since` + dropped if hidden (minimized/outdated) +
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
| `--address-review` / `--no-address-review` | Run the AI review-feedback pass. Requires `review_response.trusted_reviewers` non-empty. | off |
| `--only <url-or-id>` | Single tracked PR (URL — source or rebase) or feature/group id. | — |
| `--dry-run` | No writes anywhere; print intended actions. | off |
| `--stateless` | Skip session/state. Requires `--pr`. | off |

Stateless-only overrides: `--origin`, `--build-command`,
`--claude-command`, `--prompt-file`, `--timeout`, `--max-iterations`.
Rejected without `--stateless`.

Exit: `1` if any PR ended up in `conflict`, any address-review run
failed, or any analyze-fails per-PR run errored — else `0`.

### `releasy discover-deps`

*Auto-discover a PR dependency DAG.*

Trial-cherry-picks every candidate in a scratch worktree, traces conflicts
to older un-ported PRs touching the same files, emits a YAML grouping +
writes a deps overlay at `<session-stem>.deps.yaml` that the loader picks
up on the next [`run`](#releasy-run). Main session is never modified.

Declared `pr_sources.groups[]` are treated as **single super-nodes** —
discovery never subdivides them.

```bash
releasy discover-deps [--onto <ver>] [--work-dir <path>]
                      [-o <path>] [--deps-file <path> | --no-write]
                      [--no-ai] [--max-depth <N>] [--limit <N>]
                      [--include-already-merged]
```

| Output | Where | Override |
|--------|-------|----------|
| Diagnostic report (always written) | `<config-dir>/discover-deps.<base>.yaml` | `-o <path>` |
| Deps overlay (consumed by `run`) | `<session-stem>.deps.yaml` | `pr_sources.deps_file:` in session, or `--deps-file <path>`, or `--no-write` to skip |

`--no-write` and `--deps-file` are mutually exclusive.

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

**Port-branch caching:** when overlay write is enabled, the trial-pick
result is preserved as `feature/<base>/<unit_id>`. The next
[`run`](#releasy-run) reuses it via `if_exists: skip` — no re-cherry-pick,
**no second AI resolve**. `--no-write` disables caching too (true
dry-run). Re-runs always rewrite cache branches.

**Round-trip notes:**

- Auto-discovered singletons become **1-PR groups** in the overlay
  (carries `depends_on:`). Branch naming and AI-context semantics shift
  to `is_group=True`. Move the entry into the main session (and drop
  `auto_discovered:`) to make permanent.
- Re-running rewrites the deps file from scratch. Hand-edits there will
  be lost; use `--no-write` or `--deps-file <path>` to redirect.
- Cycles in `depends_on` (from hand-edits) are rejected at session-load.

**After the target moves:** just re-run `discover-deps`. PRs that landed
upstream drop out automatically. Summary line:

```
discover-deps · base=antalya-26.3 · 24 candidates · 8 already in target
  refresh: 3 removed [auto-pr-100, ...] · 1 added [auto-pr-300]
```

Exit: `0` regardless of conflicts found — read-only diagnostic.

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
```

| Option | Description | Default |
|--------|-------------|---------|
| `--branch <name>` | Operate on one entry — flip from `conflict` to `needs_review`. | full pass |
| `--work-dir <path>` | Working dir. | config / cwd |

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

Writes `config.yaml` (at `--out`) + sibling `<name>.session.yaml`. Refuses
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

## Feature management

Manages the static `features:` list in the session file (the dynamic
counterpart is `pr_sources.*`). Schema:
[configuration.md](configuration.md#namesessionyaml-per-effort-source-data).

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
