"""Tests for the unified AutoCAB command surface."""

from __future__ import annotations

import json
from pathlib import Path

from click.testing import CliRunner
import pytest

from autocab.deid.models import WeightStatus
from autocab.orchestrator import run_demo
import autocab.unified_cli as unified_cli
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


def test_init_guides_setup_when_interactive(tmp_path: Path) -> None:
    home = tmp_path / "autocab"
    environment = {
        "AUTOCAB_HOME": str(home),
        "WFREC_HOME": str(home),
        "WFREC_RUN": str(tmp_path / "run"),
    }

    result = CliRunner().invoke(
        cli,
        ["init", "--interactive"],
        input="analyst-guide\nregex\nn\nn\n",
        env=environment,
    )

    assert result.exit_code == 0, result.output
    assert "Analyst: analyst-guide" in result.output
    assert "PHI redaction: regex" in result.output
    assert "Install shell capture hooks?" in result.output
    assert "Run readiness checks?" in result.output


def test_init_rejects_interactive_json_output(tmp_path: Path) -> None:
    result = CliRunner().invoke(
        cli,
        ["init", "--interactive", "--json"],
        env={"AUTOCAB_HOME": str(tmp_path / "autocab")},
    )

    assert result.exit_code == 2
    assert "Use --no-interactive with --json" in result.output


@pytest.mark.parametrize("engine", ["gliner", "gliner2-pii"])
def test_init_configures_a_verified_model_engine(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    engine: str,
) -> None:
    home = tmp_path / "autocab"
    config = home / "config.toml"
    config.parent.mkdir(parents=True)
    config.write_text(
        '[deid]\nengine = "regex"\n\n[deid.llm]\nenabled = false\n',
        encoding="utf-8",
    )
    status = WeightStatus(home / "models" / engine, True, True)

    def prepared(selected: str, *, fetch: bool) -> WeightStatus:
        assert selected == engine
        assert fetch is True
        return status

    monkeypatch.setattr(unified_cli, "prepare_model", prepared)
    result = CliRunner().invoke(
        cli,
        ["init", "--redaction", engine, "--fetch-model", "--json"],
        env={"AUTOCAB_HOME": str(home), "WFREC_RUN": str(tmp_path / "run")},
    )

    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["redaction_engine"] == engine
    saved = config.read_text(encoding="utf-8")
    assert f'engine = "{engine}"' in saved
    assert "[deid.llm]\nenabled = false" in saved


def test_failed_model_setup_keeps_the_previous_configuration(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "autocab"
    config = home / "config.toml"
    config.parent.mkdir(parents=True)
    original = '[deid]\nengine = "regex"\n'
    config.write_text(original, encoding="utf-8")

    def fail_model(_engine: str, *, fetch: bool) -> WeightStatus:
        assert fetch is True
        raise unified_cli.ModelWeightsError("download failed")

    monkeypatch.setattr(unified_cli, "prepare_model", fail_model)
    result = CliRunner().invoke(
        cli,
        ["init", "--redaction", "gliner", "--fetch-model"],
        env={"AUTOCAB_HOME": str(home), "WFREC_RUN": str(tmp_path / "run")},
    )

    assert result.exit_code == 1
    assert "download failed" in result.output
    assert config.read_text(encoding="utf-8") == original


def test_init_runs_explicit_setup_actions(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def hooks() -> list[dict[str, str]]:
        return [{"shell": "zsh", "status": "installed"}]

    def migration() -> dict[str, list[str]]:
        return {"copied": ["session-1"], "skipped": []}

    def readiness() -> dict[str, object]:
        return {
            "available_sources": 4,
            "total_sources": 5,
            "sources": {},
            "redaction_engine": "regex",
            "redaction_ready": True,
            "warnings": [],
        }

    monkeypatch.setattr(unified_cli, "install_shell_hooks", hooks)
    monkeypatch.setattr(unified_cli, "migrate_legacy_sessions", migration)
    monkeypatch.setattr(unified_cli, "readiness_summary", readiness)

    result = CliRunner().invoke(
        cli,
        ["init", "--install-hooks", "--migrate-wfrec", "--check", "--json"],
        env={
            "AUTOCAB_HOME": str(tmp_path / "autocab"),
            "WFREC_RUN": str(tmp_path / "run"),
        },
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["hooks"][0]["status"] == "installed"
    assert payload["migration"]["copied"] == ["session-1"]
    assert payload["readiness"]["available_sources"] == 4


def test_record_finish_uses_the_configured_redaction_engine(
    wfrec_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session, _created = SessionStore().start(title="Configured seal", analyst="analyst")
    (wfrec_home / "config.toml").write_text(
        '[deid]\nengine = "gliner2-pii"\n',
        encoding="utf-8",
    )
    calls: list[list[str]] = []
    monkeypatch.setattr(unified_cli, "_run_wfrec", calls.append)

    result = CliRunner().invoke(cli, ["record", "finish", session.session_id, "--seal"])

    assert result.exit_code == 0, result.output
    assert calls[-1] == ["seal", session.session_id, "--engine", "gliner2-pii"]


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
