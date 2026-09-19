"""Export the complete sealed session as one portable JSON document."""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ..session import Session


def build_events_document(
    session: Session,
    seal: dict[str, Any],
) -> tuple[dict[str, Any], int]:
    """Return session metadata, seal provenance, and every recorded event.

    Events stay in their canonical append order. The ``seq`` field preserves
    that order without changing timestamps produced by concurrent collectors.
    """

    events = [event.to_dict() for event in session.writer.read()]
    document = {
        "schema_version": 1,
        "session": session.manifest.to_dict(),
        "seal": dict(seal),
        "events": events,
    }
    return document, len(events)


def write_events_json(
    session: Session,
    seal: dict[str, Any],
    destination: Path | None = None,
) -> tuple[Path, int]:
    """Write the complete session document and return its path and event count."""

    document, count = build_events_document(session, seal)
    target = destination or (session.root / "exports" / "events.json")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(document, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    return target, count
