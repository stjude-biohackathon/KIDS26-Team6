"""Session controller for AutoCAB's ActivityWatch tooling.

The forked dashboard (aw-webui) can't itself spawn a screen-capture process —
browsers are sandboxed from doing that. This small local API is what the
dashboard's Start/Stop buttons actually call: it launches/kills the capture
script as a real OS process and keeps track of sessions, grouped by task name
so the same task can be resumed across multiple start/stop sessions.

Run: python3 session_controller.py   (serves on http://localhost:5677)
"""

from __future__ import annotations

import json
import os
import time
import signal
import subprocess
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

from flask import Flask, jsonify, request
from flask_cors import CORS

BACKEND_DIR = Path(__file__).resolve().parent
SCRIPTS_DIR = BACKEND_DIR.parent / "scripts"
WATCHER_SCRIPT = SCRIPTS_DIR / "screenshot_ocr_watcher.py"
SESSIONS_FILE = BACKEND_DIR / "sessions.json"

app = Flask(__name__)
CORS(app, origins=["http://localhost:27180"])

# Popen objects for processes started by *this* backend run, so we can
# properly wait()/reap them on stop instead of leaving zombies behind.
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


@app.route("/api/sessions", methods=["GET"])
def list_sessions():
    sessions = load_sessions()
    # Refresh status in case a process died on its own (crash, manual kill, etc.)
    changed = False
    for s in sessions:
        if s["status"] == "running" and not is_pid_alive(s["pid"]):
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
    if not task_name:
        return jsonify({"error": "task_name is required"}), 400

    session_id = uuid.uuid4().hex[:12]
    log_path = BACKEND_DIR / f"session_{session_id}.log"

    proc = subprocess.Popen(
        [sys.executable, str(WATCHER_SCRIPT), "--label", task_name],
        stdout=open(log_path, "w"),
        stderr=subprocess.STDOUT,
    )

    session = {
        "session_id": session_id,
        "task_name": task_name,
        "status": "running",
        "start_time": now_iso(),
        "stop_time": None,
        "pid": proc.pid,
        "log_file": str(log_path),
    }
    _processes[session_id] = proc
    sessions = load_sessions()
    sessions.append(session)
    save_sessions(sessions)
    return jsonify(session), 201


@app.route("/api/session/stop", methods=["POST"])
def stop_session():
    body = request.get_json(force=True) or {}
    session_id = body.get("session_id")
    sessions = load_sessions()
    session = next((s for s in sessions if s["session_id"] == session_id), None)
    if session is None:
        return jsonify({"error": f"no session with id {session_id}"}), 404

    if session["status"] == "running" and is_pid_alive(session["pid"]):
        os.kill(session["pid"], signal.SIGTERM)
        proc = _processes.pop(session_id, None)
        if proc is not None:
            # We started this process ourselves in this run, so we can
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
                if not is_pid_alive(session["pid"]):
                    break
                time.sleep(0.1)

    session["status"] = "stopped"
    session["stop_time"] = now_iso()
    save_sessions(sessions)
    return jsonify(session)


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5677, debug=False)
