"""De-identification for AutoCAB and wfrec.

One detector, one taxonomy, one offset convention. The pipeline's
``SensitiveDataRedactor`` and the recorder's inline capture path both come
through here, so a pattern added once is in force on every path.

Typical use::

    from autocab.deid import mask_text
    text, spans = mask_text("Send to a@example.org with MRN 123456")

The seal uses the lower-level pieces directly (``detect`` ->
``spans.resolve`` -> ``spans.render`` with a ``Pseudonymizer``), because it
needs the span list for the audit and for frame box mapping.

**What this does not do.** It detects and rewrites text. It is not a HIPAA Safe
Harbor determination: Safe Harbor requires all 18 identifier classes removed
*and* no actual knowledge that the residue is identifiable, and a recall number
establishes neither. ``docs/deid-evaluation.md`` states the measured limits.
"""

from __future__ import annotations

from typing import Sequence

from .allowlist import Allowlist, shared as shared_allowlist
from .compat import findings_from_spans, mask_token
from .labels import ALL_LABELS, DeidLabel
from .policy import Action, Policy, RenderMode
from .pseudonym import Pseudonymizer
from .spans import (
    Detector,
    DetectorInfo,
    OffsetError,
    Span,
    check_bounds,
    coverage,
    normalize_text,
    render,
    resolve,
)
from .engines.base import EngineUnavailable
from .engines.regex_rules import RegexRules, find_spans, find_spans_one, patterns_sha256
from .engines.surrogate_guard import SurrogateGuard

__all__ = [
    "ALL_LABELS",
    "Action",
    "Allowlist",
    "DeidLabel",
    "Detector",
    "DetectorInfo",
    "EngineUnavailable",
    "OffsetError",
    "Policy",
    "Pseudonymizer",
    "RegexRules",
    "RenderMode",
    "Span",
    "SurrogateGuard",
    "check_bounds",
    "coverage",
    "detect",
    "find_spans",
    "find_spans_one",
    "findings_from_spans",
    "mask_text",
    "mask_token",
    "normalize_text",
    "patterns_sha256",
    "render",
    "resolve",
]


def detect(
    texts: Sequence[str],
    *,
    detectors: Sequence[Detector] | None = None,
    deny_terms: Sequence[str] = (),
    allowlist: Allowlist | None = None,
    guard: bool = True,
) -> list[list[Span]]:
    """Run the detector stack over ``texts`` and resolve each result.

    ``surrogate_guard`` and ``regex_rules`` always run -- ``detectors`` *adds*
    tiers, it does not replace them. ``guard=False`` exists only for the
    scorer, which measures the regex tier against text that contains no
    surrogates and where a protect span would silently suppress a true positive.

    Inputs are NFC-normalized first, so returned offsets index the normalized
    string. Callers that will rewrite the text must rewrite the same normalized
    string -- use :func:`normalize_text` on the way in and keep the result.
    """

    stack: list[Detector] = []
    if guard:
        stack.append(SurrogateGuard())
    stack.append(RegexRules(deny_terms))
    stack.extend(detectors or ())

    normalized = [normalize_text(text) for text in texts]
    keeps = (allowlist or shared_allowlist()).keeps

    per_detector = [detector.detect(normalized) for detector in stack]
    out: list[list[Span]] = []
    for index, text in enumerate(normalized):
        candidates: list[Span] = []
        for results in per_detector:
            candidates.extend(results[index])
        out.append(resolve(text, candidates, keeps=keeps))
    return out


def mask_text(
    text: str,
    *,
    deny_terms: Sequence[str] = (),
    detectors: Sequence[Detector] | None = None,
    allowlist: Allowlist | None = None,
) -> tuple[str, list[Span]]:
    """Mask ``text`` in place, returning the rewritten string and its spans.

    ``mask``, not ``pseudonymize``: only the seal pseudonymizes, because a
    pseudonym needs a key stable across processes and there is no safe place to
    keep one. See :class:`~autocab.deid.policy.RenderMode`.
    """

    normalized = normalize_text(text)
    if not normalized:
        return normalized, []
    spans = detect([normalized], detectors=detectors, deny_terms=deny_terms, allowlist=allowlist)[0]
    return render(normalized, spans, lambda span, _surface: mask_token(span.label)), spans
