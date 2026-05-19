# Configuration reference

Two YAML files: `config.yaml` (stable) and `<name>.session.yaml`
(per-effort). Templates in [`config.yaml.example`](../config.yaml.example)
and [`session.yaml.example`](../session.yaml.example). Conceptual model:
[concepts.md → files](concepts.md#files-releasy-reads--writes).

## config.yaml (stable infrastructure)

```yaml
# Unique slug for this project on this machine (required).
# Keys ${XDG_STATE_HOME:-~/.local/state}/releasy/<name>.state.yaml
# and (by default) <config-dir>/<name>.session.yaml.
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
```

## `<name>.session.yaml` (per-effort source data)

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

  exclude_labels: ["do-not-port"]
  exclude_authors: ["dependabot[bot]"]
  # include_authors: ["alice", "bob"]

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
```

If a PR URL appears in two of `include_prs` / `exclude_prs` / a group's
`prs`, you get a one-line stderr warning. The pipeline still resolves
deterministically (group wins over `include_prs`; `exclude_prs` is final).

## Key options

Options live in `config.yaml` unless marked **(session)**.

| Option | Description | Default |
|--------|-------------|---------|
| `name` | Project slug (required). Matches `[A-Za-z0-9._-]{1,64}`. | — |
| `session_file` | Override session file path. | `<config-dir>/<name>.session.yaml` |
| `push` | Push branches + open PRs. | `false` |
| `work_dir` | Repo clone path. | cwd |
| `origin.remote` | Origin repo URL (required). | — |
| `project` | Short project id used in branch names. | — |
| `target_branch` | Explicit base branch; makes `--onto` optional. | derived |
| `sequential` | One PR per invocation, gated on the previous rebase PR merging. See [Sequential mode](commands.md#sequential-mode). Incompatible with `pr_sources.groups`. | `false` |
| `update_existing_prs` | Reuse existing PR and overwrite its title/body. | `false` |
| `ai_resolve.max_iterations` | Build attempts per conflict (passed to Claude). | `5` |
| `ai_resolve.api_retries` | Retries on transient Anthropic API errors. | `3` |
| `ai_resolve.label` | Label for AI-resolved PRs. | `ai-resolved` |
| `ai_resolve.needs_attention_label` | Label for partial-group draft PRs. | `ai-needs-attention` |
| `ai_resolve.prompt_file` | Prompt for cherry-pick conflicts. | `prompts/resolve_conflict.md` |
| `ai_resolve.merge_prompt_file` | Prompt for merge conflicts (`refresh`). | `prompts/resolve_merge_conflict.md` |
| `ai_changelog.enabled` | Synthesize one CHANGELOG entry per multi-PR group. Singletons reuse the source PR's entry. | `false` |
| `ai_changelog.command` | Claude executable. | `claude` |
| `ai_changelog.prompt_file` | Prompt template. | `prompts/synthesize_changelog.md` |
| `ai_changelog.timeout_seconds` | Per-call timeout. | `300` |
| `ai_changelog.max_pr_body_chars` | Per-PR body trim before inlining. | `3000` |
| `review_response.trusted_reviewers` | Reviewer login allowlist. Combined with `--reviewer`. Empty both ⇒ command refuses. | `[]` |
| `review_response.reply_to_non_addressable` | In-thread reply on non-actionable comments. | `true` |
| `review_response.post_summary_comment` | Also post a top-level summary comment. | `false` |
| `review_response.prompt_file` | Prompt template. | `prompts/address_review.md` |
| `review_response.max_iterations` | Build-attempt cap. | `15` |
| `review_response.timeout_seconds` | Per-invocation Claude timeout. | `7200` |
| `analyze_fails.command` | Claude executable. | `claude` |
| `analyze_fails.prompt_file` | Prompt template. | `prompts/analyze_fails.md` |
| `analyze_fails.timeout_seconds` | Per-invocation Claude timeout. | `7200` |
| `analyze_fails.max_iterations` | Build attempts per failed test. | `6` |
| `analyze_fails.max_prs_per_run` | Cap on tracked PRs when `--pr` omitted (0 = no cap). | `0` |
| `analyze_fails.flaky_elsewhere_threshold` | Failure seen on this many other PRs ⇒ flagged as master-side flake. `0` disables. | `2` |
| `analyze_fails.flaky_check_prs` | Cap on PRs scanned for the flaky-elsewhere map. | `12` |
| `analyze_fails.post_comment_to_pr` | Post summary comment per PR. | `true` |
| `pr_policy.auto_pr` | Open a PR for every pushed port branch. Needs `push: true`. | `true` |
| `pr_policy.if_exists` | What to do with an existing port branch: `skip` (leave it) / `recreate` (rebuild from base — only if no rebase PR open yet) / `append` (cherry-pick declared PRs not yet on the branch). | `skip` |
| `pr_policy.retry_failed` | Revisit `conflict` entries per their `if_exists`. Override per-run with `--retry-failed`/`--no-retry-failed`. | `true` |
| `pr_policy.recreate_closed_prs` | If a rebase PR is closed (not merged), allocate `<canonical>-1`, `-2`, … and open a fresh one. The closed entry stays terminal until this flag opts it back in. | `false` |
| `pr_policy.detect_superseded` | Each refresh / run sweeps the target branch's recent git log AND open PRs targeting the same base for `(cherry picked from commit <sha>)` footers citing any tracked entry's source PR. Matches mark the entry `superseded` — terminal, no more retries. | `true` |
| `pr_sources.by_labels[].labels` **(session)** | Labels a PR must have (AND). | — |
| `pr_sources.by_labels[].merged_only` **(session)** | Only merged PRs. | `false` |
| `pr_sources.by_labels[].if_exists` **(session)** | Override `pr_policy.if_exists`. | inherits |
| `pr_sources.by_labels[].ai_context` **(session)** | AI resolver hint applied to every matched PR. | `""` |
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
| `features[].id` **(session)** | Feature id → branch suffix. | — |
| `features[].source_branch` **(session)** | Branch holding the commits. | — |
| `features[].description` **(session)** | PR title + board text. | — |
| `features[].enabled` **(session)** | Active on next run. | `true` |
| `features[].depends_on` **(session)** | Feature ids that must port first. | `[]` |
| `features[].ai_context` **(session)** | Hint on porting conflicts. | `""` |

## Environment variables

| Variable | Purpose |
|----------|---------|
| `RELEASY_GITHUB_TOKEN` | GitHub PAT — PR discovery, PR creation, Project sync. |
| `RELEASY_SSH_KEY_PATH` | SSH key for git. Optional; defaults to agent. |
| `RELEASY_STATE_DIR` | Override state + lock dir. Default: `${XDG_STATE_HOME:-~/.local/state}/releasy`. |

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
   `Branch Created`, `Conflict`, `Skipped`.
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
