"""Unit tests for keeping a resolution that trips ``settings_history``.

Covers the downgrade decision in ``resolve_with_claude`` (push + warn vs
discard), the warning rendering shared by every caller, and the config
knob's round-trip.

Stdlib unittest (no pytest dependency). Run:
    python3 -m unittest discover -s tests
"""
from __future__ import annotations

import unittest
from pathlib import Path

import releasy.ai_resolve as ar
from releasy.config import make_stateless_config
from releasy.github_ops import PRInfo


def _ctx() -> ar.AIResolveContext:
    return ar.AIResolveContext(
        port_branch="port/x",
        base_branch="antalya",
        source_pr=PRInfo(
            number=1, title="t", body="", state="merged",
            merge_commit_sha="a" * 40, head_sha="a" * 40,
            url="https://github.com/o/r/pull/1", repo_slug="o/r",
        ),
        start_sha="b" * 40,
        skip_build=True,
    )


class _Stubs:
    """Patch out everything ``resolve_with_claude`` does besides deciding."""

    def __init__(self, test, *, postcondition):
        self.test = test
        self.postcondition = postcondition
        self.saved: dict = {}

    def __enter__(self):
        for name, repl in (
            ("_resolve_backend", lambda *a, **k: (None, None)),
            ("_render_prompt", lambda *a, **k: "prompt"),
            ("_invoke_claude_with_retries",
             lambda *a, **k: (0, "", False, 1.0)),
            ("_render_correction_prompt", lambda *a, **k: "fix prompt"),
            ("_verify_postconditions", lambda *a, **k: self.postcondition),
        ):
            self.saved[name] = getattr(ar, name)
            setattr(ar, name, repl)
        return self

    def __exit__(self, *exc):
        for name, orig in self.saved.items():
            setattr(ar, name, orig)
        return False


class DowngradeDecision(unittest.TestCase):
    """A persistent ``settings_history`` failure keeps the resolution."""

    FAILED = (False, "c" * 40, "25 unauthorized setting row(s)", "settings_history")

    def _resolve(self, *, warn_on_unfixed: bool, postcondition=None):
        config = make_stateless_config("git@github.com:o/r.git", ai_enabled=True)
        config.ai_resolve.postcondition_retries = 2
        config.ai_resolve.warn_on_unfixed_postconditions = warn_on_unfixed
        with _Stubs(self, postcondition=postcondition or self.FAILED):
            return ar.resolve_with_claude(config, Path("/nonexistent"), _ctx())

    def test_kept_and_warned_by_default(self):
        result = self._resolve(warn_on_unfixed=True)
        self.assertTrue(result.success)
        self.assertEqual(result.new_head, "c" * 40)
        self.assertEqual(len(result.warnings), 1)
        self.assertIn("unauthorized setting", result.warnings[0])
        # The correction budget was spent before giving up.
        self.assertIn("correction pass(es)", result.warnings[0])

    def test_discarded_when_knob_off(self):
        result = self._resolve(warn_on_unfixed=False)
        self.assertFalse(result.success)
        self.assertEqual(result.warnings, [])
        self.assertIn("unauthorized setting", result.error or "")

    def test_other_postconditions_still_fail(self):
        # "claude didn't finish" failures are not downgradable.
        result = self._resolve(
            warn_on_unfixed=True,
            postcondition=(False, None, "unmerged paths after claude: a.cpp", None),
        )
        self.assertFalse(result.success)
        self.assertEqual(result.warnings, [])

    def test_clean_resolve_has_no_warnings(self):
        result = self._resolve(
            warn_on_unfixed=True, postcondition=(True, "c" * 40, None, None),
        )
        self.assertTrue(result.success)
        self.assertEqual(result.warnings, [])


class WarningRendering(unittest.TestCase):
    """Warnings survive into a one-line bullet and a PR comment body."""

    def test_flatten_squashes_newlines(self):
        lines = ar.flatten_resolve_warnings(
            ["a b\n(still failing after 2 correction pass(es))", "", "  "]
        )
        self.assertEqual(
            lines, ["a b (still failing after 2 correction pass(es))"],
        )

    def test_comment_body_carries_every_warning(self):
        body = ar.resolve_warning_comment_body(["first thing", "second\nthing"])
        self.assertIn("- first thing", body)
        self.assertIn("- second thing", body)
        self.assertTrue(body.endswith("\n"))

    def test_no_comment_without_a_pr(self):
        config = make_stateless_config("git@github.com:o/r.git")
        self.assertFalse(ar.flag_resolution_warnings_on_pr(config, None, ["x"]))
        self.assertFalse(
            ar.flag_resolution_warnings_on_pr(config, "https://x/pull/1", [])
        )


class ConfigKnob(unittest.TestCase):
    def test_defaults_to_keeping(self):
        config = make_stateless_config("git@github.com:o/r.git")
        self.assertTrue(config.ai_resolve.warn_on_unfixed_postconditions)

    def test_round_trips_through_yaml(self):
        import tempfile

        from releasy.config import load_config, save_config

        config = make_stateless_config("git@github.com:o/r.git")
        config.ai_resolve.warn_on_unfixed_postconditions = False
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.yaml"
            save_config(config, path)
            self.assertIn("warn_on_unfixed_postconditions", path.read_text())
            reloaded = load_config(path)
        self.assertFalse(
            reloaded.ai_resolve.warn_on_unfixed_postconditions
        )


if __name__ == "__main__":
    unittest.main()
