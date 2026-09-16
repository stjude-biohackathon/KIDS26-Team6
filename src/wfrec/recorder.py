"""The Recorder: owns the active session and its collectors.

This is the object the control API, the CLI and the GUI all drive, so all three
surfaces share one implementation of what "pause" means. Collectors are started
and stopped individually, which is what makes a source toggleable at any point
mid-session rather than only at session start.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Any

from . import SOURCES
from .collectors.agents import AgentCollector
from .collectors.base import Collector, CollectorStatus
from .collectors.files import FileCollector
from .collectors.screen import ScreenCollector
from .collectors.shell import ShellCollector
from .session import STATUS_ACTIVE, Session, SessionStore
from .state import RecorderState

#: How often to emit a `session.waiting` heartbeat while paused.
WAITING_HEARTBEAT_SECONDS = 300.0


class Recorder:
    """Supervises collectors for whichever session is active."""

    def __init__(self, *, supervise: bool = True) -> None:
        #: When False, no background collectors are started. The CLI uses this
        #: for one-shot invocations, where any thread would die with the
        #: process a few milliseconds later -- and where an agent collector
        #: would briefly attach and dump a pile of unrelated transcript.
        self.supervise = supervise
        self.store = SessionStore()
        self._collectors: dict[str, Collector] = {}
        self._lock = threading.RLock()
        self._session: Session | None = None
        self._heartbeat: threading.Thread | None = None
        self._heartbeat_stop = threading.Event()
        self._pause_reason = ""
        self._paused_at = 0.0
        self._next_file_trigger = "session-start"

    # ------------------------------------------------------------------ state
    @property
    def session(self) -> Session | None:
        return self._session

    def attach(self, session: Session) -> None:
        """Adopt an already-active session, e.g. after a daemon restart."""

        with self._lock:
            self._session = session

    def statuses(self) -> dict[str, dict[str, Any]]:
        with self._lock:
            return {
                name: collector.status.to_dict()
                for name, collector in self._collectors.items()
            }

    def status(self) -> dict[str, Any]:
        """Everything a UI or the agent skill needs in one call."""

        state = RecorderState.load()
        with self._lock:
            session = self._session
            if session is None and state.active_session:
                # A fresh Recorder (the CLI builds one per invocation) has no
                # session attached yet, but there may well be an active one on
                # disk. Reporting None here made `wfrec status` claim nothing
                # was being recorded while a session was running.
                session = self.store.active()
                self._session = session
            payload: dict[str, Any] = {
                "active_session": state.active_session,
                "paused_sessions": list(state.paused),
                "sources": dict(state.sources),
                "shell_output": state.shell_output,
                "collectors": self.statuses(),
                "running": {
                    name: collector.running
                    for name, collector in self._collectors.items()
                },
            }
            if session is not None:
                payload["session"] = {
                    "id": session.session_id,
                    "title": session.manifest.title,
                    "analyst": session.manifest.analyst,
                    "status": session.manifest.status,
                    "root": str(session.root),
                    "watch_roots": list(session.manifest.watch_roots),
                    "events": _count_lines(session.writer.path),
                    "active_seconds": round(session.manifest.active_seconds, 1),
                    "paused_seconds": round(session.manifest.paused_seconds, 1),
                }
            if state.active_session:
                payload["pause_reason"] = self._pause_reason
        return payload

    # -------------------------------------------------------------- lifecycle
    def start_session(
        self,
        *,
        title: str = "",
        analyst: str = "unknown-analyst",
        workflow_family: str = "",
        tags: list[str] | None = None,
        watch: list[Path] | None = None,
        sources: dict[str, bool] | None = None,
    ) -> dict[str, Any]:
        """Create, activate and begin capturing a new session."""

        with self._lock:
            self._stop_collectors()
            session, preempted = self.store.start(
                title=title,
                analyst=analyst,
                workflow_family=workflow_family,
                tags=tags,
            )
            for root in watch or []:
                session.add_watch_root(root)
            if sources:
                for name, enabled in sources.items():
                    if name in SOURCES:
                        session.set_source(name, bool(enabled))
            self._session = session
            self._start_collectors()
            result = self.status()
            result["preempted"] = preempted.session_id if preempted else None
            return result

    def resume_session(self, session_id: str | None = None) -> dict[str, Any]:
        with self._lock:
            self._stop_collectors()
            session, preempted = self.store.resume(session_id)
            self._session = session
            self._stop_heartbeat()
            # The file collector's own start-up snapshot IS the gap
            # reconciliation: snapshotting at both ends of a pause is how file
            # edits made while capture was off are still recovered. Labelling
            # it "resume" avoids emitting two snapshots for one instant.
            self._next_file_trigger = "resume"
            self._start_collectors()
            self._next_file_trigger = "session-start"
            result = self.status()
            result["preempted"] = preempted.session_id if preempted else None
            return result

    def pause_session(
        self, session_id: str | None = None, *, reason: str = "", expect: str = ""
    ) -> dict[str, Any]:
        with self._lock:
            self._snapshot_files("pause")
            self._stop_collectors()
            session = self.store.pause(session_id, reason=reason, expect=expect)
            self._session = session
            self._pause_reason = reason
            self._paused_at = time.time()
            self._start_heartbeat(reason)
            return self.status()

    def stop_session(self, session_id: str | None = None) -> dict[str, Any]:
        with self._lock:
            self._stop_heartbeat()
            self._stop_collectors()
            session = self.store.stop(session_id)
            self._session = None
            status = self.status()
            status["stopped"] = session.session_id
            status["root"] = str(session.root)
            return status

    # ----------------------------------------------------------------- toggle
    def set_source(self, source: str, enabled: bool) -> dict[str, Any]:
        """Turn one capture source on or off, taking effect immediately."""

        if source not in SOURCES:
            raise ValueError(
                f"Unknown source '{source}'. Known: {', '.join(SOURCES)}"
            )
        with self._lock:
            session = self._require_session()
            session.set_source(source, enabled)
            if enabled and self.supervise:
                self._start_collector(source)
            else:
                collector = self._collectors.pop(source, None)
                if collector is not None:
                    collector.stop()
            return self.status()

    def set_shell_output(self, enabled: bool) -> dict[str, Any]:
        with self._lock:
            self._require_session().set_shell_output(enabled)
            return self.status()

    def add_note(
        self, text: str, *, label: str = "", pasted: bool = False, window: str = ""
    ) -> dict[str, Any]:
        with self._lock:
            session = self._require_session()
            event = session.add_note(text, label=label, pasted=pasted, window=window)
            return {"seq": event.seq, "redactions": event.redactions, "ref": event.ref}

    def add_marker(self, label: str, detail: str = "") -> dict[str, Any]:
        with self._lock:
            event = self._require_session().add_marker(label, detail)
            return {"seq": event.seq}

    def add_watch_root(self, root: Path) -> dict[str, Any]:
        """Declare a project root and restart the file collector to pick it up."""

        with self._lock:
            session = self._require_session()
            session.add_watch_root(root)
            collector = self._collectors.pop("files", None)
            if collector is not None:
                collector.stop()
            if session.manifest.sources.get("files"):
                self._start_collector("files")
            return self.status()

    # ------------------------------------------------------------- collectors
    def _require_session(self) -> Session:
        if self._session is None:
            active = self.store.active()
            if active is None:
                raise NoActiveSession()
            self._session = active
        return self._session

    def _build(self, source: str, session: Session) -> Collector | None:
        if source == "shell":
            return ShellCollector(session)
        if source == "files":
            roots = [Path(r) for r in session.manifest.watch_roots]
            return FileCollector(
                session, roots=roots, initial_trigger=self._next_file_trigger
            )
        if source == "screen":
            return ScreenCollector(session)
        if source == "agents":
            return AgentCollector(session)
        # `context` needs no background collector: notes arrive by explicit
        # user action through the API, never by polling.
        return None

    def _start_collector(self, source: str) -> CollectorStatus | None:
        session = self._require_session()
        if source in self._collectors:
            return self._collectors[source].status
        collector = self._build(source, session)
        if collector is None:
            return None
        status = collector.start()
        self._collectors[source] = collector
        return status

    def _start_collectors(self) -> None:
        if not self.supervise:
            return
        session = self._require_session()
        if session.manifest.status != STATUS_ACTIVE:
            return
        for source in SOURCES:
            if session.manifest.sources.get(source):
                self._start_collector(source)

    def _stop_collectors(self) -> None:
        for source, collector in list(self._collectors.items()):
            try:
                collector.stop()
            except Exception:  # pragma: no cover - defensive
                pass
            self._collectors.pop(source, None)

    def _snapshot_files(self, trigger: str) -> None:
        collector = self._collectors.get("files")
        if isinstance(collector, FileCollector):
            try:
                collector.snapshot_all(trigger)
            except Exception:  # pragma: no cover - defensive
                pass

    # ------------------------------------------------------------- heartbeat
    def _start_heartbeat(self, reason: str) -> None:
        """Emit `session.waiting` ticks while paused.

        Without this, a six-hour wait on an alignment job is indistinguishable
        from the analyst going home. The wait is itself workflow signal.
        """

        self._stop_heartbeat()
        session = self._session
        if session is None:
            return
        self._heartbeat_stop.clear()

        def beat() -> None:
            started = time.time()
            while not self._heartbeat_stop.wait(WAITING_HEARTBEAT_SECONDS):
                try:
                    session.mark_waiting(
                        reason=reason,
                        elapsed_ms=int((time.time() - started) * 1000),
                    )
                except Exception:  # pragma: no cover - defensive
                    return

        self._heartbeat = threading.Thread(
            target=beat, name="wfrec-waiting", daemon=True
        )
        self._heartbeat.start()

    def _stop_heartbeat(self) -> None:
        self._heartbeat_stop.set()
        thread, self._heartbeat = self._heartbeat, None
        if thread is not None and thread.is_alive():
            thread.join(timeout=2)

    def shutdown(self) -> None:
        with self._lock:
            self._stop_heartbeat()
            self._stop_collectors()


class NoActiveSession(RuntimeError):
    """Raised when an operation needs an active session and there is none."""

    def __init__(self) -> None:
        super().__init__("No active session. Run `wfrec start` first.")


def _count_lines(path: Path) -> int:
    try:
        with path.open("rb") as handle:
            return sum(1 for _ in handle)
    except (OSError, FileNotFoundError):
        return 0
