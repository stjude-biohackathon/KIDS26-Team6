"""CLI and control-API surface."""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from wfrec import paths
from wfrec.api import create_app
from wfrec.cli import main
from wfrec.recorder import NoActiveSession, Recorder
from wfrec.session import Manifest, Session, SessionStore


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


def test_status_reports_os_username_as_editable_default(client, monkeypatch):
    monkeypatch.setattr(paths.getpass, "getuser", lambda: "system-user")

    status = client.get("/status").json()

    assert status["default_analyst"] == "system-user"


def test_status_reports_current_session_durations(client, monkeypatch):
    def current_durations(_manifest: Manifest) -> tuple[float, float]:
        return 5.4, 2.1

    client.post("/sessions/start", json={"title": "A", "analyst": "a"})
    monkeypatch.setattr(Manifest, "duration_snapshot", current_durations)

    status = client.get("/status").json()

    assert status["session"]["active_seconds"] == 5.4
    assert status["session"]["paused_seconds"] == 2.1


def test_start_without_analyst_uses_os_username(client, monkeypatch):
    monkeypatch.setattr(paths.getpass, "getuser", lambda: "system-user")

    started = client.post("/sessions/start", json={"title": "A"}).json()

    assert started["session"]["analyst"] == "system-user"


def test_default_analyst_falls_back_when_username_is_unavailable(monkeypatch):
    def unavailable_username() -> str:
        raise OSError("user lookup failed")

    monkeypatch.setattr(paths.getpass, "getuser", unavailable_username)

    assert paths.default_analyst() == "unknown-analyst"


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
    assert "Recent Events Log" in body
    assert "Latest ${events.length} of ${total} events" in body
    assert "'agent.message':'Agent message'" in body
    assert "Start or resume a session to see activity." in body
    assert "[...events].reverse()" in body
    assert "event-source" not in body
    assert "event-more" in body
    assert "aria-expanded" in body
    assert "EXPANDED_EVENTS" in body
    assert "STATE.default_analyst" in body
    assert "ANALYST_INITIALIZED" in body
    assert "button.danger:not(:disabled)" in body
    assert "startButton.disabled = Boolean(s)" in body
    assert "'Session Active'" in body
    assert "'Session Paused'" in body
    assert 'id="session-title"' in body
    assert "sessionTitle || 'Untitled session'" in body
    assert "getElementById('sid')" not in body
    assert 'id="pause-dialog"' in body
    assert '<label for="pause-reason">Pause reason (optional)</label>' in body
    assert "Optionally record why the session is being paused." not in body
    assert "dialog.showModal()" in body
    assert "function closePauseDialog()" in body
    assert "prompt(" not in body
    assert "STATE_RECEIVED_AT = performance.now()" in body
    assert "setInterval(renderSessionStats, 1000)" in body
    assert "appendInlineCode(sourceDescription, why)" in body
    assert "sourceName.textContent = displayName" in body
    assert "details.push('reason: '+col.reason)" not in body
    assert '<label for="title">Session title</label>' in body
    assert '<label for="analyst">Analyst name or ID</label>' in body
    assert '<textarea id="note" aria-label="Note"' in body
    assert '<label for="note">Note</label>' not in body
    assert 'class="row note-actions"' in body
    assert "btn.setAttribute('role', 'switch')" in body
    assert "btn.setAttribute('aria-checked', String(on))" in body
    assert 'id="feedback" role="status"' in body
    assert 'id="log" aria-live=' not in body
    assert "Recorder disconnected. Retrying" in body
    assert "paths.join('\\n')" in body
    assert "Load older events" in body
    assert "EVENT_LIMIT += EVENT_PAGE_SIZE" in body
    assert "restoreScroll(host, anchor, previousTop)" in body
    assert "row.dataset.eventKey = eventKey(event)" in body


def test_export_endpoint_writes_files(client):
    client.post("/sessions/start", json={"title": "A", "analyst": "a"})
    status = client.get("/status").json()
    session = Session.load(status["session"]["id"])
    from wfrec.events import Event

    session.writer.append(
        Event(source="shell", type="shell.command.completed", payload={"command": "ls"})
    )
    # /export refuses an unsealed session, and /sessions/seal is the route the
    # GUI uses to satisfy it. Stop first: the route will not silently stop a
    # live recording, so an active session needs an explicit `force`.
    session_id = status["session"]["id"]
    client.post("/sessions/stop", json={})
    sealed = client.post("/sessions/seal", json={"session_id": session_id}).json()
    assert sealed["assurance"] == "regex-only"

    result = client.post("/export", json={"formats": ["autocab"]}).json()
    assert len(result["written"]) == 2
    assert result["seal"]["generation"] == 1


def test_seal_endpoint_refuses_a_live_session(client):
    """The GUI route will not silently stop a recording to seal it.

    `--force` means "stop first, then seal", and deciding to stop somebody's
    live recording is not a decision an API call should make implicitly.
    """

    client.post("/sessions/start", json={"title": "A", "analyst": "a"})
    response = client.post("/sessions/seal", json={})

    assert response.status_code == 409
    assert "not idle" in response.json()["detail"]


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


def test_cli_events_filters_and_labels_agent_messages(wfrec_home, capsys):
    from wfrec.events import Event

    main(["start", "--title", "Agent filters", "--no-daemon"])
    session = SessionStore().resolve(None)
    client_session_id = session.session_id
    session.writer.append(
        Event(
            source="agents",
            type="agent.message",
            payload={"tool": "codex", "role": "user", "text": "user prompt"},
        )
    )
    session.writer.append(
        Event(
            source="agents",
            type="agent.message",
            payload={
                "tool": "claude-code",
                "role": "assistant",
                "text": "assistant response",
            },
        )
    )
    capsys.readouterr()

    assert main(["events", "--role", "USER", "--tool", "CODEX"]) == 0
    output = capsys.readouterr().out
    assert "agent.message [codex/user]" in output
    assert "user prompt" in output
    assert "assistant response" not in output

    assert main(["--json", "events", "--role", "user"]) == 0
    events = json.loads(capsys.readouterr().out)
    assert events[0]["type"] == "agent.message"
    assert events[0]["payload"]["role"] == "user"
    assert client_session_id == events[0]["session"]


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


def test_cli_powershell_hook_installs_both_profile_generations(
    wfrec_home, tmp_path, monkeypatch, capsys
):
    from wfrec import hookinstall

    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setattr("pathlib.Path.home", lambda: fake_home)
    profile_paths = (
        fake_home / "Documents/PowerShell/profile.ps1",
        fake_home / "Documents/WindowsPowerShell/profile.ps1",
    )

    for _ in range(2):
        assert main(["hooks", "install", "--shell", "powershell"]) == 0
    capsys.readouterr()

    for profile_path in profile_paths:
        profile = profile_path.read_text(encoding="utf-8")
        assert profile.count(">>> wfrec hook >>>") == 1
        assert "wfrec.ps1" in profile

    powershell_status = next(
        entry
        for entry in hookinstall.status()
        if entry["shell"] == "powershell"
    )
    assert powershell_status["installed"] is True
    assert powershell_status["missing_rc_files"] == []

    assert main(["hooks", "uninstall", "--shell", "powershell"]) == 0
    for profile_path in profile_paths:
        assert "wfrec" not in profile_path.read_text(encoding="utf-8")


def test_powershell_hook_status_reports_a_missing_profile(
    wfrec_home, tmp_path, monkeypatch, capsys
):
    from wfrec import hookinstall

    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setattr("pathlib.Path.home", lambda: fake_home)

    assert main(["hooks", "install", "--shell", "powershell"]) == 0
    capsys.readouterr()
    windows_powershell_profile = (
        fake_home / "Documents/WindowsPowerShell/profile.ps1"
    )
    windows_powershell_profile.unlink()

    powershell_status = next(
        entry
        for entry in hookinstall.status()
        if entry["shell"] == "powershell"
    )
    assert powershell_status["installed"] is False
    assert powershell_status["missing_rc_files"] == [
        str(windows_powershell_profile)
    ]


def test_cli_doctor_renders(wfrec_home, capsys):
    assert main(["doctor"]) == 0
    out = capsys.readouterr().out
    assert "wfrec 0.1.0" in out
    assert "System" in out
    assert "Sources" in out
    assert "Shell hooks" in out
    assert "Status" in out
