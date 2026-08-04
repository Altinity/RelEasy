"""Unit tests for session PR labels — unconditional + per-port-mode.

Covers ``session.pr_labels`` / ``session.pr_labels_by_mode`` parsing and
validation, label resolution per port mode, the ``ensure_label`` name set,
and the ``refresh`` reconciliation pass (with a stubbed GitHub layer).

Stdlib unittest (no pytest dependency). Run:
    python3 -m unittest discover -s tests
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import releasy.pipeline as p
from releasy.config import (
    Config,
    OriginConfig,
    SessionConfig,
    load_session,
    save_session,
)


def _config(
    labels: list[str] | None = None,
    by_mode: dict[str, list[str]] | None = None,
) -> Config:
    return Config(
        name="proj",
        origin=OriginConfig(remote="git@github.com:acme/repo.git"),
        project="acme",
        session=SessionConfig(
            pr_labels=list(labels or []),
            pr_labels_by_mode=dict(by_mode or {}),
        ),
    )


class LabelResolution(unittest.TestCase):
    def test_mode_bucket_is_appended_to_unconditional_labels(self):
        cfg = _config(["v26.6"], {"forward_port": ["forwardport"]})
        self.assertEqual(
            p._session_pr_labels(cfg, "forward_port"), ["v26.6", "forwardport"],
        )
        self.assertEqual(p._session_pr_labels(cfg, "backport"), ["v26.6"])

    def test_unknown_mode_never_gets_a_mode_label(self):
        cfg = _config(["v26.6"], {"forward_port": ["forwardport"]})
        self.assertEqual(p._session_pr_labels(cfg, None), ["v26.6"])
        self.assertEqual(p._session_pr_labels(cfg), ["v26.6"])

    def test_ensure_pass_covers_every_mode_bucket(self):
        cfg = _config(
            ["v26.6"],
            {"forward_port": ["forwardport"], "backport": ["backport", "v26.6"]},
        )
        # Deduped, unconditional first — these all have to exist on origin
        # before any unit is ported.
        self.assertEqual(
            p._all_session_label_names(cfg),
            ["v26.6", "forwardport", "backport"],
        )

    def test_no_session_means_no_labels(self):
        cfg = _config()
        cfg.session = None
        self.assertEqual(p._session_pr_labels(cfg, "forward_port"), [])
        self.assertEqual(p._all_session_label_names(cfg), [])


class Parsing(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / "s.session.yaml"

    def tearDown(self):
        self._tmp.cleanup()

    def _load(self, body: str) -> SessionConfig:
        self.path.write_text(body)
        cfg = _config()
        cfg.session_file = self.path
        return load_session(cfg)

    def test_roundtrip(self):
        sess = self._load(
            "pr_labels:\n- v26.6\n"
            "pr_labels_by_mode:\n  forward_port:\n  - forwardport\n"
        )
        self.assertEqual(sess.pr_labels, ["v26.6"])
        self.assertEqual(
            sess.pr_labels_by_mode, {"forward_port": ["forwardport"]},
        )
        out = Path(self._tmp.name) / "out.session.yaml"
        save_session(sess, out)
        self.assertEqual(
            self._load(out.read_text()).pr_labels_by_mode,
            {"forward_port": ["forwardport"]},
        )

    def test_bare_string_is_accepted(self):
        sess = self._load("pr_labels_by_mode:\n  backport: bp\n")
        self.assertEqual(sess.pr_labels_by_mode, {"backport": ["bp"]})

    def test_absent_key_defaults_to_empty(self):
        self.assertEqual(self._load("pr_labels: [v26.6]\n").pr_labels_by_mode, {})

    def test_unknown_mode_key_is_rejected(self):
        with self.assertRaises(ValueError) as cm:
            self._load("pr_labels_by_mode:\n  frontport:\n  - x\n")
        self.assertIn("not a port mode", str(cm.exception))

    def test_non_mapping_is_rejected(self):
        with self.assertRaises(ValueError) as cm:
            self._load("pr_labels_by_mode:\n- forwardport\n")
        self.assertIn("must be a mapping", str(cm.exception))

    def test_empty_label_name_is_rejected(self):
        with self.assertRaises(ValueError) as cm:
            self._load("pr_labels_by_mode:\n  forward_port:\n  - ' '\n")
        self.assertIn("non-empty strings", str(cm.exception))


class Reconcile(unittest.TestCase):
    """``reconcile_session_labels_on_prs`` with the GitHub layer stubbed."""

    def setUp(self):
        self.added: list[tuple[int, str]] = []
        self._real_add = p.add_label_to_pr
        p.add_label_to_pr = lambda cfg, num, lbl: (  # type: ignore[assignment]
            self.added.append((num, lbl)) or True
        )
        import releasy.github_ops as gh

        self._real_fetch = gh.fetch_pr_by_url
        self.current: dict[str, list[str]] = {}
        gh.fetch_pr_by_url = (  # type: ignore[assignment]
            lambda cfg, url, include_closed=False: type(
                "I", (), {"labels": self.current.get(url, [])},
            )()
        )
        self._gh = gh

    def tearDown(self):
        p.add_label_to_pr = self._real_add  # type: ignore[assignment]
        self._gh.fetch_pr_by_url = self._real_fetch  # type: ignore[assignment]

    def test_only_missing_labels_are_added_per_mode(self):
        cfg = _config(["v26.6"], {"forward_port": ["forwardport"]})
        fwd = "https://github.com/acme/repo/pull/1"
        back = "https://github.com/acme/repo/pull/2"
        self.current[fwd] = ["v26.6"]  # short the forwardport label
        self.current[back] = ["v26.6"]  # already complete for a backport
        result = p.reconcile_session_labels_on_prs(
            cfg, [(fwd, 1, "forward_port"), (back, 2, "backport")],
        )
        self.assertEqual(result, [(fwd, ["forwardport"])])
        self.assertEqual(self.added, [(1, "forwardport")])

    def test_unknown_mode_pr_is_not_given_a_mode_label(self):
        cfg = _config(["v26.6"], {"forward_port": ["forwardport"]})
        url = "https://github.com/acme/repo/pull/3"
        self.current[url] = ["v26.6"]
        self.assertEqual(p.reconcile_session_labels_on_prs(cfg, [(url, 3, None)]), [])
        self.assertEqual(self.added, [])

    def test_no_configured_labels_is_a_no_op(self):
        cfg = _config()
        url = "https://github.com/acme/repo/pull/4"
        self.assertEqual(
            p.reconcile_session_labels_on_prs(cfg, [(url, 4, "forward_port")]), [],
        )
        self.assertEqual(self.added, [])


if __name__ == "__main__":
    unittest.main()
