"""Shell collector: drains hook spool files into the timeline.

The hooks do the capture; this collector only converts. That split is what
keeps a dead daemon from ever hanging a terminal, and it means shell commands
typed while the daemon was down are still picked up on the next start.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .. import spool
from ..devsql import DevSQLClient
from ..events import (
    AGENT_MESSAGE,
    Event,
    JOB_SUBMITTED,
    MARKER_USER,
    SHELL_COMMAND,
    SESSION_RESUMED,
    SESSION_STARTED,
)
from ..redaction import shared as shared_redactor
from .base import Collector, CollectorStatus

if TYPE_CHECKING:
    from ..session import Session


DEVSQL_COMMAND_COLUMNS = (
    "source",
    "session_id",
    "source_id",
    "timestamp",
    "duration_ms",
    "command",
    "cwd",
    "exit_code",
    "hostname",
    "channel",
    "actor",
    "provenance_quality",
    "provenance_reason",
    "agent_id",
    "agent_role",
    "originator",
    "tool_name",
)


def datetime_to_iso(moment: datetime) -> str:
    """Format a timestamp using wfrec's millisecond UTC representation."""

    utc_timestamp = moment.astimezone(timezone.utc).isoformat(
        timespec="milliseconds"
    )
    return utc_timestamp.replace("+00:00", "Z")


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
    return datetime_to_iso(moment)


def parse_devsql_timestamp(value: object) -> datetime | None:
    """Parse a DevSQL timestamp without guessing a timezone."""

    if not isinstance(value, str) or not value:
        return None
    normalized = f"{value[:-1]}+00:00" if value.endswith("Z") else value
    try:
        moment = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if moment.tzinfo is None:
        return None
    return moment.astimezone(timezone.utc)


def active_interval_start(session: "Session") -> str:
    """Return the start of the session's current active interval."""

    for event in reversed(session.writer.read()):
        if event.type in {SESSION_STARTED, SESSION_RESUMED}:
            return event.ts
    return session.manifest.created_at


def optional_int(value: object) -> int | None:
    """Normalize a nullable integer from DevSQL."""

    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


class DevSQLCommandReader:
    """Read Atuin and agent commands from one active recording interval."""

    def __init__(self, client: DevSQLClient, *, since: str) -> None:
        interval_start = parse_devsql_timestamp(since)
        if interval_start is None:
            raise ValueError(
                "DevSQL interval start must be a timezone-aware timestamp."
            )
        self.client = client
        self.interval_start = interval_start
        self._seen: set[tuple[str, str, str]] = set()

    def read(self) -> list[Event]:
        """Return new commands while retaining both Claude and Codex rows."""

        # DevSQL timestamps have mixed fractional precision. SQL narrows to the
        # correct second, then Python applies the exact interval boundary.
        query_second = self.interval_start.strftime("%Y-%m-%dT%H:%M:%S")
        columns = ", ".join(DEVSQL_COMMAND_COLUMNS)
        sql = (
            f"SELECT {columns} FROM command_events "
            f"WHERE substr(timestamp, 1, 19) >= '{query_second}' "
            "AND (source = 'atuin' OR channel = 'agent_tool') "
            "ORDER BY timestamp, source, session_id, source_id"
        )
        rows = self.client.query(sql, required_columns=DEVSQL_COMMAND_COLUMNS)

        events: list[Event] = []
        for row in rows:
            event = self._convert(row)
            if event is not None:
                events.append(event)
        return events

    def _convert(self, row: dict[str, Any]) -> Event | None:
        source = row["source"]
        channel = row["channel"]
        if source != "atuin" and channel != "agent_tool":
            return None

        session_id = row["session_id"]
        source_id = row["source_id"]
        stable_identity = (source, session_id, source_id)
        if not all(
            isinstance(value, str) and value for value in stable_identity
        ):
            return None

        if stable_identity in self._seen:
            return None

        occurred_at = parse_devsql_timestamp(row["timestamp"])
        if occurred_at is None or occurred_at < self.interval_start:
            return None

        command = row["command"]
        if not isinstance(command, str) or not command:
            return None
        redacted = shared_redactor().apply(command)
        if not redacted.available:
            raise RuntimeError("DevSQL command redaction is unavailable.")

        self._seen.add(stable_identity)
        payload = {
            "command": redacted.text,
            "cwd": row["cwd"],
            "exit_code": optional_int(row["exit_code"]),
            "duration_ms": optional_int(row["duration_ms"]),
            "source": source,
            "session_id": session_id,
            "source_id": source_id,
            "channel": channel,
            "actor": row["actor"],
            "provenance_quality": row["provenance_quality"],
            "provenance_reason": row["provenance_reason"],
            "agent_id": row["agent_id"],
            "agent_role": row["agent_role"],
            "originator": row["originator"],
            "tool_name": row["tool_name"],
        }
        hostname = row["hostname"]
        return Event(
            source="shell",
            type=SHELL_COMMAND,
            payload=payload,
            ts=datetime_to_iso(occurred_at),
            host=hostname if isinstance(hostname, str) and hostname else None,
            redactions=redacted.findings,
        )


class ShellCollector(Collector):
    """Ingests ``<session>/spool/*.rec`` and any pulled-back remote spools."""

    source = "shell"
    interval = 1.0

    def __init__(
        self,
        session: "Session",
        extra_spools: dict[Path, str] | None = None,
        devsql_client: DevSQLClient | None = None,
    ) -> None:
        super().__init__(session)
        self._readers: dict[Path, spool.SpoolReader] = {}
        self._origins: dict[Path, str] = {session.spool_dir: "local"}
        self._devsql_reader = (
            DevSQLCommandReader(
                devsql_client,
                since=active_interval_start(session),
            )
            if devsql_client is not None
            else None
        )
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
        # Emit spool events first so a DevSQL failure cannot block remote or
        # fallback records that were already drained successfully.
        if self._devsql_reader is not None:
            self.emit(self._devsql_reader.read())

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
