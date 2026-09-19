"""Tests for the unified AutoCAB command surface."""

from __future__ import annotations

import json
from pathlib import Path

from click.testing import CliRunner

from autocab.orchestrator import run_demo
from autocab.unified_cli import cli
from wfrec.session import SessionStore


def test_help_lists_the_integrated_workflow_commands():
    result = CliRunner().invoke(cli, ["--help"])

    assert result.exit_code == 0
    for command in ("record", "forge", "review", "approve", "package", "status"):
        assert command in result.output


def test_demo_does_not_approve_by_default(tmp_path: Path):
    proposals = run_demo(tmp_path)

    assert proposals
    assert all(proposal.review_status == "pending" for proposal in proposals)
    assert all(proposal.export_path is None for proposal in proposals)


def test_record_start_uses_the_existing_recorder_service(wfrec_home):
    result = CliRunner().invoke(
        cli,
        ["record", "start", "--title", "Unified CLI", "--no-daemon"],
    )

    assert result.exit_code == 0, result.output
    session = SessionStore().resolve(None)
    assert session.manifest.title == "Unified CLI"


def test_migrate_copies_legacy_sessions_into_autocab_home(tmp_path: Path):
    legacy = tmp_path / "legacy"
    target = tmp_path / "autocab"
    session = legacy / "sessions" / "session-1"
    session.mkdir(parents=True)
    (session / "manifest.json").write_text(
        json.dumps({"session_id": "session-1"}), encoding="utf-8"
    )
    (session / "events.jsonl").write_text("", encoding="utf-8")

    result = CliRunner().invoke(
        cli,
        ["migrate", "--from-wfrec", "--source", str(legacy), "--json"],
        env={"AUTOCAB_HOME": str(target)},
    )

    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["copied"] == ["session-1"]
    assert (target / "sessions" / "session-1" / "manifest.json").is_file()
