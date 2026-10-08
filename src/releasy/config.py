"""Configuration: ``config.yaml`` (infrastructure + policy, :func:`load_config`)
and the per-effort session file (features + PR sources, :func:`load_session`)."""

from __future__ import annotations

import os
import re
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path

import yaml


# The name doubles as a filename (state, lock, default session file).
_VALID_NAME_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
_STATE_SUBDIR = "releasy"


def state_root() -> Path:
    """Per-user state dir: ``$RELEASY_STATE_DIR`` or ``$XDG_STATE_HOME/releasy`` (created on demand)."""
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
    return state_root() / f"{name}.state.yaml"


def lock_file_path(name: str) -> Path:
    return state_root() / f"{name}.lock"


def validate_project_name(name: str) -> str:
    if not isinstance(name, str) or not _VALID_NAME_RE.match(name):
        raise ValueError(
            f"Invalid project name {name!r}. Must match "
            f"{_VALID_NAME_RE.pattern} (1-64 chars, letters/digits/._-)."
        )
    return name


def extract_version_suffix(onto: str) -> str:
    """``v26.3.4.234-lts`` → ``26.3``; a hex SHA → its first 8 chars."""
    m = re.match(r"v?(\d+)\.(\d+)", onto)
    if m:
        return f"{m.group(1)}.{m.group(2)}"
    if re.fullmatch(r"[0-9a-f]{7,40}", onto):
        return onto[:8]
    return onto


@dataclass
class OriginConfig:
    remote: str
    remote_name: str = "origin"


@dataclass
class UpstreamConfig:
    """Remote fetched only for prereq detection during AI conflict resolution; never pushed to."""
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
    # Appended to the AI conflict-resolver prompt.
    ai_context: str = ""


_VALID_IF_EXISTS = ("skip", "recreate", "append")
_VALID_GROUP_SORT = ("listed", "merged_at")
# Config accepts all three; post-detection only the last two survive.
_VALID_PORT_MODES = ("auto", "backport", "forward_port")
_VALID_EFFORTS = ("low", "medium", "high", "xhigh", "max")
_VALID_AI_BACKENDS = ("cli", "codex", "api")

from typing import Literal, get_args  # noqa: E402
PortMode = Literal["backport", "forward_port"]


@dataclass
class PRSourceConfig:
    labels: list[str]
    description: str = ""
    merged_only: bool = False
    # "skip" | "recreate" | "append" (cherry-pick missing PRs onto the
    # existing branch tip). Defaults to ``pr_policy.if_exists``.
    if_exists: str = "skip"
    # Appended to the AI conflict-resolver prompt for every matched PR.
    ai_context: str = ""
    # ``auto`` | ``backport`` | ``forward_port``; explicit values bypass
    # ``_detect_port_mode``.
    mode: str = "auto"


@dataclass
class PRGroupConfig:
    """PRs cherry-picked onto one port branch ``feature/<base>/<id>`` and shipped as one PR."""
    id: str
    prs: list[str]
    description: str = ""
    # See ``PRSourceConfig.if_exists``.
    if_exists: str = "skip"
    # "listed" (order in ``prs``) or "merged_at" (merge time, PR number breaks ties).
    sort: str = "listed"
    ai_context: str = ""
    # Per-URL ai_context from dict-form ``prs`` entries.
    pr_ai_contexts: dict[str, str] = field(default_factory=dict)
    # Unit IDs (group id, ``pr-<N>`` or ``<owner>-<repo>-pr-<N>``) that must
    # be merged in target before this group runs.
    depends_on: list[str] = field(default_factory=list)
    # Set by ``releasy graph discover``; only such entries may be rewritten by it.
    auto_discovered: bool = False
    # See ``PRSourceConfig.mode``.
    mode: str = "auto"


@dataclass
class PRPolicyConfig:
    """``pr_policy:`` in config.yaml; ``if_exists`` is the default for session entries."""
    if_exists: str = "skip"
    auto_pr: bool = True
    retry_failed: bool = True
    recreate_closed_prs: bool = False
    # Re-port an entry marked ``reverted`` on a renumbered port branch.
    recreate_reverted_prs: bool = False
    # Mark entries whose source PRs were already cherry-picked by another
    # PR targeting the same base as ``superseded``.
    detect_superseded: bool = True
    # Auto-resumes of a partially-applied group by `releasy run`; 0 disables.
    max_partial_continue_attempts: int = 2
    # Skip a unit whose recorded stall cannot clear on its own (overridden by
    # `releasy run --ignore-stalls`).
    honor_stall_reasons: bool = True


@dataclass
class PRSourcesConfig:
    """PR discovery selectors from the session file.

    union(by_labels) − exclude_labels − exclude_authors ∩ include_authors
    + include_prs − exclude_prs − on_hold. Groups take all their listed PRs
    regardless of labels, minus the exclude/author filters.
    """
    by_labels: list[PRSourceConfig] = field(default_factory=list)
    exclude_labels: list[str] = field(default_factory=list)
    include_prs: list[str] = field(default_factory=list)
    exclude_prs: list[str] = field(default_factory=list)
    # Parked PRs: stay in the graph but their units are skipped by `run`.
    on_hold: list[str] = field(default_factory=list)
    include_authors: list[str] = field(default_factory=list)
    exclude_authors: list[str] = field(default_factory=list)
    groups: list[PRGroupConfig] = field(default_factory=list)
    # Sidecar YAML with extra ``groups[]`` merged in memory; relative to the
    # session file's directory.
    deps_file: str | None = None
    on_hold_reasons: dict[str, str] = field(default_factory=dict)
    # Per-URL ai_context from dict-form ``include_prs`` entries.
    include_pr_contexts: dict[str, str] = field(default_factory=dict)
    # Case-insensitive labels marking a source PR as a forward-port.
    forward_port_labels: list[str] = field(default_factory=list)


def _default_assignee_dev_options() -> list[str]:
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
    # "Verified by Dev" is a meta-option, not a person.
    return [
        "Alsu Giliazova",
        "Carlos",
        "Davit Mnatobishvili",
        "strtgbb",
        "vzakaznikov",
        "Verified by Dev",
    ]


def _default_assignee_dev_login_map() -> dict[str, str]:
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
    # Options provisioned on the GitHub Project's Assignee Dev / QA fields.
    assignee_dev_options: list[str] = field(
        default_factory=_default_assignee_dev_options,
    )
    assignee_qa_options: list[str] = field(
        default_factory=_default_assignee_qa_options,
    )
    # GitHub login (case-insensitive) → ``Assignee Dev`` option label.
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
    return _default_allowed_tools() + [
        "WebFetch", "WebSearch",
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
        "Bash(build/src/unit_tests_dbms:*)",
        "Bash(./build/src/unit_tests_dbms:*)",
        "Bash(./build/programs/clickhouse:*)",
        "Bash(pytest:*)",
        "Bash(python:*)", "Bash(python3:*)",
        # {work_dir} is resolved at runtime by analyze_fails._resolve_tool_paths.
        "Bash({work_dir}/build/programs/clickhouse:*)",
        "Bash({work_dir}/build/src/unit_tests_dbms:*)",
        "Bash({work_dir}/tests/clickhouse-test:*)",
        "Bash({work_dir}/tests/integration/runner:*)",
    ]


def _default_test_file_globs() -> list[str]:
    return [
        "tests/queries/**",
        "tests/integration/**",
        "src/**/tests/**",
        "**/gtest_*",
        "**/*_test.cpp",
    ]


@dataclass
class AIChangelogConfig:
    """AI-synthesized CHANGELOG entry for multi-PR group ports."""
    enabled: bool = False
    command: str = "claude"
    prompt_file: str = "prompts/synthesize_changelog.md"
    timeout_seconds: int = 300
    # Per-PR body is truncated to this before inlining into the prompt.
    max_pr_body_chars: int = 3000


@dataclass
class AIApiConfig:
    """``ai_backend: api`` — drive the Anthropic Messages API in-process."""
    # Env var name holding the token; checked before ``api_key``.
    api_key_env: str = "ANTHROPIC_API_KEY"
    api_key: str | None = None
    base_url: str | None = None
    # Falls back to the global ``ai_model`` when unset.
    model: str | None = None
    max_tokens: int = 64000
    # Cap on model round-trips per invocation.
    max_turns: int = 300
    thinking: bool = True
    # SDK-level retries for 429 / 5xx.
    max_retries: int = 5
    request_timeout_seconds: int = 1800
    bash_timeout_seconds: int = 3600
    tool_output_max_chars: int = 30000
    # Appended to the built-in system prompt.
    system_prompt_extra: str = ""


@dataclass
class AICodexConfig:
    """``ai_backend: codex`` — spawn ``codex exec``; ``allowed_tools`` only picks the sandbox."""
    command: str = "codex"
    # Unset → codex's own default.
    model: str | None = None
    reasoning_effort: str | None = None
    extra_args: list[str] = field(default_factory=list)


@dataclass
class AutoAddPrerequisitePRsConfig:
    """Port missing-prerequisite PRs found during conflict resolution as part of the unit."""
    enabled: bool = False
    # Recursion cap in dives (PR_A → PR_B → PR_C is depth 2).
    max_prereq_depth: int = 7
    # An in-origin prereq of an in-origin PR must match ``pr_sources.by_labels``.
    require_origin_prereq_label: bool = True


@dataclass
class ReviewResponseConfig:
    """``releasy refresh --address-review``: AI pass over trusted review comments."""
    command: str = "claude"
    prompt_file: str = "prompts/address_review.md"
    timeout_seconds: int = 7200
    max_iterations: int = 15
    # GitHub ``author_association`` values whose comments reach the AI.
    trusted_associations: list[str] = field(
        default_factory=lambda: [
            "OWNER", "MEMBER", "COLLABORATOR", "CONTRIBUTOR",
        ],
    )
    # Extra trusted GitHub logins (case-insensitive), additive.
    trusted_reviewers: list[str] = field(default_factory=list)
    reply_to_non_addressable: bool = True
    post_summary_comment: bool = False
    allowed_tools: list[str] = field(default_factory=_default_allowed_tools)
    extra_args: list[str] = field(default_factory=list)


@dataclass
class GraphConfig:
    """``releasy graph discover`` / ``graph update``."""
    command: str = "claude"
    prompt_file: str = "prompts/adjust_graph.md"
    timeout_seconds: int = 7200
    # GitHub ``author_association`` values whose comments reach the AI.
    trusted_associations: list[str] = field(
        default_factory=lambda: ["OWNER", "MEMBER", "COLLABORATOR"],
    )
    # Extra trusted GitHub logins (case-insensitive), additive.
    trusted_reviewers: list[str] = field(default_factory=list)
    # Labels for the graph issue; the target-branch name is always added too.
    issue_labels: list[str] = field(default_factory=lambda: ["releasy"])
    post_comment: bool = True
    # Refresh the graph issue's progress checkboxes after `run` / `refresh`.
    sync_progress: bool = True
    # Enforce vetoes by adding the PR to exclude_prs (else record-only).
    apply_exclusions: bool = True
    # Mark addressed comments as Outdated.
    minimize_addressed_comments: bool = True
    extra_args: list[str] = field(default_factory=list)


@dataclass
class AnalyzeFailsConfig:
    """``releasy analyze-fails``: AI investigation of failed CI checks on a PR."""
    command: str = "claude"
    prompt_file: str = "prompts/analyze_fails.md"
    # Empty = all. Known: fasttest, quick_functional, stateless,
    # integration, regression, other.
    categories: list[str] = field(default_factory=list)
    # Also investigate failed checks that published no per-test results.
    job_level_failures: bool = True
    timeout_seconds: int = 7200
    max_iterations: int = 6
    # Cap on tracked PRs per invocation when `--pr` is omitted; 0 = no cap.
    max_prs_per_run: int = 0
    # Tell the AI which failures were already red on the target-branch baseline.
    baseline_check: bool = True
    # How far back from the merge base to look for a commit with a CI run.
    baseline_scan_commits: int = 25
    # Same test failing in this many other tracked PRs → "likely flake"; 0 disables.
    flaky_elsewhere_threshold: int = 2
    # Cap on other tracked PRs fetched for the flaky-elsewhere map.
    flaky_check_prs: int = 12
    # Advisory read-only audit of doubtful shard outcomes.
    verify_outcome: bool = True
    # Investigator sessions per shard (a disputed audit triggers a redo); 1 = no redo.
    max_investigation_rounds: int = 2
    verify_prompt_file: str = "prompts/verify_analysis.md"
    verify_timeout_seconds: int = 1800
    verify_label: str = "ai-needs-verify"
    verify_label_color: str = "FBCA04"
    # Post a per-shard summary comment on origin PRs.
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
    # Used for `git merge` conflicts in ``releasy refresh``.
    merge_prompt_file: str = "prompts/resolve_merge_conflict.md"
    # Used when ``split_conflict_commit`` is on.
    split_prompt_file: str = "prompts/resolve_conflict_split.md"
    # Commit the conflict markers as-is first, then the resolution as a
    # second commit; False = single-commit flow.
    split_conflict_commit: bool = True
    allowed_tools: list[str] = field(default_factory=_default_allowed_tools)
    max_iterations: int = 5
    # Runs that may re-resolve a unit stuck at a dead end before it is
    # parked; 0 = no cap.
    max_dead_end_attempts: int = 2
    timeout_seconds: int = 7200
    build_command: str = "cd build && ninja"
    # True: AI resolves only, RelEasy builds/tests and loops build fixes.
    # False: single-session resolve+build.
    deterministic_build: bool = True
    max_build_attempts: int = 5  # consecutive build-fix attempts per run
    # Resumes of a parked build_failed branch on later runs; 0 disables.
    max_verify_resume_attempts: int = 2
    # Re-port instead of resuming once the parked branch is this many
    # commits behind base; 0 disables.
    max_resume_base_drift: int = 50
    max_verify_iterations: int = 12  # cap on build↔test iterations per pass
    build_log_tail_lines: int = 500  # log tail fed to the fix-build prompt
    build_timeout_seconds: int = 7200
    # Run the source PR's own tests after a green build.
    run_pr_tests: bool = True
    # Repo-relative globs marking a changed file as a runnable test.
    test_file_globs: list[str] = field(default_factory=_default_test_file_globs)
    test_timeout_seconds: int = 3600
    resolve_only_prompt_file: str = "prompts/resolve_conflict_nobuild.md"
    fix_build_prompt_file: str = "prompts/fix_build.md"
    run_tests_prompt_file: str = "prompts/run_tests.md"
    label: str = "ai-resolved"
    label_color: str = "8B5CF6"
    # PR needs a human because the resolver gave up.
    needs_attention_label: str = "ai-needs-attention"
    needs_attention_label_color: str = "D93F0B"
    # Conflict caused by a known missing prerequisite PR.
    missing_prereqs_label: str = "missing-prerequisites"
    missing_prereqs_label_color: str = "E4E669"
    # PR scope was expanded with auto-added prerequisite PRs.
    auto_prereq_label: str = "auto-prereq-added"
    auto_prereq_label_color: str = "0E8A16"
    # Advisory second AI pass auditing the resolution against the source PR.
    verify_resolution: bool = False
    verify_prompt_file: str = "prompts/verify_resolution.md"
    verify_timeout_seconds: int = 1800
    verify_label: str = "ai-needs-verify"
    verify_label_color: str = "FBCA04"
    extra_args: list[str] = field(default_factory=list)
    # Re-invocations after a transient API error (stream idle, overloaded, …).
    api_retries: int = 3
    api_retry_backoff_seconds: int = 15
    # On an exhausted usage session, poll and re-prompt instead of failing.
    wait_on_session_exhaustion: bool = True
    session_exhaustion_max_wait_hours: int = 60
    session_exhaustion_poll_minutes: int = 30
    # Extra regexes (OR-ed with the built-ins) for recognising a limit message.
    session_exhaustion_extra_patterns: list[str] = field(default_factory=list)
    # Corrective AI passes for a content-correctable postcondition failure; 0 disables.
    postcondition_retries: int = 2
    # When those passes are spent: True keeps and flags the resolution,
    # False discards it.
    warn_on_unfixed_postconditions: bool = True
    auto_add_prerequisite_prs: AutoAddPrerequisitePRsConfig = field(
        default_factory=AutoAddPrerequisitePRsConfig,
    )


@dataclass
class SessionConfig:
    """Per-effort source data; ``session_path`` is None for in-memory only."""
    features: list[FeatureConfig] = field(default_factory=list)
    pr_sources: PRSourcesConfig = field(default_factory=PRSourcesConfig)
    # Applied to every rebase PR (auto-created on origin).
    pr_labels: list[str] = field(default_factory=list)
    # Extra labels keyed by ``PortMode``, merged with ``pr_labels``.
    pr_labels_by_mode: dict[str, list[str]] = field(default_factory=dict)
    session_path: Path | None = None
    # Non-fatal load issues, surfaced once at CLI startup.
    load_warnings: list[str] = field(default_factory=list)


@dataclass
class Config:
    name: str  # unique slug identifying this project (state file key)
    origin: OriginConfig
    project: str  # short project identifier, e.g. "antalya"
    upstream: UpstreamConfig | None = None
    target_branch: str | None = None  # explicit base/target branch override
    # Overwrite title/body of an already-existing port PR.
    update_existing_prs: bool = False
    # Added to a merged port PR and stripped from its origin source PRs.
    merged_label: str | None = None
    merged_label_color: str = "8B5CF6"
    pr_policy: PRPolicyConfig = field(default_factory=PRPolicyConfig)
    notifications: NotificationsConfig = field(default_factory=NotificationsConfig)
    ai_resolve: AIResolveConfig = field(default_factory=AIResolveConfig)
    ai_changelog: AIChangelogConfig = field(default_factory=AIChangelogConfig)
    review_response: ReviewResponseConfig = field(default_factory=ReviewResponseConfig)
    analyze_fails: AnalyzeFailsConfig = field(default_factory=AnalyzeFailsConfig)
    graph: GraphConfig = field(default_factory=GraphConfig)
    config_path: Path = field(default_factory=lambda: Path.cwd() / "config.yaml")
    work_dir: Path | None = None
    # Copy of all terminal output is appended here (see releasy.termlog).
    log_file: Path | None = None
    push: bool = False
    # Port the merged-time-sorted PR queue one PR per invocation.
    sequential: bool = False
    # As given in config.yaml; resolved by :func:`session_file_path`.
    session_file: Path | None = None
    # Populated by :func:`load_session` for commands that need it.
    session: SessionConfig | None = None
    stateless: bool = False
    # Mutating helpers become logged no-ops.
    dry_run: bool = False
    # Re-attempt every parked unit, whatever ``pr_policy.honor_stall_reasons`` says.
    ignore_stalls: bool = False
    # Global claude --model / --effort applied to every AI invocation.
    ai_model: str | None = None
    ai_effort: str | None = None
    # "cli" | "codex" | "api"
    ai_backend: str = "cli"
    ai_api: AIApiConfig = field(default_factory=AIApiConfig)
    ai_codex: AICodexConfig = field(default_factory=AICodexConfig)

    @property
    def repo_dir(self) -> Path:
        """Directory containing ``config.yaml``; base for relative paths in config."""
        return self.config_path.parent

    @property
    def state_path(self) -> Path:
        return state_file_path(self.name)

    @property
    def lock_path(self) -> Path:
        return lock_file_path(self.name)

    def resolve_work_dir(self, cli_override: Path | None = None) -> Path:
        """CLI --work-dir > config work_dir > current directory."""
        if cli_override is not None:
            return cli_override.resolve()
        if self.work_dir is not None:
            return self.work_dir.resolve()
        return Path.cwd()

    @property
    def features(self) -> list[FeatureConfig]:
        if self.session is None:
            return []
        return self.session.features

    @property
    def pr_sources(self) -> PRSourcesConfig:
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
            if onto and branch.startswith(self.feature_branch_name(f.id, onto)):
                return f
        return None

    def base_branch_name(self, onto: str) -> str:
        """``target_branch`` if set, else ``<project>-<version suffix of onto>``."""
        if self.target_branch:
            return self.target_branch
        suffix = extract_version_suffix(onto)
        return f"{self.project_name}-{suffix}"

    def feature_branch_name(self, feature_id: str, onto: str) -> str:
        return f"feature/{self.base_branch_name(onto)}/{feature_id}"


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


def _parse_ai_codex(raw: object) -> AICodexConfig:
    if not isinstance(raw, dict):
        raise ValueError("ai_codex must be a mapping")
    unknown = set(raw) - {f.name for f in fields(AICodexConfig)}
    if unknown:
        raise ValueError(f"unknown ai_codex keys: {sorted(unknown)}")
    extra_args = raw.get("extra_args") or []
    if not isinstance(extra_args, list):
        raise ValueError("ai_codex.extra_args must be a list")
    return AICodexConfig(
        command=str(raw.get("command") or AICodexConfig.command),
        model=(str(raw["model"]) if raw.get("model") else None),
        reasoning_effort=(
            str(raw["reasoning_effort"]) if raw.get("reasoning_effort") else None
        ),
        extra_args=[str(a) for a in extra_args],
    )


def _parse_ai_api(raw: object) -> AIApiConfig:
    if not isinstance(raw, dict):
        raise ValueError("ai_api must be a mapping")
    defaults = AIApiConfig()
    unknown = set(raw) - {f.name for f in fields(AIApiConfig)}
    if unknown:
        raise ValueError(f"unknown ai_api keys: {sorted(unknown)}")
    # A token pasted here (instead of a variable NAME) would silently
    # resolve to "no token found" at call time.
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


def _parse_trusted(
    section_raw: dict, section: str, default_assocs: list[str],
) -> tuple[list[str], list[str]]:
    """Return deduped ``(trusted_reviewers, trusted_associations)`` of a section."""
    reviewers_raw = section_raw.get("trusted_reviewers", []) or []
    if not isinstance(reviewers_raw, list) or not all(
        isinstance(x, str) for x in reviewers_raw
    ):
        raise ValueError(f"{section}.trusted_reviewers must be a list of strings")
    seen: set[str] = set()
    reviewers: list[str] = []
    for login in reviewers_raw:
        stripped = login.strip()
        key = stripped.lower()
        if not stripped or key in seen:
            continue
        seen.add(key)
        reviewers.append(stripped)
    assocs_raw = section_raw.get("trusted_associations", default_assocs)
    if not isinstance(assocs_raw, list) or not all(
        isinstance(x, str) for x in assocs_raw
    ):
        raise ValueError(
            f"{section}.trusted_associations must be a list of strings"
        )
    assoc_seen: set[str] = set()
    assocs: list[str] = []
    for a in assocs_raw:
        up = a.strip().upper()
        if not up or up in assoc_seen:
            continue
        assoc_seen.add(up)
        assocs.append(up)
    return reviewers, assocs


def load_config(config_path: Path | None = None) -> Config:
    """Load and validate ``config.yaml``. Does not touch the session file."""
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
        remote_name=raw["origin"].get("remote_name", OriginConfig.remote_name),
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
        upstream_remote_name = upstream_raw.get(
            "remote_name", UpstreamConfig.remote_name,
        )
        upstream_branch = upstream_raw.get("branch", UpstreamConfig.branch)
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
    pp_d = PRPolicyConfig()
    pp_if_exists = pp_raw.get("if_exists", pp_d.if_exists)
    if pp_if_exists not in _VALID_IF_EXISTS:
        raise ValueError(
            f"pr_policy.if_exists must be one of {_VALID_IF_EXISTS}, "
            f"got {pp_if_exists!r}"
        )
    pr_policy = PRPolicyConfig(
        if_exists=pp_if_exists,
        auto_pr=bool(pp_raw.get("auto_pr", pp_d.auto_pr)),
        retry_failed=bool(pp_raw.get("retry_failed", pp_d.retry_failed)),
        recreate_closed_prs=bool(
            pp_raw.get("recreate_closed_prs", pp_d.recreate_closed_prs)
        ),
        recreate_reverted_prs=bool(
            pp_raw.get("recreate_reverted_prs", pp_d.recreate_reverted_prs)
        ),
        detect_superseded=bool(
            pp_raw.get("detect_superseded", pp_d.detect_superseded)
        ),
        max_partial_continue_attempts=int(
            pp_raw.get(
                "max_partial_continue_attempts",
                pp_d.max_partial_continue_attempts,
            ) or 0
        ),
        honor_stall_reasons=bool(
            pp_raw.get("honor_stall_reasons", pp_d.honor_stall_reasons)
        ),
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
        # Original key casing is kept; lookups lower-case at call time.
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

    aic_raw = raw.get("ai_changelog", {}) or {}
    aic_d = AIChangelogConfig()
    ai_changelog = AIChangelogConfig(
        enabled=bool(aic_raw.get("enabled", aic_d.enabled)),
        command=aic_raw.get("command", aic_d.command),
        prompt_file=aic_raw.get("prompt_file", aic_d.prompt_file),
        timeout_seconds=int(aic_raw.get("timeout_seconds", aic_d.timeout_seconds)),
        max_pr_body_chars=int(
            aic_raw.get("max_pr_body_chars", aic_d.max_pr_body_chars)
        ),
    )

    ai_raw = raw.get("ai_resolve", {}) or {}

    # Accepts a bool (shorthand for ``{enabled: <bool>}``) or a mapping.
    auto_prereq_raw = ai_raw.get("auto_add_prerequisite_prs")
    ap_d = AutoAddPrerequisitePRsConfig()
    if auto_prereq_raw is None:
        auto_add_prerequisite_prs = AutoAddPrerequisitePRsConfig()
    elif isinstance(auto_prereq_raw, bool):
        auto_add_prerequisite_prs = AutoAddPrerequisitePRsConfig(
            enabled=auto_prereq_raw,
        )
    elif isinstance(auto_prereq_raw, dict):
        auto_add_prerequisite_prs = AutoAddPrerequisitePRsConfig(
            enabled=bool(auto_prereq_raw.get("enabled", ap_d.enabled)),
            max_prereq_depth=int(
                auto_prereq_raw.get("max_prereq_depth", ap_d.max_prereq_depth)
            ),
            require_origin_prereq_label=bool(
                auto_prereq_raw.get(
                    "require_origin_prereq_label",
                    ap_d.require_origin_prereq_label,
                )
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

    ai_d = AIResolveConfig()
    ai_resolve = AIResolveConfig(
        enabled=ai_raw.get("enabled", ai_d.enabled),
        command=ai_raw.get("command", ai_d.command),
        prompt_file=ai_raw.get("prompt_file", ai_d.prompt_file),
        merge_prompt_file=ai_raw.get(
            "merge_prompt_file", ai_d.merge_prompt_file,
        ),
        split_prompt_file=ai_raw.get(
            "split_prompt_file", ai_d.split_prompt_file,
        ),
        split_conflict_commit=bool(
            ai_raw.get("split_conflict_commit", ai_d.split_conflict_commit)
        ),
        allowed_tools=ai_raw.get("allowed_tools") or _default_allowed_tools(),
        max_iterations=int(ai_raw.get("max_iterations", ai_d.max_iterations)),
        max_dead_end_attempts=int(
            ai_raw.get("max_dead_end_attempts", ai_d.max_dead_end_attempts) or 0
        ),
        timeout_seconds=int(ai_raw.get("timeout_seconds", ai_d.timeout_seconds)),
        build_command=ai_raw.get("build_command", ai_d.build_command),
        deterministic_build=bool(
            ai_raw.get("deterministic_build", ai_d.deterministic_build)
        ),
        max_build_attempts=int(
            ai_raw.get("max_build_attempts", ai_d.max_build_attempts)
        ),
        max_verify_resume_attempts=int(
            ai_raw.get(
                "max_verify_resume_attempts", ai_d.max_verify_resume_attempts,
            ) or 0
        ),
        max_resume_base_drift=int(
            ai_raw.get("max_resume_base_drift", ai_d.max_resume_base_drift) or 0
        ),
        max_verify_iterations=int(
            ai_raw.get("max_verify_iterations", ai_d.max_verify_iterations)
        ),
        build_log_tail_lines=int(
            ai_raw.get("build_log_tail_lines", ai_d.build_log_tail_lines)
        ),
        build_timeout_seconds=int(
            ai_raw.get("build_timeout_seconds", ai_d.build_timeout_seconds)
        ),
        run_pr_tests=bool(ai_raw.get("run_pr_tests", ai_d.run_pr_tests)),
        test_file_globs=ai_raw.get("test_file_globs") or _default_test_file_globs(),
        test_timeout_seconds=int(
            ai_raw.get("test_timeout_seconds", ai_d.test_timeout_seconds)
        ),
        resolve_only_prompt_file=ai_raw.get(
            "resolve_only_prompt_file", ai_d.resolve_only_prompt_file,
        ),
        fix_build_prompt_file=ai_raw.get(
            "fix_build_prompt_file", ai_d.fix_build_prompt_file,
        ),
        run_tests_prompt_file=ai_raw.get(
            "run_tests_prompt_file", ai_d.run_tests_prompt_file,
        ),
        label=ai_raw.get("label", ai_d.label),
        label_color=ai_raw.get("label_color", ai_d.label_color),
        needs_attention_label=ai_raw.get(
            "needs_attention_label", ai_d.needs_attention_label,
        ),
        needs_attention_label_color=ai_raw.get(
            "needs_attention_label_color", ai_d.needs_attention_label_color,
        ),
        missing_prereqs_label=ai_raw.get(
            "missing_prereqs_label", ai_d.missing_prereqs_label,
        ),
        missing_prereqs_label_color=ai_raw.get(
            "missing_prereqs_label_color", ai_d.missing_prereqs_label_color,
        ),
        auto_prereq_label=ai_raw.get(
            "auto_prereq_label", ai_d.auto_prereq_label,
        ),
        auto_prereq_label_color=ai_raw.get(
            "auto_prereq_label_color", ai_d.auto_prereq_label_color,
        ),
        verify_resolution=bool(
            ai_raw.get("verify_resolution", ai_d.verify_resolution)
        ),
        verify_prompt_file=ai_raw.get(
            "verify_prompt_file", ai_d.verify_prompt_file,
        ),
        verify_timeout_seconds=int(
            ai_raw.get("verify_timeout_seconds", ai_d.verify_timeout_seconds)
        ),
        verify_label=ai_raw.get("verify_label", ai_d.verify_label),
        verify_label_color=ai_raw.get(
            "verify_label_color", ai_d.verify_label_color,
        ),
        extra_args=ai_raw.get("extra_args", []) or [],
        api_retries=int(ai_raw.get("api_retries", ai_d.api_retries)),
        api_retry_backoff_seconds=int(
            ai_raw.get("api_retry_backoff_seconds", ai_d.api_retry_backoff_seconds)
        ),
        wait_on_session_exhaustion=bool(
            ai_raw.get("wait_on_session_exhaustion", ai_d.wait_on_session_exhaustion)
        ),
        session_exhaustion_max_wait_hours=int(
            ai_raw.get(
                "session_exhaustion_max_wait_hours",
                ai_d.session_exhaustion_max_wait_hours,
            )
        ),
        session_exhaustion_poll_minutes=int(
            ai_raw.get(
                "session_exhaustion_poll_minutes",
                ai_d.session_exhaustion_poll_minutes,
            )
        ),
        session_exhaustion_extra_patterns=list(
            ai_raw.get("session_exhaustion_extra_patterns", []) or []
        ),
        postcondition_retries=int(
            ai_raw.get("postcondition_retries", ai_d.postcondition_retries)
        ),
        warn_on_unfixed_postconditions=bool(
            ai_raw.get(
                "warn_on_unfixed_postconditions",
                ai_d.warn_on_unfixed_postconditions,
            )
        ),
        auto_add_prerequisite_prs=auto_add_prerequisite_prs,
    )

    rr_raw = raw.get("review_response", {}) or {}
    rr_d = ReviewResponseConfig()
    rr_reviewers, rr_assocs = _parse_trusted(
        rr_raw, "review_response", rr_d.trusted_associations,
    )
    review_response = ReviewResponseConfig(
        command=rr_raw.get("command", rr_d.command),
        prompt_file=rr_raw.get("prompt_file", rr_d.prompt_file),
        timeout_seconds=int(rr_raw.get("timeout_seconds", rr_d.timeout_seconds)),
        max_iterations=int(rr_raw.get("max_iterations", rr_d.max_iterations)),
        trusted_associations=rr_assocs,
        trusted_reviewers=rr_reviewers,
        reply_to_non_addressable=bool(
            rr_raw.get("reply_to_non_addressable", rr_d.reply_to_non_addressable),
        ),
        post_summary_comment=bool(
            rr_raw.get("post_summary_comment", rr_d.post_summary_comment)
        ),
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
    af_d = AnalyzeFailsConfig()
    analyze_fails = AnalyzeFailsConfig(
        command=af_raw.get("command", af_d.command),
        prompt_file=af_raw.get("prompt_file", af_d.prompt_file),
        categories=[c.strip() for c in af_categories if c.strip()],
        job_level_failures=bool(
            af_raw.get("job_level_failures", af_d.job_level_failures)
        ),
        baseline_check=bool(af_raw.get("baseline_check", af_d.baseline_check)),
        baseline_scan_commits=int(
            af_raw.get("baseline_scan_commits", af_d.baseline_scan_commits)
        ),
        verify_outcome=bool(af_raw.get("verify_outcome", af_d.verify_outcome)),
        max_investigation_rounds=int(
            af_raw.get("max_investigation_rounds", af_d.max_investigation_rounds),
        ),
        verify_prompt_file=af_raw.get(
            "verify_prompt_file", af_d.verify_prompt_file,
        ),
        verify_timeout_seconds=int(
            af_raw.get("verify_timeout_seconds", af_d.verify_timeout_seconds),
        ),
        verify_label=af_raw.get("verify_label", af_d.verify_label),
        verify_label_color=af_raw.get(
            "verify_label_color", af_d.verify_label_color,
        ),
        timeout_seconds=int(af_raw.get("timeout_seconds", af_d.timeout_seconds)),
        max_iterations=int(af_raw.get("max_iterations", af_d.max_iterations)),
        max_prs_per_run=int(af_raw.get("max_prs_per_run", af_d.max_prs_per_run)),
        flaky_elsewhere_threshold=int(
            af_raw.get("flaky_elsewhere_threshold", af_d.flaky_elsewhere_threshold),
        ),
        flaky_check_prs=int(af_raw.get("flaky_check_prs", af_d.flaky_check_prs)),
        post_comment_to_pr=bool(
            af_raw.get("post_comment_to_pr", af_d.post_comment_to_pr)
        ),
        allowed_tools=(
            af_raw.get("allowed_tools")
            or _default_analyze_fails_allowed_tools()
        ),
        extra_args=af_raw.get("extra_args", []) or [],
    )

    gr_raw = raw.get("graph", {}) or {}
    gr_d = GraphConfig()
    gr_reviewers, gr_assocs = _parse_trusted(
        gr_raw, "graph", gr_d.trusted_associations,
    )
    gr_labels_raw = gr_raw.get("issue_labels", gr_d.issue_labels)
    if not isinstance(gr_labels_raw, list) or not all(
        isinstance(x, str) for x in gr_labels_raw
    ):
        raise ValueError("graph.issue_labels must be a list of strings")
    graph = GraphConfig(
        command=gr_raw.get("command", gr_d.command),
        prompt_file=gr_raw.get("prompt_file", gr_d.prompt_file),
        timeout_seconds=int(gr_raw.get("timeout_seconds", gr_d.timeout_seconds)),
        trusted_associations=gr_assocs,
        trusted_reviewers=gr_reviewers,
        issue_labels=[s.strip() for s in gr_labels_raw if s.strip()],
        post_comment=bool(gr_raw.get("post_comment", gr_d.post_comment)),
        sync_progress=bool(gr_raw.get("sync_progress", gr_d.sync_progress)),
        apply_exclusions=bool(
            gr_raw.get("apply_exclusions", gr_d.apply_exclusions)
        ),
        minimize_addressed_comments=bool(
            gr_raw.get(
                "minimize_addressed_comments", gr_d.minimize_addressed_comments,
            )
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
        session_file = Path(raw_session_file).expanduser()

    sequential = bool(raw.get("sequential", Config.sequential))

    ai_model = raw.get("ai_model") or None
    if ai_model is not None and not isinstance(ai_model, str):
        raise ValueError("ai_model must be a string")
    ai_effort = raw.get("ai_effort") or None
    if ai_effort is not None and ai_effort not in _VALID_EFFORTS:
        raise ValueError(
            f"ai_effort must be one of {_VALID_EFFORTS}, got {ai_effort!r}"
        )

    ai_backend = str(raw.get("ai_backend") or Config.ai_backend).strip().lower()
    if ai_backend not in _VALID_AI_BACKENDS:
        raise ValueError(
            f"ai_backend must be one of {_VALID_AI_BACKENDS}, "
            f"got {ai_backend!r}"
        )
    ai_api = _parse_ai_api(raw.get("ai_api") or {})
    ai_codex = _parse_ai_codex(raw.get("ai_codex") or {})

    from releasy.termlog import configure as _configure_term_log

    cfg = Config(
        name=name,
        origin=origin,
        upstream=upstream,
        project=project,
        target_branch=raw.get("target_branch") or None,
        update_existing_prs=bool(
            raw.get("update_existing_prs", Config.update_existing_prs)
        ),
        merged_label=_parse_optional_label(
            raw.get("merged_label"), key="merged_label",
        ),
        merged_label_color=_parse_label_color(
            raw.get("merged_label_color"),
            key="merged_label_color",
            default=Config.merged_label_color,
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
        push=raw.get("push", Config.push),
        sequential=sequential,
        session_file=session_file,
        ai_model=ai_model,
        ai_effort=ai_effort,
        ai_backend=ai_backend,
        ai_api=ai_api,
        ai_codex=ai_codex,
    )
    _configure_term_log(log_file)
    return cfg


def _non_default_fields(obj: object, defaults: object) -> dict:
    """Fields of dataclass ``obj`` whose values differ from ``defaults``, in field order."""
    return {
        f.name: getattr(obj, f.name)
        for f in fields(obj)
        if getattr(obj, f.name) != getattr(defaults, f.name)
    }


def save_config(config: Config, config_path: Path | None = None) -> None:
    """Persist ``config.yaml`` (infrastructure + policy only)."""
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
        if config.merged_label_color != Config.merged_label_color:
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

    pp_data = _non_default_fields(config.pr_policy, PRPolicyConfig())
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

    if config.ai_backend != Config.ai_backend:
        data["ai_backend"] = config.ai_backend
    api_data = _non_default_fields(config.ai_api, AIApiConfig())
    if api_data:
        data["ai_api"] = api_data
    codex_data = _non_default_fields(config.ai_codex, AICodexConfig())
    if codex_data:
        data["ai_codex"] = codex_data

    ai_data = _non_default_fields(config.ai_resolve, AIResolveConfig())
    if "auto_add_prerequisite_prs" in ai_data:
        # Always the mapping form, even if the user wrote a bare bool.
        ai_data["auto_add_prerequisite_prs"] = asdict(
            config.ai_resolve.auto_add_prerequisite_prs
        )
    if ai_data:
        data["ai_resolve"] = ai_data

    aic_data = _non_default_fields(config.ai_changelog, AIChangelogConfig())
    if aic_data:
        data["ai_changelog"] = aic_data

    rr_data = _non_default_fields(config.review_response, ReviewResponseConfig())
    if rr_data:
        data["review_response"] = rr_data

    if config.push:
        data["push"] = True

    if config.sequential:
        data["sequential"] = True

    with open(config_path, "w") as f:
        yaml.dump(data, f, default_flow_style=False, sort_keys=False)


def default_session_stem(name: str, target_branch: str | None) -> str:
    """Filename-safe ``target_branch`` (falling back to ``name``) for the default session file."""
    raw = (target_branch or "").strip() or name
    stem = re.sub(r"[^A-Za-z0-9._-]+", "-", raw).strip("-")
    return stem or name


def session_file_path(
    config: Config, cli_override: Path | None = None,
) -> Path:
    """``--session-file`` > ``session_file:`` (relative to config dir) > ``<config-dir>/<stem>.session.yaml``."""
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
    """Combined ``include_prs`` / group / per-PR ai_context for ``pr_url`` (label-based context excluded)."""
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
        # A PR can be in only one group (enforced at load time).
        break

    return "\n\n".join(parts)


def _parse_pr_url_entries(
    raw: list, *, where: str, value_key: str = "ai_context",
) -> tuple[list[str], dict[str, str]]:
    """Parse a list of bare URLs and/or ``{url, <value_key>}`` dicts into ``(urls, url → value)``."""
    if not isinstance(raw, list):
        raise ValueError(f"{where} must be a list, got {type(raw).__name__}")
    urls: list[str] = []
    values: dict[str, str] = {}
    seen: set[str] = set()
    for idx, entry in enumerate(raw):
        if isinstance(entry, str):
            url = entry.strip()
            value = ""
        elif isinstance(entry, dict):
            url = (entry.get("url") or "").strip()
            if not url:
                raise ValueError(
                    f"{where}[{idx}]: dict entry must specify 'url'"
                )
            value = (entry.get(value_key) or "").strip()
            extra = set(entry.keys()) - {"url", value_key}
            if extra:
                raise ValueError(
                    f"{where}[{idx}]: unknown keys {sorted(extra)} "
                    f"(allowed: 'url', {value_key!r})"
                )
        else:
            raise ValueError(
                f"{where}[{idx}]: must be a URL string or "
                f"{{url, {value_key}}} mapping, got {type(entry).__name__}"
            )
        if url in seen:
            raise ValueError(f"{where}: duplicate URL {url!r}")
        seen.add(url)
        urls.append(url)
        if value:
            values[url] = value
    return urls, values


def resolve_deps_file_path(
    session_path: Path, deps_file: str | None,
) -> Path:
    """``deps_file`` (relative to the session dir), else ``<session-stem>.deps.yaml`` next to it."""
    if deps_file:
        p = Path(deps_file)
        if not p.is_absolute():
            p = (session_path.parent / p).resolve()
        return p
    name = session_path.name
    for suffix in (".yaml", ".yml"):
        if name.endswith(suffix):
            stem = name[: -len(suffix)]
            return session_path.with_name(f"{stem}.deps.yaml")
    return session_path.with_name(f"{name}.deps.yaml")


def load_session(
    config: Config, cli_override: Path | None = None,
) -> SessionConfig:
    """Load and validate the session file for ``config``; raises FileNotFoundError if missing."""
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
                enabled=feat_raw.get("enabled", FeatureConfig.enabled),
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
        entry_mode = entry.get("mode", PRSourceConfig.mode)
        if entry_mode not in _VALID_PORT_MODES:
            raise ValueError(
                f"pr_sources.by_labels[].mode must be one of "
                f"{_VALID_PORT_MODES}, got {entry_mode!r}"
            )
        by_labels.append(
            PRSourceConfig(
                labels=raw_labels,
                description=entry.get("description", PRSourceConfig.description),
                merged_only=entry.get("merged_only", PRSourceConfig.merged_only),
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
        group_sort = entry.get("sort", PRGroupConfig.sort)
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
        group_mode = entry.get("mode", PRGroupConfig.mode)
        if group_mode not in _VALID_PORT_MODES:
            raise ValueError(
                f"{source_label}: groups[{gid!r}].mode must be one of "
                f"{_VALID_PORT_MODES}, got {group_mode!r}"
            )
        return PRGroupConfig(
            id=gid,
            prs=prs_list,
            description=entry.get("description", PRGroupConfig.description),
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

    # Deps-file overlay is purely additive: main session entries win on id clash.
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
        # Only an explicitly configured missing file is worth a warning.
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

    on_hold_list, on_hold_reasons = _parse_pr_url_entries(
        ps_raw.get("on_hold", []) or [],
        where="pr_sources.on_hold",
        value_key="reason",
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
        on_hold=on_hold_list,
        on_hold_reasons=on_hold_reasons,
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
        on_hold_list,
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


# Mirrors ``_singleton_feature_id`` in pipeline.py (not importable here:
# circular import).
_SINGLETON_FEATURE_ID_RE = re.compile(
    r"^(?:[A-Za-z0-9._-]+-[A-Za-z0-9._-]+-)?pr-\d+$"
)
_PR_URL_PREFIX_RE = re.compile(r"^https?://", re.IGNORECASE)


def _warn_on_redundant_pr_listings(
    groups: list[PRGroupConfig],
    include_prs: list[str],
    exclude_prs: list[str],
    overlay_warnings: list[str],
    on_hold: list[str] | None = None,
) -> None:
    """Warn about PR URLs listed in two places where one listing has no effect."""
    group_pr_to_id: dict[str, str] = {}
    for g in groups:
        for url in g.prs:
            group_pr_to_id[url] = g.id

    include_set = set(include_prs)
    exclude_set = set(exclude_prs)

    for url in set(on_hold or []) & exclude_set:
        overlay_warnings.append(
            f"PR {url} appears in both pr_sources.on_hold and "
            "pr_sources.exclude_prs; the veto already keeps it out, so the "
            "hold entry has no effect"
        )

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
    """Reject unknown ``depends_on`` refs and cycles among groups.

    Accepted refs: a group id, a listed PR URL, or a singleton feature_id
    (``pr-<N>`` / ``<owner>-<repo>-pr-<N>``, resolved at run time). An
    unlisted PR URL only warns.
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
                overlay_warnings.append(
                    f"group {g.id!r}.depends_on references PR URL "
                    f"{dep!r} which is not in include_prs / any group; "
                    "the dependent will stay blocked until a unit with "
                    "that URL appears in state"
                )
                continue
            if _SINGLETON_FEATURE_ID_RE.match(dep):
                continue
            raise ValueError(
                f"group {g.id!r}.depends_on references unknown unit "
                f"{dep!r}: must be a group id, a PR URL listed in "
                "include_prs / any group's prs, or a feature_id of the "
                "form 'pr-<N>' / '<owner>-<repo>-pr-<N>'."
            )

    # Cycle check covers group-id edges only.
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
    """Persist the session file to ``path`` or ``session.session_path``."""
    target = path or session.session_path
    if target is None:
        raise ValueError(
            "save_session: no path to write to — pass `path` or set "
            "`session.session_path` before calling."
        )

    data: dict = {}

    def _dump_pr_url_list(
        urls: list[str], values: dict[str, str], key: str = "ai_context",
    ) -> list:
        out: list = []
        for url in urls:
            val = values.get(url, "")
            if val:
                out.append({"url": url, key: val})
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
                    "mode": entry.mode if entry.mode != PRSourceConfig.mode else None,
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
    if ps.on_hold:
        ps_data["on_hold"] = _dump_pr_url_list(
            ps.on_hold, ps.on_hold_reasons, "reason",
        )
    if ps.include_authors:
        ps_data["include_authors"] = ps.include_authors
    if ps.exclude_authors:
        ps_data["exclude_authors"] = ps.exclude_authors
    if ps.deps_file:
        ps_data["deps_file"] = ps.deps_file
    # Auto-discovered groups belong to the deps_file sidecar.
    main_groups = [g for g in ps.groups if not g.auto_discovered]
    if main_groups:
        ps_data["groups"] = [
            {
                k: v
                for k, v in {
                    "id": g.id,
                    "description": g.description or None,
                    "if_exists": g.if_exists,
                    "sort": g.sort if g.sort != PRGroupConfig.sort else None,
                    "ai_context": g.ai_context or None,
                    "mode": g.mode if g.mode != PRGroupConfig.mode else None,
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
    """In-memory ``Config`` for stateless flows; never pass it to state/lock I/O.

    ``ai_prompt_file`` defaults to the bundled ``prompts/resolve_conflict.md``.
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
    """Apply non-None ``analyze-fails`` CLI overrides to ``config`` in place."""
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
    """In-memory ``Config`` for ``releasy analyze-fails`` without a ``config.yaml``."""
    if prompt_file is None:
        bundled = (
            Path(__file__).parent / "prompts" / "analyze_fails.md"
        ).resolve()
        prompt_file = str(bundled)
    # No project dir to resolve a relative template against.
    verify_prompt_file = str(
        (Path(__file__).parent / "prompts" / "verify_analysis.md").resolve()
    )

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
        verify_prompt_file=verify_prompt_file,
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


def make_stateless_config_for_repo(work_dir: Path) -> Config:
    """In-memory ``Config`` whose origin/upstream come from the clone's git remotes."""
    # Local import: git_ops imports Config from this module.
    from releasy.git_ops import run_git

    repo = work_dir.expanduser().resolve()
    if not (repo / ".git").exists():
        raise ValueError(
            f"--work-dir {repo} is not a git clone. Without a config.yaml "
            "the origin remote is read from the clone, so point --work-dir "
            "at an existing checkout of the repo you're releasing."
        )

    def _remote_url(name: str) -> str | None:
        result = run_git(["remote", "get-url", name], repo, check=False)
        if result.returncode != 0:
            return None
        return (result.stdout or "").strip() or None

    origin_url = _remote_url("origin")
    if not origin_url:
        raise ValueError(
            f"The clone at {repo} has no 'origin' remote — cannot tell "
            "which GitHub repo to query."
        )

    config = make_stateless_config(origin_url, work_dir=repo, push=False)
    upstream_url = _remote_url("upstream")
    if upstream_url:
        config.upstream = UpstreamConfig(remote=upstream_url)
    return config


def is_stateless(config: Config) -> bool:
    return config.stateless


def get_github_token() -> str | None:
    return os.environ.get("RELEASY_GITHUB_TOKEN")


def get_ssh_key_path() -> str | None:
    return os.environ.get("RELEASY_SSH_KEY_PATH")
