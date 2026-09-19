"""Shared engine scaffolding.

An engine is a :class:`~autocab.deid.spans.Detector`: batch in, spans out. It
does **not** know about policy, pseudonyms, rendering, or the audit -- those
belong to the caller, which is how a new engine can never weaken an existing
control.
"""

from __future__ import annotations

import hashlib
from typing import Iterable, Sequence

from ..spans import Detector, DetectorInfo, Span

__all__ = ["Detector", "DetectorInfo", "Span", "EngineUnavailable", "BaseEngine", "table_digest"]


class EngineUnavailable(RuntimeError):
    """Raised when an engine cannot run.

    The seal converts this into a refusal to produce a sealed artifact.
    ``autocab.recording.redaction.Redacted.available``'s degraded mode is right for
    *capture* -- losing a frame's redaction is better than losing the recorder --
    and wrong for a *seal*, where the whole point is the guarantee.
    """

    def __init__(self, engine: str, reason: str) -> None:
        super().__init__(f"{engine} unavailable: {reason}")
        self.engine = engine
        self.reason = reason


class BaseEngine:
    """Minimal ``Detector`` implementation with a per-string hook.

    Subclasses override :meth:`detect_one`. ``detect`` stays the public,
    batch-shaped entry point so that a model engine can override it with a real
    batched forward pass and every call site already passes a list.
    """

    name = "base"
    kind = "regex"
    version = ""

    def info(self) -> DetectorInfo:
        return DetectorInfo(name=self.name, kind=self.kind, available=True, version=self.version)

    def detect_one(self, text: str) -> list[Span]:  # pragma: no cover - abstract
        raise NotImplementedError

    def detect(self, texts: Sequence[str]) -> list[list[Span]]:
        return [self.detect_one(text) for text in texts]


def table_digest(entries: Iterable[tuple[str, ...]]) -> str:
    """Stable sha256 over a pattern table, for ``seal.json``.

    Sorted and newline-joined so the digest depends on the *content* of the
    rules and not on the order they happen to be declared in -- otherwise
    reordering the table for readability would invalidate every prior seal
    record's integrity comparison.
    """

    digest = hashlib.sha256()
    for row in sorted("\x1f".join(entry) for entry in entries):
        digest.update(row.encode("utf-8"))
        digest.update(b"\n")
    return digest.hexdigest()
