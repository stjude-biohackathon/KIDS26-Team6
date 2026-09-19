"""Measure PHI coverage on the original text.

Strict recall requires predictions to cover each full gold span and controls
the gate. Partial recall shows clipped matches. Label accuracy and the
over-redaction ceiling measure utility while keeping missed PHI visible.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, Mapping, Sequence

from ..labels import HIPAA_BY_LABEL
from ..spans import Span, coverage
from .corpus import Corpus, GoldSpan, Record

#: One prediction: the rewritten text and the spans that produced it. The
#: rewritten text is needed for ``must_survive`` and for the independent
#: post-check; the spans for everything else.
Prediction = tuple[str, Sequence[Span]]


@dataclass
class LabelScore:
    """Per-label tallies. Rates are computed, never stored, so they cannot drift
    out of sync with the counts a reviewer is checking them against."""

    label: str
    gold: int = 0
    strict: int = 0
    partial: int = 0
    label_correct: int = 0

    @property
    def recall_strict(self) -> float:
        return self.strict / self.gold if self.gold else 0.0

    @property
    def recall_partial(self) -> float:
        return self.partial / self.gold if self.gold else 0.0

    @property
    def clip_gap(self) -> float:
        """``recall_partial - recall_strict``. Non-zero means clipping."""

        return self.recall_partial - self.recall_strict

    @property
    def label_accuracy(self) -> float:
        return self.label_correct / self.strict if self.strict else 0.0

    def to_dict(self) -> dict[str, object]:
        return {
            "label": self.label,
            "hipaa": HIPAA_BY_LABEL.get(self.label),
            "gold": self.gold,
            "strict": self.strict,
            "partial": self.partial,
            "recall_strict": round(self.recall_strict, 4),
            "recall_partial": round(self.recall_partial, 4),
            "clip_gap": round(self.clip_gap, 4),
            "label_accuracy": round(self.label_accuracy, 4),
        }


@dataclass
class Leak:
    """One strict miss.

    Safe to print with the gold text because the corpus is synthetic and every
    reservable value is in a reserved range. A **full** leak table in the
    generated doc is what turns "recall is 0.94" into six lines somebody can
    actually go and fix.
    """

    record_id: str
    channel: str
    difficulty: str
    label: str
    text: str
    start: int
    end: int
    partial: bool

    def to_dict(self) -> dict[str, object]:
        return {
            "record_id": self.record_id,
            "channel": self.channel,
            "difficulty": self.difficulty,
            "label": self.label,
            "text": self.text,
            "start": self.start,
            "end": self.end,
            "partial_hit": self.partial,
        }


@dataclass
class Scorecard:
    """The whole measurement. Serializes to the committed snapshot.

    **No nondeterministic fields.** No timestamp, no hostname, no wall-clock
    latency. A snapshot that churns on every run is a snapshot nobody reads, and
    a snapshot nobody reads cannot catch a slide from 0.98 to 0.91.
    """

    engine: str
    corpus_fingerprint: str
    records: int = 0
    gold_spans: int = 0
    predicted_spans: int = 0

    by_label: dict[str, LabelScore] = field(default_factory=dict)
    by_label_gated: dict[str, LabelScore] = field(default_factory=dict)
    by_difficulty: dict[str, LabelScore] = field(default_factory=dict)
    by_channel: dict[str, LabelScore] = field(default_factory=dict)
    by_hipaa: dict[str, LabelScore] = field(default_factory=dict)

    safe_records: int = 0
    total_characters: int = 0
    gold_characters: int = 0
    predicted_characters: int = 0
    over_redacted_characters: int = 0

    negative_records: int = 0
    negative_false_positive_spans: int = 0
    must_survive_total: int = 0
    must_survive_violations: list[dict[str, str]] = field(default_factory=list)
    leaks: list[Leak] = field(default_factory=list)

    # -- headline ----------------------------------------------------------
    @property
    def recall_strict(self) -> float:
        gold = sum(score.gold for score in self.by_label.values())
        strict = sum(score.strict for score in self.by_label.values())
        return strict / gold if gold else 0.0

    @property
    def recall_strict_gated(self) -> float:
        gold = sum(score.gold for score in self.by_label_gated.values())
        strict = sum(score.strict for score in self.by_label_gated.values())
        return strict / gold if gold else 0.0

    @property
    def safe_record_rate(self) -> float:
        """Share of records with **zero** strict misses.

        The headline a reviewer should read first: per-span recall of 0.95 sounds
        reassuring, but if the misses are spread one-per-record across a corpus
        where every record is a session, every session leaked."""

        return self.safe_records / self.records if self.records else 0.0

    @property
    def leaks_per_1000_records(self) -> float:
        return 1000.0 * len(self.leaks) / self.records if self.records else 0.0

    @property
    def over_redaction_rate(self) -> float:
        """Over-redacted characters as a share of **all** corpus characters.

        Denominator is the corpus, not ``|P|``, so the number reads as "this
        share of the text was destroyed unnecessarily" and stays stable as
        recall improves. ``over_redaction_share_of_predicted`` is reported
        alongside it for the other reading; only this one is gated."""

        return (
            self.over_redacted_characters / self.total_characters if self.total_characters else 0.0
        )

    @property
    def over_redaction_share_of_predicted(self) -> float:
        return (
            self.over_redacted_characters / self.predicted_characters
            if self.predicted_characters
            else 0.0
        )

    @property
    def false_positive_spans_per_negative_record(self) -> float:
        return (
            self.negative_false_positive_spans / self.negative_records
            if self.negative_records
            else 0.0
        )

    def headline(self) -> dict[str, object]:
        return {
            "engine": self.engine,
            "corpus_fingerprint": self.corpus_fingerprint,
            "records": self.records,
            "gold_spans": self.gold_spans,
            "predicted_spans": self.predicted_spans,
            "safe_record_rate": round(self.safe_record_rate, 4),
            "leaks_per_1000_records": round(self.leaks_per_1000_records, 2),
            "recall_strict_overall": round(self.recall_strict, 4),
            "recall_strict_gated": round(self.recall_strict_gated, 4),
            "over_redaction_rate": round(self.over_redaction_rate, 4),
            "over_redaction_share_of_predicted": round(self.over_redaction_share_of_predicted, 4),
            "false_positive_spans_per_negative_record": round(
                self.false_positive_spans_per_negative_record, 4
            ),
            "must_survive_total": self.must_survive_total,
            "must_survive_violations": len(self.must_survive_violations),
        }

    def to_dict(self) -> dict[str, object]:
        def table(scores: Mapping[str, LabelScore]) -> list[dict[str, object]]:
            return [scores[key].to_dict() for key in sorted(scores)]

        return {
            "headline": self.headline(),
            "by_label": table(self.by_label),
            "by_label_gated": table(self.by_label_gated),
            "by_difficulty": table(self.by_difficulty),
            "by_channel": table(self.by_channel),
            "by_hipaa": table(self.by_hipaa),
            "must_survive_violations": sorted(
                self.must_survive_violations, key=lambda item: (item["record_id"], item["text"])
            ),
            "leaks": [
                leak.to_dict()
                for leak in sorted(self.leaks, key=lambda leak: (leak.record_id, leak.start))
            ],
        }


def _bump(
    table: dict[str, LabelScore],
    key: str,
    *,
    strict: bool,
    partial: bool,
    label_ok: bool,
) -> None:
    score = table.setdefault(key, LabelScore(label=key))
    score.gold += 1
    score.strict += int(strict)
    score.partial += int(partial)
    score.label_correct += int(strict and label_ok)


def _label_matches(gold: GoldSpan, predicted: Sequence[Span]) -> bool:
    """Whether the best-overlapping prediction agrees on the label.

    ``Span.absorbed`` counts as agreement: ``https://x/SJ001234`` resolves to a
    single ``URL`` hull that swallowed a ``SUBJECT_ID``, and calling that a
    mislabel would penalize the hull rule for doing the right thing.
    """

    gold_range = range(gold.start, gold.end)
    gold_set = set(gold_range)
    best: Span | None = None
    best_overlap = 0
    for span in predicted:
        overlap = len(gold_set & set(range(span.start, span.end)))
        if overlap > best_overlap:
            best, best_overlap = span, overlap
    if best is None:
        return False
    return gold.label == best.label or gold.label in best.absorbed


def score(
    corpus: Corpus | Iterable[Record],
    predictions: Sequence[Prediction],
    *,
    engine: str,
    corpus_fingerprint: str = "",
) -> Scorecard:
    """Score ``predictions`` against ``corpus``, index-aligned.

    Aligned by position rather than by record id so that a predictor which
    silently drops a record produces a loud length mismatch instead of a
    quietly smaller denominator.
    """

    records = tuple(corpus)
    if len(records) != len(predictions):
        raise ValueError(
            f"{len(predictions)} predictions for {len(records)} records; "
            "the scorer will not infer an alignment"
        )
    if isinstance(corpus, Corpus) and not corpus_fingerprint:
        corpus_fingerprint = corpus.fingerprint

    card = Scorecard(engine=engine, corpus_fingerprint=corpus_fingerprint)

    for record, (rendered, predicted) in zip(records, predictions):
        card.records += 1
        card.total_characters += len(record.text)
        redacted = coverage(span for span in predicted if not span.protect)
        card.predicted_spans += sum(1 for span in predicted if not span.protect)
        card.predicted_characters += len(redacted)

        gold_covered: set[int] = set()
        record_is_safe = True
        for gold in record.spans:
            card.gold_spans += 1
            gold_set = set(range(gold.start, gold.end))
            gold_covered |= gold_set
            hit = gold_set & redacted
            strict = gold_set <= redacted
            partial = bool(hit)
            label_ok = _label_matches(gold, predicted)

            hipaa = HIPAA_BY_LABEL.get(gold.label)
            hipaa_key = f"hipaa-{hipaa:02d}" if isinstance(hipaa, int) else "hipaa-none"
            for table, key in (
                (card.by_label, gold.label),
                (card.by_difficulty, record.difficulty),
                (card.by_channel, record.channel),
                (card.by_hipaa, hipaa_key),
            ):
                _bump(table, key, strict=strict, partial=partial, label_ok=label_ok)
            if record.gated:
                _bump(
                    card.by_label_gated,
                    gold.label,
                    strict=strict,
                    partial=partial,
                    label_ok=label_ok,
                )

            if not strict:
                record_is_safe = False
                card.leaks.append(
                    Leak(
                        record_id=record.id,
                        channel=record.channel,
                        difficulty=record.difficulty,
                        label=gold.label,
                        text=gold.text,
                        start=gold.start,
                        end=gold.end,
                        partial=partial,
                    )
                )

        card.gold_characters += len(gold_covered)
        card.over_redacted_characters += len(redacted - gold_covered)
        if record_is_safe:
            card.safe_records += 1

        if record.channel == "negatives":
            card.negative_records += 1
            card.negative_false_positive_spans += sum(1 for span in predicted if not span.protect)

        for survivor in record.must_survive:
            card.must_survive_total += 1
            if survivor not in rendered:
                card.must_survive_violations.append(
                    {"record_id": record.id, "text": survivor, "channel": record.channel}
                )

    return card
