# Command reference

`releasy <command> --help` is authoritative; this page summarizes behavior.
Concepts: [concepts.md](concepts.md). Config keys:
[configuration.md](configuration.md).

## Contents

- Pipeline: [`run`](#releasy-run) · [`continue`](#releasy-continue) ·
  [Sequential mode](#sequential-mode) · [`refresh`](#releasy-refresh) ·
  [`analyze-fails`](#releasy-analyze-fails) · [`skip`](#releasy-skip) ·
  [`hold`](#releasy-hold) · [`unhold`](#releasy-unhold) ·
  [`mark-reverted`](#releasy-mark-reverted) · [`abort`](#releasy-abort) ·
  [`clear`](#releasy-clear)
- Dependency graph: [`graph discover`](#releasy-graph-discover) ·
  [`graph update`](#releasy-graph-update) · [`graph sync`](#releasy-graph-sync)
- One-off porting: [`cherry-pick`](#releasy-cherry-pick) ·
  [`project-backport`](#releasy-project-backport) · [`rebase`](#releasy-rebase)
- Projects: [`status`](#releasy-status) · [`new`](#releasy-new) ·
  [`list`](#releasy-list) · [`where`](#releasy-where) · [`adopt`](#releasy-adopt)
- Project board: [`setup-project`](#releasy-setup-project) ·
  [`project push`](#releasy-project-push) · [`project pull`](#releasy-project-pull)
- Release: [`release`](#releasy-release) · [`draft-release`](#releasy-draft-release)
- Session editing: [`feature`](#feature-management) · [`pr`](#pr-membership)

## Global options

| Option | Description | Default |
|--------|-------------|---------|
| `--config`, `--config-file <path>` | Path to `config.yaml`. | `./config.yaml` |
| `--session-file <path>` | Session file; overrides `session_file:`. | `<config-dir>/<target_branch>.session.yaml` |
| `--version` | Print version. | — |

Commands marked *stateless* need no project: only `RELEASY_GITHUB_TOKEN`.

## Pipeline

### `releasy run`

Discover PRs, cherry-pick each unit onto its own branch, push, open PRs.

- Conflicts are AI-resolved when `ai_resolve.enabled`; otherwise see
  [Conflicts](concepts.md#conflicts).
- Units with an open rebase PR are not rebuilt: the base is merged in (pushed
  only on conflict, or always with `--merge-target`; never force-pushed). New
  commits are added only with `if_exists: append` or for a group that gained
  members.
- Partially applied groups and `build_failed` branches are resumed, bounded by
  `pr_policy.max_partial_continue_attempts` and
  `ai_resolve.max_verify_resume_attempts` / `max_resume_base_drift`.
- Units with a blocking [stall](concepts.md#stall-reasons) are skipped.
- A unit marked **outdated** (a PR left it via `pr remove`, a graph veto, or
  regrouping) is re-ported as with `--redo`.
- A standalone PR folded into a group loses its own entry; its in-flight port
  PR is closed as superseded by the group's PR.

| Option | Description | Default |
|--------|-------------|---------|
| `--onto <ver>` | Version label for the derived base name; not a git ref. | `target_branch` |
| `--work-dir <path>` | Working dir for git. | config / cwd |
| `--resolve-conflicts` / `--no-resolve-conflicts` | AI resolver (also needs `ai_resolve.enabled`). | on |
| `--retry-failed` / `--no-retry-failed` | Re-attempt `conflict` entries. | `pr_policy.retry_failed` |
| `--merge-target` / `--no-merge-target` | Push a base merge into open PRs even without conflicts. | off |
| `--only <url-or-id>` | One PR URL or unit id (`pr-123`, group id). Exit 1 if no match. | — |
| `--pr <url>` | One PR by URL; exit 0 if not in scope (for webhooks/cron). Mutex with `--only`. | — |
| `--redo` | With `--only`/`--pr`: drop the unit's state, close its open port PR and rebuild on a renumbered branch (or rebuild a PR-less branch in place). Refused for merged ports and in sequential mode. | off |
| `--ignore-stalls` | Retry stalled units anyway. | off |
| `--dry-run` | No writes (state, git, GitHub); can't predict conflicts. | off |

Exit `1` if any in-scope unit is in `conflict`.

### `releasy continue`

Reconcile state after a manual fix: no discovery, no cherry-picks. Pushes and
opens PRs for resolved conflicts and for branches without a PR, reports
unresolved conflicts, then syncs the board. Unlike `run`, it keeps a hand-made
fix on a branch that has no PR yet.

| Option | Description | Default |
|--------|-------------|---------|
| `--branch <id>` | Only mark this branch / feature id resolved. | all entries |
| `--work-dir <path>` | Working dir. | config / cwd |
| `--dry-run` | No writes, pushes, PRs or board sync. | off |

Exit `1` if a conflict remains.

### Sequential mode

With `sequential: true`, `run` and `continue` (without `--branch`) port one PR
per invocation in `merged_at` order. The next invocation proceeds only if the
previous rebase PR has merged; otherwise it exits `1`. Incompatible with
`pr_sources.groups`; `continue` needs `target_branch`.

### `releasy refresh`

Maintenance over tracked PRs; never discovers, cherry-picks or opens PRs.
Always syncs status (upstream merges/closes, superseded sweep, `merged_label`,
`pr_labels`). Optional passes, in this fixed order:

1. `--merge-target` — merge `origin/<base>` into each PR branch; AI-resolve
   conflicts; plain push. Unresolved → `conflict`, skipped by later passes.
2. `--analyze-fails` — same as [`analyze-fails`](#releasy-analyze-fails).
3. `--address-review` — AI appends commits for trusted, unresolved review
   comments newer than the last addressed run (see `review_response`).
   History stays linear.

| Option | Description | Default |
|--------|-------------|---------|
| `--pr <url>` | One PR; exit 0 if untracked (any PR with `--stateless`). | all tracked |
| `--only <url-or-id>` | One tracked PR or unit id. | — |
| `--work-dir <path>` | Working dir. | config / cwd |
| `--resolve-conflicts` / `--no-resolve-conflicts` | AI resolver for merge conflicts. | on |
| `--merge-target` / `--no-merge-target` | Pass 1. | off |
| `--analyze-fails` / `--no-analyze-fails` | Pass 2. | off |
| `--no-flaky-check`, `--no-baseline-check`, `--post-comment` / `--no-post-comment` | Pass 2 options, as in `analyze-fails`. | — |
| `--address-review` / `--no-address-review` | Pass 3. | off |
| `--ai-backend cli\|codex\|api` | Override `ai_backend`. | config |
| `--dry-run` | No writes. | off |
| `--stateless` | No session/state/lock; requires `--pr`. Uses `config.yaml` if present. | off |
| `--origin`, `--build-command`, `--claude-command`, `--prompt-file`, `--timeout`, `--max-iterations` | Overrides, `--stateless` only. | config |

Exit `1` if any PR ends in `conflict` or a pass fails.

### `releasy analyze-fails`

AI triage of failed CI on a PR (or every tracked PR). Reads every failed
commit status: praktika JSON reports and TestFlows regression reports. Checks
without per-test results become job-level shards
(`analyze_fails.job_level_failures`). Per shard, one AI session loops triage →
fix → build → re-run, up to `analyze_fails.max_iterations`.

- **Baseline:** failures are compared with the last target-branch CI run
  predating the PR and labelled *pre-existing*, *new since baseline* or
  *baseline says nothing*.
- **Flaky-elsewhere:** failures seen on ≥ `flaky_elsewhere_threshold` other
  tracked PRs are flagged as likely flakes.
- **Audit:** shards with commits, or with a verdict contradicting the
  evidence, are audited by a read-only session. A dispute triggers a new round
  (up to `max_investigation_rounds`); a remaining dispute labels the PR
  `ai-needs-verify`.
- Outcomes: `DONE`, `PARTIAL`, `UNRELATED`, `UNRESOLVED` (`DISPUTED` if an
  audit dispute stands). Commits are append-only; push is plain.

| Option | Description | Default |
|--------|-------------|---------|
| `--pr <url>` | PR to analyze. | all tracked with a rebase PR |
| `--only <url-or-id>` | One tracked PR or unit. Mutex with `--pr`, `--stateless`. | — |
| `--work-dir <path>` | Working dir. | config / cwd |
| `--dry-run` | List failures and verdicts; no AI, no push. | off |
| `--push` / `--no-push` | Push AI commits. | on |
| `--no-flaky-check` | Skip flaky-elsewhere map. | off |
| `--no-baseline-check` | Skip baseline comparison. | off |
| `--post-comment` / `--no-post-comment` | Summary comment per PR. | `analyze_fails.post_comment_to_pr` |
| `--ai-backend cli\|codex\|api` | Override `ai_backend`. | config |
| `--stateless` | No session/state/lock; acts on `--pr`. | off |
| `--origin`, `--build-command`, `--claude-command`, `--prompt-file`, `--timeout`, `--max-iterations`, `--max-prs` | Overrides, `--stateless` only. | config |

Exit `1` on any per-PR failure (fetch, push race, non-linear history).

### `releasy skip`

`releasy skip --branch <id>` — mark a port `skipped`. State only.

### `releasy hold`

`releasy hold <pr-url> [--reason <text>]` — add the PR to
`pr_sources.on_hold` ([semantics](configuration.md#on-hold-vs-excluded)).
Re-holding updates the reason. Refused (exit `1`) for a PR in `exclude_prs`.

### `releasy unhold`

`releasy unhold <pr-url>` — remove it from `pr_sources.on_hold`; it ports on
the next `run`.

### `releasy mark-reverted`

`releasy mark-reverted --branch <id> [--reason <text>]` — mark a merged port
`reverted` (state only). `run`/`refresh` then leave it alone; only
`pr_policy.recreate_reverted_prs` re-ports it. The reason (default
`port reverted on target — do not re-port`) shows in `status` and on the
graph issue. To undo, set the entry's `status:` back to `merged` in the state
file (`releasy where`).

### `releasy abort`

Persist state and exit; nothing is rolled back.

### `releasy clear`

`releasy clear [<identifier>] [--work-dir <path>] [--dry-run] [-y|--yes]` —
for ports that never got a PR: abort any in-progress git operation, delete the
local branch, and drop the state entry. `<identifier>` is a feature id,
branch, source PR number or URL; without it, every `conflict` /
`branch_created` entry without a PR is listed and cleared after confirmation
(`--yes` skips it). Refuses entries with an open rebase PR.

## Dependency graph

### `releasy graph discover`

Trial-cherry-picks candidate PRs (oldest merged first) onto the target in a
scratch worktree. PRs that really conflict with an earlier one are grouped
into one unit, applied prerequisite first. Writes:

| Output | Default path | Override |
|--------|--------------|----------|
| Report | `<config-dir>/graph.<base>.yaml` | `-o` |
| Deps overlay (read by `run`) | `<session-stem>.deps.yaml` | `pr_sources.deps_file`, `--deps-file`, `--no-write` |
| Graph issue (`--open-issue`) | issue on origin | `--issue-title` |

- Re-runs reuse the prior graph and only trial-pick new PRs (`--redo` starts
  over). Hand edits to the overlay are overwritten; move a group into the
  session to keep it.
- Declared session groups are never split or merged; their `depends_on` edges
  are kept.
- Clean trial picks are cached as port branches that `run` reuses.
- Conflicts are mapped to prerequisites via git history, refined by AI
  (`--no-ai` disables). With `ai_resolve.auto_add_prerequisite_prs` and an
  `upstream` remote, missing upstream prerequisites are pulled in recursively.
- Ports whose unit lost a PR are marked outdated for `run`.

| Option | Description | Default |
|--------|-------------|---------|
| `--onto <ver>` | Base branch. | `target_branch` |
| `--work-dir <path>` | Working dir. | config / cwd |
| `-o`, `--output <path>` | Report path. Mutex with `--open-issue`. | see above |
| `--deps-file <path>` / `--no-write` | Redirect / skip the overlay (mutex). | — |
| `--no-ai` | Deterministic mapping only. | off |
| `--max-depth <n>` | Upstream prerequisite recursion cap. | `auto_add_prerequisite_prs.max_prereq_depth` |
| `--limit <n>` | Scan only the most recent N units. | all |
| `--include-already-merged` | Keep units already in target in the report. | off |
| `--redo` | Ignore the prior graph. | off |
| `--open-issue` / `--no-open-issue` | Open or update the graph issue. | off |
| `--issue-title <text>` | Issue title. | `Port graph for <base>` |

Exit `0` regardless of conflicts.

### `releasy graph update`

Feeds new trusted comments on the graph issue (`graph.trusted_*`) to the AI
and rebuilds the graph — no git. Comments can add, veto (→ `exclude_prs`),
hold / release (→ `on_hold`), regroup or reorder PRs. Rewrites the report,
overlay and session, refreshes the issue, and collapses addressed comments.
Added PRs are not trial-picked; run `graph discover` to analyze them.

| Option | Description | Default |
|--------|-------------|---------|
| `--onto <ver>` | Base branch (same as for `discover`). | `target_branch` |
| `--since <iso>` | Only comments after this time. | stored watermark |
| `--work-dir <path>` | Locates config/report. | config / cwd |
| `--post-comment` / `--no-post-comment` | Summary comment. | `graph.post_comment` |
| `--dry-run` | Show result; write nothing. | off |

Exit `1` on fetch error, malformed AI reply, cycle, or failed session edit.

### `releasy graph sync`

Re-renders the graph issue with per-unit progress checkboxes and status
markers (merged, in review, conflict, blocked, …, plus the stall reason). A box
is ticked once the port PR exists. Merged/superseded groups are folded;
reverted, discarded and excluded units get their own sections. Runs
automatically after `run` / `refresh` (`graph.sync_progress`). No git, no AI.

| Option | Description | Default |
|--------|-------------|---------|
| `--onto <ver>` | Base branch. | `target_branch` |
| `--open-issue` | Create the issue from the saved report if it has none. | off |
| `--dry-run` | Print; don't edit the issue. | off |

Exit `1` if there is no report, no issue, or the edit fails.

## One-off porting

### `releasy cherry-pick`

*Stateless.* Cherry-pick a PR (merge commit, `-m 1`), commit or tag from any
public GitHub repo onto a new branch off `--target` in `--origin`; optionally
AI-resolve, push and open a PR. The PR body uses the source changelog entry
and the target's PR-template `CI/CD Options` section.

| Option | Description | Default |
|--------|-------------|---------|
| `--origin <url>` | Origin remote (required). | — |
| `--target <branch>` | Existing origin branch (required). | — |
| `--commit <url>` | PR / commit / tag URL (required). | — |
| `--branch-name <name>` | Port branch. | `releasy/port/<id>-<6hex>` |
| `--push` / `--no-push` | Push the branch. | on |
| `--with-pr` | Open a PR into `--target`. | off |
| `--resolve-conflicts` | AI-resolve; needs `--build-command`. | off |
| `--mode backport\|forward_port` | Resolver mode. | `backport` |
| `--build-command <cmd>` | Build check for the resolver. | — |
| `--claude-command <exe>` | Claude executable. | `claude` |
| `--ai-backend cli\|api` | AI backend. | `cli` |
| `--prompt-file <path>` | Resolver prompt. | bundled |
| `--timeout <s>` | Per-attempt timeout. | `7200` |
| `--max-iterations <n>` | Build attempts. | `5` |
| `--formatting-example <pr-url>` | Take `CI/CD Options` from this PR (needs `--with-pr`). | target template |
| `--work-dir <path>` | Working dir. | cwd |

### `releasy project-backport`

*Stateless.* For each item in a GitHub Project that is an upstream
`ClickHouse/ClickHouse` PR with `--version` in its `Port Versions` field:
cherry-pick it onto `--target`, open a backport PR on origin (title
`<version> Backport of #<n> - <title>`, label `<version>`), and add that PR to
the project. Items that already have a backport PR are skipped. Token needs
the `project` scope.

| Option | Description | Default |
|--------|-------------|---------|
| `--project <url>` | ProjectV2 URL (required). | — |
| `--version <ver>` | Version, e.g. `24.8` (required). | — |
| `--target <branch>` | Existing origin branch (required). | — |
| `--origin <url>` | Origin remote. | `git@github.com:Altinity/ClickHouse.git` |
| `--work-dir <path>` | Working dir. | `$XDG_CACHE_HOME/releasy/Altinity-ClickHouse` |
| `--resolve-conflicts` | AI-resolve (backport mode); needs `--build-command`. | off |
| `--build-command`, `--claude-command`, `--ai-backend cli\|api`, `--prompt-file`, `--timeout`, `--max-iterations` | As in `cherry-pick`. | — |
| `--limit <n>` | Process at most N items, newest first. | all |
| `--dry-run` | Plan only. | off |

### `releasy rebase`

Re-port rebase PRs onto another branch: for each PR not already targeting
`--target`, cherry-pick its commits onto a new branch from `origin/<target>`
(falling back to a squash merge), open a new PR, and close the old one as
superseded. Does not modify state.

| Option | Description | Default |
|--------|-------------|---------|
| `--target <branch>` | Existing origin branch (required). | — |
| `--pr <url>` | One rebase PR. | all tracked |
| `--only <url-or-id>` | One tracked PR or unit. Mutex with `--pr`. | — |
| `--resolve-conflicts` / `--no-resolve-conflicts` | AI resolver (needs `ai_resolve.enabled`). | on |
| `--work-dir <path>` | Working dir. | config / cwd |
| `--dry-run` | No writes. | off |

## Projects

### `releasy status`

Print state grouped by status, with a **Why** column for stalls. Read-only.

### `releasy new`

Write `config.yaml` and a sibling `<target_branch>.session.yaml` (or
`<name>.session.yaml`). Refuses to overwrite. Prints the config path on
stdout.

| Option | Description | Default |
|--------|-------------|---------|
| `--name <slug>` | Project name. | `<target-branch>-<6hex>` |
| `--target-branch <branch>` | Seeds `target_branch`. | empty |
| `--project <id>` | Seeds `project`. | empty |
| `--out <path>` | Config path. | `./config.yaml` |

### `releasy list`

List all projects on this machine with their config paths. Alias: `ls`.

### `releasy where`

Print the state-file path for the current config.

### `releasy adopt`

Rebind the state file to the current config (after moving it); creates empty
state if none exists.

## Project board

Needs a token with `project` scope; `project push` / `pull` also need
`notifications.github_project`. See
[GitHub Project board](configuration.md#github-project-board).

### `releasy setup-project`

If `notifications.github_project` is unset, create a project and print the URL
to add to config. Otherwise verify it: reconcile Status options (non-canonical
ones are dropped), create missing `AI Cost` / assignee fields, then sync cards.

### `releasy project push`

Reconcile the board with local state (add, update, remove cards). Exit `1` if
sync was skipped or any item failed.

### `releasy project pull`

Rebuild local state from GitHub and the board (e.g. on a new machine).
Merged into existing state; the board wins for `Skipped` and `AI Cost`.

## Release

### `releasy release`

`releasy release --base-tag <tag> --name <branch> [--strict] [--include-skipped] [--work-dir <path>]`

Create branch `--name` from `--base-tag` and merge every enabled port with
state that is not `conflict` (and not `skipped`, unless `--include-skipped`).
`--strict` aborts if any enabled port is in conflict, skipped, or has no
state.

### `releasy draft-release`

*Stateless with `--work-dir`.* Build release notes from PRs merged between
`--from` (exclusive) and `--to`, and create a **draft** GitHub release on
origin (or write markdown with `-o`). If `--from` is an ancestor of `--to`,
the first-parent PRs of the range are used; otherwise PRs into `--base` merged
in the date window. Forward-ports are dropped. Refs resolve remote-first.

| Option | Description | Default |
|--------|-------------|---------|
| `--from <ref>` | Lower bound, exclusive (required). | — |
| `--to <ref>` | Upper bound and release commitish (required). | — |
| `--base <branch>` | Branch for the date-window fallback. | `target_branch`, else `--to` |
| `--prs <url>` | Explicit PR (repeatable); skips discovery. | — |
| `--prs-file <path>` | File of PR URLs (`#` comments allowed). | — |
| `--name <tag>` | Release tag. | `--to` if it is a tag |
| `--title <text>` | Heading / display name. | prettified `--name` |
| `-o`, `--output <file>` | Write markdown instead of a draft release. | — |
| `--docker-image-url <url>` | Docker image link. | `sha256-TBD` placeholder |
| `--build-report-url <url>` | CI report link. | looked up, else `RUN-ID-TBD` |
| `--release-notes-url <url>` | Release-notes link. | derived for `antalya` / `stable` |
| `--work-dir <path>` | Clone to resolve refs; its remotes replace `config.yaml`. | config / cwd |

## Feature management

Edit the session's `features:` list.

| Command | Description |
|---------|-------------|
| `releasy feature add --id <id> --source-branch <branch> --description <text>` | Add a feature. |
| `releasy feature enable --id <id>` / `disable --id <id>` | Toggle `enabled`. |
| `releasy feature remove --id <id>` | Remove it (branches untouched). |
| `releasy feature list` | List features. |

## PR membership

Edit session PR lists without hand-editing YAML.

| Command | Description |
|---------|-------------|
| `releasy pr add <url> [--group <id>] [--context <text>]` | Add to `include_prs` (or a group's `prs`); removes it from `exclude_prs`. `--context` sets `ai_context`. |
| `releasy pr remove <url> [--keep-discovery]` | Remove from all lists and drop its singleton state. Adds it to `exclude_prs` unless `--keep-discovery`. An in-flight group port losing it is marked outdated. |
| `releasy pr list` | Show every URL in the session, with hold reasons and `ai_context`. |

Exit `1` on a malformed or unreachable URL, unknown group, or cross-list
collision.
