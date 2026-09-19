"""Span algebra, the allowlist, pseudonyms, and the legacy compatibility map.

Step 1 of the build spec. The `resolve` tests are the important ones: every
later tier's safety depends on the sweep merging into the outer hull rather
than clipping or splitting.
"""

from __future__ import annotations

import pytest

from autocab.deid import mask_text
from autocab.deid.allowlist import Allowlist, normalize_surface
from autocab.deid.compat import findings_from_spans, mask_token
from autocab.deid.engines.regex_rules import RegexRules, find_spans_one, patterns_sha256
from autocab.deid.engines.surrogate_guard import SurrogateGuard, is_sealed_surface
from autocab.deid.labels import LABEL_RANK, PSEUDONYM_PREFIXES, DeidLabel, label_for
from autocab.deid.policy import Action, Policy, RenderMode
from autocab.deid.pseudonym import Pseudonymizer, normalize_for_label, year_of
from autocab.deid.spans import OffsetError, Span, check_bounds, coverage, render, resolve


def span(start, end, label="MRN", detector="regex:test", **kwargs):
    return Span(start=start, end=end, label=label, detector=detector, **kwargs)


# --------------------------------------------------------------------------
# resolve
# --------------------------------------------------------------------------
def test_resolve_clips_to_bounds_and_drops_empties():
    text = "abcdef"
    out = resolve(text, [span(-5, 3), span(4, 99), span(2, 2)])

    assert [(item.start, item.end) for item in out] == [(0, 3), (4, 6)]


def test_resolve_drops_candidates_overlapping_a_protect_span():
    # A capture-time mask must not be re-detected and re-replaced.
    text = "id [REDACTED_MRN] here"
    guard = span(3, 17, label="PROTECTED", detector="surrogate_guard:mask", protect=True)
    out = resolve(text, [guard, span(4, 12, label="NAME")])

    assert [item.protect for item in out] == [True]
    assert out[0].label == "PROTECTED"


def test_resolve_drops_allowlisted_surfaces_but_never_protect_spans():
    text = "GRCh38 and MRN 4412345"
    allow = Allowlist.default()
    out = resolve(
        text,
        [span(0, 6, label="NAME"), span(11, 22, label="MRN")],
        keeps=allow.keeps,
    )

    assert [(item.start, item.end) for item in out] == [(11, 22)]


def test_resolve_absorbs_overlap_into_the_outer_hull_and_records_the_label():
    # Never split: `NAME_ab12 MRN_cd34` from one string is noisier *and* leaks
    # the structure of the original.
    text = "https://x/SJ001234"
    out = resolve(
        text,
        [
            span(0, 18, label="URL", detector="regex:url_scheme"),
            span(10, 18, label="SUBJECT_ID", detector="regex:sj_id"),
        ],
    )

    assert len(out) == 1
    assert (out[0].start, out[0].end, out[0].label) == (0, 18, "URL")
    assert out[0].absorbed == ("SUBJECT_ID",)


def test_resolve_keeps_adjacent_spans_separate():
    out = resolve("abcdef", [span(0, 3, label="MRN"), span(3, 6, label="NAME")])

    assert [(item.start, item.end, item.label) for item in out] == [
        (0, 3, "MRN"),
        (3, 6, "NAME"),
    ]


def test_resolve_prefers_the_more_certain_label_on_an_exact_tie():
    # Same extent, same detector kind: LABEL_RANK decides, and SSN outranks NAME.
    assert LABEL_RANK["SSN"] < LABEL_RANK["NAME"]
    out = resolve(
        "123-45-6789",
        [
            span(0, 11, label="NAME", detector="model:x"),
            span(0, 11, label="SSN", detector="regex:ssn_dashed"),
        ],
    )

    assert out[0].label == "SSN"
    assert "NAME" in out[0].absorbed


def test_resolve_prefers_regex_over_a_model_on_a_label_tie():
    out = resolve(
        "MRN 4412345",
        [
            span(0, 11, label="MRN", detector="model:gliner_onnx"),
            span(0, 11, label="MRN", detector="regex:mrn_labelled"),
        ],
    )

    assert out[0].detector == "regex:mrn_labelled"


def test_resolve_merges_a_chain_of_overlaps_into_one_hull():
    out = resolve(
        "x" * 30,
        [span(0, 10, label="MRN"), span(8, 20, label="NAME"), span(18, 30, label="LOCATION")],
    )

    assert [(item.start, item.end) for item in out] == [(0, 30)]
    assert set(out[0].absorbed) == {"NAME", "LOCATION"}


# --------------------------------------------------------------------------
# offsets
# --------------------------------------------------------------------------
def test_malformed_span_offsets_raise_at_construction():
    with pytest.raises(OffsetError):
        Span(start=5, end=2, label="MRN", detector="regex:test")


def test_check_bounds_aborts_rather_than_clipping():
    # Invariant 3: a replacement in the wrong region *looks* like success.
    with pytest.raises(OffsetError) as excinfo:
        check_bounds("short", [span(0, 500)])

    assert "outside text of length 5" in str(excinfo.value)


def test_render_replaces_right_to_left_so_offsets_stay_valid():
    text = "a MRN 4412345 and b@example.org end"
    spans = resolve(text, find_spans_one(text))
    out = render(text, spans, lambda item, _surface: f"<{item.label}>")

    assert out == "a <MRN> and <EMAIL> end"


def test_render_skips_protect_spans():
    text = "already [REDACTED_MRN] sealed"
    spans = resolve(text, SurrogateGuard().detect_one(text))
    assert render(text, spans, lambda *_: "XXX") == text


def test_render_handles_non_bmp_offsets():
    # Python offsets are code points, so an astral character is one position.
    text = "\U0001f9ea MRN 4412345 \U0001f9ea"
    spans = resolve(text, find_spans_one(text))
    out = render(text, spans, lambda item, _surface: "[X]")

    assert out == "\U0001f9ea [X] \U0001f9ea"


def test_coverage_is_label_blind():
    assert coverage([span(0, 3), span(2, 5, label="NAME")]) == {0, 1, 2, 3, 4}


# --------------------------------------------------------------------------
# allowlist
# --------------------------------------------------------------------------
@pytest.mark.parametrize(
    "surface",
    [
        "GRCh38",
        "hg19",
        "HG008",
        "NA12878",
        "samtools",
        "MET",
        "CLOCK",
        "SRR12345678",
        "PRJNA123456",
        "ENSG00000139618",
        "rs334",
        "c.1521A>G",
        "p.Phe508del",
        "chr7:117559590",
        "ATCACGTT",
        "SI-GA-A1",
        "T2T-CHM13",
    ],
)
def test_allowlist_keeps_bioinformatics_surfaces(surface):
    allow = Allowlist.default()
    assert allow.keeps(surface, span(0, len(surface), label="NAME"))


def test_allowlist_does_not_keep_the_institutional_subject_id_shape():
    # Deliberately not allowlisted: it is the most likely direct identifier in
    # this domain, and allowlisting it "because it looks internal" is the
    # mistake allowlist.py exists to make expensive.
    allow = Allowlist.default()
    for surface in ("SJ-1234", "SJ001234", "SJALL018"):
        assert not allow.keeps(surface, span(0, len(surface), label="SUBJECT_ID"))


def test_allowlist_keeps_a_container_build_stamp_but_not_a_bare_date():
    allow = Allowlist.default()
    tagged = "biocontainers/gatk:4.5.0.0--2024-01-15"
    date_at = tagged.index("2024-01-15")
    assert allow.keeps(tagged, span(date_at, date_at + 10, label="DATE_BARE"))

    bare = "collected 2024-01-15"
    assert not allow.keeps(bare, span(10, 20, label="DATE_BARE"))


def test_allowlist_does_not_swallow_a_reserved_range_ip_address():
    # The conda-spec rule once matched `analyst@198.51.100.180` as a package pin
    # and allowlisted the address, dropping measured IP recall to 0.67.
    text = "ssh analyst@198.51.100.180 'ls'"
    at = text.index("198.51.100.180")
    allow = Allowlist.default()
    assert not allow.keeps(text, span(at, at + 14, label="IP"))
    assert "198.51.100.180" not in mask_text(text)[0]


def test_allowlist_reads_the_user_file(tmp_path):
    extra = tmp_path / "deid-allowlist.txt"
    extra.write_text("# comment\nMYCOHORT\n\n", encoding="utf-8")
    allow = Allowlist.default(user_file=extra)

    assert allow.keeps("MYCOHORT", span(0, 8, label="NAME"))
    assert str(extra) in allow.sources


def test_normalize_surface_folds_width_and_case():
    assert normalize_surface("ｈｇ３８") == normalize_surface("hg38")


# --------------------------------------------------------------------------
# policy
# --------------------------------------------------------------------------
def test_dates_and_ages_generalize_under_balanced_profiles():
    for render_mode in (RenderMode.MASK, RenderMode.PSEUDONYMIZE):
        policy = Policy(profile="balanced", render=render_mode)
        assert policy.action_for("DATE_BARE") is Action.GENERALIZE
        assert policy.action_for("AGE_OVER_89") is Action.GENERALIZE
        assert policy.action_for("DOB_LABELLED") is Action.GENERALIZE


def test_strict_profile_masks_even_generalized_labels():
    policy = Policy(profile="strict", render=RenderMode.PSEUDONYMIZE)

    assert policy.action_for("DATE_BARE") is Action.MASK
    assert policy.action_for("NAME") is Action.MASK


def test_strict_profile_refuses_overrides_rather_than_ignoring_them():
    with pytest.raises(ValueError):
        Policy(profile="strict", overrides=frozenset({"allow-unboxed-frames"}))

    balanced = Policy(profile="balanced", overrides=frozenset({"allow-unboxed-frames"}))
    assert balanced.allows("allow-unboxed-frames")


def test_unknown_profile_is_rejected():
    with pytest.raises(ValueError):
        Policy(profile="lenient")


# --------------------------------------------------------------------------
# pseudonyms
# --------------------------------------------------------------------------
def test_surrogates_are_stable_within_a_run_and_carry_48_bits():
    with Pseudonymizer(key=b"\x01" * 32) as pseudo:
        first = pseudo.surrogate_for("MRN", "MRN 4412345")
        again = pseudo.surrogate_for("MRN", "mrn: 4412345")

        assert first.text == again.text
        assert first.text.startswith("MRN_")
        assert len(first.text.split("_", 1)[1]) == 12
        assert first.value_id == again.value_id == 1
        assert first.first_seen and not again.first_seen


def test_the_label_is_inside_the_hmac_so_classes_do_not_collapse():
    with Pseudonymizer(key=b"\x02" * 32) as pseudo:
        as_mrn = pseudo.surrogate_for("MRN", "4412345")
        as_account = pseudo.surrogate_for("ACCOUNT", "4412345")

        assert as_mrn.text.split("_", 1)[1] != as_account.text.split("_", 1)[1]


def test_two_keys_produce_different_surrogates_for_the_same_value():
    with Pseudonymizer(key=b"\x03" * 32) as one, Pseudonymizer(key=b"\x04" * 32) as two:
        assert (
            one.surrogate_for("NAME", "Jane Smith").text
            != two.surrogate_for("NAME", "Jane Smith").text
        )


def test_dates_generalize_to_the_year_and_ages_to_a_band():
    with Pseudonymizer(key=b"\x05" * 32) as pseudo:
        assert pseudo.surrogate_for("DATE_BARE", "2012-06-01").text == "[DATE:2012]"
        assert pseudo.surrogate_for("DOB_LABELLED", "DOB 1972-03-14").text == "[DATE:1972]"
        assert pseudo.surrogate_for("AGE_OVER_89", "aged 94").text == "AGE_90_PLUS"
        assert pseudo.surrogate_for("DATE_BARE", "3/14/72").text == "[DATE:unknown]"


def test_name_normalization_strips_titles_but_keeps_middle_initials():
    assert normalize_for_label("NAME", "Dr. Jane Smith, MD") == "jane smith,"
    assert normalize_for_label("NAME", "Jane A Smith") != normalize_for_label(
        "NAME", "Jane B Smith"
    )


def test_identifier_normalization_keeps_a_discriminating_prefix():
    # Stripping any leading letters would turn SJ-1234 into 1234 and collapse it
    # with every other bare four-digit subject id.
    assert normalize_for_label("SUBJECT_ID", "SJ-1234") == "SJ1234"
    assert normalize_for_label("MRN", "MRN #44-12345") == "4412345"


def test_email_normalization_does_not_apply_gmail_dot_rules():
    assert normalize_for_label("EMAIL", "A.B@Example.ORG") == "a.b@example.org"
    assert normalize_for_label("EMAIL", "ab@example.org") != normalize_for_label(
        "EMAIL", "a.b@example.org"
    )


def test_year_of_refuses_to_guess_a_two_digit_century():
    assert year_of("2012-06-01") == "2012"
    assert year_of("3/14/72") == "unknown"


def test_the_key_is_never_serializable_and_is_discarded():
    import pickle

    pseudo = Pseudonymizer()
    assert repr(pseudo) == "Pseudonymizer(key=<discarded>)"
    with pytest.raises(TypeError):
        pickle.dumps(pseudo)

    pseudo.close()
    with pytest.raises(RuntimeError):
        pseudo.surrogate_for("MRN", "4412345")


def test_every_label_prefix_is_recognized_by_the_surrogate_guard():
    # A label with a novel prefix would mint surrogates the idempotency guard
    # cannot see, so a reseal would pseudonymize its own pseudonyms.
    with Pseudonymizer(key=b"\x06" * 32) as pseudo:
        for label in DeidLabel:
            if label.value in {"DATE_BARE", "DOB_LABELLED", "AGE_OVER_89"}:
                continue
            surrogate = pseudo.surrogate_for(label.value, "value-" + label.value)
            assert surrogate.text.split("_", 1)[0] in PSEUDONYM_PREFIXES
            assert is_sealed_surface(surrogate.text), surrogate.text


# --------------------------------------------------------------------------
# idempotency
# --------------------------------------------------------------------------
def test_sealed_text_is_a_fixpoint():
    original = "MRN 4412345 for jane.smith@example.org on 2012-06-01"
    once, _ = mask_text(original)
    twice, _ = mask_text(once)

    assert once == twice
    assert "4412345" not in once


def test_pseudonymized_text_is_a_fixpoint():
    from autocab.deid import detect, normalize_text

    text = normalize_text("MRN 4412345 and jane.smith@example.org")
    with Pseudonymizer(key=b"\x07" * 32) as pseudo:
        spans = detect([text])[0]
        sealed = render(
            text, spans, lambda item, surface: pseudo.surrogate_for(item.label, surface).text
        )
        spans_again = detect([sealed])[0]
        resealed = render(
            sealed,
            spans_again,
            lambda item, surface: pseudo.surrogate_for(item.label, surface).text,
        )

    assert sealed == resealed
    assert "4412345" not in sealed


# --------------------------------------------------------------------------
# legacy compatibility
# --------------------------------------------------------------------------
def test_legacy_finding_names_and_mask_tokens_survive_verbatim():
    assert mask_token("EMAIL") == "[REDACTED_EMAIL]"
    assert mask_token("SUBJECT_ID") == "[REDACTED_SJ_ID]"
    assert mask_token("DOB_LABELLED") == "[REDACTED_DOB]"
    assert mask_token("MRN") == "[REDACTED_MRN]"
    assert mask_token("DENY") == "[REDACTED_TERM]"
    assert mask_token("SLURM_JOB_NAME") == "[REDACTED_SLURM_JOB_NAME]"


def test_deny_findings_carry_the_term():
    _text, spans = mask_text("pathology report", deny_terms=("pathology",))
    assert findings_from_spans(spans) == ["deny:pathology"]


def test_the_deny_rule_is_escaped_and_word_bounded():
    # The old rule was `re.sub(token, ...)` on a raw term: it rewrote
    # `outpatients_cohort.tsv` and raised re.error on a term containing `(`.
    text, _ = mask_text(
        "outpatients_cohort.tsv lists patient and patients", deny_terms=("patient",)
    )
    assert "outpatients_cohort.tsv" in text
    assert text.count("[REDACTED_TERM]") == 2

    # A metacharacter in a deny term must not raise.
    assert mask_text("a b(c) d", deny_terms=("b(c)",))[0] == "a [REDACTED_TERM] d"


def test_patterns_digest_changes_with_the_deny_terms():
    assert patterns_sha256() != patterns_sha256(("patient",))
    assert patterns_sha256(("a", "b")) == patterns_sha256(("b", "a"))


def test_label_for_rejects_an_unknown_label():
    assert label_for("MRN") is DeidLabel.MRN
    with pytest.raises(ValueError):
        label_for("PATIENT_NAME")


def test_regex_engine_reports_its_pattern_digest():
    info = RegexRules(("patient",)).info()

    assert info.available
    assert info.detail is not None
    assert len(str(info.detail["patterns_sha256"])) == 64
