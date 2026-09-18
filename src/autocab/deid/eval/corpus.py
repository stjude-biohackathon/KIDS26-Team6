"""Load and validate ``data/deid-eval``.

Validation is not optional politeness. ``span.text`` is a redundant copy of
``text[start:end]`` and **CI asserts equality for every span in every record**
-- the cheapest available defence against offset rot, and it will fire the first
time somebody edits a fixture by hand.
"""

from __future__ import annotations

import json
import unicodedata
from dataclasses import dataclass
import hashlib
from pathlib import Path
from typing import Iterable, Iterator

from ..labels import ALL_LABELS
from .generate import (
    CHANNELS,
    DIFFICULTIES,
    GATED_DIFFICULTIES,
    MRN_TEST_BAND,
    RESERVED_EMAIL_DOMAINS,
    RESERVED_IPV4_PREFIXES,
    RESERVED_IPV6_PREFIX,
    TEST_CARDS,
    corpus_fingerprint,
    dump_jsonl,
)

_KNOWN_LABELS = frozenset(label.value for label in ALL_LABELS)

REQUIRED_KEYS = (
    "id",
    "channel",
    "difficulty",
    "text",
    "spans",
    "must_survive",
    "provenance",
    "license",
)


def default_root() -> Path:
    """``<repo>/data/deid-eval``, resolved from this file's location.

    Resolved by walking up rather than from the cwd so that ``pytest`` and
    ``autocab deid eval`` agree regardless of where they were invoked.
    """

    here = Path(__file__).resolve()
    for parent in here.parents:
        candidate = parent / "data" / "deid-eval"
        if candidate.is_dir():
            return candidate
    raise FileNotFoundError("data/deid-eval not found above " + str(here))


@dataclass(frozen=True, slots=True)
class GoldSpan:
    start: int
    end: int
    label: str
    text: str
    hipaa: int | None


@dataclass(frozen=True, slots=True)
class Record:
    id: str
    channel: str
    difficulty: str
    text: str
    spans: tuple[GoldSpan, ...]
    must_survive: tuple[str, ...]
    provenance: str
    license: str

    @property
    def gated(self) -> bool:
        return self.difficulty in GATED_DIFFICULTIES


@dataclass(frozen=True, slots=True)
class Corpus:
    records: tuple[Record, ...]
    fingerprint: str

    def __iter__(self) -> Iterator[Record]:
        return iter(self.records)

    def __len__(self) -> int:
        return len(self.records)

    def by_channel(self, channel: str) -> tuple[Record, ...]:
        return tuple(record for record in self.records if record.channel == channel)

    def gated(self) -> tuple[Record, ...]:
        return tuple(record for record in self.records if record.gated)


def _record_from_json(raw: dict[str, object], source: Path) -> Record:
    missing = [key for key in REQUIRED_KEYS if key not in raw]
    if missing:
        raise ValueError(f"{source}: record missing keys {', '.join(missing)}")
    spans = tuple(
        GoldSpan(
            start=int(span["start"]),
            end=int(span["end"]),
            label=str(span["label"]),
            text=str(span["text"]),
            hipaa=span.get("hipaa"),  # type: ignore[arg-type]
        )
        for span in raw["spans"]  # type: ignore[union-attr]
    )
    return Record(
        id=str(raw["id"]),
        channel=str(raw["channel"]),
        difficulty=str(raw["difficulty"]),
        text=str(raw["text"]),
        spans=spans,
        must_survive=tuple(str(item) for item in raw["must_survive"]),  # type: ignore[union-attr]
        provenance=str(raw["provenance"]),
        license=str(raw["license"]),
    )


def load(root: Path | None = None) -> Corpus:
    """Read every channel file. Raises on a malformed record."""

    root = root or default_root()
    corpus_dir = root / "corpus"
    records: list[Record] = []
    raw_by_channel: dict[str, list[dict[str, object]]] = {}
    for channel in CHANNELS:
        path = corpus_dir / f"{channel}.jsonl"
        if not path.exists():
            raise FileNotFoundError(f"corpus channel file missing: {path}")
        raws: list[dict[str, object]] = []
        for line_no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            if not line.strip():
                continue
            try:
                raw = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"{path}:{line_no}: {exc}") from exc
            raws.append(raw)
            records.append(_record_from_json(raw, path))
        raw_by_channel[channel] = raws
    return Corpus(records=tuple(records), fingerprint=corpus_fingerprint(raw_by_channel))


def validate(corpus: Corpus) -> list[str]:
    """Every structural invariant the corpus must hold. Returns problems."""

    problems: list[str] = []
    seen_ids: set[str] = set()

    for record in corpus:
        where = record.id
        if record.id in seen_ids:
            problems.append(f"{where}: duplicate record id")
        seen_ids.add(record.id)
        if record.channel not in CHANNELS:
            problems.append(f"{where}: unknown channel {record.channel!r}")
        if record.difficulty not in DIFFICULTIES:
            problems.append(f"{where}: unknown difficulty {record.difficulty!r}")
        if record.license != "CC0-1.0":
            problems.append(f"{where}: unexpected license {record.license!r}")
        if unicodedata.normalize("NFC", record.text) != record.text:
            problems.append(f"{where}: text is not NFC-normalized, so offsets will shift")

        limit = len(record.text)
        ordered = sorted(record.spans, key=lambda span: (span.start, span.end))
        previous_end = -1
        for span in ordered:
            if span.label not in _KNOWN_LABELS:
                problems.append(f"{where}: unknown label {span.label!r}")
            if not 0 <= span.start < span.end <= limit:
                problems.append(
                    f"{where}: span {span.label}[{span.start}:{span.end}] outside text of length {limit}"
                )
                continue
            # The assertion that earns its keep.
            actual = record.text[span.start : span.end]
            if actual != span.text:
                problems.append(
                    f"{where}: span {span.label}[{span.start}:{span.end}] text is "
                    f"{span.text!r} but text[start:end] is {actual!r}"
                )
            if span.start < previous_end:
                problems.append(
                    f"{where}: gold spans overlap at {span.start}; gold must be disjoint"
                )
            previous_end = max(previous_end, span.end)

        if record.channel == "negatives" and record.spans:
            problems.append(f"{where}: negatives must carry zero gold spans")

        for survivor in record.must_survive:
            if survivor not in record.text:
                problems.append(f"{where}: must_survive {survivor!r} is not in the text")
            for span in record.spans:
                if survivor and survivor in span.text:
                    problems.append(
                        f"{where}: must_survive {survivor!r} overlaps gold span {span.label}"
                    )

    problems.extend(_reserved_range_problems(corpus))
    problems.extend(_leak_problems(corpus))
    return problems


#: Strings that must not appear anywhere in the corpus. A fixture carrying a
#: real institutional domain or this repo's own benchmark string as *gold* would
#: make the suite either a disclosure or a self-fulfilling failure.
FORBIDDEN_SUBSTRINGS = ("@stjude.org", "stjude.org", ".hpc.stjude", "@gmail.com")


def _leak_problems(corpus: Corpus) -> list[str]:
    problems: list[str] = []
    for record in corpus:
        lowered = record.text.casefold()
        for needle in FORBIDDEN_SUBSTRINGS:
            if needle in lowered:
                problems.append(f"{record.id}: contains forbidden substring {needle!r}")
        for span in record.spans:
            if "GIAB HG008" in span.text:
                problems.append(
                    f"{record.id}: the repo's own benchmark_dataset is labelled as PHI; "
                    "it belongs in must_survive"
                )
    return problems


def _reserved_range_problems(corpus: Corpus) -> list[str]:
    """Tier 1 conformance: every reservable identifier is in its reserved space."""

    problems: list[str] = []
    for record in corpus:
        for span in record.spans:
            text = span.text
            if span.label == "EMAIL":
                if not any(text.endswith("@" + domain) or "@" + domain in text for domain in RESERVED_EMAIL_DOMAINS):
                    problems.append(f"{record.id}: EMAIL {text!r} is outside RFC 2606 space")
            elif span.label == "URL":
                if not any(domain in text for domain in RESERVED_EMAIL_DOMAINS):
                    problems.append(f"{record.id}: URL {text!r} is outside RFC 2606 space")
            elif span.label == "IP":
                reserved = any(prefix in text for prefix in RESERVED_IPV4_PREFIXES) or (
                    RESERVED_IPV6_PREFIX in text
                )
                if not reserved:
                    problems.append(
                        f"{record.id}: IP {text!r} is outside RFC 5737 / RFC 3849 space"
                    )
            elif span.label == "MRN":
                digits = "".join(char for char in text if char.isdigit())
                if not digits.startswith(MRN_TEST_BAND):
                    problems.append(
                        f"{record.id}: MRN {text!r} is outside the documented "
                        f"{MRN_TEST_BAND}xxxxx test band"
                    )
            elif span.label == "SSN":
                digits = "".join(char for char in text if char.isdigit())
                area = digits[:3]
                if not (area in {"000", "666"} or area.startswith("9")):
                    problems.append(
                        f"{record.id}: SSN {text!r} area {area!r} is an issuable range"
                    )
            elif span.label == "PHONE" or span.label == "FAX":
                digits = "".join(char for char in text if char.isdigit())
                if "5555550" not in digits:
                    problems.append(
                        f"{record.id}: {span.label} {text!r} is outside the 555-555-01xx range"
                    )
            elif span.label == "ACCOUNT":
                if text not in TEST_CARDS:
                    problems.append(f"{record.id}: ACCOUNT {text!r} is not a published test card")
    return problems


def coverage_by_label(records: Iterable[Record], *, gated_only: bool = True) -> dict[str, int]:
    """Gold span counts per label. Feeds the per-label coverage floor test."""

    counts: dict[str, int] = {}
    for record in records:
        if gated_only and not record.gated:
            continue
        for span in record.spans:
            counts[span.label] = counts.get(span.label, 0) + 1
    return counts


def fingerprint_of_files(root: Path | None = None) -> str:
    """Fingerprint recomputed from the on-disk files, without the generator."""

    root = root or default_root()
    digest = hashlib.sha256()
    for channel in CHANNELS:
        path = root / "corpus" / f"{channel}.jsonl"
        digest.update(channel.encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
    return digest.hexdigest()


__all__ = [
    "Corpus",
    "GoldSpan",
    "Record",
    "coverage_by_label",
    "default_root",
    "dump_jsonl",
    "fingerprint_of_files",
    "load",
    "validate",
]
