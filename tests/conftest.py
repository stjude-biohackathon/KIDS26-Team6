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

    # Same treatment for the de-identification allowlist: it caches
    # `~/.wfrec/deid-allowlist.txt` process-wide, so without this a test that
    # plants a user allowlist leaks its entries into every later test.
    from autocab.deid import allowlist as deid_allowlist

    deid_allowlist._SHARED = None

    from wfrec import paths

    paths.ensure_home()
    return home


@pytest.fixture()
def store(wfrec_home):
    from wfrec.session import SessionStore

    return SessionStore()


@pytest.fixture()
def seal_now():
    """Seal a session so the export gates let it through.

    ``export_session`` and ``autocab.session_bundle`` both refuse an unsealed
    session -- that refusal is the control, so tests that exercise *export* have
    to seal first rather than have the gate weakened for them. A fixed key keeps
    surrogates stable across a test run, which makes assertions on them
    possible.
    """

    def _seal(session, **kwargs):
        from wfrec.seal import seal_session

        kwargs.setdefault("key", b"\x2a" * 32)
        kwargs.setdefault("engine_label", "regex")
        # Test sessions are usually still active; --force stops them first.
        kwargs.setdefault("force", True)
        return seal_session(session, **kwargs)

    return _seal


@pytest.fixture()
def fixed_key() -> bytes:
    """A deterministic pseudonym key. **Tests only.**

    Production always mints ``secrets.token_bytes(32)`` and discards it; a
    caller-supplied key exists so that a surrogate can be asserted on.
    """

    return b"\x2a" * 32
