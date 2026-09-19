"""Canonical AutoCAB storage and legacy wfrec discovery."""

from __future__ import annotations

import json
from pathlib import Path

from autocab import paths as autocab_paths
from autocab.migration import migrate_wfrec_sessions
from autocab.recording.session import Manifest, Session, SessionStore


def _write_session(root: Path, session_id: str, title: str) -> Path:
    session_root = root / "sessions" / session_id
    session_root.mkdir(parents=True)
    manifest = Manifest(session_id=session_id, title=title)
    (session_root / "manifest.json").write_text(
        json.dumps(manifest.to_dict()),
        encoding="utf-8",
    )
    return session_root


def test_autocab_home_precedes_legacy_override(tmp_path: Path, monkeypatch) -> None:
    canonical = tmp_path / "autocab"
    legacy = tmp_path / "wfrec"
    monkeypatch.setenv("AUTOCAB_HOME", str(canonical))
    monkeypatch.setenv("WFREC_HOME", str(legacy))

    assert autocab_paths.home() == canonical
    assert autocab_paths.legacy_home() == legacy
    assert autocab_paths.session_roots() == (
        canonical / "sessions",
        legacy / "sessions",
    )


def test_wfrec_home_remains_a_canonical_compatibility_override(tmp_path: Path, monkeypatch) -> None:
    legacy_override = tmp_path / "wfrec-override"
    monkeypatch.delenv("AUTOCAB_HOME", raising=False)
    monkeypatch.setenv("WFREC_HOME", str(legacy_override))

    assert autocab_paths.home() == legacy_override
    assert autocab_paths.legacy_home() is None


def test_session_store_discovers_canonical_and_legacy_sessions(tmp_path: Path, monkeypatch) -> None:
    canonical = tmp_path / "autocab"
    legacy = tmp_path / "wfrec"
    monkeypatch.setenv("AUTOCAB_HOME", str(canonical))
    monkeypatch.setenv("WFREC_HOME", str(legacy))
    _write_session(canonical, "2026-01-01T00-00-00_canonical", "Canonical")
    legacy_root = _write_session(legacy, "2025-01-01T00-00-00_legacy", "Legacy")

    store = SessionStore()

    assert store.list_ids() == [
        "2025-01-01T00-00-00_legacy",
        "2026-01-01T00-00-00_canonical",
    ]
    loaded = Session.load("2025-01-01T00-00-00_legacy")
    assert loaded.root == legacy_root
    assert loaded.manifest.title == "Legacy"


def test_canonical_session_wins_duplicate_id(tmp_path: Path, monkeypatch) -> None:
    canonical = tmp_path / "autocab"
    legacy = tmp_path / "wfrec"
    monkeypatch.setenv("AUTOCAB_HOME", str(canonical))
    monkeypatch.setenv("WFREC_HOME", str(legacy))
    session_id = "2026-01-01T00-00-00_duplicate"
    canonical_root = _write_session(canonical, session_id, "Canonical")
    _write_session(legacy, session_id, "Legacy")

    loaded = Session.load(session_id)

    assert loaded.root == canonical_root
    assert loaded.manifest.title == "Canonical"


def test_migrate_copies_verified_sessions_without_removing_legacy(
    tmp_path: Path,
) -> None:
    canonical = tmp_path / "autocab"
    legacy = tmp_path / "wfrec"
    session_id = "2026-01-01T00-00-00_migrate"
    legacy_session = _write_session(legacy, session_id, "Legacy")
    (legacy_session / "events.jsonl").write_text('{"seq": 1}\n', encoding="utf-8")

    result = migrate_wfrec_sessions(source_root=legacy, target_root=canonical)

    migrated = canonical / "sessions" / session_id
    assert result.copied == [session_id]
    assert (
        migrated.joinpath("manifest.json").read_bytes()
        == legacy_session.joinpath("manifest.json").read_bytes()
    )
    assert legacy_session.exists()


def test_migrate_skips_existing_and_invalid_sessions(tmp_path: Path) -> None:
    canonical = tmp_path / "autocab"
    legacy = tmp_path / "wfrec"
    existing_id = "2026-01-01T00-00-00_existing"
    invalid_id = "2026-01-01T00-00-00_invalid"
    legacy_existing = _write_session(legacy, existing_id, "Legacy")
    (legacy_existing / "events.jsonl").write_text("", encoding="utf-8")
    canonical_existing = _write_session(canonical, existing_id, "Canonical")
    (canonical_existing / "events.jsonl").write_text("", encoding="utf-8")
    _write_session(legacy, invalid_id, "Invalid")

    result = migrate_wfrec_sessions(source_root=legacy, target_root=canonical)

    assert result.existing == [existing_id]
    assert result.invalid == [invalid_id]
    assert json.loads((canonical_existing / "manifest.json").read_text())["title"] == ("Canonical")
