"""Cross-platform bioinformatics workflow recording for AutoCAB.

Captures a working session into a per-session folder containing an append-only,
verbose JSONL event timeline plus sidecar artifacts. The timeline is the source
of truth; exports into AutoCAB's input formats are deliberately lossy
projections of it.
"""

from __future__ import annotations

__version__ = "0.1.0"

SOURCES = ("screen", "context", "shell", "agents", "files")
"""The five toggleable capture sources."""

__all__ = ["__version__", "SOURCES"]
