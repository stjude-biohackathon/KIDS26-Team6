"""Non-destructive migration from legacy wfrec session storage."""

from __future__ import annotations

import hashlib
import json
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from uuid import uuid4

from . import paths


@dataclass(frozen=True, slots=True)
class MigrationResult:
    """Summary of one legacy session migration."""

    copied: list[str] = field(default_factory=list)
    existing: list[str] = field(default_factory=list)
    invalid: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, list[str]]:
        return {
            "copied": self.copied,
            "existing": self.existing,
            "invalid": self.invalid,
        }


def _session_files(root: Path) -> list[Path]:
    """Return regular session files, rejecting symbolic links."""

    files: list[Path] = []
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            raise ValueError(f"symbolic link is not allowed: {path.relative_to(root)}")
        if path.is_file():
            files.append(path)
    return files


def _digest_tree(root: Path) -> str:
    """Hash relative paths and contents so a copied tree can be verified."""

    digest = hashlib.sha256()
    for path in _session_files(root):
        digest.update(path.relative_to(root).as_posix().encode("utf-8"))
        digest.update(b"\0")
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
    return digest.hexdigest()


def _validate_session(source: Path) -> None:
    manifest_path = source / "manifest.json"
    events_path = source / "events.jsonl"
    if not manifest_path.is_file() or not events_path.is_file():
        raise ValueError("missing manifest.json or events.jsonl")
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    if payload.get("session_id") != source.name:
        raise ValueError("manifest session_id does not match its folder")
    _session_files(source)


def migrate_wfrec_sessions(
    *,
    source_root: Path | None = None,
    target_root: Path | None = None,
) -> MigrationResult:
    """Copy verified legacy sessions without overwriting or deleting originals."""

    legacy = source_root or paths.legacy_home()
    destination = target_root or paths.home()
    if legacy is None or not (legacy / "sessions").is_dir():
        return MigrationResult()

    destination_sessions = destination / "sessions"
    destination_sessions.mkdir(parents=True, exist_ok=True)
    copied: list[str] = []
    existing: list[str] = []
    invalid: list[str] = []

    for source in sorted((legacy / "sessions").iterdir()):
        if not source.is_dir():
            continue
        target = destination_sessions / source.name
        if target.exists():
            existing.append(source.name)
            continue
        temporary: Path | None = None
        try:
            _validate_session(source)
            source_digest = _digest_tree(source)
            temporary = destination_sessions / f".{source.name}.migrate-{uuid4().hex}"
            shutil.copytree(source, temporary)
            if _digest_tree(temporary) != source_digest:
                raise OSError("copied session failed checksum verification")
            temporary.replace(target)
        except (OSError, ValueError, json.JSONDecodeError):
            if temporary is not None and temporary.exists():
                shutil.rmtree(temporary)
            invalid.append(source.name)
            continue
        copied.append(source.name)

    return MigrationResult(copied=copied, existing=existing, invalid=invalid)
