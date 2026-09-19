"""The corpus is valid, reserved, reproducible, and adversarial.

Step 2's gate. None of these assertions is decorative:

* ``span.text == text[start:end]`` for every span in every record is the
  cheapest defence against offset rot;
* the reserved-range checks are what let the leak table in
  ``docs/deid-evaluation.md`` print gold text at all;
* byte-for-byte reproduction is what makes the committed fixtures reviewable.
"""

from __future__ import annotations

import json

import pytest

from autocab.deid.eval import corpus as corpus_mod
from autocab.deid.eval import generate
from autocab.deid.labels import ALL_LABELS


@pytest.fixture(scope="module")
def loaded():
    return corpus_mod.load()


def test_corpus_is_structurally_valid(loaded):
    problems = corpus_mod.validate(loaded)

    assert problems == [], "\n".join(problems)


def test_corpus_reproduces_byte_for_byte(loaded):
    # The step-2 gate. `autocab deid gen-corpus --seed 1337 --check` in CI.
    problems = generate.check_corpus(corpus_mod.default_root(), generate.DEFAULT_SEED)

    assert problems == [], "\n".join(problems)


def test_every_channel_is_populated(loaded):
    for channel in generate.CHANNELS:
        assert loaded.by_channel(channel), f"channel {channel} is empty"


def test_every_gold_span_text_matches_its_slice(loaded):
    # Duplicated from validate() deliberately: this is the single assertion the
    # whole corpus rests on, and it should fail on its own line.
    for record in loaded:
        for span in record.spans:
            assert record.text[span.start : span.end] == span.text, record.id


def test_gold_labels_are_all_in_the_taxonomy(loaded):
    known = {label.value for label in ALL_LABELS}
    for record in loaded:
        for span in record.spans:
            assert span.label in known, f"{record.id}: {span.label}"


def test_all_three_difficulty_tiers_are_present(loaded):
    present = {record.difficulty for record in loaded}

    assert present == set(generate.DIFFICULTIES)
    # And the gate must not be computed over `easy` alone -- that would be a
    # tautology, since the generator and the regex table share one
    # understanding of the same shapes.
    assert generate.GATED_DIFFICULTIES == {"easy", "medium"}


@pytest.mark.parametrize(
    "label,floor",
    [
        ("EMAIL", 8),
        ("MRN", 8),
        ("SSN", 8),
        ("IP", 8),
        ("URL", 8),
        ("PHONE", 8),
        ("DOB_LABELLED", 8),
        ("DATE_BARE", 8),
        ("SUBJECT_ID", 8),
        ("SAMPLE_ID", 8),
        ("SLURM_JOB_NAME", 8),
        ("NAME", 8),
        ("LOCATION", 8),
        ("AGE_OVER_89", 8),
    ],
)
def test_per_label_coverage_floors(loaded, label, floor):
    """Every gated label has enough fixtures for its rate to mean something.

    A label with two gated spans has a recall that can only be 0.0, 0.5 or 1.0,
    which makes its floor unmeasurable in practice.
    """

    counts = corpus_mod.coverage_by_label(loaded, gated_only=True)

    assert counts.get(label, 0) >= floor, f"{label} has {counts.get(label, 0)} gated spans"


def test_reservable_identifiers_are_all_in_reserved_space(loaded):
    problems = corpus_mod._reserved_range_problems(loaded)

    assert problems == [], "\n".join(problems)


def test_no_real_institutional_domain_or_benchmark_label(loaded):
    problems = corpus_mod._leak_problems(loaded)

    assert problems == [], "\n".join(problems)


def test_negatives_are_adversarial_and_carry_no_gold(loaded):
    negatives = loaded.by_channel("negatives")

    assert negatives
    for record in negatives:
        assert record.spans == ()
        assert record.must_survive, f"{record.id}: a negative with nothing to protect"

    # The specific adversarial shapes precision depends on.
    blob = "\n".join(record.text for record in negatives)
    for needle in ("GRCh38", "chr", "c.", "p.", "SRR", "PRJNA", "biocontainers/", "SI-GA-"):
        assert needle in blob, f"negatives lack {needle!r}"


def test_prescrubbed_channel_carries_surrogates_and_a_residual_identifier(loaded):
    """The tier that measures the risk of running the local tier first.

    Pre-inserted placeholders could plausibly confuse a later detector. This
    channel makes that a measurement instead of a worry.
    """

    records = loaded.by_channel("prescrubbed")

    assert records
    for record in records:
        assert record.spans, f"{record.id}: no residual identifier to find"
        assert any(marker in record.text for marker in ("_", "[REDACTED_", "[DATE:")), record.id
    blob = "\n".join(record.text for record in records)
    assert "[REDACTED_" in blob
    assert "[DATE:" in blob


def test_fixtures_are_jsonl_not_ignored_file_extensions():
    """`.gitignore` ignores *.log, *.vcf, *.fastq, *.bam.

    A fixture named `slurm-4213.log` would be silently dropped from the commit
    and CI would score a corpus with a missing channel.
    """

    root = corpus_mod.default_root()
    for path in (root / "corpus").iterdir():
        assert path.suffix == ".jsonl", path


def test_every_record_declares_its_provenance_and_license(loaded):
    for record in loaded:
        assert record.license == "CC0-1.0"
        assert record.provenance.startswith("generated:v1:seed=")
        assert ":tmpl=" in record.provenance


def test_generator_rng_is_independent_of_the_stdlib_random_module(monkeypatch):
    """The committed fixtures must not depend on CPython's Mersenne Twister.

    If the generator used `random.Random`, a future change to `_randbelow`'s
    rejection sampling would break every fixture in the repo with no code
    change. Breaking `random` entirely must leave generation unaffected.
    """

    import random

    def explode(*_args, **_kwargs):  # pragma: no cover - only if regressed
        raise AssertionError("the corpus generator must not use random.Random")

    monkeypatch.setattr(random, "Random", explode)
    monkeypatch.setattr(random, "seed", explode)
    monkeypatch.setattr(random, "choice", explode)

    built = generate.build_corpus(
        generate.DEFAULT_SEED, lexicon_dir=corpus_mod.default_root() / "lexicons"
    )

    assert generate.corpus_fingerprint(built) == corpus_mod.load().fingerprint


def test_thresholds_are_keyed_on_the_current_corpus(loaded):
    """A floor derived from a different corpus is worse than no floor."""

    root = corpus_mod.default_root()
    thresholds = json.loads((root / "thresholds.json").read_text(encoding="utf-8"))

    assert thresholds["corpus_fingerprint"] == loaded.fingerprint, (
        "thresholds.json was set against a different corpus; rerun "
        "`autocab deid eval --engine regex --write-scorecard` and update the "
        "fingerprint deliberately"
    )


def test_corpus_fingerprint_matches_the_bytes_on_disk(loaded):
    assert corpus_mod.fingerprint_of_files() == loaded.fingerprint


def test_validate_catches_a_corrupted_offset(loaded):
    """The validator itself is tested -- a validator that never fires is decor."""

    from dataclasses import replace

    bad = replace(loaded.records[0], spans=())
    record = loaded.records[0]
    if not record.spans:  # pragma: no cover - the first record always has spans
        pytest.skip("first record has no spans")
    broken = replace(
        record,
        spans=(replace(record.spans[0], start=record.spans[0].start + 1),),
    )
    problems = corpus_mod.validate(corpus_mod.Corpus(records=(broken,), fingerprint="x"))

    assert any("text[start:end]" in problem for problem in problems)
    assert corpus_mod.validate(corpus_mod.Corpus(records=(bad,), fingerprint="x")) == []
