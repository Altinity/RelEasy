# RelEasy

RelEasy ports features and PRs onto a stable **base branch** (a tag or commit
you pin to), one cherry-picked port branch + PR at a time — a resumable
alternative to rebasing long-lived branches. One machine can drive several
porting projects at once.

This page is a quick start. For the full picture, see **[docs/](docs/)**.

## TL;DR — which command do I want?

All commands need `RELEASY_GITHUB_TOKEN`. The first group works anywhere
(no project / config / state); the rest assume a set-up project (see below).

**One-off (stateless):**

- Cherry-pick / backport one PR or commit into a branch → `releasy cherry-pick --origin <url> --target <branch> --commit <github-url> [--with-pr]`
- Batch-backport upstream PRs queued in a GitHub Project (Stable releases) → `releasy project-backport --project <url> --version <ver> --target <branch>`
- Address review comments on one PR in any repo → `releasy refresh --pr <url> --address-review --stateless`
- Draft GitHub release notes for any repo → `releasy draft-release --from <tag> --to <branch> --name <name> --work-dir <clone>`

**In a set-up project:**

- Port everything in scope (discover, cherry-pick, open PRs) → `releasy run`
- Resume after fixing a conflict by hand → `releasy continue`
- Map PR dependencies before porting → `releasy graph discover` (add `--open-issue` to track it)
- Tick off ported units on that issue → `releasy graph sync` (automatic after `run` / `refresh`)
- Update every in-scope PR's branch to the latest target → `releasy refresh --merge-target`
- Address reviewer comments across all in-scope PRs → `releasy refresh --address-review`
- CI is red — let AI triage failing tests → `releasy analyze-fails` (or `releasy refresh --analyze-fails`)
- Re-port all rebase PRs onto a different target → `releasy rebase --target <branch>`
- Discard a broken local-only branch with no PR yet → `releasy clear <id>`
- A merged port was reverted on target; never port it again → `releasy mark-reverted --branch <id>`
- See where everything stands → `releasy status`
- Draft the GitHub release notes → `releasy draft-release --from <tag> --to <branch> --name <name>`

The flags compose: e.g. `releasy refresh --merge-target --analyze-fails --address-review`
does all three in one locked pass.

## Install

```bash
pip install -e .
export RELEASY_GITHUB_TOKEN="ghp_..."   # needs repo scope (+ project, to sync a board)
```

## Set up a project

> Your base branch (e.g. `antalya-26.3`) must already exist on the origin remote.

**1. Scaffold it.** Use one directory per project:

```bash
mkdir -p ~/work/antalya-26.3 && cd ~/work/antalya-26.3
releasy new --target-branch antalya-26.3 --project antalya
```

This writes two files side by side:

- `config.yaml` — stable settings (origin remote, push, AI).
- `antalya-26.3.session.yaml` — *what* to port (named after the target branch).

**2. Edit `config.yaml`.** Set your origin remote, and `push: true` once you
want RelEasy to push branches and open PRs (leave it off to keep everything
local while you try things out):

```yaml
origin:
  remote: git@github.com:Altinity/ClickHouse.git
push: true
```

**3. Edit the session file.** Say what to port — by label, explicit PR URLs,
or groups:

```yaml
pr_sources:
  by_labels:
    - labels: ["forward-port", "v26.3"]
      merged_only: true
  include_prs:
    - https://github.com/Altinity/ClickHouse/pull/1500
```

## Port, resolve, repeat

```bash
releasy run        # discover PRs, cherry-pick each onto its own branch, open PRs
```

For every PR or group, RelEasy makes a `feature/<base>/<id>` branch off the
base, cherry-picks the merge commit(s), and — with `push: true` — opens a PR
into the base.

**Hit a conflict?** The run stops and names the branch. Resolve it in the
work-dir repo, then:

```bash
releasy continue   # mark the conflict resolved
releasy run        # resume with the remaining PRs
```

(Set `ai_resolve.enabled: true` in `config.yaml` to let Claude attempt
conflicts for you. Claude resolves; RelEasy then builds the result itself
and runs the PR's tests, retrying build fixes in fresh context. A
resolution that won't build yet is parked on a local branch as
`build_failed` and retried on the next `releasy run`.)

By default the AI work runs through the `claude` CLI. To use an API token
instead — no CLI install, no subscription — set `ai_backend: api` and
`export ANTHROPIC_API_KEY=...`; see
[AI backends](docs/configuration.md#ai-backends).

**Keep open PRs healthy** as the target branch moves and CI runs:

```bash
releasy refresh                  # re-sync status; catch upstream merges/closes
releasy refresh --merge-target   # also merge the moved target into each PR
releasy status                   # see where everything stands
```

Running several projects? Each directory has its own `config.yaml`; `releasy
list` shows them all.

## Cut a release

When the base branch is ready to ship, draft the GitHub release notes from the
PRs merged into it:

```bash
releasy draft-release --from v26.1.6.6-stable --to antalya-26.3 --name v26.1.6.20001.altinityantalya
```

Creates a **draft** release on origin (its URL is printed). Full flags:
[docs/commands.md](docs/commands.md#releasy-draft-release).

## Learn more

- **[docs/concepts.md](docs/concepts.md)** — how it works: the pipeline, branch
  naming, the files RelEasy reads & writes, conflict handling, and the "PRs
  always target origin" safety guarantee.
- **[docs/configuration.md](docs/configuration.md)** — every `config.yaml` and
  session-file key.
- **[docs/commands.md](docs/commands.md)** — every command and flag.
- **[config.yaml.example](config.yaml.example)** ·
  **[session.yaml.example](session.yaml.example)** — fully-commented references.

## License

See [LICENSE](LICENSE).
