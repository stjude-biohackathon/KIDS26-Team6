"""Export a sealed session into portable and AutoCAB-compatible formats.

``events.json`` retains the complete timeline, manifest, and seal provenance.
The AutoCAB adapter files are deliberately lossy projections.
``autocab.models.WorkflowStep`` has exactly four fields
(``timestamp``, ``tool``, ``action``, ``detail``), so exit codes, durations,
diffs and OCR either collapse into ``detail`` or are dropped.

Keeping the two representations separate is intentional. The pipeline's model
is tuned for clustering and lexical matching; the recorder's is tuned for
completeness. Widening ``WorkflowStep`` to fit the recorder would degrade both.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .autocab_screen import write_screen_capture
from .autocab_terminal import write_terminal_log
from .events_json import write_events_json
from .trace import write_trace

__all__ = [
    "export_session",
    "write_events_json",
    "write_screen_capture",
    "write_terminal_log",
    "write_trace",
]


def export_session(
    session,
    formats: list[str] | None = None,
    destination: Path | None = None,
    *,
    prefix_filenames: bool | None = None,
) -> dict[str, Any]:
    """Write the requested export formats into the selected directory.

    **Hard gate:** raises ``NotSealed`` unless ``<session>/seal.json`` exists.
    This is what turns a failed de-identification pass into a blocked leak
    rather than a permitted one -- an unsealed session simply cannot be
    exported. ``autocab.session_bundle`` carries the matching gate on the
    timeline itself, since gating ``exports/`` alone is bypassable.
    """

    from ..seal import require_sealed

    seal = require_sealed(session.root, what="export_session")

    wanted = set(formats or ["events", "autocab", "trace"])
    if "all" in wanted:
        wanted.update({"events", "terminal-log", "screen-capture", "trace"})
    if "autocab" in wanted:
        wanted.update({"terminal-log", "screen-capture"})

    written: list[str] = []
    details: dict[str, Any] = {}
    exports = (
        destination.expanduser().resolve()
        if destination is not None
        else session.root / "exports"
    )
    exports.mkdir(parents=True, exist_ok=True)
    if prefix_filenames is None:
        prefix_filenames = destination is not None
    filename_prefix = f"{session.session_id}-" if prefix_filenames else ""

    if "events" in wanted:
        path, count = write_events_json(
            session,
            seal,
            exports / f"{filename_prefix}events.json",
        )
        written.append(str(path))
        details["events"] = {"path": str(path), "events": count}

    if "terminal-log" in wanted:
        path, count = write_terminal_log(
            session,
            exports / f"{filename_prefix}terminal.log",
        )
        written.append(str(path))
        details["terminal_log"] = {"path": str(path), "commands": count}

    if "screen-capture" in wanted:
        path, count = write_screen_capture(
            session,
            exports / f"{filename_prefix}screen-events.json",
        )
        written.append(str(path))
        details["screen_capture"] = {"path": str(path), "events": count}

    if "trace" in wanted:
        path, steps = write_trace(
            session,
            exports / f"{filename_prefix}workflow-trace.json",
        )
        written.append(str(path))
        details["trace"] = {"path": str(path), "steps": steps}

    return {
        "session": session.session_id,
        "destination": str(exports),
        "written": written,
        "details": details,
        "seal": {
            "assurance": seal.get("assurance"),
            "generation": seal.get("generation"),
            "engine": seal.get("engine"),
            "sealed_at": seal.get("sealed_at"),
        },
    }
