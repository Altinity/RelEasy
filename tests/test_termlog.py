from __future__ import annotations

import io
import logging
import tempfile
import unittest
from pathlib import Path

from releasy import termlog


class LogHandlers(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.log_path = Path(self._tmp.name) / "releasy.log"
        self._saved_stderr = termlog._real_stderr
        self.term = io.StringIO()
        termlog._real_stderr = self.term
        self.log = logging.getLogger("releasy.test_termlog")

    def tearDown(self) -> None:
        termlog.configure(None)
        termlog._real_stderr = self._saved_stderr
        self._tmp.cleanup()

    @property
    def _releasy_logger(self) -> logging.Logger:
        return logging.getLogger("releasy")

    def test_info_is_recorded_with_level_and_logger_name(self) -> None:
        termlog.configure(self.log_path)
        self.log.info("hello from info")
        body = self.log_path.read_text()
        self.assertIn("hello from info", body)
        self.assertIn("INFO", body)
        self.assertIn("releasy.test_termlog", body)

    def test_debug_is_below_the_threshold(self) -> None:
        termlog.configure(self.log_path)
        self.log.debug("noisy detail")
        self.assertNotIn("noisy detail", self.log_path.read_text())

    def test_warning_hits_the_file_once_and_the_terminal(self) -> None:
        termlog.configure(self.log_path)
        self.log.warning("heads up")
        self.assertEqual(self.log_path.read_text().count("heads up"), 1)
        self.assertEqual(self.term.getvalue(), "heads up\n")

    def test_teardown_restores_the_logger(self) -> None:
        termlog.configure(self.log_path)
        termlog.configure(None)
        self.assertEqual(self._releasy_logger.handlers, [])
        self.assertEqual(self._releasy_logger.level, logging.NOTSET)
        self.log.info("after teardown")
        self.assertNotIn("after teardown", self.log_path.read_text())

    def test_reconfigure_does_not_stack_handlers(self) -> None:
        termlog.configure(self.log_path)
        termlog.configure(self.log_path)
        self.assertEqual(len(self._releasy_logger.handlers), 2)

    def test_console_output_is_still_mirrored(self) -> None:
        termlog.configure(self.log_path)
        termlog.console.print("rich line")
        self.assertIn("rich line", self.log_path.read_text())


if __name__ == "__main__":
    unittest.main()
