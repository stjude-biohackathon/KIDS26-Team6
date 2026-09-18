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


# --------------------------------------------------------------------------
# step 7: remote output used to land verbatim
# --------------------------------------------------------------------------
def test_pulled_remote_output_is_redacted_before_it_touches_disk(store, monkeypatch):
    """`shell/remote/*` is remote command output from an HPC host.

    It was written verbatim, which left the seal as its only control over the
    channel most likely to contain a clinical `grep`.
    """

    import subprocess

    from wfrec import remote

    session, _ = store.start(title="A", analyst="a")

    def fake_run(argv, **kwargs):
        joined = " ".join(argv)
        if "ls " in joined or "find" in joined:
            return subprocess.CompletedProcess(argv, 0, "/tmp/wfrec-spool/host-1.tsv\n", "")
        if "cat" in joined:
            return subprocess.CompletedProcess(
                argv, 0, "$ grep 'MRN 4419902' /data/SJ-4817/chart.txt\n", ""
            )
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(remote, "_run", fake_run)
    result = remote.pull_spool("hpc-login", session)

    assert result["pulled"], result
    blob = "\n".join(
        path.read_text(encoding="utf-8")
        for path in (session.root / "shell" / "remote").rglob("*")
        if path.is_file()
    )
    assert "4419902" not in blob
    assert "SJ-4817" not in blob
    assert "[REDACTED_MRN]" in blob


def test_slurm_job_output_and_accounting_fields_are_redacted(store, monkeypatch):
    """Scheduler output is a high-risk channel: a job's stdout is whatever the
    pipeline printed, which routinely includes sample manifests and paths."""

    import subprocess

    from wfrec import remote

    session, _ = store.start(title="A", analyst="a")
    header = (
        "JobID|JobName|State|Submit|Start|End|Elapsed|ExitCode|NodeList|"
        "Partition|ReqCPUS|ReqMem|WorkDir"
    )
    row = (
        "412391|wgs_SJ-4817_dx|COMPLETED|2026-09-01T10:00:00|2026-09-01T10:01:00|"
        "2026-09-01T11:00:00|01:00:00|0:0|node07|standard|8|32G|/data/proj/SJ-4817"
    )

    def fake_run(argv, **kwargs):
        joined = " ".join(argv)
        if "sacct" in joined:
            return subprocess.CompletedProcess(argv, 0, f"{header}\n{row}\n", "")
        if "scontrol" in joined:
            return subprocess.CompletedProcess(
                argv, 0, "Sample manifest: MRN 4419902 /data/SJ-4817/x.bam\n", ""
            )
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(remote, "_run", fake_run)
    events = remote.collect_slurm("hpc-login", session, ["412391"])

    completed = [event for event in events if event.type == "job.completed"]
    assert completed, events
    payload = completed[0].payload
    # Free-text fields scrubbed...
    assert "SJ-4817" not in payload["JobName"]
    assert "SJ-4817" not in payload["WorkDir"]
    assert "sj_id" in completed[0].redactions
    # ...while the numeric accounting columns the dashboard reads are untouched.
    assert payload["ReqCPUS"] == "8"
    assert payload["ReqMem"] == "32G"
    assert payload["Elapsed"] == "01:00:00"
    assert payload["JobID"] == "412391"

    written = list((session.root / "jobs").glob("*.out"))
    assert written
    blob = written[0].read_text(encoding="utf-8")
    assert "4419902" not in blob
    assert "SJ-4817" not in blob


def test_remote_pull_refuses_when_redaction_is_unavailable(store, monkeypatch):
    import subprocess

    from wfrec import remote

    session, _ = store.start(title="A", analyst="a")

    class BrokenRedactor:
        available = False
        error = "boom"

        def apply(self, text):
            from wfrec.redaction import Redacted

            return Redacted(text=text, findings=[], available=False)

    def fake_run(argv, **kwargs):
        joined = " ".join(argv)
        if "ls " in joined or "find" in joined:
            return subprocess.CompletedProcess(argv, 0, "/tmp/wfrec-spool/host-1.tsv\n", "")
        if "cat" in joined:
            return subprocess.CompletedProcess(argv, 0, "MRN 4419902\n", "")
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(remote, "_run", fake_run)
    monkeypatch.setattr(remote, "shared_redactor", lambda: BrokenRedactor())

    with pytest.raises(RuntimeError, match="shared redactor is unavailable"):
        remote.pull_spool("hpc-login", session)
