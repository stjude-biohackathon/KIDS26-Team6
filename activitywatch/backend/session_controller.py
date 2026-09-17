"""Session controller for AutoCAB's ActivityWatch tooling.

The forked dashboard (aw-webui) can't itself spawn a screen-capture process —
browsers are sandboxed from doing that. This small local API is what the
dashboard's Start/Stop buttons actually call: it launches/kills capture
processes and keeps track of sessions, grouped by task name so the same task
can be resumed across multiple start/stop sessions.

Capture is pluggable: a session picks one or more *modes* (see CAPTURE_MODES
below) and one process gets spawned per mode. Adding a new capture type
(video, browser activity, whatever's next) means adding one entry to that
registry — nothing else about session/task tracking needs to change.

Run: python3 session_controller.py   (serves on http://localhost:5677)
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

import requests
from flask import Flask, jsonify, request
from flask_cors import CORS

BACKEND_DIR = Path(__file__).resolve().parent
SCRIPTS_DIR = BACKEND_DIR.parent / "scripts"
SESSIONS_FILE = BACKEND_DIR / "sessions.json"
DRAFTS_DIR = BACKEND_DIR / "drafted_skills"
REDACTIONS_FILE = BACKEND_DIR / "redactions.json"
AW_SERVER = "http://127.0.0.1:5600"

# The real redaction module already built for the main pipeline — reused
# here for auto-suggesting redactions in the post-stop review step, rather
# than writing a second pattern-matching engine.
sys.path.insert(0, str(BACKEND_DIR.parent.parent / "src"))
from redaction import redact_text  # noqa: E402

# Which AW bucket each capture mode posts events into. Used to pull a task's
# data back out for merging — this is the other half of "add a capture mode
# in one place": if a new mode posts to its own bucket, add its prefix here
# too and merge/draft-skill pick it up automatically.
BUCKET_PREFIXES: dict[str, str] = {
    "ocr": "aw-watcher-screenocr",
    "audio": "aw-watcher-audiotranscript",
    "video": "aw-watcher-video",
}

# --- Capture modes registry -------------------------------------------------
# To add a new capture mode: add an entry here pointing at a script that
# accepts --label (and whatever else it needs). That's the whole integration
# point — start/stop/task-grouping/resume all work automatically for it.
CAPTURE_MODES: dict[str, dict] = {
    "ocr": {
        "script": SCRIPTS_DIR / "screenshot_ocr_watcher.py",
        "extra_args": [],
        "description": "Periodic screenshot + OCR",
    },
    "audio": {
        "script": SCRIPTS_DIR / "audio_transcript_watcher.py",
        "extra_args": [],
        "description": "Mic audio, transcribed locally in near-real-time",
    },
    "video": {
        "script": SCRIPTS_DIR / "video_watcher.py",
        "extra_args": [],
        "description": "Real screen video, recorded in rolling chunks",
    },
}
DEFAULT_MODES = ["ocr"]

app = Flask(__name__)
CORS(app, origins=["http://localhost:27180"])

# Popen objects for processes started by *this* backend run, keyed by
# f"{session_id}:{mode}", so we can properly wait()/reap them on stop
# instead of leaving zombies behind.
_processes: dict[str, subprocess.Popen] = {}


def load_sessions() -> list[dict]:
    if not SESSIONS_FILE.exists():
        return []
    return json.loads(SESSIONS_FILE.read_text(encoding="utf-8"))


def load_redactions() -> dict[str, dict]:
    """Confirmed redactions, keyed by "<bucket_id>:<event_id>". Stored
    separately from AW's own database rather than rewriting AW events in
    place — safer (nothing destructive happens to the raw capture) and
    means merge/draft-skill can apply redactions without needing an
    event-edit API from AW itself."""
    if not REDACTIONS_FILE.exists():
        return {}
    return json.loads(REDACTIONS_FILE.read_text(encoding="utf-8"))


def save_redactions(redactions: dict[str, dict]) -> None:
    REDACTIONS_FILE.write_text(json.dumps(redactions, indent=2), encoding="utf-8")


def save_sessions(sessions: list[dict]) -> None:
    SESSIONS_FILE.write_text(json.dumps(sessions, indent=2), encoding="utf-8")


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def is_pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except (OSError, ProcessLookupError):
        return False


def any_process_alive(session: dict) -> bool:
    return any(is_pid_alive(p["pid"]) for p in session["processes"])


@app.route("/api/status", methods=["GET"])
def status():
    # Never returns the key itself, just whether one is present — so the UI
    # can show "ready" vs "not configured" without a round-trip that burns
    # an actual API call just to check.
    return jsonify({"anthropic_configured": bool(os.environ.get("ANTHROPIC_API_KEY"))})


@app.route("/api/capture_modes", methods=["GET"])
def list_capture_modes():
    return jsonify(
        [{"mode": name, "description": cfg["description"]} for name, cfg in CAPTURE_MODES.items()]
    )


@app.route("/api/sessions", methods=["GET"])
def list_sessions():
    sessions = load_sessions()
    # Refresh status in case a process died on its own (crash, manual kill, etc.)
    changed = False
    for s in sessions:
        if s["status"] == "running" and not any_process_alive(s):
            s["status"] = "stopped"
            s["stop_time"] = s.get("stop_time") or now_iso()
            changed = True
    if changed:
        save_sessions(sessions)
    return jsonify(sessions)


@app.route("/api/tasks", methods=["GET"])
def list_tasks():
    """Sessions grouped by task_name, for the resume/merge UI."""
    sessions = load_sessions()
    tasks: dict[str, list[dict]] = {}
    for s in sessions:
        tasks.setdefault(s["task_name"], []).append(s)
    result = [
        {"task_name": name, "session_count": len(sess), "sessions": sess}
        for name, sess in sorted(tasks.items())
    ]
    return jsonify(result)


@app.route("/api/session/start", methods=["POST"])
def start_session():
    body = request.get_json(force=True) or {}
    task_name = (body.get("task_name") or "").strip()
    modes = body.get("modes") or DEFAULT_MODES
    if not task_name:
        return jsonify({"error": "task_name is required"}), 400
    unknown = [m for m in modes if m not in CAPTURE_MODES]
    if unknown:
        return jsonify({"error": f"unknown capture mode(s): {unknown}. Known: {list(CAPTURE_MODES)}"}), 400

    session_id = uuid.uuid4().hex[:12]
    processes = []
    for mode in modes:
        cfg = CAPTURE_MODES[mode]
        log_path = BACKEND_DIR / f"session_{session_id}_{mode}.log"
        proc = subprocess.Popen(
            [sys.executable, str(cfg["script"]), "--label", task_name, *cfg["extra_args"]],
            stdout=open(log_path, "w"),
            stderr=subprocess.STDOUT,
            # Own process group per capture process: several watchers (audio,
            # video) block on their own ffmpeg child for a whole chunk. Without
            # this, SIGTERM to the watcher alone leaves that child orphaned and
            # still recording — confirmed happening with a real video chunk.
            # killpg on this group takes down the watcher AND its child together.
            start_new_session=True,
        )
        _processes[f"{session_id}:{mode}"] = proc
        processes.append({"mode": mode, "pid": proc.pid, "log_file": str(log_path)})

    session = {
        "session_id": session_id,
        "task_name": task_name,
        "status": "running",
        "start_time": now_iso(),
        "stop_time": None,
        "modes": modes,
        "processes": processes,
    }
    sessions = load_sessions()
    sessions.append(session)
    save_sessions(sessions)
    return jsonify(session), 201


def _stop_one_process(session_id: str, proc_info: dict) -> None:
    pid = proc_info["pid"]
    if not is_pid_alive(pid):
        return
    # Kill the whole process group (see start_new_session above) so a
    # watcher's own ffmpeg child dies with it, not just the watcher itself.
    try:
        os.killpg(os.getpgid(pid), signal.SIGTERM)
    except ProcessLookupError:
        pass
    key = f"{session_id}:{proc_info['mode']}"
    proc = _processes.pop(key, None)
    if proc is not None:
        # Started by this backend run — we hold the Popen handle, so we can
        # properly wait() on it and avoid leaving a zombie behind.
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(os.getpgid(pid), signal.SIGKILL)
            except ProcessLookupError:
                pass
            proc.wait(timeout=5)
    else:
        # Session from a previous backend run (no Popen handle here) —
        # best effort: give the OS a moment to clean it up.
        for _ in range(20):
            if not is_pid_alive(pid):
                break
            time.sleep(0.1)


@app.route("/api/session/stop", methods=["POST"])
def stop_session():
    body = request.get_json(force=True) or {}
    session_id = body.get("session_id")
    sessions = load_sessions()
    session = next((s for s in sessions if s["session_id"] == session_id), None)
    if session is None:
        return jsonify({"error": f"no session with id {session_id}"}), 404

    if session["status"] == "running":
        for proc_info in session["processes"]:
            _stop_one_process(session_id, proc_info)

    session["status"] = "stopped"
    session["stop_time"] = now_iso()
    save_sessions(sessions)
    return jsonify(session)


def _find_bucket_id(prefix: str) -> str | None:
    resp = requests.get(f"{AW_SERVER}/api/0/buckets/", timeout=5)
    resp.raise_for_status()
    for bucket_id in resp.json():
        if bucket_id.startswith(prefix):
            return bucket_id
    return None


def merge_task(task_name: str) -> dict:
    """Pull every event tagged with this task's label across all capture-mode
    buckets and merge them into one timeline, sorted by time. This is the
    "combine multiple sessions of the same task into one record" step —
    it works across sessions automatically since it filters by label, not
    by session_id, so a task resumed tomorrow merges in with today's.
    """
    timeline = []
    bucket_ids: dict[str, str] = {}
    for mode, prefix in BUCKET_PREFIXES.items():
        bucket_id = _find_bucket_id(prefix)
        if not bucket_id:
            continue
        bucket_ids[mode] = bucket_id
        resp = requests.get(f"{AW_SERVER}/api/0/buckets/{bucket_id}/events", params={"limit": 1000}, timeout=10)
        resp.raise_for_status()
        for event in resp.json():
            if event.get("data", {}).get("label") == task_name:
                timeline.append({"mode": mode, "timestamp": event["timestamp"], "data": event["data"]})

    timeline.sort(key=lambda e: e["timestamp"])

    sessions = [s for s in load_sessions() if s["task_name"] == task_name]

    return {
        "task_name": task_name,
        "session_count": len(sessions),
        "sessions": sessions,
        "buckets_used": bucket_ids,
        "event_count": len(timeline),
        "timeline": timeline,
    }


@app.route("/api/task/<task_name>/merge", methods=["GET"])
def get_task_merge(task_name: str):
    return jsonify(merge_task(task_name))


def build_skill_prompt(merged: dict) -> str:
    lines = [
        f"You are drafting a reusable AI agent skill (SKILL.md) from an observed task session.",
        f"Task name: {merged['task_name']}",
        f"Observed across {merged['session_count']} session(s), {merged['event_count']} captured events.",
        "",
        "Timeline of what was observed (OCR = text visible on screen, audio = transcribed narration,",
        "video = a recorded screen clip reference):",
        "",
    ]
    for event in merged["timeline"]:
        mode = event["mode"]
        data = event["data"]
        if mode == "video":
            lines.append(f"[{event['timestamp']}] (video clip recorded, {data.get('duration_seconds')}s)")
        else:
            text = (data.get("text") or "").strip()
            if text:
                lines.append(f"[{event['timestamp']}] ({mode}) {text}")

    lines += [
        "",
        "Based on this observed activity, draft a SKILL.md for this task following standard",
        "agent-skill conventions: a purpose section, required inputs, expected outputs, numbered",
        "steps inferred from what was actually observed, and validation notes. If the observed",
        "data is too sparse to confidently infer steps, say so explicitly rather than inventing them.",
    ]
    return "\n".join(lines)


@app.route("/api/task/<task_name>/draft_skill", methods=["POST"])
def draft_skill(task_name: str):
    # .strip() clears leading/trailing whitespace/newlines from a pasted key,
    # but can't fix a *mid-string* corruption (embedded newline, homoglyph
    # characters swapped in from a bad copy/paste) — that still needs a
    # cleanly re-copied key from the user, caught explicitly below with a
    # clear message rather than leaking a raw exception.
    api_key = (os.environ.get("ANTHROPIC_API_KEY") or "").strip()
    if not api_key:
        return jsonify({"error": "ANTHROPIC_API_KEY is not set in this backend's environment."}), 400
    if not api_key.isascii():
        return jsonify({
            "error": "ANTHROPIC_API_KEY contains non-ASCII characters (likely a corrupted copy/paste — "
                     "check for look-alike characters from a different alphabet). Re-copy it fresh from "
                     "the Anthropic Console and re-export it."
        }), 400

    merged = merge_task(task_name)
    if merged["event_count"] == 0:
        return jsonify({"error": f"No captured events found for task '{task_name}' — nothing to draft from."}), 400

    import anthropic

    client = anthropic.Anthropic(api_key=api_key)
    prompt = build_skill_prompt(merged)
    try:
        response = client.messages.create(
            model="claude-sonnet-4-5",
            max_tokens=2000,
            messages=[{"role": "user", "content": prompt}],
        )
    except anthropic.APIError as e:
        return jsonify({"error": f"Anthropic API error: {e}"}), 502
    except Exception as e:
        # Catches things like the malformed-header case (httpcore raises its
        # own error type before the request is even sent) that don't
        # subclass anthropic.APIError.
        return jsonify({"error": f"Could not call the Anthropic API: {e}"}), 502
    draft_text = "".join(block.text for block in response.content if hasattr(block, "text"))

    DRAFTS_DIR.mkdir(parents=True, exist_ok=True)
    slug = task_name.replace(" ", "-")
    draft_path = DRAFTS_DIR / f"{slug}.md"
    draft_path.write_text(draft_text, encoding="utf-8")

    return jsonify({"task_name": task_name, "draft_path": str(draft_path), "draft": draft_text})


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5677, debug=False)
