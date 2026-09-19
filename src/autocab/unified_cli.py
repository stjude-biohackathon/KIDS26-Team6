"""Unified command line for recording and reviewed skill creation."""

from __future__ import annotations

import json
from pathlib import Path
import sys
from typing import Any

import click

from autocab.deid.config import ConfigRejected, load as load_deid_config
from autocab.deid.models import ModelWeightsError, verify_weights
from autocab.initialization import (
    REDACTION_ENGINES,
    initialize,
    install_shell_hooks,
    legacy_session_count,
    migrate_legacy_sessions,
    prepare_model,
    readiness_summary,
)
from autocab.migration import migrate_wfrec_sessions
from autocab.orchestrator import run_pipeline
from autocab.terminal_logs import convert_terminal_log, write_trace_json
from autocab.workflow import ForgeWorkflow, RunStore, WorkflowError
from autocab.recording.recorder import Recorder
from autocab.recording.seal import SealError
from autocab.recording.seal_service import apply_phi_redaction
from autocab.recording.session import SessionNotFound, SessionStore
from autocab.recording.state import resolved_default_analyst


def _emit(payload: Any, *, as_json: bool = False) -> None:
    """Render structured results consistently for people and scripts."""

    if as_json:
        click.echo(json.dumps(payload, indent=2, default=str))
        return
    for label, value in payload.items():
        click.echo(f"{label.replace('_', ' ').title()}: {value}")


def _run_recorder(arguments: list[str]) -> None:
    """Use the recorder's established daemon-aware command implementation."""

    from autocab.recording.cli import main as recorder_main

    result = recorder_main(arguments)
    if result:
        raise click.exceptions.Exit(result)


@click.group(help="Record workflows and turn reviewed sessions into reusable skills.")
@click.version_option(package_name="autocab")
def cli() -> None:
    """AutoCAB's unified workflow command group."""


@cli.command("init")
@click.option("--analyst", default="", help="Default analyst name or identifier.")
@click.option(
    "--redaction",
    "redaction_engine",
    type=click.Choice(REDACTION_ENGINES),
    help="Default session redaction engine.",
)
@click.option("--fetch-model", is_flag=True, help="Download and verify selected model weights.")
@click.option("--install-hooks", is_flag=True, help="Install auto-detected shell capture hooks.")
@click.option("--migrate-legacy", is_flag=True, help="Copy legacy recording sessions.")
@click.option("--check", "run_checks", is_flag=True, help="Run recorder readiness checks.")
@click.option(
    "--interactive/--no-interactive",
    default=None,
    help="Prompt for setup choices. Defaults to prompts in an interactive terminal.",
)
@click.option("--json", "as_json", is_flag=True, help="Emit machine-readable JSON.")
def init_command(
    analyst: str,
    redaction_engine: str | None,
    fetch_model: bool,
    install_hooks: bool,
    migrate_legacy: bool,
    run_checks: bool,
    interactive: bool | None,
    as_json: bool,
) -> None:
    """Create and optionally configure a local AutoCAB workspace."""

    try:
        current_engine = load_deid_config().engine
        current_engine = current_engine if current_engine in REDACTION_ENGINES else "regex"
        explicit_setup = any(
            (
                analyst,
                redaction_engine,
                fetch_model,
                install_hooks,
                migrate_legacy,
                run_checks,
            )
        )
        guided = (
            interactive
            if interactive is not None
            else sys.stdin.isatty() and not explicit_setup and not as_json
        )
        if as_json and guided:
            raise click.UsageError("Use --no-interactive with --json.")

        if guided:
            analyst = click.prompt("Analyst name", default=analyst or resolved_default_analyst())
            redaction_engine = click.prompt(
                "PHI redaction",
                type=click.Choice(REDACTION_ENGINES),
                default=current_engine,
                show_choices=True,
            )
            if redaction_engine != "regex" and not verify_weights(model=redaction_engine).valid:
                model_name = "GLiNER2 PII" if redaction_engine == "gliner2-pii" else "GLiNER"
                model_size = (
                    "about 1.25 GB" if redaction_engine == "gliner2-pii" else "about 192 MB"
                )
                fetch_model = click.confirm(
                    f"Download and verify the local {model_name} model ({model_size})?",
                    default=False,
                )
                if not fetch_model:
                    click.echo(f"Keeping regex redaction because {model_name} is not installed.")
                    redaction_engine = "regex"
            install_hooks = click.confirm(
                "Install shell capture hooks? This updates your shell profile.",
                default=False,
            )
            if legacy_session_count():
                migrate_legacy = click.confirm(
                    "Copy legacy recording sessions into AutoCAB?",
                    default=False,
                )
            run_checks = click.confirm("Run readiness checks?", default=True)

        selected_engine = redaction_engine or current_engine
        model_status = (
            prepare_model(selected_engine, fetch=fetch_model)
            if redaction_engine or fetch_model
            else None
        )
        result = initialize(analyst=analyst, redaction_engine=redaction_engine)
        hooks = install_shell_hooks() if install_hooks else []
        migration = migrate_legacy_sessions() if migrate_legacy else None
        readiness = readiness_summary() if run_checks else None
    except click.ClickException:
        raise
    except (ConfigRejected, ModelWeightsError, OSError, ValueError) as exc:
        raise click.ClickException(f"Could not initialize AutoCAB: {exc}") from exc

    payload = result.to_dict()
    payload.update(
        {
            "model": model_status.to_dict() if model_status else None,
            "hooks": hooks,
            "migration": migration,
            "readiness": readiness,
        }
    )
    if as_json:
        _emit(payload, as_json=True)
        return
    click.echo(f"AutoCAB home: {result.home}")
    click.echo(f"Configuration: {result.config}")
    click.echo(f"Analyst: {result.analyst}")
    click.echo(f"PHI redaction: {result.redaction_engine}")
    if model_status:
        click.echo(f"Redaction model: verified at {model_status.path}")
    if hooks:
        click.echo(f"Shell hooks: {len(hooks)} configuration change(s)")
    if migration:
        click.echo(f"Migrated sessions: {len(migration['copied'])}")
    if readiness:
        click.echo(
            "Readiness: "
            f"{readiness['available_sources']}/{readiness['total_sources']} capture sources available"
        )
        click.echo(f"Redaction ready: {'yes' if readiness['redaction_ready'] else 'no'}")
        click.echo(f"Readiness warnings: {len(readiness['warnings'])}")
    if result.legacy_sessions:
        click.echo(f"Legacy recording sessions found: {result.legacy_sessions}")
    click.echo("Next:")
    for command in result.next_commands:
        click.echo(f"  {command}")


@cli.command("dashboard")
@click.option("--port", type=int, help="Port for the dashboard server.")
@click.option("--host", help="Listen address. Defaults to loopback.")
@click.option("--bind-all", is_flag=True, help="Listen on all interfaces.")
@click.option("--advertise-url", help="Browser-facing base URL for this server.")
@click.option(
    "--allow-remote",
    is_flag=True,
    help="Allow a non-loopback bind. Required with --bind-all.",
)
@click.option(
    "--open/--no-open",
    "open_dashboard",
    default=True,
    help="Open the dashboard after the server starts.",
)
@click.option("--stop", is_flag=True, help="Stop the running dashboard server.")
@click.option(
    "--no-indicator",
    is_flag=True,
    help="Do not show the menu-bar or tray recording indicator.",
)
def dashboard_command(
    port: int | None,
    host: str | None,
    bind_all: bool,
    advertise_url: str | None,
    allow_remote: bool,
    open_dashboard: bool,
    stop: bool,
    no_indicator: bool,
) -> None:
    """Start the AutoCAB dashboard and recording service."""

    arguments = ["daemon"]
    if port is not None:
        arguments.extend(["--port", str(port)])
    if host:
        arguments.extend(["--host", host])
    if bind_all:
        arguments.append("--bind-all")
    if advertise_url:
        arguments.extend(["--advertise-url", advertise_url])
    if allow_remote:
        arguments.append("--allow-remote")
    if stop:
        arguments.append("--stop")
    elif open_dashboard:
        arguments.append("--gui")
    if no_indicator:
        arguments.append("--no-indicator")
    _run_recorder(arguments)


@cli.group(help="Start, annotate, pause, resume, or finish a recording session.")
def record() -> None:
    """Manage recording through AutoCAB."""


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
    _run_recorder(arguments)


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
    _run_recorder(arguments)


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
    _run_recorder(arguments)


@record.command("resume")
@click.argument("session_id", required=False)
def record_resume(session_id: str | None) -> None:
    """Resume a paused recording session."""

    _run_recorder(["resume", *([session_id] if session_id else [])])


@record.command("finish")
@click.argument("session_id", required=False)
@click.option("--seal", is_flag=True, help="Apply PHI redaction and seal after stopping.")
@click.option(
    "--engine",
    type=click.Choice(REDACTION_ENGINES),
    help="PHI detector tier. Defaults to the engine selected during init.",
)
def record_finish(session_id: str | None, seal: bool, engine: str | None) -> None:
    """Stop a session permanently and optionally seal it."""

    resolved = SessionStore().resolve(session_id)
    _run_recorder(["stop", resolved.session_id])
    if seal:
        _apply_redaction(resolved.session_id, engine)


@record.command("redact")
@click.argument("session_id", required=False)
@click.option(
    "--engine",
    type=click.Choice(REDACTION_ENGINES),
    help="PHI detector tier. Defaults to the engine selected during init.",
)
def record_redact(session_id: str | None, engine: str | None) -> None:
    """Apply PHI redaction and seal an archived session."""

    resolved = SessionStore().resolve(session_id)
    _apply_redaction(resolved.session_id, engine)


def _apply_redaction(session_id: str, engine: str | None) -> None:
    """Apply configured redaction without silently weakening the selected engine."""

    from autocab.deid.engines.base import EngineUnavailable

    try:
        config = load_deid_config()
    except ConfigRejected as exc:
        raise click.ClickException(f"PHI redaction configuration is invalid: {exc}") from exc
    selected_engine = engine or config.engine
    if selected_engine not in REDACTION_ENGINES:
        raise click.ClickException(
            f"Configured redaction engine {selected_engine!r} is unsupported. "
            f"Choose one of: {', '.join(REDACTION_ENGINES)}."
        )
    try:
        result = apply_phi_redaction(
            SessionStore().resolve(session_id),
            engine=selected_engine,
            profile=config.profile,
        )
    except EngineUnavailable as exc:
        raise click.ClickException(f"PHI redaction is unavailable: {exc}") from exc
    click.echo(f"PHI redaction applied: {session_id}")
    click.echo(f"Engine: {result.record['engine']}")
    click.echo(f"Findings: {result.record['findings']}")


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
@click.option("--legacy", is_flag=True, help="Copy sessions from legacy recorder storage.")
@click.option(
    "--source",
    type=click.Path(path_type=Path, file_okay=False),
    help="Legacy storage root. Uses the discovered compatibility location by default.",
)
@click.option("--json", "as_json", is_flag=True, help="Emit machine-readable JSON.")
def migrate_command(legacy: bool, source: Path | None, as_json: bool) -> None:
    """Copy legacy sessions into canonical AutoCAB storage."""

    if not legacy:
        raise click.UsageError("Choose a migration source, for example --legacy.")
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
