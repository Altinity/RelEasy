# RelEasy

RelEasy ports features and PRs onto a stable **base branch** (a tag or commit
you pin to), one cherry-picked port branch + PR at a time — a resumable
alternative to rebasing long-lived branches. One machine can drive several
porting projects at once.

This page is a quick start. For the full picture, see **[docs/](docs/)**.

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
conflicts for you.)

**Keep open PRs healthy** as the target branch moves and CI runs:

```bash
releasy refresh                  # re-sync status; catch upstream merges/closes
releasy refresh --merge-target   # also merge the moved target into each PR
releasy status                   # see where everything stands
```

Running several projects? Each directory has its own `config.yaml`; `releasy
list` shows them all.

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
