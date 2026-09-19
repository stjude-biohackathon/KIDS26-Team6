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
from importlib import resources
from ipaddress import ip_address
from itertools import islice
from pathlib import Path
from typing import Any, Literal

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response
from pydantic import BaseModel, Field

from . import SOURCES, __version__
from .desktop import (
    DirectoryOpenError,
    DirectorySelectionError,
    choose_directory,
    open_directory,
)
from .events import Event, SessionSealed
from .markdown import render_markdown
from .recorder import NoActiveSession, Recorder
from .session import (
    STATUS_ACTIVE,
    STATUS_PAUSED,
    STATUS_STOPPED,
    Session,
    SessionNotFound,
)
from .state import RecorderState, StateTransaction, shell_output_unavailable_reason


MAX_EVENT_PAGE_SIZE = 2_000


def _request_is_local(request: Request) -> bool:
    """Return whether a desktop action came from the daemon host."""

    if request.client is None:
        return False
    try:
        return ip_address(request.client.host).is_loopback
    except ValueError:
        return False


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


class RenameSessionRequest(BaseModel):
    title: str = Field(min_length=1, max_length=200)


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
    formats: list[str] = Field(default_factory=lambda: ["events", "autocab", "trace"])
    choose_destination: bool = False


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


def _display_status(session: Session, state: RecorderState) -> str:
    """Reconcile a stored manifest with the authoritative recorder state."""

    if session.session_id == state.active_session:
        return STATUS_ACTIVE
    if session.manifest.status == STATUS_STOPPED:
        return STATUS_STOPPED
    if session.session_id in state.paused or session.manifest.status in {
        STATUS_ACTIVE,
        STATUS_PAUSED,
    }:
        # A crashed or competing daemon can leave an active manifest behind.
        return STATUS_PAUSED
    return session.manifest.status


def _session_summary(session: Session, state: RecorderState) -> dict[str, Any]:
    """Build the bounded metadata used by session navigation and selection."""

    status = _display_status(session, state)
    if status == session.manifest.status:
        active_seconds, paused_seconds = session.manifest.duration_snapshot()
    else:
        active_seconds = session.manifest.active_seconds
        paused_seconds = session.manifest.paused_seconds
    return {
        "id": session.session_id,
        "title": session.manifest.title,
        "analyst": session.manifest.analyst,
        "workflow_family": session.manifest.workflow_family,
        "status": status,
        "created_at": session.manifest.created_at,
        "active_seconds": round(active_seconds, 1),
        "paused_seconds": round(paused_seconds, 1),
        "events": _event_count(session),
        "is_default": session.session_id == state.active_session,
        "sealed": session.writer.sealed,
    }


def _selected_status(recorder: Recorder, session: Session) -> dict[str, Any]:
    """Return UI state for one stored session without changing its lifecycle."""

    state = RecorderState.load()
    summary = _session_summary(session, state)
    live = recorder.status()
    is_active = summary["status"] == STATUS_ACTIVE
    live["session"] = {
        **summary,
        "root": str(session.root),
        "watch_roots": list(session.manifest.watch_roots),
        "shell_backend": session.manifest.shell_backend,
    }
    live["sources"] = dict(session.manifest.sources)
    live["shell_output"] = False
    live["shell_output_requested"] = session.manifest.shell_output
    live["shell_output_available"] = False
    live["shell_output_reason"] = shell_output_unavailable_reason(session.manifest.shell_backend)
    live["shell_backend"] = session.manifest.shell_backend
    if not is_active:
        # Collector state belongs to the live session, not the historical one
        # selected for inspection.
        live["collectors"] = {}
        live["running"] = dict.fromkeys(SOURCES, False)
        live.pop("pause_reason", None)
    return live


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
        supplied = (
            header[7:]
            if header.lower().startswith("bearer ")
            else (request.query_params.get("token") or "")
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

    @app.exception_handler(SessionSealed)
    async def _sealed(_request: Request, exc: SessionSealed) -> JSONResponse:
        return JSONResponse(status_code=409, content={"detail": str(exc)})

    @app.exception_handler(ValueError)
    async def _bad_value(_request: Request, exc: ValueError) -> JSONResponse:
        return JSONResponse(status_code=400, content={"detail": str(exc)})

    # ------------------------------------------------------------------ meta
    @app.get("/health")
    def health() -> dict[str, Any]:
        """Unauthenticated liveness probe, so the CLI can detect the daemon."""

        return {"ok": True, "version": __version__, "sources": list(SOURCES)}

    @app.get("/status", dependencies=guard)
    def status(session_id: str | None = None) -> dict[str, Any]:
        if session_id is None:
            return recorder.status()
        return _selected_status(recorder, recorder.store.resolve(session_id))

    @app.get("/doctor", dependencies=guard)
    def doctor() -> dict[str, Any]:
        return _doctor_response()

    @app.get("/sessions", dependencies=guard)
    def sessions() -> dict[str, Any]:
        state = RecorderState.load()
        trashed = set(state.trashed_sessions)
        return {
            "sessions": [
                _session_summary(session, state)
                for session in recorder.store.list_sessions()
                if session.session_id not in trashed
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

    @app.patch("/sessions/{session_id}", dependencies=guard)
    def rename_session(session_id: str, payload: RenameSessionRequest) -> dict[str, str]:
        return recorder.rename_session(session_id, payload.title)

    @app.post("/sessions/{session_id}/trash", dependencies=guard)
    def trash_session(session_id: str) -> dict[str, Any]:
        """Hide one archived session without changing its recorded files."""

        session = recorder.store.resolve(session_id)
        if _display_status(session, RecorderState.load()) != STATUS_STOPPED:
            raise HTTPException(
                status_code=409,
                detail="Only archived sessions can be removed from the dashboard.",
            )
        with StateTransaction() as state:
            if session_id not in state.trashed_sessions:
                state.trashed_sessions.append(session_id)
        return {"id": session_id, "trashed": True}

    @app.post("/sessions/{session_id}/open-folder", dependencies=guard)
    def open_session_folder(session_id: str, request: Request) -> dict[str, Any]:
        """Open a trusted session directory on the daemon's desktop host."""

        if not _request_is_local(request):
            raise HTTPException(
                status_code=409,
                detail="Session folders can only be opened from the daemon host.",
            )
        session = recorder.store.resolve(session_id)
        try:
            open_directory(session.root)
        except DirectoryOpenError as error:
            raise HTTPException(status_code=503, detail=str(error)) from error
        return {"id": session_id, "opened": True}

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
                    None if not payload.force else lambda: recorder.stop_session(session.session_id)
                ),
            )
        except SealError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return {"session": session.session_id, "dry_run": result.dry_run, **result.record}

    # ----------------------------------------------------------------- export
    @app.post("/export", dependencies=guard)
    def export(payload: ExportRequest, request: Request) -> dict[str, Any]:
        from .exporters.snapshot import (
            ExportStateError,
            export_session_safely,
            validate_export_state,
        )
        from .seal import SealError

        session = recorder.store.resolve(payload.session_id)
        status = _display_status(session, RecorderState.load())
        destination = None
        try:
            validate_export_state(session, status)
        except ExportStateError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        if payload.choose_destination:
            if not _request_is_local(request):
                raise HTTPException(
                    status_code=409,
                    detail=("Export folders can only be chosen on the daemon host."),
                )
            try:
                destination = choose_directory()
            except DirectorySelectionError as error:
                raise HTTPException(status_code=503, detail=str(error)) from error
            if destination is None:
                return {
                    "session": session.session_id,
                    "cancelled": True,
                    "written": [],
                    "details": {},
                }

        try:
            result = export_session_safely(
                session,
                formats=payload.formats,
                destination=destination,
                status=status,
            )
        except (ExportStateError, SealError) as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        return {**result, "cancelled": False}

    # --------------------------------------------------------------------- UI
    def ui_asset(filename: str, media_type: str) -> Response:
        content = resources.files("wfrec.ui").joinpath(filename).read_text(encoding="utf-8")
        return Response(
            content=content,
            media_type=media_type,
            headers={"Cache-Control": "no-cache"},
        )

    @app.get("/styles.css", response_class=Response)
    def styles() -> Response:
        return ui_asset("styles.css", "text/css")

    @app.get("/dashboard.css", response_class=Response)
    def dashboard_styles() -> Response:
        return ui_asset("dashboard.css", "text/css")

    @app.get("/app.js", response_class=Response)
    def javascript() -> Response:
        return ui_asset("app.js", "application/javascript")

    @app.get("/dashboard.js", response_class=Response)
    def dashboard_javascript() -> Response:
        return ui_asset("dashboard.js", "application/javascript")

    @app.get("/", response_class=HTMLResponse)
    def index() -> str:
        html = resources.files("wfrec.ui").joinpath("index.html").read_text(encoding="utf-8")
        # The token is injected into the page rather than exposed as a URL
        # parameter, so it does not end up in shell history or a browser's
        # visible address bar after navigation.
        return html.replace("@WFREC_TOKEN@", token)

    return app
