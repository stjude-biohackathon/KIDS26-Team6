"""Deterministic, network-free corpus generator.

The corpus comes **before** the detector. Without it, "we scrub things" stays an
assertion; with it, every claim in ``docs/deid-evaluation.md`` is a number
somebody can reproduce with ``autocab deid gen-corpus --seed 1337 --check``.

Two safety tiers, because the two halves of the taxonomy have different
options:

**Tier 1 -- structural reservation.** Where a reserved space exists, the
generator uses it and a test enforces it: ``@example.org`` / ``.com`` / ``.net``
(RFC 2606), ``555-555-01xx`` (NANP fictitious exchange in an unassignable area
code), SSN areas ``000`` / ``666`` / ``9xx`` (never issued), ``192.0.2.0/24``,
``198.51.100.0/24``, ``203.0.113.0/24`` (RFC 5737), ``2001:db8::/32`` (RFC 3849),
published payment-card test numbers, and a documented test-only ``44xxxxx`` MRN
band. A fixture physically cannot collide with a live value.

**Tier 2 -- independent-draw provenance.** ``NAME``, ``LOCATION``, ``DATE`` and
``AGE`` have no reserved space -- every surname is somebody's. So every field of
every record is an **independent** seeded draw from a committed public-domain
lexicon. The surname does not know the given name, which does not know the city,
which does not know the ZIP or the date. No *combination* in this corpus maps to
a real person, which is the property that matters; a lone surname in a synthetic
shell command is not a disclosure.

The same reasoning covers ``SUBJECT_ID``, ``SAMPLE_ID`` and ``ACCESSION``: there
is no reserved band for them either, so they are independent draws carrying no
linked attributes.

**Everything is ``.jsonl``.** ``.gitignore`` already ignores ``*.vcf``,
``*.fastq``, ``*.bam`` and ``*.log``, so a fixture file named ``slurm-4213.log``
would be silently dropped from the commit and CI would score a corpus with a
missing channel. Those shapes are embedded as strings inside JSONL instead.
"""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable, Sequence

from ..labels import HIPAA_BY_LABEL, DeidLabel

CORPUS_VERSION = "v1"
DEFAULT_SEED = 1337
LICENSE = "CC0-1.0"
RECORDS_PER_TEMPLATE = 4

CHANNELS: tuple[str, ...] = (
    "shell",
    "ocr",
    "notes",
    "agents",
    "diffs",
    "jobs",
    "prescrubbed",
    "negatives",
)

DIFFICULTIES: tuple[str, ...] = ("easy", "medium", "hard")

#: Difficulties the recall gate is computed over. ``hard`` is reported and
#: gated far lower: the regex tier scores ~1.00 on shapes the generator built
#: from the same regexes, so gating the easy tier alone would be a tautology,
#: and gating ``hard`` would make the suite fail for the wrong reason.
GATED_DIFFICULTIES: frozenset[str] = frozenset({"easy", "medium"})


# --------------------------------------------------------------------------
# PRNG
# --------------------------------------------------------------------------
class Rng:
    """SplitMix64. Deliberately **not** ``random.Random``.

    CI asserts the committed corpus reproduces byte-for-byte. ``random``'s
    stream is stable in practice but it is an implementation detail of CPython's
    Mersenne Twister wrapper, and a future change to ``_randbelow``'s rejection
    sampling would break every fixture in the repo with no code change. Forty
    lines of arithmetic we own removes that whole class of failure.

    ``below`` uses plain modulo, so the distribution is very slightly biased for
    non-power-of-two bounds. That is irrelevant for fixtures and is the reason
    this is not offered as a general-purpose RNG.
    """

    MASK = (1 << 64) - 1
    GAMMA = 0x9E3779B97F4A7C15

    __slots__ = ("_state",)

    def __init__(self, seed: int) -> None:
        self._state = seed & self.MASK

    def next_u64(self) -> int:
        self._state = (self._state + self.GAMMA) & self.MASK
        z = self._state
        z = ((z ^ (z >> 30)) * 0xBF58476D1CE4E5B9) & self.MASK
        z = ((z ^ (z >> 27)) * 0x94D049BB133111EB) & self.MASK
        return (z ^ (z >> 31)) & self.MASK

    def below(self, bound: int) -> int:
        if bound <= 0:
            raise ValueError("bound must be positive")
        return self.next_u64() % bound

    def between(self, low: int, high: int) -> int:
        """Inclusive on both ends."""

        return low + self.below(high - low + 1)

    def choice(self, seq: Sequence[str]) -> str:
        return seq[self.below(len(seq))]

    def digits(self, count: int) -> str:
        return "".join(str(self.below(10)) for _ in range(count))

    def letters(self, count: int) -> str:
        return "".join(chr(ord("A") + self.below(26)) for _ in range(count))


# --------------------------------------------------------------------------
# Lexicons
# --------------------------------------------------------------------------
LEXICON_FILES = ("surnames", "given-names", "cities", "gene-symbols")


@dataclass(frozen=True, slots=True)
class Lexicons:
    surnames: tuple[str, ...]
    given_names: tuple[str, ...]
    cities: tuple[tuple[str, str], ...]
    gene_symbols: tuple[str, ...]

    @classmethod
    def load(cls, root: Path) -> "Lexicons":
        def lines(name: str) -> tuple[str, ...]:
            path = root / f"{name}.txt"
            out: list[str] = []
            for line in path.read_text(encoding="utf-8").splitlines():
                entry = line.strip()
                if entry and not entry.startswith("#"):
                    out.append(entry)
            if not out:
                raise ValueError(f"lexicon {path} is empty")
            return tuple(out)

        cities = tuple(
            (city.strip(), state.strip())
            for city, _, state in (entry.partition(",") for entry in lines("cities"))
        )
        return cls(
            surnames=lines("surnames"),
            given_names=lines("given-names"),
            cities=cities,
            gene_symbols=lines("gene-symbols"),
        )


# --------------------------------------------------------------------------
# Reserved value spaces (tier 1)
# --------------------------------------------------------------------------
RESERVED_EMAIL_DOMAINS = ("example.org", "example.com", "example.net")
RESERVED_IPV4_PREFIXES = ("192.0.2.", "198.51.100.", "203.0.113.")
RESERVED_IPV6_PREFIX = "2001:db8:"
MRN_TEST_BAND = "44"
"""Documented test-only MRN band. Every MRN in the corpus is ``44`` followed by
five digits, so no fixture can coincide with a live record number."""

#: Published payment-card test numbers. Luhn-valid and reserved by the card
#: networks for exactly this purpose.
TEST_CARDS = (
    "4111-1111-1111-1111",
    "4012-8888-8888-1881",
    "5500-0000-0000-0004",
    "3400-0000-0000-009",
)

TISSUES = ("dx", "rel", "germ", "tumor", "normal", "bm", "pb")
FAMILIES = ("wgs", "wes", "rnaseq", "scrna", "atac", "methyl", "panel")
STREET_SUFFIXES = ("Street", "Avenue", "Road", "Drive", "Lane", "Court", "Boulevard")
TOOLS = ("samtools", "bcftools", "gatk", "bwa", "minimap2", "fastqc", "multiqc", "STAR")
BUILDS = ("GRCh38", "GRCh37", "hg38", "hg19", "T2T-CHM13")
GIAB = ("HG002", "HG003", "HG004", "HG008", "NA12878", "NA24385")


# --------------------------------------------------------------------------
# Fields
# --------------------------------------------------------------------------
@dataclass(frozen=True)
class FieldSpec:
    """One substitutable ``{kind}`` marker.

    ``label`` is ``None`` for a *neutral* field: its rendered value goes into
    ``must_survive`` instead of ``spans``. That is what makes over-redaction
    measurable in the same pass as recall.
    """

    kind: str
    label: str | None
    render: Callable[[Rng, str, Lexicons], str]


def _surname(rng: Rng, lex: Lexicons) -> str:
    return rng.choice(lex.surnames)


def _given(rng: Rng, lex: Lexicons) -> str:
    return rng.choice(lex.given_names)


def _iso_date(rng: Rng) -> str:
    return f"{rng.between(1935, 2024)}-{rng.between(1, 12):02d}-{rng.between(1, 28):02d}"


def _mrn_number(rng: Rng) -> str:
    return MRN_TEST_BAND + rng.digits(5)


def _ocr_garble(value: str, rng: Rng) -> str:
    """Substitute digit lookalikes the way a real OCR pass does.

    ``1`` -> ``l``, ``0`` -> ``O``, ``5`` -> ``S``. Applied to at most two
    characters so the ``hard`` tier stays recognizable to a human reader: a
    fixture nobody can read is a fixture nobody can debug.
    """

    swaps = {"1": "l", "0": "O", "5": "S", "8": "B"}
    chars = list(value)
    candidates = [index for index, char in enumerate(chars) if char in swaps]
    for _ in range(min(2, len(candidates))):
        if not candidates:
            break
        pick = candidates.pop(rng.below(len(candidates)))
        chars[pick] = swaps[chars[pick]]
    return "".join(chars)


def _name(rng: Rng, difficulty: str, lex: Lexicons) -> str:
    given, surname = _given(rng, lex), _surname(rng, lex)
    if difficulty == "easy":
        return f"{given} {surname}"
    if difficulty == "medium":
        return rng.choice((f"{given} {rng.letters(1)} {surname}", f"{given} {surname}"))
    return rng.choice((f"{given} {surname}", f"{given}  {surname}"))


def _mrn(rng: Rng, difficulty: str, _lex: Lexicons) -> str:
    number = _mrn_number(rng)
    if difficulty == "easy":
        return f"MRN {number}"
    if difficulty == "medium":
        return rng.choice((f"MRN:{number}", f"MRN #{number}", f"medical record number {number}"))
    return rng.choice((f"MRN {_ocr_garble(number, rng)}", f"M.R.N. {number}", f"MRN:{number}"))


def _ssn(rng: Rng, difficulty: str, _lex: Lexicons) -> str:
    area = rng.choice(("000", "666", f"9{rng.digits(2)}"))
    body = f"{area}-{rng.digits(2)}-{rng.digits(4)}"
    if difficulty == "easy":
        return f"SSN {body}"
    if difficulty == "medium":
        return rng.choice((body, f"SSN: {body}"))
    return body.replace("-", " ")


def _email(rng: Rng, difficulty: str, lex: Lexicons) -> str:
    local = f"{_given(rng, lex).lower()}.{_surname(rng, lex).lower()}"
    if difficulty != "easy":
        local += str(rng.between(1, 99))
    return f"{local}@{rng.choice(RESERVED_EMAIL_DOMAINS)}"


def _phone(rng: Rng, difficulty: str, _lex: Lexicons) -> str:
    line = f"01{rng.between(0, 99):02d}"
    if difficulty == "easy":
        return f"+1-555-555-{line}"
    if difficulty == "medium":
        return rng.choice((f"(555) 555-{line}", f"555.555.{line}", f"555-555-{line}"))
    return f"+1 (555) 555-{line}"


def _fax(rng: Rng, difficulty: str, lex: Lexicons) -> str:
    return f"fax {_phone(rng, difficulty, lex).lstrip('+1- ')}"


def _ipv4(rng: Rng, _difficulty: str, _lex: Lexicons) -> str:
    return rng.choice(RESERVED_IPV4_PREFIXES) + str(rng.between(1, 254))


def _ipv6(rng: Rng, _difficulty: str, _lex: Lexicons) -> str:
    # Hextets must be hex. `rng.letters` draws from a-z, which produced
    # `2001:db8:5k::42` -- not an address, so the IP rule correctly ignored it
    # and the corpus quietly reported a 0.58 IP recall that was really a broken
    # fixture. Draw from 0-9a-f instead.
    hextet = "".join("0123456789abcdef"[rng.below(16)] for _ in range(2))
    return f"{RESERVED_IPV6_PREFIX}{hextet}::{rng.between(1, 99)}"


def _url(rng: Rng, _difficulty: str, lex: Lexicons) -> str:
    return (
        f"https://records.{rng.choice(RESERVED_EMAIL_DOMAINS)}/"
        f"chart/{_surname(rng, lex).lower()}/{rng.digits(6)}"
    )


def _subject_id(rng: Rng, difficulty: str, _lex: Lexicons) -> str:
    if difficulty == "easy":
        return f"SJ-{rng.digits(4)}"
    return rng.choice((f"SJ{rng.digits(6)}", f"SJ_{rng.digits(5)}", f"SJ-{rng.digits(6)}"))


def _cohort_id(rng: Rng, _difficulty: str, _lex: Lexicons) -> str:
    return f"SJ{rng.choice(('ALL', 'AML', 'MB', 'NBL', 'OS'))}{rng.digits(3)}"


def _subject_labelled(rng: Rng, difficulty: str, lex: Lexicons) -> str:
    return f"subject id {_subject_id(rng, difficulty, lex)}"


def _sample_id(rng: Rng, difficulty: str, _lex: Lexicons) -> str:
    if difficulty == "easy":
        return f"sample: SM-{rng.digits(5)}"
    return rng.choice((f"SMP{rng.digits(5)}", f"LIB{rng.digits(6)}", f"sample = SM{rng.digits(5)}"))


def _sample_flag(rng: Rng, _difficulty: str, _lex: Lexicons) -> str:
    return f"SJ{rng.digits(4)}_{rng.choice(TISSUES)}"


def _job_name(rng: Rng, _difficulty: str, lex: Lexicons) -> str:
    return f"{rng.choice(FAMILIES)}_{_surname(rng, lex).lower()}_{rng.digits(2)}"


def _accession(rng: Rng, difficulty: str, _lex: Lexicons) -> str:
    body = f"{rng.letters(2)}{rng.digits(2)}-{rng.digits(5)}"
    return f"accession: {body}" if difficulty == "easy" else f"specimen {body}"


def _dob(rng: Rng, difficulty: str, _lex: Lexicons) -> str:
    iso = _iso_date(rng)
    if difficulty == "easy":
        return f"DOB {iso}"
    year, month, day = iso.split("-")
    if difficulty == "medium":
        return rng.choice(
            (f"DOB: {iso}", f"date of birth {int(month)}/{int(day)}/{year}", f"D.O.B. {iso}")
        )
    return f"DOB {int(month)}/{int(day)}/{year[2:]}"


def _date(rng: Rng, difficulty: str, _lex: Lexicons) -> str:
    iso = _iso_date(rng)
    if difficulty == "easy":
        return iso
    year, month, day = iso.split("-")
    months = (
        "Jan", "Feb", "Mar", "Apr", "May", "Jun",
        "Jul", "Aug", "Sep", "Oct", "Nov", "Dec",
    )
    if difficulty == "medium":
        return rng.choice((f"{int(month)}/{int(day)}/{year}", f"{int(day)} {months[int(month) - 1]} {year}"))
    return f"{months[int(month) - 1]} {int(day)}, {year}"


def _age90(rng: Rng, difficulty: str, _lex: Lexicons) -> str:
    age = rng.between(90, 106)
    if difficulty == "easy":
        return f"age {age}"
    return rng.choice((f"aged {age}", f"{age} years old", f"{age}-year-old"))


def _street(rng: Rng, _difficulty: str, lex: Lexicons) -> str:
    return f"{rng.between(100, 9899)} {_surname(rng, lex)} {rng.choice(STREET_SUFFIXES)}"


def _city_state_zip(rng: Rng, _difficulty: str, lex: Lexicons) -> str:
    city, state = lex.cities[rng.below(len(lex.cities))]
    # The ZIP is drawn independently of the city -- see the module docstring.
    return f"{city}, {state} {rng.digits(5)}"


def _npi(rng: Rng, _difficulty: str, _lex: Lexicons) -> str:
    """A 10-digit NPI that deliberately **fails** the Luhn checksum.

    A checksum-valid NPI could belong to a real clinician. A checksum-invalid
    one cannot be issued, so it is reserved by arithmetic -- and the fact that
    recall on it is still 1.00 is precisely the "validators raise the score,
    they never reject" rule being demonstrated rather than asserted.
    """

    from ..engines.validators import npi_ok

    for _ in range(32):
        candidate = "1" + rng.digits(9)
        if not npi_ok(candidate):
            return f"NPI {candidate}"
    return "NPI 1000000000"  # pragma: no cover - 32 draws never all pass


def _card(rng: Rng, _difficulty: str, _lex: Lexicons) -> str:
    return rng.choice(TEST_CARDS)


def _member_id(rng: Rng, _difficulty: str, _lex: Lexicons) -> str:
    return f"member id {rng.letters(2)}{rng.digits(7)}"


def _serial(rng: Rng, _difficulty: str, _lex: Lexicons) -> str:
    return f"serial: DV{rng.digits(6)}"


def _name_path(rng: Rng, _difficulty: str, lex: Lexicons) -> str:
    return f"{_surname(rng, lex).lower()}_{_given(rng, lex).lower()}"


# -- neutral fields: these go into must_survive ----------------------------
def _gene(rng: Rng, _difficulty: str, lex: Lexicons) -> str:
    return rng.choice(lex.gene_symbols)


def _build(rng: Rng, _difficulty: str, _lex: Lexicons) -> str:
    return rng.choice(BUILDS)


def _giab(rng: Rng, _difficulty: str, _lex: Lexicons) -> str:
    return rng.choice(GIAB)


def _tool(rng: Rng, _difficulty: str, _lex: Lexicons) -> str:
    return rng.choice(TOOLS)


def _hgvs_c(rng: Rng, _difficulty: str, _lex: Lexicons) -> str:
    return f"c.{rng.between(100, 2999)}{rng.choice(('A', 'C', 'G', 'T'))}>{rng.choice(('A', 'C', 'G', 'T'))}"


def _hgvs_p(rng: Rng, _difficulty: str, _lex: Lexicons) -> str:
    residues = ("Phe", "Arg", "Gly", "Val", "Leu", "Ser", "Trp", "Cys")
    return f"p.{rng.choice(residues)}{rng.between(10, 899)}{rng.choice(residues)}"


def _coord(rng: Rng, _difficulty: str, _lex: Lexicons) -> str:
    return f"chr{rng.between(1, 22)}:{rng.between(1000000, 199999999)}"


def _sra(rng: Rng, _difficulty: str, _lex: Lexicons) -> str:
    return f"SRR{rng.digits(8)}"


def _project(rng: Rng, _difficulty: str, _lex: Lexicons) -> str:
    return f"PRJNA{rng.digits(6)}"


def _barcode(rng: Rng, _difficulty: str, _lex: Lexicons) -> str:
    return "".join(rng.choice(("A", "C", "G", "T")) for _ in range(rng.between(6, 10)))


def _index_set(rng: Rng, _difficulty: str, _lex: Lexicons) -> str:
    return f"SI-GA-{chr(ord('A') + rng.below(8))}{rng.between(1, 12)}"


def _container(rng: Rng, _difficulty: str, _lex: Lexicons) -> str:
    # Lowercase: the OCI distribution spec requires lowercase repository names,
    # so `biocontainers/STAR:...` was never a real reference.
    return (
        f"biocontainers/{rng.choice(TOOLS).lower()}:"
        f"{rng.between(1, 9)}.{rng.between(0, 19)}.{rng.between(0, 9)}"
        f"--2024-{rng.between(1, 12):02d}-{rng.between(1, 28):02d}"
    )


def _version(rng: Rng, _difficulty: str, _lex: Lexicons) -> str:
    return f"version {rng.between(1, 9)}.{rng.between(0, 19)}.{rng.between(0, 9)}.{rng.between(0, 9)}"


def _lane(rng: Rng, _difficulty: str, _lex: Lexicons) -> str:
    return f"L00{rng.between(1, 8)}"


def _benchmark(_rng: Rng, _difficulty: str, _lex: Lexicons) -> str:
    return "GIAB HG008"


def _surrogate(rng: Rng, _difficulty: str, _lex: Lexicons) -> str:
    prefix = rng.choice(("NAME", "MRN", "SUBJ", "EMAIL", "ADDR"))
    return f"{prefix}_{''.join('0123456789abcdef'[rng.below(16)] for _ in range(12))}"


def _mask(rng: Rng, _difficulty: str, _lex: Lexicons) -> str:
    return f"[REDACTED_{rng.choice(('EMAIL', 'MRN', 'DOB', 'SJ_ID', 'NAME'))}]"


def _generalized_date(rng: Rng, _difficulty: str, _lex: Lexicons) -> str:
    return f"[DATE:{rng.between(1960, 2024)}]"


_FIELD_TABLE: tuple[FieldSpec, ...] = (
    FieldSpec("name", DeidLabel.NAME.value, _name),
    FieldSpec("name_path", DeidLabel.NAME.value, _name_path),
    FieldSpec("mrn", DeidLabel.MRN.value, _mrn),
    FieldSpec("ssn", DeidLabel.SSN.value, _ssn),
    FieldSpec("email", DeidLabel.EMAIL.value, _email),
    FieldSpec("phone", DeidLabel.PHONE.value, _phone),
    FieldSpec("fax", DeidLabel.FAX.value, _fax),
    FieldSpec("ip", DeidLabel.IP.value, _ipv4),
    FieldSpec("ipv6", DeidLabel.IP.value, _ipv6),
    FieldSpec("url", DeidLabel.URL.value, _url),
    FieldSpec("subject_id", DeidLabel.SUBJECT_ID.value, _subject_id),
    FieldSpec("cohort_id", DeidLabel.SUBJECT_ID.value, _cohort_id),
    FieldSpec("subject_labelled", DeidLabel.SUBJECT_ID.value, _subject_labelled),
    FieldSpec("sample_id", DeidLabel.SAMPLE_ID.value, _sample_id),
    FieldSpec("sample_flag", DeidLabel.SAMPLE_ID.value, _sample_flag),
    FieldSpec("job_name", DeidLabel.SLURM_JOB_NAME.value, _job_name),
    FieldSpec("accession", DeidLabel.ACCESSION.value, _accession),
    FieldSpec("dob", DeidLabel.DOB_LABELLED.value, _dob),
    FieldSpec("date", DeidLabel.DATE_BARE.value, _date),
    FieldSpec("age90", DeidLabel.AGE_OVER_89.value, _age90),
    FieldSpec("street", DeidLabel.LOCATION.value, _street),
    FieldSpec("city", DeidLabel.LOCATION.value, _city_state_zip),
    FieldSpec("npi", DeidLabel.LICENSE.value, _npi),
    FieldSpec("card", DeidLabel.ACCOUNT.value, _card),
    FieldSpec("member_id", DeidLabel.HEALTH_PLAN_ID.value, _member_id),
    FieldSpec("serial", DeidLabel.DEVICE.value, _serial),
    # -- neutral --
    FieldSpec("gene", None, _gene),
    FieldSpec("build", None, _build),
    FieldSpec("giab", None, _giab),
    FieldSpec("tool", None, _tool),
    FieldSpec("hgvs_c", None, _hgvs_c),
    FieldSpec("hgvs_p", None, _hgvs_p),
    FieldSpec("coord", None, _coord),
    FieldSpec("sra", None, _sra),
    FieldSpec("project", None, _project),
    FieldSpec("barcode", None, _barcode),
    FieldSpec("index_set", None, _index_set),
    FieldSpec("container", None, _container),
    FieldSpec("version", None, _version),
    FieldSpec("lane", None, _lane),
    FieldSpec("benchmark", None, _benchmark),
    FieldSpec("surrogate", None, _surrogate),
    FieldSpec("mask", None, _mask),
    FieldSpec("gen_date", None, _generalized_date),
)

FIELDS: dict[str, FieldSpec] = {spec.kind: spec for spec in _FIELD_TABLE}


# --------------------------------------------------------------------------
# Templates
# --------------------------------------------------------------------------
@dataclass(frozen=True)
class Template:
    tid: str
    channel: str
    difficulty: str
    body: str
    note: str = field(default="", compare=False)


TEMPLATES: tuple[Template, ...] = (
    # ------------------------------------------------------------- shell
    Template("sh01", "shell", "easy", "$ grep -n '{mrn}' /data/proj/{cohort_id}/notes.txt"),
    Template("sh02", "shell", "easy", "$ scp {email}:/scratch/{sra}_1.fastq.gz ."),
    Template("sh03", "shell", "easy", "$ ssh analyst@{ip} '{tool} view -c run.bam'"),
    Template(
        "sh04", "shell", "medium",
        "$ {tool} mpileup -f /ref/{build}.fa /data/{name_path}_R1.bam | head",
    ),
    Template(
        "sh05", "shell", "medium",
        "$ curl -s {url} -o chart.json   # {benchmark} unaffected",
    ),
    Template("sh06", "shell", "medium", "$ export PATIENT_ID={subject_id}; echo $PATIENT_ID"),
    Template(
        "sh07", "shell", "easy",
        "$ ls -l /data/proj/{cohort_id}/{name_path}_R1.fastq.gz /ref/{build}.fa",
    ),
    Template("sh08", "shell", "medium", "$ rsync -av {sample_id} backup@{ipv6}:/vault/"),
    Template(
        "sh09", "shell", "hard",
        "$ echo '{mrn}' >> audit.txt && {tool} --version   # {version}",
    ),
    Template(
        "sh10", "shell", "easy",
        "$ python3 qc.py --sample={sample_flag} --ref {build} --lane {lane}",
    ),
    Template("sh11", "shell", "medium", "$ git commit -m 'fix chart link {url}'"),
    Template("sh12", "shell", "easy", "$ psql -h {ip} -c \"select * from subjects where mrn='{mrn}'\""),
    # ------------------------------------------------------------- ocr
    Template("oc01", "ocr", "easy", "Chart Review\n{name}\n{mrn}\n{dob}"),
    Template("oc02", "ocr", "easy", "Patient Header   {name}   {mrn}   {age90}"),
    Template("oc03", "ocr", "medium", "Contact  {phone}   {email}\nAddress  {street}, {city}"),
    Template("oc04", "ocr", "medium", "Order Entry | {subject_labelled} | {accession} | {date}"),
    Template("oc05", "ocr", "hard", "CHART  {mrn}  {dob}  {name}"),
    Template("oc06", "ocr", "hard", "PT HEADER   {mrn}   {ssn}"),
    Template(
        "oc07", "ocr", "medium",
        "IGV  {coord}  {gene}  {hgvs_c}  {hgvs_p}\nSession: {giab} on {build}",
    ),
    Template("oc08", "ocr", "easy", "Insurance  {member_id}  {card}"),
    Template("oc09", "ocr", "easy", "Device  {serial}  Provider  {npi}"),
    Template("oc10", "ocr", "medium", "Terminal\n$ {tool} sort -@ 8 {name_path}_R1.bam\nJob {job_name}"),
    Template("oc11", "ocr", "hard", "FAX COVER   {fax}   {name}"),
    Template("oc12", "ocr", "easy", "Results for {name} collected {date} at {city}"),
    # ------------------------------------------------------------- notes
    Template(
        "nt01", "notes", "easy",
        "Subject {subject_id} ({name}, {dob}) consented on {date}. Contact {phone}.",
    ),
    Template(
        "nt02", "notes", "easy",
        "Re-ran alignment for {name} ({mrn}); {build} reference, {benchmark} as control.",
    ),
    Template(
        "nt03", "notes", "medium",
        "Called {gene} {hgvs_c} / {hgvs_p} at {coord} in {cohort_id}. Reviewed {date}.",
    ),
    Template(
        "nt04", "notes", "medium",
        "Sent QC report to {email} and cc'd {email}. Chart: {url}",
    ),
    Template(
        "nt05", "notes", "easy",
        "Patient name: {name}\nMRN on file: {mrn}\nHome: {street}, {city}\nAge: {age90}",
    ),
    Template(
        "nt06", "notes", "medium",
        "Specimen {accession} from {subject_labelled} logged {date}; SSN on intake form {ssn}.",
    ),
    Template(
        "nt07", "notes", "hard",
        "handoff note - {name} , {mrn} , dob {dob} - see {url}",
    ),
    Template(
        "nt08", "notes", "easy",
        "Escalated to {name} (pager {phone}); dataset {sra} from {project} is public.",
    ),
    Template(
        "nt09", "notes", "medium",
        "Insurance verification: {member_id}, guarantor {name}, {city}.",
    ),
    Template(
        "nt10", "notes", "easy",
        "Cohort {cohort_id} manifest points at /data/{name_path}_R1.fastq.gz ({lane}).",
    ),
    Template(
        "nt11", "notes", "medium",
        "Ordering provider {npi} requested repeat on {date} for {subject_id}.",
    ),
    Template(
        "nt12", "notes", "hard",
        "pt  {name}  //  {mrn}  //  {ssn}  //  {dob}",
    ),
    # ------------------------------------------------------------- agents
    Template(
        "ag01", "agents", "easy",
        "assistant: I will fetch {url} for subject {subject_id}.",
    ),
    Template(
        "ag02", "agents", "easy",
        "tool_use bash: grep -c '{mrn}' /data/{cohort_id}/manifest.tsv",
    ),
    Template(
        "ag03", "agents", "medium",
        "assistant: Summary for {name} ({dob}) - {gene} variant {hgvs_p} on {build}.",
    ),
    Template(
        "ag04", "agents", "medium",
        "tool_result: wrote /out/{name_path}_qc.html; emailed {email}",
    ),
    Template(
        "ag05", "agents", "easy",
        "user: please de-identify the note for {subject_labelled} dated {date}",
    ),
    Template(
        "ag06", "agents", "hard",
        "assistant: found  {mrn}  and  {ssn}  in the transcript; redacting.",
    ),
    Template(
        "ag07", "agents", "medium",
        "tool_use: sbatch --job-name={job_name} --sample={sample_flag} pipeline.sh",
    ),
    Template(
        "ag08", "agents", "easy",
        "assistant: {benchmark} truth set from {project} needs no consent; {giab} is public.",
    ),
    # ------------------------------------------------------------- diffs
    Template(
        "df01", "diffs", "easy",
        "--- a/manifest.tsv\n+++ b/manifest.tsv\n-{cohort_id}\told\n+{cohort_id}\t{mrn}",
    ),
    Template(
        "df02", "diffs", "easy",
        "+++ b/config.yaml\n+contact: {email}\n+endpoint: {url}\n+ref: {build}",
    ),
    Template(
        "df03", "diffs", "medium",
        "--- a/samples.csv\n+++ b/samples.csv\n+{sample_flag},{name},{dob}",
    ),
    Template(
        "df04", "diffs", "medium",
        "+++ b/hosts\n+{ip}\tredcap\n+{ipv6}\tvault\n# {version}",
    ),
    Template(
        "df05", "diffs", "easy",
        "+++ b/run.sh\n+{tool} view -T /ref/{build}.fa /data/{name_path}_R1.cram",
    ),
    Template(
        "df06", "diffs", "hard",
        "+++ b/notes.md\n+  {mrn}\n+  {name}\n+  {street}, {city}",
    ),
    # ------------------------------------------------------------- jobs
    Template(
        "jb01", "jobs", "easy",
        "slurm-{job_name}.out\nStarted {date}\nSample {sample_flag}\nRef {build}",
    ),
    Template(
        "jb02", "jobs", "easy",
        "#SBATCH --job-name={job_name}\n#SBATCH --mail-user={email}\n#SBATCH -N 1",
    ),
    Template(
        "jb03", "jobs", "medium",
        "JOBID  NAME              USER\n412391 {job_name}  analyst-a\nInput: /data/{name_path}_R1.fastq.gz",
    ),
    Template(
        "jb04", "jobs", "easy",
        "ERROR: could not open /data/proj/{cohort_id}/{name_path}_R2.fastq.gz",
    ),
    Template(
        "jb11", "jobs", "easy",
        # A bare file stem with **no leading separator**. This exists because
        # its absence hid a real bug: `name_in_path` was anchored on `(?<=/)`,
        # and the seal scrubs path fields component-wise -- splitting on the
        # separators before detection -- so the rule could never fire on the
        # very fields it was written for. Every other fixture happens to have a
        # `/` immediately before the stem, so the corpus scored 1.00 on a rule
        # that was dead in production.
        "Input file {name_path}_R1.fastq.gz not found in cwd; ref {build}",
    ),
    Template(
        "jb05", "jobs", "medium",
        "srun: node compute-07 ({ip}) reported OOM for {job_name} at {date}",
    ),
    Template(
        "jb06", "jobs", "easy",
        "Traceback: KeyError: '{mrn}' while indexing {sample_id}",
    ),
    Template(
        "jb07", "jobs", "medium",
        "Completed {job_name}: {giab} vs {build}, {sra} reads, container {container}",
    ),
    Template(
        "jb08", "jobs", "hard",
        "sacct -j 412391\n  JobName  {job_name}\n  Comment  {mrn} / {name}",
    ),
    Template(
        "jb09", "jobs", "easy",
        "Wrote report to {url}; notify {phone} on failure.",
    ),
    Template(
        "jb10", "jobs", "medium",
        "scancel: job {job_name} cancelled by request of {name} on {date}",
    ),
    # ------------------------------------------------- prescrubbed (gold tier)
    # Already-sealed text with exactly one surviving identifier. This is the
    # tier that *measures* the risk the mandatory-local-tier-first rule takes
    # on: pre-inserted placeholders could plausibly confuse a later detector.
    Template(
        "ps01", "prescrubbed", "easy",
        "Subject {surrogate} ({mask}) still lists {mrn} in the free-text field.",
    ),
    Template(
        "ps02", "prescrubbed", "easy",
        "{surrogate} seen {gen_date}; contact {email} was missed on the first pass.",
    ),
    Template(
        "ps03", "prescrubbed", "medium",
        "{mask} / {surrogate} / {gen_date} / {ssn}",
    ),
    Template(
        "ps04", "prescrubbed", "medium",
        "Sealed note: {surrogate} at {mask}. Residual {subject_id} in the filename.",
    ),
    Template(
        "ps05", "prescrubbed", "easy",
        "gen-1 surrogates {surrogate} and {surrogate} intact; new finding {phone}.",
    ),
    Template(
        "ps06", "prescrubbed", "hard",
        "{mask}{surrogate}  {mrn}",
    ),
    # ---------------------------------------------- negatives (zero gold spans)
    # Adversarial by construction. Without this, precision is meaningless: a
    # corpus of pure positives rewards a `.*` patch with a perfect score.
    Template("ng01", "negatives", "easy", "{tool} sort -@ 8 -o out.bam in.bam   # {version}"),
    Template("ng02", "negatives", "easy", "Variant {gene} {hgvs_c} {hgvs_p} at {coord} on {build}"),
    Template("ng03", "negatives", "easy", "{benchmark} truth set, {giab}, {project}, {sra}"),
    Template("ng04", "negatives", "medium", "docker run {container} {tool} --help"),
    Template("ng05", "negatives", "medium", "Index {index_set} barcode {barcode} lane {lane}"),
    Template("ng06", "negatives", "easy", "Reference {build}; aligner {tool}; region {coord}"),
    Template("ng07", "negatives", "medium", "{gene} {gene} {gene} panel on {giab} ({build})"),
    Template("ng08", "negatives", "easy", "Downloaded {sra} from {project} for {benchmark}"),
    Template("ng09", "negatives", "medium", "conda install -c bioconda {tool}=1.19 {build}"),
    Template("ng10", "negatives", "easy", "p-value 0.05, n=1234, {gene} vs {gene}"),
    Template("ng11", "negatives", "medium", "{coord} {coord} {hgvs_c} {barcode} {index_set}"),
    Template("ng12", "negatives", "hard", "{container}\n{version}\n{build}\n{gene}"),
)

_MARKER = re.compile(r"\{([a-z0-9_]+)\}")


def _unknown_fields() -> list[str]:
    missing: list[str] = []
    for template in TEMPLATES:
        for kind in _MARKER.findall(template.body):
            if kind not in FIELDS:
                missing.append(f"{template.tid}:{kind}")
    return missing


# --------------------------------------------------------------------------
# Generation
# --------------------------------------------------------------------------
def _template_seed(seed: int, tid: str, index: int) -> int:
    """Per-record seed derived from ``(seed, template id, index)``.

    Derived rather than sequential so that adding a template in the middle of
    the table does not reshuffle every record after it -- otherwise every
    corpus edit produces a whole-file diff and the byte-for-byte check stops
    being reviewable.
    """

    material = f"{seed}:{tid}:{index}".encode("utf-8")
    return int.from_bytes(hashlib.sha256(material).digest()[:8], "big")


def render_record(template: Template, seed: int, index: int, lex: Lexicons) -> dict[str, object]:
    """Build one record, tracking offsets as the text is assembled.

    Offsets come from *construction*, never from searching the finished string
    for the value. A search-based generator silently mislabels the second
    occurrence of a repeated surname, and `span.text == text[start:end]` would
    still pass.
    """

    rng = Rng(_template_seed(seed, template.tid, index))
    pieces: list[str] = []
    spans: list[dict[str, object]] = []
    must_survive: list[str] = []
    position = 0

    # `re.split` with exactly one capture group yields the strict alternation
    # [literal, kind, literal, kind, ..., literal]. Parity identifies the
    # markers, so a literal that happens to read like a field name -- `date` in
    # prose, say -- can never be mistaken for one. Empty literals must not be
    # skipped or the parity breaks.
    for offset, part in enumerate(_MARKER.split(template.body)):
        if offset % 2 == 0:
            pieces.append(part)
            position += len(part)
            continue
        spec = FIELDS[part]
        value = spec.render(rng, template.difficulty, lex)
        if spec.label is not None:
            spans.append(
                {
                    "start": position,
                    "end": position + len(value),
                    "label": spec.label,
                    "text": value,
                    "hipaa": HIPAA_BY_LABEL.get(spec.label),
                }
            )
        else:
            must_survive.append(value)
        pieces.append(value)
        position += len(value)

    text = "".join(pieces)
    if unicodedata.normalize("NFC", text) != text:  # pragma: no cover - defensive
        raise ValueError(f"{template.tid} produced non-NFC text")

    return {
        "id": f"{template.channel}-{template.tid}-{index:02d}",
        "channel": template.channel,
        "difficulty": template.difficulty,
        "text": text,
        "spans": spans,
        "must_survive": must_survive,
        "provenance": f"generated:{CORPUS_VERSION}:seed={seed}:tmpl={template.tid}",
        "license": LICENSE,
    }


def build_corpus(
    seed: int = DEFAULT_SEED,
    *,
    lexicon_dir: Path,
    per_template: int = RECORDS_PER_TEMPLATE,
) -> dict[str, list[dict[str, object]]]:
    """Generate every channel. Pure: no I/O beyond reading the lexicons."""

    missing = _unknown_fields()
    if missing:
        raise ValueError(f"templates reference unknown fields: {', '.join(missing)}")

    lex = Lexicons.load(lexicon_dir)
    by_channel: dict[str, list[dict[str, object]]] = {channel: [] for channel in CHANNELS}
    for template in TEMPLATES:
        for index in range(per_template):
            by_channel[template.channel].append(render_record(template, seed, index, lex))
    return by_channel


def dump_jsonl(records: Iterable[dict[str, object]]) -> str:
    """One compact JSON object per line, keys sorted.

    ``sort_keys`` and a fixed separator pair are what make the byte-for-byte
    check meaningful across Python versions and dict-ordering changes.
    """

    return "".join(
        json.dumps(record, sort_keys=True, ensure_ascii=False, separators=(",", ":")) + "\n"
        for record in records
    )


def corpus_fingerprint(by_channel: dict[str, list[dict[str, object]]]) -> str:
    """sha256 over every channel's serialized bytes, channel order fixed.

    ``thresholds.json`` is keyed on this, so a corpus change invalidates the
    floors rather than silently re-scoring them against a different corpus.
    """

    digest = hashlib.sha256()
    for channel in CHANNELS:
        digest.update(channel.encode("utf-8"))
        digest.update(b"\0")
        digest.update(dump_jsonl(by_channel[channel]).encode("utf-8"))
    return digest.hexdigest()


def write_corpus(
    out_dir: Path,
    seed: int = DEFAULT_SEED,
    *,
    lexicon_dir: Path | None = None,
    per_template: int = RECORDS_PER_TEMPLATE,
) -> dict[str, object]:
    """Write ``corpus/*.jsonl`` under ``out_dir``. Returns a summary."""

    lexicon_dir = lexicon_dir or (out_dir / "lexicons")
    by_channel = build_corpus(seed, lexicon_dir=lexicon_dir, per_template=per_template)
    corpus_dir = out_dir / "corpus"
    corpus_dir.mkdir(parents=True, exist_ok=True)
    for channel in CHANNELS:
        (corpus_dir / f"{channel}.jsonl").write_text(
            dump_jsonl(by_channel[channel]), encoding="utf-8"
        )
    return summarize(by_channel)


def check_corpus(
    out_dir: Path,
    seed: int = DEFAULT_SEED,
    *,
    lexicon_dir: Path | None = None,
    per_template: int = RECORDS_PER_TEMPLATE,
) -> list[str]:
    """Regenerate in memory and diff against what is on disk.

    Returns a list of human-readable differences; empty means byte-for-byte
    reproduction. This is the step-2 gate.
    """

    lexicon_dir = lexicon_dir or (out_dir / "lexicons")
    by_channel = build_corpus(seed, lexicon_dir=lexicon_dir, per_template=per_template)
    problems: list[str] = []
    for channel in CHANNELS:
        path = out_dir / "corpus" / f"{channel}.jsonl"
        expected = dump_jsonl(by_channel[channel])
        if not path.exists():
            problems.append(f"{path} is missing")
            continue
        actual = path.read_text(encoding="utf-8")
        if actual != expected:
            problems.append(
                f"{path} differs from the generator output "
                f"({len(actual)} bytes on disk vs {len(expected)} expected); "
                "rerun `autocab deid gen-corpus` and commit the result"
            )
    return problems


def summarize(by_channel: dict[str, list[dict[str, object]]]) -> dict[str, object]:
    """Counts a human reads to sanity-check a regeneration."""

    label_counts: dict[str, int] = {}
    gated_label_counts: dict[str, int] = {}
    per_channel: dict[str, int] = {}
    total_spans = 0
    for channel, records in by_channel.items():
        per_channel[channel] = len(records)
        for record in records:
            gated = record["difficulty"] in GATED_DIFFICULTIES
            for span in record["spans"]:  # type: ignore[index]
                label = str(span["label"])  # type: ignore[index]
                label_counts[label] = label_counts.get(label, 0) + 1
                if gated:
                    gated_label_counts[label] = gated_label_counts.get(label, 0) + 1
                total_spans += 1
    return {
        "corpus_version": CORPUS_VERSION,
        "records": sum(per_channel.values()),
        "spans": total_spans,
        "records_by_channel": per_channel,
        "spans_by_label": dict(sorted(label_counts.items())),
        "gated_spans_by_label": dict(sorted(gated_label_counts.items())),
        "corpus_fingerprint": corpus_fingerprint(by_channel),
    }
