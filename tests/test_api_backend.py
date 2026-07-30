"""Unit tests for the Anthropic-API backend (``ai_backend: api``).

Covers the allow-list gate, the local tool implementations, cost accounting,
the agent loop (against a stub ``anthropic`` module — no network, no SDK
install), backend selection / dispatch, and config round-trip.

Stdlib unittest (no pytest dependency). Run:
    python3 -m unittest discover -s tests
"""
from __future__ import annotations

import os
import sys
import tempfile
import types
import unittest
from pathlib import Path

import releasy.ai_resolve as a
import releasy.api_agent as ag
from releasy.config import (
    AIApiConfig,
    Config,
    OriginConfig,
    load_config,
    save_config,
)


# ---------------------------------------------------------------------------
# Stub SDK
# ---------------------------------------------------------------------------


class _StubUsage:
    def __init__(self, inp=1000, out=500):
        self.input_tokens = inp
        self.output_tokens = out
        self.cache_creation_input_tokens = 0
        self.cache_read_input_tokens = 0


class _StubMessage:
    def __init__(self, content, stop_reason="end_turn", usage=None):
        self.content = content
        self.stop_reason = stop_reason
        self.usage = usage or _StubUsage()
        self.stop_details = None


class _StubStream:
    def __init__(self, message):
        self._message = message

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def __iter__(self):
        return iter(())

    def get_final_message(self):
        return self._message


class _StubMessages:
    def __init__(self, script, raise_on_first=None):
        self.script = list(script)
        self.calls: list[dict] = []
        self.raise_on_first = raise_on_first

    def stream(self, **kwargs):
        self.calls.append(kwargs)
        if self.raise_on_first is not None and len(self.calls) == 1:
            raise self.raise_on_first
        return _StubStream(self.script.pop(0))


def _install_stub_anthropic(script, raise_on_first=None) -> _StubMessages:
    """Register a fake ``anthropic`` module and return its messages stub."""
    messages = _StubMessages(script, raise_on_first)

    class _APIError(Exception):
        def __init__(self, message="boom", status_code=None):
            super().__init__(message)
            self.message = message
            self.status_code = status_code

    class _APITimeoutError(_APIError):
        pass

    class _APIConnectionError(_APIError):
        pass

    class _Anthropic:
        def __init__(self, **kwargs):
            self.kwargs = kwargs
            self.messages = messages

    mod = types.ModuleType("anthropic")
    mod.Anthropic = _Anthropic
    mod.APIError = _APIError
    mod.APITimeoutError = _APITimeoutError
    mod.APIConnectionError = _APIConnectionError
    sys.modules["anthropic"] = mod
    return messages


class _StubSDK(unittest.TestCase):
    """Base class that installs / removes the stub SDK."""

    def setUp(self):
        self._prev = sys.modules.get("anthropic")
        self._tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self._tmp.name)

    def tearDown(self):
        if self._prev is None:
            sys.modules.pop("anthropic", None)
        else:
            sys.modules["anthropic"] = self._prev
        self._tmp.cleanup()


# ---------------------------------------------------------------------------
# Allow-list
# ---------------------------------------------------------------------------


class AllowList(unittest.TestCase):
    def test_parses_names_and_bash_prefixes(self):
        names, prefixes = ag._parse_allowed(
            ["Read", "Edit", "Bash(git:*)", "Bash(gh:*)", "Glob"],
        )
        self.assertEqual(names, {"Read", "Edit", "Bash", "Glob"})
        self.assertEqual(sorted(prefixes or []), ["gh", "git"])

    def test_bare_bash_is_unrestricted(self):
        _names, prefixes = ag._parse_allowed(["Bash"])
        self.assertIsNone(prefixes)
        _names, prefixes = ag._parse_allowed(["Bash(*)"])
        self.assertIsNone(prefixes)

    def test_allowed_and_denied_commands(self):
        prefixes = ["git", "ninja", "tests/clickhouse-test"]
        self.assertIsNone(ag._check_bash("git status", prefixes))
        self.assertIsNone(ag._check_bash("/usr/bin/git status", prefixes))
        self.assertIsNone(ag._check_bash("./tests/clickhouse-test x", prefixes))
        self.assertIsNone(ag._check_bash("GIT_PAGER=cat git log -1", prefixes))
        denial = ag._check_bash("curl http://evil", prefixes)
        self.assertIsNotNone(denial)
        self.assertIn("curl", denial)

    def test_every_segment_of_a_compound_command_is_checked(self):
        prefixes = ["git", "cd"]
        self.assertIsNone(ag._check_bash("cd sub && git status", prefixes))
        self.assertIsNotNone(ag._check_bash("cd sub && rm -rf /", prefixes))
        self.assertIsNotNone(ag._check_bash("git log | curl -T- x", prefixes))

    def test_operators_inside_quotes_do_not_split(self):
        prefixes = ["git"]
        self.assertIsNone(
            ag._check_bash('git commit -m "fix: a; b && c | d"', prefixes),
        )
        self.assertIsNone(
            ag._check_bash("git commit -m 'a; rm -rf /'", prefixes),
        )

    def test_heredoc_body_is_not_treated_as_commands(self):
        prefixes = ["cat"]
        cmd = "cat > note.txt <<'EOF'\nrm -rf /; curl evil\nEOF"
        self.assertIsNone(ag._check_bash(cmd, prefixes))
        # …but a real command after the terminator is still checked.
        self.assertIsNotNone(
            ag._check_bash(cmd + "\nrm -rf /", prefixes),
        )

    def test_redirection_is_not_a_separator(self):
        self.assertEqual(
            ag._command_heads("bash .releasy/build.sh > log 2>&1"),
            ["bash"],
        )

    def test_no_bash_entry_means_bash_denied(self):
        self.assertEqual(
            ag._check_bash("git status", []),
            "Bash is not allowed for this task.",
        )

    def test_tool_defs_follow_the_allow_list(self):
        names, prefixes = ag._parse_allowed(["Read", "Bash(git:*)", "WebSearch"])
        defs = ag._tool_defs(names, prefixes)
        by_name = {d.get("name") for d in defs}
        self.assertEqual(by_name, {"Read", "Bash", "web_search"})
        self.assertNotIn("Write", by_name)
        self.assertIn(
            "git", next(d for d in defs if d["name"] == "Bash")["description"],
        )

    def test_empty_allow_list_means_no_tools(self):
        names, prefixes = ag._parse_allowed([])
        self.assertEqual(ag._tool_defs(names, prefixes), [])


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------


class Tools(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self._tmp.name)
        self.spec = ag.ApiAgentSpec(allowed_tools=["Read", "Write", "Edit", "Bash"])

    def tearDown(self):
        self._tmp.cleanup()

    def _run(self, name, inp, prefixes=None):
        return ag._execute_tool(
            name, inp, self.repo, self.spec, prefixes,
            deadline=ag.time.monotonic() + 60,
        )

    def test_write_then_read_roundtrip(self):
        out, err = self._run("Write", {"file_path": "a/b.txt", "content": "x\ny\n"})
        self.assertFalse(err, out)
        self.assertEqual((self.repo / "a/b.txt").read_text(), "x\ny\n")
        out, err = self._run("Read", {"file_path": "a/b.txt"})
        self.assertFalse(err)
        self.assertEqual(out.splitlines(), ["1\tx", "2\ty"])

    def test_read_offset_and_missing_file(self):
        (self.repo / "f.txt").write_text("1\n2\n3\n4\n")
        out, err = self._run("Read", {"file_path": "f.txt", "offset": 3, "limit": 1})
        self.assertFalse(err)
        self.assertEqual(out, "3\t3")
        out, err = self._run("Read", {"file_path": "nope.txt"})
        self.assertTrue(err)
        self.assertIn("not found", out)

    def test_edit_requires_a_unique_match(self):
        (self.repo / "f.txt").write_text("a\na\n")
        out, err = self._run(
            "Edit", {"file_path": "f.txt", "old_string": "a", "new_string": "b"},
        )
        self.assertTrue(err)
        self.assertIn("occurs 2 times", out)
        out, err = self._run("Edit", {
            "file_path": "f.txt", "old_string": "a", "new_string": "b",
            "replace_all": True,
        })
        self.assertFalse(err, out)
        self.assertEqual((self.repo / "f.txt").read_text(), "b\nb\n")

    def test_edit_reports_a_missing_old_string(self):
        (self.repo / "f.txt").write_text("hello\n")
        out, err = self._run(
            "Edit", {"file_path": "f.txt", "old_string": "zzz", "new_string": "x"},
        )
        self.assertTrue(err)
        self.assertIn("not found", out)

    def test_bash_runs_and_reports_exit_code(self):
        out, err = self._run("Bash", {"command": "echo hi"}, prefixes=None)
        self.assertFalse(err)
        self.assertIn("hi", out)
        out, err = self._run("Bash", {"command": "exit 3"}, prefixes=None)
        self.assertTrue(err)
        self.assertIn("[exit code: 3]", out)

    def test_bash_denial_never_executes(self):
        out, err = self._run(
            "Bash", {"command": "touch pwned"}, prefixes=["git"],
        )
        self.assertTrue(err)
        self.assertFalse((self.repo / "pwned").exists())

    def test_bash_timeout_is_reported(self):
        out, err = self._run(
            "Bash", {"command": "sleep 30", "timeout": 1}, prefixes=None,
        )
        self.assertTrue(err)
        self.assertIn("timed out", out)

    def test_glob_and_grep(self):
        (self.repo / "x.cpp").write_text("int needle = 1;\n")
        (self.repo / "y.txt").write_text("nothing\n")
        out, err = self._run("Glob", {"pattern": "*.cpp"})
        self.assertFalse(err)
        self.assertIn("x.cpp", out)
        out, err = self._run("Grep", {"pattern": "needle"})
        self.assertFalse(err)
        self.assertIn("x.cpp", out)
        out, err = self._run(
            "Grep", {"pattern": "needle", "output_mode": "content"},
        )
        self.assertFalse(err)
        self.assertIn("int needle", out)
        out, err = self._run("Grep", {"pattern": "absent-token"})
        self.assertFalse(err)
        self.assertEqual(out, "[no matches]")

    def test_python_grep_fallback_matches_rg_shape(self):
        (self.repo / "x.cpp").write_text("int needle = 1;\n")
        out = ag._python_grep("needle", self.repo, {}, "files_with_matches")
        self.assertIn("x.cpp", out)
        self.assertIsNone(ag._python_grep("[bad", self.repo, {}, "content"))

    def test_output_is_truncated(self):
        self.spec.tool_output_max_chars = 200
        (self.repo / "big.txt").write_text("line\n" * 5000)
        out, err = self._run("Read", {"file_path": "big.txt"})
        self.assertFalse(err)
        self.assertIn("truncated", out)
        self.assertLess(len(out), 400)


# ---------------------------------------------------------------------------
# Cost
# ---------------------------------------------------------------------------


class Cost(unittest.TestCase):
    def test_price_lookup_is_by_longest_prefix(self):
        self.assertEqual(ag._prices("claude-opus-5"), (5.0, 25.0))
        self.assertEqual(ag._prices("claude-sonnet-5"), (3.0, 15.0))
        self.assertEqual(ag._prices("claude-haiku-4-5"), (1.0, 5.0))
        # Unknown model falls back to Opus pricing rather than $0.
        self.assertEqual(ag._prices("claude-brand-new"), (5.0, 25.0))

    def test_usage_cost_counts_cache_tiers(self):
        usage = _StubUsage(inp=1_000_000, out=1_000_000)
        usage.cache_read_input_tokens = 1_000_000
        usage.cache_creation_input_tokens = 1_000_000
        cost = ag._usage_cost("claude-opus-5", usage)
        # 5 + 25 + (5 * 1.25) + (5 * 0.1)
        self.assertAlmostEqual(cost, 5 + 25 + 6.25 + 0.5, places=6)

    def test_missing_usage_is_free_not_a_crash(self):
        self.assertEqual(ag._usage_cost("claude-opus-5", None), 0.0)


# ---------------------------------------------------------------------------
# Agent loop
# ---------------------------------------------------------------------------


class AgentLoop(_StubSDK):
    def _spec(self, **kw):
        kw.setdefault("allowed_tools", ["Read", "Write", "Bash(git:*)"])
        kw.setdefault("api_key", "sk-test")
        return ag.ApiAgentSpec(**kw)

    def test_tool_call_then_finish(self):
        messages = _install_stub_anthropic([
            _StubMessage(
                [
                    {"type": "text", "text": "writing the file"},
                    {
                        "type": "tool_use", "id": "tu_1", "name": "Write",
                        "input": {"file_path": "out.txt", "content": "done"},
                    },
                ],
                stop_reason="tool_use",
            ),
            _StubMessage([{"type": "text", "text": "all set\nDONE"}]),
        ])
        seen: list[str] = []
        code, transcript, timed_out = ag.run_agent(
            self._spec(), self.repo, 60, "do the thing", seen.append,
        )
        self.assertEqual(code, 0)
        self.assertFalse(timed_out)
        # The tool really ran.
        self.assertEqual((self.repo / "out.txt").read_text(), "done")
        # Two model calls; the second one carries the tool result.
        self.assertEqual(len(messages.calls), 2)
        replayed = messages.calls[1]["messages"]
        self.assertEqual(replayed[-1]["role"], "user")
        self.assertEqual(replayed[-1]["content"][0]["type"], "tool_result")
        # Transcript is parseable by the existing CLI-mode helpers.
        self.assertIn("DONE", a._extract_assistant_text(transcript))
        self.assertIsNotNone(a._extract_cost_usd(transcript))
        self.assertTrue(any('"type": "system"' in line for line in seen))

    def test_prompt_and_tools_reach_the_api(self):
        _install_stub_anthropic([_StubMessage([{"type": "text", "text": "hi"}])])
        spec = self._spec(effort="high")
        code, _transcript, _to = ag.run_agent(spec, self.repo, 60, "PROMPT")
        self.assertEqual(code, 0)
        call = sys.modules["anthropic"].Anthropic().messages.calls[0]
        self.assertEqual(call["messages"][0]["content"], "PROMPT")
        self.assertEqual(call["model"], "claude-opus-5")
        self.assertEqual(call["output_config"], {"effort": "high"})
        self.assertEqual(call["thinking"]["type"], "adaptive")
        self.assertEqual(
            {t["name"] for t in call["tools"]}, {"Read", "Write", "Bash"},
        )
        self.assertIn(str(self.repo), call["system"])

    def test_no_tools_means_no_tools_param(self):
        _install_stub_anthropic([_StubMessage([{"type": "text", "text": "text"}])])
        ag.run_agent(self._spec(allowed_tools=[]), self.repo, 60, "P")
        self.assertNotIn("tools", sys.modules["anthropic"].Anthropic().messages.calls[0])

    def test_rate_limit_is_reported_as_session_exhaustion(self):
        stub = _install_stub_anthropic([])
        err = sys.modules["anthropic"].APIError("quota gone", status_code=429)
        stub.raise_on_first = err
        code, transcript, timed_out = ag.run_agent(
            self._spec(), self.repo, 60, "P",
        )
        self.assertEqual(code, 1)
        self.assertFalse(timed_out)
        # Reuses the CLI-mode detectors: this one waits, not retries.
        self.assertIsNotNone(a._find_session_exhausted(transcript))

    def test_server_error_is_reported_as_transient(self):
        stub = _install_stub_anthropic([])
        stub.raise_on_first = sys.modules["anthropic"].APIError(
            "overloaded", status_code=529,
        )
        code, transcript, _to = ag.run_agent(self._spec(), self.repo, 60, "P")
        self.assertEqual(code, 1)
        self.assertIsNotNone(a._find_transient_api_error(transcript))
        self.assertIsNone(a._find_session_exhausted(transcript))

    def test_bad_request_is_not_retried(self):
        stub = _install_stub_anthropic([])
        stub.raise_on_first = sys.modules["anthropic"].APIError(
            "bad schema", status_code=400,
        )
        code, transcript, _to = ag.run_agent(self._spec(), self.repo, 60, "P")
        self.assertEqual(code, 1)
        self.assertIsNone(a._find_transient_api_error(transcript))
        self.assertIsNone(a._find_session_exhausted(transcript))

    def test_max_turns_stops_the_loop(self):
        _install_stub_anthropic([
            _StubMessage(
                [{
                    "type": "tool_use", "id": f"tu_{i}", "name": "Read",
                    "input": {"file_path": "missing.txt"},
                }],
                stop_reason="tool_use",
            )
            for i in range(5)
        ])
        code, transcript, _to = ag.run_agent(
            self._spec(max_turns=2), self.repo, 60, "P",
        )
        self.assertEqual(code, 1)
        self.assertIn("error_max_turns", transcript)

    def test_pause_turn_resumes(self):
        _install_stub_anthropic([
            _StubMessage([{"type": "text", "text": "searching"}],
                         stop_reason="pause_turn"),
            _StubMessage([{"type": "text", "text": "done"}]),
        ])
        code, _transcript, _to = ag.run_agent(self._spec(), self.repo, 60, "P")
        self.assertEqual(code, 0)
        self.assertEqual(
            len(sys.modules["anthropic"].Anthropic().messages.calls), 2,
        )

    def test_refusal_fails_the_run(self):
        _install_stub_anthropic([_StubMessage([], stop_reason="refusal")])
        code, transcript, _to = ag.run_agent(self._spec(), self.repo, 60, "P")
        self.assertEqual(code, 1)
        self.assertIn("refused", transcript)

    def test_timeout_is_reported(self):
        _install_stub_anthropic([_StubMessage([{"type": "text", "text": "x"}])])
        code, _transcript, timed_out = ag.run_agent(
            self._spec(), self.repo, -1, "P",
        )
        self.assertTrue(timed_out)
        self.assertEqual(code, -1)

    def test_old_sdk_params_move_to_extra_body(self):
        """A param the installed SDK doesn't know is retried via extra_body."""
        stub = _install_stub_anthropic([
            _StubMessage([{"type": "text", "text": "ok"}]),
        ])
        real_stream = stub.stream
        rejected = {"n": 0}

        def picky_stream(**kwargs):
            if "output_config" in kwargs:
                rejected["n"] += 1
                raise TypeError(
                    "create() got an unexpected keyword argument 'output_config'"
                )
            return real_stream(**kwargs)

        stub.stream = picky_stream
        code, _transcript, _to = ag.run_agent(
            self._spec(effort="max"), self.repo, 60, "P",
        )
        self.assertEqual(code, 0)
        self.assertEqual(rejected["n"], 1)
        self.assertEqual(
            stub.calls[-1]["extra_body"]["output_config"], {"effort": "max"},
        )


class Availability(_StubSDK):
    def test_missing_token_is_reported(self):
        _install_stub_anthropic([])
        err = ag.check_available(ag.ApiAgentSpec(api_key=None))
        self.assertIn("API token", err or "")

    def test_missing_sdk_is_reported(self):
        sys.modules.pop("anthropic", None)
        sys.modules["anthropic"] = None  # import → ImportError
        try:
            err = ag.check_available(ag.ApiAgentSpec(api_key="sk-x"))
        finally:
            sys.modules.pop("anthropic", None)
        self.assertIn("anthropic", err or "")

    def test_ready_spec_passes(self):
        _install_stub_anthropic([])
        self.assertIsNone(ag.check_available(ag.ApiAgentSpec(api_key="sk-x")))


# ---------------------------------------------------------------------------
# Backend selection / dispatch
# ---------------------------------------------------------------------------


def _config(**kw) -> Config:
    return Config(
        name="t", origin=OriginConfig(remote="git@github.com:o/r.git"),
        project="t", **kw,
    )


class BackendSelection(unittest.TestCase):
    def setUp(self):
        self._prev_key = os.environ.get("ANTHROPIC_API_KEY")
        os.environ["ANTHROPIC_API_KEY"] = "sk-env"

    def tearDown(self):
        if self._prev_key is None:
            os.environ.pop("ANTHROPIC_API_KEY", None)
        else:
            os.environ["ANTHROPIC_API_KEY"] = self._prev_key

    def test_cli_backend_builds_no_spec(self):
        self.assertIsNone(a._build_api_spec(_config()))

    def test_api_spec_inherits_model_effort_and_tools(self):
        cfg = _config(ai_backend="api", ai_model="opus", ai_effort="xhigh")
        cfg.ai_resolve.allowed_tools = ["Read", "Bash(git:*)"]
        spec = a._build_api_spec(cfg)
        self.assertIsNotNone(spec)
        self.assertEqual(spec.effort, "xhigh")
        # CLI-style model aliases map to real API model IDs.
        self.assertEqual(spec.resolved_model(), "claude-opus-5")
        self.assertEqual(spec.allowed_tools, ["Read", "Bash(git:*)"])
        self.assertEqual(spec.api_key, "sk-env")

    def test_ai_api_model_wins_over_global_ai_model(self):
        cfg = _config(
            ai_backend="api", ai_model="sonnet",
            ai_api=AIApiConfig(model="claude-haiku-4-5"),
        )
        self.assertEqual(a._build_api_spec(cfg).resolved_model(), "claude-haiku-4-5")

    def test_token_comes_from_the_configured_env_var(self):
        os.environ["MY_TOKEN"] = "sk-custom"
        try:
            cfg = _config(
                ai_backend="api", ai_api=AIApiConfig(api_key_env="MY_TOKEN"),
            )
            self.assertEqual(a._build_api_spec(cfg).api_key, "sk-custom")
        finally:
            os.environ.pop("MY_TOKEN", None)

    def test_env_var_beats_inline_key(self):
        cfg = _config(
            ai_backend="api", ai_api=AIApiConfig(api_key="sk-inline"),
        )
        self.assertEqual(a._build_api_spec(cfg).api_key, "sk-env")

    def test_inline_key_used_when_env_is_unset(self):
        os.environ.pop("ANTHROPIC_API_KEY", None)
        cfg = _config(
            ai_backend="api", ai_api=AIApiConfig(api_key="sk-inline"),
        )
        self.assertEqual(a._build_api_spec(cfg).api_key, "sk-inline")

    def test_explicit_allowed_tools_override(self):
        cfg = _config(ai_backend="api")
        spec = a._build_api_spec(cfg, allowed_tools=["Grep"])
        self.assertEqual(spec.allowed_tools, ["Grep"])

    def test_backend_label(self):
        self.assertEqual(a._backend_label(_config(), "claude"), "claude")
        label = a._backend_label(_config(ai_backend="api"), "claude")
        self.assertIn("anthropic api", label)

    def test_resolve_backend_checks_the_binary_in_cli_mode(self):
        spec, err = a._resolve_backend(_config(), "definitely-not-on-path-xyz")
        self.assertIsNone(spec)
        self.assertIn("not found on PATH", err or "")

    def test_spawn_dispatches_to_the_api_backend(self):
        calls: list[tuple] = []
        orig = a._run_api_agent_once
        a._run_api_agent_once = lambda api, repo, timeout, prompt: (
            calls.append((api, prompt)) or (0, "DONE", False)
        )
        try:
            spec = a._build_api_spec(_config(ai_backend="api"))
            ec, out, to = a._spawn_claude(
                ["claude"], Path("."), 10, prompt="P", api=spec,
            )
        finally:
            a._run_api_agent_once = orig
        self.assertEqual((ec, out, to), (0, "DONE", False))
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][1], "P")

    def test_spawn_without_api_still_uses_the_cli(self):
        orig = a._spawn_claude_once
        a._spawn_claude_once = lambda argv, repo, timeout, prompt: (0, "CLI", False)
        try:
            ec, out, _to = a._spawn_claude(["claude"], Path("."), 10, prompt="P")
        finally:
            a._spawn_claude_once = orig
        self.assertEqual((ec, out), (0, "CLI"))


class ApiConfigParsing(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._prev = os.environ.get("RELEASY_STATE_DIR")
        os.environ["RELEASY_STATE_DIR"] = self._tmp.name
        self.path = Path(self._tmp.name) / "config.yaml"

    def tearDown(self):
        if self._prev is None:
            os.environ.pop("RELEASY_STATE_DIR", None)
        else:
            os.environ["RELEASY_STATE_DIR"] = self._prev
        self._tmp.cleanup()

    def _write(self, body: str) -> Config:
        self.path.write_text(
            "name: t\nproject: t\norigin:\n  remote: git@github.com:o/r.git\n"
            + body
        )
        return load_config(self.path)

    def test_defaults_to_the_cli_backend(self):
        cfg = self._write("")
        self.assertEqual(cfg.ai_backend, "cli")
        self.assertEqual(cfg.ai_api.api_key_env, "ANTHROPIC_API_KEY")

    def test_parses_ai_api_block(self):
        cfg = self._write(
            "ai_backend: api\n"
            "ai_api:\n"
            "  model: claude-sonnet-5\n"
            "  max_turns: 40\n"
            "  thinking: false\n"
            "  api_key_env: MY_TOKEN\n"
        )
        self.assertEqual(cfg.ai_backend, "api")
        self.assertEqual(cfg.ai_api.model, "claude-sonnet-5")
        self.assertEqual(cfg.ai_api.max_turns, 40)
        self.assertFalse(cfg.ai_api.thinking)
        self.assertEqual(cfg.ai_api.api_key_env, "MY_TOKEN")

    def test_rejects_a_token_pasted_into_api_key_env(self):
        # api_key_env holds a variable NAME; a token there would silently
        # resolve to "no token found" at call time.
        with self.assertRaises(ValueError) as cm:
            self._write("ai_api:\n  api_key_env: sk-ant-api03-abc-def\n")
        self.assertIn("ai_api.api_key", str(cm.exception))
        # …and the message must not echo the whole secret back.
        self.assertNotIn("abc-def", str(cm.exception))

    def test_rejects_unknown_backend_and_keys(self):
        with self.assertRaises(ValueError):
            self._write("ai_backend: openai\n")
        with self.assertRaises(ValueError):
            self._write("ai_api:\n  modle: x\n")

    def test_round_trips_through_save(self):
        cfg = self._write("ai_backend: api\nai_api:\n  max_turns: 7\n")
        save_config(cfg, self.path)
        again = load_config(self.path)
        self.assertEqual(again.ai_backend, "api")
        self.assertEqual(again.ai_api.max_turns, 7)


if __name__ == "__main__":
    unittest.main()
