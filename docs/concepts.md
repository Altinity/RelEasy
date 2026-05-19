# Concepts

The mental model behind RelEasy. For the schema, see
[configuration.md](configuration.md); for commands, see
[commands.md](commands.md).

## The model

```
origin/antalya-26.3:          * (stable base branch on origin — you maintain it)
                              |
feature/antalya-26.3/pr-42:   * --- fix   (PR → antalya-26.3)
feature/antalya-26.3/pr-99:   * --- feat  (PR → antalya-26.3)
```

You maintain the **base branch**. Each PR you want to port becomes its own
**port branch** carrying the cherry-picked commits, opened as a rebase PR
back into the base. RelEasy never creates or rewrites the base.

## Pipeline

`releasy run` does:

1. Discover PRs from `pr_sources` in the session file.
2. For each PR / group, create `feature/<base>/<id>` from the base.
3. Cherry-pick the PR merge commit(s).
4. Push and open a PR into the base (when `push: true` and
   `pr_policy.auto_pr: true`).

On conflict: pipeline stops with instructions. Resolve, then
[`releasy continue`](commands.md#releasy-continue), then
[`releasy run`](commands.md#releasy-run) again.

The other commands fit around this loop — see the
[at-a-glance matrix](commands.md#at-a-glance-which-command-does-what).

## Branch naming

Base branch = `target_branch` from config, or derived
`<project>-<version>` (with `<version>` parsed from `--onto`).
`--onto` is a **naming label** — never resolved as a git ref.

| Type | Pattern | Example |
|------|---------|---------|
| Base | `target_branch` or `<project>-<version>` | `antalya-26.3` |
| Feature | `feature/<base>/<id>` | `feature/antalya-26.3/s3-disk` |
| Origin PR | `feature/<base>/pr-<N>` | `feature/antalya-26.3/pr-42` |
| External PR | `feature/<base>/<owner>-<repo>-pr-<N>` | `feature/antalya-26.3/ClickHouse-ClickHouse-pr-12345` |

## Multiple projects in parallel

Each `config.yaml` has a required `name:`. That name keys a state file +
lock under `${XDG_STATE_HOME:-~/.local/state}/releasy/` (override with
`$RELEASY_STATE_DIR`). Different-named projects run truly concurrently;
same-named projects serialize on the lock.

```bash
(cd ~/work/antalya-26.3 && releasy run) &
(cd ~/work/antalya-25.8 && releasy run) &
releasy list   # see every project on this machine
```

> One `work_dir` per project — git itself isn't safe with two processes
> mutating the same checkout.

Moving a `config.yaml` trips an ownership check. Fix with
[`releasy adopt`](commands.md#releasy-adopt) from the new location.

## Files RelEasy reads & writes

| Path | Purpose | Edited by |
|------|---------|-----------|
| `config.yaml` | Stable per-project config — origin, target branch, AI, notifications. [Schema](configuration.md#configyaml-stable-infrastructure). | You; scaffolded by `releasy new`. |
| `<config-dir>/<name>.session.yaml` | `features:` + `pr_sources:`. [Schema](configuration.md#namesessionyaml-per-effort-source-data). Override path with `session_file:` or `--session-file`. | You; mutated by [`releasy feature *`](commands.md#feature-management). |
| `${XDG_STATE_HOME:-~/.local/state}/releasy/<name>.state.yaml` | Pipeline state (phase, branches, statuses, AI cost). | Auto. Not user-editable. |
| `${XDG_STATE_HOME:-~/.local/state}/releasy/<name>.lock` | POSIX advisory lock. | Auto. Crash leftovers self-reclaim. |

The state file remembers the owning `config.yaml`'s absolute path, so
[`releasy list`](commands.md#releasy-list) can show the back-link and a
moved/copied config trips a clear ownership error.

## Conflict resolution

When AI resolve is disabled or gives up, RelEasy flags the unit for manual
review and keeps the pipeline moving. Two flavours:

**Singleton (or first PR of a group)** — no useful commits yet, so:
abort cherry-pick, delete local port branch, mark `Conflict` on the board,
do not push, do not open a PR.

**Partial group (later PR in a group fails)** — earlier picks are valid, so:
abort failed pick, push the branch, open a draft PR labelled
`ai-needs-attention` with a banner explaining the failure, mark `Conflict`.

After conflicts:

| You want to… | Run |
|--------------|-----|
| Re-attempt a source PR you just fixed manually | [`releasy run`](commands.md#releasy-run) |
| Mark a manually-resolved port branch as done | [`releasy continue`](commands.md#releasy-continue) |
| Resolve target-drift conflicts on an open rebase PR | [`releasy refresh`](commands.md#releasy-refresh) |
| Drop a port from this run | [`releasy skip`](commands.md#releasy-skip) |
| Resync the GitHub Project board | [`releasy project push`](commands.md#releasy-project-push) |

**Status semantics:** clean or AI-resolved port with rebase PR open →
`needs_review`. Pushed branch without PR → `branch_created`. Needs human →
`conflict`. Rebase PR landed → `merged`. Rebase PR closed without merging
→ `closed` (terminal — `pr_policy.recreate_closed_prs` lets a fresh
`<canonical>-1` / `-2` / … attempt opt back in). Another PR on the same
base already cherry-picks the source(s) → `superseded` (terminal — gated
by `pr_policy.detect_superseded`). AI involvement is signalled by
`ai_resolved` + the `ai-resolved` PR label.

For per-PR hints to the resolver, see
[`ai_context`](configuration.md#per-pr--per-group-ai_context).

## PR title & labels

Rebase PR title: `"<Project> <version>: <subject>"` — e.g.
`"Antalya 26.3: Token Authentication and Authorization"`. Version comes
from the base branch; project is title-cased only if all-lowercase
(`ClickHouse` preserved). A leading `<version>[<project>]:` prefix on the
source title is stripped first.

Labels: every PR gets `releasy` (auto-created on first run with
`push: true`). AI-resolved PRs also get `ai_resolve.label` (default
`ai-resolved`). Optional `merged_label` is stamped on the rebase PR when it
merges and stripped from the source PRs — see
[`merged_label`](configuration.md#configyaml-stable-infrastructure).

## Safety: PRs always target origin

Cross-repo PR URLs are read-only sources for cherry-picks. RelEasy only
ever **creates, updates, labels** PRs in the `origin` repo and only ever
**pushes branches** to that same remote. Enforced at the API helper layer
(`create_pull_request`, `update_pull_request`, `add_label_to_pr`,
`ensure_label`, `force_push`) — there's no parameter to point elsewhere.

`releasy run` prints `PRs will be opened against <owner>/<repo> (origin)`
on startup so the target is visible. If you see writes to anywhere else,
it's a bug — please report.
