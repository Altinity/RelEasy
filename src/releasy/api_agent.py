"""Anthropic API backend — run the agent loop in-process, no CLI.

Selected by ``ai_backend: api`` in config.yaml. Instead of spawning
``claude -p``, this drives the Messages API with an API token and executes
the tool calls locally (Bash / Read / Write / Edit / Glob / Grep, plus the
server-side web tools when the allow-list grants them).

The transcript is emitted as Claude Code's ``--output-format stream-json``
events, so every existing parser (assistant text, cost, session-exhaustion,
MISSING_PREREQS, iteration count) and the console renderer work unchanged.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import signal
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

_LOCAL_TOOLS = ("Bash", "Read", "Write", "Edit", "Glob", "Grep")

# Friendly CLI aliases (``--model opus``) are not valid API model IDs.
_MODEL_ALIASES = {
    "opus": "claude-opus-5",
    "sonnet": "claude-sonnet-5",
    "haiku": "claude-haiku-4-5",
    "fable": "claude-fable-5",
}

DEFAULT_MODEL = "claude-opus-5"


@dataclass
class ApiAgentSpec:
    """Everything the API backend needs for one invocation."""
    model: str = DEFAULT_MODEL
    effort: str | None = None
    max_tokens: int = 64000
    max_turns: int = 300
    thinking: bool = True
    # Claude-Code-style allow-list (``Read``, ``Bash(git:*)``, …). Empty
    # list ⇒ no tools at all (pure text generation).
    allowed_tools: list[str] = field(default_factory=list)
    api_key: str | None = None
    base_url: str | None = None
    max_retries: int = 5
    request_timeout_seconds: int = 1800
    bash_timeout_seconds: int = 3600
    tool_output_max_chars: int = 30000
    system_prompt_extra: str = ""

    def resolved_model(self) -> str:
        return _MODEL_ALIASES.get(self.model, self.model)


# ---------------------------------------------------------------------------
# Availability / allow-list
# ---------------------------------------------------------------------------


def check_available(spec: ApiAgentSpec) -> str | None:
    """Return an error string when this spec can't run, else ``None``."""
    try:
        import anthropic  # noqa: F401
    except ImportError:
        return (
            "ai_backend: api needs the 'anthropic' package — reinstall "
            "releasy (pip install -e .) or pip install anthropic"
        )
    if not spec.api_key:
        return (
            "no Anthropic API token found — set ANTHROPIC_API_KEY "
            "(or ai_api.api_key_env / ai_api.api_key in config)"
        )
    return None


def _parse_allowed(entries: list[str]) -> tuple[set[str], list[str] | None]:
    """Split an allow-list into (tool names, bash prefixes).

    Bash prefixes are the command heads granted by ``Bash(git:*)``-style
    entries. ``None`` means unrestricted (a bare ``Bash`` / ``Bash(*)``).
    """
    names: set[str] = set()
    prefixes: list[str] = []
    unrestricted_bash = False
    for raw in entries:
        entry = (raw or "").strip()
        if not entry:
            continue
        m = re.match(r"^([A-Za-z_][A-Za-z0-9_]*)\s*(?:\((.*)\))?$", entry)
        if not m:
            continue
        name, arg = m.group(1), (m.group(2) or "").strip()
        names.add(name)
        if name != "Bash":
            continue
        if not arg or arg == "*":
            unrestricted_bash = True
            continue
        head = arg.split(":", 1)[0].strip()
        if head in ("", "*"):
            unrestricted_bash = True
        elif head:
            prefixes.append(head)
    return names, (None if unrestricted_bash else prefixes)


# Operators that start a new command. Claude Code refuses compound
# commands outright; we accept them as long as every segment's head is
# individually allowed. A bare ``&`` is not a separator here (it would
# split ``2>&1``), so a backgrounded command is checked as one segment.
_OPERATORS = ("&&", "||", ";", "|")
_HEREDOC_RE = re.compile(r"<<-?\s*['\"]?([A-Za-z_][A-Za-z0-9_]*)['\"]?")


def _split_unquoted(text: str, operators: tuple[str, ...]) -> list[str]:
    """Split on ``operators`` that appear outside quotes.

    Keeps ``git commit -m "fix: a; b"`` in one piece — a naive split would
    turn the message into a bogus second command and get it denied.
    """
    parts: list[str] = []
    buf: list[str] = []
    quote: str | None = None
    i = 0
    while i < len(text):
        ch = text[i]
        if quote:
            buf.append(ch)
            if ch == "\\" and quote == '"' and i + 1 < len(text):
                buf.append(text[i + 1])
                i += 2
                continue
            if ch == quote:
                quote = None
            i += 1
            continue
        if ch in "'\"":
            quote = ch
            buf.append(ch)
            i += 1
            continue
        for op in operators:
            if text.startswith(op, i):
                parts.append("".join(buf))
                buf = []
                i += len(op)
                break
        else:
            buf.append(ch)
            i += 1
    parts.append("".join(buf))
    return parts


def _command_heads(command: str) -> list[str]:
    """The executable each segment of ``command`` would run."""
    heads: list[str] = []
    heredoc_marker: str | None = None
    for line in _split_unquoted(command, ("\n",)):
        # Heredoc bodies are data, not commands — skip to the terminator.
        if heredoc_marker is not None:
            if line.strip() == heredoc_marker:
                heredoc_marker = None
            continue
        m = _HEREDOC_RE.search(line)
        if m:
            heredoc_marker = m.group(1)
        for segment in _split_unquoted(line, _OPERATORS):
            segment = segment.strip().lstrip("(").strip()
            if not segment:
                continue
            try:
                words = shlex.split(segment)
            except ValueError:
                words = segment.split()
            for word in words:
                # Skip leading VAR=value assignments.
                if re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", word):
                    continue
                heads.append(word)
                break
    return heads


def _head_allowed(head: str, prefixes: list[str]) -> bool:
    base = os.path.basename(head)
    for prefix in prefixes:
        if head == prefix or base == prefix or base == os.path.basename(prefix):
            return True
        if head.endswith("/" + prefix) or head == "./" + prefix:
            return True
    return False


def _check_bash(command: str, prefixes: list[str] | None) -> str | None:
    """Return a denial message when ``command`` is outside the allow-list."""
    if prefixes is None:
        return None
    if not prefixes:
        return "Bash is not allowed for this task."
    for head in _command_heads(command):
        if not _head_allowed(head, prefixes):
            return (
                f"command '{head}' is not in the allow-list. Allowed: "
                + ", ".join(sorted(set(prefixes)))
            )
    return None


# ---------------------------------------------------------------------------
# Tool schemas
# ---------------------------------------------------------------------------


def _tool_defs(names: set[str], prefixes: list[str] | None) -> list[dict]:
    """Anthropic tool definitions for the granted tools."""
    defs: list[dict] = []
    if "Bash" in names:
        allowed = (
            "Any command is allowed." if prefixes is None
            else "Allowed command heads: " + ", ".join(sorted(set(prefixes)))
            + ". Compound commands are fine as long as every segment "
              "starts with an allowed head."
        )
        defs.append({
            "name": "Bash",
            "description": (
                "Run a shell command in the repository working directory. "
                "Output is stdout+stderr combined and truncated when huge; "
                "redirect to a file and read it back if you need more. "
                + allowed
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "command": {"type": "string", "description": "Command to run."},
                    "timeout": {
                        "type": "integer",
                        "description": "Optional timeout in seconds.",
                    },
                },
                "required": ["command"],
            },
        })
    if "Read" in names:
        defs.append({
            "name": "Read",
            "description": (
                "Read a text file. Returns lines prefixed with line numbers "
                "(the numbers are display-only; never include them in Edit "
                "or Write content)."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "file_path": {"type": "string"},
                    "offset": {
                        "type": "integer",
                        "description": "1-based first line to read.",
                    },
                    "limit": {"type": "integer", "description": "Line count."},
                },
                "required": ["file_path"],
            },
        })
    if "Write" in names:
        defs.append({
            "name": "Write",
            "description": "Write a file, creating or overwriting it.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "file_path": {"type": "string"},
                    "content": {"type": "string"},
                },
                "required": ["file_path", "content"],
            },
        })
    if "Edit" in names:
        defs.append({
            "name": "Edit",
            "description": (
                "Replace an exact string in a file. old_string must match "
                "byte-for-byte (including indentation) and be unique unless "
                "replace_all is true."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "file_path": {"type": "string"},
                    "old_string": {"type": "string"},
                    "new_string": {"type": "string"},
                    "replace_all": {"type": "boolean"},
                },
                "required": ["file_path", "old_string", "new_string"],
            },
        })
    if "Glob" in names:
        defs.append({
            "name": "Glob",
            "description": (
                "List files matching a glob pattern (supports **), newest "
                "first."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "pattern": {"type": "string"},
                    "path": {
                        "type": "string",
                        "description": "Directory to search in. Defaults to cwd.",
                    },
                },
                "required": ["pattern"],
            },
        })
    if "Grep" in names:
        defs.append({
            "name": "Grep",
            "description": (
                "Search file contents with a regular expression."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "pattern": {"type": "string"},
                    "path": {"type": "string"},
                    "glob": {
                        "type": "string",
                        "description": "Restrict to files matching this glob.",
                    },
                    "output_mode": {
                        "type": "string",
                        "enum": ["files_with_matches", "content", "count"],
                    },
                    "-i": {"type": "boolean", "description": "Case-insensitive."},
                    "head_limit": {"type": "integer"},
                },
                "required": ["pattern"],
            },
        })
    if "WebSearch" in names:
        defs.append({"type": "web_search_20260209", "name": "web_search"})
    if "WebFetch" in names:
        defs.append({"type": "web_fetch_20260209", "name": "web_fetch"})
    return defs


_SYSTEM_PROMPT = """\
You are an autonomous engineering agent driving a git repository from a \
headless runner. There is no human to answer questions: never ask for \
confirmation, never run interactive commands (use --no-pager, -y, \
--no-edit), and keep working until the task is done or provably stuck.

Working directory: {cwd} (all relative paths resolve there).

Rules:
- Use the tools to inspect and change the repo. Never claim you ran a \
command you did not run, and never report success you have not verified.
- Prefer Edit over Write for existing files. Read a file before editing it.
- Long-running build/test commands belong in Bash; redirect noisy output to \
a file and read the interesting part back.
- Follow the task's output contract exactly. When it asks for a marker line \
(DONE, UNRESOLVED, BUILD FAILED, MISSING_PREREQS: …), print it verbatim on \
its own line as the last thing you say.
"""


def _system_prompt(repo_path: Path, spec: ApiAgentSpec) -> str:
    text = _SYSTEM_PROMPT.format(cwd=repo_path)
    if spec.system_prompt_extra.strip():
        text += "\n" + spec.system_prompt_extra.strip() + "\n"
    return text


# ---------------------------------------------------------------------------
# Tool execution
# ---------------------------------------------------------------------------


def _truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    head = text[: limit // 2]
    tail = text[-limit // 2:]
    dropped = len(text) - len(head) - len(tail)
    return f"{head}\n… [{dropped} characters truncated] …\n{tail}"


def _resolve_path(repo_path: Path, raw: str) -> Path:
    p = Path(raw).expanduser()
    return p if p.is_absolute() else (repo_path / p)


def _as_int(value: Any, default: int) -> int:
    """Coerce a model-supplied numeric argument, e.g. ``"65, "`` -> 65."""
    if isinstance(value, bool) or value is None or value == "":
        return default
    if isinstance(value, (int, float)):
        return int(value)
    match = re.search(r"-?\d+", str(value))
    return int(match.group()) if match else default


def _run_bash(
    command: str, repo_path: Path, timeout: int, limit: int,
) -> tuple[str, bool]:
    proc = subprocess.Popen(
        ["bash", "-c", command],
        cwd=repo_path,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        errors="replace",
        start_new_session=True,
    )
    try:
        out, _ = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        _kill_group(proc)
        out, _ = proc.communicate()
        return (
            _truncate(out or "", limit)
            + f"\n[command timed out after {timeout}s]",
            True,
        )
    except KeyboardInterrupt:
        _kill_group(proc)
        raise
    code = proc.returncode or 0
    body = _truncate(out or "", limit)
    if code != 0:
        return f"{body}\n[exit code: {code}]", True
    return (body or "[no output]"), False


def _kill_group(proc: subprocess.Popen) -> None:
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
    except (ProcessLookupError, OSError):
        return
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except (ProcessLookupError, OSError):
            pass


def _do_read(inp: dict, repo_path: Path, limit: int) -> tuple[str, bool]:
    path = _resolve_path(repo_path, str(inp.get("file_path", "")))
    if not path.exists():
        return f"file not found: {path}", True
    if path.is_dir():
        return f"{path} is a directory (use Glob / Bash ls)", True
    try:
        text = path.read_text(errors="replace")
    except OSError as exc:
        return f"could not read {path}: {exc}", True
    lines = text.splitlines()
    offset = max(1, _as_int(inp.get("offset"), 1))
    count = max(1, _as_int(inp.get("limit"), 2000))
    chunk = lines[offset - 1: offset - 1 + count]
    if not chunk:
        return f"[{path} has {len(lines)} lines; offset {offset} is past the end]", False
    numbered = "\n".join(
        f"{offset + i}\t{line}" for i, line in enumerate(chunk)
    )
    return _truncate(numbered, limit), False


def _do_write(inp: dict, repo_path: Path) -> tuple[str, bool]:
    path = _resolve_path(repo_path, str(inp.get("file_path", "")))
    content = inp.get("content")
    if not isinstance(content, str):
        return "content must be a string", True
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    except OSError as exc:
        return f"could not write {path}: {exc}", True
    return f"wrote {path} ({len(content)} bytes)", False


def _do_edit(inp: dict, repo_path: Path) -> tuple[str, bool]:
    path = _resolve_path(repo_path, str(inp.get("file_path", "")))
    old = inp.get("old_string")
    new = inp.get("new_string")
    if not isinstance(old, str) or not isinstance(new, str):
        return "old_string and new_string must be strings", True
    if not path.exists():
        return f"file not found: {path}", True
    try:
        text = path.read_text(errors="replace")
    except OSError as exc:
        return f"could not read {path}: {exc}", True
    hits = text.count(old)
    if hits == 0:
        return (
            f"old_string not found in {path} — Read the file and match the "
            "exact text including indentation",
            True,
        )
    if hits > 1 and not inp.get("replace_all"):
        return (
            f"old_string occurs {hits} times in {path}; add more context to "
            "make it unique or pass replace_all",
            True,
        )
    updated = text.replace(old, new) if inp.get("replace_all") else text.replace(old, new, 1)
    try:
        path.write_text(updated)
    except OSError as exc:
        return f"could not write {path}: {exc}", True
    return f"edited {path} ({hits if inp.get('replace_all') else 1} replacement)", False


def _do_glob(inp: dict, repo_path: Path, limit: int) -> tuple[str, bool]:
    pattern = str(inp.get("pattern", ""))
    if not pattern:
        return "pattern is required", True
    base = _resolve_path(repo_path, str(inp.get("path") or "."))
    try:
        matches = [p for p in base.glob(pattern) if p.is_file()]
    except (OSError, ValueError) as exc:
        return f"bad glob: {exc}", True
    if not matches:
        return "[no matches]", False
    matches.sort(key=lambda p: p.stat().st_mtime if p.exists() else 0, reverse=True)
    return _truncate("\n".join(str(p) for p in matches[:1000]), limit), False


def _do_grep(inp: dict, repo_path: Path, limit: int) -> tuple[str, bool]:
    pattern = str(inp.get("pattern", ""))
    if not pattern:
        return "pattern is required", True
    mode = str(inp.get("output_mode") or "files_with_matches")
    target = str(inp.get("path") or ".")
    head = inp.get("head_limit")
    rg = shutil.which("rg")
    if rg:
        argv = [rg, "--color=never"]
        if inp.get("-i"):
            argv.append("-i")
        if mode == "files_with_matches":
            argv.append("-l")
        elif mode == "count":
            argv.append("-c")
        else:
            argv += ["-n", "--no-heading"]
        if inp.get("glob"):
            argv += ["--glob", str(inp["glob"])]
        argv += ["-e", pattern, target]
        try:
            res = subprocess.run(
                argv, cwd=repo_path, capture_output=True, text=True,
                errors="replace", timeout=180,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            return f"grep failed: {exc}", True
        out = res.stdout
        if res.returncode not in (0, 1):
            return (res.stderr or f"rg exited {res.returncode}").strip(), True
    else:
        out = _python_grep(pattern, repo_path, inp, mode)
        if out is None:
            return f"bad regex: {pattern}", True
    lines = out.splitlines()
    if head:
        lines = lines[: max(1, _as_int(head, len(lines)))]
    if not lines:
        return "[no matches]", False
    return _truncate("\n".join(lines), limit), False


def _python_grep(
    pattern: str, repo_path: Path, inp: dict, mode: str,
) -> str | None:
    """``rg``-less fallback: walk the tree with ``re``."""
    try:
        rx = re.compile(pattern, re.IGNORECASE if inp.get("-i") else 0)
    except re.error:
        return None
    base = _resolve_path(repo_path, str(inp.get("path") or "."))
    file_glob = str(inp.get("glob") or "")
    out: list[str] = []
    for path in sorted(base.rglob("*")):
        if not path.is_file() or ".git/" in str(path):
            continue
        if file_glob and not path.match(file_glob):
            continue
        try:
            text = path.read_text(errors="replace")
        except OSError:
            continue
        hits = [
            (i + 1, line) for i, line in enumerate(text.splitlines())
            if rx.search(line)
        ]
        if not hits:
            continue
        if mode == "files_with_matches":
            out.append(str(path))
        elif mode == "count":
            out.append(f"{path}:{len(hits)}")
        else:
            out += [f"{path}:{n}:{line}" for n, line in hits]
    return "\n".join(out)


def _execute_tool(
    name: str, inp: dict, repo_path: Path, spec: ApiAgentSpec,
    prefixes: list[str] | None, deadline: float,
) -> tuple[str, bool]:
    """Run one tool call. Returns ``(result_text, is_error)``.

    A handler crash is reported back to the model, not raised: a malformed
    tool argument must not abort the run.
    """
    if not isinstance(inp, dict):
        return f"{name}: input must be an object", True
    try:
        return _dispatch_tool(name, inp, repo_path, spec, prefixes, deadline)
    except KeyboardInterrupt:
        raise
    except Exception as exc:  # noqa: BLE001 - report, don't kill the run
        return f"{name} failed: {type(exc).__name__}: {exc}", True


def _dispatch_tool(
    name: str, inp: dict, repo_path: Path, spec: ApiAgentSpec,
    prefixes: list[str] | None, deadline: float,
) -> tuple[str, bool]:
    limit = spec.tool_output_max_chars
    if name == "Bash":
        command = str(inp.get("command", "")).strip()
        if not command:
            return "command is required", True
        denial = _check_bash(command, prefixes)
        if denial:
            return denial, True
        budget = max(1, _as_int(inp.get("timeout"), spec.bash_timeout_seconds))
        remaining = int(max(1, deadline - time.monotonic()))
        return _run_bash(command, repo_path, min(budget, remaining), limit)
    if name == "Read":
        return _do_read(inp, repo_path, limit)
    if name == "Write":
        return _do_write(inp, repo_path)
    if name == "Edit":
        return _do_edit(inp, repo_path)
    if name == "Glob":
        return _do_glob(inp, repo_path, limit)
    if name == "Grep":
        return _do_grep(inp, repo_path, limit)
    return f"unknown tool: {name}", True


# ---------------------------------------------------------------------------
# Cost
# ---------------------------------------------------------------------------

# USD per million tokens (input, output). Cache reads bill at 0.1x input,
# cache writes at 1.25x. Server-tool surcharges are not counted.
_PRICES: dict[str, tuple[float, float]] = {
    "claude-fable-5": (10.0, 50.0),
    "claude-mythos-5": (10.0, 50.0),
    "claude-opus-5": (5.0, 25.0),
    "claude-opus-4": (5.0, 25.0),
    "claude-sonnet-5": (3.0, 15.0),
    "claude-sonnet-4": (3.0, 15.0),
    "claude-haiku-4": (1.0, 5.0),
}


def _prices(model: str) -> tuple[float, float]:
    for prefix in sorted(_PRICES, key=len, reverse=True):
        if model.startswith(prefix):
            return _PRICES[prefix]
    return _PRICES["claude-opus-5"]


def _usage_cost(model: str, usage: Any) -> float:
    inp, out = _prices(model)

    def _get(attr: str) -> int:
        value = getattr(usage, attr, None)
        return int(value) if isinstance(value, int) else 0

    return (
        _get("input_tokens") * inp
        + _get("cache_creation_input_tokens") * inp * 1.25
        + _get("cache_read_input_tokens") * inp * 0.1
        + _get("output_tokens") * out
    ) / 1_000_000


# ---------------------------------------------------------------------------
# The loop
# ---------------------------------------------------------------------------


def _blocks_to_json(content: Any) -> list[dict]:
    """Serialize SDK content blocks into stream-json-compatible dicts."""
    out: list[dict] = []
    for block in content or []:
        if hasattr(block, "model_dump"):
            try:
                out.append(block.model_dump(mode="json", exclude_none=True))
                continue
            except Exception:  # pragma: no cover - defensive
                pass
        if isinstance(block, dict):
            out.append(block)
        else:
            out.append({"type": getattr(block, "type", "unknown")})
    return out


# Retryable error *types* from the response body, in match order. The API
# can report one of these as an error event inside an otherwise-200
# response, where the status code says nothing about retryability — so the
# body has to be classified too. Each maps to the wording the transient /
# exhaustion detectors in ai_resolve look for.
_RETRYABLE_BODY_ERRORS = (
    ("overloaded_error", "Overloaded"),
    ("rate_limit_error", "429 rate limit reached"),
    ("timeout_error", "Request timed out"),
    # Anthropic's generic "unexpected internal error"; retryable per docs.
    ("api_error", "500 internal error"),
)


def _api_error_line(exc: Exception) -> str:
    """Map an SDK exception to a transcript line.

    The ``API Error: …`` wording is deliberate: it is what the existing
    transient-retry (5xx / connection / timeout) and session-exhaustion
    (429) detectors match on, so API-mode failures get the same backoff /
    wait treatment as CLI-mode ones. Errors that must NOT be retried
    (400/401/403/404, invalid_request_error) are reported without that
    prefix.
    """
    import anthropic

    msg = str(getattr(exc, "message", None) or exc).strip().replace("\n", " ")
    if isinstance(exc, anthropic.APITimeoutError):
        return "API Error: Request timed out"
    if isinstance(exc, anthropic.APIConnectionError):
        return f"API Error: Connection reset: {msg}"
    status = getattr(exc, "status_code", None)
    if status == 429:
        return f"API Error: 429 rate limit reached: {msg}"
    if isinstance(status, int) and status >= 500:
        overloaded = " Overloaded" if status == 529 else ""
        return f"API Error: {status}{overloaded}: {msg}"
    lowered = msg.lower()
    for needle, wording in _RETRYABLE_BODY_ERRORS:
        if needle in lowered:
            return f"API Error: {wording}: {msg}"
    return f"[runner] anthropic error ({status or type(exc).__name__}): {msg}"


_OPTIONAL_PARAMS = ("thinking", "output_config", "cache_control")
_UNEXPECTED_KWARG_RE = re.compile(r"unexpected keyword argument '([^']+)'")


def _call_model(client: Any, kwargs: dict, on_activity: Callable[[], None]) -> Any:
    """One streamed Messages request → the accumulated final message.

    Streaming keeps the connection alive on long turns and lets the caller
    show a heartbeat. An installed SDK that predates one of the optional
    request fields rejects it with ``TypeError``; that field is then moved
    to ``extra_body`` so it still reaches the API.
    """
    def _stream(params: dict) -> Any:
        with client.messages.stream(**params) as stream:
            for _ in stream:
                on_activity()
            return stream.get_final_message()

    for _ in range(len(_OPTIONAL_PARAMS) + 1):
        try:
            return _stream(kwargs)
        except TypeError as exc:
            m = _UNEXPECTED_KWARG_RE.search(str(exc))
            key = m.group(1) if m else None
            if key not in _OPTIONAL_PARAMS or key not in kwargs:
                raise
            kwargs["extra_body"] = {
                **kwargs.get("extra_body", {}), key: kwargs.pop(key),
            }
    return _stream(kwargs)


def run_agent(
    spec: ApiAgentSpec,
    repo_path: Path,
    timeout: int,
    prompt: str,
    on_event: Callable[[str], None] | None = None,
) -> tuple[int, str, bool]:
    """Run the agent loop against the Messages API.

    Mirrors ``_spawn_claude_once``: returns
    ``(exit_code, stream_json_transcript, timed_out)``. Each transcript
    line is also handed to ``on_event`` as it is produced so the caller can
    render progress live.
    """
    import anthropic

    collected: list[str] = []

    def emit(event: dict) -> None:
        line = json.dumps(event)
        collected.append(line + "\n")
        if on_event is not None:
            on_event(line)

    model = spec.resolved_model()
    names, prefixes = _parse_allowed(spec.allowed_tools)
    names = {n for n in names if n in _LOCAL_TOOLS or n in ("WebSearch", "WebFetch")}
    tools = _tool_defs(names, prefixes)

    client = anthropic.Anthropic(
        api_key=spec.api_key,
        base_url=spec.base_url or None,
        max_retries=spec.max_retries,
        timeout=float(spec.request_timeout_seconds),
    )

    emit({
        "type": "system",
        "subtype": "init",
        "model": model,
        "tools": [t.get("name") for t in tools],
    })

    messages: list[dict] = [{"role": "user", "content": prompt}]
    start = time.monotonic()
    deadline = start + timeout
    cost = 0.0
    turns = 0
    final_text = ""
    exit_code = 0
    timed_out = False
    subtype = "success"

    last_activity = [time.monotonic()]

    def _touch() -> None:
        last_activity[0] = time.monotonic()

    stop_heartbeat = threading.Event()
    threading.Thread(
        target=_heartbeat, args=(stop_heartbeat, last_activity, start), daemon=True,
    ).start()

    try:
        while True:
            if time.monotonic() > deadline:
                timed_out = True
                exit_code = -1
                subtype = "error_timeout"
                break
            if turns >= spec.max_turns:
                emit({
                    "type": "assistant",
                    "message": {"content": [{
                        "type": "text",
                        "text": f"[runner] stopping: max_turns ({spec.max_turns}) reached",
                    }]},
                })
                exit_code = 1
                subtype = "error_max_turns"
                break

            params: dict = {
                "model": model,
                "max_tokens": spec.max_tokens,
                "system": _system_prompt(repo_path, spec),
                "messages": messages,
                # Auto-caches the last cacheable block: the growing
                # conversation prefix is re-read instead of re-billed.
                "cache_control": {"type": "ephemeral"},
            }
            if tools:
                params["tools"] = tools
            if spec.thinking:
                params["thinking"] = {"type": "adaptive", "display": "summarized"}
            if spec.effort:
                params["output_config"] = {"effort": spec.effort}

            _touch()
            try:
                response = _call_model(client, params, _touch)
            except anthropic.APIError as exc:
                line = _api_error_line(exc)
                collected.append(line + "\n")
                if on_event is not None:
                    on_event(line)
                exit_code = 1
                subtype = "error_api"
                break

            turns += 1
            cost += _usage_cost(model, getattr(response, "usage", None))
            blocks = _blocks_to_json(getattr(response, "content", None))
            if blocks:
                emit({"type": "assistant", "message": {"content": blocks}})

            text = "\n".join(
                b.get("text", "") for b in blocks if b.get("type") == "text"
            ).strip()
            if text:
                final_text = text

            stop_reason = getattr(response, "stop_reason", None)

            if stop_reason == "refusal":
                details = getattr(response, "stop_details", None)
                category = getattr(details, "category", None)
                collected.append(
                    f"[runner] request refused by safety classifiers "
                    f"(category={category})\n"
                )
                exit_code = 1
                subtype = "error_refusal"
                break

            if stop_reason == "tool_use":
                results: list[dict] = []
                for block in blocks:
                    if block.get("type") != "tool_use":
                        continue
                    body, is_error = _execute_tool(
                        str(block.get("name", "")),
                        block.get("input") or {},
                        repo_path, spec, prefixes, deadline,
                    )
                    results.append({
                        "type": "tool_result",
                        "tool_use_id": block.get("id"),
                        "content": [{"type": "text", "text": body}],
                        **({"is_error": True} if is_error else {}),
                    })
                messages.append({"role": "assistant", "content": _replay(blocks)})
                messages.append({"role": "user", "content": results})
                emit({"type": "user", "message": {"content": results}})
                continue

            if stop_reason == "pause_turn":
                # A server-side tool (web search / fetch) hit its iteration
                # cap: resend as-is and the server resumes.
                messages.append({"role": "assistant", "content": _replay(blocks)})
                continue

            if stop_reason == "max_tokens":
                collected.append(
                    "[runner] response truncated: max_tokens reached\n"
                )
            break
    except KeyboardInterrupt:
        stop_heartbeat.set()
        raise
    finally:
        stop_heartbeat.set()

    emit({
        "type": "result",
        "subtype": subtype,
        "num_turns": turns,
        "duration_ms": int((time.monotonic() - start) * 1000),
        "total_cost_usd": round(cost, 6),
        "result": final_text,
    })
    return exit_code, "".join(collected), timed_out


def _replay(blocks: list[dict]) -> list[dict]:
    """Assistant blocks echoed back on the next turn.

    Thinking blocks must be replayed unchanged; everything else passes
    through as received.
    """
    return [b for b in blocks if b.get("type") != "fallback"]


def _heartbeat(
    stop: threading.Event, last_activity: list[float], start: float,
) -> None:
    from releasy.termlog import console

    while not stop.wait(15.0):
        idle = time.monotonic() - last_activity[0]
        if idle >= 30.0:
            elapsed = int(time.monotonic() - start)
            console.print(
                f"    [dim]│ [{elapsed}s] …still working (idle {int(idle)}s)[/dim]"
            )
