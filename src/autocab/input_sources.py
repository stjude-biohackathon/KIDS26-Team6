"""Input sources for AutoCAB workflow ingestion."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .demo_data import load_workflow_traces
from .models import WorkflowStep, WorkflowTrace
from .terminal_logs import convert_terminal_log


@dataclass(slots=True)
class InputBundle:
    """Resolved workflow input for one pipeline run."""

    mode: str
    traces: list[WorkflowTrace]
    source_note: str


def load_trace_input(trace_path: Path | None = None) -> InputBundle:
    """Load workflow traces from JSON input."""

    resolved_path = trace_path or Path(__file__).resolve().parents[2] / "data" / "sample_workflow_traces.json"
    traces = load_workflow_traces(resolved_path)
    return InputBundle(
        mode="trace",
        traces=traces,
        source_note=f"Loaded workflow traces from {resolved_path}.",
    )


def _slugify(text: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return slug or "workflow"


def _summarize_capture_event(event: dict[str, Any]) -> str:
    detail_parts = [
        event.get("ocr_text", ""),
        event.get("notes", ""),
    ]
    cleaned = " ".join(part.strip() for part in detail_parts if part and part.strip())
    return cleaned or "Observed capture event."


def _tags_from_capture_session(session: dict[str, Any]) -> list[str]:
    seed_tags = list(session.get("tags", []))
    for event in session.get("events", []):
        tool = event.get("tool")
        action = event.get("action")
        if isinstance(tool, str):
            seed_tags.append(tool.lower())
        if isinstance(action, str):
            seed_tags.append(action.lower())

    normalized = []
    seen: set[str] = set()
    for tag in seed_tags:
        clean = re.sub(r"[^a-z0-9]+", "-", tag.lower()).strip("-")
        if clean and clean not in seen:
            seen.add(clean)
            normalized.append(clean)
    return normalized


def _trace_from_capture_session(session: dict[str, Any], index: int) -> WorkflowTrace:
    events = session.get("events", [])
    if not events:
        raise ValueError(f"Capture session {index} has no events.")

    workflow_family = session.get("workflow_family") or session.get("title") or f"capture-session-{index}"
    title = session.get("title") or workflow_family.title()
    analyst = session.get("analyst", "unknown-analyst")
    steps = [
        WorkflowStep(
            timestamp=str(event["timestamp"]),
            tool=str(event.get("tool", "screen-capture")),
            action=str(event.get("action", "observe")),
            detail=_summarize_capture_event(event),
        )
        for event in events
    ]
    summary = session.get("summary") or " ".join(step.detail for step in steps[:4])
    return WorkflowTrace(
        trace_id=session.get("trace_id", f"capture-{_slugify(workflow_family)}-{index}"),
        analyst=analyst,
        title=title,
        summary=summary,
        tags=_tags_from_capture_session(session),
        source="screen-capture",
        workflow_family=workflow_family,
        steps=steps,
    )


def load_screen_capture_input(capture_path: Path | None = None) -> InputBundle:
    """Load a consented screen-capture export and map it into workflow traces."""

    resolved_path = capture_path or Path(__file__).resolve().parents[2] / "data" / "sample_screen_capture.json"
    payload = json.loads(resolved_path.read_text(encoding="utf-8"))
    sessions = payload.get("sessions", [])
    if not sessions:
        raise ValueError(f"Screen capture input {resolved_path} does not contain any sessions.")

    traces = [_trace_from_capture_session(session, index) for index, session in enumerate(sessions, start=1)]
    return InputBundle(
        mode="screen-capture",
        traces=traces,
        source_note=(
            f"Loaded consented screen-capture export from {resolved_path}. "
            "This demo expects pre-exported local events rather than controlling a recorder directly."
        ),
    )


def load_terminal_log_input(log_path: Path | None = None) -> InputBundle:
    """Load a terminal workflow log and convert it into one workflow trace."""

    resolved_path = log_path or Path(__file__).resolve().parents[2] / "data" / "sample_terminal_session.log"
    trace = convert_terminal_log(resolved_path)
    return InputBundle(
        mode="terminal-log",
        traces=[trace],
        source_note=f"Loaded terminal workflow log from {resolved_path} and converted it into trace JSON.",
    )
