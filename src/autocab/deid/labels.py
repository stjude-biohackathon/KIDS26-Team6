"""THE de-identification taxonomy.

One taxonomy, one place. This module feeds *four* consumers and they must never
drift apart:

1. the regex tier (``engines/regex_rules.py``) -- which label each pattern emits;
2. the zero-shot model prompt (``engines/gliner_onnx.py``) -- the ``description``
   fields below are the literal label strings handed to GLiNER;
3. the LLM structured-output schema (``engines/llm_findings.py``) -- ``DeidLabel``
   is the enum in the JSON schema, so a model physically cannot return a label
   we do not know;
4. the corpus validator (``eval/corpus.py``) -- a fixture carrying an unknown
   label is a test failure, not a silent skip.

Adding a label here is therefore the whole change; there is no second list to
update. That is the point.

HIPAA mapping uses the Safe Harbor identifier numbers (45 CFR 164.514(b)(2)).
``hipaa=None`` means "not a Safe Harbor identifier" -- the deny-term rule is a
site policy control, not a HIPAA one, and saying so keeps the compliance rollup
in ``docs/deid-evaluation.md`` honest.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class DeidLabel(str, Enum):
    """The closed label set.

    ``str``-valued so it serializes as its own name in JSON and compares equal
    to the plain string a regex table or a corpus fixture carries. Deriving from
    ``enum.StrEnum`` would be nicer but that is 3.11+, and ``requires-python``
    is ``>=3.10``.
    """

    # --- HIPAA 1-3: names, geography, dates ---
    NAME = "NAME"
    LOCATION = "LOCATION"
    DATE_BARE = "DATE_BARE"
    DOB_LABELLED = "DOB_LABELLED"
    AGE_OVER_89 = "AGE_OVER_89"
    # --- HIPAA 4-6: contact ---
    PHONE = "PHONE"
    FAX = "FAX"
    EMAIL = "EMAIL"
    # --- HIPAA 7-13: numbers ---
    SSN = "SSN"
    MRN = "MRN"
    HEALTH_PLAN_ID = "HEALTH_PLAN_ID"
    ACCOUNT = "ACCOUNT"
    LICENSE = "LICENSE"
    VEHICLE = "VEHICLE"
    DEVICE = "DEVICE"
    # --- HIPAA 14-16: network and biometric ---
    URL = "URL"
    IP = "IP"
    BIOMETRIC = "BIOMETRIC"
    # --- HIPAA 18: any other unique identifying number or code ---
    SUBJECT_ID = "SUBJECT_ID"
    ACCESSION = "ACCESSION"
    SAMPLE_ID = "SAMPLE_ID"
    SLURM_JOB_NAME = "SLURM_JOB_NAME"
    ORG = "ORG"
    # --- site policy, not HIPAA ---
    DENY = "DENY"

    def __str__(self) -> str:  # pragma: no cover - cosmetic
        return self.value


@dataclass(frozen=True, slots=True)
class LabelSpec:
    """Everything the four consumers need to know about one label."""

    label: DeidLabel
    hipaa: int | None
    prefix: str
    """Pseudonym prefix. Constrained to the ``SurrogateGuard`` alphabet -- see
    ``PSEUDONYM_PREFIXES`` below."""
    description: str
    """The zero-shot label string. Written for a *model*, not for a human: GLiNER
    matches on the semantics of this phrase, so "medical record number" beats
    "MRN" and "patient name embedded in a file path" beats "name"."""
    rank: int
    """Tie-break precedence in ``spans.resolve`` -- lower wins. Structurally
    certain identifiers outrank inferred ones so that an MRN overlapping a bare
    number range keeps the MRN pseudonym class."""


# Ordered as LABEL_RANK in the build spec:
#   SSN < MRN < SUBJECT_ID < ACCESSION < PHONE < EMAIL < DEVICE
#       < DATE < NAME < AGE < LOCATION < ORG < DENY
# with the bio shapes and the remaining HIPAA number classes slotted in beside
# their nearest structural sibling.
_SPECS: tuple[LabelSpec, ...] = (
    LabelSpec(DeidLabel.SSN, 7, "SSN", "US social security number", 10),
    LabelSpec(DeidLabel.MRN, 8, "MRN", "medical record number", 20),
    LabelSpec(DeidLabel.HEALTH_PLAN_ID, 9, "ACC", "health plan beneficiary number", 25),
    LabelSpec(
        DeidLabel.SUBJECT_ID,
        18,
        "SUBJ",
        "study subject or patient identifier code",
        30,
    ),
    LabelSpec(DeidLabel.ACCESSION, 18, "ACC", "laboratory or specimen accession number", 40),
    LabelSpec(DeidLabel.SAMPLE_ID, 18, "ID", "sequencing sample identifier", 45),
    LabelSpec(DeidLabel.SLURM_JOB_NAME, 18, "ID", "SLURM job name", 48),
    LabelSpec(DeidLabel.ACCOUNT, 10, "ACC", "financial account number", 49),
    LabelSpec(DeidLabel.PHONE, 4, "PHONE", "telephone number", 50),
    LabelSpec(DeidLabel.FAX, 5, "PHONE", "fax number", 52),
    LabelSpec(DeidLabel.EMAIL, 6, "EMAIL", "email address", 60),
    LabelSpec(DeidLabel.URL, 14, "ID", "web URL", 62),
    LabelSpec(DeidLabel.IP, 15, "ID", "IP address", 64),
    LabelSpec(DeidLabel.LICENSE, 11, "ID", "professional certificate or license number", 66),
    LabelSpec(DeidLabel.VEHICLE, 12, "ID", "vehicle identifier or license plate", 68),
    LabelSpec(DeidLabel.DEVICE, 13, "DEV", "medical device serial number", 70),
    LabelSpec(DeidLabel.BIOMETRIC, 16, "ID", "biometric identifier", 72),
    LabelSpec(DeidLabel.DOB_LABELLED, 3, "ID", "date of birth", 80),
    LabelSpec(DeidLabel.DATE_BARE, 3, "ID", "calendar date", 85),
    LabelSpec(DeidLabel.NAME, 1, "NAME", "person full name", 90),
    LabelSpec(DeidLabel.AGE_OVER_89, 3, "ID", "age over 89 years", 100),
    LabelSpec(
        DeidLabel.LOCATION,
        2,
        "ADDR",
        "street address, city, or postal code smaller than a state",
        110,
    ),
    LabelSpec(DeidLabel.ORG, 18, "ORG", "hospital, clinic, or employer name", 120),
    LabelSpec(DeidLabel.DENY, None, "ID", "site-denied sensitive term", 130),
)

SPECS: dict[DeidLabel, LabelSpec] = {spec.label: spec for spec in _SPECS}

ALL_LABELS: tuple[DeidLabel, ...] = tuple(spec.label for spec in _SPECS)

LABEL_RANK: dict[str, int] = {spec.label.value: spec.rank for spec in _SPECS}

HIPAA_BY_LABEL: dict[str, int | None] = {spec.label.value: spec.hipaa for spec in _SPECS}

PREFIX: dict[str, str] = {spec.label.value: spec.prefix for spec in _SPECS}

ZERO_SHOT_LABELS: tuple[str, ...] = tuple(spec.description for spec in _SPECS)

LABEL_BY_DESCRIPTION: dict[str, str] = {spec.description: spec.label.value for spec in _SPECS}

# The prefix alphabet SurrogateGuard recognizes. Kept as an explicit constant so
# adding a label with a novel prefix breaks a test instead of quietly producing
# surrogates that the idempotency guard cannot see -- which would make a reseal
# pseudonymize its own pseudonyms.
PSEUDONYM_PREFIXES: tuple[str, ...] = tuple(sorted({spec.prefix for spec in _SPECS}))

# Detector kinds, ranked. regex wins ties because its label is structurally
# certain: a Luhn-valid 9-digit string preceded by "SSN:" is an SSN, whereas a
# model calling the same region NAME is a guess.
DETECTOR_RANK: dict[str, int] = {"regex": 0, "model": 1, "llm": 2}

#: Labels whose *value* is generalized rather than replaced with a surrogate.
#: Pseudonymizing a date destroys the timeline the recorder exists to capture,
#: and Safe Harbor only asks for date elements finer than the year.
GENERALIZED_LABELS: frozenset[str] = frozenset(
    {DeidLabel.DATE_BARE.value, DeidLabel.DOB_LABELLED.value, DeidLabel.AGE_OVER_89.value}
)


def label_for(value: str) -> DeidLabel:
    """Coerce ``value`` to a known label, raising on anything unknown.

    Used by the corpus validator and the LLM finding parser. An unknown label is
    always a bug in one of the four consumers, never data to be tolerated.
    """

    try:
        return DeidLabel(value)
    except ValueError as exc:  # pragma: no cover - exercised via tests
        raise ValueError(
            f"unknown de-identification label {value!r}; "
            f"known labels: {', '.join(sorted(label.value for label in DeidLabel))}"
        ) from exc


def hipaa_rollup() -> dict[int | None, list[str]]:
    """HIPAA identifier number -> the labels covering it.

    This is the shape a compliance reviewer reads, and it is deliberately
    computed rather than written down: identifier 17 (full-face photographs)
    shows up as absent because it *is* absent.
    """

    rollup: dict[int | None, list[str]] = {}
    for spec in _SPECS:
        rollup.setdefault(spec.hipaa, []).append(spec.label.value)
    return rollup
