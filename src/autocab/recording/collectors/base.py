"""Collector base class and probe result types."""

from __future__ import annotations

import contextlib
import threading
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from ..events import SOURCE_DISABLED, Event

if TYPE_CHECKING:  # pragma: no cover
    from ..session import Session


@dataclass(slots=True)
class Degraded(Exception):
    """Raised by ``probe()`` when a source cannot run as intended.

    Carrying a machine-readable ``reason`` plus a human ``detail`` means the
    timeline, ``wfrec doctor`` and the GUI can all explain the same failure in
    their own idiom without re-deriving it.
    """

    reason: str
    detail: str = ""
    fallback: str | None = None

    def __str__(self) -> str:  # pragma: no cover - cosmetic
        return f"{self.reason}: {self.detail}" if self.detail else self.reason


@dataclass(slots=True)
class CollectorStatus:
    """What a collector resolved to on this machine."""

    source: str
    available: bool
    backend: str = ""
    reason: str = ""
    detail: str = ""
    extra: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "available": self.available,
            "backend": self.backend,
            "reason": self.reason,
            "detail": self.detail,
            **({"extra": self.extra} if self.extra else {}),
        }


class Collector:
    """Base class for a capture source.

    Subclasses implement ``probe``, ``_run_once`` (for polled collectors) or
    override ``start``/``stop`` entirely for event-driven ones.
    """

    #: Source name; must be one of ``autocab.recording.SOURCES``.
    source = "base"
    #: Seconds between ``_run_once`` calls for polled collectors.
    interval = 2.0

    def __init__(self, session: "Session") -> None:
        self.session = session
        self.status = CollectorStatus(source=self.source, available=False)
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()

    # ------------------------------------------------------------------ probe
    def probe(self) -> CollectorStatus:
        """Resolve a backend for this machine. Override in subclasses."""

        self.status = CollectorStatus(source=self.source, available=True, backend="noop")
        return self.status

    def safe_probe(self) -> CollectorStatus:
        """Probe without ever raising, recording degradation on the timeline."""

        try:
            self.status = self.probe()
        except Degraded as degraded:
            self.status = CollectorStatus(
                source=self.source,
                available=False,
                backend=degraded.fallback or "",
                reason=degraded.reason,
                detail=degraded.detail,
            )
        except Exception as exc:  # pragma: no cover - defensive
            self.status = CollectorStatus(
                source=self.source,
                available=False,
                reason="probe-failed",
                detail=f"{type(exc).__name__}: {exc}",
            )
        return self.status

    # ------------------------------------------------------------- lifecycle
    def start(self) -> CollectorStatus:
        """Probe, then run the collector in a background thread if available."""

        self.safe_probe()
        if not self.status.available:
            self.session.record(
                SOURCE_DISABLED,
                source=self.source,
                payload={
                    "source": self.source,
                    "enabled": False,
                    "reason": self.status.reason,
                    "detail": self.status.detail,
                    "fallback": self.status.backend,
                },
            )
            return self.status

        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name=f"wfrec-{self.source}", daemon=True)
        self._thread.start()
        return self.status

    def stop(self, timeout: float = 5.0) -> None:
        """Signal the loop to finish and wait briefly for it."""

        self._stop.set()
        thread, self._thread = self._thread, None
        if thread is not None and thread.is_alive():
            thread.join(timeout=timeout)
        with contextlib.suppress(Exception):  # pragma: no cover - defensive
            self.teardown()

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def teardown(self) -> None:
        """Release backend resources. Override as needed."""

    # ------------------------------------------------------------------- loop
    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self._run_once()
            except Exception as exc:  # pragma: no cover - defensive
                # One collector throwing must never take down the recorder, and
                # the failure has to be visible rather than swallowed.
                self.session.record(
                    "collector.error",
                    source=self.source,
                    payload={"error": f"{type(exc).__name__}: {exc}"},
                )
            self._stop.wait(self.interval)
        # A final pass so anything produced between the last tick and the stop
        # signal still lands on the timeline.
        with contextlib.suppress(Exception):  # pragma: no cover - defensive
            self._run_once()

    def _run_once(self) -> None:
        """One unit of collection work. Override in polled subclasses."""

    # ------------------------------------------------------------------ utils
    def emit(self, events: list[Event]) -> None:
        """Append a batch of events under a single lock acquisition."""

        if events:
            self.session.writer.extend(events)
