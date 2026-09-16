"""Shared fixtures.

The recorder writes to a user-level home and a runtime directory, both of which
are redirected into ``tmp_path`` here. Without that, running the suite would
clobber a developer's real sessions -- and on a machine where someone is
actually recording, that is destructive.
"""

from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


@pytest.fixture()
def terminal_log_path(tmp_path: Path) -> Path:
    """Create a synthetic terminal log without storing user activity."""

    path = tmp_path / "terminal-session.txt"
    path.write_text(
        "\n".join(
            [
                "# analyst: analyst-a",
                "# workflow_family: hg008 qc report assembly",
                "2026-07-21T09:00:00 ls results/hg008",
                "2026-07-21T09:05:00 python3 scripts/qc_summary.py --sample HG008",
                "2026-07-21T09:10:00 python3 scripts/build_report.py --output reports/hg008.md",
                "2026-07-21T09:15:00 tar -czf reports/hg008.tar.gz reports/hg008.md",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    return path


@pytest.fixture()
def wfrec_home(tmp_path, monkeypatch):
    """Redirect wfrec's durable and runtime roots into a temp directory."""

    home = tmp_path / "wfrec-home"
    run = tmp_path / "wfrec-run"
    monkeypatch.setenv("WFREC_HOME", str(home))
    monkeypatch.setenv("WFREC_RUN", str(run))

    # State is cached per-process in a module global; drop it so each test sees
    # a clean recorder rather than the previous test's redactor/state.
    import wfrec.redaction as redaction

    redaction._SHARED = None

    from wfrec import paths

    paths.ensure_home()
    return home


@pytest.fixture()
def store(wfrec_home):
    from wfrec.session import SessionStore

    return SessionStore()
