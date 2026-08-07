"""Unit tests for the deterministic build/test split.

Covers the cleanly unit-testable pieces: the new ``build_failed`` status and
verify-counter persistence, the ``ai_resolve`` config knobs, and the pure
helpers in ``build_verify`` (test detection, runner-hint selection, marker
parsing).

Stdlib unittest (no pytest dependency). Run:
    python3 -m unittest discover -s tests
"""
from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path

import releasy.build_verify as bv
from releasy.config import (
    AIResolveConfig,
    _default_test_file_globs,
    load_config,
    save_config,
)
from releasy.state import (
    STATUS_DISPLAY_ORDER,
    FeatureState,
    PipelineState,
    _parse_features,
    load_state,
    save_state,
)


class BuildFailedStatus(unittest.TestCase):
    """``build_failed`` is a known, displayable status."""

    def test_in_display_order(self):
        self.assertIn("build_failed", STATUS_DISPLAY_ORDER)

    def test_has_icon_and_heading(self):
        from releasy.status import STATUS_ICONS, STATUS_HEADINGS
        self.assertIn("build_failed", STATUS_ICONS)
        self.assertIn("build_failed", STATUS_HEADINGS)


class VerifyCounterPersistence(unittest.TestCase):
    """New FeatureState verify fields survive the state parse round-trip."""

    def test_defaults_when_absent(self):
        feats = _parse_features({"f1": {"status": "build_failed"}})
        self.assertEqual(feats["f1"].build_attempts, 0)
        self.assertEqual(feats["f1"].verify_resume_attempts, 0)
        self.assertIsNone(feats["f1"].last_verify_error)

    def test_explicit_values_parsed(self):
        feats = _parse_features({"f1": {
            "status": "build_failed",
            "build_attempts": 5,
            "verify_resume_attempts": 2,
            "last_verify_error": "build still failing",
        }})
        self.assertEqual(feats["f1"].build_attempts, 5)
        self.assertEqual(feats["f1"].verify_resume_attempts, 2)
        self.assertEqual(feats["f1"].last_verify_error, "build still failing")

    def test_null_counters_coerced_to_zero(self):
        feats = _parse_features({"f1": {
            "status": "build_failed",
            "build_attempts": None,
            "verify_resume_attempts": None,
        }})
        self.assertEqual(feats["f1"].build_attempts, 0)
        self.assertEqual(feats["f1"].verify_resume_attempts, 0)


class StateRoundTrip(unittest.TestCase):
    """save_state → load_state preserves build_failed + verify counters."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._prev = os.environ.get("RELEASY_STATE_DIR")
        os.environ["RELEASY_STATE_DIR"] = self._tmp.name

    def tearDown(self):
        if self._prev is None:
            os.environ.pop("RELEASY_STATE_DIR", None)
        else:
            os.environ["RELEASY_STATE_DIR"] = self._prev
        self._tmp.cleanup()

    def _config(self) -> "object":
        path = Path(self._tmp.name) / "config.yaml"
        path.write_text(
            "name: test-proj\nproject: testp\n"
            "origin:\n  remote: https://github.com/o/r.git\n",
            encoding="utf-8",
        )
        return load_config(path)

    def test_round_trip(self):
        cfg = self._config()
        st = PipelineState(base_branch="b")
        st.features["f1"] = FeatureState(
            status="build_failed", branch_name="feature/b/1",
            base_commit="deadbeef", build_attempts=5,
            verify_resume_attempts=1, last_verify_error="boom",
        )
        save_state(st, cfg)
        loaded = load_state(cfg)
        fs = loaded.features["f1"]
        self.assertEqual(fs.status, "build_failed")
        self.assertEqual(fs.build_attempts, 5)
        self.assertEqual(fs.verify_resume_attempts, 1)
        self.assertEqual(fs.last_verify_error, "boom")

    def test_zero_counters_not_written(self):
        # Lazy-write: zero/empty verify fields must not bloat the YAML.
        cfg = self._config()
        st = PipelineState(base_branch="b")
        st.features["f1"] = FeatureState(
            status="needs_review", branch_name="feature/b/1",
        )
        save_state(st, cfg)
        from releasy.config import state_file_path
        text = state_file_path(cfg.name).read_text(encoding="utf-8")
        self.assertNotIn("build_attempts", text)
        self.assertNotIn("verify_resume_attempts", text)
        self.assertNotIn("last_verify_error", text)


class AIResolveBuildKnobs(unittest.TestCase):
    """The deterministic-build config knobs: defaults, parse, round-trip."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._prev = os.environ.get("RELEASY_STATE_DIR")
        os.environ["RELEASY_STATE_DIR"] = self._tmp.name

    def tearDown(self):
        if self._prev is None:
            os.environ.pop("RELEASY_STATE_DIR", None)
        else:
            os.environ["RELEASY_STATE_DIR"] = self._prev
        self._tmp.cleanup()

    def _write(self, ai_block: str = "") -> Path:
        path = Path(self._tmp.name) / "config.yaml"
        path.write_text(
            "name: test-proj\nproject: testp\n"
            "origin:\n  remote: https://github.com/o/r.git\n" + ai_block,
            encoding="utf-8",
        )
        return path

    def test_defaults(self):
        c = AIResolveConfig()
        self.assertTrue(c.deterministic_build)
        self.assertEqual(c.max_build_attempts, 5)
        self.assertEqual(c.max_verify_resume_attempts, 2)
        self.assertEqual(c.max_resume_base_drift, 50)
        self.assertEqual(c.build_log_tail_lines, 500)
        self.assertTrue(c.run_pr_tests)
        self.assertEqual(c.test_file_globs, _default_test_file_globs())

    def test_parse_overrides(self):
        cfg = load_config(self._write(
            "ai_resolve:\n"
            "  deterministic_build: false\n"
            "  max_build_attempts: 8\n"
            "  max_verify_resume_attempts: 0\n"
            "  max_resume_base_drift: 120\n"
            "  run_pr_tests: false\n"
            "  test_file_globs:\n"
            "    - 'tests/foo/**'\n"
        ))
        ai = cfg.ai_resolve
        self.assertFalse(ai.deterministic_build)
        self.assertEqual(ai.max_build_attempts, 8)
        self.assertEqual(ai.max_verify_resume_attempts, 0)
        self.assertEqual(ai.max_resume_base_drift, 120)
        self.assertFalse(ai.run_pr_tests)
        self.assertEqual(ai.test_file_globs, ["tests/foo/**"])

    def test_save_config_round_trip(self):
        path = self._write()
        cfg = load_config(path)
        cfg.ai_resolve.deterministic_build = False
        cfg.ai_resolve.max_build_attempts = 7
        save_config(cfg, path)
        again = load_config(path)
        self.assertFalse(again.ai_resolve.deterministic_build)
        self.assertEqual(again.ai_resolve.max_build_attempts, 7)


class TestDetection(unittest.TestCase):
    """`_touched_test_files` / `_categorise` against the default globs."""

    def test_filters_to_test_paths(self):
        globs = _default_test_file_globs()
        changed = [
            "src/Storages/StorageX.cpp",
            "tests/queries/0_stateless/01_x.sql",
            "tests/queries/0_stateless/01_x.reference",
            "tests/integration/test_a/test.py",
            "src/Storages/tests/gtest_storage.cpp",
            "docs/whatever.md",
        ]
        touched = bv._touched_test_files(changed, globs)
        self.assertIn("tests/queries/0_stateless/01_x.sql", touched)
        self.assertIn("tests/integration/test_a/test.py", touched)
        self.assertIn("src/Storages/tests/gtest_storage.cpp", touched)
        self.assertNotIn("src/Storages/StorageX.cpp", touched)
        self.assertNotIn("docs/whatever.md", touched)

    def test_no_tests_touched(self):
        globs = _default_test_file_globs()
        self.assertEqual(bv._touched_test_files(["src/a.cpp"], globs), [])

    def test_categorise(self):
        cats = bv._categorise([
            "tests/queries/0_stateless/01_x.sql",
            "tests/integration/test_a/test.py",
        ])
        self.assertEqual(cats, {"stateless", "integration"})

    def test_runner_hints_nonempty_for_known(self):
        hints = bv._runner_hints(["tests/queries/0_stateless/01_x.sql"])
        self.assertIn("clickhouse-test", hints)

    def test_runner_hints_fallback_for_unknown(self):
        hints = bv._runner_hints(["src/Storages/tests/gtest_x.cpp"])
        self.assertIn("ci/jobs", hints)


class BuildLogExcerpt(unittest.TestCase):
    """The excerpt must stay under the OS arg limit (the crash this fixes)."""

    def _excerpt(self, lines: list[str]) -> str:
        d = tempfile.mkdtemp()
        try:
            p = Path(d) / bv._BUILD_LOG
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text("\n".join(lines), encoding="utf-8")
            return bv._build_log_excerpt(Path(d), 500)
        finally:
            import shutil
            shutil.rmtree(d)

    def test_huge_log_stays_under_arg_limit(self):
        # Thousands of very long lines must not produce a >128 KiB arg.
        lines = [
            f"/src/Foo.cpp:{i}: error: " + "X" * 9000 if i % 3 == 0
            else f"FAILED: link step {i}"
            for i in range(4000)
        ]
        exc = self._excerpt(lines)
        self.assertLess(len(exc.encode("utf-8")), 128 * 1024)
        self.assertLessEqual(len(exc.encode("utf-8")), bv._MAX_EXCERPT_BYTES + 4096)
        self.assertIn("FAILED:", exc)  # the failing-target line survives

    def test_realistic_log_surfaces_markers(self):
        lines = [
            f"/src/Storages/Foo.cpp:{i}:12: error: no member named bar"
            if i % 50 == 0 else f"[{i}/3000] Building CXX object x/{i}.o"
            for i in range(3000)
        ]
        exc = self._excerpt(lines)
        self.assertIn("error:", exc)
        self.assertLess(len(exc.encode("utf-8")), 128 * 1024)

    def test_missing_log(self):
        self.assertEqual(
            bv._build_log_excerpt(Path(tempfile.mkdtemp()), 500),
            "(build log unavailable)",
        )


class MarkerParsing(unittest.TestCase):
    """`_last_marker` finds the final verdict, tolerating backticks/order."""

    def test_fixed(self):
        self.assertEqual(
            bv._last_marker("did stuff\nFIXED", ("FIXED", "CANNOT FIX")),
            "FIXED",
        )

    def test_cannot_fix_with_reason(self):
        self.assertEqual(
            bv._last_marker(
                "tried\nCANNOT FIX: needs upstream PR", ("FIXED", "CANNOT FIX"),
            ),
            "CANNOT FIX: needs upstream PR",
        )

    def test_backticked_marker(self):
        self.assertEqual(
            bv._last_marker("ran\n`TESTS PASSED`", ("TESTS PASSED", "TESTS FAILED")),
            "TESTS PASSED",
        )

    def test_last_wins(self):
        # If a marker word appears mid-text and again at the end, take the end.
        self.assertEqual(
            bv._last_marker(
                "FIXED was my goal\n...\nCANNOT FIX: gave up",
                ("FIXED", "CANNOT FIX"),
            ),
            "CANNOT FIX: gave up",
        )

    def test_none_when_absent(self):
        self.assertIsNone(
            bv._last_marker("no verdict here", ("FIXED", "CANNOT FIX")),
        )


class BuildReachedCompiler(unittest.TestCase):
    """A red build with no compile error is an environment fault."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self._tmp.name)
        (self.repo / ".releasy").mkdir()

    def tearDown(self):
        self._tmp.cleanup()

    def _log(self, text: str) -> None:
        (self.repo / bv._BUILD_LOG).write_text(text, encoding="utf-8")

    def test_compiler_error(self):
        self._log("[1/2] Building X.cpp\nX.cpp:9:1: error: no member named 'y'\n")
        self.assertTrue(bv.build_reached_compiler(self.repo))

    def test_ninja_failed_line(self):
        self._log("FAILED: src/x.o \nlink step blew up\n")
        self.assertTrue(bv.build_reached_compiler(self.repo))

    def test_ninja_stopped_line(self):
        self._log("ninja: build stopped: subcommand failed.\n")
        self.assertTrue(bv.build_reached_compiler(self.repo))

    def test_missing_build_dir(self):
        self._log(
            "[releasy] build started at 2026-08-05T14:56:10Z\n"
            ".releasy/build.sh: line 12: cd: build: No such file or directory\n"
        )
        self.assertFalse(bv.build_reached_compiler(self.repo))

    def test_absent_log(self):
        self.assertFalse(bv.build_reached_compiler(self.repo))


class EnvironmentFaultShortCircuit(unittest.TestCase):
    """A build that never compiles must not spend a fix attempt."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self._tmp.name)
        (self.repo / ".releasy").mkdir()
        (self.repo / bv._BUILD_LOG).write_text(
            ".releasy/build.sh: line 12: cd: build: No such file or directory\n",
            encoding="utf-8",
        )
        self._orig_build = bv.run_build
        self._orig_script = bv._write_build_script
        self._orig_claude = bv._invoke_claude_with_retries
        self.claude_calls = 0

        def _no_claude(*a, **kw):
            self.claude_calls += 1
            return (0, "FIXED", False, 0.0)

        bv.run_build = lambda *a, **kw: (1, False)
        bv._write_build_script = lambda *a, **kw: None
        bv._invoke_claude_with_retries = _no_claude

    def tearDown(self):
        bv.run_build = self._orig_build
        bv._write_build_script = self._orig_script
        bv._invoke_claude_with_retries = self._orig_claude
        self._tmp.cleanup()

    def _config(self):
        path = self.repo / "config.yaml"
        path.write_text(
            "name: test-proj\nproject: testp\n"
            "origin:\n  remote: https://github.com/o/r.git\n",
            encoding="utf-8",
        )
        return load_config(path)

    def _pr(self):
        from releasy.github_ops import PRInfo
        return PRInfo(
            number=1, title="t", body="", state="merged",
            merge_commit_sha=None, head_sha="abc", url="https://x/1",
            repo_slug="o/r",
        )

    def test_returns_error_outcome_without_calling_claude(self):
        res = bv.verify_build_and_tests(
            self._config(), self.repo, self._pr(),
            port_branch="feature/b/1", base_branch="b", base_sha="deadbeef",
        )
        self.assertFalse(res.success)
        self.assertEqual(res.outcome, "error")
        self.assertEqual(res.build_attempts, 0)
        self.assertEqual(self.claude_calls, 0)
        self.assertIn("never reached the compiler", res.error)


class ResumeDryRun(unittest.TestCase):
    """``--dry-run`` must not check out a parked branch or start a build."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self._tmp.name)
        self._prev = os.environ.get("RELEASY_STATE_DIR")
        os.environ["RELEASY_STATE_DIR"] = self._tmp.name
        self._git("init", "-b", "main")
        self._git("config", "user.email", "t@t")
        self._git("config", "user.name", "t")
        (self.repo / "f.txt").write_text("x", encoding="utf-8")
        self._git("add", "-A")
        self._git("commit", "-m", "c0")
        self._git("branch", "feature/b/1")
        # Dirty worktree: stash_and_clean would wipe this.
        (self.repo / "dirty.txt").write_text("keep me", encoding="utf-8")

    def tearDown(self):
        if self._prev is None:
            os.environ.pop("RELEASY_STATE_DIR", None)
        else:
            os.environ["RELEASY_STATE_DIR"] = self._prev
        self._tmp.cleanup()

    def _git(self, *args: str) -> str:
        return subprocess.run(
            ["git", "-C", str(self.repo), *args],
            capture_output=True, text=True, check=True,
        ).stdout.strip()

    def _config(self, **ai):
        path = self.repo / "config.yaml"
        block = "".join(f"  {k}: {v}\n" for k, v in ai.items())
        path.write_text(
            "name: test-proj\nproject: testp\n"
            "origin:\n  remote: https://github.com/o/r.git\n"
            + ("ai_resolve:\n" + block if block else ""),
            encoding="utf-8",
        )
        return load_config(path)

    def _resume(self, cfg):
        """Call the resume path; return (outcome, verify_phase_call_count)."""
        import releasy.pipeline as pl
        from releasy.github_ops import PRInfo

        unit = pl.FeatureUnit(
            feature_id="f1",
            prs=[PRInfo(
                number=1, title="t", body="", state="merged",
                merge_commit_sha=None, head_sha="abc", url="https://x/1",
                repo_slug="o/r",
            )],
            if_exists="recreate",
        )
        prev = FeatureState(
            status="build_failed", branch_name="feature/b/1",
            base_commit=self._git("rev-parse", "feature/b/1"),
        )
        built = []
        orig = pl._run_verify_phase
        pl._run_verify_phase = lambda *a, **kw: built.append(1)
        try:
            out = pl._resume_build_failed_unit(
                cfg, self.repo, PipelineState(base_branch="b"), unit, prev,
                "feature/b/1", "b", "main", "lbl",
            )
        finally:
            pl._run_verify_phase = orig
        return out, len(built)

    def _advance_main(self, n: int) -> None:
        for i in range(n):
            (self.repo / f"m{i}.txt").write_text("x", encoding="utf-8")
            self._git("add", "-A")
            self._git("commit", "-m", f"m{i}")

    def test_dry_run_no_checkout_no_build(self):
        cfg = self._config()
        cfg.dry_run = True
        out, built = self._resume(cfg)
        self.assertEqual(out, "continue")
        self.assertEqual(built, 0)
        self.assertEqual(self._git("rev-parse", "--abbrev-ref", "HEAD"), "main")
        self.assertTrue((self.repo / "dirty.txt").exists())

    def test_near_base_resumes(self):
        self._advance_main(2)
        cfg = self._config(max_resume_base_drift=5)
        cfg.dry_run = True
        out, _ = self._resume(cfg)
        self.assertEqual(out, "continue")  # resumed, not re-ported

    def test_drifted_branch_reports_from_base(self):
        self._advance_main(6)
        cfg = self._config(max_resume_base_drift=5)
        cfg.dry_run = True
        out, built = self._resume(cfg)
        self.assertIsNone(out)  # falls through to a fresh port
        self.assertEqual(built, 0)

    def test_drift_check_disabled_by_zero(self):
        self._advance_main(99)
        cfg = self._config(max_resume_base_drift=0)
        cfg.dry_run = True
        out, _ = self._resume(cfg)
        self.assertEqual(out, "continue")


if __name__ == "__main__":
    unittest.main()
