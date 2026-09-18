"""Detectors. Text in, spans out -- and nothing else.

``engines/`` versus ``providers/`` is load-bearing. *Every* safety-relevant
decision -- chunking, prompt, schema, the verification ladder, the post-check,
the audit -- lives in an engine. A provider's whole job is to move bytes; it
cannot weaken verification because it never runs it.
"""

from __future__ import annotations

from .base import BaseEngine, Detector, DetectorInfo, EngineUnavailable, Span

__all__ = ["BaseEngine", "Detector", "DetectorInfo", "EngineUnavailable", "Span"]
