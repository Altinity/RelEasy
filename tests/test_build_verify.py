from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import releasy.build_verify as bv
import releasy.pipeline as pl
from releasy.ai_resolve import _BUILD_SCRIPT, _write_build_script
from releasy.config import (
    _default_test_file_globs,
    load_config,
    save_config,
    state_file_path,
)
from releasy.github_ops import PRInfo
from releasy.state import (
    FeatureState,
    PipelineState,
    _parse_features,
    load_state,
    save_state,
)


def _pr() -> PRInfo:
    return PRInfo(
        number=1, title="t", body="", state="merged",
        merge_commit_sha=None, head_sha="abc", url="https://x/1",
        repo_slug="o/r",
    )


def _unit() -> pl.FeatureUnit:
    return pl.FeatureUnit(feature_id="f1", prs=[_pr()], if_exists="recreate")


def _write_config(path: Path, extra: str = ""):
    path.write_text(
        "name: test-proj\nproject: testp\n"
        "origin:\n  remote: https://github.com/o/r.git\n" + extra,
        encoding="utf-8",
    )
    return load_config(path)


class _TmpStateDir(unittest.TestCase):
    """A temp dir that is also ``RELEASY_STATE_DIR``."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.repo = Path(tmp.name)
        patcher = mock.patch.dict(os.environ, {"RELEASY_STATE_DIR": tmp.name})
        patcher.start()
        self.addCleanup(patcher.stop)

    def _config(self, extra: str = ""):
        return _write_config(self.repo / "config.yaml", extra)


class VerifyCounterPersistence(unittest.TestCase):
    def test_null_counters_coerced_to_zero(self):
        feats = _parse_features({"f1": {
            "status": "build_failed",
            "build_attempts": None,
            "verify_resume_attempts": None,
        }})
        self.assertEqual(feats["f1"].build_attempts, 0)
        self.assertEqual(feats["f1"].verify_resume_attempts, 0)


class StateRoundTrip(_TmpStateDir):
    def test_round_trip(self):
        cfg = self._config()
        st = PipelineState(base_branch="b")
        st.features["f1"] = FeatureState(
            status="build_failed", branch_name="feature/b/1",
            base_commit="deadbeef", build_attempts=5,
            verify_resume_attempts=1, last_verify_error="boom",
            branch_url="https://github.com/o/r/tree/feature/b/1",
        )
        save_state(st, cfg)
        loaded = load_state(cfg)
        fs = loaded.features["f1"]
        self.assertEqual(fs.status, "build_failed")
        self.assertEqual(fs.build_attempts, 5)
        self.assertEqual(fs.verify_resume_attempts, 1)
        self.assertEqual(fs.last_verify_error, "boom")
        self.assertEqual(
            fs.branch_url, "https://github.com/o/r/tree/feature/b/1",
        )

    def test_zero_counters_not_written(self):
        cfg = self._config()
        st = PipelineState(base_branch="b")
        st.features["f1"] = FeatureState(
            status="needs_review", branch_name="feature/b/1",
        )
        save_state(st, cfg)
        text = state_file_path(cfg.name).read_text(encoding="utf-8")
        self.assertNotIn("build_attempts", text)
        self.assertNotIn("verify_resume_attempts", text)
        self.assertNotIn("last_verify_error", text)
        self.assertNotIn("branch_url", text)


class AIResolveBuildKnobs(_TmpStateDir):
    def test_parse_and_round_trip(self):
        cfg = self._config(
            "ai_resolve:\n"
            "  deterministic_build: false\n"
            "  max_build_attempts: 8\n"
            "  max_verify_resume_attempts: 0\n"
            "  max_resume_base_drift: 120\n"
            "  run_pr_tests: false\n"
            "  test_file_globs:\n"
            "    - 'tests/foo/**'\n"
        )
        path = self.repo / "out.yaml"
        save_config(cfg, path)
        for loaded in (cfg, load_config(path)):
            ai = loaded.ai_resolve
            self.assertFalse(ai.deterministic_build)
            self.assertEqual(ai.max_build_attempts, 8)
            self.assertEqual(ai.max_verify_resume_attempts, 0)
            self.assertEqual(ai.max_resume_base_drift, 120)
            self.assertFalse(ai.run_pr_tests)
            self.assertEqual(ai.test_file_globs, ["tests/foo/**"])


class TestDetection(unittest.TestCase):
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
    def _excerpt(self, lines: list[str]) -> str:
        rel = bv.build_log_path("feature/b/1")
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text("\n".join(lines), encoding="utf-8")
            return bv._build_log_excerpt(Path(d), rel, 500)

    def test_huge_log_stays_under_arg_limit(self):
        lines = [
            f"/src/Foo.cpp:{i}: error: " + "X" * 9000 if i % 3 == 0
            else f"FAILED: link step {i}"
            for i in range(4000)
        ]
        exc = self._excerpt(lines)
        self.assertLess(len(exc.encode("utf-8")), 128 * 1024)
        self.assertLessEqual(len(exc.encode("utf-8")), bv._MAX_EXCERPT_BYTES + 4096)
        self.assertIn("FAILED:", exc)

    def test_realistic_log_surfaces_markers(self):
        lines = [
            f"/src/Storages/Foo.cpp:{i}:12: error: no member named bar"
            if i % 50 == 0 else f"[{i}/3000] Building CXX object x/{i}.o"
            for i in range(3000)
        ]
        self.assertIn("error:", self._excerpt(lines))

    def test_missing_log(self):
        self.assertEqual(
            bv._build_log_excerpt(
                Path(tempfile.mkdtemp()), bv.build_log_path("feature/b/1"), 500,
            ),
            "(build log unavailable)",
        )


class MarkerParsing(unittest.TestCase):
    def test_fixed(self):
        self.assertEqual(
            bv._last_marker("did stuff\nFIXED", ("FIXED", "CANNOT FIX")),
            "FIXED",
        )

    def test_backticked_marker(self):
        self.assertEqual(
            bv._last_marker("ran\n`TESTS PASSED`", ("TESTS PASSED", "TESTS FAILED")),
            "TESTS PASSED",
        )

    def test_last_wins(self):
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
    LOG = bv.build_log_path("feature/b/1")

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.repo = Path(tmp.name)
        (self.repo / ".releasy").mkdir()

    def _log(self, text: str) -> None:
        (self.repo / self.LOG).write_text(text, encoding="utf-8")

    def test_compiler_or_ninja_failure_counts(self):
        for text in (
            "[1/2] Building X.cpp\nX.cpp:9:1: error: no member named 'y'\n",
            "FAILED: src/x.o \nlink step blew up\n",
            "ninja: build stopped: subcommand failed.\n",
        ):
            with self.subTest(text=text):
                self._log(text)
                self.assertTrue(bv.build_reached_compiler(self.repo, self.LOG))

    def test_missing_build_dir(self):
        self._log(
            "[releasy] build started at 2026-08-05T14:56:10Z\n"
            ".releasy/build.sh: line 12: cd: build: No such file or directory\n"
        )
        self.assertFalse(bv.build_reached_compiler(self.repo, self.LOG))

    def test_cmake_compiler_check_failure(self):
        self._log(
            "-- Check for working C compiler: /usr/bin/clang-cl-21 - broken\n"
            "CMake Error at /usr/share/cmake-3.28/Modules/CMakeTestCCompiler.cmake:67 (message):\n"
            "    [2/2] : && /usr/bin/clang-cl-21   x.o -o cmTC_c6c9a   && :\n"
            "    FAILED: cmTC_c6c9a \n"
            "    /usr/bin/ld: Error: unable to disambiguate: -nologo\n"
            "    clang-cl-21: error: linker command failed with exit code 1\n"
            "    ninja: build stopped: subcommand failed.\n"
            "-- Configuring incomplete, errors occurred!\n"
        )
        self.assertFalse(bv.build_reached_compiler(self.repo, self.LOG))

    def test_absent_log(self):
        self.assertFalse(bv.build_reached_compiler(self.repo, self.LOG))


class EnvironmentFaultShortCircuit(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.repo = Path(tmp.name)
        (self.repo / ".releasy").mkdir()
        (self.repo / bv.build_log_path("feature/b/1")).write_text(
            ".releasy/build.sh: line 12: cd: build: No such file or directory\n",
            encoding="utf-8",
        )

    def test_returns_error_outcome_without_calling_claude(self):
        claude = mock.Mock(return_value=(0, "FIXED", False, 0.0))
        with mock.patch.multiple(
            bv,
            run_build=lambda *a, **kw: (1, False),
            _write_build_script=lambda *a, **kw: None,
            _invoke_claude_with_retries=claude,
        ):
            res = bv.verify_build_and_tests(
                _write_config(self.repo / "config.yaml"), self.repo, _pr(),
                port_branch="feature/b/1", base_branch="b", base_sha="deadbeef",
            )
        self.assertFalse(res.success)
        self.assertEqual(res.outcome, "error")
        self.assertEqual(res.build_attempts, 0)
        claude.assert_not_called()
        self.assertIn("never reached the compiler", res.error)


class ReconfigureWipesCMakeCache(unittest.TestCase):
    """A poisoned CMake cache (e.g. clang-cl picked) is wiped and rebuilt, not parked."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.repo = Path(tmp.name)
        (self.repo / ".releasy").mkdir()
        self.cache = self.repo / "build" / "CMakeCache.txt"
        (self.repo / "build" / "CMakeFiles").mkdir(parents=True)
        self.cache.write_text("CMAKE_C_COMPILER:STRING=/usr/bin/clang-cl-21\n")
        self.builds = 0

    def _verify(self, log: str, claude: mock.Mock):
        def _build(*a, **kw):
            self.builds += 1
            if self.cache.exists():
                (self.repo / bv.build_log_path("feature/b/1")).write_text(log)
                return (1, False)
            return (0, False)

        with mock.patch.multiple(
            bv,
            run_build=_build,
            _write_build_script=lambda *a, **kw: None,
            _render=lambda *a, **kw: "prompt",
            _common_placeholders=lambda *a, **kw: {},
            _invoke_claude_with_retries=claude,
        ):
            return bv.verify_build_and_tests(
                _write_config(
                    self.repo / "config.yaml",
                    "ai_resolve:\n  run_pr_tests: false\n",
                ),
                self.repo, _pr(),
                port_branch="feature/b/1", base_branch="b", base_sha="deadbeef",
            )

    def test_reconfigure_marker(self):
        claude = mock.Mock(return_value=(
            0, "RECONFIGURE: clang-cl-21 picked instead of clang-22", False, 0.0,
        ))
        res = self._verify(
            "-- Check for working C compiler: /usr/bin/clang-cl-21 - broken\n"
            "-- Configuring incomplete, errors occurred!\n"
            "ninja: error: rebuilding 'build.ninja': subcommand failed\n",
            claude,
        )
        self.assertTrue(res.success)
        self.assertEqual(self.builds, 2)
        claude.assert_called_once()
        self.assertFalse((self.repo / "build" / "CMakeFiles").exists())

    def test_never_reached_compiler(self):
        claude = mock.Mock(return_value=(0, "FIXED", False, 0.0))
        res = self._verify("-- Configuring incomplete, errors occurred!\n", claude)
        self.assertTrue(res.success)
        self.assertEqual(self.builds, 2)
        claude.assert_not_called()


class CMakeListsErrorGoesToFixer(unittest.TestCase):
    """A CMake Error that survives a fresh configure is the port's to fix."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.repo = Path(tmp.name)
        (self.repo / ".releasy").mkdir()

    def test_fixer_called_after_wipe(self):
        fixed = []
        builds = []

        def _build(*a, **kw):
            builds.append(1)
            if fixed:
                return (0, False)
            (self.repo / bv.build_log_path("feature/b/1")).write_text(
                "CMake Error at src/CMakeLists.txt:12 (add_library):\n"
                "  Cannot find source file:\n    Storages/Gone.cpp\n"
                "-- Configuring incomplete, errors occurred!\n"
            )
            return (1, False)

        def _claude(*a, **kw):
            fixed.append(1)
            return (0, "FIXED", False, 0.0)

        with mock.patch.multiple(
            bv,
            run_build=_build,
            _write_build_script=lambda *a, **kw: None,
            _render=lambda *a, **kw: "prompt",
            _common_placeholders=lambda *a, **kw: {},
            _invoke_claude_with_retries=_claude,
        ):
            res = bv.verify_build_and_tests(
                _write_config(
                    self.repo / "config.yaml",
                    "ai_resolve:\n  run_pr_tests: false\n",
                ),
                self.repo, _pr(),
                port_branch="feature/b/1", base_branch="b", base_sha="deadbeef",
            )
        self.assertTrue(res.success)
        self.assertEqual(len(builds), 3)
        self.assertEqual(len(fixed), 1)


class RunTestsAllowlistPaths(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.repo = Path(tmp.name)
        self.calls: list[dict] = []
        self.verdict = "TESTS PASSED"

        def _claude(*a, **kw):
            self.calls.append(kw)
            return (0, self.verdict, False, 0.0)

        patcher = mock.patch.multiple(
            bv,
            run_build=lambda *a, **kw: (0, False),
            _write_build_script=lambda *a, **kw: None,
            _changed_files=lambda *a, **kw: ["tests/integration/test_x/test.py"],
            _render=lambda *a, **kw: "prompt",
            _common_placeholders=lambda *a, **kw: {},
            _invoke_claude_with_retries=_claude,
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def _verify(self):
        return bv.verify_build_and_tests(
            _write_config(self.repo / "config.yaml"), self.repo, _pr(),
            port_branch="feature/b/1", base_branch="b", base_sha="deadbeef",
        )

    def test_runner_and_binaries_allowed_with_work_dir_resolved(self):
        res = self._verify()
        self.assertTrue(res.success)
        self.assertEqual(len(self.calls), 1)
        tools = self.calls[0]["allowed_tools"]
        for t in (
            f"Bash({self.repo}/tests/integration/runner:*)",
            f"Bash({self.repo}/build/src/unit_tests_dbms:*)",
            "Bash(build/src/unit_tests_dbms:*)",
            "Bash(./build/src/unit_tests_dbms:*)",
            "Bash(./build/programs/clickhouse:*)",
        ):
            self.assertIn(t, tools)
        self.assertFalse([t for t in tools if "{work_dir}" in t])

    def test_could_not_run_tests_is_environment_fault(self):
        self.verdict = "TESTS FAILED: could not run tests: runner denied"
        res = self._verify()
        self.assertFalse(res.success)
        self.assertEqual(res.outcome, "error")

    def test_no_verdict_is_environment_fault(self):
        self.verdict = "ran out of turns"
        res = self._verify()
        self.assertFalse(res.success)
        self.assertEqual(res.outcome, "error")
        self.assertIn("no verdict", res.error)

    def test_run_tests_output_logged(self):
        self.verdict = "transcript\nTESTS FAILED: 1 of 2 tests failed"
        self._verify()
        log = self.repo / ".releasy" / "build-feature-b-1.tests.log"
        self.assertEqual(log.read_text(encoding="utf-8"), self.verdict)

    def test_failing_tests_stay_tests_failed(self):
        self.verdict = "TESTS FAILED: 2 of 5 tests failed"
        res = self._verify()
        self.assertFalse(res.success)
        self.assertEqual(res.outcome, "tests_failed")


class ResumeDryRun(_TmpStateDir):
    def setUp(self):
        super().setUp()
        self._git("init", "-b", "main")
        self._git("config", "user.email", "t@t")
        self._git("config", "user.name", "t")
        (self.repo / "f.txt").write_text("x", encoding="utf-8")
        self._git("add", "-A")
        self._git("commit", "-m", "c0")
        self._git("branch", "feature/b/1")
        (self.repo / "dirty.txt").write_text("keep me", encoding="utf-8")

    def _git(self, *args: str) -> str:
        return subprocess.run(
            ["git", "-C", str(self.repo), *args],
            capture_output=True, text=True, check=True,
        ).stdout.strip()

    def _resume(self, drift=None):
        cfg = self._config(
            "" if drift is None
            else f"ai_resolve:\n  max_resume_base_drift: {drift}\n"
        )
        cfg.dry_run = True
        prev = FeatureState(
            status="build_failed", branch_name="feature/b/1",
            base_commit=self._git("rev-parse", "feature/b/1"),
        )
        built = []
        with mock.patch.object(
            pl, "_run_verify_phase", lambda *a, **kw: built.append(1),
        ):
            out = pl._resume_build_failed_unit(
                cfg, self.repo, PipelineState(base_branch="b"), _unit(), prev,
                "feature/b/1", "b", "main", "lbl",
            )
        return out, len(built)

    def _advance_main(self, n: int) -> None:
        for i in range(n):
            (self.repo / f"m{i}.txt").write_text("x", encoding="utf-8")
            self._git("add", "-A")
            self._git("commit", "-m", f"m{i}")

    def test_dry_run_no_checkout_no_build(self):
        out, built = self._resume()
        self.assertEqual(out, "continue")
        self.assertEqual(built, 0)
        self.assertEqual(self._git("rev-parse", "--abbrev-ref", "HEAD"), "main")
        self.assertTrue((self.repo / "dirty.txt").exists())

    def test_near_base_resumes(self):
        self._advance_main(2)
        out, _ = self._resume(drift=5)
        self.assertEqual(out, "continue")

    def test_drifted_branch_reports_from_base(self):
        self._advance_main(6)
        out, built = self._resume(drift=5)
        self.assertIsNone(out)
        self.assertEqual(built, 0)

    def test_drift_check_disabled_by_zero(self):
        self._advance_main(99)
        out, _ = self._resume(drift=0)
        self.assertEqual(out, "continue")


class PerBranchBuildLog(unittest.TestCase):
    def test_path_is_flat_and_sanitised(self):
        self.assertEqual(
            bv.build_log_path("feature/antalya-26.8/auto-grp-pr-2141"),
            ".releasy/build-feature-antalya-26.8-auto-grp-pr-2141.log",
        )

    def test_empty_branch_falls_back(self):
        self.assertEqual(bv.build_log_path(""), ".releasy/build.log")

    def test_build_script_tees_to_the_given_log(self):
        d = Path(tempfile.mkdtemp())
        rel = bv.build_log_path("feature/b/1")
        _write_build_script(d, "ninja", rel)
        self.assertIn(f"tee {rel}", (d / _BUILD_SCRIPT).read_text())


class ParkBuildFailedPush(_TmpStateDir):
    def _park(self, *, push: bool, push_fn=None):
        cfg = self._config(f"push: {str(push).lower()}\n")
        state = PipelineState(base_branch="b")
        pushed: list[str] = []
        with mock.patch.object(
            pl, "_push", push_fn or (lambda c, r, branch: pushed.append(branch)),
        ):
            pl._park_build_failed(
                cfg, self.repo, state, _unit(), "feature/b/1", "deadbeef",
                bv.VerifyResult(
                    success=False, outcome="build_failed", build_attempts=5,
                    error="build still failing after 5 fix attempt(s)",
                ),
                resume_attempts=0,
            )
        return pushed, state.features["f1"]

    def test_pushes_and_records_branch_url(self):
        pushed, fs = self._park(push=True)
        self.assertEqual(pushed, ["feature/b/1"])
        self.assertEqual(fs.status, "build_failed")
        self.assertEqual(
            fs.branch_url, "https://github.com/o/r/tree/feature/b/1",
        )

    def test_push_disabled_leaves_branch_local(self):
        pushed, fs = self._park(push=False)
        self.assertEqual(pushed, [])
        self.assertIsNone(fs.branch_url)

    def test_failed_push_still_parks(self):
        def _boom(*a, **kw):
            raise subprocess.CalledProcessError(1, ["git", "push"])

        _, fs = self._park(push=True, push_fn=_boom)
        self.assertEqual(fs.status, "build_failed")
        self.assertIsNone(fs.branch_url)


if __name__ == "__main__":
    unittest.main()
