"""Remote shell and Slurm capture through mocked SSH calls."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest

from wfrec import remote, spool
from wfrec.cli import main
from wfrec.events import JOB_COMPLETED, SHELL_COMMAND
from wfrec.session import Session, SessionStore
from wfrec.state import SHELL_BACKEND_DEVSQL


def test_cli_ssh_bootstraps_and_opens_recorded_shell(
    store: SessionStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session, _ = store.start(title="Remote shell")
    captured: dict[str, object] = {}

    def bootstrap(host: str) -> dict[str, Any]:
        captured["bootstrap_host"] = host
        return {"host": host, "ok": True}

    def interactive_shell(
        host: str,
        session_id: str,
        extra_args: list[str] | None = None,
    ) -> int:
        captured.update(
            host=host,
            session_id=session_id,
            extra_args=extra_args,
        )
        return 0

    monkeypatch.setattr(remote, "bootstrap", bootstrap)
    monkeypatch.setattr(remote, "interactive_shell", interactive_shell)

    assert main(["ssh", "hpc", "--", "-J", "jump-host"]) == 0

    manifest = Session.load(session.session_id).manifest
    assert captured == {
        "bootstrap_host": "hpc",
        "host": "hpc",
        "session_id": session.session_id,
        "extra_args": ["-J", "jump-host"],
    }
    assert manifest.remote_hosts == ["hpc"]


def test_cli_pull_preserves_remote_capture_with_devsql(
    store: SessionStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session, _ = store.start(
        title="Remote pull",
        shell_backend=SHELL_BACKEND_DEVSQL,
    )
    spool.append(
        spool.spool_file(session.spool_dir, "laptop", 10),
        spool.encode(
            spool.KIND_COMMAND,
            "1",
            "/local",
            "0",
            "1",
            "local-command",
            "zsh",
            "10",
            "1",
        ),
    )
    remote_record = spool.encode(
        spool.KIND_COMMAND,
        "2",
        "/scratch",
        "0",
        "2",
        "remote-command",
        "sh",
        "20",
        "1",
    )
    accounting = (
        "JobID|JobName|State|Submit|Start|End|Elapsed|ExitCode|NodeList|"
        "Partition|ReqCPUS|ReqMem|WorkDir\n"
        "42|align|COMPLETED|s|s|e|00:01|0:0|n1|compute|1|1G|/scratch\n"
    )

    def run_ssh(
        command: list[str],
        *,
        input_text: str | None = None,
        timeout: int = remote.SSH_TIMEOUT,
    ) -> subprocess.CompletedProcess[str]:
        del input_text, timeout
        remote_command = command[-1]
        if "ls -1" in remote_command:
            stdout = "/home/user/.wfrec/spool/session/login2-20.rec\n"
        elif remote_command.startswith("cat "):
            stdout = remote_record
        elif "sacct -X" in remote_command:
            stdout = accounting
        elif "scontrol show job" in remote_command:
            stdout = "alignment finished\n"
        else:
            raise AssertionError(f"Unexpected SSH command: {remote_command}")
        return subprocess.CompletedProcess(command, 0, stdout, "")

    monkeypatch.setattr(remote, "_run", run_ssh)

    assert main(["pull", "hpc", "--job", "42"]) == 0

    events = session.writer.read()
    commands = [event for event in events if event.type == SHELL_COMMAND]
    jobs = [event for event in events if event.type == JOB_COMPLETED]
    assert [(event.origin, event.payload["command"]) for event in commands] == [
        ("remote:hpc", "remote-command")
    ]
    assert len(jobs) == 1
    assert jobs[0].origin == "remote:hpc"
    assert jobs[0].payload["JobID"] == "42"
    assert (session.root / "jobs" / "hpc-42.out").read_text(
        encoding="utf-8"
    ) == "alignment finished\n"
