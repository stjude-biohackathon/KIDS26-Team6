"""Input adapter for wfrec session folders.

This is the native, full-fidelity path from a recorded session into the
AutoCAB pipeline. It accepts either:

* a **session directory** (``~/.wfrec/sessions/<id>/``), or
* a **trace JSON file** already exported from one, or
* a **directory of session directories**, which is how several analysts'
  sessions are ingested together for the multi-analyst clustering in
  challenge extension (b).

The conversion is deliberately lossy: ``WorkflowStep`` carries only
``timestamp``/``tool``/``action``/``detail``, so a session's exit codes,
durations, diffs and OCR are folded into ``detail``. The session folder remains
the source of truth for anything that needs the full record.
"""

from __future__ import annotations

import json
from pathlib import Path

from .input_sources import InputBundle
from .models import WorkflowTrace


class SessionBundleError(ValueError):
    """Raised when a path is not a usable wfrec session bundle."""


def _looks_like_session(path: Path) -> bool:
    return (path / "manifest.json").exists() and (path / "events.jsonl").exists()


def _traces_from_trace_json(path: Path) -> list[WorkflowTrace]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    records = payload if isinstance(payload, list) else [payload]
    return [WorkflowTrace.from_dict(record) for record in records]


def _trace_from_session_dir(path: Path) -> WorkflowTrace:
    """Build a trace from a session folder, preferring a live rebuild.

    A previously written ``exports/trace.json`` is only used as a fallback: the
    timeline may have grown since it was written, and silently ingesting a
    stale export is the kind of bug that shows up as "the demo didn't include
    the thing I just did".
    """

    try:
        from wfrec.exporters.trace import build_trace
        from wfrec.seal import require_sealed
        from wfrec.session import Manifest, Session

        # The **second** hard gate, and the one that actually matters: this
        # function rebuilds the trace from `events.jsonl` live, so gating only
        # `exports/` would leave the timeline ingestible by anyone who pointed
        # `--session-dir` at the folder. Guard the timeline, not the projection.
        require_sealed(path, what="session ingestion")

        manifest = Manifest.from_dict(
            json.loads((path / "manifest.json").read_text(encoding="utf-8"))
        )
        session = Session(manifest)
        # Point the session at this folder explicitly: a bundle handed over by
        # a teammate will not live under the local WFREC_HOME.
        session.root = path
        from wfrec.events import EventWriter

        session.writer = EventWriter(
            path,
            session_id=manifest.session_id,
            analyst=manifest.analyst,
            host=manifest.host,
        )
        trace, steps = build_trace(session)
        if steps:
            return WorkflowTrace.from_dict(trace)
        raise SessionBundleError(
            f"Session {path} contains no convertible workflow steps."
        )
    except ImportError as exc:
        exported = path / "exports" / "trace.json"
        if exported.exists():
            traces = _traces_from_trace_json(exported)
            if traces:
                return traces[0]
        raise SessionBundleError(
            f"wfrec is not importable ({exc}) and {path} has no exports/trace.json. "
            "Run `wfrec export --format trace` inside the session, or install wfrec."
        ) from exc


def load_session_input(session_path: Path | None = None) -> InputBundle:
    """Load one or many wfrec sessions as workflow traces."""

    if session_path is None:
        raise SessionBundleError(
            "Session mode needs a path: pass --session-dir <folder>. "
            "Find recorded sessions with `wfrec sessions`."
        )

    path = Path(session_path).expanduser()
    if not path.exists():
        raise SessionBundleError(f"No such session path: {path}")

    if path.is_file():
        traces = _traces_from_trace_json(path)
        if not traces:
            raise SessionBundleError(f"{path} contains no traces.")
        return InputBundle(
            mode="session",
            traces=traces,
            source_note=f"Loaded {len(traces)} wfrec trace(s) from {path}.",
        )

    if _looks_like_session(path):
        trace = _trace_from_session_dir(path)
        return InputBundle(
            mode="session",
            traces=[trace],
            source_note=(
                f"Loaded wfrec session {path.name} from {path} "
                f"({len(trace.steps)} steps, analyst {trace.analyst})."
            ),
        )

    # A directory of sessions: this is the multi-analyst ingest path.
    candidates = sorted(
        child for child in path.iterdir() if child.is_dir() and _looks_like_session(child)
    )
    if not candidates:
        raise SessionBundleError(
            f"{path} is neither a wfrec session folder nor a directory of them "
            "(expected manifest.json + events.jsonl)."
        )

    traces: list[WorkflowTrace] = []
    skipped: list[str] = []
    for child in candidates:
        try:
            traces.append(_trace_from_session_dir(child))
        except SessionBundleError:
            skipped.append(child.name)

    if not traces:
        raise SessionBundleError(f"No convertible sessions found under {path}.")

    analysts = sorted({trace.analyst for trace in traces})
    note = (
        f"Loaded {len(traces)} wfrec session(s) from {path} "
        f"across {len(analysts)} analyst(s): {', '.join(analysts)}."
    )
    if skipped:
        note += f" Skipped {len(skipped)} empty session(s): {', '.join(skipped)}."
    return InputBundle(mode="session", traces=traces, source_note=note)
