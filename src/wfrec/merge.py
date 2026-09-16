"""Merge multiple session folders into one export.

Serves challenge extension (b), the multi-analyst aggregation layer. Each
session already records its own ``analyst`` in the manifest, and
``autocab.framework.components.WorkflowClusterer`` groups traces by
``workflow_family`` while collecting the distinct analysts per cluster -- so a
merged trace file is all the pipeline needs to separate workflows several people
repeat from one-off idiosyncratic ones.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .exporters.trace import build_trace
from .session import Session


def merge_sessions(
    session_ids: list[str], destination: Path, *, workflow_family: str | None = None
) -> dict[str, Any]:
    """Write one trace-array JSON combining several sessions."""

    traces: list[dict] = []
    analysts: list[str] = []
    skipped: list[dict[str, str]] = []

    for session_id in session_ids:
        try:
            session = Session.load(session_id)
        except Exception as exc:
            skipped.append({"session": session_id, "error": str(exc)})
            continue
        trace, steps = build_trace(session)
        if not steps:
            skipped.append({"session": session_id, "error": "no steps recorded"})
            continue
        if workflow_family:
            # Forcing a shared family is how you make several analysts' runs
            # land in one cluster when they titled their sessions differently.
            trace["workflow_family"] = workflow_family
        traces.append(trace)
        if trace["analyst"] not in analysts:
            analysts.append(trace["analyst"])

    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(traces, indent=2) + "\n", encoding="utf-8")

    families: dict[str, list[str]] = {}
    for trace in traces:
        families.setdefault(trace["workflow_family"], [])
        if trace["analyst"] not in families[trace["workflow_family"]]:
            families[trace["workflow_family"]].append(trace["analyst"])

    return {
        "path": str(destination),
        "traces": len(traces),
        "analysts": analysts,
        "workflow_families": families,
        "multi_analyst_families": {
            family: people for family, people in families.items() if len(people) > 1
        },
        "skipped": skipped,
    }
