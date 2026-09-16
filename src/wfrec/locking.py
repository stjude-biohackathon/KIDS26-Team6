"""Portable advisory file locking.

Session state is mutated by several unrelated processes -- the CLI, the daemon,
and the GUI -- so read-modify-write cycles need a real lock rather than a
best-effort flag. ``fcntl`` covers POSIX and ``msvcrt`` covers Windows; if
neither is importable the lock degrades to a no-op rather than crashing, since
a recorder that refuses to run is worse than one with a rare race.
"""

from __future__ import annotations

import contextlib
import os
from collections.abc import Iterator
from pathlib import Path

try:  # POSIX
    import fcntl

    _HAVE_FCNTL = True
except ImportError:  # pragma: no cover - Windows
    _HAVE_FCNTL = False

try:  # Windows
    import msvcrt

    _HAVE_MSVCRT = True
except ImportError:  # pragma: no cover - POSIX
    _HAVE_MSVCRT = False


@contextlib.contextmanager
def file_lock(path: Path) -> Iterator[None]:
    """Hold an exclusive advisory lock on ``path`` for the duration of the block."""

    path.parent.mkdir(parents=True, exist_ok=True)
    handle = os.open(str(path), os.O_RDWR | os.O_CREAT, 0o600)
    try:
        if _HAVE_FCNTL:
            fcntl.flock(handle, fcntl.LOCK_EX)
        elif _HAVE_MSVCRT:  # pragma: no cover - Windows
            msvcrt.locking(handle, msvcrt.LK_LOCK, 1)
        try:
            yield
        finally:
            if _HAVE_FCNTL:
                fcntl.flock(handle, fcntl.LOCK_UN)
            elif _HAVE_MSVCRT:  # pragma: no cover - Windows
                with contextlib.suppress(OSError):
                    msvcrt.locking(handle, msvcrt.LK_UNLCK, 1)
    finally:
        os.close(handle)


def atomic_write_text(path: Path, text: str) -> None:
    """Replace ``path`` atomically so a concurrent reader never sees a torn file.

    The shell hooks read the state file on every prompt, so a partially written
    file would surface as a JSON parse error in the user's terminal.
    """

    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.tmp.{os.getpid()}")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)
