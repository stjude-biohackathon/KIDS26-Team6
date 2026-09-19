"""Capture collectors, one per data source.

Every collector follows the same contract: probe the environment, degrade to a
named fallback, and report *why* it degraded. A teammate on an unsupported
configuration must still get a usable session, and must be able to find out
what they are not getting without reading the source.
"""

from __future__ import annotations

from .base import Collector, CollectorStatus, Degraded

__all__ = ["Collector", "CollectorStatus", "Degraded"]
