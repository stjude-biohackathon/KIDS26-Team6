"""Shell collector: drains hook spool files into the timeline.

The hooks do the capture; this collector only converts. That split is what
keeps a dead daemon from ever hanging a terminal, and it means shell commands
typed while the daemon was down are still picked up on the next start.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from .. import spool
from ..events import (
    Event,
    JOB_SUBMITTED,
    MARKER_USER,
    SHELL_COMMAND,
    AGENT_MESSAGE,
)
from ..redaction import shared as shared_redactor
from .base import Collector, CollectorStatus


def epoch_ms_to_iso(value: str | int | None) -> str | None:
    """Convert hook-emitted epoch milliseconds into the timeline's ISO format."""

    if value in (None, ""):
        return None
    try:
        ms = int(value)
    except (TypeError, ValueError):
        return None
    if ms <= 0:
        return None
    moment = datetime.fromtimestamp(ms / 1000, tz=timezone.utc)
    return moment.strftime("%Y-%m-%dT%H:%M:%S.") + f"{moment.microsecond // 1000:03d}Z"


class ShellCollector(Collector):
    """Ingests ``<session>/spool/*.rec`` and any pulled-back remote spools."""

    source = "shell"
    interval = 1.0

    def __init__(self, session, extra_spools: dict[Path, str] | None = None) -> None:
        super().__init__(session)
        self._readers: dict[Path, spool.SpoolReader] = {}
        self._origins: dict[Path, str] = {session.spool_dir: "local"}
        if extra_spools:
            self._origins.update(extra_spools)
        # Idempotency keys already on the timeline, so a daemon restart that
        # re-reads a spool file from offset zero cannot duplicate commands.
        self._seen: set[tuple[str, str, str]] = set()
        self._load_seen()

    def probe(self) -> CollectorStatus:
        self.session.spool_dir.mkdir(parents=True, exist_ok=True)
        return CollectorStatus(
            source=self.source,
            available=True,
            backend="spool",
            extra={"spool_dir": str(self.session.spool_dir)},
        )

    def _load_seen(self) -> None:
        """Rebuild the dedupe set from the existing timeline."""

        for event in self.session.writer.read():
            if event.type != SHELL_COMMAND:
                continue
            key = (
                str(event.host or ""),
                str(event.payload.get("pid") or ""),
                str(event.payload.get("prompt_seq") or ""),
            )
            if any(key):
                self._seen.add(key)

    def add_spool(self, directory: Path, origin: str) -> None:
        """Register another spool directory, e.g. one pulled back from HPC."""

        self._origins[directory] = origin

    def _run_once(self) -> None:
        events: list[Event] = []
        for directory, origin in list(self._origins.items()):
            reader = self._readers.get(directory)
            if reader is None:
                reader = spool.SpoolReader(directory, origin=origin)
                self._readers[directory] = reader
            for record in reader.drain():
                event = self._convert(record, origin)
                if event is not None:
                    events.append(event)
        self.emit(events)

    def _convert(self, record: spool.SpoolRecord, origin: str) -> Event | None:
        if record.kind == spool.KIND_COMMAND:
            return self._convert_command(record, origin)
        if record.kind == spool.KIND_JOB:
            return self._convert_job(record, origin)
        if record.kind == spool.KIND_AGENT:
            return self._convert_agent(record, origin)
        if record.kind == spool.KIND_MARKER:
            event = Event(
                source="context",
                type=MARKER_USER,
                origin=origin,
                payload={
                    "label": record.named("label"),
                    "detail": record.named("detail"),
                },
            )
            ts = epoch_ms_to_iso(record.named("epoch_ms"))
            if ts:
                event.ts = ts
            return event
        return None

    def _convert_command(self, record: spool.SpoolRecord, origin: str) -> Event | None:
        host = self._host_from(record, origin)
        pid = record.named("pid")
        prompt_seq = record.named("prompt_seq")
        key = (host, pid, prompt_seq)
        if any(key) and key in self._seen:
            return None
        if any(key):
            self._seen.add(key)

        command = record.named("command")
        redacted = shared_redactor().apply(command)
        payload = {
            "command": redacted.text,
            "cwd": record.named("cwd"),
            "exit_code": record.named_int("exit_code"),
            "duration_ms": record.named_int("duration_ms"),
            "shell": record.named("shell") or "shell",
            "pid": record.named_int("pid"),
            "prompt_seq": record.named_int("prompt_seq"),
        }
        ts = epoch_ms_to_iso(record.named("epoch_ms"))
        event = Event(
            source="shell",
            type=SHELL_COMMAND,
            payload=payload,
            origin=origin,
            redactions=redacted.findings,
            host=host or None,
        )
        if ts:
            event.ts = ts
        return event

    def _convert_job(self, record: spool.SpoolRecord, origin: str) -> Event:
        detail = record.named("detail")
        event = Event(
            source="shell",
            type=JOB_SUBMITTED,
            origin=origin,
            host=self._host_from(record, origin) or None,
            payload={
                "scheduler": record.named("scheduler") or "slurm",
                "job_id": record.named("job_id"),
                **_parse_scontrol(detail),
            },
        )
        ts = epoch_ms_to_iso(record.named("epoch_ms"))
        if ts:
            event.ts = ts
        return event

    def _convert_agent(self, record: spool.SpoolRecord, origin: str) -> Event:
        redacted = shared_redactor().apply(record.named("text"))
        event = Event(
            source="agents",
            type=AGENT_MESSAGE,
            origin=origin,
            payload={
                "tool": record.named("tool"),
                "role": record.named("role"),
                "text": redacted.text,
                "cwd": record.named("cwd"),
            },
            redactions=redacted.findings,
        )
        ts = epoch_ms_to_iso(record.named("epoch_ms"))
        if ts:
            event.ts = ts
        return event

    @staticmethod
    def _host_from(record: spool.SpoolRecord, origin: str) -> str:
        """Derive the originating host from the spool filename.

        Spool files are named ``<host>-<pid>.rec``, so a remote file pulled back
        from a cluster keeps its own hostname rather than inheriting the
        laptop's -- which matters when the timeline interleaves both.
        """

        stem = record.source_file.stem
        if "-" in stem:
            return stem.rsplit("-", 1)[0]
        return stem


def _parse_scontrol(detail: str) -> dict[str, str]:
    """Parse ``scontrol show job -o`` output into the fields worth keeping.

    ``scontrol`` is queried at submit time because it is the only source of the
    real ``StdOut``/``StdErr``/``WorkDir`` paths -- guessing ``slurm-%j.out`` is
    wrong whenever the submitter used ``-o``.
    """

    if not detail:
        return {}
    wanted = {
        "JobName",
        "StdOut",
        "StdErr",
        "WorkDir",
        "Command",
        "Partition",
        "NumNodes",
        "NumCPUs",
        "JobState",
        "SubmitTime",
    }
    fields: dict[str, str] = {}
    for token in detail.split():
        if "=" not in token:
            continue
        key, _, value = token.partition("=")
        if key in wanted:
            fields[key.lower()] = value
    return fields
