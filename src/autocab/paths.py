"""Application-owned durable storage paths.

AutoCAB writes new durable data beneath ``~/.autocab``. ``WFREC_HOME`` remains
an accepted override so existing installations and automation keep working,
while ``AUTOCAB_HOME`` takes precedence for the unified application.
"""

from __future__ import annotations

import os
from pathlib import Path

ENV_HOME = "AUTOCAB_HOME"
LEGACY_ENV_HOME = "WFREC_HOME"


def home() -> Path:
    """Return the canonical durable root for new AutoCAB data."""

    override = os.environ.get(ENV_HOME) or os.environ.get(LEGACY_ENV_HOME)
    if override:
        return Path(override).expanduser()
    return Path.home() / ".autocab"


def legacy_home() -> Path | None:
    """Return the legacy wfrec root when it differs from the canonical root.

    When only ``WFREC_HOME`` is set, it remains the canonical root for backward
    compatibility. When ``AUTOCAB_HOME`` is set, ``WFREC_HOME`` may identify a
    separate legacy tree that should still be discovered.
    """

    canonical = home()
    if os.environ.get(ENV_HOME):
        candidate = Path(os.environ.get(LEGACY_ENV_HOME, "~/.wfrec")).expanduser()
    elif os.environ.get(LEGACY_ENV_HOME):
        return None
    else:
        candidate = Path.home() / ".wfrec"
    return candidate if candidate != canonical else None


def sessions_dir() -> Path:
    """Return the canonical directory for newly recorded sessions."""

    return home() / "sessions"


def session_roots() -> tuple[Path, ...]:
    """Return session roots in read precedence order."""

    roots = [sessions_dir()]
    legacy = legacy_home()
    if legacy is not None:
        roots.append(legacy / "sessions")
    return tuple(roots)


def session_dir(session_id: str) -> Path:
    """Return the canonical write location for a session."""

    return sessions_dir() / session_id


def find_session_dir(session_id: str) -> Path:
    """Resolve a session from canonical or legacy storage.

    The canonical root is checked first, so a migrated session wins if the
    legacy copy still exists.
    """

    for root in session_roots():
        candidate = root / session_id
        if (candidate / "manifest.json").exists():
            return candidate
    return session_dir(session_id)


def runs_dir() -> Path:
    """Return the directory containing persistent forge runs."""

    return home() / "runs"


def models_dir() -> Path:
    """Return the directory containing optional local model weights."""

    return home() / "models"


def ensure_home() -> Path:
    """Create canonical AutoCAB durable directories and return the root."""

    root = home()
    for path in (root, sessions_dir(), runs_dir(), models_dir()):
        path.mkdir(parents=True, exist_ok=True)
    return root
