"""Compatibility wrappers for privacy and redaction helpers."""

from __future__ import annotations

from autocab.framework.components import SensitiveDataRedactor
from autocab.framework.config import PipelineConfig
from autocab.models import RedactionReport, TraceCluster


def redact_text(text: str) -> RedactionReport:
    """Replace obvious sensitive patterns with placeholders."""

    redactor = SensitiveDataRedactor(PipelineConfig().redact_deny_terms)
    return redactor.redact_text(text)


def redact_cluster(cluster: TraceCluster) -> RedactionReport:
    """Redact the merged text from an aggregated workflow cluster."""

    return redact_text(cluster.merged_text())
