"""Surrogate minting. Deterministic within a run, unrecoverable after it.

::

    surrogate = f"{PREFIX[label]}_{hmac_sha256(key, label + b'\\0' + norm)[:6].hex()}"

Three properties, each load-bearing:

**48 bits of output.** Six bytes, twelve hex characters. Enough that a
collision inside one session is a curiosity rather than an expectation, short
enough that a human can read ``MRN_a7f3c14b9e02`` in a diff.

**The label is inside the HMAC.** A value that is simultaneously a plausible MRN
and a plausible account number must not collapse to one surrogate across labels;
with the label in the input, it cannot.

**The key is ephemeral and never leaves the process.** ``secrets.token_bytes(32)``
into a ``bytearray``, zeroed in place in a ``finally``. There is **no reverse
map, ever** -- no ``pseudonyms.json``, and no ``sha256(surface)`` in the audit
either: a six-digit MRN brute-forces against a bare digest in milliseconds, so a
"hash" would itself be the disclosure. Linkage inside a run comes from
``value_id``, a monotonic integer that carries no content and dies with the run.

The zeroing is documented as **best effort** -- CPython may have copied the bytes
during construction and we cannot reach those copies. The real guarantee is
narrower and stronger: the key is never written to disk, never passed across a
process boundary, and never serialized.
"""

from __future__ import annotations

import hmac
import re
import secrets
import unicodedata
from dataclasses import dataclass
from hashlib import sha256
from typing import Iterator

from .labels import PREFIX, DeidLabel

SURROGATE_BYTES = 6
"""Six bytes -> 12 hex chars -> 48 bits. See the module docstring."""

#: Labels normalized as "an identifier number with an optional label word":
#: strip the captured label keyword, drop every non-alphanumeric, uppercase.
#: The spec names MRN/SUBJECT_ID/SSN/PHONE/ACCESSION/DEVICE; the remaining
#: numeric HIPAA classes behave identically and are folded in here rather than
#: falling through to the generic rule, which would make ``Acct # 123-456`` and
#: ``acct#123456`` two different values.
IDENTIFIER_LABELS: frozenset[str] = frozenset(
    {
        DeidLabel.MRN.value,
        DeidLabel.SUBJECT_ID.value,
        DeidLabel.SSN.value,
        DeidLabel.PHONE.value,
        DeidLabel.FAX.value,
        DeidLabel.ACCESSION.value,
        DeidLabel.DEVICE.value,
        DeidLabel.SAMPLE_ID.value,
        DeidLabel.ACCOUNT.value,
        DeidLabel.HEALTH_PLAN_ID.value,
        DeidLabel.LICENSE.value,
        DeidLabel.VEHICLE.value,
        DeidLabel.BIOMETRIC.value,
    }
)

#: Leading tokens stripped from an identifier surface. Only *known* keywords are
#: removed. A blanket "strip any leading letters" rule would turn ``SJ-1234``
#: into ``1234`` and collapse it with every other bare four-digit subject id --
#: losing exactly the prefix that distinguishes an institutional identifier.
_LABEL_KEYWORDS: frozenset[str] = frozenset(
    """
    MRN MEDICAL RECORD SSN SOCIAL SECURITY DOB BIRTH DATE OF PHONE TEL
    TELEPHONE MOBILE CELL CONTACT FAX ACCESSION SPECIMEN SAMPLE SUBJECT
    PARTICIPANT PATIENT STUDY ACCOUNT ACCT MEMBER POLICY SUBSCRIBER INSURANCE
    PLAN HEALTH NPI DEA LICENSE LICENCE CERT CERTIFICATE SERIAL SN DEVICE VIN
    PLATE NUMBER NO NUM ID IDS
    """.split()
)

_TITLES: frozenset[str] = frozenset(
    {
        "dr", "dr.", "doctor", "mr", "mr.", "mrs", "mrs.", "ms", "ms.",
        "miss", "prof", "prof.", "professor", "md", "m.d.", "do", "d.o.",
        "phd", "ph.d.", "rn", "np", "pa", "dds", "dvm", "jr", "jr.", "sr",
        "sr.", "ii", "iii", "iv", "esq", "esq.",
    }
)

_NON_ALNUM = re.compile(r"[^0-9A-Za-z]+")
_YEAR = re.compile(r"\b(19\d{2}|20\d{2})\b")

GENERALIZED_AGE = "AGE_90_PLUS"


def _collapse(value: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", value).casefold().split())


def _strip_label_keywords(surface: str) -> str:
    """Drop leading known label keywords and their separators."""

    tokens = re.split(r"(\s+|[:#=]+)", surface)
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if not token or not token.strip():
            index += 1
            continue
        bare = _NON_ALNUM.sub("", token).upper()
        if bare and bare in _LABEL_KEYWORDS:
            index += 1
            continue
        if token.strip() in {":", "#", "=", "#:", ":#"}:
            index += 1
            continue
        break
    return "".join(tokens[index:])


def normalize_for_label(label: str, surface: str) -> str:
    """The HMAC input's value half. Pure, and the reason surrogates are stable.

    Because pseudonymization is a pure function of ``(label,
    normalized_surface)`` within a run, the seal caches the *rewritten string*
    rather than the spans -- which is what makes tens of thousands of OCR events
    sharing near-identical text affordable.
    """

    if label in IDENTIFIER_LABELS:
        stripped = _strip_label_keywords(surface)
        return _NON_ALNUM.sub("", stripped).upper()
    if label == DeidLabel.EMAIL.value:
        # casefold the whole address. Deliberately *not* Gmail-style dot
        # stripping or plus-tag removal: those are provider-specific routing
        # rules, and applying them would silently merge two addresses that a
        # non-Gmail server treats as different people.
        return surface.strip().casefold()
    if label == DeidLabel.NAME.value:
        words = [word for word in _collapse(surface).split() if word not in _TITLES]
        # Middle initials are kept: `Jane A Smith` and `Jane B Smith` are
        # different people and must not share a surrogate.
        return " ".join(words)
    return _collapse(surface)


def year_of(surface: str) -> str:
    """The year to keep when generalizing a date.

    ``unknown`` for a two-digit year rather than a pivot guess: choosing between
    1968 and 2068 would be inventing data inside a control whose whole job is to
    remove it.
    """

    match = _YEAR.search(surface)
    return match.group(1) if match else "unknown"


@dataclass(frozen=True, slots=True)
class Surrogate:
    """One minted replacement plus the audit fields that may be recorded."""

    label: str
    text: str
    value_id: int
    first_seen: bool
    generalized: bool = False


class Pseudonymizer:
    """Mints surrogates under an ephemeral per-run key.

    Use as a context manager so the key is zeroed even on an exception path::

        with Pseudonymizer() as pseudo:
            ...
    """

    __slots__ = ("_key", "_value_ids", "_surrogates", "_collisions", "_closed")

    def __init__(self, key: bytes | bytearray | None = None) -> None:
        # A caller-supplied key exists only for tests (`fixed_key` fixture).
        # Production always mints its own and discards it.
        self._key = bytearray(key) if key is not None else bytearray(secrets.token_bytes(32))
        self._value_ids: dict[tuple[str, str], int] = {}
        self._surrogates: dict[str, tuple[str, str]] = {}
        self._collisions: list[dict[str, object]] = []
        self._closed = False

    # -- lifecycle ---------------------------------------------------------
    def __enter__(self) -> "Pseudonymizer":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def close(self) -> None:
        """Zero the key in place. Idempotent."""

        for index in range(len(self._key)):
            self._key[index] = 0
        self._closed = True

    def __repr__(self) -> str:
        return "Pseudonymizer(key=<discarded>)"

    def __reduce__(self):  # pragma: no cover - exercised by the audit test
        raise TypeError(
            "Pseudonymizer is not serializable: the key must never leave the process"
        )

    def __getstate__(self):  # pragma: no cover - belt and braces with __reduce__
        raise TypeError(
            "Pseudonymizer is not serializable: the key must never leave the process"
        )

    # -- minting -----------------------------------------------------------
    def surrogate_for(self, label: str, surface: str) -> Surrogate:
        """Mint (or recall) the replacement for ``surface`` under ``label``."""

        if self._closed:
            raise RuntimeError("Pseudonymizer key has been discarded")

        if label == DeidLabel.AGE_OVER_89.value:
            return Surrogate(label, GENERALIZED_AGE, value_id=0, first_seen=False, generalized=True)
        if label in {DeidLabel.DATE_BARE.value, DeidLabel.DOB_LABELLED.value}:
            return Surrogate(
                label,
                f"[DATE:{year_of(surface)}]",
                value_id=0,
                first_seen=False,
                generalized=True,
            )

        norm = normalize_for_label(label, surface)
        key = (label, norm)
        first_seen = key not in self._value_ids
        if first_seen:
            self._value_ids[key] = len(self._value_ids) + 1
        value_id = self._value_ids[key]

        digest = hmac.new(
            bytes(self._key), label.encode("utf-8") + b"\0" + norm.encode("utf-8"), sha256
        ).digest()
        text = f"{PREFIX.get(label, 'ID')}_{digest[:SURROGATE_BYTES].hex()}"

        previous = self._surrogates.get(text)
        if previous is None:
            self._surrogates[text] = key
        elif previous != key:
            # Two different values under the same label minted the same
            # surrogate. Recorded in `seal.json` as a count, never with the
            # values, so a reviewer can see it happened at all.
            self._collisions.append(
                {"surrogate": text, "label": label, "value_ids": [self._value_ids[previous], value_id]}
            )
        return Surrogate(label, text, value_id=value_id, first_seen=first_seen)

    # -- reporting ---------------------------------------------------------
    @property
    def distinct_values(self) -> int:
        return len(self._value_ids)

    @property
    def collisions(self) -> list[dict[str, object]]:
        return list(self._collisions)

    def counts_by_label(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for label, _norm in self._value_ids:
            counts[label] = counts.get(label, 0) + 1
        return counts

    def iter_value_ids(self) -> Iterator[tuple[str, int]]:
        """``(label, value_id)`` pairs. Never the surface -- by construction the
        normalized value is not exposed by any public accessor."""

        for (label, _norm), value_id in self._value_ids.items():
            yield label, value_id
