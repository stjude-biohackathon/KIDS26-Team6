"""Unified command line for recording and reviewed skill creation."""

from __future__ import annotations

import json
from pathlib import Path
import sys
from typing import Any

import click

from autocab.initialization import initialize
from autocab.migration import migrate_wfrec_sessions
from autocab.orchestrator import run_pipeline
from autocab.terminal_logs import convert_terminal_log, write_trace_json
from autocab.workflow import ForgeWorkflow, RunStore, WorkflowError
from wfrec.recorder import Recorder
from wfrec.seal import SealError
from wfrec.session import SessionNotFound, SessionStore


def _emit(payload: Any, *, as_json: bool = False) -> None:
    """Render structured results consistently for people and scripts."""

    if as_json:
        click.echo(json.dumps(payload, indent=2, default=str))
        return
    for label, value in payload.items():
        click.echo(f"{label.replace('_', ' ').title()}: {value}")


def _run_wfrec(arguments: list[str]) -> None:
    """Use the recorder's established daemon-aware command implementation."""

    from wfrec.cli import main as wfrec_main

    result = wfrec_main(arguments)
    if result:
        raise click.exceptions.Exit(result)


@click.group(help="Record workflows and turn reviewed sessions into reusable skills.")
@click.version_option(package_name="autocab")
def cli() -> None:
    """AutoCAB's unified workflow command group."""


@cli.command("init")
@click.option("--analyst", default="", help="Default analyst name or identifier.")
@click.option("--json", "as_json", is_flag=True, help="Emit machine-readable JSON.")
def init_command(analyst: str, as_json: bool) -> None:
    """Create a local AutoCAB workspace and safe default configuration."""

    try:
        result = initialize(analyst=analyst)
    except OSError as exc:
        raise click.ClickException(f"Could not initialize AutoCAB: {exc}") from exc
    if as_json:
        _emit(result.to_dict(), as_json=True)
        return
    click.echo(f"AutoCAB home: {result.home}")
    click.echo(f"Configuration: {result.config}")
    click.echo(f"Analyst: {result.analyst}")
    if result.legacy_sessions:
        click.echo(f"Legacy wfrec sessions found: {result.legacy_sessions}")
    click.echo("Next:")
    for command in result.next_commands:
        click.echo(f"  {command}")


@cli.group(help="Start, annotate, pause, resume, or finish a recording session.")
def record() -> None:
    """Manage wfrec recording through AutoCAB."""


@record.command("start")
@click.option("--title", default="", help="What you are working on.")
@click.option("--analyst", default="", help="Analyst name or identifier.")
@click.option("--workflow-family", default="", help="Workflow grouping label.")
@click.option("--tag", "tags", multiple=True, help="Session tag. Repeatable.")
@click.option(
    "--watch",
    "watch_roots",
    multiple=True,
    type=click.Path(path_type=Path),
    help="Project root to watch. Repeatable.",
)
@click.option("--no-daemon", is_flag=True, help="Do not start the recording daemon.")
def record_start(
    title: str,
    analyst: str,
    workflow_family: str,
    tags: tuple[str, ...],
    watch_roots: tuple[Path, ...],
    no_daemon: bool,
) -> None:
    """Start a recording session."""

    arguments = ["start", "--title", title]
    if analyst:
        arguments.extend(["--analyst", analyst])
    if workflow_family:
        arguments.extend(["--workflow-family", workflow_family])
    for tag in tags:
        arguments.extend(["--tag", tag])
    for root in watch_roots:
        arguments.extend(["--watch", str(root)])
    if no_daemon:
        arguments.append("--no-daemon")
    _run_wfrec(arguments)


@record.command("note")
@click.argument("text", required=False)
@click.option("--label", default="", help="Short note label.")
def record_note(text: str | None, label: str) -> None:
    """Add context to the active session timeline."""

    arguments = ["note"]
    if text is not None:
        arguments.append(text)
    if label:
        arguments.extend(["--label", label])
    _run_wfrec(arguments)


@record.command("pause")
@click.argument("session_id", required=False)
@click.option("--reason", default="", help="Why recording is paused.")
@click.option("--expect", default="", help="Expected pause length, such as 6h.")
def record_pause(session_id: str | None, reason: str, expect: str) -> None:
    """Pause a recording session."""

    arguments = ["pause"]
    if session_id:
        arguments.append(session_id)
    if reason:
        arguments.extend(["--reason", reason])
    if expect:
        arguments.extend(["--expect", expect])
    _run_wfrec(arguments)


@record.command("resume")
@click.argument("session_id", required=False)
def record_resume(session_id: str | None) -> None:
    """Resume a paused recording session."""

    _run_wfrec(["resume", *([session_id] if session_id else [])])


@record.command("finish")
@click.argument("session_id", required=False)
@click.option("--seal", is_flag=True, help="Apply PHI redaction and seal after stopping.")
@click.option("--engine", default="regex", help="PHI detector tier used when sealing.")
def record_finish(session_id: str | None, seal: bool, engine: str) -> None:
    """Stop a session permanently and optionally seal it."""

    resolved = SessionStore().resolve(session_id)
    _run_wfrec(["stop", resolved.session_id])
    if seal:
        _run_wfrec(["seal", resolved.session_id, "--engine", engine])


@cli.command("forge")
@click.option("--session", "session_id", required=True, help="Sealed session ID.")
@click.option("--json", "as_json", is_flag=True, help="Emit machine-readable JSON.")
def forge_command(session_id: str, as_json: bool) -> None:
    """Create an evidence-linked, blocked skill draft from a sealed session."""

    run = ForgeWorkflow().forge(session_id)
    _emit(run.to_dict(), as_json=as_json)


@cli.command("review")
@click.argument("run_id")
@click.option("--reviewer", required=True, help="Person performing the review.")
@click.option("--notes", default="", help="Review notes.")
@click.option(
    "--spec",
    "spec_path",
    type=click.Path(path_type=Path, exists=True, dir_okay=False),
    help="Edited SkillSpec to validate and store.",
)
@click.option("--json", "as_json", is_flag=True, help="Emit machine-readable JSON.")
def review_command(
    run_id: str,
    reviewer: str,
    notes: str,
    spec_path: Path | None,
    as_json: bool,
) -> None:
    """Validate reviewer edits and report any remaining blockers."""

    run = ForgeWorkflow().review(
        run_id,
        reviewer=reviewer,
        notes=notes,
        spec_path=spec_path,
    )
    _emit(run.to_dict(), as_json=as_json)


@cli.command("approve")
@click.argument("run_id")
@click.option("--reviewer", required=True, help="Person granting approval.")
@click.option("--notes", default="", help="Approval notes.")
@click.option("--json", "as_json", is_flag=True, help="Emit machine-readable JSON.")
def approve_command(run_id: str, reviewer: str, notes: str, as_json: bool) -> None:
    """Explicitly approve a reviewed run for packaging."""

    run = ForgeWorkflow().approve(run_id, reviewer=reviewer, notes=notes)
    _emit(run.to_dict(), as_json=as_json)


@cli.command("package")
@click.argument("run_id")
@click.option("--json", "as_json", is_flag=True, help="Emit machine-readable JSON.")
def package_command(run_id: str, as_json: bool) -> None:
    """Render and strictly validate an approved skill package."""

    run = ForgeWorkflow().package(run_id)
    _emit(run.to_dict(), as_json=as_json)


@cli.command("status")
@click.option("--json", "as_json", is_flag=True, help="Emit machine-readable JSON.")
def status_command(as_json: bool) -> None:
    """Show recording state and recent forge runs."""

    recorder = Recorder(supervise=False)
    recording = recorder.status()
    recorder.shutdown()
    runs = [run.to_dict() for run in RunStore().list()[:10]]
    if as_json:
        _emit({"recording": recording, "forge_runs": runs}, as_json=True)
        return
    session = recording.get("session") or {}
    click.echo(f"Recording: {session.get('status', 'none')}")
    if session:
        click.echo(f"Session: {session.get('id')}")
    click.echo(f"Forge runs: {len(runs)}")
    for run in runs:
        click.echo(f"  {run['run_id']}  {run['state']}  {', '.join(run['session_ids'])}")


@cli.command("migrate")
@click.option("--from-wfrec", is_flag=True, help="Copy legacy ~/.wfrec sessions.")
@click.option(
    "--source",
    type=click.Path(path_type=Path, file_okay=False),
    help="Legacy root. Defaults to ~/.wfrec.",
)
@click.option("--json", "as_json", is_flag=True, help="Emit machine-readable JSON.")
def migrate_command(from_wfrec: bool, source: Path | None, as_json: bool) -> None:
    """Copy legacy sessions into canonical AutoCAB storage."""

    if not from_wfrec:
        raise click.UsageError("Choose a migration source, for example --from-wfrec.")
    result = migrate_wfrec_sessions(source_root=source or Path.home() / ".wfrec")
    _emit(result.to_dict(), as_json=as_json)


@cli.command("demo")
@click.option(
    "--output-dir",
    type=click.Path(path_type=Path),
    default=Path("skills/generated-drafts"),
    show_default=True,
)
@click.option("--reviewer", default="CAB Maintainer", show_default=True)
@click.option("--approve", is_flag=True, help="Explicitly approve and export proposals.")
@click.option(
    "--input-mode",
    type=click.Choice(("trace", "screen-capture", "terminal-log", "session")),
    default="trace",
    show_default=True,
)
@click.option("--trace-file", type=click.Path(path_type=Path, dir_okay=False))
@click.option("--capture-file", type=click.Path(path_type=Path, dir_okay=False))
@click.option("--log-file", type=click.Path(path_type=Path, dir_okay=False))
@click.option("--session-dir", type=click.Path(path_type=Path))
def demo_command(
    output_dir: Path,
    reviewer: str,
    approve: bool,
    input_mode: str,
    trace_file: Path | None,
    capture_file: Path | None,
    log_file: Path | None,
    session_dir: Path | None,
) -> None:
    """Run the legacy demonstration pipeline without automatic approval."""

    proposals = run_pipeline(
        output_dir=output_dir,
        input_mode=input_mode,
        trace_path=trace_file,
        capture_path=capture_file,
        log_path=log_file,
        session_path=session_dir,
        reviewer=reviewer,
        approve=approve,
    )
    click.echo(json.dumps([proposal.to_dict() for proposal in proposals], indent=2))


@cli.command("ingest-terminal-log")
@click.argument("log_file", type=click.Path(path_type=Path, exists=True, dir_okay=False))
@click.option(
    "--output",
    type=click.Path(path_type=Path, dir_okay=False),
    default=Path("data/generated_terminal_trace.json"),
    show_default=True,
)
def ingest_command(log_file: Path, output: Path) -> None:
    """Convert a timestamped terminal log through the legacy adapter."""

    trace = convert_terminal_log(log_file)
    write_trace_json(trace, output)
    click.echo(json.dumps([trace.to_dict()], indent=2))


@cli.command(
    "deid",
    context_settings={
        "ignore_unknown_options": True,
        "allow_extra_args": True,
        "help_option_names": [],
    },
)
@click.argument("arguments", nargs=-1, type=click.UNPROCESSED)
def deid_command(arguments: tuple[str, ...]) -> None:
    """Run the de-identification evaluation and benchmark commands."""

    from autocab.cli import legacy_main

    result = legacy_main(["deid", *arguments])
    if result:
        raise click.exceptions.Exit(result)


def run(argv: list[str] | None = None) -> int:
    """Run Click without allowing it to terminate library callers."""

    try:
        result = cli.main(args=argv, prog_name="autocab", standalone_mode=False)
    except click.ClickException as exc:
        exc.show(file=sys.stderr)
        return exc.exit_code
    except click.exceptions.Exit as exc:
        return exc.exit_code
    except (WorkflowError, SessionNotFound, SealError, ValueError) as exc:
        click.echo(f"autocab: {exc}", err=True)
        return 1
    return int(result or 0)
