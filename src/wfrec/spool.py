"""The shell-to-daemon spool format.

Shell hooks never talk to the daemon. They append one record per command to
their own per-writer file under ``<session>/spool/``, and the daemon converts
those records into timeline events on ingest.

Why not JSON
------------
Emitting valid JSON from a shell hook means escaping quotes, backslashes,
newlines and control bytes in the command line. In bash/zsh that is four
``${var//x/y}`` expansions; in POSIX ``sh`` -- which is what an HPC login node
gives you, and what the remote hook must be -- that syntax does not exist at
all, so it would cost a fork per prompt. Instead each record is delimited with
the ASCII control characters reserved for exactly this purpose::

    <RS> kind <US> field <US> field ... <LF>

The only byte a field may not contain is the delimiter itself, and neither
0x1e nor 0x1f occurs in a realistic command line, cwd or hostname. This removes
an entire class of escaping bugs and lets the local and remote hooks be
byte-identical.

Why one file per writer
-----------------------
Each shell process writes to ``<host>-<pid>.rec`` and nothing else does. With a
single writer there is no interleaving and no lock required -- which also makes
appends safe on the NFS-mounted ``$HOME`` of a load-balanced HPC login node,
where multi-writer ``O_APPEND`` is not.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

RS = "\x1e"
US = "\x1f"

# Hazard: ``str.splitlines()`` treats \x1c, \x1d and \x1e as line boundaries,
# so it cannot be used to split a stream of these records -- it would cut every
# record in half at its own leading RS. Always split on "\n" explicitly.

KIND_COMMAND = "cmd"
KIND_AGENT = "agent"
KIND_JOB = "job"
KIND_MARKER = "mark"

#: Canonical field order per record kind, after the leading ``kind`` field.
#:
#: Named rather than positional because the hooks are written in four different
#: shell languages and the consumers in Python: a bare index like ``field(4)``
#: scattered across both sides is the obvious place for a silent drift bug.
FIELDS: dict[str, tuple[str, ...]] = {
    KIND_COMMAND: (
        "epoch_ms",
        "cwd",
        "exit_code",
        "duration_ms",
        "command",
        "shell",
        "pid",
        "prompt_seq",
    ),
    KIND_AGENT: ("epoch_ms", "tool", "role", "text", "cwd"),
    KIND_JOB: ("epoch_ms", "scheduler", "job_id", "detail"),
    KIND_MARKER: ("epoch_ms", "label", "detail"),
}

#: Hooks emit epoch milliseconds rather than a formatted timestamp.
#:
#: Rendering UTC ISO-8601 inside a shell hook means either a ``date`` fork per
#: prompt or relying on ``printf %(...)T``, which formats in *localtime* and
#: differs between bash, zsh and BSD userlands. Epoch integers are unambiguous,
#: need no forks, and the daemon converts them once on ingest.


@dataclass(slots=True)
class SpoolRecord:
    """One decoded spool record."""

    kind: str
    fields: list[str]
    source_file: Path
    origin: str = "local"

    def field(self, index: int, default: str = "") -> str:
        """Read a positional field, tolerating hooks that emit fewer of them.

        Older or remote hooks may write shorter records (a POSIX ``sh`` hook has
        no sub-second clock, for instance), so every consumer must cope with a
        missing tail rather than raising.
        """

        if 0 <= index < len(self.fields):
            return self.fields[index]
        return default

    def named(self, name: str, default: str = "") -> str:
        """Read a field by its canonical name for this record kind."""

        layout = FIELDS.get(self.kind)
        if not layout or name not in layout:
            return default
        return self.field(layout.index(name), default)

    def named_int(self, name: str, default: int | None = None) -> int | None:
        """Read a field by name and coerce it to ``int``."""

        layout = FIELDS.get(self.kind)
        if not layout or name not in layout:
            return default
        return self.int_field(layout.index(name), default)

    def as_dict(self) -> dict[str, str]:
        """Return every populated field keyed by its canonical name."""

        layout = FIELDS.get(self.kind, ())
        return {
            name: self.field(index)
            for index, name in enumerate(layout)
            if self.field(index)
        }

    def int_field(self, index: int, default: int | None = None) -> int | None:
        raw = self.field(index).strip()
        if not raw:
            return default
        try:
            return int(raw)
        except ValueError:
            return default


def encode(kind: str, *fields: str) -> str:
    """Build a spool record. Used by the Python-side writers and by tests."""

    cleaned = [str(value).replace(RS, " ").replace(US, " ") for value in fields]
    return RS + US.join([kind, *cleaned]) + "\n"


def decode_line(line: str, source_file: Path, origin: str = "local") -> SpoolRecord | None:
    """Decode a single spool line, or return ``None`` if it is not a record."""

    if not line.startswith(RS):
        return None
    body = line[len(RS) :].rstrip("\n")
    if not body:
        return None
    parts = body.split(US)
    return SpoolRecord(
        kind=parts[0], fields=parts[1:], source_file=source_file, origin=origin
    )


def spool_file(spool_dir: Path, host: str, pid: int | None = None) -> Path:
    """Path of the per-writer spool file for one process."""

    return spool_dir / f"{host}-{pid or os.getpid()}.rec"


def append(path: Path, record: str) -> None:
    """Append one record to a spool file.

    Kept deliberately simple and non-fatal: a recorder that raises inside a
    shell prompt is worse than one that silently drops a single command.
    """

    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(record)
    except OSError:
        return


class SpoolReader:
    """Incrementally drains spool files, remembering a byte offset per file.

    Offsets are held in memory by the daemon and re-derived on restart from the
    timeline, so a restart re-reads a little rather than losing data. Records
    carry an idempotency key (host, pid, prompt sequence) which the ingest side
    uses to drop anything already on the timeline.
    """

    def __init__(self, spool_dir: Path, origin: str = "local") -> None:
        self._dir = spool_dir
        self._origin = origin
        self._offsets: dict[Path, int] = {}

    def set_offset(self, path: Path, offset: int) -> None:
        self._offsets[path] = offset

    def drain(self) -> list[SpoolRecord]:
        """Return every record appended since the last drain."""

        records: list[SpoolRecord] = []
        if not self._dir.is_dir():
            return records

        for path in sorted(self._dir.glob("*.rec")):
            start = self._offsets.get(path, 0)
            try:
                size = path.stat().st_size
            except OSError:
                continue
            if size < start:
                # File was truncated or replaced; re-read from the beginning
                # rather than skipping into the middle of a record.
                start = 0
            if size == start:
                continue
            try:
                with path.open("r", encoding="utf-8", errors="replace") as handle:
                    handle.seek(start)
                    chunk = handle.read()
            except OSError:
                continue

            # Only consume through the last complete line: a hook may be
            # mid-append, and half a record must not be decoded now and again
            # on the next drain.
            cut = chunk.rfind("\n")
            if cut == -1:
                continue
            complete, consumed = chunk[: cut + 1], chunk[: cut + 1]
            self._offsets[path] = start + len(consumed.encode("utf-8"))

            # NOTE: split("\n"), never splitlines(). Python's splitlines()
            # treats \x1c, \x1d and \x1e as line boundaries -- and RS *is*
            # \x1e -- so splitlines() would shred every record it parsed.
            for line in complete.split("\n"):
                if not line:
                    continue
                record = decode_line(line + "\n", path, self._origin)
                if record is not None:
                    records.append(record)
        return records
