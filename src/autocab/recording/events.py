"""Store the append-only event timeline for each session.

``seq`` records write order and ``ts`` records event time. Delayed ingestion
can place an older timestamp after a newer event. Consumers use
``read_events_sorted``, which sorts by ``(ts, seq)``.
"""

from __future__ import annotations

import json
import os
import platform
import socket
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .locking import file_lock

# --------------------------------------------------------------------- types

# Lifecycle
SESSION_CREATED = "session.created"
SESSION_STARTED = "session.started"
SESSION_PAUSED = "session.paused"
SESSION_RESUMED = "session.resumed"
SESSION_STOPPED = "session.stopped"
SESSION_PREEMPTED = "session.preempted"
SESSION_WAITING = "session.waiting"

# Toggles
SOURCE_ENABLED = "source.enabled"
SOURCE_DISABLED = "source.disabled"

# Capture
SHELL_COMMAND = "shell.command.completed"
SCREEN_FRAME = "screen.frame"
SCREEN_OCR = "screen.ocr"
SCREEN_WINDOW = "screen.window"
SCREEN_RECORDING_STARTED = "screen.recording.started"
SCREEN_RECORDING_STOPPED = "screen.recording.stopped"
CONTEXT_NOTE = "context.note"
AGENT_MESSAGE = "agent.message"
AGENT_ADAPTER_FAILED = "agent.adapter.failed"
FILE_CHANGED = "file.changed"
FILE_DIFF = "file.diff"
FILE_FLOOD = "file.flood"
GIT_SNAPSHOT = "git.snapshot"
JOB_SUBMITTED = "job.submitted"
JOB_COMPLETED = "job.completed"
MARKER_USER = "marker.user"

# De-identification
DEID_SEALED = "deid.sealed"
"""Appended as the **last line of the staged ``events.jsonl``, before hashing**,
so the timeline self-documents that it was sealed and the seal record's
``after_sha256`` covers the statement. ``exporters/trace.py`` skips it."""


class SessionSealed(RuntimeError):
    """A mutation was attempted on a sealed session.

    This closes both timeline-append and metadata-mutation holes. Without it a
    *stopped* session could change after being scrubbed, while still carrying a
    seal that described its earlier contents.
    """


def utc_now() -> str:
    """Timestamp as UTC ISO-8601 with milliseconds, e.g. ``2026-09-16T14:22:03.418Z``."""

    # Sample the clock exactly once: reading it twice can straddle a second
    # boundary and emit milliseconds that belong to a different second.
    now = datetime.now(timezone.utc)
    return now.strftime("%Y-%m-%dT%H:%M:%S.") + f"{now.microsecond // 1000:03d}Z"


def iso_to_seconds(ts: str) -> str:
    """Truncate an event timestamp to whole seconds without a timezone suffix.

    ``autocab.terminal_logs.TIMESTAMP_PATTERN`` accepts *only*
    ``YYYY-MM-DDTHH:MM:SS`` and raises on anything else, so the exporter has to
    strip both the milliseconds and the trailing ``Z``.
    """

    return ts.replace("Z", "").split(".")[0]


@dataclass(slots=True)
class Event:
    """One observation on the session timeline."""

    source: str
    type: str
    payload: dict[str, Any] = field(default_factory=dict)
    ts: str = field(default_factory=utc_now)
    session: str | None = None
    host: str | None = None
    analyst: str | None = None
    origin: str = "local"
    seq: int | None = None
    redactions: list[str] = field(default_factory=list)
    ref: str | None = None

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "seq": self.seq,
            "ts": self.ts,
            "session": self.session,
            "host": self.host,
            "analyst": self.analyst,
            "origin": self.origin,
            "source": self.source,
            "type": self.type,
            "payload": self.payload,
        }
        if self.redactions:
            payload["redactions"] = self.redactions
        if self.ref:
            payload["ref"] = self.ref
        return payload

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "Event":
        return cls(
            source=payload["source"],
            type=payload["type"],
            payload=dict(payload.get("payload") or {}),
            ts=payload["ts"],
            session=payload.get("session"),
            host=payload.get("host"),
            analyst=payload.get("analyst"),
            origin=payload.get("origin", "local"),
            seq=payload.get("seq"),
            redactions=list(payload.get("redactions") or []),
            ref=payload.get("ref"),
        )


def host_name() -> str:
    """Short hostname, used to attribute events across local and remote hosts."""

    try:
        return socket.gethostname().split(".")[0]
    except OSError:  # pragma: no cover
        return "unknown-host"


def platform_summary() -> dict[str, str]:
    """Describe the recording machine, for the session manifest."""

    return {
        "system": platform.system(),
        "release": platform.release(),
        "machine": platform.machine(),
        "python": platform.python_version(),
        "session_type": os.environ.get("XDG_SESSION_TYPE", ""),
        "display_server": _display_server(),
    }


def _display_server() -> str:
    """Best-effort display server detection, which gates screen capture."""

    if os.environ.get("WAYLAND_DISPLAY") and not os.environ.get("DISPLAY"):
        return "wayland"
    if os.environ.get("DISPLAY"):
        return "x11"
    if platform.system() in {"Darwin", "Windows"}:
        return "native"
    return "headless"


class EventWriter:
    """Append-only writer for one session's ``events.jsonl``."""

    def __init__(
        self,
        session_dir: Path,
        *,
        session_id: str,
        analyst: str | None = None,
        host: str | None = None,
        allow_sealed: bool = False,
    ) -> None:
        self._dir = session_dir
        self._path = session_dir / "events.jsonl"
        self._seq_path = session_dir / ".seq"
        self._lock_path = session_dir / ".events.lock"
        self._session_id = session_id
        self._analyst = analyst
        self._host = host or host_name()
        self._allow_sealed = allow_sealed

        # An interrupted seal is finished or discarded here, before anything
        # reads or writes the timeline. Gated on one `stat` of the journal so
        # the common case -- no seal in flight -- costs a single syscall per
        # Session construction.
        if (session_dir / ".seal" / "journal.json").exists():
            from .seal import recover

            recover(session_dir)

    @property
    def sealed(self) -> bool:
        return (self._dir / "seal.json").exists()

    def _refuse_if_sealed_locked(self) -> None:
        if self.sealed and not self._allow_sealed:
            raise SessionSealed(
                f"session {self._session_id} is sealed ({self._dir / 'seal.json'}); "
                "appending to it would add unredacted text underneath a seal that "
                "says the timeline was scrubbed. Use `wfrec seal --reseal` if the "
                "session genuinely needs to change."
            )

    @property
    def path(self) -> Path:
        return self._path

    def append(self, event: Event) -> Event:
        """Assign a sequence number and append one event atomically."""

        event.session = event.session or self._session_id
        event.host = event.host or self._host
        event.analyst = event.analyst or self._analyst

        self._dir.mkdir(parents=True, exist_ok=True)
        with file_lock(self._lock_path):
            self._refuse_if_sealed_locked()
            event.seq = self._next_seq_locked()
            line = json.dumps(event.to_dict(), ensure_ascii=False, default=str)
            with self._path.open("a", encoding="utf-8") as handle:
                handle.write(line + "\n")
                handle.flush()
                os.fsync(handle.fileno())
        return event

    def extend(self, events: list[Event]) -> list[Event]:
        """Append many events under a single lock acquisition."""

        if not events:
            return []
        self._dir.mkdir(parents=True, exist_ok=True)
        with file_lock(self._lock_path):
            self._refuse_if_sealed_locked()
            seq = self._next_seq_locked(count=len(events))
            with self._path.open("a", encoding="utf-8") as handle:
                for offset, event in enumerate(events):
                    event.session = event.session or self._session_id
                    event.host = event.host or self._host
                    event.analyst = event.analyst or self._analyst
                    event.seq = seq + offset
                    handle.write(
                        json.dumps(event.to_dict(), ensure_ascii=False, default=str) + "\n"
                    )
                handle.flush()
                os.fsync(handle.fileno())
        return events

    def _next_seq_locked(self, count: int = 1) -> int:
        """Reserve ``count`` sequence numbers. Caller must hold the lock."""

        current = 0
        try:
            current = int(self._seq_path.read_text(encoding="utf-8").strip() or 0)
        except (OSError, ValueError):
            current = 0
        self._seq_path.write_text(str(current + count), encoding="utf-8")
        return current + 1

    # ------------------------------------------------------------------ read
    def read(self) -> list[Event]:
        """Read the whole timeline in file (``seq``) order.

        Malformed lines are skipped rather than fatal: a torn final line is
        expected when reading while a writer is mid-append.
        """

        return list(read_events(self._path))

    def read_sorted(self) -> list[Event]:
        """Read the whole timeline in chronological (``ts``) order."""

        return read_events_sorted(self._path)


def sort_key(event: "Event") -> tuple[str, int]:
    """Chronological ordering key: when it happened, then when it was recorded."""

    return (event.ts, event.seq or 0)


def read_events_sorted(path: Path) -> list[Event]:
    """Read a timeline in chronological order.

    The file is append-ordered by ``seq``, which is not the same as ``ts`` --
    see the module docstring. Anything producing an ordered narrative (every
    exporter) needs this rather than raw file order.
    """

    return sorted(read_events(path), key=sort_key)


def read_events(path: Path) -> list[Event]:
    """Parse an ``events.jsonl`` file, tolerating partial trailing writes."""

    events: list[Event] = []
    try:
        handle = path.open("r", encoding="utf-8")
    except (OSError, FileNotFoundError):
        return events
    with handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                events.append(Event.from_dict(json.loads(line)))
            except (json.JSONDecodeError, KeyError):
                # A torn final line is expected if we read while a writer is
                # mid-append; dropping it is correct and self-healing.
                continue
    return events
