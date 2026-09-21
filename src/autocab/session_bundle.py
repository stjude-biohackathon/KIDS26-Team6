"""Load recording sessions into the AutoCAB workflow pipeline.

The adapter accepts a session directory, an exported trace, or a directory of
sessions. It projects full events into the smaller ``WorkflowStep`` model while
the session folder remains the complete source.
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

    A previously written workflow trace is only used as a fallback. The
    timeline may have grown since it was written, and silently ingesting a
    stale export is the kind of bug that shows up as "the demo didn't include
    the thing I just did".
    """

    try:
        from autocab.recording.exporters.trace import build_trace
        from autocab.recording.seal import require_sealed
        from autocab.recording.session import Manifest, Session

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
        from autocab.recording.events import EventWriter

        session.writer = EventWriter(
            path,
            session_id=manifest.session_id,
            analyst=manifest.analyst,
            host=manifest.host,
        )
        trace, steps = build_trace(session)
        if steps:
            return WorkflowTrace.from_dict(trace)
        raise SessionBundleError(f"Session {path} contains no convertible workflow steps.")
    except ImportError as exc:
        export_paths = (
            path / "exports" / "workflow-trace.json",
            path / "exports" / "trace.json",
        )
        for exported in export_paths:
            if exported.exists():
                traces = _traces_from_trace_json(exported)
                if traces:
                    return traces[0]
        raise SessionBundleError(
            f"wfrec is not importable ({exc}) and {path} has no exported "
            "workflow trace. "
            "Run `wfrec export --format trace` inside the session, or install AutoCAB."
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
