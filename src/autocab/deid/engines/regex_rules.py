"""The regex tier: the floor, and the only tier that runs inline at capture.

**This table is the single source of truth for pattern-based detection.**
``SensitiveDataRedactor`` is reimplemented on top of ``find_spans`` rather than
carrying its own four patterns; two pattern tables in one repo is how a term
ends up scrubbed on the pipeline path and leaked on the recorder path.

It is also the tier whose recall is *measured*: ``thresholds.json`` carries a
per-label floor derived from this table's behaviour on ``data/deid-eval``, and
the floors that look absurd -- ``NAME 0.10``, ``LOCATION 0.05`` -- are the most
useful lines in that file. They encode "the regex tier does not detect names" as
a tested, blame-able fact rather than an assumption, and they are the baseline
the model tier has to beat.

Two rejection mechanisms, deliberately distinct:

``Rule.require``
    a **shape** refinement: an octet over 255 means the string was never an IP
    address, so there is nothing to redact. Rejecting is correct.
``Rule.validator``
    a **plausibility** check (Luhn, SSN issuance ranges). Only ever adjusts
    ``Span.score``. A mistyped SSN is still PHI -- see ``validators.py``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Callable, Iterable, Sequence

from ..labels import DeidLabel
from ..spans import Span
from . import validators as v
from .base import BaseEngine, DetectorInfo, table_digest

# Common workflow words that must never be mistaken for the two halves of a
# `surname_givenname` file stem. Without this, `sample_qc_R1.fastq.gz` reads as
# a patient name and the over-redaction ceiling catches it immediately.
_PATH_STOPWORDS = frozenset(
    """
    sample samples qc raw trimmed merged sorted dedup deduped markdup aligned
    filtered final test out output input tmp temp run batch lane read reads
    ref reference log logs tumor tumour normal control case ctrl untreated
    treated wt mut mutant baseline followup pre post day week month year
    plate well pool library lib index barcode adapter fwd rev paired single
    pass fail summary report stats metrics counts matrix expr norm scaled
    train valid holdout fold rep replicate cohort group arm dose time point
    """.split()
)

_LABEL_WORD_STOPWORDS = frozenset({"none", "unknown", "n/a", "na", "null", "redacted"})


@dataclass(frozen=True, slots=True)
class Rule:
    """One pattern in the table."""

    name: str
    """Stable rule id. Appears in ``Span.detector`` as ``regex:<name>`` and in
    the audit, so a reviewer can tell *which* rule fired without re-running."""

    label: DeidLabel
    pattern: re.Pattern[str]
    group: int = 0
    """Which capture group is the span. ``0`` (the whole match) for labelled
    identifiers, so ``MRN 4419902`` becomes ``MRN_a7f3c1`` and the label word
    goes with it. A named group for CLI flags, so ``--job-name=cohort7`` becomes
    ``--job-name=ID_9f2a11`` and the command still parses."""

    base_score: float = 1.0
    difficulty: str = "easy"
    """``hard`` marks the OCR-garble rules (``MRN l23456`` with an ell). Reported
    separately and gated far lower, because gating on them would make the
    benchmark a tautology."""

    require: Callable[[str], bool] | None = None
    validator: Callable[[str], bool] | None = None
    invalid_score: float = 0.55
    """Score when ``validator`` fails. Still well above zero: the span is still
    redacted, the audit just records lower confidence."""

    trim: str = ""
    """Characters stripped from the right of the match. URLs at the end of a
    sentence otherwise swallow the full stop."""

    notes: str = field(default="", compare=False)


def _digit_count_between(low: int, high: int) -> Callable[[str], bool]:
    def check(value: str) -> bool:
        return low <= len(v.digits_only(value)) <= high

    return check


def _has_a_digit(value: str) -> bool:
    return any(char.isdigit() for char in value)


def _alnum_mixed(value: str) -> bool:
    """At least one letter **and** at least one digit."""

    return any(char.isalpha() for char in value) and any(char.isdigit() for char in value)


def _path_name_ok(value: str) -> bool:
    parts = value.lower().split("_")
    if len(parts) != 2:
        return False
    return not any(part in _PATH_STOPWORDS for part in parts)


def _not_placeholder(value: str) -> bool:
    return value.strip().casefold() not in _LABEL_WORD_STOPWORDS


_MONTH = r"(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*"
_US_STATE = (
    r"(?:A[LKZR]|C[AOT]|D[EC]|FL|GA|HI|I[ADLN]|K[SY]|LA|M[ADEINOST]|"
    r"N[CDEHJMVY]|O[HKR]|P[AR]|RI|S[CD]|T[NX]|UT|V[AT]|W[AIVY])"
)
_STREET_SUFFIX = (
    r"(?:St|Street|Ave|Avenue|Rd|Road|Blvd|Boulevard|Dr|Drive|Ln|Lane|Ct|Court|"
    r"Way|Pl|Place|Ter|Terrace|Cir|Circle|Hwy|Highway|Pkwy|Parkway)"
)
_OCTET = r"(?:25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d)"


RULES: tuple[Rule, ...] = (
    # ------------------------------------------------------------------ contact
    Rule(
        "email",
        DeidLabel.EMAIL,
        re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}\b"),
        notes="Byte-compatible with the pattern SensitiveDataRedactor shipped.",
    ),
    Rule(
        "url_scheme",
        DeidLabel.URL,
        re.compile(r"\b(?:https?|ftps?|sftp|ssh|smb|s3|gs|az)://[^\s<>\"'\]}),;]+"),
        trim=".,;:!?)",
    ),
    Rule(
        "url_www",
        DeidLabel.URL,
        re.compile(r"\bwww\.[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+\b"),
    ),
    Rule(
        "ipv4",
        DeidLabel.IP,
        re.compile(rf"(?<![\d.])(?:{_OCTET}\.){{3}}{_OCTET}(?![\d.])"),
        notes=(
            "Octet ranges are in the pattern rather than in a validator: an "
            "octet over 255 means the string was never an address."
        ),
    ),
    Rule(
        "ipv6",
        DeidLabel.IP,
        re.compile(
            r"(?<![\w:])(?:"
            r"(?:[0-9A-Fa-f]{1,4}:){7}[0-9A-Fa-f]{1,4}"
            r"|(?:[0-9A-Fa-f]{1,4}:){1,7}:(?:[0-9A-Fa-f]{1,4}(?::[0-9A-Fa-f]{1,4}){0,6})?"
            r"|::1"
            r")(?![0-9A-Za-z])(?!:[0-9A-Fa-f])"
        ),
        notes=(
            "A bare `::` alternative is deliberately absent -- it is too loose "
            "to distinguish from ordinary punctuation. `09:00:00` cannot match: "
            "the full form needs eight groups and the compressed form needs `::`. "
            "The trailing guard is two lookaheads rather than `(?![\\w:])` "
            "because an address is routinely followed by a bare colon -- "
            "`rsync backup@2001:db8:f4::75:/vault/`, `host:port` -- and the "
            "simpler guard rejected every one of those."
        ),
    ),
    Rule(
        "phone_nanp",
        DeidLabel.PHONE,
        re.compile(r"(?<![\d-])(?:\+?1[-. ]?)?\(?[2-9]\d{2}\)?[-. ][2-9]\d{2}[-. ]\d{4}(?![\d-])"),
        validator=v.nanp_plausible,
        notes=(
            "A separator between groups is mandatory. Allowing ten bare digits "
            "would make every 10-digit MRN a phone number and hand the wrong "
            "pseudonym class to the more sensitive label."
        ),
    ),
    Rule(
        "phone_labelled",
        DeidLabel.PHONE,
        re.compile(
            r"(?i)\b(?:phone|tel|telephone|mobile|cell|contact)\s*"
            r"(?:no\.?|number|#)?\s*[:=]\s*\+?[\d()\s.-]{6,20}\d"
        ),
        require=_digit_count_between(7, 15),
    ),
    Rule(
        "fax_labelled",
        DeidLabel.FAX,
        re.compile(r"(?i)\bfax\s*(?:no\.?|number|#)?\s*[:=]?\s*\+?[\d()\s.-]{6,20}\d"),
        require=_digit_count_between(7, 15),
    ),
    # ------------------------------------------------------------------ numbers
    Rule(
        "ssn_labelled",
        DeidLabel.SSN,
        re.compile(r"(?i)\bssn\s*(?:no\.?|number|#)?\s*[:=]?\s*\d{3}[- ]?\d{2}[- ]?\d{4}\b"),
        validator=v.ssn_plausible,
    ),
    Rule(
        "ssn_dashed",
        DeidLabel.SSN,
        re.compile(r"\b\d{3}-\d{2}-\d{4}\b"),
        validator=v.ssn_plausible,
    ),
    Rule(
        "ssn_spaced",
        DeidLabel.SSN,
        re.compile(r"\b\d{3} \d{2} \d{4}\b"),
        validator=v.ssn_plausible,
        base_score=0.8,
    ),
    Rule(
        "mrn_labelled",
        DeidLabel.MRN,
        re.compile(r"(?i)\bm\.?r\.?n\.?\s*(?:no\.?|number|#)?\s*[:=]?\s*\d{6,10}(?!\d)"),
        notes="Covers `MRN 4419902`, `MRN:123456`, `M.R.N. 123456`, `MRN# 123456`.",
    ),
    Rule(
        "mrn_spelled",
        DeidLabel.MRN,
        re.compile(r"(?i)\bmedical\s+record\s*(?:number|no\.?|#|id)?\s*[:=]?\s*\d{6,10}(?!\d)"),
    ),
    Rule(
        "mrn_ocr_garble",
        DeidLabel.MRN,
        re.compile(r"(?i)\bm\.?r\.?n\.?\s*(?:no\.?|number|#)?\s*[:=]?\s*[0-9OoIlSB|]{6,10}\b"),
        difficulty="hard",
        base_score=0.75,
        require=_digit_count_between(3, 10),
        notes=(
            "The `hard` tier: OCR turns 1 into l, 0 into O, 5 into S and 8 into "
            "B. The alphabet here must stay in step with `_ocr_garble` in "
            "eval/generate.py -- they are two halves of one claim. Safe to be "
            "this loose only because the `MRN` anchor carries the precision and "
            "`require` still demands three real digits."
        ),
    ),
    Rule(
        "npi_labelled",
        DeidLabel.LICENSE,
        re.compile(r"(?i)\bnpi\s*(?:no\.?|number|#)?\s*[:=]?\s*\d{10}\b"),
        validator=v.npi_ok,
    ),
    Rule(
        "dea_labelled",
        DeidLabel.LICENSE,
        re.compile(r"(?i)\bdea\s*(?:no\.?|number|#)?\s*[:=]?\s*[A-Z]{2}\d{7}\b"),
    ),
    Rule(
        "license_labelled",
        DeidLabel.LICENSE,
        re.compile(
            r"(?i)\b(?:license|licence|cert(?:ificate)?)\s*"
            r"(?:no\.?|number|#|id)\s*[:=]?\s*[A-Z0-9][A-Z0-9-]{4,19}\b"
        ),
    ),
    Rule(
        "payment_card",
        DeidLabel.ACCOUNT,
        re.compile(
            r"(?<![\d-])(?:4\d{3}|5[1-5]\d{2}|3[47]\d{2}|6(?:011|5\d{2}))"
            r"[- ]?\d{4}[- ]?\d{4}[- ]?\d{2,4}(?![\d-])"
        ),
        validator=v.luhn_ok,
    ),
    Rule(
        "account_labelled",
        DeidLabel.ACCOUNT,
        re.compile(
            r"(?i)\b(?:account|acct)\s*(?:no\.?|number|#|id)\s*[:=]?\s*[A-Z0-9][A-Z0-9-]{4,19}\b"
        ),
    ),
    Rule(
        "health_plan_id",
        DeidLabel.HEALTH_PLAN_ID,
        re.compile(
            r"(?i)\b(?:member|policy|subscriber|insurance|health\s*plan)\s*"
            r"(?:id|no\.?|number|#)\s*[:=]?\s*[A-Z0-9][A-Z0-9-]{4,19}\b"
        ),
    ),
    Rule(
        "serial_labelled",
        DeidLabel.DEVICE,
        re.compile(
            r"(?i)\b(?:serial|s/n|device)\s*(?:no\.?|number|#|id)?\s*[:=]\s*"
            r"[A-Z0-9][A-Z0-9-]{4,19}\b"
        ),
    ),
    Rule(
        "vin_labelled",
        DeidLabel.VEHICLE,
        re.compile(r"(?i)\bvin\s*(?:no\.?|number|#)?\s*[:=]?\s*[A-HJ-NPR-Z0-9]{17}\b"),
    ),
    Rule(
        "plate_labelled",
        DeidLabel.VEHICLE,
        re.compile(
            r"(?i)\b(?:license\s+plate|plate)\s*(?:no\.?|number|#)?\s*[:=]\s*[A-Z0-9-]{4,8}\b"
        ),
    ),
    # ----------------------------------------------------- subject / study ids
    Rule(
        "sj_id",
        DeidLabel.SUBJECT_ID,
        re.compile(r"(?<![A-Za-z0-9])SJ[-_ ]?\d{4,8}(?!\d)", re.IGNORECASE),
        notes=(
            "The legacy `sj_id` rule. `SJ-1234` is deliberately NOT allowlisted "
            "anywhere -- see allowlist.py.\n\n"
            "The terminator is `(?!\\d)`, not the `\\b` this rule originally "
            "carried. `\\b` sits *between* a word and a non-word character, so a "
            "match ending in digits fails when the next character is a word "
            "character -- and `_` is one. `SJ-4817_notes.txt` was therefore "
            "missed entirely, which is exactly how a subject id ends up in a "
            "filename on disk. Every rule in this table whose match ends in "
            "digits uses `(?!\\d)` for the same reason; `\\b` remains correct "
            "for the rules that end in a letter."
        ),
    ),
    Rule(
        "sj_cohort_id",
        DeidLabel.SUBJECT_ID,
        re.compile(r"(?<![A-Za-z0-9])SJ[A-Z]{2,6}\d{2,6}(?!\d)"),
        notes="`SJALL018`: protocol code plus sequence number.",
    ),
    Rule(
        "subject_labelled",
        DeidLabel.SUBJECT_ID,
        re.compile(
            r"(?i)\b(?:subject|participant|patient|study\s+subject)\s*"
            r"(?:id|no\.?|number|#)\s*[:=]?\s*[A-Z0-9][A-Z0-9_-]{2,15}\b"
        ),
        require=_not_placeholder,
    ),
    Rule(
        "accession_labelled",
        DeidLabel.ACCESSION,
        re.compile(
            r"(?i)\b(?:accession|specimen)(?:\s+(?:id|no\.?|number|#))?[\s:=]+"
            r"[A-Z0-9][A-Z0-9-]{3,15}\b"
        ),
        require=_has_a_digit,
        notes=(
            "The separator is optional because `specimen XY12-34567` is at least "
            "as common as `accession: XY12-34567`; requiring `[:=]` scored 0.00 "
            "on the corpus's whole gated ACCESSION tier. Requiring a digit in "
            "the value is what keeps `specimen collection` out of it."
        ),
    ),
    Rule(
        "sample_labelled",
        DeidLabel.SAMPLE_ID,
        re.compile(
            r"(?i)(?<![-\w])sample(?:\s+(?:id|name|no\.?|#))?[\s:=]+"
            r"[A-Za-z0-9][A-Za-z0-9._-]{2,24}\b"
        ),
        require=_has_a_digit,
        notes=(
            "`(?<![-\\w])` keeps this rule off `--sample=SJ1234`: without it the "
            "hull starts one character later than `sample_flag`'s, wins the "
            "start-ordered sort, and swallows the `--sample=` flag itself -- "
            "producing `--ID_9f2a11` and a command that no longer parses."
        ),
    ),
    Rule(
        "sample_flag",
        DeidLabel.SAMPLE_ID,
        re.compile(r"(?i)--sample(?:-id|-name)?[= ]\s*(?P<span>[A-Za-z0-9][A-Za-z0-9._-]{2,24})\b"),
        group=1,
        notes="Spans the value only, so the flag itself survives and the command still runs.",
    ),
    Rule(
        "sample_subject_tissue",
        DeidLabel.SAMPLE_ID,
        re.compile(
            r"(?<![A-Za-z0-9])SJ[-_]?\d{4,8}_[A-Za-z]{2,12}(?![A-Za-z0-9])",
            re.IGNORECASE,
        ),
        notes=(
            "`SJ1723_dx` -- a subject code plus a tissue suffix -- matched as one "
            "unit. Without this rule `sj_id` matches only the `SJ1723` prefix and "
            "leaves `_dx` on disk: a **clipped** span, which is a leak rather "
            "than a partial success, and which the clip-gap assertion in "
            "test_deid_regex_recall.py caught immediately.\n\n"
            "It wins over `sj_id` for free: `spans.resolve` sorts by "
            "`(start, -end, ...)`, so at an equal start the longer span takes the "
            "hull and absorbs SUBJECT_ID into `Span.absorbed` -- both labels are "
            "still reported, and nothing is split."
        ),
    ),
    Rule(
        "sample_prefixed",
        DeidLabel.SAMPLE_ID,
        re.compile(r"(?<![A-Za-z0-9])(?:SMP|SM|LIB|LBR)[-_]?\d{3,8}(?!\d)"),
    ),
    Rule(
        "slurm_job_name_flag",
        DeidLabel.SLURM_JOB_NAME,
        re.compile(r"(?i)--job[-_]?name[= ]\s*(?P<span>[A-Za-z0-9][A-Za-z0-9._-]{1,40})"),
        group=1,
    ),
    Rule(
        "slurm_job_name_short",
        DeidLabel.SLURM_JOB_NAME,
        re.compile(r"(?<![\w-])-J\s+(?P<span>[A-Za-z0-9][A-Za-z0-9._-]{1,40})"),
        group=1,
    ),
    Rule(
        "slurm_job_name_logfile",
        DeidLabel.SLURM_JOB_NAME,
        re.compile(r"(?i)\bslurm-(?P<span>[A-Za-z][A-Za-z0-9._-]{1,40})\.(?:out|err)\b"),
        group=1,
        notes=(
            "`slurm-wgs_smith_07.out`. Requires a leading letter so the far more "
            "common `slurm-4213.out` -- a job *id*, not a name, and not an "
            "identifier -- is left alone."
        ),
    ),
    Rule(
        "slurm_job_name_after_job_word",
        DeidLabel.SLURM_JOB_NAME,
        re.compile(r"(?i)\bjob\s+(?P<span>[A-Za-z0-9][A-Za-z0-9._-]{1,40})\b"),
        group=1,
        base_score=0.7,
        require=_alnum_mixed,
        notes=(
            "`scancel: job wgs_smith_07 cancelled`. The mixed letter-and-digit "
            "requirement is what separates a job *name* from `job 412391` (an "
            "id, not an identifier) and from `job failed`. Deliberately not "
            "tightened to require an underscore: that would be fitting the rule "
            "to this generator's naming convention rather than to SLURM."
        ),
    ),
    Rule(
        "slurm_job_name_column",
        DeidLabel.SLURM_JOB_NAME,
        re.compile(r"(?i)\bjob[-_ ]?name\s*[:=]?\s+(?P<span>[A-Za-z0-9][A-Za-z0-9._-]{1,40})"),
        group=1,
        notes="`sacct` / `squeue` column output: `JobName  wgs_smith_07`.",
    ),
    # -------------------------------------------------------- dates and ages
    Rule(
        "dob_labelled",
        DeidLabel.DOB_LABELLED,
        re.compile(
            r"(?i)\b(?:dob|d\.o\.b\.?|date\s+of\s+birth|birth\s*date)\s*[:=]?\s*"
            r"(?:\d{4}-\d{2}-\d{2}"
            rf"|\d{{1,2}}/\d{{1,2}}/\d{{2,4}}"
            rf"|\d{{1,2}}[-. ]{_MONTH}[-. ]\d{{2,4}}"
            rf"|{_MONTH}\.?\s+\d{{1,2}},?\s+\d{{4}})"
        ),
        notes="Spans the `DOB` token too, matching the legacy `dob` finding's behaviour.",
    ),
    Rule(
        "date_iso",
        DeidLabel.DATE_BARE,
        re.compile(r"\b\d{4}-\d{2}-\d{2}\b"),
        require=v.iso_date_ok,
    ),
    Rule(
        "date_slash",
        DeidLabel.DATE_BARE,
        re.compile(r"\b\d{1,2}/\d{1,2}/\d{2,4}\b"),
    ),
    Rule(
        "date_month_dmy",
        DeidLabel.DATE_BARE,
        re.compile(rf"(?i)\b\d{{1,2}}[-\s]{_MONTH}[-\s]\d{{2,4}}\b"),
    ),
    Rule(
        "date_month_mdy",
        DeidLabel.DATE_BARE,
        re.compile(rf"(?i)\b{_MONTH}\.?\s+\d{{1,2}},?\s+\d{{4}}\b"),
    ),
    Rule(
        "age_over_89_labelled",
        DeidLabel.AGE_OVER_89,
        re.compile(r"(?i)\b(?:age|aged)\s*[:=]?\s*(?:9\d|1[0-4]\d)\b"),
        notes=(
            "Ages <= 89 are kept: Safe Harbor only pulls the 90+ tail, and "
            "generalizing every age would destroy a real analytic variable."
        ),
    ),
    Rule(
        "age_over_89_yo",
        DeidLabel.AGE_OVER_89,
        re.compile(r"(?i)\b(?:9\d|1[0-4]\d)[-\s]?(?:year|yr)s?[-\s]?old\b"),
    ),
    # ----------------------------------------------------- names and locations
    Rule(
        "name_labelled",
        DeidLabel.NAME,
        re.compile(
            r"(?i)\b(?:patient|subject|attending|physician|provider|guardian|"
            r"mother|father|next\s+of\s+kin)\s*(?:name)?\s*[:=]\s*"
            r"(?P<span>[A-Z][a-z'\-]+(?:\s+[A-Z]\.?)?(?:\s+[A-Z][a-z'\-]+){1,2})"
        ),
        group=1,
        notes=(
            "`analyst` is deliberately absent from the context list: it names "
            "the workforce, not the PHI subject, and WorkflowClusterer groups on "
            "it. Override with --pseudonymize-analyst."
        ),
    ),
    Rule(
        "name_in_path",
        DeidLabel.NAME,
        re.compile(
            r"(?<![A-Za-z0-9_.-])(?P<span>[A-Za-z]{2,15}_[A-Za-z]{2,15})"
            r"(?=_R[12]\b|_S\d|_L00\d|\.fastq|\.fq|\.bam|\.cram|\.vcf)"
        ),
        group=1,
        base_score=0.7,
        require=_path_name_ok,
        notes=(
            "`smith_jane` in /data/proj/SJALL018/smith_jane_R1.fastq.gz -- the "
            "leak vector that is scoped out of the taxonomy but measured anyway. "
            "_PATH_STOPWORDS keeps `sample_qc_R1.fastq.gz` out of it.\n\n"
            "The leading guard is `(?<![A-Za-z0-9_.-])` and NOT `(?<=/)`, which "
            "is what it was. The seal scrubs path fields **component-wise** -- it "
            "splits on the separators and hands each component to the detector on "
            "its own -- so a rule anchored on `/` can never fire on a path field, "
            "which is precisely where this rule is meant to work. A planted "
            "`/data/quibblewick_zephyrine_R1.fastq.gz` sailed through the seal "
            "untouched. The corpus did not catch it because every generated "
            "fixture happens to have a `/` immediately before the stem."
        ),
    ),
    Rule(
        "street_address",
        DeidLabel.LOCATION,
        re.compile(rf"\b\d{{1,5}}\s+(?:[A-Z][A-Za-z.]{{1,15}}\s+){{1,4}}{_STREET_SUFFIX}\.?\b"),
    ),
    Rule(
        "city_state_zip",
        DeidLabel.LOCATION,
        re.compile(rf"\b[A-Z][A-Za-z.\- ]{{1,25}},\s*{_US_STATE}\s+\d{{5}}(?:-\d{{4}})?\b"),
        validator=v.zip_plausible,
    ),
)

#: Rules whose recall is gated. ``hard`` rules are reported, never gated.
GATED_DIFFICULTIES: frozenset[str] = frozenset({"easy", "medium"})

DETECTOR_KIND = "regex"


def patterns_sha256(deny_terms: Sequence[str] = ()) -> str:
    """Integrity digest of the effective rule set, for ``seal.json``.

    Deny terms are part of the effective ruleset, so a site that adds one gets a
    different digest -- which is correct: the seal was produced by a different
    detector than the one the committed scorecard measured.
    """

    entries = [
        (rule.name, rule.label.value, rule.pattern.pattern, str(rule.group), rule.difficulty)
        for rule in RULES
    ]
    entries.extend(
        ("deny", DeidLabel.DENY.value, term.casefold(), "0", "easy") for term in deny_terms
    )
    return table_digest(entries)


def _deny_pattern(term: str) -> re.Pattern[str]:
    """Word-bounded, escaped, optionally plural.

    The rule this replaces was ``re.sub(token, ...)`` on a raw, unescaped term:
    it rewrote ``outpatients_cohort.tsv`` into ``out[REDACTED_TERM]s_cohort.tsv``
    and a deny term containing ``(`` raised ``re.error`` at redaction time -- a
    crash on the redaction path, which fails *open* on any caller that catches
    broadly. ``re.escape`` plus ``\\b`` fixes both.

    The ``(?:es|s)`` tail is a deliberate, narrow concession: a bare boundary
    match would stop matching ``patients``, which the old substring rule did
    catch, and losing that is a real recall regression rather than a precision
    win.

    The boundaries are ``(?<!\\w)`` / ``(?!\\w)`` rather than ``\\b`` because
    ``\\b`` is defined *between* a word and a non-word character: a deny term
    ending in punctuation, such as ``b(c)``, has a non-word character on both
    sides of its trailing boundary and so could **never** match. The lookarounds
    say what is actually meant -- "not in the middle of a word" -- for a term
    whose own edges may be punctuation.
    """

    return re.compile(rf"(?<!\w){re.escape(term)}(?:es|s)?(?!\w)", re.IGNORECASE)


class RegexRules(BaseEngine):
    """Always on. No flag substitutes for it."""

    name = DETECTOR_KIND
    kind = DETECTOR_KIND

    def __init__(
        self,
        deny_terms: Sequence[str] = (),
        *,
        include_hard: bool = True,
    ) -> None:
        self._deny_terms = tuple(dict.fromkeys(term for term in deny_terms if term))
        self._deny = tuple((term, _deny_pattern(term)) for term in sorted(self._deny_terms))
        self._rules = tuple(rule for rule in RULES if include_hard or rule.difficulty != "hard")
        self._digest = patterns_sha256(self._deny_terms)

    @property
    def deny_terms(self) -> tuple[str, ...]:
        return self._deny_terms

    def info(self) -> DetectorInfo:
        return DetectorInfo(
            name=self.name,
            kind=self.kind,
            available=True,
            version=f"rules={len(self._rules)}",
            detail={
                "patterns_sha256": self._digest,
                "deny_terms": len(self._deny_terms),
            },
        )

    @staticmethod
    def _apply(rule: Rule, text: str) -> list[Span]:
        """Scan ``text`` with one rule.

        Deliberately **not** ``pattern.finditer``. ``finditer`` resumes from the
        end of each match, so when ``require`` rejects a match the scan has
        already stepped past anything that started inside it -- and a true
        positive nested in a rejected match is silently lost. That is not
        hypothetical: the note ``"Specimen specimen YO64-40805 from ..."`` had
        its real accession skipped because the rule first matched
        ``"Specimen specimen"``, which ``require`` correctly rejected for having
        no digits. Measured ``ACCESSION`` recall sat at 0.500 and looked like a
        missing pattern.

        So: on acceptance resume at ``match.end()``; on rejection resume at
        ``match.start() + 1``.
        """

        spans: list[Span] = []
        position = 0
        limit = len(text)
        while position <= limit:
            match = rule.pattern.search(text, position)
            if match is None:
                break
            start, end = match.span(rule.group)
            if start < 0 or end <= start:
                position = max(match.end(), match.start() + 1)
                continue
            surface = text[start:end]
            if rule.trim:
                stripped = surface.rstrip(rule.trim)
                if stripped:
                    end = start + len(stripped)
                    surface = stripped
            if not surface or (rule.require is not None and not rule.require(surface)):
                position = match.start() + 1
                continue
            score = rule.base_score
            if rule.validator is not None and not rule.validator(surface):
                score = min(score, rule.invalid_score)
            spans.append(
                Span(
                    start=start,
                    end=end,
                    label=rule.label.value,
                    detector=f"{DETECTOR_KIND}:{rule.name}",
                    score=score,
                )
            )
            position = max(match.end(), match.start() + 1)
        return spans

    def detect_one(self, text: str) -> list[Span]:
        spans: list[Span] = []
        for rule in self._rules:
            spans.extend(self._apply(rule, text))
        for term, pattern in self._deny:
            for match in pattern.finditer(text):
                spans.append(
                    Span(
                        start=match.start(),
                        end=match.end(),
                        label=DeidLabel.DENY.value,
                        detector=f"{DETECTOR_KIND}:deny:{term}",
                        score=1.0,
                    )
                )
        return spans


def find_spans(
    texts: Sequence[str],
    *,
    deny_terms: Sequence[str] = (),
    include_hard: bool = True,
) -> list[list[Span]]:
    """Batch entry point. **The** regex detector; everything else delegates here.

    Batch-shaped to match the ``Detector`` protocol even though the regex tier
    has no batching to exploit -- so that swapping in a model engine at a call
    site is a one-line change and can never accidentally become a per-string
    model invocation.
    """

    return RegexRules(deny_terms, include_hard=include_hard).detect(texts)


def find_spans_one(
    text: str,
    *,
    deny_terms: Sequence[str] = (),
    include_hard: bool = True,
) -> list[Span]:
    """Single-string convenience for the inline capture path."""

    return find_spans([text], deny_terms=deny_terms, include_hard=include_hard)[0]


def rules_by_label() -> dict[str, list[str]]:
    """Label -> rule names. Feeds the per-label table in the evaluation doc."""

    out: dict[str, list[str]] = {}
    for rule in RULES:
        out.setdefault(rule.label.value, []).append(rule.name)
    return out


def iter_rules(difficulties: Iterable[str] | None = None) -> tuple[Rule, ...]:
    if difficulties is None:
        return RULES
    wanted = set(difficulties)
    return tuple(rule for rule in RULES if rule.difficulty in wanted)
