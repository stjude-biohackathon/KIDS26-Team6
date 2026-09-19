"""Tests for the unified AutoCAB command surface."""

from __future__ import annotations

import json
from pathlib import Path

from click.testing import CliRunner

from autocab.orchestrator import run_demo
from autocab.unified_cli import cli
from wfrec.session import SessionStore


def test_help_lists_the_integrated_workflow_commands() -> None:
    result = CliRunner().invoke(cli, ["--help"])

    assert result.exit_code == 0
    for command in ("init", "record", "forge", "review", "approve", "package", "status"):
        assert command in result.output


def test_init_creates_workspace_and_saves_analyst(tmp_path: Path) -> None:
    home = tmp_path / "autocab"
    runtime = tmp_path / "run"

    result = CliRunner().invoke(
        cli,
        ["init", "--analyst", "analyst-a", "--json"],
        env={"AUTOCAB_HOME": str(home), "WFREC_RUN": str(runtime)},
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["config_created"] is True
    assert payload["analyst"] == "analyst-a"
    assert (
        (home / "config.toml")
        .read_text(encoding="utf-8")
        .startswith("# AutoCAB user configuration.")
    )
    state = json.loads((home / "state.json").read_text(encoding="utf-8"))
    assert state["default_analyst"] == "analyst-a"
    for directory in ("sessions", "runs", "models", "hooks"):
        assert (home / directory).is_dir()


def test_init_is_idempotent_and_preserves_existing_config(tmp_path: Path) -> None:
    home = tmp_path / "autocab"
    runtime = tmp_path / "run"
    environment = {"AUTOCAB_HOME": str(home), "WFREC_RUN": str(runtime)}
    config = home / "config.toml"
    config.parent.mkdir(parents=True)
    config.write_text('[deid]\nengine = "gliner"\n', encoding="utf-8")

    result = CliRunner().invoke(cli, ["init", "--json"], env=environment)

    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["config_created"] is False
    assert config.read_text(encoding="utf-8") == '[deid]\nengine = "gliner"\n'


def test_init_reports_legacy_sessions(tmp_path: Path) -> None:
    home = tmp_path / "autocab"
    legacy = tmp_path / "wfrec"
    runtime = tmp_path / "run"
    session = legacy / "sessions" / "session-1"
    session.mkdir(parents=True)
    (session / "manifest.json").write_text("{}", encoding="utf-8")

    result = CliRunner().invoke(
        cli,
        ["init", "--json"],
        env={
            "AUTOCAB_HOME": str(home),
            "WFREC_HOME": str(legacy),
            "WFREC_RUN": str(runtime),
        },
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["legacy_sessions"] == 1
    assert payload["next_commands"][0] == "autocab migrate --from-wfrec"


def test_init_reports_an_unwritable_home_as_a_cli_error(tmp_path: Path) -> None:
    home = tmp_path / "not-a-directory"
    home.write_text("occupied", encoding="utf-8")

    result = CliRunner().invoke(
        cli,
        ["init"],
        env={"AUTOCAB_HOME": str(home), "WFREC_RUN": str(tmp_path / "run")},
    )

    assert result.exit_code == 1
    assert "Could not initialize AutoCAB" in result.output


def test_demo_does_not_approve_by_default(tmp_path: Path) -> None:
    proposals = run_demo(tmp_path)

    assert proposals
    assert all(proposal.review_status == "pending" for proposal in proposals)
    assert all(proposal.export_path is None for proposal in proposals)


def test_record_start_uses_the_existing_recorder_service(wfrec_home: Path) -> None:
    result = CliRunner().invoke(
        cli,
        ["record", "start", "--title", "Unified CLI", "--no-daemon"],
    )

    assert result.exit_code == 0, result.output
    session = SessionStore().resolve(None)
    assert session.manifest.title == "Unified CLI"


def test_migrate_copies_legacy_sessions_into_autocab_home(tmp_path: Path) -> None:
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
