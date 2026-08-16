"""Unit tests for the independent second opinion on a shard's outcome.

After the first session concludes, a fresh read-only session audits the
outcome — but only when the shard is in doubt: it committed code, or
its verdict contradicts the baseline. Covers that gate, the rendered
audit prompt, and how a dispute surfaces in the PR comment.

Stdlib unittest (no pytest dependency). Run:
    python3 -m unittest discover -s tests
"""
from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from releasy import analyze_fails
from releasy.analyze_fails import (
    PRRunResult,
    RedoContext,
    ShardOutcome,
    _format_pr_comment,
    _redo_section,
    _render_verify_prompt,
    _verification_reason,
)
from releasy.ci_failures import BaselineRun, FailedTest, PRFailures
from releasy.config import build_stateless_analyze_fails_config

SHARD = "Stateless tests (amd_debug, parallel)"
PR = "https://github.com/o/r/pull/7"


def _test(name: str) -> FailedTest:
    return FailedTest(
        name=name, status="FAIL", category="stateless",
        shard_context=SHARD, target_url="https://report",
    )


def _baseline(**kw) -> BaselineRun:
    return BaselineRun(
        sha="7eba7cb7099c40561fc2973a6b06a493d56ab2b4",
        committed_at="2026-08-14T08:36:13Z",
        checks_total=57, checks_failed=11,
        failing=kw.pop("failing", {("stateless", "00001_old"): "shard A"}),
        categories_run={"stateless"},
    )


class DoubtGate(unittest.TestCase):
    """Only shards in doubt are worth a second session."""

    def test_committed_code_is_always_audited(self):
        reason = _verification_reason(
            "DONE", 1, [_test("00001_old")], _baseline(), {}, 2, PR,
        )
        self.assertIsNotNone(reason)
        self.assertIn("committed", reason)

    def test_unrelated_over_a_new_failure_is_audited(self):
        reason = _verification_reason(
            "UNRELATED", 0, [_test("00002_new")], _baseline(), {}, 2, PR,
        )
        self.assertIsNotNone(reason)
        self.assertIn("passed at the baseline", reason)
        self.assertIn("00002_new", reason)

    def test_all_pre_existing_unrelated_is_left_alone(self):
        self.assertIsNone(_verification_reason(
            "UNRELATED", 0, [_test("00001_old")], _baseline(), {}, 2, PR,
        ))

    def test_flaky_elsewhere_corroboration_settles_it(self):
        # New since baseline, but failing on two other tracked PRs —
        # the UNRELATED call has evidence behind it.
        reason = _verification_reason(
            "UNRELATED", 0, [_test("00002_new")], _baseline(),
            {"stateless::00002_new": ["https://x/1", "https://x/2"]},
            2, PR,
        )
        self.assertIsNone(reason)

    def test_the_prs_own_url_is_not_corroboration(self):
        reason = _verification_reason(
            "UNRELATED", 0, [_test("00002_new")], _baseline(),
            {"stateless::00002_new": [PR, PR]}, 2, PR,
        )
        self.assertIsNotNone(reason)

    def test_unresolved_without_commits_has_nothing_to_audit(self):
        self.assertIsNone(_verification_reason(
            "UNRESOLVED", 0, [_test("00002_new")], _baseline(), {}, 2, PR,
        ))

    def test_no_baseline_means_no_contradiction_to_detect(self):
        self.assertIsNone(_verification_reason(
            "UNRELATED", 0, [_test("00002_new")], None, {}, 2, PR,
        ))


class AuditPromptRendering(unittest.TestCase):

    def _render(self, outcome: ShardOutcome, commit_range: str = ""):
        config = build_stateless_analyze_fails_config(
            origin_url="https://github.com/o/r",
        )
        return _render_verify_prompt(
            config, Path("/work/ch"), PR, 7, "head", "base", SHARD,
            "https://report", "stateless",
            [_test("00001_old"), _test("00002_new")],
            _baseline(), outcome, commit_range,
        )

    def test_carries_the_verdict_reason_and_baseline_labels(self):
        outcome = ShardOutcome(
            category="stateless", shard_context=SHARD,
            target_url="https://report", test_count=2,
            classification="DONE", commits_added=1,
            narration="Fixed it by updating the reference file.",
        )
        outcome.verify_reason = "the session committed 1 change(s)"
        prompt = self._render(outcome, "abc123..def456")
        self.assertIn("DONE", prompt)
        self.assertIn("the session committed 1 change(s)", prompt)
        self.assertIn("abc123..def456", prompt)
        self.assertIn("[pre-existing at baseline] `00001_old`", prompt)
        self.assertIn("[NEW since baseline] `00002_new`", prompt)
        self.assertIn("Fixed it by updating the reference file.", prompt)

    def test_frames_the_narration_as_claims_not_evidence(self):
        outcome = ShardOutcome(
            category="stateless", shard_context=SHARD,
            target_url="https://report", test_count=2,
            classification="DONE", narration="trust me",
        )
        outcome.verify_reason = "x"
        prompt = self._render(outcome)
        self.assertIn("claims to check", prompt)
        self.assertIn("VERDICT: OK|NEEDS_ATTENTION", prompt)
        self.assertIn("Read-only", prompt)


class AuditDriver(unittest.TestCase):
    """End-to-end bookkeeping around one audit, with the session faked."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        for cmd in (
            ["init", "-q", "-b", "main"],
            ["config", "user.email", "t@e.st"],
            ["config", "user.name", "t"],
            ["commit", "-q", "--allow-empty", "-m", "base"],
        ):
            subprocess.run(
                ["git", *cmd], cwd=self.repo, check=True,
                capture_output=True,
            )

    def _drive(self, transcript, *, exit_code=0, timed_out=False,
               side_effect=None, **kw):
        config = build_stateless_analyze_fails_config(
            origin_url="https://github.com/o/r",
        )
        outcome = ShardOutcome(
            category="stateless", shard_context=SHARD,
            target_url="https://report", test_count=1,
            classification=kw.pop("classification", "DONE"),
            commits_added=kw.pop("commits_added", 1),
            narration="all fixed", cost_usd=1.0,
        )
        run = PRRunResult(pr_url=PR, head_sha="dead", head_ref="head")
        run.outcomes.append(outcome)

        def _fake(*_a, **_kw):
            if side_effect is not None:
                side_effect(self.repo)
            return exit_code, transcript, timed_out, 0.25

        with mock.patch.object(
            analyze_fails, "_invoke_verifier", _fake,
        ):
            analyze_fails._audit_shard_outcome(
                config, self.repo, run, outcome,
                pr_url=PR, pr_number=7, pr_branch="head",
                base_branch="base", category="stateless",
                tests=[_test("00002_new")], baseline=_baseline(),
                flaky_map={}, commit_range="abc..def",
            )
        return run, outcome

    def test_an_auditor_that_commits_blocks_the_push(self):
        def _commit(repo: Path):
            subprocess.run(
                ["git", "commit", "-q", "--allow-empty", "-m", "oops"],
                cwd=repo, check=True, capture_output=True,
            )

        run, outcome = self._drive(
            "VERDICT: OK\nSUMMARY: fine\nEND_VERIFY\n",
            side_effect=_commit,
        )
        self.assertTrue(run.audit_mutated_repo)
        self.assertTrue(any("modified the repo" in w for w in run.warnings))

    def test_an_auditor_that_dirties_the_tree_blocks_the_push(self):
        def _touch(repo: Path):
            (repo / "tracked.txt").write_text("x")
            subprocess.run(
                ["git", "add", "tracked.txt"], cwd=repo, check=True,
                capture_output=True,
            )

        run, _ = self._drive(
            "VERDICT: OK\nSUMMARY: fine\nEND_VERIFY\n", side_effect=_touch,
        )
        self.assertTrue(run.audit_mutated_repo)

    def test_a_clean_audit_leaves_the_push_alone(self):
        run, _ = self._drive("VERDICT: OK\nSUMMARY: fine\nEND_VERIFY\n")
        self.assertFalse(run.audit_mutated_repo)

    def test_dispute_is_recorded_and_costed(self):
        run, outcome = self._drive(
            "VERDICT: NEEDS_ATTENTION\n"
            "SUMMARY: the commit deletes the assertion\n"
            "FINDINGS:\n"
            "- abc123 removes EXPECT_EQ in foo_test.cpp:42\n"
            "END_VERIFY\n"
        )
        self.assertTrue(outcome.disputed)
        self.assertEqual(run.shards_disputed, 1)
        self.assertEqual(run.shards_audited, 1)
        self.assertEqual(outcome.verify_findings, [
            "abc123 removes EXPECT_EQ in foo_test.cpp:42",
        ])
        # Audit cost lands on both the shard and the run.
        self.assertAlmostEqual(run.cost_usd, 0.25)
        self.assertAlmostEqual(outcome.cost_usd, 1.25)

    def test_agreement_leaves_the_run_undisputed(self):
        run, outcome = self._drive(
            "VERDICT: OK\nSUMMARY: real fix, in scope\nEND_VERIFY\n"
        )
        self.assertFalse(outcome.disputed)
        self.assertEqual(run.shards_disputed, 0)
        self.assertEqual(run.shards_audited, 1)

    def test_a_failed_audit_is_advisory_not_fatal(self):
        run, outcome = self._drive("", timed_out=True)
        self.assertEqual(outcome.verify_verdict, "unknown")
        self.assertEqual(run.shards_disputed, 0)
        self.assertTrue(any("unaudited" in w for w in run.warnings))

    def test_an_unparsable_verdict_is_not_treated_as_agreement(self):
        run, outcome = self._drive("I think it's probably fine\n")
        self.assertEqual(outcome.verify_verdict, "unknown")
        self.assertFalse(outcome.disputed)
        self.assertTrue(any("no parsable verdict" in w for w in run.warnings))

    def test_a_shard_not_in_doubt_spawns_no_session(self):
        config = build_stateless_analyze_fails_config(
            origin_url="https://github.com/o/r",
        )
        outcome = ShardOutcome(
            category="stateless", shard_context=SHARD,
            target_url="https://report", test_count=1,
            classification="UNRELATED", commits_added=0,
        )
        run = PRRunResult(pr_url=PR, head_sha="dead", head_ref="head")
        with mock.patch.object(
            analyze_fails, "_invoke_verifier",
        ) as spawn:
            analyze_fails._audit_shard_outcome(
                config, Path("/work/ch"), run, outcome,
                pr_url=PR, pr_number=7, pr_branch="head",
                base_branch="base", category="stateless",
                tests=[_test("00001_old")], baseline=_baseline(),
                flaky_map={}, commit_range="",
            )
        spawn.assert_not_called()
        self.assertIsNone(outcome.verify_reason)
        self.assertEqual(run.shards_audited, 0)

    def test_turning_the_audit_off_skips_it_entirely(self):
        config = build_stateless_analyze_fails_config(
            origin_url="https://github.com/o/r",
        )
        config.analyze_fails.verify_outcome = False
        outcome = ShardOutcome(
            category="stateless", shard_context=SHARD,
            target_url="https://report", test_count=1,
            classification="DONE", commits_added=3,
        )
        run = PRRunResult(pr_url=PR, head_sha="dead", head_ref="head")
        with mock.patch.object(
            analyze_fails, "_invoke_verifier",
        ) as spawn:
            analyze_fails._audit_shard_outcome(
                config, Path("/work/ch"), run, outcome,
                pr_url=PR, pr_number=7, pr_branch="head",
                base_branch="base", category="stateless",
                tests=[_test("00002_new")], baseline=_baseline(),
                flaky_map={}, commit_range="a..b",
            )
        spawn.assert_not_called()
        self.assertEqual(run.shards_audited, 0)


class RedoPrompt(unittest.TestCase):
    """A re-investigation is told what the audit rejected, and to act."""

    REDO = RedoContext(
        round_index=1, classification="DONE", commits_added=1,
        commit_range="abc1234..def5678",
        audit_summary="The commit rewrites the reference file.",
        audit_findings=["abc1234 rewrites 00001_x.reference"],
    )

    def test_first_round_has_no_prior_to_correct(self):
        self.assertIn("First look", _redo_section(None, "head"))

    def test_redo_carries_verdict_findings_and_the_revert_instruction(self):
        section = _redo_section(self.REDO, "head")
        self.assertIn("attempt 2", section)
        self.assertIn("abc1234..def5678", section)
        self.assertIn("rewrites 00001_x.reference", section)
        self.assertIn("git revert --no-edit", section)
        self.assertIn("append-only", section)

    def test_it_may_stand_its_ground_with_evidence(self):
        section = _redo_section(self.REDO, "head")
        self.assertIn("Standing your ground is allowed", section)
        self.assertIn("ignoring the finding is not", section)


class RoundLoop(unittest.TestCase):
    """A dispute triggers one redo; the last round is the shard's verdict."""

    def _process(self, rounds, *, max_rounds=2, mutate_on_round=None):
        """Drive ``_process_pr`` with both AI sessions scripted.

        ``rounds`` is a list of ``(classification, commits, verdict)`` —
        one per investigator session the loop is allowed to run.
        """
        config = build_stateless_analyze_fails_config(
            origin_url="https://github.com/o/r",
        )
        config.analyze_fails.max_investigation_rounds = max_rounds
        calls = {"invest": 0, "audit": 0}

        def _round(cfg, repo, prompt, start_sha, **kw):
            spec = rounds[calls["invest"]]
            calls["invest"] += 1
            outcome = ShardOutcome(
                category=kw["category"], shard_context=kw["shard_ctx"],
                target_url=kw["target_url"], test_count=kw["test_count"],
                classification=spec[0], commits_added=spec[1],
                narration="n",
            )
            new_sha = f"sha{calls['invest']}" if spec[1] else start_sha
            return outcome, new_sha, False

        def _audit(cfg, repo, result, outcome, **kw):
            idx = calls["audit"]
            calls["audit"] += 1
            verdict = rounds[idx][2]
            if verdict is None:
                return
            outcome.verify_reason = "scripted"
            outcome.verify_verdict = verdict
            if verdict == "needs_attention":
                result.shards_disputed += 1
            result.shards_audited += 1
            if mutate_on_round == idx + 1:
                result.audit_mutated_repo = True

        failures = PRFailures(
            pr_url=PR, head_sha="sha0", head_ref="head", base_ref="base",
            statuses=[], failed_tests=[_test("00002_new")],
        )
        fake_pr = mock.Mock(state="open", head_sha="sha0")

        with mock.patch.multiple(
            analyze_fails,
            fetch_pr_by_url=mock.Mock(return_value=fake_pr),
            _fetch_pr_meta=mock.Mock(
                return_value=("head", "o/r", "base", "sha0", 7),
            ),
            get_origin_repo_slug=mock.Mock(return_value="o/r"),
            discover_pr_failures=mock.Mock(return_value=(failures, None)),
            _resolve_baseline=mock.Mock(return_value=(None, None)),
            _checkout_pr_head=mock.Mock(return_value=(True, "sha0", None)),
            _write_build_script=mock.Mock(),
            _resolve_backend=mock.Mock(return_value=(None, None)),
            _write_failed_tests_manifest=mock.Mock(),
            _render_shard_prompt=mock.Mock(return_value="prompt"),
            _run_investigation_round=_round,
            _audit_shard_outcome=_audit,
            run_git=mock.Mock(
                return_value=mock.Mock(returncode=0, stdout="0", stderr=""),
            ),
        ):
            run = analyze_fails._process_pr(
                config, Path("/w"), PR, {}, push=False, dry_run=False,
            )
        return run, calls

    def test_dispute_triggers_exactly_one_redo(self):
        run, calls = self._process([
            ("DONE", 1, "needs_attention"),
            ("UNRELATED", 0, "ok"),
        ])
        self.assertEqual(calls["invest"], 2)
        self.assertEqual(len(run.outcomes), 2)
        self.assertTrue(run.outcomes[0].superseded)
        self.assertFalse(run.outcomes[1].superseded)
        self.assertEqual(run.outcomes[1].round_index, 2)

    def test_only_the_final_round_is_tallied(self):
        run, _ = self._process([
            ("DONE", 1, "needs_attention"),
            ("UNRELATED", 0, "ok"),
        ])
        self.assertEqual(run.shards_processed, 1)
        self.assertEqual(run.shards_done, 0)
        self.assertEqual(run.shards_unrelated, 1)

    def test_agreement_first_time_means_no_redo(self):
        run, calls = self._process([("DONE", 1, "ok")])
        self.assertEqual(calls["invest"], 1)
        self.assertEqual(run.shards_done, 1)

    def test_the_cap_stops_an_endless_argument(self):
        run, calls = self._process([
            ("DONE", 1, "needs_attention"),
            ("DONE", 1, "needs_attention"),
        ])
        self.assertEqual(calls["invest"], 2)
        self.assertEqual(run.shards_done, 1)
        self.assertTrue(any("still disputed" in w for w in run.warnings))

    def test_one_round_configured_means_advisory_only(self):
        run, calls = self._process(
            [("DONE", 1, "needs_attention")], max_rounds=1,
        )
        self.assertEqual(calls["invest"], 1)
        self.assertEqual(run.shards_disputed, 1)

    def test_a_dirtied_work_dir_stops_the_redo(self):
        # Redoing on a work-dir the auditor touched would build on it.
        run, calls = self._process(
            [("DONE", 1, "needs_attention"), ("DONE", 0, "ok")],
            mutate_on_round=1,
        )
        self.assertEqual(calls["invest"], 1)
        self.assertTrue(run.audit_mutated_repo)


class DisputeSurfacing(unittest.TestCase):

    def _run(self, **kw) -> PRRunResult:
        outcome = ShardOutcome(
            category="stateless", shard_context=SHARD,
            target_url="https://report", test_count=2,
            classification="DONE", commits_added=1,
            narration="all fixed",
        )
        outcome.verify_reason = "the session committed 1 change(s)"
        outcome.verify_verdict = kw.pop("verdict", "needs_attention")
        outcome.verify_summary = kw.pop(
            "summary", "The commit deletes the assertion instead of fixing it.",
        )
        outcome.verify_findings = kw.pop(
            "findings", ["abc123 removes the EXPECT_EQ in foo_test.cpp:42"],
        )
        run = PRRunResult(
            pr_url=PR, head_sha="deadbeef12", head_ref="head",
            shards_total=1, shards_processed=1, shards_done=1,
            tests_total=2, commits_added=1, shards_audited=1,
            shards_disputed=1 if outcome.disputed else 0,
        )
        run.outcomes.append(outcome)
        return run

    def test_disputed_run_never_reads_as_done(self):
        body = _format_pr_comment(self._run())
        self.assertIn("analyze-fails` — DISPUTED", body)
        self.assertIn("1 still disputed", body)

    def test_findings_are_quoted_in_the_comment(self):
        body = _format_pr_comment(self._run())
        self.assertIn("DONE — DISPUTED", body)
        self.assertIn("removes the EXPECT_EQ", body)
        self.assertIn("deletes the assertion", body)

    def test_agreement_is_stated_too(self):
        body = _format_pr_comment(
            self._run(verdict="ok", summary="", findings=[]),
        )
        self.assertIn("nothing left disputed", body)
        self.assertIn("agrees", body)
        self.assertNotIn("DISPUTED", body)

    def test_a_dispute_settled_by_a_redo_is_not_an_open_dispute(self):
        run = self._run()
        run.outcomes[0].superseded = True
        run.outcomes[0].round_index = 1
        settled = ShardOutcome(
            category="stateless", shard_context=SHARD,
            target_url="https://report", test_count=2,
            classification="UNRELATED", commits_added=1,
            narration="reverted it", round_index=2,
        )
        settled.verify_reason = "the session committed 1 change(s)"
        settled.verify_verdict = "ok"
        run.outcomes.append(settled)
        run.shards_audited = 2

        self.assertEqual(run.open_disputes, 0)
        body = _format_pr_comment(run)
        self.assertNotIn("analyze-fails` — DISPUTED", body)
        self.assertIn("sent back for re-investigation and settled", body)
        self.assertIn("REDONE", body)
        # The rejected round stays in the record, findings and all.
        self.assertIn("removes the EXPECT_EQ", body)

    def test_unaudited_run_says_nothing_was_in_doubt(self):
        run = PRRunResult(
            pr_url=PR, head_sha="deadbeef12", head_ref="head",
            shards_total=1, shards_processed=1, shards_unrelated=1,
        )
        run.outcomes.append(ShardOutcome(
            category="stateless", shard_context=SHARD,
            target_url="https://report", test_count=1,
            classification="UNRELATED",
        ))
        body = _format_pr_comment(run)
        self.assertIn("no shard was in doubt", body)


if __name__ == "__main__":
    unittest.main()
