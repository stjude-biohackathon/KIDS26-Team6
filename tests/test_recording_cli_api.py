"""CLI and control-API surface."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from autocab.recording import paths
from autocab.recording.api import create_app
from autocab.recording.cli import main
from autocab.recording.desktop import DirectoryOpenError
from autocab.recording.events import Event
from autocab.recording.recorder import Recorder
from autocab.recording.session import Manifest, Session, SessionStore
from autocab.recording.state import RecorderState


@pytest.fixture()
def client(autocab_home):
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


def test_settings_save_default_analyst_for_future_sessions(client):
    existing = client.post(
        "/sessions/start", json={"title": "Existing", "analyst": "original"}
    ).json()["session"]

    response = client.patch("/settings", json={"default_analyst": "  analyst-a  "})
    created = client.post("/sessions/start", json={"title": "Future"}).json()["session"]

    assert response.status_code == 200
    assert response.json() == {
        "default_analyst": "analyst-a",
        "uses_system_default": False,
    }
    assert client.get("/settings").json() == response.json()
    assert created["analyst"] == "analyst-a"
    assert Session.load(existing["id"]).manifest.analyst == "original"


def test_settings_blank_analyst_restores_os_default(client, monkeypatch):
    monkeypatch.setattr(paths.getpass, "getuser", lambda: "system-user")
    client.patch("/settings", json={"default_analyst": "analyst-a"})

    response = client.patch("/settings", json={"default_analyst": "  "})

    assert response.json() == {
        "default_analyst": "system-user",
        "uses_system_default": True,
    }


def test_endpoints_require_the_token(autocab_home):
    """A fresh client with no default header, since TestClient merges headers."""

    recorder = Recorder(supervise=False)
    with TestClient(create_app(recorder, token="test-token")) as anon:
        assert anon.get("/styles.css").status_code == 200
        assert anon.get("/dashboard.css").status_code == 200
        assert anon.get("/provenance.css").status_code == 200
        assert anon.get("/settings.css").status_code == 200
        assert anon.get("/forge-workflow.css").status_code == 200
        assert anon.get("/app.js").status_code == 200
        assert anon.get("/dashboard.js").status_code == 200
        assert anon.get("/provenance.js").status_code == 200
        assert anon.get("/settings.js").status_code == 200
        assert anon.get("/forge-workflow.js").status_code == 200
        assert anon.get("/status").status_code == 401
        assert anon.get("/status", headers={"Authorization": "Bearer wrong"}).status_code == 401
        assert (
            anon.get("/status", headers={"Authorization": "Bearer test-token"}).status_code == 200
        )
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


def test_forge_api_requires_a_sealed_session(client):
    started = client.post(
        "/sessions/start", json={"title": "Active workflow", "analyst": "analyst"}
    ).json()

    response = client.post(f"/sessions/{started['session']['id']}/forge-runs")

    assert response.status_code == 409
    assert "unsealed session" in response.json()["detail"]


def test_forge_api_exposes_blocked_review_state(client):
    started = client.post(
        "/sessions/start", json={"title": "Variant QC", "analyst": "analyst"}
    ).json()
    session_id = started["session"]["id"]
    session = Session.load(session_id)
    session.writer.append(
        Event(
            source="shell",
            type="shell.command.completed",
            payload={"command": "bcftools view input.vcf.gz", "exit_code": 0},
        )
    )
    client.post("/sessions/stop", json={"session_id": session_id})
    sealed = client.post("/sessions/seal", json={"session_id": session_id})
    assert sealed.status_code == 200

    created = client.post(f"/sessions/{session_id}/forge-runs")
    assert created.status_code == 200
    run_id = created.json()["run"]["run_id"]
    assert created.json()["run"]["state"] == "blocked"
    assert created.json()["skill_spec"]["steps"][0]["commandShape"].startswith("bcftools")

    listed = client.get(f"/sessions/{session_id}/forge-runs").json()["runs"]
    assert [run["run_id"] for run in listed] == [run_id]
    assert client.get(f"/forge-runs/{run_id}").status_code == 200

    reviewed = client.post(
        f"/forge-runs/{run_id}/review",
        json={"reviewer": "Reviewer", "notes": "Dependency details still needed."},
    )
    assert reviewed.status_code == 200
    assert reviewed.json()["run"]["state"] == "blocked"

    approval = client.post(
        f"/forge-runs/{run_id}/approve",
        json={"reviewer": "Reviewer"},
    )
    assert approval.status_code == 409
    assert "needs_review" in approval.json()["detail"]


def test_forge_api_keeps_review_approval_and_packaging_separate(client):
    started = client.post(
        "/sessions/start", json={"title": "Reviewed workflow", "analyst": "analyst"}
    ).json()
    session_id = started["session"]["id"]
    Session.load(session_id).writer.append(
        Event(
            source="shell",
            type="shell.command.completed",
            payload={"command": "python workflow.py input.txt", "exit_code": 0},
        )
    )
    client.post("/sessions/stop", json={"session_id": session_id})
    client.post("/sessions/seal", json={"session_id": session_id})
    created = client.post(f"/sessions/{session_id}/forge-runs").json()
    run_id = created["run"]["run_id"]
    spec = created["skill_spec"]
    spec["requestedPackaging"] = "std"
    spec["packaging"] = "std"
    spec["decision"] = "novel"
    spec["name"] = spec["name"].removesuffix("-cbd") + "-std"
    spec["unresolvedQuestions"] = []
    spec["dependencies"] = []
    spec["runtimeEnvironment"]["verified"] = True
    spec["licenseDecision"] = "No copied third-party source is included."
    for step in spec["steps"]:
        step["status"] = "supported"
        step["dependencies"] = []

    reviewed = client.post(
        f"/forge-runs/{run_id}/review",
        json={"reviewer": "Reviewer One", "skill_spec": spec},
    )
    assert reviewed.status_code == 200
    assert reviewed.json()["run"]["state"] == "needs_review"

    approved = client.post(
        f"/forge-runs/{run_id}/approve",
        json={"reviewer": "Reviewer Two"},
    )
    assert approved.status_code == 200
    assert approved.json()["run"]["state"] == "approved"

    packaged = client.post(f"/forge-runs/{run_id}/package")
    assert packaged.status_code == 200
    assert packaged.json()["run"]["state"] == "packaged"
    assert packaged.json()["run"]["package_path"].endswith("reviewed-workflow-std")


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
    assert "autocab record start" in response.json()["detail"]


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
    assert page["offset"] == 0

    tail = client.get(f"/sessions/{session_id}/events?limit=3&offset=0&tail=true").json()
    assert tail["offset"] == tail["total"] - 3
    assert [event["payload"]["label"] for event in tail["events"]] == [
        "m7",
        "m8",
        "m9",
    ]


def test_events_endpoint_adds_sanitized_markdown_presentation(client):
    from autocab.recording.events import Event

    started = client.post("/sessions/start", json={"title": "A"}).json()
    session_id = started["session"]["id"]
    note_text = "**Result** [unsafe](javascript:alert(1))"
    client.post("/notes", json={"text": note_text, "label": "finding"})
    Session.load(session_id).writer.append(
        Event(
            source="agents",
            type="agent.message",
            payload={"tool": "codex", "role": "assistant", "text": "- item"},
        )
    )

    events = client.get(f"/sessions/{session_id}/events").json()["events"]
    note = next(event for event in events if event["type"] == "context.note")
    agent = next(event for event in events if event["type"] == "agent.message")

    assert note["payload"]["text"] == note_text
    assert note["payload"]["label"] == "finding"
    assert note["presentation"]["detail_format"] == "markdown"
    assert "<strong>Result</strong>" in note["presentation"]["detail_html"]
    assert 'href="javascript:' not in note["presentation"]["detail_html"]
    assert "<li>item</li>" in agent["presentation"]["detail_html"]


def test_events_endpoint_makes_redacted_note_link_non_clickable(client):
    started = client.post("/sessions/start", json={"title": "A"}).json()
    session_id = started["session"]["id"]
    client.post(
        "/notes",
        json={"text": "[Docs](https://example.org/private?subject=1)"},
    )

    events = client.get(f"/sessions/{session_id}/events").json()["events"]
    note = next(event for event in events if event["type"] == "context.note")
    presentation = note["presentation"]["detail_html"]

    assert note["payload"]["text"] == "[Docs]([REDACTED_URL])"
    assert "Docs [link redacted]" in presentation
    assert "<a " not in presentation
    assert "%5BREDACTED" not in presentation


def test_events_endpoint_leaves_non_prose_events_plain(client):
    started = client.post("/sessions/start", json={"title": "A"}).json()
    session_id = started["session"]["id"]
    client.post("/markers", json={"label": "**plain marker**"})

    events = client.get(f"/sessions/{session_id}/events").json()["events"]
    marker = next(event for event in events if event["type"] == "marker.user")

    assert "presentation" not in marker


def test_events_endpoint_rejects_unbounded_pages(client):
    started = client.post("/sessions/start", json={"title": "A"}).json()
    session_id = started["session"]["id"]

    response = client.get(f"/sessions/{session_id}/events?limit=2001")

    assert response.status_code == 422


def test_rename_active_session_updates_live_and_persisted_titles(client):
    started = client.post("/sessions/start", json={"title": "Original"}).json()
    session_id = started["session"]["id"]

    response = client.patch(f"/sessions/{session_id}", json={"title": "  Updated title  "})

    assert response.status_code == 200
    assert response.json() == {"id": session_id, "title": "Updated title"}
    assert client.get("/status").json()["session"]["title"] == "Updated title"
    assert Session.load(session_id).manifest.title == "Updated title"


def test_rename_archived_unsealed_session(client):
    started = client.post("/sessions/start", json={"title": "Original"}).json()
    session_id = started["session"]["id"]
    client.post("/sessions/stop", json={"session_id": session_id})

    response = client.patch(f"/sessions/{session_id}", json={"title": "Archived title"})
    sessions = client.get("/sessions").json()["sessions"]

    assert response.status_code == 200
    assert next(item for item in sessions if item["id"] == session_id)["title"] == (
        "Archived title"
    )


def test_rename_session_rejects_blank_title(client):
    started = client.post("/sessions/start", json={"title": "Original"}).json()
    session_id = started["session"]["id"]

    response = client.patch(f"/sessions/{session_id}", json={"title": "   "})

    assert response.status_code == 400
    assert response.json()["detail"] == "Session title cannot be empty."

    too_long = client.patch(f"/sessions/{session_id}", json={"title": "x" * 201})
    assert too_long.status_code == 422


def test_rename_session_rejects_sealed_session(client):
    started = client.post("/sessions/start", json={"title": "Original"}).json()
    session_id = started["session"]["id"]
    client.post("/sessions/stop", json={"session_id": session_id})
    client.post("/sessions/seal", json={"session_id": session_id})

    response = client.patch(f"/sessions/{session_id}", json={"title": "Changed after sealing"})

    assert response.status_code == 409
    assert "sealed and cannot be renamed" in response.json()["detail"]
    assert Session.load(session_id).manifest.title == "Original"


def test_update_session_workflow_and_tags(client):
    started = client.post("/sessions/start", json={"title": "Metadata"}).json()
    session_id = started["session"]["id"]

    response = client.patch(
        f"/sessions/{session_id}/metadata",
        json={
            "workflow_family": "  variant-qc  ",
            "tags": ["hg38", " review ", "hg38", ""],
        },
    )

    assert response.status_code == 200
    assert response.json() == {
        "id": session_id,
        "workflow_family": "variant-qc",
        "tags": ["hg38", "review"],
    }
    selected = client.get(f"/status?session_id={session_id}").json()["session"]
    assert selected["workflow_family"] == "variant-qc"
    assert selected["tags"] == ["hg38", "review"]
    manifest = Session.load(session_id).manifest
    assert manifest.workflow_family == "variant-qc"
    assert manifest.tags == ["hg38", "review"]

    client.post("/sessions/stop", json={"session_id": session_id})
    cleared = client.patch(
        f"/sessions/{session_id}/metadata",
        json={"workflow_family": "", "tags": []},
    )
    assert cleared.status_code == 200
    assert cleared.json()["workflow_family"] == ""
    assert cleared.json()["tags"] == []


def test_update_session_metadata_rejects_sealed_session(client):
    started = client.post("/sessions/start", json={"title": "Sealed"}).json()
    session_id = started["session"]["id"]
    client.post("/sessions/stop", json={"session_id": session_id})
    client.post("/sessions/seal", json={"session_id": session_id})

    response = client.patch(
        f"/sessions/{session_id}/metadata",
        json={"tags": ["changed"]},
    )

    assert response.status_code == 409
    assert "sealed and cannot be edited" in response.json()["detail"]
    assert Session.load(session_id).manifest.tags == []


def test_trash_archived_session_hides_only_the_dashboard_entry(client):
    started = client.post("/sessions/start", json={"title": "Archived"}).json()
    session_id = started["session"]["id"]
    client.post("/sessions/stop", json={"session_id": session_id})
    session_root = Session.load(session_id).root
    events_before = (session_root / "events.jsonl").read_bytes()

    response = client.post(f"/sessions/{session_id}/trash")
    visible_ids = {session["id"] for session in client.get("/sessions").json()["sessions"]}

    assert response.status_code == 200
    assert response.json() == {"id": session_id, "trashed": True}
    assert session_id not in visible_ids
    assert session_id in RecorderState.load().trashed_sessions
    assert session_id in SessionStore().list_ids()
    assert session_root.is_dir()
    assert (session_root / "events.jsonl").read_bytes() == events_before
    assert client.get(f"/status?session_id={session_id}").status_code == 200


def test_open_folder_resolves_the_session_path_server_side(client, monkeypatch):
    started = client.post("/sessions/start", json={"title": "Folder test"}).json()
    session_id = started["session"]["id"]
    opened: list[Path] = []

    def is_local(_request: object) -> bool:
        return True

    monkeypatch.setattr("autocab.recording.api._request_is_local", is_local)
    monkeypatch.setattr("autocab.recording.api.open_directory", opened.append)

    response = client.post(f"/sessions/{session_id}/open-folder")

    assert response.status_code == 200
    assert response.json() == {"id": session_id, "opened": True}
    assert opened == [Session.load(session_id).root]


def test_open_folder_reports_an_unavailable_desktop(client, monkeypatch):
    started = client.post("/sessions/start", json={"title": "Headless"}).json()
    session_id = started["session"]["id"]

    def unavailable(_path: Path) -> None:
        raise DirectoryOpenError("No graphical desktop is available.")

    def is_local(_request: object) -> bool:
        return True

    monkeypatch.setattr("autocab.recording.api._request_is_local", is_local)
    monkeypatch.setattr("autocab.recording.api.open_directory", unavailable)

    response = client.post(f"/sessions/{session_id}/open-folder")

    assert response.status_code == 503
    assert response.json()["detail"] == "No graphical desktop is available."


def test_open_folder_rejects_a_remote_request(client):
    started = client.post("/sessions/start", json={"title": "Remote"}).json()
    session_id = started["session"]["id"]

    response = client.post(f"/sessions/{session_id}/open-folder")

    assert response.status_code == 409
    assert response.json()["detail"] == ("Session folders can only be opened from the daemon host.")


@pytest.mark.parametrize("transition", [None, "pause"])
def test_trash_requires_selected_session_to_be_archived(client, transition):
    started = client.post("/sessions/start", json={"title": "In progress"}).json()
    session_id = started["session"]["id"]
    if transition == "pause":
        client.post("/sessions/pause", json={"session_id": session_id})

    response = client.post(f"/sessions/{session_id}/trash")

    assert response.status_code == 409
    assert response.json()["detail"] == (
        "Only archived sessions can be removed from the dashboard."
    )
    assert session_id not in RecorderState.load().trashed_sessions


def test_trash_sealed_archived_session_does_not_change_the_seal(client):
    started = client.post("/sessions/start", json={"title": "Sealed"}).json()
    session_id = started["session"]["id"]
    client.post("/sessions/stop", json={"session_id": session_id})
    client.post("/sessions/seal", json={"session_id": session_id})
    seal_path = Session.load(session_id).root / "seal.json"
    seal_before = seal_path.read_bytes()

    response = client.post(f"/sessions/{session_id}/trash")

    assert response.status_code == 200
    assert seal_path.read_bytes() == seal_before


def test_sessions_support_read_only_historical_selection(client):
    first = client.post(
        "/sessions/start",
        json={
            "title": "Variant review",
            "analyst": "analyst-a",
            "workflow_family": "variant-qc",
        },
    ).json()
    first_id = first["session"]["id"]
    client.post("/markers", json={"label": "reviewed"})
    client.post("/sessions/pause", json={"session_id": first_id})
    second = client.post(
        "/sessions/start",
        json={"title": "RNA-seq", "analyst": "analyst-b"},
    ).json()
    second_id = second["session"]["id"]

    summaries = {session["id"]: session for session in client.get("/sessions").json()["sessions"]}
    selected = client.get(f"/status?session_id={first_id}").json()

    assert summaries[first_id]["status"] == "paused"
    assert summaries[first_id]["workflow_family"] == "variant-qc"
    assert summaries[first_id]["events"] >= 4
    assert summaries[first_id]["is_default"] is False
    assert summaries[first_id]["sealed"] is False
    assert summaries[second_id]["status"] == "active"
    assert summaries[second_id]["is_default"] is True
    assert selected["active_session"] == second_id
    assert selected["session"]["id"] == first_id
    assert selected["session"]["status"] == "paused"
    assert selected["session"]["root"].endswith(first_id)
    assert selected["collectors"] == {}


def test_session_provenance_reports_capture_and_pending_redaction(client):
    started = client.post(
        "/sessions/start",
        json={
            "title": "Variant review",
            "analyst": "analyst-a",
            "workflow_family": "variant-qc",
            "tags": ["hg38"],
        },
    ).json()
    session_id = started["session"]["id"]

    response = client.get(f"/sessions/{session_id}/provenance")
    provenance = response.json()

    assert response.status_code == 200
    assert provenance["session"]["id"] == session_id
    assert provenance["session"]["workflow_family"] == "variant-qc"
    assert provenance["session"]["tags"] == ["hg38"]
    assert provenance["capture"]["sources"]["shell"] is True
    assert provenance["environment"]["host"]
    assert provenance["environment"]["recorded_by_version"] == "0.1.0"
    assert provenance["environment"]["recorded_versions"] == ["0.1.0"]
    assert provenance["environment"]["dashboard_version"] == "0.1.0"
    assert provenance["redaction"] == {
        "status": "pending",
        "engine": "regex",
        "engine_source": "dashboard-default",
        "integrity": {"status": "pending"},
    }


def test_session_provenance_verifies_seal_and_hides_model_path(client):
    started = client.post("/sessions/start", json={"title": "Sealed"}).json()
    session_id = started["session"]["id"]
    client.post("/sessions/stop", json={"session_id": session_id})
    client.post("/sessions/seal", json={"session_id": session_id})
    seal_path = Session.load(session_id).root / "seal.json"
    record = json.loads(seal_path.read_text(encoding="utf-8"))
    record["engines"][0].update(
        {
            "model": "example-model",
            "revision": "a" * 40,
            "weights_sha256": "b" * 64,
            "weights_path": "/private/model/cache",
        }
    )
    record["engine"] = "gliner"
    seal_path.write_text(json.dumps(record), encoding="utf-8")

    provenance = client.get(f"/sessions/{session_id}/provenance").json()
    redaction = provenance["redaction"]

    assert redaction["status"] == "applied"
    assert redaction["engine"] == "gliner"
    assert redaction["integrity"]["status"] == "verified"
    assert redaction["integrity"]["targets"] == len(redaction["target_files"])
    assert redaction["engines"][0]["revision"] == "a" * 40
    assert redaction["engines"][0]["weights_sha256"] == "b" * 64
    assert "weights_path" not in json.dumps(provenance)


def test_session_provenance_reports_integrity_failure(client):
    started = client.post("/sessions/start", json={"title": "Changed"}).json()
    session_id = started["session"]["id"]
    client.post("/sessions/stop", json={"session_id": session_id})
    client.post("/sessions/seal", json={"session_id": session_id})
    manifest_path = Session.load(session_id).root / "manifest.json"
    manifest_path.write_text(manifest_path.read_text(encoding="utf-8") + "\n")

    redaction = client.get(f"/sessions/{session_id}/provenance").json()["redaction"]

    assert redaction["integrity"]["status"] == "failed"
    assert "no longer matches its sealed digest" in redaction["integrity"]["detail"]


def test_doctor_endpoint_reports_sources(client):
    report = client.get("/doctor").json()
    assert "sources" in report and "shell" in report["sources"]
    assert "wfrec_version" in report


def test_ui_injects_token_and_loads_packaged_assets(client):
    body = client.get("/").text

    assert "@WFREC_TOKEN@" not in body
    assert "test-token" in body
    assert '<meta name="wfrec-token" content="test-token">' in body
    assert '<link rel="stylesheet" href="/styles.css">' in body
    assert '<link rel="stylesheet" href="/dashboard.css">' in body
    assert '<link rel="stylesheet" href="/provenance.css">' in body
    assert '<link rel="stylesheet" href="/session-metadata.css">' in body
    assert '<link rel="stylesheet" href="/settings.css">' in body
    assert '<script src="/dashboard.js" defer></script>' in body
    assert '<script src="/provenance.js" defer></script>' in body
    assert '<script src="/session-metadata.js" defer></script>' in body
    assert '<script src="/settings.js" defer></script>' in body
    assert '<script src="/app.js" defer></script>' in body
    assert "<style>" not in body
    assert "<script>" not in body
    assert "Recent Events Log" in body
    assert "<title>AutoCAB Activity Dashboard</title>" in body
    assert '<h1>AutoCAB <span class="app-version ui-pill">v0.1.0</span></h1>' in body
    assert '<span id="badge" class="badge ui-pill">' in body
    assert "@AUTOCAB_VERSION@" not in body
    assert "Commands appear after they finish." in body
    assert 'id="session-title"' in body
    assert 'id="stats" aria-label="Session details"' in body
    assert 'id="rename-session"' in body
    assert 'aria-label="Rename session"' in body
    assert 'viewBox="0 0 24 24"' in body
    assert 'id="session-title-form"' in body
    assert 'maxlength="200"' in body
    assert 'id="pause-dialog"' in body
    assert '<label for="pause-reason">Pause reason (optional)</label>' in body
    assert "Optionally record why the session is being paused." not in body
    assert 'id="export-dialog"' in body
    assert "Redact PHI and export?" in body
    assert "Add notes, errors, or decisions." in body
    assert "AutoCAB masks detected personal information before saving." in body
    assert "AutoCAB will remove detected PHI from a copy." in body
    assert "Your session will not change." in body
    assert "Redaction can miss PHI." in body
    assert 'class="dialog-warning" role="note"' in body
    assert ">Select Folder</button>" in body
    assert '<label for="title">Session title</label>' in body
    assert '<label for="analyst">Analyst name or ID</label>' in body
    assert "<summary>More details</summary>" not in body
    assert '<label for="workflow-family">Workflow name (optional)</label>' in body
    assert '<label for="session-tags">Tags (optional)</label>' in body
    assert "Separate tags with commas." in body
    assert 'id="session-metadata" class="session-metadata"' in body
    assert '<label for="note">Session note</label>' in body
    assert '<textarea id="note"' in body
    assert 'class="row note-actions"' in body
    assert 'id="feedback" role="status"' in body
    assert 'id="log" aria-live=' not in body
    assert 'id="log" tabindex="0" aria-labelledby="recent-events-heading"' in body
    assert "Load older events" in body
    assert 'id="session-list" class="session-list"' in body
    assert 'id="session-mobile" class="session-mobile"' in body
    assert 'id="activity-dashboard-heading" class="sr-only"' in body
    assert 'id="activity-dashboard" class="activity-dashboard"' in body
    assert 'id="dash-timeline-summary"' in body
    assert 'class="activity-timeline-chart" id="dash-timeline"' in body
    assert "Top windows / apps by events" in body
    assert 'id="trash-session"' in body
    assert 'aria-label="Remove archived session from dashboard"' in body
    assert 'aria-label="Copy session path"' in body
    assert 'aria-label="Open session folder"' in body
    assert 'class="session-folder-control"' in body
    assert 'id="live-updates"' in body
    assert 'id="navbar-analyst"' not in body
    assert 'id="provenance-button"' in body
    assert 'aria-label="View session provenance"' in body
    assert 'id="provenance-dialog"' in body
    assert ">Session provenance</h2>" in body
    assert 'id="settings-button"' in body
    assert 'aria-label="Open settings"' in body
    assert 'id="settings-dialog"' in body
    assert 'for="default-analyst"' in body
    assert "Existing sessions will not change." in body
    assert 'aria-pressed="true"' in body
    assert 'id="theme-toggle"' in body
    assert 'id="theme-icon-moon"' in body
    assert 'id="theme-icon-sun"' in body
    assert "All files" in body
    assert "Events JSON" in body
    assert "Workflow trace JSON" in body
    assert "Terminal log" in body
    assert "Screen events JSON" in body
    assert "Export Session" in body
    assert 'aria-label="Export session"' in body
    assert 'id="export-help"' not in body
    assert 'id="archive-session" onclick="act(\'stop\')"' in body
    assert 'id="archive-session" class="danger-quiet"' not in body
    assert "Export Events" not in body
    assert "AutoCAB inputs" not in body
    assert 'class="skip-link" href="#main-content"' in body
    assert '<main id="main-content" tabindex="-1">' in body
    assert "style=" not in body


def test_ui_serves_packaged_stylesheet(client):
    response = client.get("/styles.css")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/css")
    assert response.headers["cache-control"] == "no-cache"
    assert ":root {" in response.text
    assert "--control-border: #687287" in response.text
    assert "--danger: #ff7180" in response.text
    assert ':root[data-theme="light"]' in response.text
    assert "button.danger-quiet:not(:disabled)" in response.text
    assert ".app-header-actions > button {" in response.text
    assert ".session-folder-control {" in response.text
    assert '"summary actions"' in response.text
    assert '"properties properties"' in response.text
    assert ".session-properties {" in response.text
    assert "background: color-mix(in srgb, var(--dim) 9%, transparent)" in response.text
    assert ".session-title-form {" in response.text
    assert ".ui-pill {" in response.text
    assert ".app-version {" in response.text
    assert ".export-block {" not in response.text
    assert ".action-help {" not in response.text
    assert ".session-stat--phi-pending {" in response.text
    assert ".session-stat--phi-applied {" in response.text
    assert ".event-detail-content.formatted.collapsed {" in response.text
    assert ".event-markdown {" in response.text
    assert "@media (max-width: 720px)" in response.text
    assert "@media (prefers-reduced-motion: reduce)" in response.text
    assert "@media (forced-colors: active)" in response.text
    assert ".dialog-warning" in response.text


def test_ui_serves_packaged_javascript_without_credentials(client):
    response = client.get("/app.js")
    source = response.text

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/javascript")
    assert response.headers["cache-control"] == "no-cache"
    assert "test-token" not in source
    assert "@WFREC_TOKEN@" not in source
    assert 'meta[name="wfrec-token"]' in source
    assert "Codex, Claude Code" in source
    assert "providers:" in source
    assert "Latest ${events.length} of ${total} events" in source
    assert "'agent.message':'Agent message'" in source
    assert "Start or resume a session to see activity." in source
    assert "[...events].reverse()" in source
    assert "event-source" not in source
    assert "event-more" in source
    assert "aria-expanded" in source
    assert "aria-controls" in source
    assert "EXPANDED_EVENTS" in source
    assert "markdown.innerHTML = presentation.detailHtml" in source
    assert "event.presentation?.detail_format === 'markdown'" in source
    assert "content.classList.toggle('collapsed', !expanded)" in source
    assert "expanded && presentation.detailHtml" not in source
    assert "markdown.innerHTML = p.text" not in source
    assert "STATE.default_analyst" in source
    assert "ANALYST_INITIALIZED" in source
    assert "active:'Recording'" in source
    assert "paused:'Paused'" in source
    assert "function showRenameSessionForm()" in source
    assert "function sessionTags(value)" in source
    assert "workflow_family: document.getElementById('workflow-family').value.trim()" in source
    assert "tags:sessionTags(document.getElementById('session-tags').value)" in source
    assert "function cancelRenameSession()" in source
    assert "function renameSession(event)" in source
    assert "function trashSelectedSession()" in source
    assert "session.status !== 'stopped'" in source
    assert "SELECTED_SESSION = ''" in source
    assert "SESSION_SIGNATURE = ''" in source
    assert "function clearFeedback()" in source
    assert "if(sessionId !== SELECTED_SESSION) clearFeedback()" in source
    assert "'PATCH'" in source
    assert "Sealed sessions cannot be renamed." in source
    assert "sessionTitle || 'Untitled session'" in source
    assert "getElementById('sid')" not in source
    assert "dialog.showModal()" in source
    assert "function closePauseDialog()" in source
    assert "prompt(" not in source
    assert "STATE_RECEIVED_AT = performance.now()" in source
    assert "if(LIVE_UPDATES) renderSessionStats()" in source
    assert "appendInlineCode(sourceDescription, why)" in source
    assert "sourceName.textContent = displayName" in source
    assert "details.push('reason: '+col.reason)" not in source
    assert "btn.setAttribute('role', 'switch')" in source
    assert "btn.setAttribute('aria-checked', String(on))" in source
    assert "Recorder disconnected. Retrying" in source
    assert "function exportSession(formats, label)" in source
    assert "function confirmExport(event)" in source
    assert "await writeSessionExport(request.formats, request.label)" in source
    assert "choose_destination:true" in source
    assert "if(r.cancelled)" in source
    assert "const destinations = paths.join('\\n')" in source
    assert "Exported ${label}: ${destinations}" in source
    assert "Exported ${paths.length} session files:\\n${destinations}" in source
    assert "to:\\n${r.destination}" not in source
    assert "EVENT_LIMIT += EVENT_PAGE_SIZE" in source
    assert "const EVENT_PAGE_SIZE = 25" in source
    assert "tail=true" in source
    assert "EVENT_REFRESH_IN_PROGRESS" in source
    assert "EVENT_REFRESH_PENDING" in source
    assert "if(LIVE_UPDATES) refreshLog(); }, 1000" in source
    assert "restoreScroll(host, anchor, previousTop)" in source
    assert "row.dataset.eventKey = eventKey(event)" in source
    assert "function renderSessionNavigation()" in source
    assert "function selectSession(sessionId)" in source
    assert "function openSessionFolder()" in source
    assert "function toggleLiveUpdates()" in source
    assert "function restoreFocus(element)" in source
    assert "element.focus({preventScroll:true})" in source
    assert source.count("restoreFocus(replacement)") == 3
    assert "restoreFocus(menu)" in source
    assert "restoreFocus(host)" in source
    assert "input.focus()" in source
    assert "reason.focus()" in source
    assert "function toggleTheme()" in source
    assert "localStorage.setItem(THEME_STORAGE_KEY, next)" in source
    assert "THEME_MEDIA.addEventListener('change'" in source
    assert "function renderSources(active)" in source
    assert "dataset.source" in source
    assert "dataset.eventControl" in source
    assert "window.WfrecActivityDashboard.create" in source
    assert "ACTIVITY_DASHBOARD.render(events" in source
    assert "function dashboardOverview(events)" not in source
    assert "function dashboardTimeline(events)" not in source
    assert "DASHBOARD_PAGE_SIZE = 2000" in source
    assert "const DASHBOARD_CACHE = new Map()" in source
    assert "function dashboardEvents(sessionId, expectedTotal)" in source
    assert "function sessionStat(value, modifier='')" in source
    assert "chip.className = `ui-pill session-stat" in source
    assert "badge.className = 'badge ui-pill '" in source
    assert "function phiPendingIcon()" in source
    assert "icon.setAttribute('aria-hidden', 'true')" in source
    assert "dateTimeLabel:eventDateTime" in source
    assert "limit=100000" not in source
    assert "session.sealed ? 'PHI redaction applied'" in source
    assert "'PHI redaction pending'" in source
    assert "Pause or archive the session before exporting events." in source
    assert "control.title = help" in source
    assert "Pause or archive to export." not in source
    assert "Preparing a pattern-checked export." in source
    assert "Analyst · ${session.analyst}" in source
    assert "paused total" in source
    assert "navbar-analyst" not in source
    assert (
        "return `${session.events} events · ${sessionDuration(session.active_seconds)}`;" in source
    )


def test_ui_serves_component_scoped_dashboard_styles(client):
    response = client.get("/dashboard.css")
    source = response.text

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/css")
    assert response.headers["cache-control"] == "no-cache"
    assert ".activity-overview-chart__segment" in source
    assert ".activity-overview-chart__tooltip" in source
    assert ".activity-timeline-chart__bucket" in source
    assert ".activity-timeline-chart__segment" in source
    assert ".activity-timeline-chart__tooltip" in source
    assert ".dash-pie" not in source
    assert ".dash-timeline .bucket" not in source


def test_ui_serves_component_scoped_provenance_assets(client):
    stylesheet = client.get("/provenance.css")
    javascript = client.get("/provenance.js")

    assert stylesheet.status_code == 200
    assert stylesheet.headers["content-type"].startswith("text/css")
    assert ".provenance-dialog" in stylesheet.text
    assert ".provenance-list" in stylesheet.text
    assert "@media (max-width: 560px)" in stylesheet.text
    assert javascript.status_code == 200
    assert javascript.headers["content-type"].startswith("application/javascript")
    assert "function provenanceModule(global)" in javascript.text
    assert "function render(data, host)" in javascript.text
    assert "function versionSummary(environment)" in javascript.text
    assert "(started)" in javascript.text
    assert "(current)" in javascript.text
    assert "weights_path" not in javascript.text


def test_ui_serves_component_scoped_session_metadata_assets(client):
    stylesheet = client.get("/session-metadata.css")
    javascript = client.get("/session-metadata.js")

    assert stylesheet.status_code == 200
    assert javascript.status_code == 200
    assert ".session-metadata__field" in stylesheet.text
    assert "display: flex" in stylesheet.text
    assert ".session-metadata__editor" in stylesheet.text
    assert "flex: 0 0 100%" in stylesheet.text
    assert "function sessionMetadataModule(global)" in javascript.text
    assert "function metadataEditor()" in javascript.text
    assert "'ui-pill session-metadata__value'" in javascript.text
    assert "'ui-pill session-metadata__chip'" in javascript.text
    assert "Remove tag" in javascript.text
    assert "'Not set'" in javascript.text


def test_ui_serves_component_scoped_settings_assets(client):
    stylesheet = client.get("/settings.css")
    javascript = client.get("/settings.js")

    assert stylesheet.status_code == 200
    assert javascript.status_code == 200
    assert ".settings-dialog" in stylesheet.text
    assert ".settings-dialog__message" in stylesheet.text
    assert "function settingsModule(global)" in javascript.text
    assert "function create({button, dialog, form, input, message, load, save, onSaved})" in (
        javascript.text
    )
    assert "input.focus({preventScroll:true})" in javascript.text
    assert "button.focus({preventScroll:true})" in javascript.text


def test_ui_serves_interactive_dashboard_javascript_without_credentials(client):
    response = client.get("/dashboard.js")
    source = response.text

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/javascript")
    assert response.headers["cache-control"] == "no-cache"
    assert "test-token" not in source
    assert "@WFREC_TOKEN@" not in source
    assert "window.WfrecActivityDashboard" in source
    assert "Object.freeze({create, timelineModel})" in source
    assert "TIMELINE_BUCKET_COUNT = 24" in source
    assert "activity-overview-chart__segment" in source
    assert "activity-timeline-chart__segment" in source
    assert "pointerenter" in source
    assert "ArrowLeft" in source
    assert "ArrowRight" in source
    assert "focus({preventScroll:true})" in source
    assert "segment.style.flexGrow = count" in source
    assert "['Shell commands'" not in source
    assert "['Agent messages'" not in source
    assert "Mostly ${CATEGORIES[topIndex].label.toLowerCase()} activity" in source
    assert "in this session, mostly" not in source


def test_export_endpoint_writes_files(client):
    client.post("/sessions/start", json={"title": "A", "analyst": "a"})
    status = client.get("/status").json()
    session = Session.load(status["session"]["id"])
    from autocab.recording.events import Event

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
    assert {Path(path).name for path in result["written"]} == {
        "terminal.log",
        "screen-events.json",
    }
    assert result["seal"]["generation"] == 1


def test_export_endpoint_defaults_to_all_files(client):
    client.post("/sessions/start", json={"title": "A", "analyst": "a"})
    status = client.get("/status").json()
    session_id = status["session"]["id"]
    client.post("/sessions/stop", json={})

    result = client.post("/export", json={}).json()

    assert {Path(path).name for path in result["written"]} == {
        "events.json",
        "workflow-trace.json",
        "terminal.log",
        "screen-events.json",
    }
    assert result["privacy"]["method"] == "protected-snapshot"
    assert Session.load(session_id).writer.sealed is False


def test_export_endpoint_allows_a_paused_session_to_resume(client):
    started = client.post(
        "/sessions/start",
        json={"title": "Paused export", "analyst": "a"},
    ).json()
    session_id = started["session"]["id"]
    client.post("/sessions/pause", json={"session_id": session_id})

    result = client.post("/export", json={"session_id": session_id}).json()
    resumed = client.post(
        "/sessions/resume",
        json={"session_id": session_id},
    )

    assert result["privacy"]["method"] == "protected-snapshot"
    assert result["privacy"]["source_status"] == "paused"
    assert resumed.status_code == 200
    assert resumed.json()["session"]["status"] == "active"


def test_paused_export_writes_to_the_chosen_folder(
    client,
    monkeypatch,
    tmp_path: Path,
):
    started = client.post(
        "/sessions/start",
        json={"title": "Chosen export", "analyst": "a"},
    ).json()
    session_id = started["session"]["id"]
    client.post("/sessions/pause", json={"session_id": session_id})
    destination = tmp_path / "chosen-export"
    destination.mkdir()
    monkeypatch.setattr("autocab.recording.api._request_is_local", lambda _request: True)
    monkeypatch.setattr("autocab.recording.api.choose_directory", lambda: destination)

    result = client.post(
        "/export",
        json={"session_id": session_id, "choose_destination": True},
    ).json()

    assert result["destination"] == str(destination.resolve())
    assert {path.name for path in destination.iterdir()} == {
        f"{session_id}-events.json",
        f"{session_id}-workflow-trace.json",
        f"{session_id}-terminal.log",
        f"{session_id}-screen-events.json",
    }
    assert all(Path(path).is_absolute() for path in result["written"])


def test_export_endpoint_rejects_an_active_session(client):
    started = client.post(
        "/sessions/start",
        json={"title": "Still recording", "analyst": "a"},
    ).json()

    response = client.post(
        "/export",
        json={"session_id": started["session"]["id"]},
    )

    assert response.status_code == 409
    assert response.json()["detail"] == (
        "Pause or archive the selected session before exporting events."
    )


def test_seal_endpoint_refuses_a_live_session(client):
    """The GUI route will not silently stop a recording to seal it.

    `--force` means "stop first, then seal", and deciding to stop somebody's
    live recording is not a decision an API call should make implicitly.
    """

    client.post("/sessions/start", json={"title": "A", "analyst": "a"})
    response = client.post("/sessions/seal", json={})

    assert response.status_code == 409
    assert "not idle" in response.json()["detail"]


def test_seal_endpoint_force_stops_via_the_live_recorder(client):
    client.post("/sessions/start", json={"title": "A", "analyst": "a"})
    response = client.post("/sessions/seal", json={"force": True})

    assert response.status_code == 200
    assert response.json()["assurance"] == "regex-only"


def test_seal_status_works_for_the_current_session_without_an_explicit_id(client):
    client.post("/sessions/start", json={"title": "A", "analyst": "a"})
    client.post("/sessions/stop", json={})
    response = client.post("/sessions/seal", json={"status": True})

    assert response.status_code == 200
    assert response.json()["sealed"] is False


def test_seal_endpoint_rejects_unknown_profiles(client):
    client.post("/sessions/start", json={"title": "A", "analyst": "a"})
    client.post("/sessions/stop", json={})
    response = client.post("/sessions/seal", json={"profile": "balanced"})

    assert response.status_code == 422


# ----------------------------------------------------------------------- cli
def test_cli_start_status_stop(autocab_home, capsys):
    assert main(["start", "--title", "CLI session", "--analyst", "mgatta42", "--no-daemon"]) == 0
    assert "Recording session" in capsys.readouterr().out

    assert main(["status"]) == 0
    out = capsys.readouterr().out
    assert "CLI session" in out and "mgatta42" in out

    assert main(["stop"]) == 0
    assert "Stopped" in capsys.readouterr().out


def test_cli_start_uses_saved_default_analyst(autocab_home, capsys):
    state = RecorderState.load()
    state.default_analyst = "saved-analyst"
    state.save()

    assert main(["start", "--title", "Saved default", "--no-daemon"]) == 0
    capsys.readouterr()

    assert SessionStore().resolve(None).manifest.analyst == "saved-analyst"


def test_cli_json_flag_works_on_either_side_of_the_subcommand(autocab_home, capsys):
    main(["start", "--title", "A", "--no-daemon"])
    capsys.readouterr()

    assert main(["--json", "status"]) == 0
    json.loads(capsys.readouterr().out)

    assert main(["status", "--json"]) == 0
    json.loads(capsys.readouterr().out)


def test_cli_source_accepts_on_off_words(autocab_home, capsys):
    main(["start", "--title", "A", "--no-daemon"])
    capsys.readouterr()
    assert main(["source", "shell", "off"]) == 0
    output = capsys.readouterr().out
    assert "Updated capture sources" in output
    assert "shell" not in output
    assert main(["source", "shell", "on"]) == 0


def test_cli_shell_output_reports_unsupported_backend(autocab_home, capsys):
    main(["start", "--title", "A", "--no-daemon"])
    capsys.readouterr()

    assert main(["shell-output", "on"]) == 1
    assert "unavailable with hook-spool" in capsys.readouterr().err

    assert main(["shell-output", "off"]) == 0
    output = capsys.readouterr().out
    assert "Shell output capture" in output
    assert "Status" in output and "Off" in output


def test_cli_rejects_bad_toggle_word(autocab_home):
    with pytest.raises(SystemExit):
        main(["source", "shell", "maybe"])


def test_cli_note_without_session_exits_nonzero(autocab_home, capsys):
    assert main(["note", "hello"]) == 2
    assert "autocab record start" in capsys.readouterr().err


def test_cli_events_filters_by_source(autocab_home, capsys):
    main(["start", "--title", "A", "--no-daemon"])
    main(["mark", "checkpoint"])
    capsys.readouterr()
    assert main(["events", "--source", "context"]) == 0
    out = capsys.readouterr().out
    assert "marker.user" in out
    assert "session.created" not in out


def test_cli_events_filters_and_labels_agent_messages(autocab_home, capsys):
    from autocab.recording.events import Event

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


def test_cli_lists_sessions_and_events_in_tables(autocab_home, capsys):
    main(["start", "--title", "Table test", "--analyst", "tester", "--no-daemon"])
    capsys.readouterr()

    assert main(["sessions"]) == 0
    sessions_output = capsys.readouterr().out
    assert "Sessions" in sessions_output
    assert all(heading in sessions_output for heading in ("Session", "Status", "Analyst", "Title"))
    assert "Table test" in sessions_output

    assert main(["events"]) == 0
    events_output = capsys.readouterr().out
    assert "Timeline events" in events_output
    assert all(heading in events_output for heading in ("Seq", "Time", "Type", "Summary"))


def test_cli_renders_user_text_literally(autocab_home, capsys):
    title = "[bold]literal title[/bold]"

    assert main(["start", "--title", title, "--no-daemon"]) == 0

    assert title in capsys.readouterr().out


def test_cli_hooks_install_is_idempotent(autocab_home, tmp_path, monkeypatch, capsys):
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
    autocab_home, tmp_path, monkeypatch, capsys
):
    from autocab.recording import hookinstall

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
        entry for entry in hookinstall.status() if entry["shell"] == "powershell"
    )
    assert powershell_status["installed"] is True
    assert powershell_status["missing_rc_files"] == []

    assert main(["hooks", "uninstall", "--shell", "powershell"]) == 0
    for profile_path in profile_paths:
        assert "wfrec" not in profile_path.read_text(encoding="utf-8")


def test_powershell_hook_status_reports_a_missing_profile(
    autocab_home, tmp_path, monkeypatch, capsys
):
    from autocab.recording import hookinstall

    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setattr("pathlib.Path.home", lambda: fake_home)

    assert main(["hooks", "install", "--shell", "powershell"]) == 0
    capsys.readouterr()
    windows_powershell_profile = fake_home / "Documents/WindowsPowerShell/profile.ps1"
    windows_powershell_profile.unlink()

    powershell_status = next(
        entry for entry in hookinstall.status() if entry["shell"] == "powershell"
    )
    assert powershell_status["installed"] is False
    assert powershell_status["missing_rc_files"] == [str(windows_powershell_profile)]


def test_cli_doctor_renders(autocab_home, capsys):
    assert main(["doctor"]) == 0
    out = capsys.readouterr().out
    assert "wfrec 0.1.0" in out
    assert "System" in out
    assert "Sources" in out
    assert "Shell hooks" in out
    assert "Status" in out
