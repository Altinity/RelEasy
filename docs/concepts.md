# Concepts

Schema: [configuration.md](configuration.md). Commands: [commands.md](commands.md).

## The model

```
origin/antalya-26.3:          * (base branch — you maintain it)
                              |
feature/antalya-26.3/pr-42:   * --- fix   (PR → antalya-26.3)
feature/antalya-26.3/pr-99:   * --- feat  (PR → antalya-26.3)
```

Each PR (or group of PRs) to port becomes a **unit**: its own port branch off
the base, carrying the cherry-picked commits, opened as a rebase PR into the
base. RelEasy never creates or rewrites the base branch.

## Pipeline

`releasy run`:

1. Discovers PRs from the session's `pr_sources`.
2. Creates `feature/<base>/<id>` from `origin/<base>` per unit.
3. Cherry-picks the merge commit(s), AI-resolving conflicts if enabled.
4. Pushes and opens a PR into the base (`push: true` + `pr_policy.auto_pr`).

Units already having an open rebase PR are not rebuilt; `run` merges the base
into them instead (as [`refresh --merge-target`](commands.md#releasy-refresh)
does).

## Branch naming

Base = `target_branch`, or `<project>-<version>` with `<version>` parsed from
`--onto` (a naming label, never resolved as a git ref).

| Type | Pattern | Example |
|------|---------|---------|
| Base | `target_branch` or `<project>-<version>` | `antalya-26.3` |
| Feature / group | `feature/<base>/<id>` | `feature/antalya-26.3/s3-disk` |
| Origin PR | `feature/<base>/pr-<N>` | `feature/antalya-26.3/pr-42` |
| Cross-repo PR | `feature/<base>/<owner>-<repo>-pr-<N>` | `feature/antalya-26.3/ClickHouse-ClickHouse-pr-12345` |

## Files

| Path | Purpose |
|------|---------|
| `config.yaml` | Stable per-project settings. Scaffolded by `releasy new`. |
| `<config-dir>/<target_branch>.session.yaml` | What to port (`features:`, `pr_sources:`). Named after `name` when `target_branch` is unset; override with `session_file:` / `--session-file`. |
| `<session-stem>.deps.yaml` | Deps overlay written by `graph discover`, read by `run`. |
| `${XDG_STATE_HOME:-~/.local/state}/releasy/<name>.state.yaml` | Pipeline state. Managed by RelEasy. |
| `${XDG_STATE_HOME:-~/.local/state}/releasy/<name>.lock` | Per-project lock. |

`$RELEASY_STATE_DIR` overrides the state directory.

## Multiple projects

`name:` keys the state file and lock, so differently-named projects run
concurrently; same-named ones serialize. Use one `work_dir` per project.
[`releasy list`](commands.md#releasy-list) shows all projects. The state file
records its owning config path; after moving a config, run
[`releasy adopt`](commands.md#releasy-adopt).

## Statuses

| Status | Meaning |
|--------|---------|
| `needs_review` | Rebase PR open. |
| `branch_created` | Branch pushed, no PR yet. |
| `build_failed` | Resolution landed but build/tests failed; branch kept, retried next `run`. |
| `conflict` | Needs a human. |
| `blocked` | Waiting on `depends_on` units to merge. |
| `skipped` | Dropped by `releasy skip` or an empty cherry-pick. |
| `merged` | Rebase PR merged. |
| `closed` | Rebase PR closed unmerged. Terminal unless `pr_policy.recreate_closed_prs`. |
| `superseded` | Another commit/PR on the base already cherry-picks the source. Terminal; see `pr_policy.detect_superseded`. |
| `reverted` | Merged, then reverted on target (`releasy mark-reverted`). Terminal unless `pr_policy.recreate_reverted_prs`. |

AI-resolved ports also carry `ai_resolved` and the `ai-resolved` PR label.
Units in `pr_sources.on_hold` keep whatever status they had and are skipped by
`run` (see [on hold vs. excluded](configuration.md#on-hold-vs-excluded)).

## Conflicts

When AI resolution is off or gives up, the unit is marked `conflict` and the
pipeline moves on:

- **Singleton or first PR of a group** — cherry-pick aborted, local branch
  deleted, nothing pushed.
- **Later PR of a group** — earlier picks kept; branch pushed as a draft PR
  labelled `ai-needs-attention`. The next `run` resumes it, up to
  `pr_policy.max_partial_continue_attempts`.

| To… | Run |
|-----|-----|
| Re-attempt a unit after fixing its source | [`releasy run`](commands.md#releasy-run) |
| Mark a manually resolved port branch done | [`releasy continue`](commands.md#releasy-continue) |
| Resolve target drift on an open rebase PR | [`releasy refresh --merge-target`](commands.md#releasy-refresh) |
| Drop the unit | [`releasy skip`](commands.md#releasy-skip) |

Per-PR hints for the resolver:
[`ai_context`](configuration.md#per-pr--per-group-ai_context).

## Stall reasons

A **stall** records *why* a unit stopped (on its state entry, `stall:`), and is
cleared when the unit lands, merges, is skipped, or is resolved.

```yaml
stall:
  kind: waiting_for_merge
  waiting_on_units: [auto-grp-pr-1687]
  waiting_on_prs: [https://github.com/Altinity/ClickHouse/pull/1687]
  since: "2026-08-05T09:14:02+00:00"
  runs: 3
```

| Kind | Meaning | Skipped by `run`? |
|------|---------|-------------------|
| `waiting_for_merge` | A prerequisite is queued in another unit, or a `depends_on` gate is unmet. | yes, until that unit merges |
| `missing_prereq` | A prerequisite PR was found but nobody ports it. | yes, until the prereq's port merges |
| `retries_exhausted` | An attempt cap was spent. | the cap decides |
| `unresolvable` | The resolver could not fix the conflict. | after `ai_resolve.max_dead_end_attempts` runs |
| `prereq_search_exhausted` | The auto-prereq dive hit its depth cap, a cycle, or a fetch failure. | after `ai_resolve.max_dead_end_attempts` runs |
| `resolver_unavailable` | AI resolution off, or the backend died. | no |
| `build_unfixed` | Build/tests never went green. | no |

Force a retry with `run --ignore-stalls`, or disable skipping with
`pr_policy.honor_stall_reasons: false`. Stalls show in `releasy status`
(**Why** column), on the graph issue, on the board, and in draft PR banners.

## PR title & labels

Title: `"<Project> <version>: <subject>"`, e.g.
`"Antalya 26.3: Token Authentication and Authorization"`. The project is
title-cased only if all-lowercase; a leading `<version>[<project>]:` prefix on
the source title is stripped.

Every rebase PR gets the `releasy` label; AI-resolved ones also get
`ai_resolve.label`. See also `merged_label`, `pr_labels` and
`pr_labels_by_mode` in [configuration.md](configuration.md#key-options).

## Safety: PRs always target origin

Cross-repo PR URLs are read-only cherry-pick sources. RelEasy only creates,
updates or labels PRs in the `origin` repo and only pushes to that remote;
`run` prints the target repo on startup.
