"""Unit tests for waiting out an exhausted Claude usage session.

Covers the exhaustion detector, the wait-and-retry loop in ``_spawn_claude``
(with the real subprocess + sleep stubbed out), and the config knobs.

Stdlib unittest (no pytest dependency). Run:
    python3 -m unittest discover -s tests
"""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

import releasy.ai_resolve as a
from releasy.config import AIResolveConfig, load_config, save_config


class ExhaustionDetection(unittest.TestCase):
    """`_find_session_exhausted` fires on usage-limit text, not transients."""

    POSITIVE = [
        "Claude usage limit reached. Your limit will reset at 5pm.",
        "5-hour limit reached ∙ resets 3:00 PM",
        "You have hit your weekly limit",
        "API Error: 429 rate_limit_exceeded",
        "Your limit will reset at 2026-06-26T17:00:00Z",
        # The real Claude Code org-billing message (the one that slipped past
        # the first cut of patterns):
        "💬 You've hit your org's monthly spend limit · run /usage-credits "
        "to ask your admin for a higher limit",
        "You've hit your org's monthly spend limit",
    ]
    NEGATIVE = [
        "API Error: Overloaded",
        "API Error: 503 Service Unavailable",
        "API Error: Stream idle timeout",
        "DONE\nResolved the conflict cleanly.",
        "I added a rate limiter to the storage engine.",
        "I added a spending tracker to the billing module.",
        "",
    ]

    def test_extra_patterns_extend_detection(self):
        # A wording not covered by the built-ins is caught via config.
        self.assertIsNone(a._find_session_exhausted("ACCOUNT FROZEN"))
        self.assertEqual(
            a._find_session_exhausted("ACCOUNT FROZEN", ("account frozen",)),
            "ACCOUNT FROZEN",
        )

    def test_malformed_extra_pattern_is_skipped(self):
        # A bad user regex must not crash the run.
        self.assertIsNone(a._find_session_exhausted("text", ("[unterminated",)))

    def test_positive(self):
        for txt in self.POSITIVE:
            self.assertIsNotNone(
                a._find_session_exhausted(txt), f"should match: {txt!r}",
            )

    def test_negative(self):
        for txt in self.NEGATIVE:
            self.assertIsNone(
                a._find_session_exhausted(txt), f"should NOT match: {txt!r}",
            )


class SpawnWaitLoop(unittest.TestCase):
    """`_spawn_claude` re-prompts on exhaustion, bounded by the wait cap."""

    def setUp(self):
        self._orig_once = a._spawn_claude_once
        self._orig_sleep = a._interruptible_sleep
        self.sleeps: list[float] = []
        a._interruptible_sleep = lambda s: self.sleeps.append(s)

    def tearDown(self):
        a._spawn_claude_once = self._orig_once
        a._interruptible_sleep = self._orig_sleep

    def _spawn(self, **kw):
        defaults = dict(
            exhaustion_wait=True,
            exhaustion_max_wait_seconds=10 * 3600,
            exhaustion_poll_seconds=1800,
        )
        defaults.update(kw)
        return a._spawn_claude(["claude"], Path("."), 10, **defaults)

    def test_retries_until_success(self):
        seq = [
            (1, "usage limit reached", False),
            (1, "usage limit reached", False),
            (0, "DONE", False),
        ]
        n = {"i": 0}

        def fake(argv, repo, timeout):
            r = seq[n["i"]]
            n["i"] += 1
            return r

        a._spawn_claude_once = fake
        ec, out, to = self._spawn()
        self.assertEqual(ec, 0)
        self.assertEqual(n["i"], 3)
        self.assertEqual(len(self.sleeps), 2)

    def test_respects_cap(self):
        a._spawn_claude_once = lambda argv, repo, timeout: (
            1, "usage limit reached", False,
        )
        ec, out, to = self._spawn(exhaustion_max_wait_seconds=2 * 1800)
        # Two polls fit under the cap; the 3rd attempt still fails → give up.
        self.assertEqual(ec, 1)
        self.assertEqual(len(self.sleeps), 2)

    def test_disabled_means_no_wait(self):
        n = {"i": 0}

        def fake(argv, repo, timeout):
            n["i"] += 1
            return (1, "usage limit reached", False)

        a._spawn_claude_once = fake
        ec, out, to = self._spawn(exhaustion_wait=False)
        self.assertEqual(n["i"], 1)
        self.assertEqual(self.sleeps, [])

    def test_transient_is_not_exhaustion(self):
        # A transient API error must NOT trigger the long wait — it's handled
        # by the short-backoff retry one level up.
        a._spawn_claude_once = lambda argv, repo, timeout: (
            1, "API Error: Overloaded", False,
        )
        ec, out, to = self._spawn()
        self.assertEqual(ec, 1)
        self.assertEqual(self.sleeps, [])

    def test_clean_run_never_waits(self):
        a._spawn_claude_once = lambda argv, repo, timeout: (0, "DONE", False)
        ec, out, to = self._spawn()
        self.assertEqual(ec, 0)
        self.assertEqual(self.sleeps, [])

    def test_timeout_is_not_exhaustion(self):
        # A timeout (timed_out=True) returns immediately even if the partial
        # output happens to mention a limit.
        a._spawn_claude_once = lambda argv, repo, timeout: (
            -1, "usage limit reached", True,
        )
        ec, out, to = self._spawn()
        self.assertTrue(to)
        self.assertEqual(self.sleeps, [])


class ExhaustionConfig(unittest.TestCase):
    """Config knobs: defaults, parse, kwargs mapping, round-trip."""

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
        self.assertTrue(c.wait_on_session_exhaustion)
        self.assertEqual(c.session_exhaustion_max_wait_hours, 60)
        self.assertEqual(c.session_exhaustion_poll_minutes, 30)

    def test_kwargs_mapping(self):
        cfg = load_config(self._write())
        kw = a._exhaustion_kwargs(cfg)
        self.assertEqual(kw["exhaustion_wait"], True)
        self.assertEqual(kw["exhaustion_max_wait_seconds"], 60 * 3600)
        self.assertEqual(kw["exhaustion_poll_seconds"], 30 * 60)

    def test_parse_and_round_trip(self):
        cfg = load_config(self._write(
            "ai_resolve:\n"
            "  wait_on_session_exhaustion: false\n"
            "  session_exhaustion_max_wait_hours: 12\n"
            "  session_exhaustion_poll_minutes: 10\n"
        ))
        self.assertFalse(cfg.ai_resolve.wait_on_session_exhaustion)
        self.assertEqual(cfg.ai_resolve.session_exhaustion_max_wait_hours, 12)
        self.assertEqual(cfg.ai_resolve.session_exhaustion_poll_minutes, 10)
        kw = a._exhaustion_kwargs(cfg)
        self.assertEqual(kw["exhaustion_wait"], False)
        self.assertEqual(kw["exhaustion_max_wait_seconds"], 12 * 3600)

        path = Path(self._tmp.name) / "config.yaml"
        save_config(cfg, path)
        again = load_config(path)
        self.assertFalse(again.ai_resolve.wait_on_session_exhaustion)
        self.assertEqual(again.ai_resolve.session_exhaustion_max_wait_hours, 12)
        self.assertEqual(again.ai_resolve.session_exhaustion_poll_minutes, 10)


if __name__ == "__main__":
    unittest.main()
