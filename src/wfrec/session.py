"""Session lifecycle: create, start, pause, resume, stop, preempt.

The rule that makes everything else simple: **exactly one session is active at
a time, and any number may be paused.** Starting or resuming session B while A
is active auto-pauses A and records ``session.preempted`` on A's timeline.

That constraint is deliberate rather than a limitation. If two sessions could
record simultaneously there would be no correct answer to "which session owns
this shell command?" -- the shell hook sees one sentinel and one spool
directory, and an analyst typing in a terminal has no way to express which
session they meant.
"""

from __future__ import annotations

import json
import random
import string
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import SOURCES, paths
from .events import (
    Event,
    EventWriter,
    SESSION_CREATED,
    SESSION_PAUSED,
    SESSION_PREEMPTED,
    SESSION_RESUMED,
    SESSION_STARTED,
    SESSION_STOPPED,
    SESSION_WAITING,
    SOURCE_DISABLED,
    SOURCE_ENABLED,
    CONTEXT_NOTE,
    MARKER_USER,
    host_name,
    platform_summary,
    utc_now,
)
from .locking import atomic_write_text
from .redaction import shared as shared_redactor
from .state import SHELL_BACKENDS, SHELL_BACKEND_SPOOL, StateTransaction

STATUS_CREATED = "created"
STATUS_ACTIVE = "active"
STATUS_PAUSED = "paused"
STATUS_STOPPED = "stopped"

_ID_ALPHABET = string.ascii_lowercase + string.digits


def new_session_id() -> str:
    """Sortable, collision-resistant session id, e.g. ``2026-09-16T14-03-22_a3f9c1``.

    Lexical sort equals chronological sort, which makes ``ls`` in the sessions
    directory immediately useful and lets ``--last`` be a cheap ``max()``.
    """

    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H-%M-%S")
    suffix = "".join(random.choices(_ID_ALPHABET, k=6))
    return f"{stamp}_{suffix}"


@dataclass(slots=True)
class Manifest:
    """Session metadata, persisted as ``manifest.json``."""

    session_id: str
    title: str = ""
    analyst: str = "unknown-analyst"
    workflow_family: str = ""
    tags: list[str] = field(default_factory=list)
    host: str = field(default_factory=host_name)
    platform: dict[str, str] = field(default_factory=platform_summary)
    status: str = STATUS_CREATED
    created_at: str = field(default_factory=utc_now)
    updated_at: str = field(default_factory=utc_now)
    sources: dict[str, bool] = field(
        default_factory=lambda: {name: True for name in SOURCES}
    )
    shell_output: bool = False
    shell_backend: str = SHELL_BACKEND_SPOOL
    watch_roots: list[str] = field(default_factory=list)
    remote_hosts: list[str] = field(default_factory=list)
    lifecycle: list[dict[str, Any]] = field(default_factory=list)
    toggles: list[dict[str, Any]] = field(default_factory=list)
    active_seconds: float = 0.0
    paused_seconds: float = 0.0
    _last_transition: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "title": self.title,
            "analyst": self.analyst,
            "workflow_family": self.workflow_family,
            "tags": list(self.tags),
            "host": self.host,
            "platform": dict(self.platform),
            "status": self.status,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "sources": dict(self.sources),
            "shell_output": self.shell_output,
            "shell_backend": self.shell_backend,
            "watch_roots": list(self.watch_roots),
            "remote_hosts": list(self.remote_hosts),
            "lifecycle": list(self.lifecycle),
            "toggles": list(self.toggles),
            "active_seconds": round(self.active_seconds, 3),
            "paused_seconds": round(self.paused_seconds, 3),
            "last_transition": self._last_transition,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "Manifest":
        sources = {name: True for name in SOURCES}
        for name, enabled in (payload.get("sources") or {}).items():
            if name in sources:
                sources[name] = bool(enabled)
        shell_backend = payload.get("shell_backend", SHELL_BACKEND_SPOOL)
        if shell_backend not in SHELL_BACKENDS:
            shell_backend = SHELL_BACKEND_SPOOL
        manifest = cls(
            session_id=payload["session_id"],
            title=payload.get("title", ""),
            analyst=payload.get("analyst", "unknown-analyst"),
            workflow_family=payload.get("workflow_family", ""),
            tags=list(payload.get("tags") or []),
            host=payload.get("host") or host_name(),
            platform=dict(payload.get("platform") or {}),
            status=payload.get("status", STATUS_CREATED),
            created_at=payload.get("created_at") or utc_now(),
            updated_at=payload.get("updated_at") or utc_now(),
            sources=sources,
            shell_output=bool(payload.get("shell_output", False)),
            shell_backend=shell_backend,
            watch_roots=list(payload.get("watch_roots") or []),
            remote_hosts=list(payload.get("remote_hosts") or []),
            lifecycle=list(payload.get("lifecycle") or []),
            toggles=list(payload.get("toggles") or []),
            active_seconds=float(payload.get("active_seconds") or 0.0),
            paused_seconds=float(payload.get("paused_seconds") or 0.0),
        )
        manifest._last_transition = float(payload.get("last_transition") or 0.0)
        return manifest


class Session:
    """One recorded session: its folder, manifest and timeline."""

    def __init__(self, manifest: Manifest) -> None:
        self.manifest = manifest
        self.root = paths.session_dir(manifest.session_id)
        self.writer = EventWriter(
            self.root,
            session_id=manifest.session_id,
            analyst=manifest.analyst,
            host=manifest.host,
        )

    # ------------------------------------------------------------- persistence
    @property
    def session_id(self) -> str:
        return self.manifest.session_id

    @property
    def spool_dir(self) -> Path:
        return self.root / "spool"

    def save(self) -> None:
        self.manifest.updated_at = utc_now()
        atomic_write_text(
            self.root / "manifest.json",
            json.dumps(self.manifest.to_dict(), indent=2) + "\n",
        )

    @classmethod
    def load(cls, session_id: str) -> "Session":
        path = paths.session_dir(session_id) / "manifest.json"
        if not path.exists():
            raise SessionNotFound(session_id)
        manifest = Manifest.from_dict(json.loads(path.read_text(encoding="utf-8")))
        return cls(manifest)

    @classmethod
    def create(
        cls,
        *,
        title: str = "",
        analyst: str = "unknown-analyst",
        workflow_family: str = "",
        tags: list[str] | None = None,
        sources: dict[str, bool] | None = None,
        shell_backend: str = SHELL_BACKEND_SPOOL,
    ) -> "Session":
        """Create a new session folder, manifest and timeline."""

        paths.ensure_home()
        session_id = new_session_id()
        paths.ensure_session_tree(session_id)
        manifest = Manifest(
            session_id=session_id,
            title=title or f"Session {session_id}",
            analyst=analyst,
            workflow_family=workflow_family,
            tags=list(tags or []),
            shell_backend=shell_backend,
        )
        if sources:
            for name, enabled in sources.items():
                if name in manifest.sources:
                    manifest.sources[name] = bool(enabled)
        session = cls(manifest)
        session.save()
        session.record(
            SESSION_CREATED,
            source="session",
            payload={
                "title": manifest.title,
                "analyst": manifest.analyst,
                "platform": manifest.platform,
                "sources": dict(manifest.sources),
                "shell_backend": manifest.shell_backend,
            },
        )
        return session

    # ------------------------------------------------------------------ events
    def record(
        self,
        event_type: str,
        *,
        source: str = "session",
        payload: dict[str, Any] | None = None,
        origin: str = "local",
        redactions: list[str] | None = None,
        ref: str | None = None,
        ts: str | None = None,
    ) -> Event:
        """Append one event to this session's timeline."""

        event = Event(
            source=source,
            type=event_type,
            payload=payload or {},
            origin=origin,
            redactions=redactions or [],
            ref=ref,
        )
        if ts:
            event.ts = ts
        return self.writer.append(event)

    def _log_lifecycle(self, action: str, detail: dict[str, Any]) -> None:
        self.manifest.lifecycle.append(
            {"action": action, "at": utc_now(), **detail}
        )

    def _accrue_time(self) -> None:
        """Attribute elapsed wall clock to active or paused, then reset the mark.

        Tracked because "how long was the analyst actually working versus
        waiting on a job" is a question the downstream translation skill will
        want to answer, and it cannot be recovered from timestamps alone once
        pause gaps are involved.
        """

        now = time.time()
        if self.manifest._last_transition:
            elapsed = max(0.0, now - self.manifest._last_transition)
            if self.manifest.status == STATUS_ACTIVE:
                self.manifest.active_seconds += elapsed
            elif self.manifest.status == STATUS_PAUSED:
                self.manifest.paused_seconds += elapsed
        self.manifest._last_transition = now

    # --------------------------------------------------------------- lifecycle
    def mark_started(self, *, resumed: bool = False, gap_ms: int | None = None) -> Event:
        self._accrue_time()
        self.manifest.status = STATUS_ACTIVE
        if resumed:
            self._log_lifecycle("resumed", {"gap_ms": gap_ms})
            event = self.record(
                SESSION_RESUMED,
                payload={
                    "gap_ms": gap_ms,
                    "shell_backend": self.manifest.shell_backend,
                },
            )
        else:
            self._log_lifecycle("started", {})
            event = self.record(
                SESSION_STARTED,
                payload={
                    "sources": dict(self.manifest.sources),
                    "shell_backend": self.manifest.shell_backend,
                },
            )
        self.save()
        return event

    def mark_paused(
        self,
        *,
        reason: str = "",
        expect: str = "",
        preempted_by: str | None = None,
    ) -> Event:
        self._accrue_time()
        self.manifest.status = STATUS_PAUSED
        if preempted_by:
            self._log_lifecycle("preempted", {"by": preempted_by})
            event = self.record(
                SESSION_PREEMPTED,
                payload={"preempted_by": preempted_by, "reason": reason},
            )
        else:
            self._log_lifecycle("paused", {"reason": reason, "expect": expect})
            event = self.record(
                SESSION_PAUSED, payload={"reason": reason, "expect": expect}
            )
        self.save()
        return event

    def mark_stopped(self) -> Event:
        self._accrue_time()
        self.manifest.status = STATUS_STOPPED
        self._log_lifecycle("stopped", {})
        event = self.record(
            SESSION_STOPPED,
            payload={
                "active_seconds": round(self.manifest.active_seconds, 3),
                "paused_seconds": round(self.manifest.paused_seconds, 3),
            },
        )
        self.save()
        return event

    def mark_waiting(self, *, reason: str = "", elapsed_ms: int = 0) -> Event:
        """Heartbeat emitted while paused.

        Without this a six-hour wait on an alignment job is indistinguishable
        from the analyst walking away, and the wait is itself workflow signal.
        """

        return self.record(
            SESSION_WAITING, payload={"reason": reason, "elapsed_ms": elapsed_ms}
        )

    # ----------------------------------------------------------------- toggles
    def set_source(self, source: str, enabled: bool) -> Event:
        if source not in self.manifest.sources:
            raise ValueError(
                f"Unknown capture source '{source}'. Known: {', '.join(sorted(self.manifest.sources))}"
            )
        self.manifest.sources[source] = enabled
        self.manifest.toggles.append(
            {"source": source, "enabled": enabled, "at": utc_now()}
        )
        event = self.record(
            SOURCE_ENABLED if enabled else SOURCE_DISABLED,
            source=source,
            payload={"source": source, "enabled": enabled},
        )
        self.save()
        self._sync_state()
        return event

    def set_shell_output(self, enabled: bool) -> Event:
        self.manifest.shell_output = enabled
        self.manifest.toggles.append(
            {"source": "shell_output", "enabled": enabled, "at": utc_now()}
        )
        event = self.record(
            SOURCE_ENABLED if enabled else SOURCE_DISABLED,
            source="shell",
            payload={"source": "shell_output", "enabled": enabled},
        )
        self.save()
        self._sync_state()
        return event

    def set_shell_backend(self, backend: str) -> None:
        """Persist the backend selected for the next active interval."""

        if backend not in SHELL_BACKENDS:
            raise ValueError(f"Unknown shell backend: {backend}")
        self.manifest.shell_backend = backend
        self.save()
        self._sync_state()

    def _sync_state(self) -> None:
        """Push this session's toggles into the shared state and sentinel.

        The manifest is the session's own record, but shell hooks only ever see
        the sentinel, which is derived from the shared state. Updating one
        without the other is how a "source off" toggle appears to work in the
        UI while already-open terminals keep right on recording.
        """

        with StateTransaction() as state:
            if state.active_session != self.session_id:
                # A toggle on a non-active session is recorded in its manifest
                # for when it resumes, but must not touch the live sentinel.
                return
            state.sources = dict(self.manifest.sources)
            state.shell_output = self.manifest.shell_output
            state.shell_backend = self.manifest.shell_backend

    # -------------------------------------------------------------- user input
    def add_note(
        self, text: str, *, label: str = "", pasted: bool = False, window: str = ""
    ) -> Event:
        """Record a pasted-context block or analyst note.

        Redacted on ingest: the dump-your-notes box is the single most likely
        place an identifier lands, and ``data/sample_screen_capture.json`` in
        this repo literally carries "MRN 123456 accidentally pasted into
        scratch note" as a test case.
        """

        result = shared_redactor().apply(text)
        relative = f"context/{utc_now().replace(':', '-')}.md"
        target = self.root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(result.text, encoding="utf-8")
        return self.record(
            CONTEXT_NOTE,
            source="context",
            payload={
                "label": label,
                "pasted": pasted,
                "window_title": window,
                "chars": len(text),
                "text": result.text,
                "redaction_available": result.available,
            },
            redactions=result.findings,
            ref=relative,
        )

    def add_marker(self, label: str, detail: str = "") -> Event:
        return self.record(
            MARKER_USER,
            source="context",
            payload={"label": label, "detail": detail},
        )

    def add_watch_root(self, root: Path) -> None:
        resolved = str(Path(root).expanduser().resolve())
        if resolved not in self.manifest.watch_roots:
            self.manifest.watch_roots.append(resolved)
            self.save()


class SessionNotFound(LookupError):
    """Raised when a session id has no manifest on disk."""

    def __init__(self, session_id: str) -> None:
        super().__init__(f"No such session: {session_id}")
        self.session_id = session_id


class SessionStore:
    """Discovery and the active/paused invariant across sessions."""

    def list_ids(self) -> list[str]:
        root = paths.sessions_dir()
        if not root.is_dir():
            return []
        return sorted(
            path.name
            for path in root.iterdir()
            if path.is_dir() and (path / "manifest.json").exists()
        )

    def list_sessions(self) -> list[Session]:
        sessions = []
        for session_id in self.list_ids():
            try:
                sessions.append(Session.load(session_id))
            except (SessionNotFound, json.JSONDecodeError, KeyError):
                continue
        return sessions

    def last_id(self) -> str | None:
        ids = self.list_ids()
        return ids[-1] if ids else None

    def resolve(self, session_id: str | None) -> Session:
        """Resolve an explicit id, or fall back to active-then-most-recent."""

        if session_id:
            return Session.load(session_id)
        from .state import RecorderState

        state = RecorderState.load()
        if state.active_session:
            return Session.load(state.active_session)
        last = self.last_id()
        if not last:
            raise SessionNotFound("<none>")
        return Session.load(last)

    # ------------------------------------------------------------- transitions
    def start(
        self,
        *,
        title: str = "",
        analyst: str = "unknown-analyst",
        workflow_family: str = "",
        tags: list[str] | None = None,
        shell_backend: str = SHELL_BACKEND_SPOOL,
    ) -> tuple[Session, Session | None]:
        """Create and activate a session, preempting any currently active one."""

        session = Session.create(
            title=title,
            analyst=analyst,
            workflow_family=workflow_family,
            tags=tags,
            shell_backend=shell_backend,
        )
        preempted = self._activate(session, resumed=False)
        return session, preempted

    def resume(
        self,
        session_id: str | None = None,
        *,
        shell_backend: str | None = None,
    ) -> tuple[Session, Session | None]:
        """Reactivate a paused session, preempting any currently active one."""

        session = self.resolve(session_id)
        if session.manifest.status == STATUS_STOPPED:
            raise ValueError(
                f"Session {session.session_id} is stopped and cannot be resumed."
            )
        if shell_backend is not None:
            session.set_shell_backend(shell_backend)
        gap_ms = None
        if session.manifest._last_transition:
            gap_ms = int((time.time() - session.manifest._last_transition) * 1000)
        preempted = self._activate(session, resumed=True, gap_ms=gap_ms)
        return session, preempted

    def _activate(
        self, session: Session, *, resumed: bool, gap_ms: int | None = None
    ) -> Session | None:
        """Make ``session`` the single active session."""

        preempted: Session | None = None
        with StateTransaction() as state:
            current = state.active_session
            if current and current != session.session_id:
                try:
                    other = Session.load(current)
                except (SessionNotFound, json.JSONDecodeError):
                    other = None
                if other is not None and other.manifest.status == STATUS_ACTIVE:
                    other.mark_paused(preempted_by=session.session_id)
                    preempted = other
                    if current not in state.paused:
                        state.paused.append(current)

            state.active_session = session.session_id
            state.sources = dict(session.manifest.sources)
            state.shell_output = session.manifest.shell_output
            state.shell_backend = session.manifest.shell_backend
            if session.session_id in state.paused:
                state.paused.remove(session.session_id)

        session.mark_started(resumed=resumed, gap_ms=gap_ms)
        if preempted is not None:
            # Symmetry: the incoming session's timeline should also show that it
            # displaced another, so either timeline alone explains the handover.
            session.record(
                SESSION_PREEMPTED,
                payload={"preempted": preempted.session_id, "direction": "displaced"},
            )
        return preempted

    def pause(
        self, session_id: str | None = None, *, reason: str = "", expect: str = ""
    ) -> Session:
        """Pause the active session and stop all capture."""

        session = self.resolve(session_id)
        with StateTransaction() as state:
            if state.active_session == session.session_id:
                state.active_session = None
            if session.session_id not in state.paused:
                state.paused.append(session.session_id)
        session.mark_paused(reason=reason, expect=expect)
        return session

    def stop(self, session_id: str | None = None) -> Session:
        """Stop a session permanently."""

        session = self.resolve(session_id)
        with StateTransaction() as state:
            if state.active_session == session.session_id:
                state.active_session = None
            if session.session_id in state.paused:
                state.paused.remove(session.session_id)
        session.mark_stopped()
        return session

    def active(self) -> Session | None:
        from .state import RecorderState

        state = RecorderState.load()
        if not state.active_session:
            return None
        try:
            return Session.load(state.active_session)
        except (SessionNotFound, json.JSONDecodeError):
            return None
