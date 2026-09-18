"""Recorder state, in two representations.

``state.json`` is authoritative and rich: it is what the CLI, GUI and daemon
read and mutate. The ``active`` sentinel is a *derived*, one-line projection of
it, and exists purely so that a shell hook can answer "should I record this
command, and where do I put it?" using only shell builtins.

Why two files instead of one
----------------------------
The hook runs on *every* prompt in *every* open terminal. Parsing JSON there
would mean forking ``jq`` or ``python`` (5-25 ms), which users feel, and which
fails outright when the tool is missing. The sentinel is instead a single
TAB-separated line::

    <session_id>\\t<flags>\\t<spool_dir>

which a hook reads with ``IFS='\\t' read -r`` and tests with ``case``, both
builtins, in roughly 15 microseconds. ``flags`` is a set of single letters --
presence means enabled -- so adding a source never changes the parse.
"""

from __future__ import annotations

import contextlib
import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import SOURCES, paths
from .locking import atomic_write_text, file_lock

#: Single-letter flag per capture source, used in the sentinel's ``flags`` field.
SOURCE_FLAGS: dict[str, str] = {
    "shell": "s",
    "context": "p",
    "agents": "a",
    "files": "f",
    "screen": "c",
}

FLAG_TO_SOURCE = {flag: source for source, flag in SOURCE_FLAGS.items()}

SHELL_BACKEND_DEVSQL = "devsql"
SHELL_BACKEND_SPOOL = "hook-spool"
SHELL_BACKENDS = {SHELL_BACKEND_DEVSQL, SHELL_BACKEND_SPOOL}


def shell_output_unavailable_reason(backend: str) -> str:
    """Explain why the selected backend cannot capture terminal output."""

    if backend == SHELL_BACKEND_DEVSQL:
        return (
            "Full shell output capture is unavailable with DevSQL. DevSQL "
            "records commands and execution metadata, not stdout or stderr."
        )
    return (
        "Full shell output capture is unavailable with hook-spool. Installed "
        "hooks record commands and execution metadata, not stdout or stderr."
    )


@dataclass(slots=True)
class RecorderState:
    """The recorder's cross-process state."""

    active_session: str | None = None
    sources: dict[str, bool] = field(
        default_factory=lambda: dict.fromkeys(SOURCES, True)
    )
    shell_output: bool = False
    shell_backend: str = SHELL_BACKEND_SPOOL
    paused: list[str] = field(default_factory=list)
    api_url: str | None = None
    api_token: str | None = None
    updated_at: float = field(default_factory=time.time)

    # ------------------------------------------------------------------ load
    @classmethod
    def load(cls) -> "RecorderState":
        """Read state from disk, returning defaults when absent or corrupt.

        A corrupt state file must never be fatal: the recorder is a tool people
        run *around* their real work, so it degrades to "not recording" rather
        than refusing to start.
        """

        path = paths.state_path()
        if not path.exists():
            return cls()
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return cls()
        return cls.from_dict(payload)

    @classmethod
    def from_dict(cls, payload: dict) -> "RecorderState":
        sources = dict.fromkeys(SOURCES, True)
        for name, enabled in (payload.get("sources") or {}).items():
            if name in sources:
                sources[name] = bool(enabled)
        shell_backend = payload.get("shell_backend", SHELL_BACKEND_SPOOL)
        if shell_backend not in SHELL_BACKENDS:
            shell_backend = SHELL_BACKEND_SPOOL
        return cls(
            active_session=payload.get("active_session"),
            sources=sources,
            shell_output=bool(payload.get("shell_output", False)),
            shell_backend=shell_backend,
            paused=list(payload.get("paused") or []),
            api_url=payload.get("api_url"),
            api_token=payload.get("api_token"),
            updated_at=float(payload.get("updated_at") or time.time()),
        )

    def to_dict(self) -> dict:
        return {
            "active_session": self.active_session,
            "sources": dict(self.sources),
            "shell_output": self.shell_output,
            "shell_backend": self.shell_backend,
            "paused": list(self.paused),
            "api_url": self.api_url,
            "api_token": self.api_token,
            "updated_at": self.updated_at,
        }

    # ------------------------------------------------------------------ flags
    def flags(self) -> str:
        """Render enabled sources as the sentinel's compact flag string."""

        enabled = [
            SOURCE_FLAGS[name]
            for name in SOURCES
            if self.sources.get(name)
            and name in SOURCE_FLAGS
            and not (
                name == "shell" and self.shell_backend == SHELL_BACKEND_DEVSQL
            )
        ]
        return "".join(enabled) or "-"

    # ------------------------------------------------------------------ save
    def save(self) -> None:
        """Persist rich state and refresh the derived hook sentinel."""

        paths.ensure_home()
        self.updated_at = time.time()
        atomic_write_text(
            paths.state_path(), json.dumps(self.to_dict(), indent=2) + "\n"
        )
        self._write_sentinel()

    def _write_sentinel(self) -> None:
        """Rewrite (or remove) the one-line file that shell hooks poll."""

        active = paths.active_path()
        if not self.active_session:
            # Absence means "not recording". Removing the file is the entire
            # stop operation as far as every already-open shell is concerned.
            with contextlib.suppress(FileNotFoundError):
                active.unlink()
            return

        spool = paths.session_dir(self.active_session) / "spool"
        spool.mkdir(parents=True, exist_ok=True)
        line = f"{self.active_session}\t{self.flags()}\t{spool}\n"
        atomic_write_text(active, line)


def read_sentinel() -> tuple[str, str, Path] | None:
    """Parse the hook sentinel the same way the shell hooks do.

    Used by tests and by ``wfrec doctor`` to prove the shell-visible view of the
    world matches the rich state, rather than assuming it does.
    """

    try:
        raw = paths.active_path().read_text(encoding="utf-8").strip("\n")
    except (OSError, FileNotFoundError):
        return None
    parts = raw.split("\t")
    if len(parts) != 3:
        return None
    session_id, flags, spool = parts
    return session_id, flags, Path(spool)


def sentinel_enables(flags: str, source: str) -> bool:
    """Return whether ``source`` is enabled in a sentinel ``flags`` string."""

    flag = SOURCE_FLAGS.get(source)
    return bool(flag) and flag in flags


def publish_api(url: str, token: str) -> None:
    """Record the daemon's address so the CLI and agent skill can find it."""

    paths.ensure_home()
    atomic_write_text(paths.api_path(), f"{url}\t{token}\n")


def read_api() -> tuple[str, str] | None:
    """Return ``(url, token)`` for a running daemon, or ``None``."""

    try:
        raw = paths.api_path().read_text(encoding="utf-8").strip("\n")
    except (OSError, FileNotFoundError):
        return None
    parts = raw.split("\t")
    if len(parts) != 2:
        return None
    return parts[0], parts[1]


def clear_api() -> None:
    """Remove the published daemon address."""

    for path in (paths.api_path(), paths.boot_path()):
        with contextlib.suppress(FileNotFoundError):
            path.unlink()


def mark_boot() -> str:
    """Record a fresh daemon boot id and return it."""

    paths.ensure_home()
    boot = f"{os.getpid()}-{int(time.time())}"
    atomic_write_text(paths.boot_path(), boot + "\n")
    return boot


class StateTransaction:
    """Context manager for a locked read-modify-write cycle on the state.

    The CLI, GUI and daemon are separate processes that all toggle sources, so
    a plain read-then-write would lose updates when two arrive together.
    """

    def __init__(self) -> None:
        self._lock = None
        self.state: RecorderState | None = None

    def __enter__(self) -> RecorderState:
        paths.ensure_home()
        self._lock = file_lock(paths.state_lock_path())
        self._lock.__enter__()
        self.state = RecorderState.load()
        return self.state

    def __exit__(self, exc_type, exc, tb) -> None:
        try:
            if exc_type is None and self.state is not None:
                self.state.save()
        finally:
            if self._lock is not None:
                self._lock.__exit__(exc_type, exc, tb)
