"""Normalize, validate, resolve, and render detected text spans.

Offsets use half-open NFC code-point positions. The seal stops when offsets
fall outside the source text. NFC preserves text length better than NFKC, which
keeps replacement offsets stable.
"""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass, replace
from typing import Callable, Iterable, Protocol, Sequence

from .labels import DETECTOR_RANK, LABEL_RANK


class OffsetError(ValueError):
    """An offset outside ``[0, len(text)]``. Fatal by design -- see module docs."""


@dataclass(frozen=True, slots=True)
class Span:
    """One detected region of one string."""

    start: int
    end: int
    label: str
    detector: str
    score: float = 1.0
    protect: bool = False
    """Nothing may overlap this region. Emitted by ``SurrogateGuard`` over text
    that is *already* sealed (a surrogate, a ``[REDACTED_*]`` mask, a
    ``[DATE:yyyy]`` generalization) so that a reseal is a fixpoint."""

    absorbed: tuple[str, ...] = ()
    """Labels of the spans this hull swallowed during :func:`resolve`.

    The sweep merges overlaps into the outer hull and never splits, which means
    the hull carries one label -- ``https://x/SJ001234`` resolves to a single
    ``URL`` span even though a ``SUBJECT_ID`` was nested inside it. The nested
    *label* still matters: it is what the audit and the legacy ``findings`` list
    report, and dropping it would make a session look like it contained no
    subject identifier. Coverage is unaffected either way; only the accounting
    was lossy."""

    def __post_init__(self) -> None:
        # Only *malformedness* is rejected here. A `Span` does not know its
        # text, so it cannot judge whether an offset is in bounds -- the two
        # functions that do know the text handle that, and they handle it
        # differently on purpose: `resolve` clips (a detector overshooting by a
        # character should not abandon a whole batch) and `check_bounds`, which
        # the seal calls immediately before staging a replacement, *aborts*
        # (invariant 3 -- a rewrite applied to the wrong region looks like
        # success). Rejecting a negative start here would make `resolve`'s clip
        # unreachable and move the failure to a place with no text to report.
        if self.end < self.start:
            raise OffsetError(f"malformed span offsets: {self.start}..{self.end}")

    @property
    def length(self) -> int:
        return self.end - self.start

    def surface(self, text: str) -> str:
        return text[self.start : self.end]

    def overlaps(self, other: "Span") -> bool:
        """Strict overlap. Adjacency (``a.end == b.start``) is *not* overlap --
        two touching spans of different labels stay separate."""

        return self.start < other.end and other.start < self.end

    def to_dict(self) -> dict[str, object]:
        return {
            "start": self.start,
            "end": self.end,
            "label": self.label,
            "detector": self.detector,
            "score": round(float(self.score), 4),
            "protect": self.protect,
            **({"absorbed": list(self.absorbed)} if self.absorbed else {}),
        }


@dataclass(frozen=True, slots=True)
class DetectorInfo:
    """What a detector reports about itself for the seal record and ``doctor``."""

    name: str
    kind: str
    """``regex`` | ``model`` | ``llm`` -- drives ``DETECTOR_RANK``."""
    available: bool
    version: str = ""
    reason: str = ""
    """Why it is unavailable. Rendered as ``unavailable: <reason>``; the seal
    then fails closed rather than degrading the way capture-time redaction does."""
    detail: dict[str, object] | None = None
    """Integrity material for ``seal.json``: ``patterns_sha256`` for the regex
    tier, pinned revision + ``weights_sha256`` for a model tier."""

    def to_dict(self) -> dict[str, object]:
        out: dict[str, object] = {
            "name": self.name,
            "kind": self.kind,
            "available": self.available,
        }
        if self.version:
            out["version"] = self.version
        if not self.available:
            out["unavailable"] = self.reason or "unknown"
        if self.detail:
            out.update(self.detail)
        return out


class Detector(Protocol):
    """Batch-shaped **by contract**.

    ``detect`` takes a sequence and returns one list of spans per input. There
    is no single-string entry point on purpose: with one, some call site
    eventually invokes a model once per string across 30k events and the seal
    takes hours. ``find_spans`` implements the batch as a trivial loop.
    """

    name: str

    def info(self) -> DetectorInfo: ...

    def detect(self, texts: Sequence[str]) -> list[list[Span]]: ...


def normalize_text(text: str) -> str:
    """NFC-normalize. Idempotent, and the precondition for every offset."""

    return unicodedata.normalize("NFC", text)


def check_bounds(text: str, spans: Iterable[Span]) -> None:
    """Raise :class:`OffsetError` on any span outside ``text``.

    Called by the seal *before* it stages a replacement. Invariant 3.
    """

    limit = len(text)
    for span in spans:
        if span.start < 0 or span.end > limit or span.end < span.start:
            raise OffsetError(
                f"span {span.label}[{span.start}:{span.end}] from "
                f"{span.detector!r} is outside text of length {limit}"
            )


def _sort_key(span: Span) -> tuple[int, int, int, int, str, str]:
    return (
        span.start,
        -span.end,
        LABEL_RANK.get(span.label, 10_000),
        DETECTOR_RANK.get(span.detector.split(":", 1)[0], 10_000),
        span.label,
        span.detector,
    )


def resolve(
    text: str,
    candidates: Iterable[Span],
    *,
    keeps: Callable[[str, "Span"], bool] | None = None,
) -> list[Span]:
    """Reduce overlapping candidate spans to a disjoint, ordered plan.

    ``keeps`` is the allowlist predicate, called as ``keeps(text, span)``. It
    gets the *whole* string, not just the surface, because several
    bioinformatics false positives are only recognizable in context -- the
    ``2024-01-15`` inside ``biocontainers/gatk:4.5.0.0--2024-01-15`` is
    indistinguishable from a real date until you can see the token around it.

    Order matters and is the spec's:

    1. clip to bounds and drop empties;
    2. **drop** -- never clip -- any candidate intersecting a ``protect`` span;
    3. drop allowlisted surfaces;
    4. sort by ``(start, -end, LABEL_RANK, DETECTOR_RANK, label, detector)``;
    5. sweep, absorbing overlaps into the **outer hull**.

    Step 5 never splits. Splitting a nested overlap turns one string into
    ``NAME_ab12 MRN_cd34``, which is both noisier to read and a structural leak:
    it tells a reader the original contained a name followed by an MRN.
    """

    limit = len(text)
    clipped: list[Span] = []
    for span in candidates:
        start = max(0, min(span.start, limit))
        end = max(0, min(span.end, limit))
        if end <= start:
            continue
        clipped.append(
            span if (start, end) == (span.start, span.end) else replace(span, start=start, end=end)
        )

    protected = [span for span in clipped if span.protect]
    if protected:
        survivors: list[Span] = []
        for span in clipped:
            if span.protect:
                survivors.append(span)
                continue
            if any(span.overlaps(guard) for guard in protected):
                continue
            survivors.append(span)
        clipped = survivors

    if keeps is not None:
        clipped = [span for span in clipped if span.protect or not keeps(text, span)]

    clipped.sort(key=_sort_key)

    resolved: list[Span] = []
    for span in clipped:
        if not resolved:
            resolved.append(span)
            continue
        current = resolved[-1]
        if span.start >= current.end:
            resolved.append(span)
            continue
        # Overlap: absorb into the outer hull. `current` already won the sort,
        # so it keeps label and detector; the hull only ever grows rightward
        # because the sweep is start-ordered. The swallowed label is retained in
        # `absorbed` -- see the field docstring.
        merged_labels = dict.fromkeys((*current.absorbed, span.label, *span.absorbed))
        merged_labels.pop(current.label, None)
        resolved[-1] = replace(
            current,
            end=max(current.end, span.end),
            score=max(current.score, span.score),
            protect=current.protect or span.protect,
            absorbed=tuple(merged_labels),
        )
    return resolved


def render(
    text: str,
    spans: Sequence[Span],
    replacement: Callable[[Span, str], str],
) -> str:
    """Apply ``replacement`` to each span, **right to left**.

    Right-to-left is what keeps every not-yet-applied offset valid without
    bookkeeping. ``spans`` must already be disjoint and sorted -- i.e. the
    output of :func:`resolve`. ``protect`` spans are skipped: they mark regions
    that are already sealed.
    """

    check_bounds(text, spans)
    out = text
    for span in sorted(spans, key=lambda s: s.start, reverse=True):
        if span.protect:
            continue
        out = out[: span.start] + replacement(span, span.surface(text)) + out[span.end :]
    return out


def coverage(spans: Iterable[Span]) -> set[int]:
    """The set of character indices any span covers.

    The scorer's ``P`` -- the union over *all* predicted spans regardless of
    label, because for safety purposes a region is either rewritten or it is not.
    """

    covered: set[int] = set()
    for span in spans:
        covered.update(range(span.start, span.end))
    return covered
