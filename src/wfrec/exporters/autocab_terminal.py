"""Export to the strict terminal-log format ``autocab.terminal_logs`` parses.

That parser is unforgiving, and the constraints are not obvious:

* ``METADATA_PATTERN`` accepts only ``# key: value``.
* ``TIMESTAMP_PATTERN`` accepts only ``YYYY-MM-DDTHH:MM:SS <command>`` -- no
  timezone suffix, no sub-second precision.
* **Any other line raises ``ValueError``** and aborts the whole import.

So a command containing a newline, or an empty command, cannot be emitted at
all, and timestamps must be truncated to whole seconds. A round-trip test in
``tests/test_wfrec_exporters.py`` feeds this file's output back through
``parse_terminal_session`` to prove conformance rather than assuming it.
"""

from __future__ import annotations

import re
from pathlib import Path

from ..events import SHELL_COMMAND, iso_to_seconds

#: Only these metadata keys are recognized by the parser; anything else is
#: silently ignored by it, so emitting more would be noise.
METADATA_KEYS = (
    "analyst",
    "title",
    "workflow_family",
    "trace_id",
    "summary",
    "tags",
    "source",
)

_UNSAFE = re.compile(r"[\r\n]+")


def _sanitize_command(command: str) -> str:
    """Flatten a command so it can occupy exactly one log line.

    Multi-line commands (heredocs, pasted scripts) are real and common; the
    parser cannot represent them, so newlines become a visible marker rather
    than being dropped silently.
    """

    return _UNSAFE.sub(" ; ", command).strip()


def _slug(text: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return slug or "workflow"


def build_terminal_log(session) -> tuple[str, int]:
    """Render the export body and return it with the command count."""

    manifest = session.manifest
    commands: list[tuple[str, str]] = []
    for event in session.writer.read_sorted():
        if event.type != SHELL_COMMAND:
            continue
        command = _sanitize_command(str(event.payload.get("command") or ""))
        if not command:
            continue
        commands.append((iso_to_seconds(event.ts), command))

    title = manifest.title or f"Session {session.session_id}"
    workflow_family = manifest.workflow_family or _slug(title).replace("-", " ")
    tags = ", ".join(manifest.tags) if manifest.tags else ""

    summary = (
        f"Recorded with wfrec on {manifest.platform.get('system', 'unknown')}; "
        f"{len(commands)} shell commands over "
        f"{round(manifest.active_seconds)}s active and "
        f"{round(manifest.paused_seconds)}s paused."
    )

    metadata = {
        "analyst": manifest.analyst,
        "title": title,
        "workflow_family": workflow_family,
        "trace_id": f"wfrec-{_slug(manifest.analyst)}-{session.session_id}",
        "summary": summary,
        "source": f"wfrec:{session.session_id}",
    }
    if tags:
        metadata["tags"] = tags

    lines = [f"# {key}: {metadata[key]}" for key in METADATA_KEYS if key in metadata]
    lines.extend(f"{timestamp} {command}" for timestamp, command in commands)
    return "\n".join(lines) + "\n", len(commands)


def write_terminal_log(session, destination: Path | None = None) -> tuple[Path, int]:
    """Write ``exports/autocab-terminal.log`` and return its path and count."""

    body, count = build_terminal_log(session)
    target = destination or (session.root / "exports" / "autocab-terminal.log")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(body, encoding="utf-8")
    return target, count
