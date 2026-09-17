"""Idempotency by grammar: mark already-sealed text as untouchable.

Always runs **first**, and no flag substitutes for it. It emits ``protect=True``
spans over every replacement vocabulary the system can produce -- a surrogate, a
capture-time ``[REDACTED_*]`` mask, a ``[DATE:yyyy]`` generalization -- and
``spans.resolve`` *drops* any candidate intersecting one.

The consequence is the property a reseal depends on: **sealed text is a
fixpoint.** Run the seal twice and the second pass changes nothing, because
``MRN_a7f3c1`` is protected rather than being re-detected as a nine-character
identifier and pseudonymized into ``ID_44b201``.

Note that this is idempotency by *grammar*, not by *key*. The seal key is
ephemeral and gone by the time a reseal runs, so a key-based check is
impossible; recognizing the shape is what remains, and it works across
generations and across engines.

**The limit that follows from that, stated plainly.** A real value that happens
to *look* like a surrogate is protected and passes through unredacted. A path
component ``MRN_4492000`` is indistinguishable from a legitimate surrogate --
``MRN_`` plus seven hex characters -- so the guard keeps it, and any candidate
overlapping it is dropped.

This is accepted, not overlooked, and the alternatives are worse:

* keying idempotency on the seal key is impossible, as above;
* narrowing the pattern to exactly 12 hex characters would break every seal
  written with a different surrogate width, including any future widening;
* dropping the guard entirely would make a reseal pseudonymize its own
  pseudonyms, so ``MRN_a7f3c1...`` becomes ``ID_44b201...`` and the linkage a
  reader depends on silently changes on every pass.

The exposure is small because the collision has to be exact: the prefix must be
one of ``PSEUDONYM_PREFIXES``, the separator an underscore, and every remaining
character a lowercase hex digit with at least six of them. ``MRN 4492000`` --
the way a human writes it -- is not affected. It is called out here, and in
``tests/test_deid_payload_coverage.py``, so nobody rediscovers it as a surprise.
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
