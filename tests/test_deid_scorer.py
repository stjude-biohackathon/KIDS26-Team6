"""Scorer unit tests.

The clipped-span case is the whole reason this file exists. It is the difference
between *measuring* leaks and *hiding* them behind partial credit: a detector
that emits ``[NAME] Smith`` has left a surname on disk, and a metric that called
that a partial success would report 0.9-something while PHI sat in the session
folder.
"""

from __future__ import annotations

import pytest

from autocab.deid.eval.corpus import Corpus, GoldSpan, Record
from autocab.deid.eval.scorer import score
from autocab.deid.spans import Span


def gold(start, end, text, label="NAME"):
    return GoldSpan(start=start, end=end, label=label, text=text, hipaa=1)


def record(text, spans=(), *, rid="r1", channel="notes", difficulty="easy", must=()):
    return Record(
        id=rid,
        channel=channel,
        difficulty=difficulty,
        text=text,
        spans=tuple(spans),
        must_survive=tuple(must),
        provenance="test",
        license="CC0-1.0",
    )


def predicted(*triples):
    return [
        Span(start=start, end=end, label=label, detector="regex:test")
        for start, end, label in triples
    ]


def run(rec, spans, rendered=None):
    corpus = Corpus(records=(rec,), fingerprint="test")
    text = rendered if rendered is not None else rec.text
    return score(corpus, [(text, spans)], engine="test")


# --------------------------------------------------------------------------
# the eight cases
# --------------------------------------------------------------------------
def test_exact_match_is_a_strict_hit():
    rec = record("call Jane Smith now", [gold(5, 15, "Jane Smith")])
    card = run(rec, predicted((5, 15, "NAME")))

    assert card.by_label["NAME"].recall_strict == 1.0
    assert card.by_label["NAME"].recall_partial == 1.0
    assert card.by_label["NAME"].label_accuracy == 1.0
    assert card.leaks == []
    assert card.safe_record_rate == 1.0


def test_clipped_prediction_is_a_strict_miss_and_a_partial_hit():
    # `[NAME] Smith` -- the surname is still on disk.
    rec = record("call Jane Smith now", [gold(5, 15, "Jane Smith")])
    card = run(rec, predicted((5, 9, "NAME")))

    assert card.by_label["NAME"].recall_strict == 0.0
    assert card.by_label["NAME"].recall_partial == 1.0
    assert card.by_label["NAME"].clip_gap == pytest.approx(1.0)
    assert [(leak.label, leak.partial) for leak in card.leaks] == [("NAME", True)]
    assert card.safe_record_rate == 0.0


def test_superset_prediction_is_a_strict_hit_but_charged_as_over_redaction():
    # Whole-path redaction: safe, and it destroys the directory structure.
    rec = record("/data/proj/smith_jane_R1.fastq.gz", [gold(11, 21, "smith_jane")])
    card = run(rec, predicted((0, 33, "NAME")))

    assert card.by_label["NAME"].recall_strict == 1.0
    assert card.over_redacted_characters == 33 - 10
    assert card.over_redaction_rate > 0


def test_two_overlapping_predictions_together_cover_one_gold_span():
    # `P` is the *union*, so a gold span split across two predictions is a hit.
    rec = record("call Jane Smith now", [gold(5, 15, "Jane Smith")])
    card = run(rec, predicted((5, 11, "NAME"), (9, 15, "MRN")))

    assert card.by_label["NAME"].recall_strict == 1.0


def test_adjacent_predictions_leave_no_gap():
    rec = record("call Jane Smith now", [gold(5, 15, "Jane Smith")])
    card = run(rec, predicted((5, 10, "NAME"), (10, 15, "NAME")))

    assert card.by_label["NAME"].recall_strict == 1.0


def test_adjacent_but_not_touching_is_a_strict_miss():
    rec = record("call Jane Smith now", [gold(5, 15, "Jane Smith")])
    card = run(rec, predicted((5, 9, "NAME"), (10, 15, "NAME")))

    assert card.by_label["NAME"].recall_strict == 0.0
    assert card.by_label["NAME"].recall_partial == 1.0


def test_no_predictions_at_all():
    rec = record("call Jane Smith now", [gold(5, 15, "Jane Smith")])
    card = run(rec, [])

    assert card.by_label["NAME"].recall_strict == 0.0
    assert card.by_label["NAME"].recall_partial == 0.0
    assert card.leaks[0].partial is False
    assert card.leaks_per_1000_records == 1000.0


def test_a_record_with_no_gold_and_no_predictions_is_safe():
    card = run(record("samtools sort -@ 8"), [])

    assert card.safe_record_rate == 1.0
    assert card.leaks == []
    assert card.by_label == {}


def test_non_bmp_offsets_are_code_points_not_bytes():
    # One astral character is one offset. A byte-based scorer would be off by 3.
    text = "\U0001f9ea Jane Smith"
    rec = record(text, [gold(2, 12, "Jane Smith")])
    assert text[2:12] == "Jane Smith"
    card = run(rec, predicted((2, 12, "NAME")))

    assert card.by_label["NAME"].recall_strict == 1.0


def test_crlf_does_not_shift_offsets():
    text = "line one\r\nJane Smith\r\n"
    at = text.index("Jane Smith")
    rec = record(text, [gold(at, at + 10, "Jane Smith")])
    card = run(rec, predicted((at, at + 10, "NAME")))

    assert card.by_label["NAME"].recall_strict == 1.0
    # And a prediction that starts on the CR is a superset, not a miss.
    card2 = run(rec, predicted((at - 2, at + 10, "NAME")))
    assert card2.by_label["NAME"].recall_strict == 1.0
    assert card2.over_redacted_characters == 2


# --------------------------------------------------------------------------
# label handling
# --------------------------------------------------------------------------
def test_label_is_ignored_for_safety_but_scored_separately():
    rec = record("call Jane Smith now", [gold(5, 15, "Jane Smith")])
    card = run(rec, predicted((5, 15, "ORG")))

    assert card.by_label["NAME"].recall_strict == 1.0, "coverage is label-blind"
    assert card.by_label["NAME"].label_accuracy == 0.0, "the mislabel is still recorded"


def test_an_absorbed_label_counts_as_agreement():
    # `https://x/SJ001234` resolves to one URL hull that swallowed a SUBJECT_ID.
    rec = record("see https://x/SJ001234", [gold(14, 22, "SJ001234", label="SUBJECT_ID")])
    hull = Span(start=4, end=22, label="URL", detector="regex:url_scheme", absorbed=("SUBJECT_ID",))
    card = run(rec, [hull])

    assert card.by_label["SUBJECT_ID"].recall_strict == 1.0
    assert card.by_label["SUBJECT_ID"].label_accuracy == 1.0


def test_protect_spans_do_not_count_as_predictions():
    """A capture-time mask is not a fresh detection.

    Counting it would inflate both the predicted-span count and the
    false-positive rate on the prescrubbed channel.
    """

    rec = record("[REDACTED_MRN] and Jane Smith", [gold(19, 29, "Jane Smith")])
    guard = Span(start=0, end=14, label="PROTECTED", detector="surrogate_guard:mask", protect=True)
    card = run(rec, [guard, *predicted((19, 29, "NAME"))])

    assert card.predicted_spans == 1
    assert card.predicted_characters == 10


# --------------------------------------------------------------------------
# utility metrics
# --------------------------------------------------------------------------
def test_must_survive_violation_is_detected_on_the_rendered_text():
    rec = record("aligned to GRCh38", must=("GRCh38",))
    card = run(rec, predicted((11, 17, "NAME")), rendered="aligned to [REDACTED_NAME]")

    assert card.must_survive_total == 1
    assert card.must_survive_violations == [
        {"record_id": "r1", "text": "GRCh38", "channel": "notes"}
    ]


def test_must_survive_satisfied_when_the_token_is_untouched():
    rec = record("aligned to GRCh38", must=("GRCh38",))
    card = run(rec, [])

    assert card.must_survive_violations == []


def test_negative_channel_false_positives_are_counted_per_record():
    rec = record("MET and SET", channel="negatives", must=("MET", "SET"))
    card = run(rec, predicted((0, 3, "NAME")))

    assert card.negative_records == 1
    assert card.false_positive_spans_per_negative_record == 1.0


def test_a_wildcard_detector_blows_the_over_redaction_ceiling():
    """The ceiling is what stops precision being traded for recall.

    A `.*` patch scores perfect recall. It must fail anyway.
    """

    text = "samtools sort and Jane Smith"
    rec = record(text, [gold(18, 28, "Jane Smith")])
    card = run(rec, predicted((0, len(text), "NAME")))

    assert card.recall_strict == 1.0
    assert card.over_redaction_rate > 0.05
    assert card.over_redaction_share_of_predicted > 0.5


def test_gated_table_excludes_the_hard_tier():
    corpus = Corpus(
        records=(
            record("Jane Smith", [gold(0, 10, "Jane Smith")], rid="e", difficulty="easy"),
            record("Jane Smith", [gold(0, 10, "Jane Smith")], rid="h", difficulty="hard"),
        ),
        fingerprint="test",
    )
    card = score(corpus, [("x", predicted((0, 10, "NAME"))), ("Jane Smith", [])], engine="t")

    assert card.by_label["NAME"].gold == 2
    assert card.by_label["NAME"].recall_strict == 0.5
    assert card.by_label_gated["NAME"].gold == 1
    assert card.recall_strict_gated == 1.0


def test_there_is_no_f1_anywhere_in_the_scorecard():
    """A single blended number lets precision buy back recall.

    That is the one trade a destructive rewrite must never make silently, so the
    metric simply does not exist. This test is the tripwire.
    """

    import json

    rec = record("Jane Smith", [gold(0, 10, "Jane Smith")])
    payload = json.dumps(run(rec, predicted((0, 10, "NAME"))).to_dict()).lower()

    assert "f1" not in payload
    assert "fbeta" not in payload
    assert "f_score" not in payload


def test_misaligned_predictions_are_rejected_loudly():
    corpus = Corpus(records=(record("a"), record("b", rid="r2")), fingerprint="t")

    with pytest.raises(ValueError, match="will not infer an alignment"):
        score(corpus, [("a", [])], engine="t")
