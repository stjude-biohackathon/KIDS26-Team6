"""Adapt shared redaction results to the two established return types.

An explicit name map preserves finding names such as ``sj_id`` and ``dob`` in
tests and exported skill metadata.
"""

from __future__ import annotations

from typing import Any, Sequence

from .labels import DeidLabel
from .spans import Span

#: label -> the name this rule has always reported. Additions here are a
#: compatibility decision and should be rare.
LEGACY_FINDING_NAMES: dict[str, str] = {
    DeidLabel.EMAIL.value: "email",
    DeidLabel.SUBJECT_ID.value: "sj_id",
    DeidLabel.MRN.value: "mrn",
    DeidLabel.DOB_LABELLED.value: "dob",
}


def finding_name(label: str) -> str:
    """The name a span contributes to ``findings``."""

    if label == DeidLabel.DENY.value:
        return "deny"
    return LEGACY_FINDING_NAMES.get(label, label.lower())


def mask_token(label: str) -> str:
    """The ``mask`` render-mode replacement.

    Uses the legacy name where one exists so that ``[REDACTED_DOB]`` and
    ``[REDACTED_SJ_ID]`` stay byte-identical to what downstream consumers and
    ``test_pipeline.py`` already expect. New labels get ``[REDACTED_<LABEL>]``.

    ``DENY`` is the one case where the mask token and the finding name diverge:
    the deny rule has always rendered ``[REDACTED_TERM]`` while reporting
    ``deny:<term>``, and both halves have consumers.
    """

    if label == DeidLabel.DENY.value:
        return "[REDACTED_TERM]"
    return f"[REDACTED_{LEGACY_FINDING_NAMES.get(label, label).upper()}]"


def deny_finding_name(detector: str) -> str:
    """``regex:deny:patient`` -> ``deny:patient``.

    The old redactor reported deny hits as ``deny:<term>`` and callers filter on
    that prefix, so the term has to survive into the finding name.
    """

    _, _, term = detector.partition("deny:")
    return f"deny:{term}" if term else "deny"


def findings_from_spans(spans: Sequence[Span]) -> list[str]:
    """Sorted, de-duplicated legacy finding names for ``spans``.

    Includes ``Span.absorbed``. The old redactor applied its four patterns
    sequentially, so ``https://x/SJ001234`` reported both ``url`` and ``sj_id``;
    the resolved form is a single ``URL`` hull, and without the absorbed labels
    the session would look like it never contained a subject identifier.
    ``test_recording_collectors.py`` asserts exactly this.
    """

    names: set[str] = set()
    for span in spans:
        if span.protect:
            continue
        if span.label == DeidLabel.DENY.value:
            names.add(deny_finding_name(span.detector))
        else:
            names.add(finding_name(span.label))
        for label in span.absorbed:
            names.add(finding_name(label))
    return sorted(names)


def to_redaction_report(redacted_text: str, spans: Sequence[Span]) -> Any:
    """Build an ``autocab.models.RedactionReport``.

    Imported lazily: ``autocab.deid`` must stay importable with stdlib alone so
    that neither the recorder nor the regex tier gains a dependency on the
    pipeline's dataclasses.
    """

    from ..models import RedactionReport

    return RedactionReport(redacted_text=redacted_text, findings=findings_from_spans(spans))


def to_redacted(redacted_text: str, spans: Sequence[Span], *, available: bool = True) -> Any:
    """Build a ``autocab.recording.redaction.Redacted``."""

    from autocab.recording.redaction import Redacted

    return Redacted(
        text=redacted_text,
        findings=findings_from_spans(spans),
        available=available,
    )
