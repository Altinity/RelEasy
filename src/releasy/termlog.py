"""Optional capture of the full process terminal stream to a log file.

When :func:`configure` is called with a path (typically from config
``log_file:``), ``sys.stdout`` and ``sys.stderr`` are wrapped so that
everything the Rich console, Click, the logging module, and tracebacks
emit is appended to that file in addition to the real terminal. When
``configure(None)`` runs, wrappers are removed and the file is closed.

:func:`configure` also attaches the ``releasy`` logger's handlers, so
``log.info`` / ``log.debug`` calls reach the log file — without them the
stdlib drops every record below WARNING.
"""

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

# Captured on import — the real TTY (or whatever ``sys`` pointed at) before
# we install tees.
_real_stdout: TextIO = sys.stdout
_real_stderr: TextIO = sys.stderr

_log_fp: TextIO | None = None
_patched: bool = False
_console: Console | None = None
_log_handlers: list[logging.Handler] = []

# Every module logs through ``logging.getLogger(__name__)``, so this is the
# common ancestor of all of them.
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

    Nothing configures ``logging``, so the root logger sits at WARNING with
    no handlers: every ``log.info`` is discarded and only
    ``logging.lastResort`` puts warnings on stderr. Two handlers replace
    that, on the ``releasy`` logger rather than the root one so enabling
    INFO doesn't also unleash urllib3's per-request chatter.

    The file handler writes to ``fp`` directly instead of the tee'd
    ``sys.stderr``, and the terminal handler to the pre-tee stderr, so a
    warning is written to the log file exactly once.
    """
    to_file = logging.StreamHandler(fp)
    to_file.setLevel(logging.INFO)
    file_fmt = logging.Formatter(
        "%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%SZ",
    )
    file_fmt.converter = time.gmtime  # match the UTC session header
    to_file.setFormatter(file_fmt)

    # Bare message, as lastResort rendered it — the terminal output users
    # already know stays byte-for-byte the same.
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
    # Before closing the file the handler writes to.
    _remove_log_handlers()
    if _log_fp is not None:
        _log_fp.close()
        _log_fp = None
    _reset_console()


atexit.register(_teardown)


def configure(log_file: Path | str | None) -> None:
    """Enable or disable file mirroring. Safe to call repeatedly.

    * ``log_file is None`` — remove tees + log handlers, close the log file.
    * Otherwise — open the file in append mode, tee stdout/stderr, write a
      short session header, and attach the ``releasy`` logger's handlers
      (see :func:`_install_log_handlers`). The path should already be
      absolute (as after :func:`~releasy.config.load_config` resolves
      ``log_file:`` in YAML).
    """
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
    """Return the shared :class:`rich.console.Console`, creating it lazily.

    The console always targets the current ``sys.stdout`` (after any tee
    installed by :func:`configure`). Call :func:`configure` before the
    first :meth:`~rich.console.Console.print` if you use ``log_file``.
    """
    global _console
    if _console is None:
        _console = Console()
    return _console


class _ConsoleProxy:
    def __getattr__(self, name: str) -> Any:
        return getattr(get_console(), name)


# Backwards-compatible ``from releasy.termlog import console`` — every
# attribute is resolved on the lazily created ``Console`` instance.
console = _ConsoleProxy()
