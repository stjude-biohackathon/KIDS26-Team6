"""Shared fixtures.

The recorder writes to a user-level home and a runtime directory, both of which
are redirected into ``tmp_path`` here. Without that, running the suite would
clobber a developer's real sessions -- and on a machine where someone is
actually recording, that is destructive.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


@pytest.fixture(scope="session", autouse=True)
def no_weight_downloads(tmp_path_factory):
    """**CI must never download a model weight.** Enforced, not conventional.

    ``HF_HUB_OFFLINE=1`` makes an accidental fetch fail in milliseconds instead
    of pulling 120 MB, and pointing ``HF_HUB_CACHE`` at a throwaway directory
    means no test can pollute -- or silently satisfy itself from -- a
    developer's real cache. Session-scoped and autouse, because a guard you
    have to remember to request is a guard that is missing from the test that
    needed it.

    The GLiNER engine resolves only verified files under ``WFREC_HOME``. These
    variables provide a second guard against a future library change that might
    otherwise introduce an implicit Hub request.
    """

    cache = tmp_path_factory.mktemp("hf-cache-guard")
    previous = {
        name: os.environ.get(name)
        for name in ("HF_HUB_OFFLINE", "HF_HUB_CACHE", "HF_HOME", "TRANSFORMERS_OFFLINE")
    }
    os.environ["HF_HUB_OFFLINE"] = "1"
    os.environ["TRANSFORMERS_OFFLINE"] = "1"
    os.environ["HF_HUB_CACHE"] = str(cache)
    os.environ["HF_HOME"] = str(cache)
    try:
        yield cache
    finally:
        for name, value in previous.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


def pytest_collection_modifyitems(config, items):
    """Skip the model and live-LLM tests with an explicit *reason*.

    Deliberately a hook rather than ``addopts`` in ``pyproject.toml``: CI runs a
    bare ``python -m pytest``, so the workflow needs no edit, and a developer
    running a marked file directly sees a named skip reason instead of a silent
    "0 collected" pass -- which is how somebody concludes a safety test is green
    when it never ran.
    """

    gates = {
        "model": (
            "AUTOCAB_DEID_MODEL_TESTS",
            "set AUTOCAB_DEID_MODEL_TESTS=1 to run the model tier tests",
        ),
        "live": (
            "AUTOCAB_DEID_LIVE_LLM_TESTS",
            "set AUTOCAB_DEID_LIVE_LLM_TESTS=1 to run live LLM tests (these send text to a provider)",
        ),
    }
    for item in items:
        for marker, (env_var, reason) in gates.items():
            if marker in item.keywords and os.environ.get(env_var) != "1":
                item.add_marker(pytest.mark.skip(reason=reason))


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
