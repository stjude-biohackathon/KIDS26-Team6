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

from flask import Flask, jsonify, request
from flask_cors import CORS

BACKEND_DIR = Path(__file__).resolve().parent
SCRIPTS_DIR = BACKEND_DIR.parent / "scripts"
SESSIONS_FILE = BACKEND_DIR / "sessions.json"

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
    # "video": not built yet — same shape as the two above once it is.
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
    os.kill(pid, signal.SIGTERM)
    key = f"{session_id}:{proc_info['mode']}"
    proc = _processes.pop(key, None)
    if proc is not None:
        # Started by this backend run — we hold the Popen handle, so we can
        # properly wait() on it and avoid leaving a zombie behind.
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
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


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5677, debug=False)
