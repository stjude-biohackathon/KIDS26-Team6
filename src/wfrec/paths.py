"""Filesystem layout for wfrec.

Two distinct roots, and the split matters:

* The **session root** (``~/.wfrec`` by default, overridable with ``WFREC_HOME``)
  holds durable data. Sessions live here so a folder is self-contained and
  movable -- a teammate can zip one and hand it to the matching lane.
* The **runtime root** (``$XDG_RUNTIME_DIR``/``$TMPDIR``/``%LOCALAPPDATA%``)
  holds the tiny sentinel that shell hooks poll on *every* prompt.

Keeping the sentinel out of ``$HOME`` is deliberate: on an HPC workstation
``$HOME`` is frequently NFS, where a ``stat`` can block for seconds or hang
outright on a stale mount. That single choice is the difference between a
15-microsecond prompt and a three-second one.
"""

from __future__ import annotations

import getpass
import os
import sys
import tempfile
from pathlib import Path

ENV_HOME = "WFREC_HOME"
ENV_RUN = "WFREC_RUN"
ENV_SESSION = "WFREC_SESSION"

#: Sub-directories created inside every session folder.
SESSION_SUBDIRS = (
    "screen/frames",
    "screen/ocr",
    "screen/video",
    "shell/remote",
    "spool",
    "agents",
    "files/diffs",
    "context",
    "jobs",
    "exports",
)


def home() -> Path:
    """Return the durable wfrec root, honouring ``WFREC_HOME``."""

    override = os.environ.get(ENV_HOME)
    if override:
        return Path(override).expanduser()
    return Path.home() / ".wfrec"


def runtime_dir() -> Path:
    """Return the fast, node-local directory holding the hook sentinel.

    Never ``$HOME``: see the module docstring. Falls back through
    ``WFREC_RUN`` -> ``XDG_RUNTIME_DIR`` -> ``LOCALAPPDATA`` -> ``TMPDIR``.
    """

    override = os.environ.get(ENV_RUN)
    if override:
        return Path(override).expanduser()

    try:
        user = getpass.getuser()
    except Exception:  # pragma: no cover - exotic environments with no passwd entry
        user = str(os.getuid()) if hasattr(os, "getuid") else "user"

    xdg = os.environ.get("XDG_RUNTIME_DIR")
    if xdg and Path(xdg).is_dir():
        return Path(xdg) / "wfrec"

    if sys.platform == "win32":  # pragma: no cover - Windows
        local = os.environ.get("LOCALAPPDATA")
        if local:
            return Path(local) / "wfrec" / "run"

    return Path(tempfile.gettempdir()) / f"wfrec-{user}"


def active_path() -> Path:
    """The one-line sentinel every shell hook reads on every prompt.

    Absence of this file means "not recording", so stopping a session is a
    single ``unlink`` and a crashed daemon degrades to silence rather than to
    bogus capture.
    """

    return runtime_dir() / "active"


def api_path() -> Path:
    """File holding ``<url>\\t<token>`` so the CLI and skill can reach the daemon."""

    return runtime_dir() / "api"


def pid_path() -> Path:
    """PID of the running daemon, so `wfrec daemon --stop` can signal it."""

    return runtime_dir() / "daemon.pid"


def boot_path() -> Path:
    """Daemon boot id, used to detect and clear a sentinel left by a dead daemon."""

    return runtime_dir() / "boot"


def state_path() -> Path:
    """Authoritative rich state (JSON), read by the CLI, GUI and daemon."""

    return home() / "state.json"


def state_lock_path() -> Path:
    """Lock guarding read-modify-write cycles on the rich state file."""

    return home() / "state.lock"


def sessions_dir() -> Path:
    """Directory holding one folder per recorded session."""

    return home() / "sessions"


def session_dir(session_id: str) -> Path:
    """Folder for a single session."""

    return sessions_dir() / session_id


def hooks_dir() -> Path:
    """Where installed shell hook scripts are materialized."""

    return home() / "hooks"


def ensure_home() -> Path:
    """Create the durable and runtime roots, and return the durable one."""

    root = home()
    root.mkdir(parents=True, exist_ok=True)
    sessions_dir().mkdir(parents=True, exist_ok=True)
    run = runtime_dir()
    run.mkdir(parents=True, exist_ok=True)
    if os.name == "posix":
        # The sentinel directory is world-readable by default under /tmp; the
        # session id and spool paths it contains are not secrets, but the API
        # token beside it is.
        os.chmod(run, 0o700)
    return root


def ensure_session_tree(session_id: str) -> Path:
    """Create the full sub-directory tree for one session and return its root."""

    root = session_dir(session_id)
    for relative in SESSION_SUBDIRS:
        (root / relative).mkdir(parents=True, exist_ok=True)
    return root
