# Configuration reference

Two YAML files: `config.yaml` (stable) and `<target_branch>.session.yaml`
(per-effort). Templates in [`config.yaml.example`](../config.yaml.example)
and [`session.yaml.example`](../session.yaml.example). Conceptual model:
[concepts.md → files](concepts.md#files-releasy-reads--writes).

## config.yaml (stable infrastructure)

```yaml
# Unique slug for this project on this machine (required).
# Keys ${XDG_STATE_HOME:-~/.local/state}/releasy/<name>.state.yaml
# (and the session file when target_branch is unset).
name: antalya-26.3

# Optional: override session file path. Relative paths resolve against
# this config's directory. CLI --session-file always wins.
# session_file: sessions/antalya-26.3.session.yaml

push: true                          # push branches + open PRs (default: false)
work_dir: /path/to/ClickHouse       # existing local clone (default: cwd)
project: antalya                    # used in derived branch names

origin:
  remote: https://github.com/Altinity/ClickHouse.git

target_branch: antalya-26.3         # when set, --onto becomes optional

# Optional: stamp this label on a rebase PR when it merges into target,
# and strip the same label from each source PR it ported. Cross-repo
# source PRs are skipped (releasy never writes outside origin).
# merged_label: port-antalya
# merged_label_color: "8B5CF6"      # used only when creating the label

# pr_policy:                         # all optional — defaults shown
#   if_exists: skip                  # skip | recreate | append
#   auto_pr: true
#   retry_failed: true
#   recreate_closed_prs: false
#   detect_superseded: true
```

## `<target_branch>.session.yaml` (per-effort source data)

```yaml
features:
  - id: s3-disk
    description: "Custom S3 disk improvements"
    source_branch: feature/antalya-s3-disk

# Set arithmetic:
#   union(by_labels) − exclude_labels − exclude_authors
#   ∩ (include_authors when set)
#   + include_prs − exclude_prs
# include_prs bypasses label & author filters.
pr_sources:
  by_labels:
    - labels: ["forward-port", "v26.3"]
      merged_only: true
      # mode: auto    # auto (default) | backport | forward_port

  exclude_labels: ["do-not-port"]
  exclude_authors: ["dependabot[bot]"]
  # include_authors: ["alice", "bob"]
  # forward_port_labels: ["forward-port"]   # treat these PRs as forward-ports

  include_prs:
    - https://github.com/Altinity/ClickHouse/pull/123
    - https://github.com/ClickHouse/ClickHouse/pull/12345   # cross-repo OK

  exclude_prs:
    - https://github.com/Altinity/ClickHouse/pull/789

  # Cherry-pick multiple PRs onto ONE branch, open ONE combined PR.
  # sort: listed (default, walks `prs:`) | merged_at
  # depends_on: other unit IDs that must merge first
  groups:
    - id: iceberg-rest
      description: "Iceberg REST catalog support"
      # depends_on: [pr-100, some-other-group-id]
      prs:
        - https://github.com/Altinity/ClickHouse/pull/1500
        - https://github.com/Altinity/ClickHouse/pull/1512

  # Optional: override deps overlay path (default <session-stem>.deps.yaml)
  # deps_file: deps/26.3.yaml

# Labels applied to every rebase PR opened this session (auto-created on
# origin; `refresh` reconciles them onto tracked PRs that are missing one).
# pr_labels: ["antalya-26.3"]

# Extra labels applied only to ports of a given mode (see `mode` below).
# pr_labels_by_mode:
#   forward_port: ["forwardport"]
```

If a PR URL appears in two of `include_prs` / `exclude_prs` / a group's
`prs`, you get a one-line stderr warning. The pipeline still resolves
deterministically (group wins over `include_prs`; `exclude_prs` is final).

## Key options

Options live in `config.yaml` unless marked **(session)**.

| Option | Description | Default |
|--------|-------------|---------|
| `name` | Project slug (required). Matches `[A-Za-z0-9._-]{1,64}`. | — |
| `session_file` | Override session file path. | `<config-dir>/<target_branch>.session.yaml` (or `<name>` when unset) |
| `push` | Push branches + open PRs. | `false` |
| `work_dir` | Repo clone path. | cwd |
| `origin.remote` | Origin repo URL (required). | — |
| `project` | Short project id used in branch names. | — |
| `target_branch` | Explicit base branch; makes `--onto` optional. | derived |
| `sequential` | One PR per invocation, gated on the previous rebase PR merging. See [Sequential mode](commands.md#sequential-mode). Incompatible with `pr_sources.groups`. | `false` |
| `update_existing_prs` | Reuse existing PR and overwrite its title/body. | `false` |
| `upstream.remote` | Optional fetch-only upstream remote (URL). Used **only** for `git log -S` prereq detection during AI resolve — never pushed to, never read for code. Sub-keys `upstream.remote_name` (`upstream`), `upstream.branch` (`master`). | unset |
| `ai_model` | Model for **every** AI call (resolve, changelog, review, analyze-fails, graph). Alias or full id (`opus`, `sonnet`, `claude-opus-4-8`). | claude CLI default |
| `ai_effort` | Reasoning effort for every AI call. One of `low`/`medium`/`high`/`xhigh`/`max`. | claude CLI default |
| `ai_backend` | How every AI call reaches the model: `cli` spawns the agent binary (`<section>.command`), `api` talks to the Anthropic API with a token. See [AI backends](#ai-backends). | `cli` |
| `ai_api.*` | Settings for `ai_backend: api`. See [AI backends](#ai-backends). | — |
| `ai_resolve.enabled` | Master switch for the AI conflict resolver. When off, conflicts always stop the pipeline. | `false` |
| `ai_resolve.build_command` | Shell command for the build. RelEasy runs it (deterministic flow), or Claude runs it (legacy). | `cd build && ninja` |
| `ai_resolve.deterministic_build` | Claude resolves only; RelEasy builds + runs the PR's tests, looping fresh-context build fixes. `false` = legacy single-session resolve+build. | `true` |
| `ai_resolve.max_build_attempts` | Consecutive build-fix attempts per run before parking as `build_failed`. Resets each run. | `5` |
| `ai_resolve.max_verify_resume_attempts` | How many times a `build_failed` branch is resumed on later runs before it's left for a human. `0` disables resume. | `2` |
| `ai_resolve.max_resume_base_drift` | Re-port from base instead of resuming when a parked branch is this many commits behind base. `0` disables the check. | `50` |
| `ai_resolve.max_verify_iterations` | Overall cap on build↔test iterations within one verify pass. | `12` |
| `ai_resolve.build_log_tail_lines` | Lines of `.releasy/build.log` fed to the fix-build prompt (plus grepped errors). | `500` |
| `ai_resolve.build_timeout_seconds` | RelEasy's wall-clock cap for one build subprocess. | `7200` |
| `ai_resolve.run_pr_tests` | After a green build, run the source PR's own tests (Claude-driven). | `true` |
| `ai_resolve.test_file_globs` | Globs marking a changed file as a runnable test. | ClickHouse defaults |
| `ai_resolve.test_timeout_seconds` | Wall-clock cap for one run-tests invocation. | `3600` |
| `ai_resolve.max_iterations` | Legacy build attempts per conflict (only when `deterministic_build: false`). | `5` |
| `ai_resolve.api_retries` | Retries on transient Anthropic API errors (short backoff). | `3` |
| `ai_resolve.wait_on_session_exhaustion` | When the Claude session usage limit is hit (incl. the CLI's misleadingly-worded "monthly spend limit · /usage-credits" message — it's a session reset, not a billing cap), wait and re-prompt on a schedule instead of failing. Applies to every Claude call; Ctrl-C aborts. | `true` |
| `ai_resolve.session_exhaustion_max_wait_hours` | Cap on cumulative waiting for the session to reset. | `60` |
| `ai_resolve.session_exhaustion_poll_minutes` | Sleep between re-prompts while waiting. | `30` |
| `ai_resolve.session_exhaustion_extra_patterns` | Extra regexes (OR-ed with the built-ins) for recognising a limit message, for a CLI wording the defaults miss. | `[]` |
| `ai_resolve.label` | Label for AI-resolved PRs. | `ai-resolved` |
| `ai_resolve.needs_attention_label` | Label for partial-group draft PRs. | `ai-needs-attention` |
| `ai_resolve.prompt_file` | Prompt for cherry-pick conflicts. | `prompts/resolve_conflict.md` |
| `ai_resolve.merge_prompt_file` | Prompt for merge conflicts (`refresh`). | `prompts/resolve_merge_conflict.md` |
| `ai_resolve.split_conflict_commit` | Record the raw conflict and its resolution as two separate commits (clearer history). | `true` |
| `ai_resolve.split_prompt_file` | Prompt used for the split-commit resolution pass. | `prompts/resolve_conflict_split.md` |
| `ai_resolve.auto_add_prerequisite_prs` | Auto-pull a missing prerequisite PR when the resolver detects one. Bool sugar, or `{enabled, max_prereq_depth}`. | `enabled: false`, `max_prereq_depth: 7` |
| `ai_changelog.enabled` | Synthesize one CHANGELOG entry per multi-PR group. Singletons reuse the source PR's entry. | `false` |
| `ai_changelog.command` | Claude executable. | `claude` |
| `ai_changelog.prompt_file` | Prompt template. | `prompts/synthesize_changelog.md` |
| `ai_changelog.timeout_seconds` | Per-call timeout. | `300` |
| `ai_changelog.max_pr_body_chars` | Per-PR body trim before inlining. | `3000` |
| `review_response.trusted_associations` | GitHub `author_association` values whose comments the AI is allowed to act on. The default gate handles the common case on its own. | `["OWNER", "MEMBER", "COLLABORATOR", "CONTRIBUTOR"]` |
| `review_response.trusted_reviewers` | Extra GitHub-login allowlist, additive on top of `trusted_associations` (case-insensitive). Combined with `--reviewer`. Empty is fine. | `[]` |
| `review_response.reply_to_non_addressable` | In-thread reply on non-actionable comments. | `true` |
| `review_response.post_summary_comment` | Also post a top-level summary comment. | `false` |
| `review_response.prompt_file` | Prompt template. | `prompts/address_review.md` |
| `review_response.max_iterations` | Build-attempt cap. | `15` |
| `review_response.timeout_seconds` | Per-invocation Claude timeout. | `7200` |
| `analyze_fails.command` | Claude executable. | `claude` |
| `analyze_fails.prompt_file` | Prompt template. | `prompts/analyze_fails.md` |
| `analyze_fails.categories` | Check categories to investigate; empty = every failed check. Known: `fasttest`, `quick_functional`, `stateless`, `integration`, `regression`, `other`. | `[]` |
| `analyze_fails.timeout_seconds` | Per-invocation Claude timeout. | `7200` |
| `analyze_fails.max_iterations` | Build attempts per failed test. | `6` |
| `analyze_fails.max_prs_per_run` | Cap on tracked PRs when `--pr` omitted (0 = no cap). | `0` |
| `analyze_fails.flaky_elsewhere_threshold` | Failure seen on this many other PRs ⇒ flagged as master-side flake. `0` disables. | `2` |
| `analyze_fails.flaky_check_prs` | Cap on PRs scanned for the flaky-elsewhere map. | `12` |
| `analyze_fails.post_comment_to_pr` | Post summary comment per PR. | `true` |
| `graph.trusted_associations` | GitHub `author_association` values whose comments `graph update` feeds to Claude. | `["OWNER", "MEMBER", "COLLABORATOR"]` |
| `graph.trusted_reviewers` | Extra GitHub-login allowlist, additive on top of `trusted_associations` (case-insensitive). | `[]` |
| `graph.issue_labels` | Labels on the graph issue (the target-branch name is always added too; created on origin if missing). | `["releasy"]` |
| `graph.post_comment` | Post a summary comment on the issue after each `graph update`. | `true` |
| `graph.sync_progress` | Refresh the graph issue's progress checkboxes at the end of `run` / `refresh` (same as `releasy graph sync`). | `true` |
| `graph.apply_exclusions` | Enforce member "don't port" vetoes by adding the PR to the session's `exclude_prs`. | `true` |
| `graph.minimize_addressed_comments` | After an update, collapse (mark **Outdated**) the comments it actually addressed; unaddressed ones stay visible. | `true` |
| `graph.prompt_file` | Prompt template for `graph update`. | `prompts/adjust_graph.md` |
| `graph.timeout_seconds` | Per-invocation Claude timeout for `graph update`. | `7200` |
| `pr_policy.auto_pr` | Open a PR for every pushed port branch. Needs `push: true`. | `true` |
| `pr_policy.if_exists` | What to do with an existing port branch: `skip` (leave it) / `recreate` (rebuild from base — only if no rebase PR open yet) / `append` (cherry-pick declared PRs not yet on the branch). | `skip` |
| `pr_policy.retry_failed` | Revisit `conflict` entries per their `if_exists`. Override per-run with `--retry-failed`/`--no-retry-failed`. | `true` |
| `pr_policy.recreate_closed_prs` | If a rebase PR is closed (not merged), allocate `<canonical>-1`, `-2`, … and open a fresh one. The closed entry stays terminal until this flag opts it back in. | `false` |
| `pr_policy.detect_superseded` | Each refresh / run sweeps the target branch's recent git log AND open PRs targeting the same base for `(cherry picked from commit <sha>)` footers citing any tracked entry's source PR. Matches mark the entry `superseded` — terminal, no more retries. | `true` |
| `pr_policy.max_partial_continue_attempts` | How many times `run` auto-resumes a **partially-applied group** (a prior run landed some of the group's PRs, then a conflict — often an AI token/budget exhaustion — left a draft PR labelled `ai-needs-attention`). Each run appends the not-yet-applied PRs and re-resolves (no need to set `if_exists: append` by hand); after the cap it leaves the draft PR for manual help. Wins over `if_exists: recreate` — only terminal cases (closed PR, first-pick conflict) redo from base. `0` disables, restoring plain `if_exists` handling. | `2` |
| `pr_policy.honor_stall_reasons` | Skip a unit whose recorded [stall](concepts.md#stall-reasons) can't clear on its own — waiting for another unit's PR to merge, or on a prereq nobody ports. Re-resolving those reaches the same verdict at full token price; the stall is dropped (and the unit retried) as soon as what it waits on changes. Override per run with `run --ignore-stalls`. | `true` |
| `pr_sources.by_labels[].labels` **(session)** | Labels a PR must have (AND). | — |
| `pr_sources.by_labels[].merged_only` **(session)** | Only merged PRs. | `false` |
| `pr_sources.by_labels[].if_exists` **(session)** | Override `pr_policy.if_exists`. | inherits |
| `pr_sources.by_labels[].ai_context` **(session)** | AI resolver hint applied to every matched PR. | `""` |
| `pr_sources.by_labels[].mode` / `groups[].mode` **(session)** | Port direction: `auto` / `backport` / `forward_port`. | `auto` |
| `pr_sources.forward_port_labels` **(session)** | Labels that mark a PR as a forward-port. | `[]` |
| `pr_sources.deps_file` **(session)** | Override the deps overlay path. | `<session-stem>.deps.yaml` |
| `pr_sources.exclude_labels` **(session)** | Drop PRs with any of these. | `[]` |
| `pr_sources.include_authors` **(session)** | Allowlist of GitHub logins. Bypassed by `include_prs`. | `[]` |
| `pr_sources.exclude_authors` **(session)** | Denylist of GitHub logins. Bypassed by `include_prs`. | `[]` |
| `pr_sources.include_prs` **(session)** | Always include. Bare URL or `{url, ai_context}`. | `[]` |
| `pr_sources.exclude_prs` **(session)** | Always exclude. | `[]` |
| `pr_sources.groups[].id` **(session)** | Group id → branch name. | — |
| `pr_sources.groups[].prs` **(session)** | Ordered PR list. Bare URL or `{url, ai_context}`. | — |
| `pr_sources.groups[].description` **(session)** | Combined PR title. | id |
| `pr_sources.groups[].if_exists` **(session)** | Override. | inherits |
| `pr_sources.groups[].sort` **(session)** | `listed` or `merged_at` (PR number breaks ties). | `listed` |
| `pr_sources.groups[].ai_context` **(session)** | Hint for every cherry-pick step in the group. | `""` |
| `pr_sources.groups[].depends_on` **(session)** | Other unit IDs that must port/merge first. | `[]` |
| `pr_labels` **(session)** | Labels applied to every rebase PR opened this session (auto-created on origin). | `[]` |
| `pr_labels_by_mode` **(session)** | Extra labels per detected port mode (`forward_port` / `backport`). A PR of unknown mode gets `pr_labels` only. | `{}` |
| `features[].id` **(session)** | Feature id → branch suffix. | — |
| `features[].source_branch` **(session)** | Branch holding the commits. | — |
| `features[].description` **(session)** | PR title + board text. | — |
| `features[].enabled` **(session)** | Active on next run. | `true` |
| `features[].depends_on` **(session)** | Feature ids that must port first. | `[]` |
| `features[].ai_context` **(session)** | Hint on porting conflicts. | `""` |

## AI backends

Every AI call — conflict resolve, build fixes, run-tests, verify, review
response, analyze-fails, changelog synthesis, graph discovery — goes through
one of two backends, selected by `ai_backend`.

**`cli` (default)** spawns the agent binary named by the section's `command`
(`ai_resolve.command`, `analyze_fails.command`, …) as `claude -p
--output-format stream-json`. It uses whatever credentials that CLI is
logged in with (subscription or `ANTHROPIC_API_KEY`), and `extra_args` is
passed through to it.

**`api`** drops the subprocess: RelEasy talks to the Anthropic Messages API
with a token and runs the tool calls itself. The `anthropic` SDK ships as a
regular dependency, so all it needs is a token:

```sh
export ANTHROPIC_API_KEY=sk-ant-...
```

```yaml
ai_backend: api

ai_api:
  model: claude-opus-5      # falls back to ai_model, then claude-opus-5
  max_turns: 300            # cap on model round-trips per invocation
```

What carries over unchanged in API mode: `allowed_tools` (same
Claude-Code syntax — `Read`, `Bash(git:*)`; enforced locally, and unlike the
CLI compound commands are allowed as long as every segment's head is in the
list), `timeout_seconds`, `api_retries` / `api_retry_backoff_seconds`, the
session-exhaustion wait, per-run cost reporting, and every prompt template.
What is ignored: `command` and `extra_args` (no process is spawned).

Tools available to the model: `Bash`, `Read`, `Write`, `Edit`, `Glob`,
`Grep`, plus Anthropic's server-side `WebSearch` / `WebFetch` when the
section's `allowed_tools` grants them (`analyze_fails` does by default).

| Option | Description | Default |
|--------|-------------|---------|
| `ai_api.api_key_env` | Env var holding the token. Checked before `api_key`. | `ANTHROPIC_API_KEY` |
| `ai_api.api_key` | Inline token. Only used when the env var is unset — prefer the env var. | unset |
| `ai_api.base_url` | Gateway / proxy base URL. | Anthropic API |
| `ai_api.model` | Model id. Overrides `ai_model`. | `claude-opus-5` |
| `ai_api.max_tokens` | Output cap per model response. | `64000` |
| `ai_api.max_turns` | Hard cap on model round-trips per invocation. | `300` |
| `ai_api.thinking` | Adaptive extended thinking. | `true` |
| `ai_api.max_retries` | SDK-level retries for 429/5xx before the failure reaches RelEasy's own retry ladder. | `5` |
| `ai_api.request_timeout_seconds` | Per-request HTTP timeout. | `1800` |
| `ai_api.bash_timeout_seconds` | Default cap for one `Bash` tool call (also bounded by `timeout_seconds`). | `3600` |
| `ai_api.tool_output_max_chars` | Tool results are middle-truncated past this. | `30000` |
| `ai_api.system_prompt_extra` | Appended to the built-in system prompt. | `""` |

`--ai-backend cli|api` overrides `ai_backend` on `refresh`, `analyze-fails`,
`cherry-pick`, and `project-backport` (the last two have no config file, so
API mode there uses these defaults plus `$ANTHROPIC_API_KEY`).

## Environment variables

| Variable | Purpose |
|----------|---------|
| `RELEASY_GITHUB_TOKEN` | GitHub PAT — PR discovery, PR creation, Project sync. |
| `RELEASY_SSH_KEY_PATH` | SSH key for git. Optional; defaults to agent. |
| `RELEASY_STATE_DIR` | Override state + lock dir. Default: `${XDG_STATE_HOME:-~/.local/state}/releasy`. |
| `ANTHROPIC_API_KEY` | Anthropic token for `ai_backend: api` (rename via `ai_api.api_key_env`). |

## Per-PR / per-group `ai_context`

Free-form note passed to the AI conflict resolver under a *User-supplied
context* section — only invoked when this PR/group/feature actually
conflicts.

Supported on: `pr_sources.by_labels[].ai_context`,
`pr_sources.groups[].ai_context`, `pr_sources.groups[].prs[]` (dict form),
`pr_sources.include_prs[]` (dict form), `features[].ai_context`.

```yaml
pr_sources:
  include_prs:
    - https://github.com/Altinity/ClickHouse/pull/100    # bare URL
    - url: https://github.com/Altinity/ClickHouse/pull/200
      ai_context: |
        Base renamed `Foo::run` to `Foo::execute`. Adapt the call sites.

  groups:
    - id: iceberg-rest-catalog
      ai_context: |
        These PRs depend on the new IcebergCatalog interface on master.
      prs:
        - https://github.com/Altinity/ClickHouse/pull/1500
        - url: https://github.com/Altinity/ClickHouse/pull/1530
          ai_context: "Renames list_tables → list_namespaces."
```

The note complements the source PR's diff; it never overrides it.

## GitHub Project board

Sync branch status to a GitHub Projects v2 board. One-time UI setup, then
auto-maintained.

### Setup

1. **Create the project** at `https://github.com/orgs/<org>/projects` →
   New project → Table layout.
2. **Status field options** — set to exactly: `Needs Review`,
   `Branch Created`, `Conflict`, `Blocked`, `Skipped`, `Merged`,
   `Closed`, `Superseded`.
3. **Token permissions** — `RELEASY_GITHUB_TOKEN` needs `repo` + `project`
   scopes (classic) or "Projects" read/write (fine-grained).
4. **Wire into config:**

   ```yaml
   push: true   # project sync only runs when push is enabled
   notifications:
     github_project: https://github.com/orgs/Altinity/projects/1
   ```

Or skip the UI: [`releasy setup-project`](commands.md#releasy-setup-project)
creates the project, sets canonical Status options, provisions `AI Cost`,
runs an initial sync.

> **Destructive:** the Status field is fully owned by RelEasy. Non-canonical
> options (e.g. legacy `Ok` / `Resolved`) get dropped. To keep custom
> options, edit `STATUS_OPTIONS` in `src/releasy/github_ops.py`.

### What gets synced

After each state change (when `push: true`):

- A **view (tab)** per rebase, named after the base branch.
- Real PR attached (or draft-issue stub for `Branch Created`).
- **Status** matches local pipeline state.
- **AI Cost** (USD) — cumulative Anthropic spend across all Claude calls
  (resolve, refresh, analyze-fails); `0` for untouched cards.
- **Assignee Dev** seeded once with the source PR's author (via
  `notifications.assignee_dev_login_map`). Never overwritten.
- **Assignee QA** left empty; QA team fills in.
- Card body: base commit, conflict files, compare URL (when no PR yet).

One project, multiple views — each rebase gets its own tab automatically.

### View settings to flip on (once per view)

Projects v2 GraphQL doesn't expose view-config writes, so these are manual:

| Setting | Path | Why |
|---------|------|-----|
| Group by Status | ⋯ → Group → Status | Mirrors the [`releasy status`](commands.md#releasy-status) layout. |
| Show `AI Cost` column | ⋯ → Fields → toggle on | Field exists on every card but isn't auto-added to views. |
| Show `Assignee Dev` / `Assignee QA` | ⋯ → Fields → toggle on | Same limitation. |

Field option lists come from `notifications.assignee_dev_options` /
`assignee_qa_options`. On a fresh board, RelEasy provisions exactly those
options; on subsequent runs **never edits the option list** — manual
additions/removals stick. To add a team member: edit the option list in
GitHub, then add the login → label entry to `assignee_dev_login_map`.
