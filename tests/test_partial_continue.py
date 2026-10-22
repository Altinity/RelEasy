from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import releasy.ai_resolve as ar
import releasy.pipeline as p
from releasy.config import Config, OriginConfig, load_config, save_config
from releasy.state import FeatureState, PipelineState, _parse_features


def _config(cap: int = 2) -> Config:
    cfg = Config(
        name="proj",
        origin=OriginConfig(remote="git@github.com:acme/repo.git"),
        project="acme",
    )
    cfg.pr_policy.max_partial_continue_attempts = cap
    return cfg


def _partial() -> FeatureState:
    return FeatureState(
        status="conflict", partial_pr_count=2, failed_step_index=2,
        rebase_pr_url="https://github.com/acme/repo/pull/9",
    )


class AttemptCounterPersistence(unittest.TestCase):
    def test_null_coerced_to_zero(self):
        feats = _parse_features(
            {"f1": {"status": "conflict", "partial_continue_attempts": None}}
        )
        self.assertEqual(feats["f1"].partial_continue_attempts, 0)


class PartialContinueAllowed(unittest.TestCase):
    def test_recreate_still_resumes_a_partial_group(self):
        self.assertTrue(p._partial_continue_allowed(
            _config(), _partial(), "recreate", True,
        ))

    def test_append_uses_its_own_resume_path(self):
        self.assertFalse(p._partial_continue_allowed(
            _config(), _partial(), "append", True,
        ))

    def test_first_pick_conflict_has_nothing_to_resume(self):
        nothing = FeatureState(status="conflict", partial_pr_count=0)
        self.assertFalse(p._partial_continue_allowed(
            _config(), nothing, "recreate", True,
        ))

    def test_cap_zero_opts_out(self):
        self.assertFalse(p._partial_continue_allowed(
            _config(cap=0), _partial(), "recreate", True,
        ))

    def test_no_retry_failed_opts_out(self):
        self.assertFalse(p._partial_continue_allowed(
            _config(), _partial(), "recreate", False,
        ))

    def test_untracked_unit_is_a_fresh_port(self):
        self.assertFalse(p._partial_continue_allowed(
            _config(), None, "recreate", True,
        ))


class ApiAbortedNotAResolverVerdict(unittest.TestCase):
    def setUp(self):
        self.reply = (1, "", False, None)
        for patcher in (
            mock.patch.object(ar, "_render_prompt", lambda cfg, repo, ctx: "prompt"),
            mock.patch.object(
                ar, "_invoke_claude_with_retries",
                lambda cfg, repo, prompt, **kw: self.reply,
            ),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def _resolve(self, command="true"):  # a real binary, so the CLI gate passes
        cfg = _config()
        cfg.ai_resolve.command = command
        ctx = ar.AIResolveContext(
            port_branch="b", base_branch="base", source_pr=None,
            conflict_files=["f.cpp"], operation="cherry-pick", skip_build=True,
        )
        return ar.resolve_with_claude(cfg, Path("."), ctx)

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
        res = self._resolve(command="definitely-not-on-path-xyz")
        self.assertTrue(res.api_aborted)


class ClosedPRClearsPartialMarkers(unittest.TestCase):
    def _refresh(self, pr_state):
        cfg = _config()
        st = PipelineState()
        st.features["grp"] = _partial()
        with mock.patch.object(
            p, "fetch_pr_by_url",
            lambda cfg, url, include_closed=False: SimpleNamespace(state=pr_state),
        ):
            changed = p._refresh_all_merge_status_from_github(cfg, st)
        return cfg, changed, st.features["grp"]

    def test_closed_pr_becomes_terminal_and_loses_partial_markers(self):
        cfg, changed, fs = self._refresh("closed")
        self.assertEqual(changed, 1)
        self.assertEqual(fs.status, "closed")
        self.assertIsNone(fs.partial_pr_count)
        self.assertFalse(p._partial_continue_allowed(cfg, fs, "recreate", True))

    def test_still_open_pr_keeps_its_partial_markers(self):
        cfg, changed, fs = self._refresh("open")
        self.assertEqual(changed, 0)
        self.assertEqual(fs.partial_pr_count, 2)
        self.assertTrue(p._partial_continue_allowed(cfg, fs, "recreate", True))


class MaxPartialContinueAttemptsConfig(unittest.TestCase):
    def test_round_trip(self):
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.dict(os.environ, {"RELEASY_STATE_DIR": tmp}):
            path = Path(tmp) / "config.yaml"
            path.write_text(
                "name: test-proj\nproject: testp\n"
                "origin:\n  remote: https://github.com/o/r.git\n"
                "pr_policy:\n  max_partial_continue_attempts: 5\n",
                encoding="utf-8",
            )
            out = Path(tmp) / "out.yaml"
            save_config(load_config(path), out)
            self.assertEqual(
                load_config(out).pr_policy.max_partial_continue_attempts, 5
            )


if __name__ == "__main__":
    unittest.main()
