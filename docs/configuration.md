# Configuration reference

Two YAML files per project:

- `config.yaml` — stable settings. Annotated template:
  [`config.yaml.example`](../config.yaml.example).
- `<target_branch>.session.yaml` — what to port. Annotated template:
  [`session.yaml.example`](../session.yaml.example).

File locations: [concepts.md → Files](concepts.md#files).

## Session: PR selection

```
union(by_labels) − exclude_labels − exclude_authors
∩ (include_authors when set)
+ include_prs − exclude_prs − on_hold
```

- `include_prs` bypasses label and author filters. URLs may point to any
  public GitHub repo.
- Every PR in a `groups[]` entry is ported in that group (one branch, one
  combined PR) regardless of labels; exclusions still drop individual members.
- PR URLs match by full owner/repo/number.
- A URL listed in two of `include_prs` / `exclude_prs` / a group's `prs`
  warns; group wins over `include_prs`, `exclude_prs` always wins.

### On hold vs. excluded

`exclude_prs` is a veto: the PR leaves the graph. `on_hold` is a pause: the
unit keeps its dependency edges and any existing port PR, `run` skips it, and
units that `depends_on` it report `blocked`. Holding one PR of a group holds
the whole group.

Change holds with [`releasy hold`](commands.md#releasy-hold) /
[`unhold`](commands.md#releasy-unhold), a graph-issue comment +
[`graph update`](commands.md#releasy-graph-update), or by editing
`pr_sources.on_hold`.

## Key options

`config.yaml` unless marked **(session)**.

### General

| Option | Description | Default |
|--------|-------------|---------|
| `name` | Project slug (required), `[A-Za-z0-9._-]{1,64}`. Keys state file + lock. | — |
| `project` | Short id used in branch names and PR titles (required). | — |
| `origin.remote` | Origin repo URL (required). | — |
| `origin.remote_name` | Git remote alias. | `origin` |
| `target_branch` | Base branch; makes `--onto` optional. Must exist on origin. | derived from `--onto` |
| `session_file` | Session file path (relative to the config dir). `--session-file` wins. | `<config-dir>/<target_branch>.session.yaml` |
| `work_dir` | Local clone used for git operations. | cwd |
| `log_file` | Append a full transcript plus INFO logs here (relative to the config dir). | unset |
| `push` | Push branches and open PRs. | `false` |
| `sequential` | One PR per `run` / `continue`, gated on the previous one merging. See [Sequential mode](commands.md#sequential-mode). | `false` |
| `update_existing_prs` | Overwrite title/body of an existing rebase PR. | `false` |
| `merged_label` | Label added to a rebase PR when it merges and removed from its origin-repo source PRs. Needs `push: true`. | unset |
| `merged_label_color` | Color used when creating `merged_label`. | `8B5CF6` |
| `upstream.remote` | Fetch-only upstream URL, used for prerequisite detection. Also `upstream.remote_name` (`upstream`), `upstream.branch` (`master`). | unset |

### `pr_policy`

| Option | Description | Default |
|--------|-------------|---------|
| `auto_pr` | Open a PR for every pushed port branch. | `true` |
| `if_exists` | Existing port branch without a rebase PR: `skip`, `recreate` (rebuild from base) or `append` (cherry-pick missing declared PRs on top). A branch with an open rebase PR is never rebuilt; a group with an open PR always gets new members appended. | `skip` |
| `retry_failed` | Re-process `conflict` entries per `if_exists`. CLI: `--retry-failed` / `--no-retry-failed`. | `true` |
| `recreate_closed_prs` | Re-port a closed (unmerged) rebase PR on a renumbered branch (`<id>-1`, `-2`, …). | `false` |
| `recreate_reverted_prs` | Same, for entries marked [`reverted`](commands.md#releasy-mark-reverted). | `false` |
| `detect_superseded` | Mark an entry `superseded` when the target's history or an open PR into the base carries `(cherry picked from commit …)` for its source. | `true` |
| `max_partial_continue_attempts` | Times `run` resumes a partially applied group. `0` disables. | `2` |
| `honor_stall_reasons` | Skip units whose [stall](concepts.md#stall-reasons) can't clear on its own. CLI: `run --ignore-stalls`. | `true` |

### AI (shared)

| Option | Description | Default |
|--------|-------------|---------|
| `ai_backend` | `cli`, `codex` or `api`. See [AI backends](#ai-backends). | `cli` |
| `ai_model` | Model for every AI call (alias or full id). Ignored by `codex`. | CLI default |
| `ai_effort` | `low` / `medium` / `high` / `xhigh` / `max`. Ignored by `codex`. | CLI default |

Each AI section (`ai_resolve`, `review_response`, `analyze_fails`, `graph`,
`ai_changelog`) has `command` (default `claude`), `prompt_file` and
`timeout_seconds`. `ai_resolve`, `review_response` and `analyze_fails` also
take `allowed_tools` (Claude Code tool allowlist) and, with `graph`,
`extra_args` (extra CLI flags). Relative prompt paths resolve against the
config dir, falling back to the prompts bundled with releasy.

### `ai_resolve`

| Option | Description | Default |
|--------|-------------|---------|
| `enabled` | Use AI to resolve conflicts. | `false` |
| `timeout_seconds` | Per-invocation timeout. | `7200` |
| `build_command` | Build command. | `cd build && ninja` |
| `deterministic_build` | AI resolves only; RelEasy builds and runs the PR's tests, looping fresh-context build fixes. `false` = single AI session resolves and builds. | `true` |
| `max_build_attempts` | Build-fix attempts per run before parking as `build_failed`. | `5` |
| `max_verify_resume_attempts` | Runs that resume a `build_failed` branch. `0` disables. | `2` |
| `max_resume_base_drift` | Re-port instead of resuming when the branch is this many commits behind base. `0` disables. | `50` |
| `max_verify_iterations` | Build↔test iterations per verify pass. | `12` |
| `build_log_tail_lines` | Build-log lines fed to the fix-build prompt. | `500` |
| `build_timeout_seconds` | Timeout for one build. | `7200` |
| `run_pr_tests` | Run the source PR's tests after a green build. | `true` |
| `test_file_globs` | Globs that mark a changed file as a test. | ClickHouse test paths |
| `test_timeout_seconds` | Timeout for one test run. | `3600` |
| `max_iterations` | Build attempts per conflict when `deterministic_build: false`. | `5` |
| `max_dead_end_attempts` | Runs that may re-resolve a unit stalled `unresolvable` / `prereq_search_exhausted`. `0` = no cap. | `2` |
| `split_conflict_commit` | Commit the raw conflict and its resolution separately. | `true` |
| `prompt_file` / `merge_prompt_file` / `split_prompt_file` | Prompts for cherry-pick, merge (`refresh`) and split-commit resolution. | `prompts/resolve_conflict.md` / `prompts/resolve_merge_conflict.md` / `prompts/resolve_conflict_split.md` |
| `resolve_only_prompt_file` / `fix_build_prompt_file` / `run_tests_prompt_file` | Prompts for the deterministic flow. | `prompts/resolve_conflict_nobuild.md` / `prompts/fix_build.md` / `prompts/run_tests.md` |
| `api_retries` / `api_retry_backoff_seconds` | Retries on transient API errors. | `3` / `15` |
| `wait_on_session_exhaustion` | On a usage-limit message, wait and re-prompt instead of failing (all AI calls). | `true` |
| `session_exhaustion_max_wait_hours` | Total wait cap. | `60` |
| `session_exhaustion_poll_minutes` | Interval between re-prompts. | `30` |
| `session_exhaustion_extra_patterns` | Extra regexes recognised as a limit message. | `[]` |
| `postcondition_retries` | AI passes to fix a failed content postcondition (`SettingsChangesHistory.cpp` whitelist). | `2` |
| `warn_on_unfixed_postconditions` | When still failing: keep and flag the resolution (`true`) or discard it (`false`). | `true` |
| `auto_add_prerequisite_prs` | Auto-add a detected missing prerequisite PR. Bool, or `{enabled, max_prereq_depth, require_origin_prereq_label}`. | `false`, `7`, `true` |
| `verify_resolution` | Read-only AI audit of each resolution; findings → `verify_label` + comment. | `false` |
| `verify_prompt_file` / `verify_timeout_seconds` | Audit prompt and timeout. | `prompts/verify_resolution.md` / `1800` |
| `label` / `label_color` | Label on AI-resolved PRs. | `ai-resolved` / `8B5CF6` |
| `needs_attention_label` / `_color` | Label on partial-group draft PRs. | `ai-needs-attention` / `D93F0B` |
| `missing_prereqs_label` / `_color` | Label on PRs whose conflict is a missing prerequisite. | `missing-prerequisites` / `E4E669` |
| `auto_prereq_label` / `_color` | Label when a prerequisite was auto-added. | `auto-prereq-added` / `0E8A16` |
| `verify_label` / `_color` | Label when the audit finds issues. | `ai-needs-verify` / `FBCA04` |

`require_origin_prereq_label`: an origin-repo prerequisite (for an origin PR)
must match a `by_labels` entry to be auto-added.

### `ai_changelog`

| Option | Description | Default |
|--------|-------------|---------|
| `enabled` | AI-synthesize one changelog entry per multi-PR group (singletons reuse the source entry). | `false` |
| `prompt_file` | Prompt. | `prompts/synthesize_changelog.md` |
| `timeout_seconds` | Timeout. | `300` |
| `max_pr_body_chars` | Per-PR body trim. | `3000` |

### `review_response` (`refresh --address-review`)

| Option | Description | Default |
|--------|-------------|---------|
| `trusted_associations` | `author_association` values whose comments the AI acts on. | `[OWNER, MEMBER, COLLABORATOR, CONTRIBUTOR]` |
| `trusted_reviewers` | Extra trusted logins (case-insensitive). | `[]` |
| `reply_to_non_addressable` | Reply in-thread on non-actionable comments. | `true` |
| `post_summary_comment` | Also post a top-level summary. | `false` |
| `prompt_file` | Prompt. | `prompts/address_review.md` |
| `max_iterations` | Build attempts. | `15` |
| `timeout_seconds` | Timeout. | `7200` |

### `analyze_fails`

| Option | Description | Default |
|--------|-------------|---------|
| `categories` | Check categories to investigate (`fasttest`, `quick_functional`, `stateless`, `integration`, `regression`, `other`); empty = all. | `[]` |
| `job_level_failures` | Investigate failed checks without per-test results (build, packaging, killed jobs); off = warn only. | `true` |
| `baseline_check` | Compare against the last target-branch CI run predating the PR. | `true` |
| `baseline_scan_commits` | Commits to walk back looking for that run. | `25` |
| `verify_outcome` | Audit in-doubt shard outcomes with a second read-only session. | `true` |
| `max_investigation_rounds` | Investigator rounds per shard after a disputed audit. `1` = no redo. | `2` |
| `verify_prompt_file` / `verify_timeout_seconds` | Audit prompt and timeout. | `prompts/verify_analysis.md` / `1800` |
| `verify_label` / `verify_label_color` | Label when a dispute remains. | `ai-needs-verify` / `FBCA04` |
| `prompt_file` | Prompt. | `prompts/analyze_fails.md` |
| `timeout_seconds` | Timeout. | `7200` |
| `max_iterations` | Build attempts per failed test. | `6` |
| `max_prs_per_run` | Cap on tracked PRs when `--pr` is omitted (`0` = none). | `0` |
| `flaky_elsewhere_threshold` | Failing on this many other PRs ⇒ treated as a flake. `0` disables. | `2` |
| `flaky_check_prs` | PRs scanned for that map. | `12` |
| `post_comment_to_pr` | Post a summary comment per PR. | `true` |

`allowed_tools` entries may use `{work_dir}` (aliases `{repo_dir}`, `{cwd}`),
e.g. `Bash({work_dir}/build/programs/clickhouse:*)`.

### `graph`

| Option | Description | Default |
|--------|-------------|---------|
| `trusted_associations` | Associations whose issue comments `graph update` reads. | `[OWNER, MEMBER, COLLABORATOR]` |
| `trusted_reviewers` | Extra trusted logins. | `[]` |
| `issue_labels` | Graph-issue labels (target-branch name always added). | `[releasy]` |
| `post_comment` | Summary comment after `graph update`. | `true` |
| `sync_progress` | Run `graph sync` at the end of `run` / `refresh`. | `true` |
| `apply_exclusions` | Add vetoed PRs to `exclude_prs`. | `true` |
| `minimize_addressed_comments` | Collapse addressed comments as Outdated. | `true` |
| `prompt_file` | Prompt. | `prompts/adjust_graph.md` |
| `timeout_seconds` | Timeout. | `7200` |

### `notifications`

| Option | Description | Default |
|--------|-------------|---------|
| `github_project` | GitHub Projects v2 URL to sync. See [board](#github-project-board). | unset |
| `assignee_dev_options` / `assignee_qa_options` | Options for the `Assignee Dev` / `Assignee QA` fields when first created. | built-in list |
| `assignee_dev_login_map` | GitHub login → `Assignee Dev` option, seeded from the source PR author. | built-in map |

### Session file

| Option | Description | Default |
|--------|-------------|---------|
| `features[]` | Static branches: `id`, `source_branch`, `description`, `enabled` (`true`), `depends_on`, `ai_context`. Managed by [`releasy feature`](commands.md#feature-management). | `[]` |
| `pr_sources.by_labels[]` | `labels` (all required), `merged_only` (`false`), `description` (PR title prefix), `if_exists`, `ai_context`, `mode`. | `[]` |
| `pr_sources.exclude_labels` | Drop PRs with any of these labels. | `[]` |
| `pr_sources.include_authors` / `exclude_authors` | Author allow/deny lists (case-insensitive). | `[]` |
| `pr_sources.include_prs` | Always include. URL or `{url, ai_context}`. | `[]` |
| `pr_sources.exclude_prs` | Always exclude. | `[]` |
| `pr_sources.on_hold` | Park. URL or `{url, reason}`. | `[]` |
| `pr_sources.groups[]` | `id` (branch name), `prs` (URL or `{url, ai_context}`), `description` (PR title), `sort` (`listed` / `merged_at`), `depends_on` (unit ids that must merge first), `if_exists`, `ai_context`, `mode`. | `[]` |
| `pr_sources.forward_port_labels` | Labels marking a PR as a forward-port. | `[]` |
| `pr_sources.deps_file` | Deps overlay path (relative to the session file). | `<session-stem>.deps.yaml` |
| `pr_labels` | Labels added to every rebase PR of this session. | `[]` |
| `pr_labels_by_mode` | Extra labels per port mode (`backport` / `forward_port`). | `{}` |

`mode` (`auto` / `backport` / `forward_port`): `backport` lets the resolver
adapt code and declare a prerequisite only when unavoidable; `forward_port` is
strict. `auto` picks `forward_port` for PRs with a `forward_port_labels` label,
`backport` for cross-repo PRs or when `upstream` is set, else `forward_port`.

## AI backends

`ai_backend` selects how every AI call reaches the model:

- **`cli`** (default) — runs each section's `command` (`claude -p`) with its
  own login. `extra_args` are passed through.
- **`codex`** — runs `codex exec --json` with its own login. `command`,
  `extra_args`, `ai_model` and `ai_effort` are ignored. Calls without
  `Edit`/`Write` in `allowed_tools` run `--sandbox read-only`; others run
  `--dangerously-bypass-approvals-and-sandbox`. Cost is not reported.
- **`api`** — calls the Anthropic API directly with a token
  (`export ANTHROPIC_API_KEY=...`); RelEasy executes the tools (`Bash`, `Read`,
  `Write`, `Edit`, `Glob`, `Grep`, plus server-side `WebSearch` / `WebFetch`
  when allowed). `allowed_tools` is enforced locally; `command` and
  `extra_args` are ignored.

| Option | Description | Default |
|--------|-------------|---------|
| `ai_codex.command` | Codex executable. | `codex` |
| `ai_codex.model` | `--model`. | codex default |
| `ai_codex.reasoning_effort` | `-c model_reasoning_effort=…`. | codex default |
| `ai_codex.extra_args` | Extra `codex exec` flags. | `[]` |
| `ai_api.api_key_env` | Env var holding the token (checked before `api_key`). | `ANTHROPIC_API_KEY` |
| `ai_api.api_key` | Inline token. | unset |
| `ai_api.base_url` | Gateway / proxy URL. | Anthropic API |
| `ai_api.model` | Model id; overrides `ai_model`. | `claude-opus-5` |
| `ai_api.max_tokens` | Output cap per response. | `64000` |
| `ai_api.max_turns` | Model round-trips per invocation. | `300` |
| `ai_api.thinking` | Adaptive extended thinking. | `true` |
| `ai_api.max_retries` | SDK retries on 429/5xx. | `5` |
| `ai_api.request_timeout_seconds` | Per-request timeout. | `1800` |
| `ai_api.bash_timeout_seconds` | Per `Bash` call timeout. | `3600` |
| `ai_api.tool_output_max_chars` | Tool output truncation. | `30000` |
| `ai_api.system_prompt_extra` | Appended to the system prompt. | `""` |

`--ai-backend` overrides `ai_backend` on `refresh` and `analyze-fails`
(`cli|codex|api`), and on `cherry-pick` and `project-backport` (`cli|api`).

## Environment variables

| Variable | Purpose |
|----------|---------|
| `RELEASY_GITHUB_TOKEN` | GitHub token (`repo`; plus `project` for board sync). Required. |
| `RELEASY_SSH_KEY_PATH` | SSH key for git. Optional. |
| `RELEASY_STATE_DIR` | State + lock directory. Default `${XDG_STATE_HOME:-~/.local/state}/releasy`. |
| `ANTHROPIC_API_KEY` | Token for `ai_backend: api` (rename via `ai_api.api_key_env`). |

## Per-PR / per-group `ai_context`

A free-form note passed to the AI resolver only when that unit conflicts.
Accepted on `features[]`, `by_labels[]`, `groups[]`, and dict-form entries in
`include_prs[]` and `groups[].prs[]` (added to the group's note).

```yaml
pr_sources:
  include_prs:
    - url: https://github.com/Altinity/ClickHouse/pull/200
      ai_context: Base renamed `Foo::run` to `Foo::execute`. Adapt the call sites.
```

## GitHub Project board

Syncs unit status to a Projects v2 board (only with `push: true`).

Setup: run [`releasy setup-project`](commands.md#releasy-setup-project), or
create a table project by hand with Status options `Needs Review`,
`Branch Created`, `Conflict`, `Blocked`, `Skipped`, `Merged`, `Closed`,
`Superseded`, `Reverted`, then set:

```yaml
notifications:
  github_project: https://github.com/orgs/Altinity/projects/1
```

The token needs the `project` scope. RelEasy owns the Status field:
non-canonical options are dropped.

Per base branch it maintains a view; per unit a card with Status, `AI Cost`
(USD), `Assignee Dev` (seeded once from the source PR author via
`assignee_dev_login_map`) and `Assignee QA` (left empty). Assignee option
lists are only set when the field is created; edit them in GitHub afterwards.

Views can't be configured via the API — set *Group by Status* and show the
`AI Cost` / `Assignee Dev` / `Assignee QA` fields manually.
