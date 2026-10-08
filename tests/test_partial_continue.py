from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

import releasy.pipeline as p
from releasy.config import load_config, save_config
from releasy.state import FeatureState, _parse_features


class IsPartialGroup(unittest.TestCase):
    def test_conflict_no_partial_count_not_partial(self):
        self.assertFalse(
            p._is_partial_group(
                FeatureState(status="conflict", partial_pr_count=None)
            )
        )

    def test_partial_count_set_but_status_resolved_not_partial(self):
        self.assertFalse(
            p._is_partial_group(
                FeatureState(status="needs_review", partial_pr_count=2)
            )
        )


class AttemptCounterPersistence(unittest.TestCase):
    def test_explicit_value_parsed(self):
        feats = _parse_features(
            {"f1": {"status": "conflict", "partial_continue_attempts": 3}}
        )
        self.assertEqual(feats["f1"].partial_continue_attempts, 3)

    def test_null_coerced_to_zero(self):
        feats = _parse_features(
            {"f1": {"status": "conflict", "partial_continue_attempts": None}}
        )
        self.assertEqual(feats["f1"].partial_continue_attempts, 0)


class PartialContinueAllowed(unittest.TestCase):
    def _config(self, cap: int = 2):
        from releasy.config import Config, OriginConfig

        cfg = Config(
            name="proj",
            origin=OriginConfig(remote="git@github.com:acme/repo.git"),
            project="acme",
        )
        cfg.pr_policy.max_partial_continue_attempts = cap
        return cfg

    def _partial(self) -> FeatureState:
        return FeatureState(
            status="conflict", partial_pr_count=2, failed_step_index=2,
            rebase_pr_url="https://github.com/acme/repo/pull/9",
        )

    def test_recreate_still_resumes_a_partial_group(self):
        self.assertTrue(p._partial_continue_allowed(
            self._config(), self._partial(), "recreate", True,
        ))

    def test_append_uses_its_own_resume_path(self):
        self.assertFalse(p._partial_continue_allowed(
            self._config(), self._partial(), "append", True,
        ))

    def test_first_pick_conflict_has_nothing_to_resume(self):
        nothing = FeatureState(status="conflict", partial_pr_count=0)
        self.assertFalse(p._partial_continue_allowed(
            self._config(), nothing, "recreate", True,
        ))

    def test_cap_zero_opts_out(self):
        self.assertFalse(p._partial_continue_allowed(
            self._config(cap=0), self._partial(), "recreate", True,
        ))

    def test_no_retry_failed_opts_out(self):
        self.assertFalse(p._partial_continue_allowed(
            self._config(), self._partial(), "recreate", False,
        ))

    def test_untracked_unit_is_a_fresh_port(self):
        self.assertFalse(p._partial_continue_allowed(
            self._config(), None, "recreate", True,
        ))


class ApiAbortedNotAResolverVerdict(unittest.TestCase):
    def setUp(self):
        import releasy.ai_resolve as ar

        self.ar = ar
        self._real_invoke = ar._invoke_claude_with_retries
        self._real_prompt = ar._render_prompt
        ar._render_prompt = lambda cfg, repo, ctx: "prompt"  # type: ignore[assignment]
        self.reply = (1, "", False, None)
        ar._invoke_claude_with_retries = (  # type: ignore[assignment]
            lambda cfg, repo, prompt, **kw: self.reply
        )

    def tearDown(self):
        self.ar._invoke_claude_with_retries = self._real_invoke  # type: ignore[assignment]
        self.ar._render_prompt = self._real_prompt  # type: ignore[assignment]

    def _resolve(self):
        from releasy.config import Config, OriginConfig

        cfg = Config(
            name="proj",
            origin=OriginConfig(remote="git@github.com:acme/repo.git"),
            project="acme",
        )
        cfg.ai_resolve.command = "true"  # a real binary, so the CLI gate passes
        ctx = self.ar.AIResolveContext(
            port_branch="b", base_branch="base", source_pr=None,
            conflict_files=["f.cpp"], operation="cherry-pick", skip_build=True,
        )
        return self.ar.resolve_with_claude(cfg, Path("."), ctx)

    def test_transient_api_error_is_an_abort(self):
        self.reply = (1, "API Error: Overloaded\n", False, None)
        res = self._resolve()
        self.assertFalse(res.success)
        self.assertTrue(res.api_aborted)

    def test_exit_with_no_billed_work_is_an_abort(self):
        self.reply = (1, "[runner] something died\n", False, None)
        res = self._resolve()
        self.assertTrue(res.api_aborted)

    def test_unresolved_verdict_is_not_an_abort(self):
        self.reply = (0, "…work…\nUNRESOLVED\n", False, 1.25)
        res = self._resolve()
        self.assertFalse(res.success)
        self.assertFalse(res.api_aborted)
        self.assertEqual(res.error, "claude reported UNRESOLVED")

    def test_timeout_is_not_an_abort(self):
        self.reply = (1, "", True, None)
        res = self._resolve()
        self.assertTrue(res.timed_out)
        self.assertFalse(res.api_aborted)

    def test_missing_backend_is_an_abort(self):
        from releasy.config import Config, OriginConfig

        cfg = Config(
            name="proj",
            origin=OriginConfig(remote="git@github.com:acme/repo.git"),
            project="acme",
        )
        cfg.ai_resolve.command = "definitely-not-on-path-xyz"
        ctx = self.ar.AIResolveContext(
            port_branch="b", base_branch="base", source_pr=None,
            conflict_files=["f.cpp"], operation="cherry-pick", skip_build=True,
        )
        res = self.ar.resolve_with_claude(cfg, Path("."), ctx)
        self.assertTrue(res.api_aborted)


class ClosedPRClearsPartialMarkers(unittest.TestCase):
    def setUp(self):
        import releasy.pipeline as pipeline

        self._real = pipeline.fetch_pr_by_url
        self.pr_state = "closed"
        pipeline.fetch_pr_by_url = (  # type: ignore[assignment]
            lambda cfg, url, include_closed=False: type(
                "I", (), {"state": self.pr_state},
            )()
        )

    def tearDown(self):
        import releasy.pipeline as pipeline

        pipeline.fetch_pr_by_url = self._real  # type: ignore[assignment]

    def _state_with_partial(self):
        from releasy.state import PipelineState

        st = PipelineState()
        st.features["grp"] = FeatureState(
            status="conflict", partial_pr_count=2, failed_step_index=2,
            rebase_pr_url="https://github.com/acme/repo/pull/9",
        )
        return st

    def test_closed_pr_becomes_terminal_and_loses_partial_markers(self):
        from releasy.config import Config, OriginConfig

        cfg = Config(
            name="proj",
            origin=OriginConfig(remote="git@github.com:acme/repo.git"),
            project="acme",
        )
        st = self._state_with_partial()
        changed = p._refresh_all_merge_status_from_github(cfg, st)
        fs = st.features["grp"]
        self.assertEqual(changed, 1)
        self.assertEqual(fs.status, "closed")
        self.assertIsNone(fs.partial_pr_count)
        self.assertFalse(p._partial_continue_allowed(cfg, fs, "recreate", True))

    def test_still_open_pr_keeps_its_partial_markers(self):
        from releasy.config import Config, OriginConfig

        cfg = Config(
            name="proj",
            origin=OriginConfig(remote="git@github.com:acme/repo.git"),
            project="acme",
        )
        self.pr_state = "open"
        st = self._state_with_partial()
        self.assertEqual(p._refresh_all_merge_status_from_github(cfg, st), 0)
        fs = st.features["grp"]
        self.assertEqual(fs.partial_pr_count, 2)
        self.assertTrue(p._partial_continue_allowed(cfg, fs, "recreate", True))


class MaxPartialContinueAttemptsConfig(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._prev_state_dir = os.environ.get("RELEASY_STATE_DIR")
        os.environ["RELEASY_STATE_DIR"] = self._tmp.name

    def tearDown(self):
        if self._prev_state_dir is None:
            os.environ.pop("RELEASY_STATE_DIR", None)
        else:
            os.environ["RELEASY_STATE_DIR"] = self._prev_state_dir
        self._tmp.cleanup()

    def _write_config(self, pr_policy_block: str = "") -> Path:
        path = Path(self._tmp.name) / "config.yaml"
        path.write_text(
            "name: test-proj\n"
            "project: testp\n"
            "origin:\n"
            "  remote: https://github.com/o/r.git\n"
            + pr_policy_block,
            encoding="utf-8",
        )
        return path

    def test_default_is_two(self):
        cfg = load_config(self._write_config())
        self.assertEqual(cfg.pr_policy.max_partial_continue_attempts, 2)

    def test_round_trip(self):
        cfg = load_config(
            self._write_config(
                "pr_policy:\n  max_partial_continue_attempts: 5\n"
            )
        )
        out = Path(self._tmp.name) / "out.yaml"
        save_config(cfg, out)
        self.assertEqual(
            load_config(out).pr_policy.max_partial_continue_attempts, 5
        )


if __name__ == "__main__":
    unittest.main()
