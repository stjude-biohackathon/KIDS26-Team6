"""Collector behaviour: gates, degradation, and ingest."""

from __future__ import annotations

import json
import sqlite3
import subprocess
from pathlib import Path

import pytest

from autocab.recording import spool
from autocab.recording.collectors.base import Collector, CollectorStatus, Degraded
from autocab.recording.collectors.files import (
    METADATA_ONLY_BYTES,
    FileCollector,
    is_binary_path,
    looks_binary,
    run_git,
)
from autocab.recording.collectors.screen import CHANGE_THRESHOLD, changed_fraction, frame_signature
from autocab.recording.collectors.shell import (
    ShellCollector,
    _parse_scontrol,
    active_interval_start,
    epoch_ms_to_iso,
)
from autocab.recording.devsql import DevSQLClient
from autocab.recording.session import SessionStore


# --------------------------------------------------------------------- shell
def test_spool_records_become_timeline_events(store):
    session, _ = store.start(title="A", analyst="a")
    target = spool.spool_file(session.spool_dir, "lab-mbp", 4411)
    spool.append(
        target,
        spool.encode(
            spool.KIND_COMMAND,
            "1789578668017",
            "/data/hg008",
            "0",
            "8421",
            "samtools view -c HG008.bam",
            "zsh",
            "4411",
            "1",
        ),
    )

    collector = ShellCollector(session)
    collector.safe_probe()
    collector._run_once()

    events = [e for e in session.writer.read() if e.type == "shell.command.completed"]
    assert len(events) == 1
    assert events[0].payload["command"] == "samtools view -c HG008.bam"
    assert events[0].payload["exit_code"] == 0
    assert events[0].payload["duration_ms"] == 8421
    assert events[0].host == "lab-mbp"


def test_shell_ingest_is_idempotent_across_restart(store):
    """A daemon restart re-reads spool files; commands must not double up."""

    session, _ = store.start(title="A", analyst="a")
    target = spool.spool_file(session.spool_dir, "host", 10)
    spool.append(
        target, spool.encode(spool.KIND_COMMAND, "1", "/tmp", "0", "1", "ls", "bash", "10", "1")
    )

    first = ShellCollector(session)
    first.safe_probe()
    first._run_once()

    # A brand-new collector, as a restarted daemon would build, reading from
    # offset zero again.
    second = ShellCollector(session)
    second.safe_probe()
    second._run_once()

    events = [e for e in session.writer.read() if e.type == "shell.command.completed"]
    assert len(events) == 1


def test_shell_commands_are_redacted(store):
    session, _ = store.start(title="A", analyst="a")
    target = spool.spool_file(session.spool_dir, "host", 11)
    spool.append(
        target,
        spool.encode(
            spool.KIND_COMMAND,
            "1",
            "/tmp",
            "0",
            "1",
            "curl -u bob@stjude.org https://x/SJ001234",
            "bash",
            "11",
            "1",
        ),
    )
    collector = ShellCollector(session)
    collector.safe_probe()
    collector._run_once()

    event = [e for e in session.writer.read() if e.type == "shell.command.completed"][0]
    assert "bob@stjude.org" not in event.payload["command"]
    assert set(event.redactions) >= {"email", "sj_id"}


def test_devsql_ingests_atuin_claude_and_codex_without_duplicates(
    store: SessionStore,
) -> None:
    session, _ = store.start(title="A", analyst="a")
    timestamp = active_interval_start(session)
    rows = [
        _devsql_command_row(
            source="atuin",
            channel="shell",
            source_id="atuin-1",
            session_id="shell-session",
            timestamp=timestamp,
            command="cat /data/SJ001234.vcf",
        ),
        _devsql_command_row(
            source="claude",
            channel="agent_tool",
            source_id="shared-id",
            session_id="agent-session",
            timestamp=timestamp,
            command="pytest tests/test_pipeline.py",
        ),
        _devsql_command_row(
            source="codex",
            channel="agent_tool",
            source_id="shared-id",
            session_id="agent-session",
            timestamp=timestamp,
            command="ruff check src",
        ),
    ]
    queries: list[str] = []
    client = _devsql_client(rows, queries=queries)
    collector = ShellCollector(session, devsql_client=client)
    collector.safe_probe()
    collector._run_once()
    collector._run_once()

    events = [event for event in session.writer.read() if event.type == "shell.command.completed"]
    assert [event.payload["source"] for event in events] == [
        "atuin",
        "claude",
        "codex",
    ]
    assert events[1].payload["source_id"] == events[2].payload["source_id"]
    assert events[1].payload["channel"] == "agent_tool"
    assert events[2].payload["channel"] == "agent_tool"
    assert "shell" not in events[1].payload
    assert "shell" not in events[2].payload
    assert "SJ001234" not in events[0].payload["command"]
    assert "sj_id" in events[0].redactions
    assert len(queries) == 2
    assert all("source = 'atuin' OR channel = 'agent_tool'" in sql for sql in queries)


def test_devsql_restores_shell_from_atuin_database(
    store: SessionStore,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session, _ = store.start(title="Atuin shell", analyst="analyst")
    database_path = tmp_path / "history.db"
    connection = sqlite3.connect(database_path)
    try:
        connection.execute("CREATE TABLE history (id TEXT PRIMARY KEY, shell TEXT)")
        connection.execute(
            "INSERT INTO history (id, shell) VALUES (?, ?)",
            ("atuin-command", "zsh"),
        )
        connection.commit()
    finally:
        connection.close()

    connect_calls: list[tuple[str, bool]] = []
    real_connect = sqlite3.connect

    def record_connect(
        database: str,
        *,
        uri: bool = False,
        timeout: float = 5.0,
    ) -> sqlite3.Connection:
        connect_calls.append((database, uri))
        return real_connect(database, uri=uri, timeout=timeout)

    monkeypatch.setattr(
        "autocab.recording.collectors.shell.sqlite3.connect",
        record_connect,
    )
    row = _devsql_command_row(
        source="atuin",
        channel="shell",
        source_id="atuin-command",
        session_id="shell-session",
        timestamp=active_interval_start(session),
        command="python workflow.py",
        source_path=str(database_path),
    )

    ShellCollector(session, devsql_client=_devsql_client([row]))._run_once()

    event = [event for event in session.writer.read() if event.type == "shell.command.completed"][0]
    assert event.payload["shell"] == "zsh"
    assert connect_calls == [(f"{database_path.as_uri()}?mode=ro", True)]


def test_devsql_omits_shell_when_atuin_database_is_unavailable(
    store: SessionStore,
    tmp_path: Path,
) -> None:
    session, _ = store.start(title="Missing Atuin", analyst="analyst")
    row = _devsql_command_row(
        source="atuin",
        channel="shell",
        source_id="atuin-command",
        session_id="shell-session",
        timestamp=active_interval_start(session),
        command="python workflow.py",
        source_path=str(tmp_path / "missing.db"),
    )

    ShellCollector(session, devsql_client=_devsql_client([row]))._run_once()

    event = [event for event in session.writer.read() if event.type == "shell.command.completed"][0]
    assert "shell" not in event.payload


def test_devsql_rejects_rows_outside_the_safe_capture_scope(
    store: SessionStore,
) -> None:
    session, _ = store.start(title="A", analyst="a")
    rows = [
        _devsql_command_row(
            source="zsh",
            channel="shell",
            source_id="zsh-1",
            session_id="shell-session",
            timestamp=active_interval_start(session),
            command="excluded zsh command",
        ),
        _devsql_command_row(
            source="atuin",
            channel="shell",
            source_id="old-1",
            session_id="shell-session",
            timestamp="2020-01-01T00:00:00.000Z",
            command="excluded old command",
        ),
        _devsql_command_row(
            source="codex",
            channel="agent_tool",
            source_id="",
            session_id="agent-session",
            timestamp=active_interval_start(session),
            command="excluded unstable command",
        ),
    ]
    client = _devsql_client(rows)
    collector = ShellCollector(session, devsql_client=client)
    collector.safe_probe()
    collector._run_once()

    events = [event for event in session.writer.read() if event.type == "shell.command.completed"]
    assert events == []


def test_remote_spool_keeps_its_own_hostname(store, tmp_path):
    """A pulled-back remote spool must not inherit the laptop's hostname."""

    session, _ = store.start(title="A", analyst="a")
    remote_dir = tmp_path / "remote"
    remote_dir.mkdir()
    spool.append(
        remote_dir / "login2-7788.rec",
        spool.encode(
            spool.KIND_COMMAND, "1", "/scratch", "0", "5", "sbatch align.sh", "sh", "7788", "1"
        ),
    )

    collector = ShellCollector(session)
    collector.safe_probe()
    collector.add_spool(remote_dir, "remote:hpc")
    collector._run_once()

    event = [e for e in session.writer.read() if e.type == "shell.command.completed"][0]
    assert event.host == "login2"
    assert event.origin == "remote:hpc"


def test_slurm_job_records_capture_real_output_paths(store):
    session, _ = store.start(title="A", analyst="a")
    detail = "JobId=4213 JobName=bwa_align StdOut=/scratch/logs/4213.out WorkDir=/scratch/hg008 NumCPUs=16"
    spool.append(
        spool.spool_file(session.spool_dir, "login1", 1),
        spool.encode(spool.KIND_JOB, "1789578668017", "slurm", "4213", detail),
    )
    collector = ShellCollector(session)
    collector.safe_probe()
    collector._run_once()

    event = [e for e in session.writer.read() if e.type == "job.submitted"][0]
    assert event.payload["job_id"] == "4213"
    assert event.payload["stdout"] == "/scratch/logs/4213.out"
    assert event.payload["workdir"] == "/scratch/hg008"


def test_parse_scontrol_ignores_unwanted_fields():
    parsed = _parse_scontrol("JobId=1 JobName=x Secret=y WorkDir=/w")
    assert parsed == {"jobname": "x", "workdir": "/w"}


def test_epoch_conversion_rejects_garbage():
    assert epoch_ms_to_iso("") is None
    assert epoch_ms_to_iso("not-a-number") is None
    assert epoch_ms_to_iso("0") is None
    assert epoch_ms_to_iso("1789578668017").endswith("Z")


def _devsql_command_row(
    *,
    source: str,
    channel: str,
    source_id: str,
    session_id: str,
    timestamp: str,
    command: str,
    source_path: str = "",
) -> dict[str, object]:
    """Build one complete normalized row without reading local history."""

    return {
        "source": source,
        "session_id": session_id,
        "source_id": source_id,
        "timestamp": timestamp,
        "duration_ms": 25,
        "command": command,
        "cwd": "/work/project",
        "exit_code": 0,
        "hostname": "workstation",
        "channel": channel,
        "actor": "agent" if channel == "agent_tool" else None,
        "provenance_quality": "explicit",
        "provenance_reason": "test fixture",
        "agent_id": source if channel == "agent_tool" else None,
        "agent_role": "coding" if channel == "agent_tool" else None,
        "originator": "user" if channel == "agent_tool" else None,
        "tool_name": "shell" if channel == "agent_tool" else None,
        "source_path": source_path,
    }


def _devsql_client(
    rows: list[dict[str, object]],
    *,
    queries: list[str] | None = None,
) -> DevSQLClient:
    """Return a client backed by deterministic normalized rows."""

    def runner(
        command: list[str],
        **options: object,
    ) -> subprocess.CompletedProcess[str]:
        if command[-1] == "--version":
            return subprocess.CompletedProcess(
                args=command,
                returncode=0,
                stdout="devsql 0.5.1\n",
                stderr="",
            )
        if queries is not None:
            queries.append(command[-1])
        return subprocess.CompletedProcess(
            args=command,
            returncode=0,
            stdout=json.dumps(rows),
            stderr="",
        )

    return DevSQLClient(Path("/test/devsql"), runner=runner)


# --------------------------------------------------------------------- files
@pytest.mark.parametrize(
    "name,binary",
    [
        ("HG008.bam", True),
        ("reads.fastq.gz", True),
        ("x.cram", True),
        ("calls.vcf", True),
        ("matrix.h5ad", True),
        ("run.py", False),
        ("Snakefile", False),
        ("notes.md", False),
    ],
)
def test_genomics_extensions_are_never_read(name, binary):
    assert is_binary_path(Path("/data") / name) is binary


def test_binary_sniff_uses_nul_byte(tmp_path):
    text = tmp_path / "a.txt"
    text.write_text("hello\nworld\n")
    assert looks_binary(text) is False

    binary = tmp_path / "b.dat"
    binary.write_bytes(b"hello\x00world")
    assert looks_binary(binary) is True


def test_git_runs_with_no_optional_locks(monkeypatch, tmp_path):
    """The flag that stops git rehashing a tracked 100 GB BAM.

    Without --no-optional-locks, `git status` refreshes the index, which
    recomputes the OID of any modified tracked file. One touched multi-gigabyte
    BAM would then make git read the whole thing.
    """

    seen: dict[str, list[str]] = {}

    def fake_run(cmd, **kwargs):
        seen["cmd"] = cmd

        class Result:
            returncode = 0
            stdout = ""

        return Result()

    monkeypatch.setattr(subprocess, "run", fake_run)
    run_git(tmp_path, "status")
    assert "--no-optional-locks" in seen["cmd"]


def test_large_file_is_metadata_only(store, tmp_path, monkeypatch):
    session, _ = store.start(title="A", analyst="a")
    collector = FileCollector(session, roots=[tmp_path])

    big = tmp_path / "huge.tsv"
    big.write_text("x" * 64)
    monkeypatch.setattr(
        Path,
        "stat",
        lambda self, *a, **k: type(
            "S", (), {"st_size": METADATA_ONLY_BYTES + 1, "st_mtime_ns": 1}
        )(),
    )
    event = collector._describe(big)
    assert event.payload["content"] == "skipped-too-large"


def test_prune_dirs_are_ignored(store, tmp_path):
    session, _ = store.start(title="A", analyst="a")
    collector = FileCollector(session, roots=[tmp_path])
    assert collector._should_prune(tmp_path / ".git" / "index")
    assert collector._should_prune(tmp_path / "node_modules" / "x.js")
    assert collector._should_prune(tmp_path / "work" / "ab" / "cd" / "out.txt")
    assert collector._should_prune(tmp_path / "run.py.swp")
    assert not collector._should_prune(tmp_path / "run.py")


def test_repeated_identical_change_emits_once(store, tmp_path):
    """One editor save arrives as several filesystem events."""

    session, _ = store.start(title="A", analyst="a")
    collector = FileCollector(session, roots=[tmp_path])
    target = tmp_path / "run.py"
    target.write_text("print(1)\n")

    assert collector._describe(target) is not None
    assert collector._describe(target) is None  # unchanged size+mtime

    target.write_text("print(1)\nprint(2)\n")
    assert collector._describe(target) is not None


def test_file_collector_without_roots_degrades(store):
    session, _ = store.start(title="A", analyst="a")
    status = FileCollector(session, roots=[]).safe_probe()
    assert status.available is False
    assert status.reason == "no-watch-roots"


def test_git_snapshot_and_diff_against_real_repo(store, tmp_path):
    repo = tmp_path / "proj"
    repo.mkdir()
    for args in (
        ["init", "-q", "."],
        ["config", "user.email", "t@t"],
        ["config", "user.name", "t"],
    ):
        subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)
    (repo / "run.py").write_text("print('v1')\n")
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(repo), "commit", "-qm", "init"], check=True, capture_output=True
    )

    session, _ = store.start(title="A", analyst="a")
    collector = FileCollector(session, roots=[repo])
    collector.safe_probe()
    (repo / "run.py").write_text("print('v2')\nprint('new')\n")
    collector.snapshot_all("test")

    events = session.writer.read()
    snapshots = [e for e in events if e.type == "git.snapshot"]
    diffs = [e for e in events if e.type == "file.diff"]
    assert snapshots and snapshots[-1].payload["changed_count"] == 1
    assert diffs and diffs[-1].payload["path"] == "run.py"
    assert (session.root / diffs[-1].ref).read_text().startswith("diff --git")


def test_git_pathspec_excludes_genomics_files(store, tmp_path):
    """A tracked BAM must not appear in the diff stat."""

    repo = tmp_path / "proj"
    repo.mkdir()
    for args in (
        ["init", "-q", "."],
        ["config", "user.email", "t@t"],
        ["config", "user.name", "t"],
    ):
        subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)
    (repo / "x.bam").write_bytes(b"\x00" * 32)
    (repo / "run.py").write_text("a\n")
    subprocess.run(["git", "-C", str(repo), "add", "-A", "-f"], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(repo), "commit", "-qm", "init"], check=True, capture_output=True
    )

    (repo / "x.bam").write_bytes(b"\x01" * 64)
    (repo / "run.py").write_text("b\n")

    session, _ = store.start(title="A", analyst="a")
    collector = FileCollector(session, roots=[repo])
    collector.safe_probe()
    collector.snapshot_all("test")

    snapshot = [e for e in session.writer.read() if e.type == "git.snapshot"][-1]
    paths = [entry["path"] for entry in snapshot.payload["numstat"]]
    assert "run.py" in paths
    assert "x.bam" not in paths


# -------------------------------------------------------------------- screen
def test_change_detection_thresholds():
    """Measured behaviour, not a guess.

    An 8x8 dHash -- the obvious choice, and what most screenshot tools use --
    scored a Hamming distance of 1 for a new line of terminal output and 2 for
    an application switch, both of which would have been discarded as
    unchanged. Text-heavy developer screens need a spatial comparison.
    """

    from PIL import Image, ImageDraw

    def terminal(lines):
        image = Image.new("RGB", (800, 600), (18, 18, 22))
        draw = ImageDraw.Draw(image)
        for index, text in enumerate(lines):
            y = 20 + index * 24
            draw.rectangle([20, y, 20 + len(text) * 7, y + 12], fill=(200, 200, 205))
        return image

    base = frame_signature(terminal(["$ samtools view -c HG008.bam", "1234567"]))
    same = frame_signature(terminal(["$ samtools view -c HG008.bam", "1234567"]))
    cursor = frame_signature(terminal(["$ samtools view -c HG008.bam", "1234567 "]))
    added = frame_signature(
        terminal(["$ samtools view -c HG008.bam", "1234567", "$ bwa mem ref.fa"])
    )

    assert changed_fraction(base, same) < CHANGE_THRESHOLD
    assert changed_fraction(base, cursor) < CHANGE_THRESHOLD, "cursor blink must not keep a frame"
    assert changed_fraction(base, added) >= CHANGE_THRESHOLD, "new output must keep a frame"


def test_changed_fraction_handles_mismatched_signatures():
    assert changed_fraction(b"", b"abc") == 1.0
    assert changed_fraction(b"ab", b"abc") == 1.0


# ---------------------------------------------------------------------- base
def test_collector_probe_failure_is_recorded_not_raised(store):
    session, _ = store.start(title="A", analyst="a")

    class Broken(Collector):
        source = "screen"

        def probe(self) -> CollectorStatus:
            raise Degraded("nope", "because", fallback="manual")

    status = Broken(session).start()
    assert status.available is False
    assert status.reason == "nope"

    disabled = [e for e in session.writer.read() if e.type == "source.disabled"]
    assert disabled[-1].payload["reason"] == "nope"
    assert disabled[-1].payload["fallback"] == "manual"


def test_unexpected_probe_exception_is_contained(store):
    session, _ = store.start(title="A", analyst="a")

    class Exploding(Collector):
        source = "files"

        def probe(self) -> CollectorStatus:
            raise RuntimeError("kaboom")

    status = Exploding(session).safe_probe()
    assert status.available is False
    assert status.reason == "probe-failed"
    assert "kaboom" in status.detail


# --------------------------------------------------------------------------
# step 7: the inline gaps that used to write verbatim
# --------------------------------------------------------------------------
def test_file_changed_paths_are_redacted_inline(store, tmp_path):
    """A filename is the dominant identifier surface in this domain.

    `/data/proj/SJALL018/smith_jane_R1.fastq.gz` carries a subject id *and* a
    patient name, and this event wrote it verbatim -- making the seal its only
    control.
    """

    session, _ = store.start(title="A", analyst="a")
    collector = FileCollector(session, roots=[tmp_path])

    target = tmp_path / "SJALL018" / "smith_jane_R1.fastq.gz"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("ACGT\n", encoding="utf-8")

    event = collector._describe(target)

    assert "SJALL018" not in event.payload["path"]
    assert "smith_jane" not in event.payload["path"]
    assert set(event.redactions) >= {"sj_id", "name"}
    # The directory structure survives: only the components are replaced.
    assert event.payload["path"].endswith("_R1.fastq.gz")


def test_a_deleted_file_event_is_redacted_too(store, tmp_path):
    session, _ = store.start(title="A", analyst="a")
    collector = FileCollector(session, roots=[tmp_path])

    event = collector._describe(tmp_path / "SJALL018" / "gone.bam")

    assert event.payload["op"] == "deleted"
    assert "SJALL018" not in event.payload["path"]
    assert "sj_id" in event.redactions


def test_diff_patches_are_redacted_before_they_touch_disk(store, tmp_path):
    """A diff hunk is raw file content -- the highest-risk thing this collector
    writes, and it went to disk verbatim."""

    import subprocess

    session, _ = store.start(title="A", analyst="a")
    root = tmp_path / "repo"
    root.mkdir()
    for args in (
        ["init", "-q"],
        ["config", "user.email", "t@example.org"],
        ["config", "user.name", "t"],
    ):
        subprocess.run(["git", *args], cwd=root, check=True, capture_output=True)
    tracked = root / "manifest.tsv"
    tracked.write_text("id\tnote\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=root, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=root, check=True, capture_output=True)
    tracked.write_text("id\tnote\nSJ-4817\tMRN 4419902 jane.smith@example.org\n", encoding="utf-8")

    collector = FileCollector(session, roots=[root])
    collector._git_snapshot(root, trigger="test", force=True)

    patches = sorted((session.root / "files" / "diffs").glob("*.patch"))
    assert patches, "no patch was written"
    blob = "\n".join(path.read_text(encoding="utf-8") for path in patches)

    assert "4419902" not in blob
    assert "jane.smith@example.org" not in blob
    assert "SJ-4817" not in blob
    # The diff structure survives, so the patch is still readable as a patch.
    assert "+++" in blob or "---" in blob


def test_a_diff_filename_cannot_reintroduce_the_identifier(store, tmp_path):
    """The staged filename is derived from the relative path.

    Redacting the *content* while naming the file after the unredacted path
    would put the identifier straight back on disk, in a place `ls` shows.
    """

    import subprocess

    session, _ = store.start(title="A", analyst="a")
    root = tmp_path / "repo2"
    root.mkdir()
    for args in (
        ["init", "-q"],
        ["config", "user.email", "t@example.org"],
        ["config", "user.name", "t"],
    ):
        subprocess.run(["git", *args], cwd=root, check=True, capture_output=True)
    tracked = root / "SJ-4817_notes.txt"
    tracked.write_text("one\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=root, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=root, check=True, capture_output=True)
    tracked.write_text("one\ntwo\n", encoding="utf-8")

    collector = FileCollector(session, roots=[root])
    collector._git_snapshot(root, trigger="test", force=True)

    names = [path.name for path in (session.root / "files" / "diffs").glob("*.patch")]
    assert names
    assert all("SJ-4817" not in name for name in names), names


def test_git_snapshot_redacts_root_branch_and_reports_findings(store, tmp_path):
    import subprocess

    session, _ = store.start(title="A", analyst="a")
    root = tmp_path / "repo3"
    root.mkdir()
    for args in (
        ["init", "-q"],
        ["config", "user.email", "t@example.org"],
        ["config", "user.name", "t"],
    ):
        subprocess.run(["git", *args], cwd=root, check=True, capture_output=True)
    subprocess.run(
        ["git", "checkout", "-qb", "SJ-4817-branch"], cwd=root, check=True, capture_output=True
    )
    tracked = root / "SJ-4817.txt"
    tracked.write_text("one\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=root, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=root, check=True, capture_output=True)
    tracked.write_text("two\n", encoding="utf-8")

    collector = FileCollector(session, roots=[root])
    collector._git_snapshot(root, trigger="test", force=True)

    event = [item for item in session.writer.read() if item.type == "git.snapshot"][-1]
    assert "SJ-4817" not in json.dumps(event.payload)
    assert "sj_id" in event.redactions
