"""Remote capture over the user's own ssh.

Design choice that matters most here: **wrap the user's ``ssh`` binary, do not
reimplement SSH.** ``paramiko`` does not honour ``~/.ssh/config`` ``Match``/
``Include`` semantics, and its keyboard-interactive handling for Duo/2FA is
manual work -- both of which are disqualifying on hospital HPC. Shelling out to
``ssh`` means ProxyJump, GSSAPI, ControlMaster, agent forwarding and 2FA all
behave exactly as the analyst already expects.

The remote spool deliberately lives under ``$HOME``, the opposite of the local
choice. HPC login nodes are load-balanced, so ``/tmp`` on ``login2`` is
invisible from ``login1``, while ``$HOME`` is shared. Multi-writer ``O_APPEND``
over NFS would be unsafe, but there is exactly one writer per spool file.
"""

from __future__ import annotations

import shutil
import subprocess
from importlib import resources
from pathlib import Path
from typing import Any

from .events import JOB_COMPLETED, Event
from .output import error
from .redaction import shared as shared_redactor

REMOTE_DIR = "~/.wfrec"
SSH_TIMEOUT = 60


def ssh_available() -> bool:
    return bool(shutil.which("ssh"))


def _run(cmd: list[str], *, input_text: str | None = None, timeout: int = SSH_TIMEOUT):
    return subprocess.run(
        cmd,
        input=input_text,
        capture_output=True,
        text=True,
        timeout=timeout,
        errors="replace",
    )


def hook_body() -> str:
    return (
        resources.files("wfrec.hooks")
        .joinpath("wfrec-remote.sh")
        .read_text(encoding="utf-8")
    )


def bootstrap(host: str) -> dict[str, Any]:
    """Copy the POSIX hook to ``host`` and register it in the remote rc files.

    Appends to both ``~/.bashrc`` and ``~/.profile``: on RHEL a login shell
    sources ``.bash_profile`` which sources ``.bashrc``, whereas ``ssh host cmd``
    sources ``.bashrc`` only because of bash's "stdin is a socket" rule, which
    is common but not guaranteed.
    """

    if not ssh_available():
        return {"host": host, "ok": False, "error": "ssh not found on PATH"}

    script = (
        f"mkdir -p {REMOTE_DIR}/spool && cat > {REMOTE_DIR}/hook.sh && "
        f"chmod 0600 {REMOTE_DIR}/hook.sh && "
        'for f in "$HOME/.bashrc" "$HOME/.profile"; do '
        '  [ -e "$f" ] || touch "$f"; '
        '  grep -q "WFREC_HOOK" "$f" 2>/dev/null || '
        '    printf "\\n# WFREC_HOOK\\n[ -f \\$HOME/.wfrec/hook.sh ] && . \\$HOME/.wfrec/hook.sh\\n# WFREC_HOOK_END\\n" >> "$f"; '
        "done && echo WFREC_BOOTSTRAP_OK"
    )
    try:
        proc = _run(["ssh", host, script], input_text=hook_body())
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"host": host, "ok": False, "error": f"{type(exc).__name__}: {exc}"}

    ok = proc.returncode == 0 and "WFREC_BOOTSTRAP_OK" in proc.stdout
    return {
        "host": host,
        "ok": ok,
        "stdout": proc.stdout.strip()[-400:],
        "stderr": proc.stderr.strip()[-400:],
    }


def interactive_shell(host: str, session_id: str, extra_args: list[str] | None = None) -> int:
    """Open an interactive remote shell whose commands land in this session.

    Sourcing the hook explicitly rather than relying on the rc file avoids the
    standard ``case $- in *i*) ;; *) return;; esac`` early-return that would
    silently skip it for a non-login invocation.
    """

    remote_spool = f"$HOME/.wfrec/spool/{session_id}"
    command = (
        f"mkdir -p {remote_spool}; "
        f"WFREC_SESSION={session_id} WFREC_SPOOL={remote_spool} "
        f". $HOME/.wfrec/hook.sh 2>/dev/null; "
        f"export WFREC_SESSION WFREC_SPOOL; exec ${{SHELL:-/bin/bash}} -i"
    )
    cmd = ["ssh", "-t", *(extra_args or []), host, command]
    try:
        return subprocess.call(cmd)
    except OSError as exc:
        error(f"SSH failed: {exc}")
        return 1


def _redact_required(text: str, *, what: str):
    redacted = shared_redactor().apply(text)
    if text and not redacted.available:
        raise RuntimeError(
            f"cannot record {what}: shared redactor is unavailable"
        )
    return redacted


def pull_spool(host: str, session, *, session_id: str | None = None) -> dict[str, Any]:
    """Copy remote spool files into ``<session>/shell/remote/`` for ingest."""

    sid = session_id or session.session_id
    target_dir = session.root / "shell" / "remote" / host
    target_dir.mkdir(parents=True, exist_ok=True)

    listing = _run(
        ["ssh", host, f"ls -1 $HOME/.wfrec/spool/{sid}/*.rec 2>/dev/null || true"]
    )
    files = [line.strip() for line in listing.stdout.splitlines() if line.strip()]
    pulled: list[str] = []
    for remote_path in files:
        name = Path(remote_path).name
        # cat over the existing ssh path rather than scp: one mechanism, and it
        # works when scp is absent or disabled (increasingly common).
        proc = _run(["ssh", host, f"cat {remote_path}"])
        if proc.returncode != 0:
            continue
        # Redacted inline. This is remote command output pulled off an HPC host
        # and written straight to `shell/remote/*`, and until now it landed
        # verbatim -- which left the seal as its only control over the one
        # channel most likely to contain a clinical grep.
        (target_dir / name).write_text(
            _redact_required(proc.stdout, what="remote spool output").text,
            encoding="utf-8",
        )
        pulled.append(name)

    return {"host": host, "pulled": pulled, "dir": str(target_dir)}


def collect_slurm(host: str, session, job_ids: list[str]) -> list[Event]:
    """Fetch job accounting and a bounded slice of each job's output file."""

    events: list[Event] = []
    if not job_ids:
        return events

    joined = ",".join(job_ids)
    # --parsable2 is pipe-delimited with no trailing pipe. MaxRSS exists only on
    # .batch steps, so a second non-`-X` query is needed for memory numbers.
    fmt = "JobID,JobName,State,Submit,Start,End,Elapsed,ExitCode,NodeList,Partition,ReqCPUS,ReqMem,WorkDir"
    proc = _run(["ssh", host, f"sacct -X --parsable2 -j {joined} --format={fmt} 2>/dev/null || true"])
    rows = [line for line in proc.stdout.splitlines() if line.strip()]
    if len(rows) > 1:
        header = rows[0].split("|")
        for row in rows[1:]:
            values = row.split("|")
            record = dict(zip(header, values))
            # `JobName` and `WorkDir` are free-text: a job name is routinely a
            # cohort or subject code and a working directory is a path. Redacted
            # per field so the numeric accounting columns -- which the activity
            # dashboard reads -- are untouched.
            redactor = shared_redactor()
            findings: set[str] = set()
            for key in ("JobName", "WorkDir", "Comment"):
                value = record.get(key)
                if isinstance(value, str) and value:
                    redacted = _redact_required(
                        value, what=f"slurm accounting field {key}"
                    )
                    record[key] = redacted.text
                    findings.update(redacted.findings)
            events.append(
                Event(
                    source="shell",
                    type=JOB_COMPLETED,
                    origin=f"remote:{host}",
                    host=host,
                    payload={"scheduler": "slurm", **record},
                    redactions=sorted(findings),
                )
            )
    elif proc.stdout.strip() == "" :
        # sacct returns nothing at all when slurmdbd accounting is not
        # configured, which is common on smaller clusters. Say so rather than
        # reporting "no jobs".
        events.append(
            Event(
                source="shell",
                type="job.accounting.unavailable",
                origin=f"remote:{host}",
                host=host,
                payload={
                    "scheduler": "slurm",
                    "job_ids": job_ids,
                    "detail": "sacct returned no rows; slurmdbd accounting may be unconfigured.",
                },
            )
        )

    jobs_dir = session.root / "jobs"
    jobs_dir.mkdir(parents=True, exist_ok=True)
    for job_id in job_ids:
        out = _run(
            [
                "ssh",
                host,
                # A slurm-*.out from a long alignment can be gigabytes; take a
                # bounded head and tail rather than the whole file.
                f"p=$(scontrol show job {job_id} -o 2>/dev/null | tr ' ' '\\n' | "
                f"grep '^StdOut=' | cut -d= -f2-); "
                f'[ -n "$p" ] && [ -f "$p" ] && '
                f'{{ head -c 65536 "$p"; echo; echo "...[wfrec: middle elided]..."; tail -c 65536 "$p"; }} || true',
            ]
        )
        if out.returncode == 0 and out.stdout.strip():
            # Scheduler output is a high-risk channel: a job's stdout is
            # whatever the pipeline printed, which routinely includes sample
            # manifests and file paths. Also written verbatim until now.
            (jobs_dir / f"{host}-{job_id}.out").write_text(
                _redact_required(out.stdout, what="slurm job output").text,
                encoding="utf-8",
            )
    return events
