"""Connect recording events to AutoCAB's shared PHI redaction rules.

The recorder reuses the pipeline detector and deny list. If the shared detector
is unavailable, the timeline records that state for review.
"""

from __future__ import annotations

from dataclasses import dataclass

DEFAULT_DENY_TERMS = ("patient", "diagnosis", "pathology")


@dataclass(slots=True)
class Redacted:
    """Result of redacting one piece of captured text."""

    text: str
    findings: list[str]
    available: bool = True


class Redactor:
    """Thin adapter over AutoCAB's redactor with a safe degraded mode."""

    def __init__(self, deny_terms: tuple[str, ...] = DEFAULT_DENY_TERMS) -> None:
        self._deny_terms = deny_terms
        self._impl = None
        self._error: str | None = None
        try:
            from autocab.framework.components import SensitiveDataRedactor
            from autocab.framework.config import PipelineConfig

            terms = deny_terms or PipelineConfig().redact_deny_terms
            self._impl = SensitiveDataRedactor(tuple(terms))
        except Exception as exc:  # pragma: no cover - only when autocab is absent
            self._error = f"{type(exc).__name__}: {exc}"

    @property
    def available(self) -> bool:
        return self._impl is not None

    @property
    def error(self) -> str | None:
        return self._error

    def apply(self, text: str) -> Redacted:
        """Redact ``text``, returning the scrubbed form and which rules fired."""

        if not text:
            return Redacted(text=text, findings=[], available=self.available)
        if self._impl is None:
            return Redacted(text=text, findings=[], available=False)
        report = self._impl.redact_text(text)
        return Redacted(text=report.redacted_text, findings=list(report.findings))


_SHARED: Redactor | None = None


def shared() -> Redactor:
    """Process-wide redactor. Compiling the patterns once is worth it on the
    OCR path, which redacts every frame's text."""

    global _SHARED
    if _SHARED is None:
        _SHARED = Redactor()
    return _SHARED
