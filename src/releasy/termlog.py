"""Optional tee of stdout/stderr and the ``releasy`` logger to a log file."""

from __future__ import annotations

import atexit
import io
import logging
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, TextIO

from rich.console import Console

_real_stdout: TextIO = sys.stdout
_real_stderr: TextIO = sys.stderr

_log_fp: TextIO | None = None
_patched: bool = False
_console: Console | None = None
_log_handlers: list[logging.Handler] = []

_LOGGER_NAME = "releasy"


class _TeeIO(io.TextIOBase):
    """Write to two text streams; colors follow the primary (terminal)."""

    def __init__(self, primary: TextIO, secondary: TextIO) -> None:
        self._p = primary
        self._s = secondary

    @property
    def encoding(self) -> str:  # type: ignore[override]
        return self._p.encoding

    @property
    def errors(self) -> str | None:  # type: ignore[override]
        return getattr(self._p, "errors", "strict")  # type: ignore[no-any-return]

    def write(self, s: str) -> int:  # type: ignore[override]
        n = self._p.write(s)
        self._s.write(s)
        return n

    def flush(self) -> None:  # type: ignore[override]
        self._p.flush()
        self._s.flush()

    def isatty(self) -> bool:  # type: ignore[override]
        return self._p.isatty()

    def fileno(self) -> int:  # type: ignore[override]
        return self._p.fileno()


def _reset_console() -> None:
    global _console
    _console = None


def _install_log_handlers(fp: TextIO) -> None:
    """Route the ``releasy`` logger to ``fp`` (INFO+) and the terminal (WARNING+).

    Handlers write to ``fp`` and the pre-tee stderr so a warning lands in
    the log file exactly once.
    """
    to_file = logging.StreamHandler(fp)
    to_file.setLevel(logging.INFO)
    file_fmt = logging.Formatter(
        "%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%SZ",
    )
    file_fmt.converter = time.gmtime  # match the UTC session header
    to_file.setFormatter(file_fmt)

    # Bare message, matching logging.lastResort's terminal output.
    to_term = logging.StreamHandler(_real_stderr)
    to_term.setLevel(logging.WARNING)
    to_term.setFormatter(logging.Formatter("%(message)s"))

    logger = logging.getLogger(_LOGGER_NAME)
    logger.setLevel(logging.INFO)
    for handler in (to_file, to_term):
        logger.addHandler(handler)
        _log_handlers.append(handler)


def _remove_log_handlers() -> None:
    """Detach our handlers and hand WARNING+ back to ``logging.lastResort``."""
    logger = logging.getLogger(_LOGGER_NAME)
    for handler in _log_handlers:
        logger.removeHandler(handler)
        handler.close()  # StreamHandler.close() leaves the stream open
    _log_handlers.clear()
    logger.setLevel(logging.NOTSET)


def _teardown() -> None:
    global _log_fp, _patched
    if _patched:
        sys.stdout = _real_stdout
        sys.stderr = _real_stderr
        _patched = False
    # Must precede closing the file the handler writes to.
    _remove_log_handlers()
    if _log_fp is not None:
        _log_fp.close()
        _log_fp = None
    _reset_console()


atexit.register(_teardown)


def configure(log_file: Path | str | None) -> None:
    """Enable (path) or disable (None) file mirroring. Safe to call repeatedly."""
    _teardown()
    if not log_file:
        return
    path = Path(log_file) if not isinstance(log_file, Path) else log_file
    path.parent.mkdir(parents=True, exist_ok=True)
    global _log_fp, _patched
    _log_fp = open(path, "a", encoding="utf-8")
    ts = datetime.now(timezone.utc).isoformat()
    _log_fp.write(
        f"\n{'=' * 60}\nreleasy session start {ts}\n{'=' * 60}\n"
    )
    _log_fp.flush()
    sys.stdout = _TeeIO(_real_stdout, _log_fp)
    sys.stderr = _TeeIO(_real_stderr, _log_fp)
    _patched = True
    _install_log_handlers(_log_fp)
    _reset_console()


def get_console() -> Console:
    """Return the shared lazily-created Console bound to the current ``sys.stdout``."""
    global _console
    if _console is None:
        _console = Console()
    return _console


class _ConsoleProxy:
    def __getattr__(self, name: str) -> Any:
        return getattr(get_console(), name)


# Resolves every attribute on the lazily created Console.
console = _ConsoleProxy()
