"""**The gate.** Step 4 of the build spec -- the measured claim.

This is the file that turns "we scrub things" into a number somebody is on the
hook for. Three layers, each catching something the others cannot:

1. **per-label floors** from ``thresholds.json`` -- catch a catastrophic
   regression (a pattern deleted, a rule that stopped compiling);
2. **the over-redaction ceiling** -- catches the opposite failure, a detector
   buying recall with a ``.*``;
3. **the committed scorecard snapshot** -- catches a slide from 0.98 to 0.91,
   which the floors are far too loose to notice.

No network, no model, no new dependency. Runs in milliseconds.
"""

from __future__ import annotations

import pytest

from autocab.deid.eval import corpus as corpus_mod
from autocab.deid.eval import report
from autocab.deid.eval.scorer import score


@pytest.fixture(scope="module")
def scored():
    loaded = corpus_mod.load()
    predictions, _latency = report.predict(loaded, engine="regex")
    return loaded, score(loaded, predictions, engine="regex")


@pytest.fixture(scope="module")
def thresholds():
    return report.load_thresholds()


def test_no_threshold_is_violated(scored, thresholds):
    _loaded, card = scored
    failures = report.check_thresholds(card, thresholds, engine="regex")

    assert failures == [], "\n".join(failures)


def test_thresholds_were_derived_from_this_corpus(scored, thresholds):
    _loaded, card = scored

    assert thresholds["corpus_fingerprint"] == card.corpus_fingerprint


@pytest.mark.parametrize(
    "label",
    ["EMAIL", "IP", "URL", "SSN", "MRN", "DOB_LABELLED", "SUBJECT_ID"],
)
def test_the_structurally_certain_labels_have_no_gated_misses(scored, label):
    """A floor of 1.00 means exactly that: zero tolerated misses.

    These are the labels where a regex either matches the shape or the shape was
    never there. Anything below 1.00 is a bug, not a tuning question.
    """

    _loaded, card = scored
    row = card.by_label_gated[label]

    assert row.recall_strict == 1.0, (
        f"{label}: {row.gold - row.strict} of {row.gold} gated spans missed"
    )


def test_the_regex_tier_does_not_detect_names_and_says_so(scored):
    """The most valuable assertion in the file.

    ``NAME`` recall is around 0.37 and its floor is 0.10. Both halves matter:
    the floor records that low name recall is *expected* from a pattern tier, so
    nobody reads the overall number as a name-redaction guarantee -- and the
    upper bound records that this is the baseline the model tier must beat. If
    this ever passes 0.9 without a model, the corpus has been fitted to the
    patterns.
    """

    _loaded, card = scored
    name = card.by_label_gated["NAME"]

    assert name.recall_strict >= 0.10
    assert name.recall_strict < 0.90, (
        "the regex tier is scoring like a model on NAME; either a rule has been "
        "overfitted to the generator's naming convention, or the corpus has "
        "drifted toward the patterns"
    )


def test_recall_partial_never_exceeds_strict_by_much(scored):
    """A large clip gap is a leak wearing a partial-credit disguise.

    Reported per label so a regression names the label that started clipping.
    """

    _loaded, card = scored
    clipping = {
        label: round(row.clip_gap, 3) for label, row in card.by_label.items() if row.clip_gap > 0.05
    }

    assert clipping == {}, f"labels with a clip gap: {clipping}"


def test_no_must_survive_token_is_destroyed(scored):
    _loaded, card = scored

    assert card.must_survive_violations == [], card.must_survive_violations


def test_negatives_are_essentially_clean(scored, thresholds):
    _loaded, card = scored
    ceiling = thresholds["engines"]["regex"]["max_false_positive_spans_per_negative_record"]

    assert card.negative_records > 0
    assert card.false_positive_spans_per_negative_record <= ceiling


def test_the_fixed_deny_rule_no_longer_blows_the_over_redaction_ceiling(scored):
    """The build spec predicted this would fail as originally written.

    The old rule was an unescaped, unbounded ``re.sub(token, ...)``: it matched
    ``patient`` inside ``outpatients_cohort.tsv`` and rewrote the middle of a
    filename. On this corpus the fixed rule is roughly an order of magnitude
    under the ceiling; the substring version would not be.
    """

    _loaded, card = scored

    assert card.over_redaction_rate <= 0.05
    assert "patient" in report.DEFAULT_DENY_TERMS


def test_scorecard_matches_the_committed_snapshot(scored):
    """Catches the slide the floors are too loose to see.

    On failure the message names the labels that moved. Rerun
    ``autocab deid eval --engine regex --write-scorecard`` and put the delta in
    the PR description.
    """

    _loaded, card = scored
    problems = report.compare_scorecard(card)

    assert problems == [], "\n".join(problems)


def test_the_snapshot_carries_no_nondeterministic_fields():
    """A snapshot that churns is a snapshot nobody reads."""

    import json

    path = report.scorecard_path("regex")
    payload = json.loads(path.read_text(encoding="utf-8"))
    blob = json.dumps(payload).lower()

    for forbidden in ("timestamp", "generated_at", "hostname", "elapsed", "ms_per_kb", "seconds"):
        assert forbidden not in blob, f"{forbidden} would churn on every run"


def test_hard_tier_is_reported_but_not_gated(scored, thresholds):
    _loaded, card = scored

    assert "hard" in card.by_difficulty
    assert card.by_difficulty["hard"].gold > 0
    # No `hard` label floors exist under engines.regex; only a much lower
    # aggregate in its own section.
    assert (
        card.by_difficulty["hard"].recall_strict >= thresholds["hard_tier"]["overall_recall_strict"]
    )


def test_high_risk_channels_are_reported_separately(scored):
    """A corpus-wide average hides `notes` and `jobs`."""

    _loaded, card = scored

    for channel in ("notes", "jobs", "ocr"):
        assert channel in card.by_channel
        assert card.by_channel[channel].gold > 0


def test_a_deleted_pattern_would_fail_the_gate(thresholds):
    """The gate is tested, not assumed.

    Without this, a bug that made `check_thresholds` always return `[]` would
    leave every assertion above passing vacuously.
    """

    from autocab.deid.engines import regex_rules

    loaded = corpus_mod.load()
    survivors = tuple(rule for rule in regex_rules.RULES if rule.label.value != "EMAIL")
    engine = regex_rules.RegexRules(report.DEFAULT_DENY_TERMS)
    engine._rules = survivors  # type: ignore[attr-defined]

    from autocab.deid import mask_token, normalize_text, render, resolve
    from autocab.deid.allowlist import Allowlist
    from autocab.deid.engines.surrogate_guard import SurrogateGuard

    allow = Allowlist.default()
    predictions = []
    for rec in loaded:
        text = normalize_text(rec.text)
        spans = resolve(
            text,
            [*SurrogateGuard().detect_one(text), *engine.detect_one(text)],
            keeps=allow.keeps,
        )
        predictions.append((render(text, spans, lambda s, _t: mask_token(s.label)), spans))

    crippled = score(loaded, predictions, engine="regex")
    failures = report.check_thresholds(crippled, thresholds, engine="regex")

    assert any("EMAIL" in failure for failure in failures), failures
