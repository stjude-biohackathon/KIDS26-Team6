"""Protect text that already uses an AutoCAB redaction format.

This engine runs first and protects masks, generalized values, and pseudonyms.
Grammar-based protection keeps repeated sealing stable. A real value that
matches a protected format also passes through, and tests track this limit.
"""

from __future__ import annotations

import re

from ..labels import PSEUDONYM_PREFIXES
from ..spans import Span
from .base import BaseEngine, DetectorInfo, table_digest

_PREFIX_ALT = "|".join(re.escape(prefix) for prefix in PSEUDONYM_PREFIXES)

PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    # A surrogate: `MRN_a7f3c1`. 6+ hex chars so a longer future surrogate width
    # is still recognized -- widening the HMAC output must not break reseal.
    ("surrogate", re.compile(rf"\b(?:{_PREFIX_ALT})_[0-9a-f]{{6,}}\b")),
    # A capture-time mask. Matched permissively on the label so a mask written by
    # an older version, with a label this build has since renamed, is still
    # protected.
    ("mask", re.compile(r"\[REDACTED_[A-Z_]+\]")),
    # A generalized date or age.
    # `unknown` is an accepted year: a two-digit source year is generalized to
    # `[DATE:unknown]` rather than pivot-guessed, and that token must be
    # protected too or a reseal would try to redact it.
    ("generalized_date", re.compile(r"\[DATE:(?:\d{4}|unknown)\]")),
    ("generalized_age", re.compile(r"\bAGE_90_PLUS\b")),
    # The removal notice the frame/sidecar path writes when it cannot verify a
    # file was screened.
    ("removal_notice", re.compile(r"\[REMOVED:[A-Za-z0-9_-]+\]")),
)

DETECTOR_KIND = "regex"


class SurrogateGuard(BaseEngine):
    """Emits ``protect`` spans. Never emits a redaction."""

    name = "surrogate_guard"
    kind = DETECTOR_KIND

    def info(self) -> DetectorInfo:
        return DetectorInfo(
            name=self.name,
            kind=self.kind,
            available=True,
            version=f"patterns={len(PATTERNS)}",
            detail={
                "patterns_sha256": table_digest(
                    (name, pattern.pattern) for name, pattern in PATTERNS
                )
            },
        )

    def detect_one(self, text: str) -> list[Span]:
        spans: list[Span] = []
        for name, pattern in PATTERNS:
            for match in pattern.finditer(text):
                spans.append(
                    Span(
                        start=match.start(),
                        end=match.end(),
                        label="PROTECTED",
                        detector=f"{self.name}:{name}",
                        score=1.0,
                        protect=True,
                    )
                )
        return spans


def protect_spans(text: str) -> list[Span]:
    """Convenience single-string entry point."""

    return SurrogateGuard().detect_one(text)


def is_sealed_surface(surface: str) -> bool:
    """Whether ``surface`` is *entirely* an already-sealed token.

    Used by the audit to classify a span as masked-at-capture rather than
    newly pseudonymized.
    """

    return any(pattern.fullmatch(surface) for _, pattern in PATTERNS)
