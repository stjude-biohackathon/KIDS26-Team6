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

import secrets
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel, Field

from . import SOURCES, __version__
from .recorder import NoActiveSession, Recorder
from .session import SessionNotFound


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
        from .doctor import diagnose

        return diagnose()

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
    def events(session_id: str, limit: int = 200, offset: int = 0) -> dict[str, Any]:
        from .session import Session

        session = Session.load(session_id)
        all_events = session.writer.read()
        window = all_events[offset : offset + limit]
        return {
            "total": len(all_events),
            "events": [event.to_dict() for event in window],
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
