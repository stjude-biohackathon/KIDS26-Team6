"""Tests for the unified AutoCAB command surface."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from click.testing import CliRunner
import pytest

from autocab.deid.models import WeightStatus
from autocab.orchestrator import run_demo
import autocab.unified_cli as unified_cli
from autocab.unified_cli import cli
from autocab.recording.events import Event
from autocab.recording.session import SessionStore


def test_help_lists_the_integrated_workflow_commands() -> None:
    result = CliRunner().invoke(cli, ["--help"])

    assert result.exit_code == 0
    for command in (
        "init",
        "dashboard",
        "record",
        "forge",
        "review",
        "verify-runtime",
        "approve",
        "reopen",
        "package",
        "status",
    ):
        assert command in result.output


@pytest.mark.parametrize("arguments", [[], ["init", "--help"], ["migrate", "--help"]])
def test_autocab_help_uses_only_autocab_branding(arguments: list[str]) -> None:
    result = CliRunner().invoke(cli, [*arguments, "--help"] if not arguments else arguments)

    assert result.exit_code == 0, result.output
    assert "wfrec" not in result.output.lower()


def test_dashboard_opens_the_local_ui_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[list[str]] = []
    monkeypatch.setattr(unified_cli, "_run_recorder", calls.append)

    result = CliRunner().invoke(cli, ["dashboard"])

    assert result.exit_code == 0, result.output
    assert calls == [["daemon", "--gui"]]


def test_dashboard_forwards_headless_remote_options(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[list[str]] = []
    monkeypatch.setattr(unified_cli, "_run_recorder", calls.append)

    result = CliRunner().invoke(
        cli,
        [
            "dashboard",
            "--port",
            "9000",
            "--host",
            "0.0.0.0",
            "--bind-all",
            "--advertise-url",
            "https://node.example",
            "--allow-remote",
            "--no-open",
            "--no-indicator",
        ],
    )

    assert result.exit_code == 0, result.output
    assert calls == [
        [
            "daemon",
            "--port",
            "9000",
            "--host",
            "0.0.0.0",
            "--bind-all",
            "--advertise-url",
            "https://node.example",
            "--allow-remote",
            "--no-indicator",
        ]
    ]


def test_dashboard_stop_does_not_open_the_ui(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[list[str]] = []
    monkeypatch.setattr(unified_cli, "_run_recorder", calls.append)

    result = CliRunner().invoke(cli, ["dashboard", "--stop"])

    assert result.exit_code == 0, result.output
    assert calls == [["daemon", "--stop"]]


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
    assert payload["next_commands"][0] == "autocab migrate --legacy"


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
    assert "Analyst:" in result.output
    assert "analyst-guide" in result.output
    assert "PHI redaction:" in result.output
    assert "regex" in result.output
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
        ["init", "--install-hooks", "--migrate-legacy", "--check", "--json"],
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
    autocab_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session, _created = SessionStore().start(title="Configured seal", analyst="analyst")
    (autocab_home / "config.toml").write_text(
        '[deid]\nengine = "gliner2-pii"\n',
        encoding="utf-8",
    )
    recorder_calls: list[list[str]] = []
    redaction_calls: list[tuple[str, str]] = []

    def redact(current_session, *, engine: str, profile: str, reseal: bool):
        redaction_calls.append((current_session.session_id, engine))
        assert profile == "balanced"
        assert reseal is False
        return SimpleNamespace(record={"engine": engine, "findings": 2})

    monkeypatch.setattr(unified_cli, "_run_recorder", recorder_calls.append)
    monkeypatch.setattr(unified_cli, "apply_phi_redaction", redact)

    result = CliRunner().invoke(cli, ["record", "finish", session.session_id, "--seal"])

    assert result.exit_code == 0, result.output
    assert recorder_calls == [["stop", session.session_id]]
    assert redaction_calls == [(session.session_id, "gliner2-pii")]
    assert "PHI redaction applied" in result.output


def test_record_redact_seals_an_archived_session(autocab_home: Path) -> None:
    store = SessionStore()
    session, _created = store.start(title="Redact me", analyst="analyst")
    store.stop(session.session_id)

    result = CliRunner().invoke(cli, ["record", "redact", session.session_id])

    assert result.exit_code == 0, result.output
    assert (session.root / "seal.json").is_file()
    assert "Engine:" in result.output
    assert "regex" in result.output


def test_record_redact_can_repair_an_existing_seal(
    autocab_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = SessionStore()
    session, _created = store.start(title="Repair seal", analyst="analyst")
    store.stop(session.session_id)
    redaction_calls: list[bool] = []

    def redact(
        current_session,
        *,
        engine: str,
        profile: str,
        reseal: bool,
    ) -> SimpleNamespace:
        assert current_session.session_id == session.session_id
        assert engine == "regex"
        assert profile == "balanced"
        redaction_calls.append(reseal)
        return SimpleNamespace(record={"engine": engine, "findings": 2})

    monkeypatch.setattr(unified_cli, "apply_phi_redaction", redact)

    result = CliRunner().invoke(
        cli,
        ["record", "redact", session.session_id, "--reseal"],
    )

    assert result.exit_code == 0, result.output
    assert redaction_calls == [True]


def test_demo_does_not_approve_by_default(tmp_path: Path) -> None:
    proposals = run_demo(tmp_path)

    assert proposals
    assert all(proposal.review_status == "pending" for proposal in proposals)
    assert all(proposal.export_path is None for proposal in proposals)


def test_record_start_uses_the_existing_recorder_service(autocab_home: Path) -> None:
    result = CliRunner().invoke(
        cli,
        ["record", "start", "--title", "Unified CLI", "--no-daemon"],
    )

    assert result.exit_code == 0, result.output
    session = SessionStore().resolve(None)
    assert session.manifest.title == "Unified CLI"


def test_forge_reports_prior_runs_when_creating_a_new_draft(
    autocab_home: Path,
    seal_now,
) -> None:
    session, _created = SessionStore().start(title="Repeated forge", analyst="analyst")
    session.writer.append(
        Event(
            source="shell",
            type="shell.command.completed",
            payload={"command": "python workflow.py", "exit_code": 0},
        )
    )
    seal_now(session)

    first = CliRunner().invoke(cli, ["forge", "--session", session.session_id, "--json"])
    second = CliRunner().invoke(cli, ["forge", "--session", session.session_id, "--json"])

    assert first.exit_code == 0, first.output
    assert second.exit_code == 0, second.output
    first_payload = json.loads(first.output)
    second_payload = json.loads(second.output)
    assert first_payload["prior_run_ids"] == []
    assert second_payload["prior_run_ids"] == [first_payload["run_id"]]
    assert second_payload["run_id"] != first_payload["run_id"]


def test_verify_runtime_cli_records_observed_verification(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, str]] = []

    def verify_runtime(_workflow, run_id: str, **details: str) -> SimpleNamespace:
        calls.append({"run_id": run_id, **details})
        return SimpleNamespace(to_dict=lambda: {"run_id": run_id, "state": "needs_review"})

    monkeypatch.setattr(unified_cli.ForgeWorkflow, "verify_runtime", verify_runtime)
    result = CliRunner().invoke(
        cli,
        [
            "verify-runtime",
            "run-1",
            "--reviewer",
            "Verifier",
            "--result",
            "passed",
            "--environment",
            "fresh prefix",
            "--platform",
            "linux-64",
            "--smoke-test",
            "python workflow.py fixture.txt",
            "--notes",
            "Expected output reproduced.",
            "--evidence-ref",
            "verification/run.log",
            "--json",
        ],
    )

    assert result.exit_code == 0, result.output
    assert calls == [
        {
            "run_id": "run-1",
            "reviewer": "Verifier",
            "result": "passed",
            "environment": "fresh prefix",
            "platform": "linux-64",
            "smoke_test": "python workflow.py fixture.txt",
            "notes": "Expected output reproduced.",
            "evidence_ref": "verification/run.log",
        }
    ]


def test_reopen_cli_invalidates_approval_through_the_workflow(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, str, str]] = []

    def reopen(
        _workflow,
        run_id: str,
        *,
        reviewer: str,
        notes: str,
    ) -> SimpleNamespace:
        calls.append((run_id, reviewer, notes))
        return SimpleNamespace(to_dict=lambda: {"run_id": run_id, "state": "blocked"})

    monkeypatch.setattr(unified_cli.ForgeWorkflow, "reopen", reopen)
    result = CliRunner().invoke(
        cli,
        [
            "reopen",
            "run-1",
            "--reviewer",
            "Maintainer",
            "--notes",
            "Correct runtime evidence.",
            "--json",
        ],
    )

    assert result.exit_code == 0, result.output
    assert calls == [("run-1", "Maintainer", "Correct runtime evidence.")]


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
        ["migrate", "--legacy", "--source", str(legacy), "--json"],
        env={"AUTOCAB_HOME": str(target)},
    )

    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["copied"] == ["session-1"]
    assert (target / "sessions" / "session-1" / "manifest.json").is_file()
