"""Local HTTP control API.

The single place recorder operations are implemented. The CLI, the GUI and the
``recorder.md`` agent skill are all thin clients of this API, which is why
"start screen recording" behaves identically whether it was typed as a command,
clicked in the window, or asked for in natural language.

Binds to loopback by default (``127.0.0.1``). Optional non-loopback bind is
available via ``wfrec daemon --bind-all`` / ``--host`` with ``--allow-remote``
and requires a bearer token read from a file that is mode 0600 in the runtime
directory. The recorder makes no outbound network requests at all; that property
is worth keeping easy to verify.
"""

from __future__ import annotations

import json
import secrets
from itertools import islice
from pathlib import Path
from typing import Any, Literal

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel, Field

from . import SOURCES, __version__
from .events import Event
from .markdown import render_markdown
from .recorder import NoActiveSession, Recorder
from .session import Session, SessionNotFound

MAX_EVENT_PAGE_SIZE = 2_000


class StartRequest(BaseModel):
    title: str = ""
    analyst: str = ""
    workflow_family: str = ""
    tags: list[str] = Field(default_factory=list)
    watch: list[str] = Field(default_factory=list)
    sources: dict[str, bool] = Field(default_factory=dict)


class PauseRequest(BaseModel):
    session_id: str | None = None
    reason: str = ""
    expect: str = ""


class SessionRef(BaseModel):
    session_id: str | None = None


class SourceRequest(BaseModel):
    enabled: bool


class NoteRequest(BaseModel):
    text: str
    label: str = ""
    pasted: bool = False
    window: str = ""


class MarkerRequest(BaseModel):
    label: str
    detail: str = ""


class WatchRequest(BaseModel):
    root: str


class ExportRequest(BaseModel):
    session_id: str | None = None
    formats: list[str] = Field(default_factory=lambda: ["autocab"])


class SealRequest(BaseModel):
    session_id: str | None = None
    profile: Literal["regex-only"] = "regex-only"
    reseal: bool = False
    force: bool = False
    dry_run: bool = False
    status: bool = False


def _event_count(session: Session) -> int:
    """Return the append counter without parsing the complete event timeline."""

    try:
        return max(0, int((session.root / ".seq").read_text(encoding="utf-8")))
    except (OSError, ValueError):
        try:
            with session.writer.path.open(encoding="utf-8") as handle:
                return sum(1 for line in handle if line.strip())
        except FileNotFoundError:
            return 0


def _read_event_page(session: Session, *, offset: int, limit: int) -> list[Event]:
    """Parse only the requested JSONL rows instead of loading the full session."""

    events: list[Event] = []
    try:
        handle = session.writer.path.open(encoding="utf-8")
    except FileNotFoundError:
        return events
    with handle:
        for line in islice(handle, offset, offset + limit):
            try:
                payload = json.loads(line)
                if isinstance(payload, dict):
                    events.append(Event.from_dict(payload))
            except (json.JSONDecodeError, KeyError, TypeError, ValueError):
                # A partial final line is expected when the daemon is writing.
                continue
    return events


def _event_response(event: Event) -> dict[str, Any]:
    """Add safe display HTML without changing the persisted event payload."""

    response = event.to_dict()
    if event.type not in {"agent.message", "context.note"}:
        return response

    text = event.payload.get("text")
    if not isinstance(text, str) or not text.strip():
        return response

    response["presentation"] = {
        "detail_format": "markdown",
        "detail_html": render_markdown(text),
    }
    return response


def create_app(recorder: Recorder, token: str) -> FastAPI:
    """Build the control API around a live ``Recorder``."""

    app = FastAPI(title="wfrec control API", version=__version__)

    def authorize(request: Request) -> None:
        """Bearer-token check.

        The API is loopback-only, but any process on the machine can reach
        loopback, so a token still matters -- it keeps another user's script (or
        a browser page) from driving someone's recorder.
        """

        header = request.headers.get("authorization", "")
        supplied = header[7:] if header.lower().startswith("bearer ") else (
            request.query_params.get("token") or ""
        )
        if not secrets.compare_digest(supplied, token):
            raise HTTPException(status_code=401, detail="Invalid or missing API token.")

    guard = [Depends(authorize)]

    def _doctor_response() -> dict[str, Any]:
        from .doctor import diagnose

        report = diagnose()
        safe_sources: dict[str, Any] = {}
        for name, payload in (report.get("sources") or {}).items():
            if isinstance(payload, dict):
                safe_sources[str(name)] = {
                    key: value
                    for key, value in payload.items()
                    if key in {"ok", "available", "backend", "reason", "extra"}
                }
        return {
            "wfrec_version": report.get("wfrec_version", __version__),
            "python": report.get("python", ""),
            "platform": report.get("platform", {}),
            "state": report.get("state", {}),
            "sources": safe_sources,
        }

    @app.exception_handler(NoActiveSession)
    async def _no_session(_request: Request, exc: NoActiveSession) -> JSONResponse:
        return JSONResponse(status_code=409, content={"detail": str(exc)})

    @app.exception_handler(SessionNotFound)
    async def _not_found(_request: Request, exc: SessionNotFound) -> JSONResponse:
        return JSONResponse(status_code=404, content={"detail": str(exc)})

    @app.exception_handler(ValueError)
    async def _bad_value(_request: Request, exc: ValueError) -> JSONResponse:
        return JSONResponse(status_code=400, content={"detail": str(exc)})

    # ------------------------------------------------------------------ meta
    @app.get("/health")
    def health() -> dict[str, Any]:
        """Unauthenticated liveness probe, so the CLI can detect the daemon."""

        return {"ok": True, "version": __version__, "sources": list(SOURCES)}

    @app.get("/status", dependencies=guard)
    def status() -> dict[str, Any]:
        return recorder.status()

    @app.get("/doctor", dependencies=guard)
    def doctor() -> dict[str, Any]:
        return _doctor_response()

    @app.get("/sessions", dependencies=guard)
    def sessions() -> dict[str, Any]:
        return {
            "sessions": [
                {
                    "id": session.session_id,
                    "title": session.manifest.title,
                    "analyst": session.manifest.analyst,
                    "status": session.manifest.status,
                    "created_at": session.manifest.created_at,
                    "active_seconds": round(session.manifest.active_seconds, 1),
                    "paused_seconds": round(session.manifest.paused_seconds, 1),
                }
                for session in recorder.store.list_sessions()
            ]
        }

    @app.get("/sessions/{session_id}/events", dependencies=guard)
    def events(
        session_id: str,
        limit: int = Query(default=200, ge=1, le=MAX_EVENT_PAGE_SIZE),
        offset: int = Query(default=0, ge=0),
        tail: bool = False,
    ) -> dict[str, Any]:
        session = Session.load(session_id)
        total = _event_count(session)
        page_offset = max(0, total - limit) if tail else offset
        window = _read_event_page(session, offset=page_offset, limit=limit)
        return {
            "total": total,
            "offset": page_offset,
            "events": [_event_response(event) for event in window],
        }

    # ------------------------------------------------------------- lifecycle
    @app.post("/sessions/start", dependencies=guard)
    def start(payload: StartRequest) -> dict[str, Any]:
        return recorder.start_session(
            title=payload.title,
            analyst=payload.analyst,
            workflow_family=payload.workflow_family,
            tags=payload.tags,
            watch=[Path(p) for p in payload.watch],
            sources=payload.sources,
        )

    @app.post("/sessions/resume", dependencies=guard)
    def resume(payload: SessionRef) -> dict[str, Any]:
        return recorder.resume_session(payload.session_id)

    @app.post("/sessions/pause", dependencies=guard)
    def pause(payload: PauseRequest) -> dict[str, Any]:
        return recorder.pause_session(
            payload.session_id, reason=payload.reason, expect=payload.expect
        )

    @app.post("/sessions/stop", dependencies=guard)
    def stop(payload: SessionRef) -> dict[str, Any]:
        return recorder.stop_session(payload.session_id)

    # ---------------------------------------------------------------- toggles
    @app.post("/sources/{source}", dependencies=guard)
    def set_source(source: str, payload: SourceRequest) -> dict[str, Any]:
        return recorder.set_source(source, payload.enabled)

    @app.post("/shell-output", dependencies=guard)
    def shell_output(payload: SourceRequest) -> dict[str, Any]:
        return recorder.set_shell_output(payload.enabled)

    # ------------------------------------------------------------- user input
    @app.post("/notes", dependencies=guard)
    def note(payload: NoteRequest) -> dict[str, Any]:
        return recorder.add_note(
            payload.text,
            label=payload.label,
            pasted=payload.pasted,
            window=payload.window,
        )

    @app.post("/markers", dependencies=guard)
    def marker(payload: MarkerRequest) -> dict[str, Any]:
        return recorder.add_marker(payload.label, payload.detail)

    @app.post("/watch", dependencies=guard)
    def watch(payload: WatchRequest) -> dict[str, Any]:
        return recorder.add_watch_root(Path(payload.root))

    # ------------------------------------------------------------------- seal
    @app.post("/sessions/seal", dependencies=guard)
    def seal(payload: SealRequest) -> dict[str, Any]:
        """Mirror of ``/export`` for the GUI and the recorder skill.

        Runs in the API process, which is the daemon -- so this route is
        deliberately limited to the **regex** tier. A model fetch or an
        interactive confirmation has no tty here, and the build spec's rule is
        that the seal and any weight fetch run in the foreground CLI. For a
        model or LLM tier, the GUI points the operator at `wfrec seal`.
        """

        from autocab.deid.policy import Policy, RenderMode

        from .seal import SealError, seal_session, seal_status

        if payload.status:
            session = (
                recorder.store.resolve(payload.session_id)
                if payload.session_id
                else (recorder.session or recorder.store.resolve(None))
            )
            return seal_status(session.root)
        session = recorder.store.resolve(payload.session_id)
        try:
            result = seal_session(
                session,
                policy=Policy(profile=payload.profile, render=RenderMode.PSEUDONYMIZE),
                engine_label="regex",
                reseal=payload.reseal,
                force=payload.force,
                dry_run=payload.dry_run,
                stop_session=(
                    None
                    if not payload.force
                    else lambda: recorder.stop_session(session.session_id)
                ),
            )
        except SealError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return {"session": session.session_id, "dry_run": result.dry_run, **result.record}

    # ----------------------------------------------------------------- export
    @app.post("/export", dependencies=guard)
    def export(payload: ExportRequest) -> dict[str, Any]:
        from .exporters import export_session

        session = recorder.store.resolve(payload.session_id)
        return export_session(session, formats=payload.formats)

    # --------------------------------------------------------------------- UI
    @app.get("/", response_class=HTMLResponse)
    def index() -> str:
        from importlib import resources

        html = resources.files("wfrec.ui").joinpath("index.html").read_text(
            encoding="utf-8"
        )
        # The token is injected into the page rather than exposed as a URL
        # parameter, so it does not end up in shell history or a browser's
        # visible address bar after navigation.
        return html.replace("@WFREC_TOKEN@", token)

    return app
