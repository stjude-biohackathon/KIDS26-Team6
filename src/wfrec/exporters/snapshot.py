"""Create pattern-checked exports without mutating resumable sessions."""

from __future__ import annotations

import json
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from autocab.deid.policy import SEAL_POLICY

from ..events import EventWriter
from ..locking import file_lock
from ..seal import seal_session
from ..session import STATUS_PAUSED, STATUS_STOPPED, Manifest, Session
from . import export_session


class ExportStateError(RuntimeError):
    """Raised when a mutable session is not paused before export."""


def validate_export_state(session: Session, status: str | None = None) -> str:
    """Return the effective state when a session can be exported safely."""

    effective_status = status or session.manifest.status
    if session.writer.sealed:
        return effective_status
    if effective_status not in {STATUS_PAUSED, STATUS_STOPPED}:
        raise ExportStateError(
            "Pause or archive the selected session before exporting events."
        )
    return effective_status


def export_session_safely(
    session: Session,
    formats: list[str] | None = None,
    destination: Path | None = None,
    *,
    status: str | None = None,
) -> dict[str, Any]:
    """Export a sealed session directly or seal an isolated snapshot first."""

    source_status = validate_export_state(session, status)
    if session.writer.sealed:
        result = export_session(
            session,
            formats=formats,
            destination=destination,
            prefix_filenames=destination is not None,
        )
        result["privacy"] = {
            "checked": True,
            "method": "sealed-session",
            "source_status": source_status,
        }
        return result

    final_destination = destination or (session.root / "exports")
    with TemporaryDirectory(prefix="wfrec-export-") as temporary_directory:
        snapshot = _snapshot_session(
            session,
            Path(temporary_directory) / session.session_id,
            source_status,
        )
        seal_session(snapshot, policy=SEAL_POLICY, engine_label="regex")
        result = export_session(
            snapshot,
            formats=formats,
            destination=final_destination,
            prefix_filenames=destination is not None,
        )

    result["privacy"] = {
        "checked": True,
        "method": "protected-snapshot",
        "source_status": source_status,
    }
    return result


def _snapshot_session(
    session: Session,
    snapshot_root: Path,
    source_status: str,
) -> Session:
    """Copy the manifest and timeline while holding the timeline lock briefly."""

    manifest_path = session.root / "manifest.json"
    sequence_path = session.root / ".seq"
    with file_lock(session.root / ".events.lock"):
        manifest_payload = json.loads(manifest_path.read_text(encoding="utf-8"))
        timeline = session.writer.path.read_bytes()
        sequence = (
            sequence_path.read_text(encoding="utf-8")
            if sequence_path.exists()
            else "0"
        )

    manifest_payload["status"] = STATUS_STOPPED
    manifest_payload.setdefault("lifecycle", []).append(
        {
            "event": "export.snapshot",
            "source_status": source_status,
        }
    )
    snapshot_root.mkdir(parents=True)
    (snapshot_root / "manifest.json").write_text(
        json.dumps(manifest_payload, indent=2) + "\n",
        encoding="utf-8",
    )
    (snapshot_root / "events.jsonl").write_bytes(timeline)
    (snapshot_root / ".seq").write_text(sequence, encoding="utf-8")

    manifest = Manifest.from_dict(manifest_payload)
    snapshot = Session(manifest)
    snapshot.root = snapshot_root
    snapshot.writer = EventWriter(
        snapshot_root,
        session_id=manifest.session_id,
        analyst=manifest.analyst,
        host=manifest.host,
    )
    return snapshot
