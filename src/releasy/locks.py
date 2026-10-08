"""Per-project ``fcntl.flock`` lock serializing releasy runs that share a state file."""

from __future__ import annotations

import fcntl
import os
import platform
import sys
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

import click

from releasy.config import Config, lock_file_path


def _format_holder(raw: str) -> str:
    cleaned = raw.strip()
    return cleaned or "another releasy process (lockfile is empty)"


def _write_holder(fp, command: str | None = None) -> None:
    """Stamp the lockfile with our identity for contending readers."""
    fp.seek(0)
    fp.truncate()
    cmd = command if command is not None else " ".join(sys.argv)
    body = (
        f"pid={os.getpid()}\n"
        f"host={platform.node()}\n"
        f"command={cmd}\n"
        f"started={datetime.now(timezone.utc).isoformat()}\n"
    )
    fp.write(body)
    fp.flush()


@contextmanager
def project_lock(config: Config) -> Iterator[Path]:
    """Acquire a non-blocking exclusive project lock; raise ClickException on contention."""
    lock_path: Path = lock_file_path(config.name)
    lock_path.parent.mkdir(parents=True, exist_ok=True)

    fp = open(lock_path, "a+")
    try:
        try:
            fcntl.flock(fp.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            fp.seek(0)
            holder = fp.read()
            fp.close()
            raise click.ClickException(
                f"Project {config.name!r} is already locked by another "
                f"releasy process. Lockfile: {lock_path}\n"
                f"  {_format_holder(holder)}"
            )

        _write_holder(fp)
        try:
            yield lock_path
        finally:
            try:
                fcntl.flock(fp.fileno(), fcntl.LOCK_UN)
            except OSError:
                pass
    finally:
        try:
            fp.close()
        except OSError:
            pass
        try:
            lock_path.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            pass
