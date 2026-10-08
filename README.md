# RelEasy

RelEasy ports PRs and feature branches onto a stable **base branch**: one
cherry-picked port branch and PR per unit, resumable, with optional AI conflict
resolution. One machine can drive several porting projects.

Full docs: **[docs/](docs/)**.

## Which command do I want?

All commands need `RELEASY_GITHUB_TOKEN`.

**No project needed:**

- Backport one PR / commit → `releasy cherry-pick --origin <url> --target <branch> --commit <github-url> [--with-pr]`
- Backport PRs queued in a GitHub Project → `releasy project-backport --project <url> --version <ver> --target <branch>`
- Address review comments on any PR → `releasy refresh --pr <url> --address-review --stateless`
- Draft GitHub release notes → `releasy draft-release --from <tag> --to <branch> --name <name> --work-dir <clone>`

**In a project:**

- Port everything in scope → `releasy run`
- Resume after fixing a conflict by hand → `releasy continue`
- Re-port one unit from scratch → `releasy run --only <id> --redo`
- Map PR dependencies → `releasy graph discover [--open-issue]`
- Merge the moved target into open PRs → `releasy refresh --merge-target`
- Address review comments on all PRs → `releasy refresh --address-review`
- Triage red CI → `releasy analyze-fails`
- Re-port all PRs onto another branch → `releasy rebase --target <branch>`
- Park a PR → `releasy hold <pr-url> --reason <why>` (undo: `releasy unhold`)
- Discard a local-only broken port → `releasy clear <id>`
- Record a reverted port → `releasy mark-reverted --branch <id>`
- See where everything stands → `releasy status`

`refresh` flags compose: `releasy refresh --merge-target --analyze-fails --address-review`.

## Install

```bash
pip install -e .
export RELEASY_GITHUB_TOKEN="ghp_..."   # repo scope (+ project for board sync)
```

## Set up a project

The base branch (e.g. `antalya-26.3`) must already exist on origin.

```bash
mkdir -p ~/work/antalya-26.3 && cd ~/work/antalya-26.3
releasy new --target-branch antalya-26.3 --project antalya
```

This writes `config.yaml` (stable settings) and `antalya-26.3.session.yaml`
(what to port). In `config.yaml`, set the origin and enable pushing when ready:

```yaml
origin:
  remote: git@github.com:Altinity/ClickHouse.git
push: true
```

In the session file, select PRs by label, URL or group:

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
releasy run        # discover PRs, cherry-pick each onto feature/<base>/<id>, open PRs
```

An unresolved conflict marks the unit `conflict` and the run moves on (see
[Conflicts](docs/concepts.md#conflicts)). After resolving a port branch by
hand:

```bash
releasy continue   # push and open the PR for the resolved branch
releasy run        # resume with the remaining PRs
```

Set `ai_resolve.enabled: true` to let AI resolve conflicts; RelEasy then builds
the result and runs the PR's tests. The AI runs through the `claude` CLI by
default; see [AI backends](docs/configuration.md#ai-backends) for `codex` and
direct API use.

Keep open PRs current:

```bash
releasy refresh                  # sync status
releasy refresh --merge-target   # also merge the moved target into each PR
releasy status
```

## Cut a release

```bash
releasy draft-release --from v26.1.6.6-stable --to antalya-26.3 --name v26.1.6.20001.altinityantalya
```

Creates a **draft** GitHub release on origin and prints its URL.

## Learn more

- [docs/concepts.md](docs/concepts.md) — pipeline, branch naming, files,
  statuses, conflicts.
- [docs/configuration.md](docs/configuration.md) — every config and session key.
- [docs/commands.md](docs/commands.md) — every command and flag.
- [config.yaml.example](config.yaml.example) ·
  [session.yaml.example](session.yaml.example) — annotated templates.

## License

See [LICENSE](LICENSE).
