"""CLI and control-API surface."""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from wfrec.api import create_app
from wfrec.cli import main
from wfrec.recorder import NoActiveSession, Recorder
from wfrec.session import Session


@pytest.fixture()
def client(wfrec_home):
    recorder = Recorder(supervise=False)
    app = create_app(recorder, token="test-token")
    with TestClient(app) as test_client:
        test_client.headers.update({"Authorization": "Bearer test-token"})
        yield test_client
    recorder.shutdown()


# ----------------------------------------------------------------------- api
def test_health_is_unauthenticated(client):
    response = client.get("/health", headers={})
    assert response.status_code == 200
    assert response.json()["ok"] is True


def test_endpoints_require_the_token(wfrec_home):
    """A fresh client with no default header, since TestClient merges headers."""

    recorder = Recorder(supervise=False)
    with TestClient(create_app(recorder, token="test-token")) as anon:
        assert anon.get("/status").status_code == 401
        assert anon.get("/status", headers={"Authorization": "Bearer wrong"}).status_code == 401
        assert anon.get("/status", headers={"Authorization": "Bearer test-token"}).status_code == 200
    recorder.shutdown()


def test_start_pause_resume_stop_over_http(client):
    started = client.post("/sessions/start", json={"title": "A", "analyst": "mgatta42"}).json()
    session_id = started["session"]["id"]
    assert started["session"]["status"] == "active"

    paused = client.post("/sessions/pause", json={"reason": "waiting on job"}).json()
    assert paused["session"]["status"] == "paused"

    resumed = client.post("/sessions/resume", json={}).json()
    assert resumed["session"]["status"] == "active"

    stopped = client.post("/sessions/stop", json={}).json()
    assert stopped["stopped"] == session_id


def test_toggle_source_over_http(client):
    client.post("/sessions/start", json={"title": "A"})
    result = client.post("/sources/screen", json={"enabled": False}).json()
    assert result["sources"]["screen"] is False
    result = client.post("/sources/screen", json={"enabled": True}).json()
    assert result["sources"]["screen"] is True


def test_shell_output_endpoint_reports_unsupported_backend(client):
    client.post("/sessions/start", json={"title": "A"})

    response = client.post("/shell-output", json={"enabled": True})
    status = client.get("/status").json()

    assert response.status_code == 400
    assert "hook-spool" in response.json()["detail"]
    assert status["shell_output"] is False
    assert status["shell_output_available"] is False
    assert "stdout or stderr" in status["shell_output_reason"]


def test_unknown_source_is_a_400(client):
    client.post("/sessions/start", json={"title": "A"})
    response = client.post("/sources/telepathy", json={"enabled": True})
    assert response.status_code == 400
    assert "Unknown source" in response.json()["detail"]


def test_operations_without_a_session_are_a_409(client):
    response = client.post("/notes", json={"text": "hi"})
    assert response.status_code == 409
    assert "wfrec start" in response.json()["detail"]


def test_missing_session_is_a_404(client):
    response = client.get("/sessions/2026-01-01T00-00-00_nope/events")
    assert response.status_code == 404


def test_note_reports_what_it_redacted(client):
    client.post("/sessions/start", json={"title": "A"})
    result = client.post(
        "/notes", json={"text": "bob@stjude.org SJ001234", "label": "why", "pasted": True}
    ).json()
    assert set(result["redactions"]) >= {"email", "sj_id"}


def test_events_endpoint_paginates(client):
    started = client.post("/sessions/start", json={"title": "A"}).json()
    session_id = started["session"]["id"]
    for index in range(10):
        client.post("/markers", json={"label": f"m{index}"})

    page = client.get(f"/sessions/{session_id}/events?limit=3&offset=0").json()
    assert len(page["events"]) == 3
    assert page["total"] >= 12


def test_doctor_endpoint_reports_sources(client):
    report = client.get("/doctor").json()
    assert "sources" in report and "shell" in report["sources"]
    assert "wfrec_version" in report


def test_ui_injects_the_token_not_a_placeholder(client):
    body = client.get("/").text
    assert "@WFREC_TOKEN@" not in body
    assert "test-token" in body
    assert "Codex, Claude Code" in body
    assert "providers:" in body


def test_export_endpoint_writes_files(client):
    client.post("/sessions/start", json={"title": "A", "analyst": "a"})
    status = client.get("/status").json()
    session = Session.load(status["session"]["id"])
    from wfrec.events import Event

    session.writer.append(
        Event(source="shell", type="shell.command.completed", payload={"command": "ls"})
    )
    result = client.post("/export", json={"formats": ["autocab"]}).json()
    assert len(result["written"]) == 2


# ----------------------------------------------------------------------- cli
def test_cli_start_status_stop(wfrec_home, capsys):
    assert main(["start", "--title", "CLI session", "--analyst", "mgatta42", "--no-daemon"]) == 0
    assert "Recording session" in capsys.readouterr().out

    assert main(["status"]) == 0
    out = capsys.readouterr().out
    assert "CLI session" in out and "mgatta42" in out

    assert main(["stop"]) == 0
    assert "Stopped" in capsys.readouterr().out


def test_cli_json_flag_works_on_either_side_of_the_subcommand(wfrec_home, capsys):
    main(["start", "--title", "A", "--no-daemon"])
    capsys.readouterr()

    assert main(["--json", "status"]) == 0
    json.loads(capsys.readouterr().out)

    assert main(["status", "--json"]) == 0
    json.loads(capsys.readouterr().out)


def test_cli_source_accepts_on_off_words(wfrec_home, capsys):
    main(["start", "--title", "A", "--no-daemon"])
    capsys.readouterr()
    assert main(["source", "shell", "off"]) == 0
    output = capsys.readouterr().out
    assert "Updated capture sources" in output
    assert "shell" not in output
    assert main(["source", "shell", "on"]) == 0


def test_cli_shell_output_reports_unsupported_backend(wfrec_home, capsys):
    main(["start", "--title", "A", "--no-daemon"])
    capsys.readouterr()

    assert main(["shell-output", "on"]) == 1
    assert "unavailable with hook-spool" in capsys.readouterr().err

    assert main(["shell-output", "off"]) == 0
    output = capsys.readouterr().out
    assert "Shell output capture" in output
    assert "Status" in output and "Off" in output


def test_cli_rejects_bad_toggle_word(wfrec_home):
    with pytest.raises(SystemExit):
        main(["source", "shell", "maybe"])


def test_cli_note_without_session_exits_nonzero(wfrec_home, capsys):
    assert main(["note", "hello"]) == 2
    assert "wfrec start" in capsys.readouterr().err


def test_cli_events_filters_by_source(wfrec_home, capsys):
    main(["start", "--title", "A", "--no-daemon"])
    main(["mark", "checkpoint"])
    capsys.readouterr()
    assert main(["events", "--source", "context"]) == 0
    out = capsys.readouterr().out
    assert "marker.user" in out
    assert "session.created" not in out


def test_cli_lists_sessions_and_events_in_tables(wfrec_home, capsys):
    main(["start", "--title", "Table test", "--analyst", "tester", "--no-daemon"])
    capsys.readouterr()

    assert main(["sessions"]) == 0
    sessions_output = capsys.readouterr().out
    assert "Sessions" in sessions_output
    assert all(
        heading in sessions_output
        for heading in ("Session", "Status", "Analyst", "Title")
    )
    assert "Table test" in sessions_output

    assert main(["events"]) == 0
    events_output = capsys.readouterr().out
    assert "Timeline events" in events_output
    assert all(
        heading in events_output
        for heading in ("Seq", "Time", "Type", "Summary")
    )


def test_cli_renders_user_text_literally(wfrec_home, capsys):
    title = "[bold]literal title[/bold]"

    assert main(["start", "--title", title, "--no-daemon"]) == 0

    assert title in capsys.readouterr().out


def test_cli_hooks_install_is_idempotent(wfrec_home, tmp_path, monkeypatch, capsys):
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setattr("pathlib.Path.home", lambda: fake_home)
    (fake_home / ".bashrc").write_text("export EDITOR=vim\n", encoding="utf-8")

    for _ in range(3):
        assert main(["hooks", "install", "--shell", "bash"]) == 0
    capsys.readouterr()

    rc = (fake_home / ".bashrc").read_text(encoding="utf-8")
    assert rc.count(">>> wfrec hook >>>") == 1
    assert "export EDITOR=vim" in rc

    assert main(["hooks", "uninstall", "--shell", "bash"]) == 0
    rc = (fake_home / ".bashrc").read_text(encoding="utf-8")
    assert "wfrec" not in rc
    assert "export EDITOR=vim" in rc


def test_cli_doctor_renders(wfrec_home, capsys):
    assert main(["doctor"]) == 0
    out = capsys.readouterr().out
    assert "wfrec 0.1.0" in out
    assert "System" in out
    assert "Sources" in out
    assert "Shell hooks" in out
    assert "Status" in out
