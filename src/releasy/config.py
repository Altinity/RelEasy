"""Configuration loading and validation.

Three layers, one responsibility each:

* ``config.yaml`` — stable infrastructure (origin, workdir, AI settings,
  notifications, push/sequential/update policies, the global ``pr_policy:``
  block). Loaded by :func:`load_config`.

* ``<target_branch>.session.yaml`` — per-effort source data (``features:``,
  ``pr_sources:``). Loaded by :func:`load_session`. Lives next to
  ``config.yaml`` by default, named after the ``target_branch`` (falling
  back to ``<name>`` when unset); overridable via CLI (``--session-file``)
  or a ``session_file:`` key in ``config.yaml``.

* ``<name>.state.yaml`` — runtime progress (status per feature, conflict
  files, AI cost, rebase-PR URLs). See :mod:`releasy.state`.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field, fields
from pathlib import Path

import yaml


# Slug constraints for the per-project ``name:`` field. The name doubles
# as a filename (``<name>.state.yaml``, the ``<name>.lock`` file, and the
# session file when ``target_branch`` is unset) so it must be
# filesystem-safe and short enough to be readable in ``releasy list``
# output.
_VALID_NAME_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
_STATE_SUBDIR = "releasy"


def state_root() -> Path:
    """Resolve the per-user releasy state directory (created on demand).

    Priority:

    1. ``$RELEASY_STATE_DIR`` — escape hatch for tests / CI / power users.
    2. ``$XDG_STATE_HOME/releasy`` — defaults to ``~/.local/state/releasy``
       per the XDG Base Directory spec.
    """
    override = os.environ.get("RELEASY_STATE_DIR")
    if override:
        root = Path(override).expanduser().resolve()
    else:
        xdg = os.environ.get("XDG_STATE_HOME") or str(
            Path.home() / ".local" / "state"
        )
        root = (Path(xdg).expanduser() / _STATE_SUBDIR).resolve()
    root.mkdir(parents=True, exist_ok=True)
    return root


def state_file_path(name: str) -> Path:
    """Path to the per-project pipeline state file."""
    return state_root() / f"{name}.state.yaml"


def lock_file_path(name: str) -> Path:
    """Path to the per-project lock file used by ``locks.project_lock``."""
    return state_root() / f"{name}.lock"


def validate_project_name(name: str) -> str:
    """Validate ``name`` matches the slug regex; return it unchanged on success."""
    if not isinstance(name, str) or not _VALID_NAME_RE.match(name):
        raise ValueError(
            f"Invalid project name {name!r}. Must match "
            f"{_VALID_NAME_RE.pattern} (1-64 chars, letters/digits/._-)."
        )
    return name


def extract_version_suffix(onto: str) -> str:
    """Extract a version suffix from a tag or ref for branch naming.

    v26.3.4.234-lts → 26.3
    v26.2.5.45-stable → 26.2
    Raw SHA → first 8 chars
    """
    m = re.match(r"v?(\d+)\.(\d+)", onto)
    if m:
        return f"{m.group(1)}.{m.group(2)}"
    # Looks like a hex SHA
    if re.fullmatch(r"[0-9a-f]{7,40}", onto):
        return onto[:8]
    return onto


@dataclass
class OriginConfig:
    remote: str
    remote_name: str = "origin"


@dataclass
class UpstreamConfig:
    """Optional upstream remote used **solely** for git fetch during AI conflict
    resolution.

    When configured, the AI resolver may fetch this remote and ``git log -S
    <symbol>`` against it to identify which upstream PR introduced a missing
    foundation (see ``ai_resolve.prompt_file``'s "Recognising a
    missing-prerequisite conflict" section). RelEasy never pushes to this
    remote and never reads code from it for conflict resolution — only commit
    references for prereq detection.

    The upstream remote is auto-added to the local clone on demand (idempotent)
    so users only have to declare it once in config.
    """
    remote: str
    remote_name: str = "upstream"
    branch: str = "master"


@dataclass
class FeatureConfig:
    id: str
    description: str
    source_branch: str  # existing branch where feature commits live
    enabled: bool = True
    depends_on: list[str] = field(default_factory=list)
    # Optional free-form note appended to the AI conflict-resolver prompt
    # when this feature's port hits a conflict. Use it to call out gotchas
    # the model can't infer from the diff alone (e.g. "this depends on
    # the auth refactor — prefer reverting the local change in case of
    # doubt").
    ai_context: str = ""


_VALID_IF_EXISTS = ("skip", "recreate", "append")
_VALID_GROUP_SORT = ("listed", "merged_at")
# Config accepts all three; post-detection only the last two survive.
_VALID_PORT_MODES = ("auto", "backport", "forward_port")
_VALID_EFFORTS = ("low", "medium", "high", "xhigh", "max")
_VALID_AI_BACKENDS = ("cli", "api")

from typing import Literal, get_args  # noqa: E402
PortMode = Literal["backport", "forward_port"]


@dataclass
class PRSourceConfig:
    labels: list[str]
    description: str = ""
    merged_only: bool = False
    # When the port branch already exists: "skip" (leave it alone),
    # "recreate" (delete and rebuild from base), or "append" (cherry-pick
    # any PRs that aren't already on the existing branch onto its tip,
    # leaving prior commits untouched). Inherits ``pr_policy.if_exists``
    # from config.yaml when not set per-source.
    if_exists: str = "skip"
    # Optional free-form note appended to the AI conflict-resolver prompt
    # for every PR matched by this label entry. Keep it short and
    # high-signal: the model already gets the source PR body and diff.
    ai_context: str = ""
    # ``auto`` (default) | ``backport`` | ``forward_port``. See
    # ``_detect_port_mode``. Explicit values bypass the ladder.
    mode: str = "auto"


@dataclass
class PRGroupConfig:
    """A sequential group of PRs ported onto a single branch as one PR.

    All ``prs`` are cherry-picked onto the same port branch
    ``feature/<base>/<id>`` and result in a single combined PR (when push +
    ``pr_policy.auto_pr``). Cherry-pick order is controlled by ``sort``:
    ``"listed"`` (default) walks ``prs`` top-to-bottom, ``"merged_at"`` orders
    them by GitHub merge timestamp ascending (PR number breaks ties).
    """
    id: str
    prs: list[str]
    description: str = ""
    # When the port branch already exists: "skip", "recreate", or
    # "append" (cherry-pick PRs that haven't been applied yet on top of
    # the existing branch tip — useful when a PR is added to the group
    # after a prior ``releasy run`` already produced the combined branch).
    # Inherits from ``pr_policy.if_exists`` in config.yaml when not set
    # per-group.
    if_exists: str = "skip"
    # "listed" — cherry-pick in the order PRs appear in ``prs`` (default,
    # use when one PR depends on another in a non-chronological way).
    # "merged_at" — sort by GitHub merge timestamp ascending before
    # cherry-picking; PR number breaks ties.
    sort: str = "listed"
    # Optional free-form note appended to the AI conflict-resolver prompt
    # for every cherry-pick step in the group.
    ai_context: str = ""
    # Per-PR-URL ai_context for individual entries inside ``prs`` that
    # used the dict form (``{url: ..., ai_context: ...}``). Keyed by URL
    # exactly as listed in the session file. Combined with the
    # group-level ``ai_context`` at resolve time.
    pr_ai_contexts: dict[str, str] = field(default_factory=dict)
    # Other unit IDs this group depends on. A unit ID is either another
    # group's ``id`` or a singleton feature_id (``pr-<N>`` for origin PRs,
    # ``<owner>-<repo>-pr-<N>`` for cross-repo singletons). Validated at
    # session load time. The pipeline gates processing on these: a unit
    # only runs once every entry in ``depends_on`` has reached
    # ``status: merged`` in target.
    depends_on: list[str] = field(default_factory=list)
    # Marker set by ``releasy graph discover``. The CLI uses
    # it to identify entries it owns and may rewrite. Hand-written groups
    # have ``auto_discovered: false`` and are never touched.
    auto_discovered: bool = False
    # See ``PRSourceConfig.mode``.
    mode: str = "auto"


@dataclass
class PRPolicyConfig:
    """Policy knobs for PR processing. Lives in ``config.yaml`` (not the
    session file) because these settings are stable across efforts: they
    describe *how* to process discovered units, not *which* units to
    discover.

    ``if_exists`` is the default for individual ``pr_sources`` entries in
    the session file that don't set their own ``if_exists``.
    """
    if_exists: str = "skip"
    auto_pr: bool = True
    retry_failed: bool = True
    recreate_closed_prs: bool = False
    # Detect tracked entries whose source PRs were already cherry-picked
    # by some OTHER PR (open or merged) targeting the same base branch,
    # and mark them ``status="superseded"`` so refresh / run stop wasting
    # work on them. Scans target-branch git history for ``(cherry picked
    # from commit <sha>)`` footers and walks open PRs targeting the same
    # base. Default on; disable when you want the old behaviour (releasy
    # keeps refreshing its own port regardless of parallel work).
    detect_superseded: bool = True
    # How many times `releasy run` auto-resumes a partially-applied group.
    # A prior run cherry-picked some of a group's PRs, then a conflict
    # (often an AI token/budget exhaustion) stopped it mid-way, leaving a
    # draft PR labelled ai-needs-attention. With retry_failed on (default),
    # each run appends the not-yet-applied PRs and re-resolves. After this
    # many attempts it stops and leaves the draft PR for manual help.
    # 0 disables auto-continue (old behaviour: leave it until the user sets
    # if_exists: append by hand).
    max_partial_continue_attempts: int = 2
    # Skip a unit whose recorded stall cannot clear on its own — one waiting
    # for another unit's PR to merge, for a missing prereq to be queued, or
    # one that has spent its attempt cap. Re-resolving those reaches the same
    # verdict at full token price. The stall is dropped (and the unit
    # retried) as soon as what it waits on changes. Set false to always
    # re-attempt; `releasy run --ignore-stalls` overrides it per run.
    honor_stall_reasons: bool = True


@dataclass
class PRSourcesConfig:
    """PR discovery selectors. Lives in the session file — this is the
    mutable, per-effort data that changes between runs (which PRs to port
    this week, which labels define the current rebase wave, etc.).

    Set arithmetic:
        union(by_labels)
        − exclude_labels − exclude_authors
        ∩ (include_authors when set)
        + include_prs
        − exclude_prs

    ``groups`` are evaluated independently: every PR listed in any group is
    ported as part of that group (one combined PR per group), regardless of
    label matches. ``exclude_prs``, ``exclude_labels``, ``include_authors``
    and ``exclude_authors`` still drop individual PRs from a group; if a
    group ends up empty, it is dropped with a warning.

    ``include_authors`` (when non-empty) restricts discovered PRs to those
    authored by one of the listed GitHub logins. ``exclude_authors`` drops
    PRs by the listed authors. Author comparisons are case-insensitive.
    Both filters are bypassed for PRs explicitly listed in ``include_prs``.

    Policy knobs that used to live here (``if_exists``, ``auto_pr``,
    ``retry_failed``, ``recreate_closed_prs``) now live in
    :class:`PRPolicyConfig` under ``pr_policy:`` in ``config.yaml``.
    """
    by_labels: list[PRSourceConfig] = field(default_factory=list)
    exclude_labels: list[str] = field(default_factory=list)
    include_prs: list[str] = field(default_factory=list)
    exclude_prs: list[str] = field(default_factory=list)
    include_authors: list[str] = field(default_factory=list)
    exclude_authors: list[str] = field(default_factory=list)
    groups: list[PRGroupConfig] = field(default_factory=list)
    # Optional path to a sidecar YAML that carries auto-discovered (or
    # hand-curated) ``groups[]`` entries with ``depends_on:`` edges.
    # Loaded by :func:`load_session` and merged into ``groups[]`` in
    # memory; never mutates the main session file. ``releasy
    # graph discover`` writes to this path by default (unless
    # ``--no-write`` / ``--deps-file <override>`` says otherwise). Relative paths
    # resolve against the session file's directory.
    deps_file: str | None = None
    # Per-PR-URL ai_context for individual entries inside ``include_prs``
    # that used the dict form (``{url: ..., ai_context: ...}``). Keyed by
    # URL exactly as listed in the session file. Surfaced to the AI
    # conflict resolver alongside any unit-level ``ai_context``.
    include_pr_contexts: dict[str, str] = field(default_factory=dict)
    # Labels that mark a source PR as a forward-port (case-insensitive).
    # When matched, ``_detect_port_mode`` returns ``forward_port``.
    forward_port_labels: list[str] = field(default_factory=list)


def _default_assignee_dev_options() -> list[str]:
    """Single-select options for the project's ``Assignee Dev`` field.

    These are display labels (preferring each contributor's GitHub
    "name" over the bare login), kept here as a sensible team default.
    Users can override the list in ``notifications.assignee_dev_options``
    in ``config.yaml`` to add / remove team members. Whatever ends up in
    config also drives the option set RelEasy provisions on the GitHub
    Project board on first ``releasy setup-project`` run.
    """
    return [
        "Andrey Zvonov",
        "Anton Ivashkin",
        "Arthur Passos",
        "DQ",
        "Ilya Golshtein",
        "Mikhail Koviazin",
        "Vasily Nemkov",
    ]


def _default_assignee_qa_options() -> list[str]:
    """Single-select options for the project's ``Assignee QA`` field.

    ``Verified by Dev`` is a special meta-option meaning "no separate QA
    pass needed — the developer self-verified". It is intentionally not a
    real person.
    """
    return [
        "Alsu Giliazova",
        "Carlos",
        "Davit Mnatobishvili",
        "strtgbb",
        "vzakaznikov",
        "Verified by Dev",
    ]


def _default_assignee_dev_login_map() -> dict[str, str]:
    """GitHub login → ``Assignee Dev`` option label.

    Used by ``sync_project`` to seed the field with the source PR's
    author when a card is first created. Lookups are case-insensitive,
    but the original casing is preserved in this dict so it round-trips
    cleanly through ``config.yaml`` (``Enmk`` stays ``Enmk`` rather
    than being rewritten to ``enmk`` on every save). Logins that don't
    appear here leave the field empty for manual assignment.
    """
    return {
        "zvonand": "Andrey Zvonov",
        "ianton-ru": "Anton Ivashkin",
        "arthurpassos": "Arthur Passos",
        "il9ue": "DQ",
        "ilejn": "Ilya Golshtein",
        "mkmkme": "Mikhail Koviazin",
        "Enmk": "Vasily Nemkov",
    }


@dataclass
class NotificationsConfig:
    github_project: str | None = None
    # Single-select option labels for the ``Assignee Dev`` / ``Assignee QA``
    # project fields. Edit these in ``config.yaml`` to extend the team
    # roster — RelEasy provisions exactly the listed options on the
    # GitHub Project board (see ``setup_project``) and reconciles
    # missing ones on subsequent runs. Existing options the user added
    # by hand on the board are preserved (we only ever add, never
    # delete, since a different field-owner outside RelEasy may be
    # managing additions).
    assignee_dev_options: list[str] = field(
        default_factory=_default_assignee_dev_options,
    )
    assignee_qa_options: list[str] = field(
        default_factory=_default_assignee_qa_options,
    )
    # GitHub login → ``Assignee Dev`` option label. Drives the default
    # value RelEasy puts on the ``Assignee Dev`` field of newly created
    # project cards (the source PR's author). Keys are lower-cased at
    # load time so the YAML file can use whatever casing GitHub displays.
    # Logins missing from this map leave the field empty for manual
    # assignment.
    assignee_dev_login_map: dict[str, str] = field(
        default_factory=_default_assignee_dev_login_map,
    )


def _default_allowed_tools() -> list[str]:
    return [
        "Read", "Edit", "Write", "Glob", "Grep",
        "Bash(git:*)", "Bash(gh:*)", "Bash(cd:*)",
        "Bash(bash:*)",
        "Bash(ninja:*)", "Bash(cmake:*)", "Bash(make:*)",
        "Bash(ls:*)", "Bash(cat:*)", "Bash(head:*)",
        "Bash(tail:*)", "Bash(tee:*)", "Bash(rg:*)",
    ]


def _default_analyze_fails_allowed_tools() -> list[str]:
    """Allowlist for ``releasy analyze-fails`` — strictly additive to the
    base list.

    Investigating CI failures naturally needs a few capabilities the
    cherry-pick / review flows don't:

    * ``WebFetch`` / ``WebSearch`` so Claude can pull upstream issue
      threads, ClickHouse docs, error-message references, and the
      praktika report itself when it needs more than the inlined
      excerpt.
    * Test-runner binaries (``tests/clickhouse-test``,
      ``tests/integration/runner``, ``pytest``).
    * ``rm`` for the ``rm -rf ci/tmp`` directive baked into the prompt.
    * ``echo`` / ``wc`` / ``grep`` / ``awk`` / ``sed`` — staples of any
      shell-driven log triage.

    Users can override this entirely via
    ``analyze_fails.allowed_tools`` in config.yaml; the
    ``{work_dir}`` / ``{repo_dir}`` / ``{cwd}`` placeholders resolve to
    the live repo path each invocation, so absolute-binary entries
    stay portable.
    """
    return _default_allowed_tools() + [
        "WebFetch", "WebSearch",
        # File-system mutation needed by test runners that materialise
        # config trees (e.g. ClickHouse's ``ci/tmp/etc/...`` for an
        # integration scenario) before invoking the binary. Claude
        # Code's allowlist doesn't support path-scoped Bash patterns,
        # so these are deliberately broad — the safety net is that
        # Claude operates from the repo dir, ``ci/tmp`` is gitignored,
        # and the post-run linear-history + working-tree-clean check
        # rejects any drift before push.
        "Bash(rm:*)", "Bash(mkdir:*)", "Bash(touch:*)",
        "Bash(cp:*)", "Bash(mv:*)",
        "Bash(chmod:*)",
        "Bash(echo:*)", "Bash(wc:*)", "Bash(grep:*)",
        "Bash(awk:*)", "Bash(sed:*)", "Bash(find:*)",
        "Bash(diff:*)", "Bash(sort:*)", "Bash(uniq:*)",
        "Bash(xargs:*)", "Bash(tr:*)", "Bash(cut:*)",
        "Bash(tests/clickhouse-test:*)",
        "Bash(tests/integration/runner:*)",
        "Bash(./tests/clickhouse-test:*)",
        "Bash(./tests/integration/runner:*)",
        "Bash(pytest:*)",
        "Bash(python:*)", "Bash(python3:*)",
        # Project-binary paths that {work_dir} resolves to at runtime;
        # see analyze_fails._resolve_tool_paths.
        "Bash({work_dir}/build/programs/clickhouse:*)",
        "Bash({work_dir}/tests/clickhouse-test:*)",
        "Bash({work_dir}/tests/integration/runner:*)",
    ]


def _default_test_file_globs() -> list[str]:
    """Repo-relative globs marking a changed file as a runnable test.

    Used to decide whether a ported PR carries tests worth running after a
    green build. ClickHouse defaults; override per project via
    ``ai_resolve.test_file_globs``.
    """
    return [
        "tests/queries/**",
        "tests/integration/**",
        "src/**/tests/**",
        "**/gtest_*",
        "**/*_test.cpp",
    ]


@dataclass
class AIChangelogConfig:
    """Claude-driven CHANGELOG entry synthesis for grouped PR ports.

    For singleton ports the changelog entry is taken verbatim from the
    source PR's own ``Changelog entry`` section — Claude is never
    invoked, no API cost, no surprises. The synthesizer only kicks in
    for multi-PR groups, where the per-PR entries can include
    intermediate fix-ups that aren't user-visible once the whole group
    is ported as a single change.

    Defaults to disabled because it costs API tokens; enable it
    explicitly when you want polished combined-port CHANGELOG entries.
    Reuses the same Claude binary (and ``ai_resolve.command`` defaults
    so users running with one resolver setup don't have to configure
    two paths) but is otherwise independent of conflict resolution.
    """
    enabled: bool = False
    command: str = "claude"
    prompt_file: str = "prompts/synthesize_changelog.md"
    timeout_seconds: int = 300  # 5 min — should be a few seconds in practice
    # Per-PR body trimmed to this many characters before being inlined
    # into the synthesis prompt. Keeps the request payload bounded for
    # groups that drag in long source-PR descriptions.
    max_pr_body_chars: int = 3000


@dataclass
class AIApiConfig:
    """Settings for ``ai_backend: api`` — talk to the Anthropic API directly.

    In this mode RelEasy never spawns ``claude`` (or any other agent CLI):
    it drives the Messages API with an API token and runs the tool calls
    itself. The per-section ``command`` / ``extra_args`` settings are then
    unused; ``allowed_tools`` and ``timeout_seconds`` still apply.
    """
    # Env var holding the token. Checked before ``api_key`` so the secret
    # normally stays out of config.yaml.
    api_key_env: str = "ANTHROPIC_API_KEY"
    api_key: str | None = None
    base_url: str | None = None  # gateway / proxy override
    # Falls back to the global ``ai_model`` (alias-mapped) when unset.
    model: str | None = None
    max_tokens: int = 64000
    # Hard cap on model round-trips per invocation — the API-mode analogue
    # of "the CLI eventually gives up".
    max_turns: int = 300
    thinking: bool = True
    # SDK-level retries for 429 / 5xx before the failure surfaces to the
    # existing transient-retry + session-wait ladder.
    max_retries: int = 5
    request_timeout_seconds: int = 1800
    bash_timeout_seconds: int = 3600
    tool_output_max_chars: int = 30000
    # Appended verbatim to the built-in system prompt.
    system_prompt_extra: str = ""


@dataclass
class AutoAddPrerequisitePRsConfig:
    """Auto-recovery for missing-prerequisite cherry-pick conflicts.

    When ``enabled`` is true and Claude reports ``MISSING_PREREQS:`` in the
    cherry-pick prompt, RelEasy prepends the discovered prereq PR(s) to the
    current unit's cherry-pick sequence and restarts it. The unit effectively
    becomes a group with the prereq first and the original PR last; one
    combined PR is opened on success.

    ``max_prereq_depth`` caps recursion (counted in dives, not total PRs):
    PR_A → PR_B → PR_C is depth 2. Hitting the cap aborts the unit, rolls
    back all dynamic prereqs, and surfaces a verbose dependency trail in
    stdout, the GitHub Project board card, and (when one exists) the
    placeholder PR body.

    Off by default: detection-only mode still applies (the prereq is just
    labelled and reported, no recursive porting).

    ``require_origin_prereq_label`` (default true): when a discovered
    prereq lives in the origin repo *and* the PR that needs it also lives
    in origin, the prereq must carry the configured selection labels
    (i.e. it must match at least one ``pr_sources.by_labels`` entry and
    none of ``exclude_labels``) — the same labels our own discovery
    selects on. An in-origin prereq without those labels is treated as
    out of scope: the dive aborts (detection-only) and the user must
    either label it or list it explicitly in ``include_prs``. The gate is
    only reached when no unit already carries the prereq — one that some
    unit's combined port brings in parks as ``waiting_for_merge`` first.
    Prereqs on
    a different repo than origin (forward-port / backport sources) are
    never gated, and the gate is a no-op when no ``by_labels`` selection
    is configured.
    """
    enabled: bool = False
    max_prereq_depth: int = 7
    require_origin_prereq_label: bool = True


@dataclass
class ReviewResponseConfig:
    """Claude-driven PR review-response configuration.

    Drives ``releasy refresh --address-review`` — an AI pass that reads
    review feedback left on an open rebase PR and either (a) makes code
    changes and commits them, or (b) replies to the thread explaining
    why the comment doesn't translate to a code change. Opportunistically
    stateful: when the PR is tracked in state RelEasy remembers the
    last run's timestamp; otherwise it's pure stateless.

    Comments authored by anyone NOT in ``trusted_reviewers`` are
    filtered out **before** the AI sees the prompt, so an untrusted
    commenter cannot inject instructions into the run. That allowlist
    is the only safety gate — there is no master ``enabled:`` switch,
    because the ``--address-review`` flag is itself the explicit
    opt-in.
    """
    command: str = "claude"
    prompt_file: str = "prompts/address_review.md"
    timeout_seconds: int = 7200  # 2h
    max_iterations: int = 15
    # GitHub ``author_association`` values whose holders we trust to
    # feed the AI. Defaults to anyone with a real relationship to the
    # repo: ``OWNER`` (the user/org owning the repo), ``MEMBER`` (a
    # member of the repo's owning organisation), ``COLLABORATOR`` (a
    # user granted explicit repo permissions), and ``CONTRIBUTOR``
    # (anyone who has previously had a commit merged into the repo —
    # i.e. a prior code contributor, not a drive-by). GitHub sets this
    # field server-side; commenters cannot forge it. Tighten the list
    # to e.g. ``["OWNER", "MEMBER"]`` for stricter repos, or add
    # ``FIRST_TIME_CONTRIBUTOR`` for community-heavy repos.
    trusted_associations: list[str] = field(
        default_factory=lambda: [
            "OWNER", "MEMBER", "COLLABORATOR", "CONTRIBUTOR",
        ],
    )
    # Extra allowlist of GitHub logins (compared case-insensitively),
    # additive on top of ``trusted_associations``. Use this to trust a
    # specific external reviewer that GitHub does not yet record as a
    # collaborator. Empty list is fine — the association gate handles
    # the common case on its own.
    trusted_reviewers: list[str] = field(default_factory=list)
    # Whether the AI posts a reply to each comment it classifies as
    # non-actionable (already fixed / out of scope / misunderstanding).
    # Replies include a short machine-readable footer identifying them
    # as bot-generated. ADDRESSABLE comments are answered by the
    # commit that fixes them, not by a reply. Default on — when off
    # (via ``--no-reply`` or config), non-actionable comments are just
    # listed in the AI's terminal narration.
    reply_to_non_addressable: bool = True
    # Optional: let the AI post one summary comment on the PR at the
    # end of the run too, describing what it changed and which comments
    # it declined to address. Distinct from per-comment replies; both
    # can be on. Never resolves conversations — that's reserved for
    # humans.
    post_summary_comment: bool = False
    allowed_tools: list[str] = field(default_factory=_default_allowed_tools)
    extra_args: list[str] = field(default_factory=list)


@dataclass
class GraphConfig:
    """Config for ``releasy graph discover`` / ``graph update``.

    Only comments whose ``author_association`` is in ``trusted_associations``
    (or whose login is in ``trusted_reviewers``) are fed to Claude. Tighten
    to ``["MEMBER"]`` for strict org membership.
    """
    command: str = "claude"
    prompt_file: str = "prompts/adjust_graph.md"
    timeout_seconds: int = 7200
    trusted_associations: list[str] = field(
        default_factory=lambda: ["OWNER", "MEMBER", "COLLABORATOR"],
    )
    # Extra trusted GitHub logins (case-insensitive), additive.
    trusted_reviewers: list[str] = field(default_factory=list)
    # Labels for the graph issue; the target-branch name is always added too.
    # Created on origin if missing (releasy's default purple).
    issue_labels: list[str] = field(default_factory=lambda: ["releasy"])
    # Post a summary comment on the issue after a successful update.
    post_comment: bool = True
    # Refresh the graph issue's progress checkboxes at the end of
    # ``releasy run`` / ``releasy refresh`` (same as `releasy graph sync`).
    sync_progress: bool = True
    # Enforce vetoes by adding the PR to exclude_prs (else record-only).
    apply_exclusions: bool = True
    # Collapse (mark Outdated) the comments an update actually addressed;
    # unaddressed comments are left visible.
    minimize_addressed_comments: bool = True
    extra_args: list[str] = field(default_factory=list)


@dataclass
class AnalyzeFailsConfig:
    """Claude-driven CI-failure investigator configuration.

    Drives ``releasy analyze-fails`` — an AI pass that walks failed CI
    statuses on a PR's head commit, fetches their reports (praktika JSON
    for the in-repo suites, TestFlows ``fails.log.txt`` for the
    regression suites), and invokes Claude per failed shard to reproduce
    + fix the failures (or classify them as unrelated flakes).

    The build wrapper materialised inside the repo reuses
    ``ai_resolve.build_command`` (so a project that already configured
    a build for conflict resolution gets the same one here for free).
    Per-test reproduction commands are baked into the prompt; this
    config controls only how Claude is invoked.
    """
    command: str = "claude"
    prompt_file: str = "prompts/analyze_fails.md"
    # Test categories to investigate; empty (the default) means every
    # failed check. Known categories: fasttest, quick_functional,
    # stateless, integration, regression, other (the catch-all for
    # checks RelEasy has no reproduction recipe for). Narrow this to
    # skip expensive suites — e.g. drop "regression", which needs the
    # external Altinity/clickhouse-regression repo to reproduce.
    categories: list[str] = field(default_factory=list)
    # Full build + per-test rerun cycles routinely take 30-60 minutes,
    # so 2h gives Claude headroom for one iteration plus a retry.
    timeout_seconds: int = 7200  # 2h
    # Build attempts cap — the prompt forwards this verbatim and Claude
    # is instructed to exit with UNRESOLVED if it hits the wall.
    max_iterations: int = 6
    # When `--pr` is omitted, only this many tracked PRs are processed
    # per invocation (0 = no cap). Keeps a stray cron run from
    # cherry-picking everyone's CI bill at once.
    max_prs_per_run: int = 0
    # The same test failing in this many OTHER recent tracked PRs
    # earns it the "likely unrelated flake" label, which Claude is told
    # about in the prompt. Set to 0 to disable the heuristic entirely
    # (Claude will then judge purely by diff inspection).
    flaky_elsewhere_threshold: int = 2
    # Cap on how many other tracked PRs we'll fetch reports for to
    # build the flaky-elsewhere map. Cheap reads from S3, but each one
    # is a network round-trip per shard, so we don't want it unbounded.
    flaky_check_prs: int = 12
    # When the PR is on origin, post a top-level comment summarising
    # each shard's outcome (test counts, classification, Claude's
    # narration tail, commits added, cost). The comment is the durable
    # record of what an investigation concluded — local stdout is
    # cropped by the terminal, the comment is not.
    post_comment_to_pr: bool = True
    allowed_tools: list[str] = field(
        default_factory=_default_analyze_fails_allowed_tools,
    )
    extra_args: list[str] = field(default_factory=list)


@dataclass
class AIResolveConfig:
    """Claude-driven conflict resolver configuration."""
    enabled: bool = False
    command: str = "claude"
    prompt_file: str = "prompts/resolve_conflict.md"
    # Prompt template used when the AI resolver is driving a `git merge`
    # to completion (the ``releasy refresh`` flow that keeps already-open
    # rebase PRs current with their target branch). Kept
    # separate from ``prompt_file`` because the in-progress operation
    # ("merge" vs "cherry-pick"), the way to conclude it (`git commit
    # --no-edit` vs `git cherry-pick --continue`), and the framing of
    # what to preserve all differ.
    merge_prompt_file: str = "prompts/resolve_merge_conflict.md"
    # Prompt template used when ``split_conflict_commit`` is on: the
    # cherry-pick has already been concluded by RelEasy with conflict
    # markers committed as a stand-alone "with conflicts" commit, and
    # Claude's job is to make a SECOND commit on top with the resolution.
    # The flow differs from the in-progress-cherry-pick prompt enough
    # (no `git cherry-pick --continue`, two commits to keep clearly
    # separate) that it gets its own template.
    split_prompt_file: str = "prompts/resolve_conflict_split.md"
    # When True (default), a cherry-pick conflict is first concluded as a
    # "with conflicts" commit (markers committed verbatim, original
    # cherry-pick message preserved), and Claude is then asked to make a
    # second commit with the resolution. The two-commit history makes the
    # conflict and its resolution clearly visible in the port branch's
    # `git log`. When False, falls back to the legacy single-commit flow
    # (Claude resolves and concludes the cherry-pick directly).
    split_conflict_commit: bool = True
    allowed_tools: list[str] = field(default_factory=_default_allowed_tools)
    max_iterations: int = 5
    timeout_seconds: int = 7200  # 2h
    build_command: str = "cd build && ninja"
    # --- Deterministic build/test split (the `run` cherry-pick flow) ---
    # True: Claude resolves only; RelEasy builds + tests and loops fresh-context
    # build fixes. False: legacy single-session resolve+build. See docs.
    deterministic_build: bool = True
    max_build_attempts: int = 5  # consecutive build-fix attempts per run
    # Resumes of a parked build_failed branch on later runs; 0 disables.
    max_verify_resume_attempts: int = 2
    # Re-port from base instead of resuming when a parked branch has fallen
    # this many commits behind base — building it would compile stale code
    # and feed the fixer errors from a base that no longer exists.
    # 0 disables the check (always resume, however old the branch).
    max_resume_base_drift: int = 50
    max_verify_iterations: int = 12  # cap on build↔test iterations per pass
    build_log_tail_lines: int = 500  # log tail fed to the fix-build prompt
    # RelEasy's own wall-clock cap for one build subprocess.
    build_timeout_seconds: int = 7200  # 2h
    # Run the source PR's own tests after a green build (Claude-driven).
    run_pr_tests: bool = True
    # Globs (repo-relative) marking a changed file as a runnable test.
    test_file_globs: list[str] = field(default_factory=_default_test_file_globs)
    # Wall-clock cap for one run-tests Claude invocation.
    test_timeout_seconds: int = 3600  # 1h
    # Prompt templates for the split flow.
    resolve_only_prompt_file: str = "prompts/resolve_conflict_nobuild.md"
    fix_build_prompt_file: str = "prompts/fix_build.md"
    run_tests_prompt_file: str = "prompts/run_tests.md"
    label: str = "ai-resolved"
    label_color: str = "8B5CF6"
    # Label attached to a PR that needs human attention because the AI
    # resolver gave up (a partial-group draft PR; singletons get no PR).
    needs_attention_label: str = "ai-needs-attention"
    needs_attention_label_color: str = "D93F0B"
    # Label applied to the PR / draft PR for a unit whose conflict was
    # caused by a missing prerequisite PR (detection-only mode), or whose
    # auto-recovery exhausted ``max_prereq_depth``, or whose discovered
    # prereq is already queued elsewhere. Distinct from
    # ``needs_attention_label`` so dashboards can tell "AI gave up, no
    # idea why" apart from "AI knew exactly what's missing".
    missing_prereqs_label: str = "missing-prerequisites"
    missing_prereqs_label_color: str = "E4E669"
    # Label applied to a successfully-merged combined PR that picked up
    # one or more dynamically-added prerequisite PRs via auto-recovery.
    # Lets reviewers know the PR's scope was recursively expanded.
    auto_prereq_label: str = "auto-prereq-added"
    auto_prereq_label_color: str = "0E8A16"
    # Advisory second AI pass auditing the resolution against the source
    # PR. Roughly doubles AI cost on conflicted PRs. Findings drive
    # ``verify_label`` + a PR comment; never rolls back.
    verify_resolution: bool = False
    verify_prompt_file: str = "prompts/verify_resolution.md"
    verify_timeout_seconds: int = 1800  # read-only, no build
    verify_label: str = "ai-needs-verify"
    verify_label_color: str = "FBCA04"
    extra_args: list[str] = field(default_factory=list)
    # How many times to re-invoke claude when the Anthropic streaming
    # API drops the turn with a transient error ("Stream idle timeout",
    # "Overloaded", "Connection reset", …). Each retry is a fresh turn.
    api_retries: int = 3
    api_retry_backoff_seconds: int = 15
    # On an exhausted Claude usage session, poll + re-prompt instead of
    # failing (applies to every Claude call). False ⇒ fail fast. See docs.
    wait_on_session_exhaustion: bool = True
    session_exhaustion_max_wait_hours: int = 60
    session_exhaustion_poll_minutes: int = 30
    # Extra regexes (OR-ed with the built-ins) for recognising a limit message.
    session_exhaustion_extra_patterns: list[str] = field(default_factory=list)
    # How many corrective Claude passes to run when a resolution lands but
    # trips a *content-correctable* postcondition — today only the
    # append-only ``SettingsChangesHistory.cpp`` whitelist (extra rows swept
    # in from "ours"). Instead of discarding the whole resolution, RelEasy
    # hands the exact error back to Claude to trim the offending file in
    # place. 0 disables (revert to the old fail-and-reset behaviour).
    postcondition_retries: int = 2
    # What to do when those passes are spent and the check still fails.
    # True (default): keep the resolution, push it, and flag it on the PR
    # (warning comment + ``verify_label``) — the port is complete apart
    # from that one registry file, so a human reviews a diff instead of
    # redoing the whole conflict. False: discard and reset, as before.
    warn_on_unfixed_postconditions: bool = True
    # Auto-recovery on detected missing prerequisites (off by default).
    # See ``AutoAddPrerequisitePRsConfig``.
    auto_add_prerequisite_prs: AutoAddPrerequisitePRsConfig = field(
        default_factory=AutoAddPrerequisitePRsConfig,
    )


@dataclass
class SessionConfig:
    """Per-effort source data.

    Kept deliberately small: just the ``features`` list and the ``pr_sources``
    selectors. Policy knobs that used to live in ``pr_sources`` are on
    :class:`PRPolicyConfig` (in ``config.yaml``) because they don't change
    between efforts.

    ``session_path`` is the on-disk file this was loaded from (or where it
    should be written by :func:`save_session`). ``None`` means in-memory only.
    """
    features: list[FeatureConfig] = field(default_factory=list)
    pr_sources: PRSourcesConfig = field(default_factory=PRSourcesConfig)
    # Labels applied to every rebase PR opened in this session. Each name
    # is auto-created on origin (with releasy's default purple colour) the
    # first time it's needed. ``releasy refresh`` also reconciles them
    # onto tracked PRs that are missing one or more.
    pr_labels: list[str] = field(default_factory=list)
    # Extra labels applied only to PRs whose detected port mode matches.
    # Keyed by ``PortMode`` (``forward_port`` / ``backport``); merged with
    # ``pr_labels`` at apply time. A PR of unknown mode gets ``pr_labels``
    # only — never a mode label.
    pr_labels_by_mode: dict[str, list[str]] = field(default_factory=dict)
    session_path: Path | None = None
    # Non-fatal issues encountered while loading, e.g. a deps_file overlay
    # entry whose id collided with the main session. Surfaced once at CLI
    # startup; never causes load to fail.
    load_warnings: list[str] = field(default_factory=list)


@dataclass
class Config:
    name: str  # unique slug identifying this project (state file key)
    origin: OriginConfig
    project: str  # short project identifier, e.g. "antalya"
    # Optional second remote, used SOLELY for fetch during AI conflict
    # resolution (prereq detection via ``git log -S``). When ``None`` the
    # AI resolver only inspects the origin remote's history. RelEasy never
    # pushes to upstream and never reads code from it.
    upstream: UpstreamConfig | None = None
    target_branch: str | None = None  # explicit base/target branch override
    # When a PR for the port branch already exists on GitHub:
    #   false (default) — leave it exactly as-is; don't try to create a new
    #                     one and don't touch its title/body.
    #   true            — reuse that PR but overwrite its title and body with
    #                     what releasy would have set (source PR references,
    #                     combined group body, ai-resolved prefix, …). Useful
    #                     when the source PRs' descriptions changed or you
    #                     tweaked the body format and want the rebase PR to
    #                     reflect it.
    update_existing_prs: bool = False
    # Optional label applied to a port (rebase) PR once it has been merged
    # into the target branch, and stripped from each of the source PRs that
    # the port was cherry-picked from. Empty / unset disables the feature.
    # The label is auto-created on origin (purple, like other releasy
    # labels) the first time a merged port is observed. Source-PR cleanup
    # is best-effort and only touches PRs hosted on the configured origin
    # — source PRs on a different repo are skipped (we never write outside
    # origin).
    merged_label: str | None = None
    merged_label_color: str = "8B5CF6"  # purple, matches releasy defaults
    pr_policy: PRPolicyConfig = field(default_factory=PRPolicyConfig)
    notifications: NotificationsConfig = field(default_factory=NotificationsConfig)
    ai_resolve: AIResolveConfig = field(default_factory=AIResolveConfig)
    ai_changelog: AIChangelogConfig = field(default_factory=AIChangelogConfig)
    review_response: ReviewResponseConfig = field(default_factory=ReviewResponseConfig)
    analyze_fails: AnalyzeFailsConfig = field(default_factory=AnalyzeFailsConfig)
    graph: GraphConfig = field(default_factory=GraphConfig)
    config_path: Path = field(default_factory=lambda: Path.cwd() / "config.yaml")
    work_dir: Path | None = None
    # When set, a copy of all stdout+stderr (Rich, Click, logging, tracebacks)
    # is appended to this file. Relative paths resolve against the directory
    # that contains config.yaml. See :func:`releasy.termlog.configure`.
    log_file: Path | None = None
    push: bool = False
    # Sequential mode: process the merged-time-sorted PR queue one PR per
    # invocation. Each `releasy run` / `releasy continue` either confirms
    # the previous rebase PR was merged into target_branch and ports the
    # next one, or stops with an error. Incompatible with
    # session ``pr_sources.groups`` (rejected at session-load time).
    sequential: bool = False
    # Optional override for the session file path, read from
    # ``session_file:`` in config.yaml. Relative paths resolve against the
    # config.yaml directory. When None, the default path
    # ``<config-dir>/<target_branch>.session.yaml`` is used (falling back to
    # ``<name>.session.yaml`` when target_branch is unset). See
    # :func:`session_file_path`.
    session_file: Path | None = None
    # Populated by :func:`load_session` when the caller needs the session
    # data (``run``, ``continue``, ``feature *``, …). Stays ``None`` for
    # session-agnostic commands (``status``, ``where``, ``adopt``) and
    # for stateless flows.
    session: SessionConfig | None = None
    # Set by ``--stateless`` flows (``refresh --stateless``, the
    # cherry-pick entry point). Consumers that would otherwise read /
    # write per-project state skip those calls when this is True. Checked
    # via :func:`is_stateless` so the flag lives in one place.
    stateless: bool = False

    # Set by ``--dry-run`` on ``releasy run``. When True, every mutating
    # helper (state writes, repo writes, GitHub writes) short-circuits to
    # a logged no-op so the user can see what the command *would* do
    # against the current state of the world without committing any
    # changes. Read-only GitHub fetches still happen so the plan is
    # accurate.
    dry_run: bool = False

    # Set by ``--ignore-stalls`` on ``releasy run``: re-attempt every parked
    # unit this run, whatever ``pr_policy.honor_stall_reasons`` says.
    ignore_stalls: bool = False

    # Global claude --model / --effort applied to every AI invocation.
    ai_model: str | None = None
    ai_effort: str | None = None

    # How every AI invocation reaches the model:
    #   "cli" (default) — spawn ``<section>.command`` (``claude -p …``)
    #   "api"           — drive the Anthropic API with a token, in-process
    ai_backend: str = "cli"
    ai_api: AIApiConfig = field(default_factory=AIApiConfig)

    @property
    def repo_dir(self) -> Path:
        """Directory containing ``config.yaml``.

        Used as the base for resolving relative paths embedded in config
        (e.g. ``ai_resolve.prompt_file``). Pipeline state and lock files
        no longer live here — see :func:`state_root`.
        """
        return self.config_path.parent

    @property
    def state_path(self) -> Path:
        """Path to this project's state file (under the user's state dir)."""
        return state_file_path(self.name)

    @property
    def lock_path(self) -> Path:
        """Path to this project's lock file (under the user's state dir)."""
        return lock_file_path(self.name)

    def resolve_work_dir(self, cli_override: Path | None = None) -> Path:
        """Resolve the working directory for git clone operations.

        Priority: CLI --work-dir > config work_dir > current directory.
        """
        if cli_override is not None:
            return cli_override.resolve()
        if self.work_dir is not None:
            return self.work_dir.resolve()
        return Path.cwd()

    @property
    def features(self) -> list[FeatureConfig]:
        """Features declared in the loaded session (empty when no session)."""
        if self.session is None:
            return []
        return self.session.features

    @property
    def pr_sources(self) -> PRSourcesConfig:
        """PR selectors from the loaded session (empty struct when no session)."""
        if self.session is None:
            return PRSourcesConfig()
        return self.session.pr_sources

    @property
    def enabled_features(self) -> list[FeatureConfig]:
        return [f for f in self.features if f.enabled]

    def get_feature(self, feature_id: str) -> FeatureConfig | None:
        return next((f for f in self.features if f.id == feature_id), None)

    @property
    def project_name(self) -> str:
        return self.project

    def get_feature_by_branch(self, branch: str, onto: str = "") -> FeatureConfig | None:
        """Match by source_branch or by versioned branch prefix."""
        for f in self.features:
            if f.source_branch == branch:
                return f
            if onto:
                prefix = self.feature_branch_prefix(f.id, onto)
                if branch.startswith(prefix):
                    return f
        return None

    def base_branch_name(self, onto: str) -> str:
        """Build the base branch name.

        If ``target_branch`` is set in config, it is returned as-is.
        Otherwise derived as ``<project>-<version>``:

        v26.3.4.234-lts → antalya-26.3
        <sha> → antalya-<sha8>
        """
        if self.target_branch:
            return self.target_branch
        suffix = extract_version_suffix(onto)
        return f"{self.project_name}-{suffix}"

    def feature_branch_prefix(self, feature_id: str, onto: str) -> str:
        """Build the prefix for feature branches: feature/<base>/<id>"""
        return f"feature/{self.base_branch_name(onto)}/{feature_id}"

    def feature_branch_name(self, feature_id: str, onto: str) -> str:
        """Build a feature branch name: feature/<base>/<id>"""
        return f"feature/{self.base_branch_name(onto)}/{feature_id}"


# ---------------------------------------------------------------------------
# config.yaml I/O
# ---------------------------------------------------------------------------


def _parse_optional_label(value: object, *, key: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"{key} must be a string label name (got {type(value).__name__})")
    stripped = value.strip()
    return stripped or None


def _parse_label_color(value: object, *, key: str, default: str) -> str:
    if value is None:
        return default
    if not isinstance(value, str):
        raise ValueError(f"{key} must be a 6-hex-digit color string (got {type(value).__name__})")
    stripped = value.strip().lstrip("#")
    if not re.fullmatch(r"[0-9A-Fa-f]{6}", stripped):
        raise ValueError(f"{key} must be 6 hex digits (got {value!r})")
    return stripped.upper()


def _parse_ai_api(raw: object) -> AIApiConfig:
    """Parse the ``ai_api:`` block (used when ``ai_backend: api``)."""
    if not isinstance(raw, dict):
        raise ValueError("ai_api must be a mapping")
    defaults = AIApiConfig()
    unknown = set(raw) - {f.name for f in fields(AIApiConfig)}
    if unknown:
        raise ValueError(f"unknown ai_api keys: {sorted(unknown)}")
    # Easy to mix up with ``api_key``: this one holds a variable NAME, and a
    # token pasted here silently resolves to "no token found" at call time.
    api_key_env = str(raw.get("api_key_env") or defaults.api_key_env)
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", api_key_env):
        raise ValueError(
            "ai_api.api_key_env must be the NAME of an environment variable "
            f"(e.g. ANTHROPIC_API_KEY), got {api_key_env[:8]!r}… — to set a "
            "literal token in config use ai_api.api_key instead"
        )
    return AIApiConfig(
        api_key_env=api_key_env,
        api_key=(str(raw["api_key"]) if raw.get("api_key") else None),
        base_url=(str(raw["base_url"]) if raw.get("base_url") else None),
        model=(str(raw["model"]) if raw.get("model") else None),
        max_tokens=int(raw.get("max_tokens", defaults.max_tokens)),
        max_turns=int(raw.get("max_turns", defaults.max_turns)),
        thinking=bool(raw.get("thinking", defaults.thinking)),
        max_retries=int(raw.get("max_retries", defaults.max_retries)),
        request_timeout_seconds=int(
            raw.get("request_timeout_seconds", defaults.request_timeout_seconds)
        ),
        bash_timeout_seconds=int(
            raw.get("bash_timeout_seconds", defaults.bash_timeout_seconds)
        ),
        tool_output_max_chars=int(
            raw.get("tool_output_max_chars", defaults.tool_output_max_chars)
        ),
        system_prompt_extra=str(
            raw.get("system_prompt_extra") or defaults.system_prompt_extra
        ),
    )


def load_config(config_path: Path | None = None) -> Config:
    """Load and validate ``config.yaml``. Does not touch the session file.

    Callers that need feature/PR-source data must also call
    :func:`load_session` — the two files are deliberately independent so
    ``--stateless`` flows can load config without pulling session data.
    """
    if config_path is None:
        config_path = Path.cwd() / "config.yaml"

    config_path = config_path.resolve()

    if not config_path.exists():
        raise FileNotFoundError(f"Config file not found: {config_path}")

    with open(config_path) as f:
        raw = yaml.safe_load(f)

    if not raw:
        raise ValueError("Config file is empty")

    name = raw.get("name")
    if not name:
        raise ValueError(
            "Config must set 'name:' — a unique slug identifying this "
            "project on this machine. It keys the per-project state file "
            f"under {state_root()}/<name>.state.yaml. Pick something "
            "stable like 'antalya-26.3'."
        )
    validate_project_name(name)

    origin = OriginConfig(
        remote=raw["origin"]["remote"],
        remote_name=raw["origin"].get("remote_name", "origin"),
    )

    upstream: UpstreamConfig | None = None
    upstream_raw = raw.get("upstream")
    if upstream_raw is not None:
        if not isinstance(upstream_raw, dict):
            raise ValueError(
                "'upstream' must be a mapping with a 'remote' key "
                f"(got {type(upstream_raw).__name__})"
            )
        upstream_remote = upstream_raw.get("remote")
        if not upstream_remote or not isinstance(upstream_remote, str):
            raise ValueError(
                "upstream.remote is required and must be a string git URL"
            )
        upstream_remote_name = upstream_raw.get("remote_name", "upstream")
        upstream_branch = upstream_raw.get("branch", "master")
        if upstream_remote_name == origin.remote_name:
            raise ValueError(
                f"upstream.remote_name {upstream_remote_name!r} collides with "
                f"origin.remote_name — pick a distinct alias for the upstream "
                "remote so they don't shadow each other in the local clone"
            )
        upstream = UpstreamConfig(
            remote=upstream_remote,
            remote_name=upstream_remote_name,
            branch=upstream_branch,
        )

    project = raw.get("project")
    if not project:
        raise ValueError(
            "Config must set 'project' (e.g. 'antalya'). "
            "This is used to name the base and port branches."
        )

    pp_raw = raw.get("pr_policy", {}) or {}
    if not isinstance(pp_raw, dict):
        raise ValueError(
            f"pr_policy must be a mapping, got {type(pp_raw).__name__}"
        )
    pp_if_exists = pp_raw.get("if_exists", "skip")
    if pp_if_exists not in _VALID_IF_EXISTS:
        raise ValueError(
            f"pr_policy.if_exists must be one of {_VALID_IF_EXISTS}, "
            f"got {pp_if_exists!r}"
        )
    pr_policy = PRPolicyConfig(
        if_exists=pp_if_exists,
        auto_pr=bool(pp_raw.get("auto_pr", True)),
        retry_failed=bool(pp_raw.get("retry_failed", True)),
        recreate_closed_prs=bool(pp_raw.get("recreate_closed_prs", False)),
        detect_superseded=bool(pp_raw.get("detect_superseded", True)),
        max_partial_continue_attempts=int(
            pp_raw.get("max_partial_continue_attempts", 2) or 0
        ),
        honor_stall_reasons=bool(pp_raw.get("honor_stall_reasons", True)),
    )

    notif_raw = raw.get("notifications", {}) or {}

    def _opt_list(key: str, fallback: list[str]) -> list[str]:
        v = notif_raw.get(key)
        if v is None:
            return list(fallback)
        if not isinstance(v, list) or not all(isinstance(x, str) for x in v):
            raise ValueError(
                f"notifications.{key} must be a list of strings, got {v!r}"
            )
        # Strip empties / dedupe while preserving order — keeps the option
        # set on the GitHub Project board predictable even when users edit
        # the YAML by hand and accidentally duplicate or comment out an
        # entry by leaving it blank.
        seen: set[str] = set()
        out: list[str] = []
        for s in v:
            stripped = s.strip()
            if not stripped or stripped in seen:
                continue
            seen.add(stripped)
            out.append(stripped)
        return out

    raw_login_map = notif_raw.get("assignee_dev_login_map")
    if raw_login_map is None:
        login_map = _default_assignee_dev_login_map()
    else:
        if not isinstance(raw_login_map, dict):
            raise ValueError(
                "notifications.assignee_dev_login_map must be a "
                f"mapping (got {type(raw_login_map).__name__})"
            )
        # Preserve the user's original casing in config.yaml so save_config
        # round-trips don't silently rewrite ``Enmk`` to ``enmk``. Lookups
        # are case-insensitive — see how ``sync_project`` builds a
        # ``login_map_lc`` view at call time.
        login_map = {}
        seen_keys_lc: dict[str, str] = {}
        for k, v in raw_login_map.items():
            if not isinstance(k, str) or not isinstance(v, str):
                raise ValueError(
                    "notifications.assignee_dev_login_map entries must "
                    "be string→string"
                )
            key = k.strip()
            val = v.strip()
            if not key:
                continue
            key_lc = key.lower()
            if key_lc in seen_keys_lc:
                raise ValueError(
                    "notifications.assignee_dev_login_map has duplicate "
                    f"login (case-insensitive): {seen_keys_lc[key_lc]!r} "
                    f"and {key!r}"
                )
            seen_keys_lc[key_lc] = key
            login_map[key] = val

    notifications = NotificationsConfig(
        github_project=notif_raw.get("github_project"),
        assignee_dev_options=_opt_list(
            "assignee_dev_options", _default_assignee_dev_options(),
        ),
        assignee_qa_options=_opt_list(
            "assignee_qa_options", _default_assignee_qa_options(),
        ),
        assignee_dev_login_map=login_map,
    )

    ai_changelog_raw = raw.get("ai_changelog", {}) or {}
    ai_changelog = AIChangelogConfig(
        enabled=bool(ai_changelog_raw.get("enabled", False)),
        command=ai_changelog_raw.get("command", "claude"),
        prompt_file=ai_changelog_raw.get(
            "prompt_file", "prompts/synthesize_changelog.md",
        ),
        timeout_seconds=int(ai_changelog_raw.get("timeout_seconds", 300)),
        max_pr_body_chars=int(ai_changelog_raw.get("max_pr_body_chars", 3000)),
    )

    ai_raw = raw.get("ai_resolve", {}) or {}

    # `auto_add_prerequisite_prs` accepts either:
    #   - a bool (sugar: `false` → {enabled: false}, `true` → {enabled: true})
    #   - a mapping {enabled: bool, max_prereq_depth: int}
    # The bool form is the convenience syntax for users who don't need to
    # touch max_prereq_depth; the mapping is the canonical write-back shape.
    auto_prereq_raw = ai_raw.get("auto_add_prerequisite_prs")
    if auto_prereq_raw is None:
        auto_add_prerequisite_prs = AutoAddPrerequisitePRsConfig()
    elif isinstance(auto_prereq_raw, bool):
        auto_add_prerequisite_prs = AutoAddPrerequisitePRsConfig(
            enabled=auto_prereq_raw,
        )
    elif isinstance(auto_prereq_raw, dict):
        auto_add_prerequisite_prs = AutoAddPrerequisitePRsConfig(
            enabled=bool(auto_prereq_raw.get("enabled", False)),
            max_prereq_depth=int(auto_prereq_raw.get("max_prereq_depth", 7)),
            require_origin_prereq_label=bool(
                auto_prereq_raw.get("require_origin_prereq_label", True)
            ),
        )
    else:
        raise ValueError(
            "ai_resolve.auto_add_prerequisite_prs must be a bool or mapping, "
            f"got {type(auto_prereq_raw).__name__}"
        )
    if auto_add_prerequisite_prs.max_prereq_depth < 0:
        raise ValueError(
            "ai_resolve.auto_add_prerequisite_prs.max_prereq_depth must be "
            f">= 0, got {auto_add_prerequisite_prs.max_prereq_depth}"
        )

    ai_resolve = AIResolveConfig(
        enabled=ai_raw.get("enabled", False),
        command=ai_raw.get("command", "claude"),
        prompt_file=ai_raw.get("prompt_file", "prompts/resolve_conflict.md"),
        merge_prompt_file=ai_raw.get(
            "merge_prompt_file", "prompts/resolve_merge_conflict.md",
        ),
        split_prompt_file=ai_raw.get(
            "split_prompt_file", "prompts/resolve_conflict_split.md",
        ),
        split_conflict_commit=bool(ai_raw.get("split_conflict_commit", True)),
        allowed_tools=ai_raw.get("allowed_tools") or _default_allowed_tools(),
        max_iterations=int(ai_raw.get("max_iterations", 5)),
        timeout_seconds=int(ai_raw.get("timeout_seconds", 7200)),
        build_command=ai_raw.get("build_command", "cd build && ninja"),
        deterministic_build=bool(ai_raw.get("deterministic_build", True)),
        max_build_attempts=int(ai_raw.get("max_build_attempts", 5)),
        max_verify_resume_attempts=int(
            ai_raw.get("max_verify_resume_attempts", 2) or 0
        ),
        max_resume_base_drift=int(
            ai_raw.get("max_resume_base_drift", 50) or 0
        ),
        max_verify_iterations=int(ai_raw.get("max_verify_iterations", 12)),
        build_log_tail_lines=int(ai_raw.get("build_log_tail_lines", 500)),
        build_timeout_seconds=int(ai_raw.get("build_timeout_seconds", 7200)),
        run_pr_tests=bool(ai_raw.get("run_pr_tests", True)),
        test_file_globs=ai_raw.get("test_file_globs") or _default_test_file_globs(),
        test_timeout_seconds=int(ai_raw.get("test_timeout_seconds", 3600)),
        resolve_only_prompt_file=ai_raw.get(
            "resolve_only_prompt_file", "prompts/resolve_conflict_nobuild.md",
        ),
        fix_build_prompt_file=ai_raw.get(
            "fix_build_prompt_file", "prompts/fix_build.md",
        ),
        run_tests_prompt_file=ai_raw.get(
            "run_tests_prompt_file", "prompts/run_tests.md",
        ),
        label=ai_raw.get("label", "ai-resolved"),
        label_color=ai_raw.get("label_color", "8B5CF6"),
        needs_attention_label=ai_raw.get(
            "needs_attention_label", "ai-needs-attention",
        ),
        needs_attention_label_color=ai_raw.get(
            "needs_attention_label_color", "D93F0B",
        ),
        missing_prereqs_label=ai_raw.get(
            "missing_prereqs_label", "missing-prerequisites",
        ),
        missing_prereqs_label_color=ai_raw.get(
            "missing_prereqs_label_color", "E4E669",
        ),
        auto_prereq_label=ai_raw.get(
            "auto_prereq_label", "auto-prereq-added",
        ),
        auto_prereq_label_color=ai_raw.get(
            "auto_prereq_label_color", "0E8A16",
        ),
        verify_resolution=bool(ai_raw.get("verify_resolution", False)),
        verify_prompt_file=ai_raw.get(
            "verify_prompt_file", "prompts/verify_resolution.md",
        ),
        verify_timeout_seconds=int(ai_raw.get("verify_timeout_seconds", 1800)),
        verify_label=ai_raw.get("verify_label", "ai-needs-verify"),
        verify_label_color=ai_raw.get("verify_label_color", "FBCA04"),
        extra_args=ai_raw.get("extra_args", []) or [],
        api_retries=int(ai_raw.get("api_retries", 3)),
        api_retry_backoff_seconds=int(ai_raw.get("api_retry_backoff_seconds", 15)),
        wait_on_session_exhaustion=bool(
            ai_raw.get("wait_on_session_exhaustion", True)
        ),
        session_exhaustion_max_wait_hours=int(
            ai_raw.get("session_exhaustion_max_wait_hours", 60)
        ),
        session_exhaustion_poll_minutes=int(
            ai_raw.get("session_exhaustion_poll_minutes", 30)
        ),
        session_exhaustion_extra_patterns=list(
            ai_raw.get("session_exhaustion_extra_patterns", []) or []
        ),
        postcondition_retries=int(ai_raw.get("postcondition_retries", 2)),
        warn_on_unfixed_postconditions=bool(
            ai_raw.get("warn_on_unfixed_postconditions", True)
        ),
        auto_add_prerequisite_prs=auto_add_prerequisite_prs,
    )

    rr_raw = raw.get("review_response", {}) or {}
    rr_reviewers_raw = rr_raw.get("trusted_reviewers", []) or []
    if not isinstance(rr_reviewers_raw, list) or not all(
        isinstance(x, str) for x in rr_reviewers_raw
    ):
        raise ValueError(
            "review_response.trusted_reviewers must be a list of strings"
        )
    rr_seen: set[str] = set()
    rr_reviewers: list[str] = []
    for login in rr_reviewers_raw:
        stripped = login.strip()
        key = stripped.lower()
        if not stripped or key in rr_seen:
            continue
        rr_seen.add(key)
        rr_reviewers.append(stripped)
    _rr_default_assocs = ["OWNER", "MEMBER", "COLLABORATOR", "CONTRIBUTOR"]
    rr_assocs_raw = rr_raw.get("trusted_associations", _rr_default_assocs)
    if not isinstance(rr_assocs_raw, list) or not all(
        isinstance(x, str) for x in rr_assocs_raw
    ):
        raise ValueError(
            "review_response.trusted_associations must be a list of strings"
        )
    rr_assoc_seen: set[str] = set()
    rr_assocs: list[str] = []
    for a in rr_assocs_raw:
        up = a.strip().upper()
        if not up or up in rr_assoc_seen:
            continue
        rr_assoc_seen.add(up)
        rr_assocs.append(up)
    review_response = ReviewResponseConfig(
        command=rr_raw.get("command", "claude"),
        prompt_file=rr_raw.get("prompt_file", "prompts/address_review.md"),
        timeout_seconds=int(rr_raw.get("timeout_seconds", 7200)),
        max_iterations=int(rr_raw.get("max_iterations", 15)),
        trusted_associations=rr_assocs,
        trusted_reviewers=rr_reviewers,
        reply_to_non_addressable=bool(
            rr_raw.get("reply_to_non_addressable", True),
        ),
        post_summary_comment=bool(rr_raw.get("post_summary_comment", False)),
        allowed_tools=rr_raw.get("allowed_tools") or _default_allowed_tools(),
        extra_args=rr_raw.get("extra_args", []) or [],
    )

    af_raw = raw.get("analyze_fails", {}) or {}
    af_categories = af_raw.get("categories", []) or []
    if not isinstance(af_categories, list) or not all(
        isinstance(x, str) for x in af_categories
    ):
        raise ValueError(
            "analyze_fails.categories must be a list of strings"
        )
    analyze_fails = AnalyzeFailsConfig(
        command=af_raw.get("command", "claude"),
        prompt_file=af_raw.get(
            "prompt_file", "prompts/analyze_fails.md",
        ),
        categories=[c.strip() for c in af_categories if c.strip()],
        timeout_seconds=int(af_raw.get("timeout_seconds", 7200)),
        max_iterations=int(af_raw.get("max_iterations", 6)),
        max_prs_per_run=int(af_raw.get("max_prs_per_run", 0)),
        flaky_elsewhere_threshold=int(
            af_raw.get("flaky_elsewhere_threshold", 2),
        ),
        flaky_check_prs=int(af_raw.get("flaky_check_prs", 12)),
        post_comment_to_pr=bool(af_raw.get("post_comment_to_pr", True)),
        allowed_tools=(
            af_raw.get("allowed_tools")
            or _default_analyze_fails_allowed_tools()
        ),
        extra_args=af_raw.get("extra_args", []) or [],
    )

    gr_raw = raw.get("graph", {}) or {}
    gr_reviewers_raw = gr_raw.get("trusted_reviewers", []) or []
    if not isinstance(gr_reviewers_raw, list) or not all(
        isinstance(x, str) for x in gr_reviewers_raw
    ):
        raise ValueError("graph.trusted_reviewers must be a list of strings")
    gr_seen: set[str] = set()
    gr_reviewers: list[str] = []
    for login in gr_reviewers_raw:
        stripped = login.strip()
        key = stripped.lower()
        if not stripped or key in gr_seen:
            continue
        gr_seen.add(key)
        gr_reviewers.append(stripped)
    _gr_default_assocs = ["OWNER", "MEMBER", "COLLABORATOR"]
    gr_assocs_raw = gr_raw.get("trusted_associations", _gr_default_assocs)
    if not isinstance(gr_assocs_raw, list) or not all(
        isinstance(x, str) for x in gr_assocs_raw
    ):
        raise ValueError("graph.trusted_associations must be a list of strings")
    gr_assoc_seen: set[str] = set()
    gr_assocs: list[str] = []
    for a in gr_assocs_raw:
        up = a.strip().upper()
        if not up or up in gr_assoc_seen:
            continue
        gr_assoc_seen.add(up)
        gr_assocs.append(up)
    gr_labels_raw = gr_raw.get("issue_labels", ["releasy"])
    if not isinstance(gr_labels_raw, list) or not all(
        isinstance(x, str) for x in gr_labels_raw
    ):
        raise ValueError("graph.issue_labels must be a list of strings")
    graph = GraphConfig(
        command=gr_raw.get("command", "claude"),
        prompt_file=gr_raw.get("prompt_file", "prompts/adjust_graph.md"),
        timeout_seconds=int(gr_raw.get("timeout_seconds", 7200)),
        trusted_associations=gr_assocs,
        trusted_reviewers=gr_reviewers,
        issue_labels=[s.strip() for s in gr_labels_raw if s.strip()],
        post_comment=bool(gr_raw.get("post_comment", True)),
        sync_progress=bool(gr_raw.get("sync_progress", True)),
        apply_exclusions=bool(gr_raw.get("apply_exclusions", True)),
        minimize_addressed_comments=bool(
            gr_raw.get("minimize_addressed_comments", True)
        ),
        extra_args=gr_raw.get("extra_args", []) or [],
    )

    raw_work_dir = raw.get("work_dir")
    work_dir = Path(raw_work_dir).resolve() if raw_work_dir else None

    raw_log = raw.get("log_file")
    log_file: Path | None = None
    if raw_log is not None:
        if not isinstance(raw_log, str) or not raw_log.strip():
            raise ValueError(
                "log_file: must be a non-empty string path when set "
                f"(got {type(raw_log).__name__!r})"
            )
        lp = Path(raw_log).expanduser()
        if not lp.is_absolute():
            lp = (config_path.parent / lp).resolve()
        else:
            lp = lp.resolve()
        log_file = lp

    raw_session_file = raw.get("session_file")
    session_file: Path | None = None
    if raw_session_file is not None:
        if not isinstance(raw_session_file, str) or not raw_session_file.strip():
            raise ValueError(
                "session_file: must be a non-empty string path when set "
                f"(got {type(raw_session_file).__name__!r})"
            )
        # Store as-given; session_file_path() handles relative resolution
        # against the config dir so the stored value remains portable.
        session_file = Path(raw_session_file).expanduser()

    sequential = bool(raw.get("sequential", False))

    ai_model = raw.get("ai_model") or None
    if ai_model is not None and not isinstance(ai_model, str):
        raise ValueError("ai_model must be a string")
    ai_effort = raw.get("ai_effort") or None
    if ai_effort is not None and ai_effort not in _VALID_EFFORTS:
        raise ValueError(
            f"ai_effort must be one of {_VALID_EFFORTS}, got {ai_effort!r}"
        )

    ai_backend = str(raw.get("ai_backend") or "cli").strip().lower()
    if ai_backend not in _VALID_AI_BACKENDS:
        raise ValueError(
            f"ai_backend must be one of {_VALID_AI_BACKENDS}, "
            f"got {ai_backend!r}"
        )
    ai_api = _parse_ai_api(raw.get("ai_api") or {})

    from releasy.termlog import configure as _configure_term_log

    cfg = Config(
        name=name,
        origin=origin,
        upstream=upstream,
        project=project,
        target_branch=raw.get("target_branch") or None,
        update_existing_prs=bool(raw.get("update_existing_prs", False)),
        merged_label=_parse_optional_label(
            raw.get("merged_label"), key="merged_label",
        ),
        merged_label_color=_parse_label_color(
            raw.get("merged_label_color"),
            key="merged_label_color",
            default="8B5CF6",
        ),
        pr_policy=pr_policy,
        notifications=notifications,
        ai_resolve=ai_resolve,
        ai_changelog=ai_changelog,
        review_response=review_response,
        analyze_fails=analyze_fails,
        graph=graph,
        config_path=config_path,
        work_dir=work_dir,
        log_file=log_file,
        push=raw.get("push", False),
        sequential=sequential,
        session_file=session_file,
        ai_model=ai_model,
        ai_effort=ai_effort,
        ai_backend=ai_backend,
        ai_api=ai_api,
    )
    _configure_term_log(log_file)
    return cfg


def save_config(config: Config, config_path: Path | None = None) -> None:
    """Persist ``config.yaml`` (infrastructure + policy only).

    Session data (``features``, ``pr_sources``) is written by
    :func:`save_session` — the two files round-trip independently.
    """
    if config_path is None:
        config_path = config.config_path

    data: dict = {
        "name": config.name,
        "origin": {
            "remote": config.origin.remote,
            "remote_name": config.origin.remote_name,
        },
        "project": config.project,
    }

    if config.upstream is not None:
        data["upstream"] = {
            "remote": config.upstream.remote,
            "remote_name": config.upstream.remote_name,
            "branch": config.upstream.branch,
        }

    if config.target_branch:
        data["target_branch"] = config.target_branch

    if config.update_existing_prs:
        data["update_existing_prs"] = True

    if config.merged_label:
        data["merged_label"] = config.merged_label
        if config.merged_label_color != "8B5CF6":
            data["merged_label_color"] = config.merged_label_color

    if config.work_dir:
        data["work_dir"] = str(config.work_dir)

    if config.log_file is not None:
        try:
            rel = config.log_file.relative_to(config.config_path.parent)
            data["log_file"] = str(rel)
        except ValueError:
            data["log_file"] = str(config.log_file)

    if config.session_file is not None:
        data["session_file"] = str(config.session_file)

    pp = config.pr_policy
    pp_defaults = PRPolicyConfig()
    pp_data: dict = {}
    if pp.if_exists != pp_defaults.if_exists:
        pp_data["if_exists"] = pp.if_exists
    if pp.auto_pr != pp_defaults.auto_pr:
        pp_data["auto_pr"] = pp.auto_pr
    if pp.retry_failed != pp_defaults.retry_failed:
        pp_data["retry_failed"] = pp.retry_failed
    if pp.recreate_closed_prs != pp_defaults.recreate_closed_prs:
        pp_data["recreate_closed_prs"] = pp.recreate_closed_prs
    if pp.detect_superseded != pp_defaults.detect_superseded:
        pp_data["detect_superseded"] = pp.detect_superseded
    if (
        pp.max_partial_continue_attempts
        != pp_defaults.max_partial_continue_attempts
    ):
        pp_data["max_partial_continue_attempts"] = (
            pp.max_partial_continue_attempts
        )
    if pp.honor_stall_reasons != pp_defaults.honor_stall_reasons:
        pp_data["honor_stall_reasons"] = pp.honor_stall_reasons
    if pp_data:
        data["pr_policy"] = pp_data

    notif_data: dict = {}
    if config.notifications.github_project:
        notif_data["github_project"] = config.notifications.github_project
    if config.notifications.assignee_dev_options != _default_assignee_dev_options():
        notif_data["assignee_dev_options"] = config.notifications.assignee_dev_options
    if config.notifications.assignee_qa_options != _default_assignee_qa_options():
        notif_data["assignee_qa_options"] = config.notifications.assignee_qa_options
    if config.notifications.assignee_dev_login_map != _default_assignee_dev_login_map():
        notif_data["assignee_dev_login_map"] = (
            config.notifications.assignee_dev_login_map
        )
    if notif_data:
        data["notifications"] = notif_data

    if config.ai_backend != "cli":
        data["ai_backend"] = config.ai_backend
    api_defaults = AIApiConfig()
    api_data = {
        f.name: getattr(config.ai_api, f.name)
        for f in fields(AIApiConfig)
        if getattr(config.ai_api, f.name) != getattr(api_defaults, f.name)
    }
    if api_data:
        data["ai_api"] = api_data

    ai = config.ai_resolve
    ai_defaults = AIResolveConfig()
    ai_data: dict = {}
    if ai.enabled != ai_defaults.enabled:
        ai_data["enabled"] = ai.enabled
    if ai.command != ai_defaults.command:
        ai_data["command"] = ai.command
    if ai.prompt_file != ai_defaults.prompt_file:
        ai_data["prompt_file"] = ai.prompt_file
    if ai.merge_prompt_file != ai_defaults.merge_prompt_file:
        ai_data["merge_prompt_file"] = ai.merge_prompt_file
    if ai.split_prompt_file != ai_defaults.split_prompt_file:
        ai_data["split_prompt_file"] = ai.split_prompt_file
    if ai.split_conflict_commit != ai_defaults.split_conflict_commit:
        ai_data["split_conflict_commit"] = ai.split_conflict_commit
    if ai.allowed_tools != _default_allowed_tools():
        ai_data["allowed_tools"] = ai.allowed_tools
    if ai.max_iterations != ai_defaults.max_iterations:
        ai_data["max_iterations"] = ai.max_iterations
    if ai.timeout_seconds != ai_defaults.timeout_seconds:
        ai_data["timeout_seconds"] = ai.timeout_seconds
    if ai.build_command != ai_defaults.build_command:
        ai_data["build_command"] = ai.build_command
    if ai.deterministic_build != ai_defaults.deterministic_build:
        ai_data["deterministic_build"] = ai.deterministic_build
    if ai.max_build_attempts != ai_defaults.max_build_attempts:
        ai_data["max_build_attempts"] = ai.max_build_attempts
    if ai.max_verify_resume_attempts != ai_defaults.max_verify_resume_attempts:
        ai_data["max_verify_resume_attempts"] = ai.max_verify_resume_attempts
    if ai.max_resume_base_drift != ai_defaults.max_resume_base_drift:
        ai_data["max_resume_base_drift"] = ai.max_resume_base_drift
    if ai.max_verify_iterations != ai_defaults.max_verify_iterations:
        ai_data["max_verify_iterations"] = ai.max_verify_iterations
    if ai.build_log_tail_lines != ai_defaults.build_log_tail_lines:
        ai_data["build_log_tail_lines"] = ai.build_log_tail_lines
    if ai.build_timeout_seconds != ai_defaults.build_timeout_seconds:
        ai_data["build_timeout_seconds"] = ai.build_timeout_seconds
    if ai.run_pr_tests != ai_defaults.run_pr_tests:
        ai_data["run_pr_tests"] = ai.run_pr_tests
    if ai.test_file_globs != _default_test_file_globs():
        ai_data["test_file_globs"] = ai.test_file_globs
    if ai.test_timeout_seconds != ai_defaults.test_timeout_seconds:
        ai_data["test_timeout_seconds"] = ai.test_timeout_seconds
    if ai.resolve_only_prompt_file != ai_defaults.resolve_only_prompt_file:
        ai_data["resolve_only_prompt_file"] = ai.resolve_only_prompt_file
    if ai.fix_build_prompt_file != ai_defaults.fix_build_prompt_file:
        ai_data["fix_build_prompt_file"] = ai.fix_build_prompt_file
    if ai.run_tests_prompt_file != ai_defaults.run_tests_prompt_file:
        ai_data["run_tests_prompt_file"] = ai.run_tests_prompt_file
    if ai.label != ai_defaults.label:
        ai_data["label"] = ai.label
    if ai.label_color != ai_defaults.label_color:
        ai_data["label_color"] = ai.label_color
    if ai.needs_attention_label != ai_defaults.needs_attention_label:
        ai_data["needs_attention_label"] = ai.needs_attention_label
    if ai.needs_attention_label_color != ai_defaults.needs_attention_label_color:
        ai_data["needs_attention_label_color"] = ai.needs_attention_label_color
    if ai.missing_prereqs_label != ai_defaults.missing_prereqs_label:
        ai_data["missing_prereqs_label"] = ai.missing_prereqs_label
    if ai.missing_prereqs_label_color != ai_defaults.missing_prereqs_label_color:
        ai_data["missing_prereqs_label_color"] = ai.missing_prereqs_label_color
    if ai.auto_prereq_label != ai_defaults.auto_prereq_label:
        ai_data["auto_prereq_label"] = ai.auto_prereq_label
    if ai.auto_prereq_label_color != ai_defaults.auto_prereq_label_color:
        ai_data["auto_prereq_label_color"] = ai.auto_prereq_label_color
    if ai.verify_resolution != ai_defaults.verify_resolution:
        ai_data["verify_resolution"] = ai.verify_resolution
    if ai.verify_prompt_file != ai_defaults.verify_prompt_file:
        ai_data["verify_prompt_file"] = ai.verify_prompt_file
    if ai.verify_timeout_seconds != ai_defaults.verify_timeout_seconds:
        ai_data["verify_timeout_seconds"] = ai.verify_timeout_seconds
    if ai.verify_label != ai_defaults.verify_label:
        ai_data["verify_label"] = ai.verify_label
    if ai.verify_label_color != ai_defaults.verify_label_color:
        ai_data["verify_label_color"] = ai.verify_label_color
    if ai.extra_args:
        ai_data["extra_args"] = ai.extra_args
    if ai.api_retries != ai_defaults.api_retries:
        ai_data["api_retries"] = ai.api_retries
    if ai.api_retry_backoff_seconds != ai_defaults.api_retry_backoff_seconds:
        ai_data["api_retry_backoff_seconds"] = ai.api_retry_backoff_seconds
    if ai.wait_on_session_exhaustion != ai_defaults.wait_on_session_exhaustion:
        ai_data["wait_on_session_exhaustion"] = ai.wait_on_session_exhaustion
    if ai.session_exhaustion_max_wait_hours != ai_defaults.session_exhaustion_max_wait_hours:
        ai_data["session_exhaustion_max_wait_hours"] = ai.session_exhaustion_max_wait_hours
    if ai.session_exhaustion_poll_minutes != ai_defaults.session_exhaustion_poll_minutes:
        ai_data["session_exhaustion_poll_minutes"] = ai.session_exhaustion_poll_minutes
    if ai.session_exhaustion_extra_patterns:
        ai_data["session_exhaustion_extra_patterns"] = ai.session_exhaustion_extra_patterns
    if ai.postcondition_retries != ai_defaults.postcondition_retries:
        ai_data["postcondition_retries"] = ai.postcondition_retries
    if (
        ai.warn_on_unfixed_postconditions
        != ai_defaults.warn_on_unfixed_postconditions
    ):
        ai_data["warn_on_unfixed_postconditions"] = (
            ai.warn_on_unfixed_postconditions
        )
    auto_prereq_defaults = AutoAddPrerequisitePRsConfig()
    if (
        ai.auto_add_prerequisite_prs.enabled != auto_prereq_defaults.enabled
        or ai.auto_add_prerequisite_prs.max_prereq_depth
        != auto_prereq_defaults.max_prereq_depth
        or ai.auto_add_prerequisite_prs.require_origin_prereq_label
        != auto_prereq_defaults.require_origin_prereq_label
    ):
        # Always emit the canonical mapping form on write-back so the on-disk
        # config doesn't quietly switch shapes between runs even when the
        # user wrote it as a bare bool.
        ai_data["auto_add_prerequisite_prs"] = {
            "enabled": ai.auto_add_prerequisite_prs.enabled,
            "max_prereq_depth": ai.auto_add_prerequisite_prs.max_prereq_depth,
            "require_origin_prereq_label":
                ai.auto_add_prerequisite_prs.require_origin_prereq_label,
        }
    if ai_data:
        data["ai_resolve"] = ai_data

    aic = config.ai_changelog
    aic_defaults = AIChangelogConfig()
    aic_data: dict = {}
    if aic.enabled != aic_defaults.enabled:
        aic_data["enabled"] = aic.enabled
    if aic.command != aic_defaults.command:
        aic_data["command"] = aic.command
    if aic.prompt_file != aic_defaults.prompt_file:
        aic_data["prompt_file"] = aic.prompt_file
    if aic.timeout_seconds != aic_defaults.timeout_seconds:
        aic_data["timeout_seconds"] = aic.timeout_seconds
    if aic.max_pr_body_chars != aic_defaults.max_pr_body_chars:
        aic_data["max_pr_body_chars"] = aic.max_pr_body_chars
    if aic_data:
        data["ai_changelog"] = aic_data

    rr = config.review_response
    rr_defaults = ReviewResponseConfig()
    rr_data: dict = {}
    if rr.command != rr_defaults.command:
        rr_data["command"] = rr.command
    if rr.prompt_file != rr_defaults.prompt_file:
        rr_data["prompt_file"] = rr.prompt_file
    if rr.timeout_seconds != rr_defaults.timeout_seconds:
        rr_data["timeout_seconds"] = rr.timeout_seconds
    if rr.max_iterations != rr_defaults.max_iterations:
        rr_data["max_iterations"] = rr.max_iterations
    if rr.trusted_associations != rr_defaults.trusted_associations:
        rr_data["trusted_associations"] = rr.trusted_associations
    if rr.trusted_reviewers:
        rr_data["trusted_reviewers"] = rr.trusted_reviewers
    if rr.reply_to_non_addressable != rr_defaults.reply_to_non_addressable:
        rr_data["reply_to_non_addressable"] = rr.reply_to_non_addressable
    if rr.post_summary_comment != rr_defaults.post_summary_comment:
        rr_data["post_summary_comment"] = rr.post_summary_comment
    if rr.allowed_tools != _default_allowed_tools():
        rr_data["allowed_tools"] = rr.allowed_tools
    if rr.extra_args:
        rr_data["extra_args"] = rr.extra_args
    if rr_data:
        data["review_response"] = rr_data

    if config.push:
        data["push"] = True

    if config.sequential:
        data["sequential"] = True

    with open(config_path, "w") as f:
        yaml.dump(data, f, default_flow_style=False, sort_keys=False)


# ---------------------------------------------------------------------------
# session.yaml I/O
# ---------------------------------------------------------------------------


def default_session_stem(name: str, target_branch: str | None) -> str:
    """Filesystem-safe stem for the default session filename.

    Prefers ``target_branch`` — when one config directory drives several
    porting efforts it is usually the most distinguishing identifier — and
    falls back to the project ``name``. Characters outside the name-slug
    set (notably ``/`` in branch names like ``feature/x``) collapse to
    ``-`` so the result is always a valid filename.
    """
    raw = (target_branch or "").strip() or name
    stem = re.sub(r"[^A-Za-z0-9._-]+", "-", raw).strip("-")
    return stem or name


def session_file_path(
    config: Config, cli_override: Path | None = None,
) -> Path:
    """Resolve the on-disk path of this project's session file.

    Priority:

    1. ``cli_override`` — ``--session-file`` passed on the command line.
    2. ``config.session_file`` — the ``session_file:`` key in ``config.yaml``
       (relative paths resolve against the config dir).
    3. Default: ``<config-dir>/<stem>.session.yaml`` where ``<stem>`` is the
       ``target_branch`` (filesystem-sanitised) when set, else the project
       ``name``.
    """
    if cli_override is not None:
        return Path(cli_override).expanduser().resolve()
    if config.session_file is not None:
        p = Path(config.session_file).expanduser()
        if not p.is_absolute():
            p = (config.config_path.parent / p).resolve()
        else:
            p = p.resolve()
        return p
    stem = default_session_stem(config.name, config.target_branch)
    return (config.config_path.parent / f"{stem}.session.yaml").resolve()


def lookup_pr_ai_context(
    pr_sources: PRSourcesConfig, pr_url: str,
) -> str:
    """Resolve the user-supplied ``ai_context`` for ``pr_url``.

    Walks the session's PR sources and returns the combined ai_context
    (joined with blank lines) found for this URL. Used by code paths that
    don't have a ``FeatureUnit`` handy (``releasy refresh``,
    ``releasy rebase``) to feed per-PR / per-group context into the AI
    resolver prompt.

    Combination rules:

    * If ``pr_url`` is in ``include_prs`` and has a per-entry ai_context,
      that context is included.
    * If ``pr_url`` is in any group's ``prs``, both the group-level
      ``ai_context`` and the per-PR ai_context (when present) are
      included.

    Empty strings are dropped; the result is ``""`` when nothing matched.
    Label-based ai_context is not consulted here since matching requires
    fetched PR labels.
    """
    parts: list[str] = []

    if pr_url in pr_sources.include_pr_contexts:
        ctx = pr_sources.include_pr_contexts[pr_url]
        if ctx:
            parts.append(ctx)

    for group in pr_sources.groups:
        if pr_url not in group.prs:
            continue
        if group.ai_context:
            parts.append(group.ai_context)
        per_pr = group.pr_ai_contexts.get(pr_url, "")
        if per_pr:
            parts.append(per_pr)
        # A PR can only appear in one group (enforced at load time), so
        # break here to avoid scanning the rest.
        break

    return "\n\n".join(parts)


def _parse_pr_url_entries(
    raw: list, *, where: str,
) -> tuple[list[str], dict[str, str]]:
    """Parse a YAML list whose entries are either bare URL strings or
    ``{url: ..., ai_context: ...}`` dicts.

    Returns ``(urls, contexts)`` where ``urls`` preserves input order and
    ``contexts`` maps URL → ai_context for entries that supplied one (dict
    entries with no ``ai_context`` and bare-string entries do not appear in
    the contexts map).

    Both forms can be mixed freely in a single list. ``where`` is the
    user-facing path used in error messages (e.g.
    ``"pr_sources.include_prs"``).
    """
    if not isinstance(raw, list):
        raise ValueError(f"{where} must be a list, got {type(raw).__name__}")
    urls: list[str] = []
    contexts: dict[str, str] = {}
    seen: set[str] = set()
    for idx, entry in enumerate(raw):
        if isinstance(entry, str):
            url = entry.strip()
            ai_context = ""
        elif isinstance(entry, dict):
            url = (entry.get("url") or "").strip()
            if not url:
                raise ValueError(
                    f"{where}[{idx}]: dict entry must specify 'url'"
                )
            ai_context = (entry.get("ai_context") or "").strip()
            extra = set(entry.keys()) - {"url", "ai_context"}
            if extra:
                raise ValueError(
                    f"{where}[{idx}]: unknown keys {sorted(extra)} "
                    "(allowed: 'url', 'ai_context')"
                )
        else:
            raise ValueError(
                f"{where}[{idx}]: must be a URL string or "
                f"{{url, ai_context}} mapping, got {type(entry).__name__}"
            )
        if url in seen:
            raise ValueError(f"{where}: duplicate URL {url!r}")
        seen.add(url)
        urls.append(url)
        if ai_context:
            contexts[url] = ai_context
    return urls, contexts


def resolve_deps_file_path(
    session_path: Path, deps_file: str | None,
) -> Path:
    """Resolve the effective deps overlay path for a session.

    * When ``pr_sources.deps_file`` is set, return that (relative paths
      resolve against the session file's directory; absolute paths stay
      absolute).
    * Otherwise fall back to the convention default
      ``<session-stem>.deps.yaml`` next to the session file. This makes
      ``releasy graph discover`` work zero-config: just run it and a
      deps file appears alongside the session for the loader to pick
      up on the next ``releasy run``.

    Always returns a path. Whether the file *exists* is a separate
    question the caller checks when relevant.
    """
    if deps_file:
        p = Path(deps_file)
        if not p.is_absolute():
            p = (session_path.parent / p).resolve()
        return p
    # Convention default: strip the trailing .yaml/.yml from the session
    # file's name once and append .deps.yaml in the same directory.
    name = session_path.name
    for suffix in (".yaml", ".yml"):
        if name.endswith(suffix):
            stem = name[: -len(suffix)]
            return session_path.with_name(f"{stem}.deps.yaml")
    return session_path.with_name(f"{name}.deps.yaml")


def load_session(
    config: Config, cli_override: Path | None = None,
) -> SessionConfig:
    """Load and validate the session file for ``config``.

    Raises :class:`FileNotFoundError` when the file does not exist —
    callers decide whether to scaffold one or surface the error. Per-source
    ``if_exists`` overrides default to ``config.pr_policy.if_exists`` when
    omitted, and ``sequential: true`` combined with ``pr_sources.groups``
    is rejected here (instead of in :func:`load_config`) because ``groups``
    now live in the session file.
    """
    path = session_file_path(config, cli_override)
    if not path.exists():
        raise FileNotFoundError(
            f"Session file not found: {path}\n"
            f"Create one with `releasy new` (scaffolds config.yaml + the "
            f"session file), edit it manually, or point to one "
            f"explicitly with --session-file."
        )

    with open(path) as f:
        raw = yaml.safe_load(f) or {}

    if not isinstance(raw, dict):
        raise ValueError(
            f"Session file must be a YAML mapping, got {type(raw).__name__}: {path}"
        )

    features: list[FeatureConfig] = []
    for feat_raw in raw.get("features", []) or []:
        features.append(
            FeatureConfig(
                id=feat_raw["id"],
                description=feat_raw["description"],
                source_branch=feat_raw["source_branch"],
                enabled=feat_raw.get("enabled", True),
                depends_on=feat_raw.get("depends_on", []),
                ai_context=(feat_raw.get("ai_context") or "").strip(),
            )
        )

    ps_raw = raw.get("pr_sources", {}) or {}
    if not isinstance(ps_raw, dict):
        raise ValueError(
            f"pr_sources must be a mapping, got {type(ps_raw).__name__}"
        )

    policy_if_exists = config.pr_policy.if_exists

    by_labels: list[PRSourceConfig] = []
    for entry in ps_raw.get("by_labels", []) or []:
        raw_labels = entry.get("labels", [])
        if isinstance(raw_labels, str):
            raw_labels = [raw_labels]
        entry_if_exists = entry.get("if_exists", policy_if_exists)
        if entry_if_exists not in _VALID_IF_EXISTS:
            raise ValueError(
                f"pr_sources.by_labels[].if_exists must be one of "
                f"{_VALID_IF_EXISTS}, got {entry_if_exists!r}"
            )
        entry_mode = entry.get("mode", "auto")
        if entry_mode not in _VALID_PORT_MODES:
            raise ValueError(
                f"pr_sources.by_labels[].mode must be one of "
                f"{_VALID_PORT_MODES}, got {entry_mode!r}"
            )
        by_labels.append(
            PRSourceConfig(
                labels=raw_labels,
                description=entry.get("description", ""),
                merged_only=entry.get("merged_only", False),
                if_exists=entry_if_exists,
                ai_context=(entry.get("ai_context") or "").strip(),
                mode=entry_mode,
            )
        )

    groups: list[PRGroupConfig] = []
    seen_group_ids: set[str] = set()
    seen_group_prs: dict[str, str] = {}  # url -> group id

    def _parse_group_entry(
        entry: dict, *, source_label: str, allow_auto_discovered: bool,
    ) -> PRGroupConfig:
        gid = entry.get("id")
        if not gid:
            raise ValueError(f"{source_label}: groups[] entries must specify 'id'")
        if gid in seen_group_ids:
            raise ValueError(f"{source_label}: duplicate group id {gid!r}")
        seen_group_ids.add(gid)
        raw_prs = entry.get("prs", [])
        if not isinstance(raw_prs, list) or len(raw_prs) < 1:
            raise ValueError(
                f"{source_label}: groups[{gid!r}].prs must be a non-empty list of PR URLs"
            )
        prs_list, pr_ai_contexts = _parse_pr_url_entries(
            raw_prs, where=f"{source_label}: groups[{gid!r}].prs",
        )
        for url in prs_list:
            if url in seen_group_prs:
                raise ValueError(
                    f"PR {url} appears in both groups {seen_group_prs[url]!r} "
                    f"and {gid!r}"
                )
            seen_group_prs[url] = gid
        group_if_exists = entry.get("if_exists", policy_if_exists)
        if group_if_exists not in _VALID_IF_EXISTS:
            raise ValueError(
                f"{source_label}: groups[{gid!r}].if_exists must be one of "
                f"{_VALID_IF_EXISTS}, got {group_if_exists!r}"
            )
        group_sort = entry.get("sort", "listed")
        if group_sort not in _VALID_GROUP_SORT:
            raise ValueError(
                f"{source_label}: groups[{gid!r}].sort must be one of "
                f"{_VALID_GROUP_SORT}, got {group_sort!r}"
            )
        depends_on = entry.get("depends_on", []) or []
        if not isinstance(depends_on, list) or not all(
            isinstance(x, str) for x in depends_on
        ):
            raise ValueError(
                f"{source_label}: groups[{gid!r}].depends_on must be a list of unit IDs (strings)"
            )
        auto_flag = bool(entry.get("auto_discovered", False))
        if auto_flag and not allow_auto_discovered:
            raise ValueError(
                f"{source_label}: groups[{gid!r}].auto_discovered=true is "
                f"reserved for the sidecar overlay file. Remove the flag, or "
                f"move the entry into the deps_file sidecar."
            )
        group_mode = entry.get("mode", "auto")
        if group_mode not in _VALID_PORT_MODES:
            raise ValueError(
                f"{source_label}: groups[{gid!r}].mode must be one of "
                f"{_VALID_PORT_MODES}, got {group_mode!r}"
            )
        return PRGroupConfig(
            id=gid,
            prs=prs_list,
            description=entry.get("description", ""),
            if_exists=group_if_exists,
            sort=group_sort,
            ai_context=(entry.get("ai_context") or "").strip(),
            pr_ai_contexts=pr_ai_contexts,
            depends_on=depends_on,
            auto_discovered=auto_flag,
            mode=group_mode,
        )

    for entry in ps_raw.get("groups", []) or []:
        groups.append(_parse_group_entry(
            entry, source_label="pr_sources", allow_auto_discovered=False,
        ))

    overlay_warnings: list[str] = []

    # Optional deps-file overlay: an explicit reference from session.yaml
    # (``pr_sources.deps_file: <path>``) to a sidecar YAML carrying
    # auto-discovered or hand-curated ``groups[]`` entries with
    # ``depends_on:`` edges. Relative paths resolve against the session
    # file's directory; absolute paths stay absolute. Main session
    # entries always win on id conflict — the sidecar is purely
    # additive.
    deps_file_raw = ps_raw.get("deps_file")
    if deps_file_raw is not None and not isinstance(deps_file_raw, str):
        raise ValueError(
            f"pr_sources.deps_file must be a string path, got "
            f"{type(deps_file_raw).__name__}"
        )
    deps_file_value: str | None = deps_file_raw or None

    overlay_path = resolve_deps_file_path(path, deps_file_value)
    if overlay_path.exists():
        try:
            with open(overlay_path) as f:
                overlay_raw = yaml.safe_load(f) or {}
        except Exception as e:
            overlay_warnings.append(
                f"failed to read deps_file {overlay_path}: {e} — ignoring"
            )
            overlay_raw = {}
        if not isinstance(overlay_raw, dict):
            overlay_warnings.append(
                f"deps_file {overlay_path} must be a YAML mapping — ignoring"
            )
            overlay_raw = {}
        for entry in overlay_raw.get("groups", []) or []:
            entry_gid = entry.get("id") if isinstance(entry, dict) else None
            if entry_gid in seen_group_ids:
                overlay_warnings.append(
                    f"deps_file group {entry_gid!r} clashes with main "
                    f"session id; main session wins"
                )
                continue
            try:
                groups.append(_parse_group_entry(
                    entry,
                    source_label=f"deps_file {overlay_path.name}",
                    allow_auto_discovered=True,
                ))
            except ValueError as e:
                overlay_warnings.append(str(e))
    elif deps_file_value:
        # Explicit override pointed at a missing file — surface that.
        # When ``deps_file`` is unset and the convention default doesn't
        # exist yet, stay silent: the user just hasn't run graph discover
        # yet, that's the normal "first run" state.
        overlay_warnings.append(
            f"pr_sources.deps_file={deps_file_value!r} → {overlay_path} "
            "does not exist — no overlay loaded (run "
            "`releasy graph discover` to create it)"
        )

    def _str_list(key: str) -> list[str]:
        """Read a list-of-strings field, tolerating a bare string."""
        v = ps_raw.get(key, [])
        if v is None:
            return []
        if isinstance(v, str):
            return [v]
        if not isinstance(v, list) or not all(isinstance(x, str) for x in v):
            raise ValueError(
                f"pr_sources.{key} must be a list of strings, got {v!r}"
            )
        return v

    include_prs_list, include_pr_contexts = _parse_pr_url_entries(
        ps_raw.get("include_prs", []) or [],
        where="pr_sources.include_prs",
    )

    fp_labels_raw = ps_raw.get("forward_port_labels", []) or []
    if isinstance(fp_labels_raw, str):
        fp_labels_raw = [fp_labels_raw]
    if not isinstance(fp_labels_raw, list) or not all(
        isinstance(x, str) for x in fp_labels_raw
    ):
        raise ValueError(
            "pr_sources.forward_port_labels must be a list of strings"
        )
    forward_port_labels = [l.strip() for l in fp_labels_raw if l.strip()]

    pr_sources = PRSourcesConfig(
        by_labels=by_labels,
        exclude_labels=ps_raw.get("exclude_labels", []) or [],
        include_prs=include_prs_list,
        exclude_prs=ps_raw.get("exclude_prs", []) or [],
        include_authors=_str_list("include_authors"),
        exclude_authors=_str_list("exclude_authors"),
        groups=groups,
        include_pr_contexts=include_pr_contexts,
        deps_file=deps_file_value,
        forward_port_labels=forward_port_labels,
    )

    if config.sequential and groups:
        raise ValueError(
            "sequential: true (in config.yaml) is incompatible with "
            "pr_sources.groups (in the session file) — remove the groups "
            "or set sequential: false."
        )

    _validate_depends_on(groups, include_prs_list, overlay_warnings)
    _warn_on_redundant_pr_listings(
        groups, include_prs_list,
        ps_raw.get("exclude_prs", []) or [],
        overlay_warnings,
    )

    raw_pr_labels = raw.get("pr_labels", []) or []
    if isinstance(raw_pr_labels, str):
        raw_pr_labels = [raw_pr_labels]
    if not isinstance(raw_pr_labels, list) or not all(
        isinstance(x, str) and x.strip() for x in raw_pr_labels
    ):
        raise ValueError(
            "pr_labels must be a list of non-empty strings"
        )

    raw_by_mode = raw.get("pr_labels_by_mode", {}) or {}
    if not isinstance(raw_by_mode, dict):
        raise ValueError(
            "pr_labels_by_mode must be a mapping of port mode → label list"
        )
    pr_labels_by_mode: dict[str, list[str]] = {}
    for mode, names in raw_by_mode.items():
        if mode not in get_args(PortMode):
            raise ValueError(
                f"pr_labels_by_mode key {mode!r} is not a port mode "
                f"({' / '.join(get_args(PortMode))})"
            )
        if isinstance(names, str):
            names = [names]
        if not isinstance(names, list) or not all(
            isinstance(x, str) and x.strip() for x in names
        ):
            raise ValueError(
                f"pr_labels_by_mode[{mode!r}] must be a list of "
                "non-empty strings"
            )
        pr_labels_by_mode[mode] = [x.strip() for x in names]

    return SessionConfig(
        features=features,
        pr_sources=pr_sources,
        pr_labels=[x.strip() for x in raw_pr_labels],
        pr_labels_by_mode=pr_labels_by_mode,
        session_path=path,
        load_warnings=overlay_warnings,
    )


# Recognise feature_id forms emitted by ``_singleton_feature_id`` in
# pipeline.py. Origin singletons → ``pr-<N>``; cross-repo singletons →
# ``<owner>-<repo>-pr-<N>``. We can't reach pipeline.py from config.py
# without a circular import, so the regex is duplicated (kept in sync by
# the load-time validation tests).
_SINGLETON_FEATURE_ID_RE = re.compile(
    r"^(?:[A-Za-z0-9._-]+-[A-Za-z0-9._-]+-)?pr-\d+$"
)
_PR_URL_PREFIX_RE = re.compile(r"^https?://", re.IGNORECASE)


def _warn_on_redundant_pr_listings(
    groups: list[PRGroupConfig],
    include_prs: list[str],
    exclude_prs: list[str],
    overlay_warnings: list[str],
) -> None:
    """Surface configurations where the same PR URL appears in two places
    that resolve to a single, deterministic outcome at runtime.

    The pipeline already DOES the right thing in every case below — these
    are warnings, not errors. The point is to catch dead-weight or
    contradictory entries early so the user doesn't wonder why their
    explicit ``include_prs`` line had no effect.

    Cases:

    * URL in ``include_prs`` AND in a ``groups[].prs`` list — the group
      claim wins, the ``include_prs`` entry is dead weight.
    * URL in ``include_prs`` AND in ``exclude_prs`` — exclude wins
      (``exclude_prs`` is the documented final override), so the include
      entry is contradictory.
    * URL in some ``groups[].prs`` AND in ``exclude_prs`` — the PR is
      dropped from the group, possibly emptying it.
    """
    group_pr_to_id: dict[str, str] = {}
    for g in groups:
        for url in g.prs:
            group_pr_to_id[url] = g.id

    include_set = set(include_prs)
    exclude_set = set(exclude_prs)

    for url in include_set & set(group_pr_to_id):
        overlay_warnings.append(
            f"PR {url} appears in both pr_sources.include_prs and group "
            f"{group_pr_to_id[url]!r}; the group claim wins, the "
            "include_prs entry is redundant"
        )
    for url in include_set & exclude_set:
        overlay_warnings.append(
            f"PR {url} appears in both pr_sources.include_prs and "
            "pr_sources.exclude_prs; exclude_prs is the final override, "
            "so the PR will NOT be ported despite being explicitly included"
        )
    for url in set(group_pr_to_id) & exclude_set:
        overlay_warnings.append(
            f"PR {url} appears in group {group_pr_to_id[url]!r} and in "
            "pr_sources.exclude_prs; the PR will be dropped from the "
            "group at runtime, leaving the group smaller (or empty)"
        )


def _validate_depends_on(
    groups: list[PRGroupConfig],
    include_prs: list[str],
    overlay_warnings: list[str],
) -> None:
    """Catch typos in ``depends_on`` at session-load time.

    A reference is accepted when it resolves to one of:

    * the ``id`` of another group in the merged set (main + overlay),
    * a PR URL listed in any group's ``prs`` or in ``include_prs``,
    * a singleton feature_id pattern (``pr-<N>`` or
      ``<owner>-<repo>-pr-<N>``) — these are forward-references to PRs
      discovered from ``by_labels`` at run time, which the loader can't
      resolve fully but pattern-checks instead.

    Cycles among ``depends_on`` edges (within the merged group set) are
    rejected with a clear error listing the offending IDs.

    Anything else raises :class:`ValueError` so a typo doesn't silently
    block a unit forever (``_unmet_deps`` would treat a typo as
    permanently unmet).
    """
    group_ids = {g.id for g in groups}
    known_urls = set(include_prs)
    for g in groups:
        known_urls.update(g.prs)

    for g in groups:
        for dep in g.depends_on:
            if dep in group_ids:
                continue
            if dep in known_urls:
                continue
            if _PR_URL_PREFIX_RE.match(dep):
                # PR URL form, but not in the known-URL set — likely a typo
                # or a forward-ref to a not-yet-fetched PR. Warn rather
                # than fail: the user may have removed it from include_prs
                # without removing the dep yet.
                overlay_warnings.append(
                    f"group {g.id!r}.depends_on references PR URL "
                    f"{dep!r} which is not in include_prs / any group; "
                    "the dependent will stay blocked until a unit with "
                    "that URL appears in state"
                )
                continue
            if _SINGLETON_FEATURE_ID_RE.match(dep):
                # ``pr-<N>`` style — defer resolution to runtime
                # (the actual PR comes from by_labels discovery).
                continue
            raise ValueError(
                f"group {g.id!r}.depends_on references unknown unit "
                f"{dep!r}: must be a group id, a PR URL listed in "
                "include_prs / any group's prs, or a feature_id of the "
                "form 'pr-<N>' / '<owner>-<repo>-pr-<N>'."
            )

    # Cycle detection on the (group_id-only) sub-graph. URL / pr-<N>
    # forward refs are skipped since we can't yet know which group they
    # would land in — the runtime gate will refuse to advance on cycles
    # via ``_topo_sort_units``.
    indeg: dict[str, int] = {gid: 0 for gid in group_ids}
    succ: dict[str, list[str]] = {gid: [] for gid in group_ids}
    for g in groups:
        for dep in g.depends_on:
            if dep in group_ids:
                indeg[g.id] += 1
                succ[dep].append(g.id)
    ready = [gid for gid, n in indeg.items() if n == 0]
    visited = 0
    while ready:
        gid = ready.pop()
        visited += 1
        for child in succ[gid]:
            indeg[child] -= 1
            if indeg[child] == 0:
                ready.append(child)
    if visited != len(group_ids):
        leftover = sorted(gid for gid, n in indeg.items() if n > 0)
        raise ValueError(
            "depends_on cycle among groups: " + ", ".join(leftover)
        )


def save_session(session: SessionConfig, path: Path | None = None) -> None:
    """Persist the session file.

    Uses ``session.session_path`` when ``path`` is omitted; raises
    :class:`ValueError` if neither is set (nowhere to write).
    """
    target = path or session.session_path
    if target is None:
        raise ValueError(
            "save_session: no path to write to — pass `path` or set "
            "`session.session_path` before calling."
        )

    data: dict = {}

    def _dump_pr_url_list(
        urls: list[str], contexts: dict[str, str],
    ) -> list:
        """Render a PR URL list: bare strings when no ai_context is
        attached, dict form (``{url, ai_context}``) when one is.
        """
        out: list = []
        for url in urls:
            ctx = contexts.get(url, "")
            if ctx:
                out.append({"url": url, "ai_context": ctx})
            else:
                out.append(url)
        return out

    data["features"] = [
        {
            k: v
            for k, v in {
                "id": f.id,
                "description": f.description,
                "source_branch": f.source_branch,
                "enabled": f.enabled,
                "depends_on": f.depends_on or None,
                "ai_context": f.ai_context or None,
            }.items()
            if v is not None
        }
        for f in session.features
    ]

    ps = session.pr_sources
    ps_data: dict = {}
    if ps.by_labels:
        ps_data["by_labels"] = [
            {
                k: v
                for k, v in {
                    "labels": entry.labels,
                    "description": entry.description or None,
                    "merged_only": entry.merged_only or None,
                    "if_exists": entry.if_exists,
                    "ai_context": entry.ai_context or None,
                    "mode": entry.mode if entry.mode != "auto" else None,
                }.items()
                if v is not None
            }
            for entry in ps.by_labels
        ]
    if ps.exclude_labels:
        ps_data["exclude_labels"] = ps.exclude_labels
    if ps.include_prs:
        ps_data["include_prs"] = _dump_pr_url_list(
            ps.include_prs, ps.include_pr_contexts,
        )
    if ps.exclude_prs:
        ps_data["exclude_prs"] = ps.exclude_prs
    if ps.include_authors:
        ps_data["include_authors"] = ps.include_authors
    if ps.exclude_authors:
        ps_data["exclude_authors"] = ps.exclude_authors
    if ps.deps_file:
        ps_data["deps_file"] = ps.deps_file
    # Auto-discovered entries belong to the deps_file sidecar, not the
    # main session — skip them here so a save_session() round-trip never
    # leaks overlay state into the user's hand-curated config.
    main_groups = [g for g in ps.groups if not g.auto_discovered]
    if main_groups:
        ps_data["groups"] = [
            {
                k: v
                for k, v in {
                    "id": g.id,
                    "description": g.description or None,
                    "if_exists": g.if_exists,
                    "sort": g.sort if g.sort != "listed" else None,
                    "ai_context": g.ai_context or None,
                    "mode": g.mode if g.mode != "auto" else None,
                    "prs": _dump_pr_url_list(g.prs, g.pr_ai_contexts),
                    "depends_on": g.depends_on or None,
                }.items()
                if v is not None
            }
            for g in main_groups
        ]
    if ps.forward_port_labels:
        ps_data["forward_port_labels"] = list(ps.forward_port_labels)
    if ps_data:
        data["pr_sources"] = ps_data

    if session.pr_labels:
        data["pr_labels"] = list(session.pr_labels)

    if session.pr_labels_by_mode:
        data["pr_labels_by_mode"] = {
            mode: list(names)
            for mode, names in session.pr_labels_by_mode.items()
            if names
        }

    target.parent.mkdir(parents=True, exist_ok=True)
    with open(target, "w") as f:
        yaml.dump(data, f, default_flow_style=False, sort_keys=False)


# ---------------------------------------------------------------------------
# Stateless helpers
# ---------------------------------------------------------------------------


def make_stateless_config(
    origin_url: str,
    *,
    work_dir: Path | None = None,
    push: bool = True,
    auto_pr: bool = False,
    ai_enabled: bool = False,
    ai_command: str = "claude",
    ai_build_command: str = "",
    ai_prompt_file: str | None = None,
    ai_timeout_seconds: int = 7200,
    ai_max_iterations: int = 5,
    ai_backend: str = "cli",
    ai_api: AIApiConfig | None = None,
) -> Config:
    """Build an in-memory ``Config`` for the stateless cherry-pick command.

    No YAML is read or written. The resulting ``Config`` has a sentinel
    ``name`` (``"_stateless"``) and ``session=None`` — callers MUST NOT
    pass it to ``load_state`` / ``save_state`` / ``project_lock``; the
    stateless flow deliberately bypasses all per-project persistence.

    ``ai_prompt_file`` defaults to the bundled
    ``src/releasy/prompts/resolve_conflict.md`` so users running
    ``releasy cherry-pick --resolve-conflicts`` don't need to ship a
    template alongside the install.
    """
    if ai_prompt_file is None:
        bundled = (
            Path(__file__).parent / "prompts" / "resolve_conflict.md"
        ).resolve()
        ai_prompt_file = str(bundled)

    return Config(
        name="_stateless",
        origin=OriginConfig(remote=origin_url),
        project="stateless",
        target_branch=None,
        update_existing_prs=False,
        pr_policy=PRPolicyConfig(auto_pr=auto_pr),
        notifications=NotificationsConfig(),
        ai_resolve=AIResolveConfig(
            enabled=ai_enabled,
            command=ai_command,
            prompt_file=ai_prompt_file,
            build_command=ai_build_command,
            max_iterations=ai_max_iterations,
            timeout_seconds=ai_timeout_seconds,
        ),
        config_path=(Path.cwd() / "<stateless>").resolve(),
        work_dir=work_dir.resolve() if work_dir is not None else None,
        push=push,
        sequential=False,
        session=None,
        stateless=True,
        ai_backend=ai_backend,
        ai_api=ai_api or AIApiConfig(),
    )


def overlay_analyze_fails_overrides(
    config: Config,
    *,
    claude_command: str | None = None,
    ai_backend: str | None = None,
    build_command: str | None = None,
    prompt_file: str | None = None,
    timeout_seconds: int | None = None,
    max_iterations: int | None = None,
    max_prs_per_run: int | None = None,
    flaky_elsewhere_threshold: int | None = None,
    flaky_check_prs: int | None = None,
    post_comment_to_pr: bool | None = None,
    allowed_tools: list[str] | None = None,
    extra_args: list[str] | None = None,
) -> None:
    """Apply CLI overrides on top of a loaded ``Config`` for ``analyze-fails``.

    Mutates ``config`` in place. ``None`` arguments leave the existing
    value untouched. A ``--build-command`` override flows into
    ``ai_resolve.build_command`` so the build wrapper materialised in
    the work-dir matches.
    """
    af = config.analyze_fails
    if claude_command is not None:
        af.command = claude_command
    if ai_backend is not None:
        config.ai_backend = ai_backend
    if prompt_file is not None:
        af.prompt_file = prompt_file
    if timeout_seconds is not None:
        af.timeout_seconds = timeout_seconds
    if max_iterations is not None:
        af.max_iterations = max_iterations
    if max_prs_per_run is not None:
        af.max_prs_per_run = max_prs_per_run
    if flaky_elsewhere_threshold is not None:
        af.flaky_elsewhere_threshold = flaky_elsewhere_threshold
    if flaky_check_prs is not None:
        af.flaky_check_prs = flaky_check_prs
    if post_comment_to_pr is not None:
        af.post_comment_to_pr = post_comment_to_pr
    if allowed_tools is not None:
        af.allowed_tools = list(allowed_tools)
    if extra_args is not None:
        af.extra_args = list(extra_args)
    if build_command is not None:
        config.ai_resolve.build_command = build_command


def build_stateless_analyze_fails_config(
    *,
    origin_url: str,
    work_dir: Path | None = None,
    claude_command: str = "claude",
    ai_backend: str = "cli",
    build_command: str = "",
    prompt_file: str | None = None,
    timeout_seconds: int = 7200,
    max_iterations: int = 6,
    max_prs_per_run: int = 0,
    flaky_elsewhere_threshold: int = 2,
    flaky_check_prs: int = 12,
    post_comment_to_pr: bool = True,
    allowed_tools: list[str] | None = None,
    extra_args: list[str] | None = None,
) -> Config:
    """Build an in-memory ``Config`` for ``releasy analyze-fails`` with no
    on-disk ``config.yaml``.

    Prefer :func:`load_config` + :func:`overlay_analyze_fails_overrides`
    when a config file exists — that path inherits AI settings,
    notifications, and the build command from the project's config.
    """
    if prompt_file is None:
        bundled = (
            Path(__file__).parent / "prompts" / "analyze_fails.md"
        ).resolve()
        prompt_file = str(bundled)

    base = make_stateless_config(
        origin_url,
        work_dir=work_dir,
        push=True,
        auto_pr=False,
        ai_enabled=False,
        ai_command=claude_command,
        ai_build_command=build_command,
        ai_backend=ai_backend,
    )
    base.analyze_fails = AnalyzeFailsConfig(
        command=claude_command,
        prompt_file=prompt_file,
        timeout_seconds=timeout_seconds,
        max_iterations=max_iterations,
        max_prs_per_run=max_prs_per_run,
        flaky_elsewhere_threshold=flaky_elsewhere_threshold,
        flaky_check_prs=flaky_check_prs,
        post_comment_to_pr=post_comment_to_pr,
        allowed_tools=(
            list(allowed_tools) if allowed_tools is not None
            else _default_analyze_fails_allowed_tools()
        ),
        extra_args=list(extra_args or []),
    )
    return base


def is_stateless(config: Config) -> bool:
    """True when ``config`` was built for a stateless flow.

    Set by :func:`make_stateless_config` and the ``refresh --stateless``
    CLI path (which loads the real ``config.yaml`` and flips this flag
    so downstream code skips state I/O). Use this in place of raw
    name/field checks so the policy lives in one place.
    """
    return config.stateless


def get_github_token() -> str | None:
    return os.environ.get("RELEASY_GITHUB_TOKEN")


def get_ssh_key_path() -> str | None:
    return os.environ.get("RELEASY_SSH_KEY_PATH")
