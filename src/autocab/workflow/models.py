"""Persistent state for the reviewed session-to-skill workflow."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from autocab.recording.events import utc_now


class WorkflowError(RuntimeError):
    """Base error for forge-run lifecycle failures."""


class InvalidTransition(WorkflowError):
    """A forge run was asked to enter an invalid state."""


class ForgeState(StrEnum):
    """Human-review states for a persistent forge run."""

    DRAFT = "draft"
    BLOCKED = "blocked"
    NEEDS_REVIEW = "needs_review"
    APPROVED = "approved"
    PACKAGED = "packaged"


ALLOWED_TRANSITIONS: dict[ForgeState, frozenset[ForgeState]] = {
    ForgeState.DRAFT: frozenset({ForgeState.BLOCKED, ForgeState.NEEDS_REVIEW}),
    ForgeState.BLOCKED: frozenset({ForgeState.NEEDS_REVIEW}),
    ForgeState.NEEDS_REVIEW: frozenset({ForgeState.BLOCKED, ForgeState.APPROVED}),
    ForgeState.APPROVED: frozenset({ForgeState.BLOCKED, ForgeState.PACKAGED}),
    ForgeState.PACKAGED: frozenset(),
}


@dataclass(slots=True)
class ForgeRun:
    """Summary and audit state stored in one run directory."""

    run_id: str
    session_ids: list[str]
    state: ForgeState = ForgeState.DRAFT
    created_at: str = field(default_factory=utc_now)
    updated_at: str = field(default_factory=utc_now)
    evidence_count: int = 0
    unresolved_count: int = 0
    source_seals: dict[str, dict[str, Any]] = field(default_factory=dict)
    package_path: str | None = None
    approved_by: str | None = None
    approved_at: str | None = None
    history: list[dict[str, str]] = field(default_factory=list)

    def transition(self, state: ForgeState, *, reason: str) -> None:
        """Enter an allowed state and append an auditable history record."""

        if state == self.state:
            return
        if state not in ALLOWED_TRANSITIONS[self.state]:
            raise InvalidTransition(f"Cannot move forge run from {self.state} to {state}.")
        timestamp = utc_now()
        self.history.append(
            {
                "from": self.state.value,
                "to": state.value,
                "at": timestamp,
                "reason": reason,
            }
        )
        self.state = state
        self.updated_at = timestamp

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-ready representation."""

        return {
            "run_id": self.run_id,
            "session_ids": list(self.session_ids),
            "state": self.state.value,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "evidence_count": self.evidence_count,
            "unresolved_count": self.unresolved_count,
            "source_seals": self.source_seals,
            "package_path": self.package_path,
            "approved_by": self.approved_by,
            "approved_at": self.approved_at,
            "history": list(self.history),
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "ForgeRun":
        """Restore a forge run from persisted JSON."""

        return cls(
            run_id=str(payload["run_id"]),
            session_ids=[str(value) for value in payload["session_ids"]],
            state=ForgeState(payload["state"]),
            created_at=str(payload["created_at"]),
            updated_at=str(payload["updated_at"]),
            evidence_count=int(payload.get("evidence_count", 0)),
            unresolved_count=int(payload.get("unresolved_count", 0)),
            source_seals=dict(payload.get("source_seals") or {}),
            package_path=payload.get("package_path"),
            approved_by=payload.get("approved_by"),
            approved_at=payload.get("approved_at"),
            history=list(payload.get("history") or []),
        )
