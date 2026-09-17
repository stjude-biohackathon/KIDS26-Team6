"""Export a session into the formats AutoCAB's existing adapters consume.

The timeline is the source of truth; **these exports are deliberately lossy
projections of it.** ``autocab.models.WorkflowStep`` has exactly four fields
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
from .trace import write_trace

__all__ = [
    "export_session",
    "write_screen_capture",
    "write_terminal_log",
    "write_trace",
]


def export_session(session, formats: list[str] | None = None) -> dict[str, Any]:
    """Write the requested export formats into ``<session>/exports/``.

    **Hard gate:** raises ``NotSealed`` unless ``<session>/seal.json`` exists.
    This is what turns a failed de-identification pass into a blocked leak
    rather than a permitted one -- an unsealed session simply cannot be
    exported. ``autocab.session_bundle`` carries the matching gate on the
    timeline itself, since gating ``exports/`` alone is bypassable.
    """

    from ..seal import require_sealed

    seal = require_sealed(session.root, what="export_session")

    wanted = set(formats or ["autocab"])
    if "autocab" in wanted:
        wanted.update({"terminal-log", "screen-capture"})

    written: list[str] = []
    details: dict[str, Any] = {}
    exports = session.root / "exports"
    exports.mkdir(parents=True, exist_ok=True)

    if "terminal-log" in wanted:
        path, count = write_terminal_log(session)
        written.append(str(path))
        details["terminal_log"] = {"path": str(path), "commands": count}

    if "screen-capture" in wanted:
        path, count = write_screen_capture(session)
        written.append(str(path))
        details["screen_capture"] = {"path": str(path), "events": count}

    if "trace" in wanted:
        path, steps = write_trace(session)
        written.append(str(path))
        details["trace"] = {"path": str(path), "steps": steps}

    return {
        "session": session.session_id,
        "written": written,
        "details": details,
        "seal": {
            "assurance": seal.get("assurance"),
            "generation": seal.get("generation"),
            "engine": seal.get("engine"),
            "sealed_at": seal.get("sealed_at"),
        },
    }
