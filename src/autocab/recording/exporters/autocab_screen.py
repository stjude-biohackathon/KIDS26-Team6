"""Export to the screenpipe-style JSON ``load_screen_capture_input`` reads.

The shape that function expects is::

    {"sessions":[{..., "events":[
        {timestamp, tool, action, window_title, ocr_text, notes}]}]}

OCR text and active-window titles map directly onto ``ocr_text`` and
``window_title``, which is the concrete payoff for choosing screenshots-plus-OCR
over video-only: the pipeline already knows how to read this.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from ..events import (
    AGENT_MESSAGE,
    AGENT_TOOL_COMPLETED,
    CONTEXT_NOTE,
    FILE_DIFF,
    JOB_SUBMITTED,
    MARKER_USER,
    SCREEN_OCR,
    SCREEN_WINDOW,
    SHELL_COMMAND,
)

#: Which timeline events become capture events, and how each is labelled.
#: ``tool`` and ``action`` feed ``_tags_from_capture_session``, so these strings
#: end up as cluster tags -- they are part of the matching signal, not cosmetic.
EVENT_MAP = {
    SCREEN_OCR: ("screen", "observe"),
    SCREEN_WINDOW: ("window", "focus"),
    SHELL_COMMAND: ("terminal", "run"),
    CONTEXT_NOTE: ("notes", "annotate"),
    AGENT_MESSAGE: ("agent", "converse"),
    AGENT_TOOL_COMPLETED: ("agent", "use"),
    FILE_DIFF: ("editor", "edit"),
    JOB_SUBMITTED: ("scheduler", "submit"),
    MARKER_USER: ("notes", "mark"),
}


def _slug(text: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return slug or "workflow"


def build_screen_capture(session) -> tuple[dict, int]:
    manifest = session.manifest
    events: list[dict[str, object]] = []

    for event in session.writer.read_sorted():
        mapping = EVENT_MAP.get(event.type)
        if mapping is None:
            continue
        tool, action = mapping
        payload = event.payload

        ocr_text = ""
        notes = ""
        window_title = str(payload.get("window_title") or "")

        if event.type == SCREEN_OCR:
            ocr_text = str(payload.get("ocr_text") or "")
        elif event.type == SHELL_COMMAND:
            exit_code = payload.get("exit_code")
            duration = payload.get("duration_ms")
            notes = f"$ {payload.get('command', '')}"
            extra = []
            if exit_code not in (None, ""):
                extra.append(f"exit={exit_code}")
            if duration not in (None, ""):
                extra.append(f"{duration}ms")
            if payload.get("cwd"):
                extra.append(f"cwd={payload['cwd']}")
            if extra:
                notes += f" [{' '.join(extra)}]"
        elif event.type == CONTEXT_NOTE:
            notes = str(payload.get("text") or "")
            if payload.get("label"):
                notes = f"{payload['label']}: {notes}"
        elif event.type == AGENT_MESSAGE:
            notes = f"[{payload.get('tool')}/{payload.get('role')}] {payload.get('text', '')}"
        elif event.type == AGENT_TOOL_COMPLETED:
            detail = payload.get("command") or payload.get("arguments") or payload.get("output")
            notes = f"[{payload.get('agent')}/{payload.get('tool_name')}] {detail or ''}"
        elif event.type == FILE_DIFF:
            notes = (
                f"edited {payload.get('path')} (+{payload.get('added')}/-{payload.get('deleted')})"
            )
        elif event.type == JOB_SUBMITTED:
            notes = (
                f"submitted {payload.get('scheduler')} job {payload.get('job_id')} "
                f"{payload.get('jobname', '')}".strip()
            )
        elif event.type == MARKER_USER:
            notes = f"{payload.get('label')} {payload.get('detail', '')}".strip()

        if not (ocr_text or notes or window_title):
            continue

        events.append(
            {
                "timestamp": event.ts,
                "tool": tool,
                "action": action,
                "window_title": window_title,
                "ocr_text": ocr_text,
                "notes": notes,
            }
        )

    title = manifest.title or f"Session {session.session_id}"
    document = {
        "sessions": [
            {
                "trace_id": f"wfrec-{_slug(manifest.analyst)}-{session.session_id}",
                "analyst": manifest.analyst,
                "title": title,
                "workflow_family": manifest.workflow_family or _slug(title).replace("-", " "),
                "summary": (
                    f"wfrec session on {manifest.host} "
                    f"({manifest.platform.get('system', 'unknown')}) with "
                    f"{len(events)} observed events."
                ),
                "tags": list(manifest.tags),
                "events": events,
            }
        ]
    }
    return document, len(events)


def write_screen_capture(session, destination: Path | None = None) -> tuple[Path, int]:
    document, count = build_screen_capture(session)
    target = destination or (session.root / "exports" / "screen-events.json")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")
    return target, count
