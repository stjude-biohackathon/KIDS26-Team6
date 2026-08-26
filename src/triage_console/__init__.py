"""Compatibility wrapper for the hackathon review queue."""

from __future__ import annotations

from autocab.framework.components import InMemoryReviewService
from autocab.models import ReviewDecision, SkillProposal


class ReviewQueue:
    """Store pending proposals and apply human review decisions."""

    def __init__(self) -> None:
        self._service = InMemoryReviewService()

    def submit(self, proposal: SkillProposal) -> None:
        self._service.submit(proposal)

    def apply_decision(self, proposal_id: str, decision: ReviewDecision) -> SkillProposal:
        return self._service.decide(proposal_id, decision)

    def list_pending(self) -> list[SkillProposal]:
        return self._service.list_pending()
