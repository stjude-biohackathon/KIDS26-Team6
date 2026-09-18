"""`wfrec` command line interface.

Every subcommand is a thin client of the control API, so the CLI, the GUI and
the ``recorder.md`` agent skill share one implementation. When no daemon is
running, lifecycle commands fall back to acting on the store directly and say
so, because a recorder that refuses to work without its daemon is a bad tool.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from rich.text import Text

from autocab.deid.engines.registry import ENGINE_CHOICES
from autocab.deid.policy import PROFILES

from . import SOURCES, __version__, paths
from .client import Client, DaemonUnavailable
from .events import Event
from .output import (
    console,
    data_table,
    error,
    plain_text,
    status_table,
    status_text,
    success,
    summary,
    warning,
)
from .recorder import NoActiveSession, Recorder
from .session import SessionNotFound, SessionStore


def _emit(payload: Any, as_json: bool) -> None:
    if as_json:
        sys.stdout.write(json.dumps(payload, indent=2, default=str) + "\n")


def _bool_arg(value: str) -> bool:
    lowered = value.strip().lower()
    if lowered in {"on", "true", "yes", "1", "enable", "enabled", "start"}:
        return True
    if lowered in {"off", "false", "no", "0", "disable", "disabled", "stop"}:
        return False
    raise argparse.ArgumentTypeError(f"Expected on/off, got '{value}'")


# --------------------------------------------------------------------- parser
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="wfrec",
        description="Record a bioinformatics workflow session for AutoCAB.",
    )
    parser.add_argument("--version", action="version", version=f"wfrec {__version__}")
    parser.add_argument(
        "--json", action="store_true", help="Emit machine-readable JSON."
    )

    # Shared so `--json` works on either side of the subcommand. Requiring it
    # before the verb is the kind of papercut that makes a CLI feel hostile,
    # and the agent skill composes these strings without knowing the rule.
    common = argparse.ArgumentParser(add_help=False)
    # default=SUPPRESS matters: with a normal False default the subparser writes
    # json=False onto the shared namespace and silently overrides a --json
    # given before the verb.
    common.add_argument(
        "--json", action="store_true", default=argparse.SUPPRESS, help=argparse.SUPPRESS
    )

    sub = parser.add_subparsers(dest="command", required=True, parser_class=lambda **kw: argparse.ArgumentParser(parents=[common], **kw))

    start = sub.add_parser("start", help="Start a new recording session.")
    start.add_argument("--title", default="", help="What you are working on.")
    start.add_argument("--analyst", default="", help="Analyst id for clustering.")
    start.add_argument("--workflow-family", default="", help="Group label for matching.")
    start.add_argument("--tag", action="append", default=[], help="Repeatable tag.")
    start.add_argument(
        "--watch", action="append", default=[], type=Path,
        help="Project root to watch for file changes. Repeatable.",
    )
    start.add_argument(
        "--without", action="append", default=[], choices=list(SOURCES),
        help="Start with a source disabled. Repeatable.",
    )
    start.add_argument(
        "--no-daemon", action="store_true",
        help="Do not auto-start the background daemon (shell capture still works).",
    )

    pause = sub.add_parser("pause", help="Pause capture (e.g. while a job runs).")
    pause.add_argument("session_id", nargs="?")
    pause.add_argument("--reason", default="", help="Why you are pausing.")
    pause.add_argument("--expect", default="", help="Expected wait, e.g. 6h.")

    resume = sub.add_parser("resume", help="Resume a paused session.")
    resume.add_argument("session_id", nargs="?")

    stop = sub.add_parser("stop", help="Stop a session permanently.")
    stop.add_argument("session_id", nargs="?")

    source = sub.add_parser("source", help="Toggle one capture source.")
    source.add_argument("name", choices=list(SOURCES))
    source.add_argument("state", type=_bool_arg, help="on or off")

    output = sub.add_parser(
        "shell-output", help="Report or clear unsupported shell output capture."
    )
    output.add_argument("state", type=_bool_arg, help="on or off")

    note = sub.add_parser("note", help="Add pasted context or a note.")
    note.add_argument("text", nargs="?", help="Text, or omit to read stdin.")
    note.add_argument("--label", default="", help="Short label for the note.")

    mark = sub.add_parser("mark", help="Drop a labelled marker on the timeline.")
    mark.add_argument("label")
    mark.add_argument("--detail", default="")

    watch = sub.add_parser("watch", help="Declare a project root to watch.")
    watch.add_argument("root", type=Path)

    sub.add_parser("status", help="Show what is being recorded right now.")
    sub.add_parser("doctor", help="Report resolved backends and why.")
    sub.add_parser("sessions", help="List all sessions.")

    events = sub.add_parser("events", help="Print timeline events.")
    events.add_argument("session_id", nargs="?")
    events.add_argument("--limit", type=int, default=40)
    events.add_argument("--source", default="", help="Filter by source.")
    events.add_argument("--type", default="", help="Filter by event type prefix.")
    events.add_argument("--role", default="", help="Filter agent messages by role.")
    events.add_argument("--tool", default="", help="Filter agent messages by tool.")

    export = sub.add_parser("export", help="Export a session for AutoCAB.")
    export.add_argument("session_id", nargs="?")
    export.add_argument(
        "--format", action="append", default=[],
        choices=["autocab", "terminal-log", "screen-capture", "trace"],
        help="Repeatable. Defaults to autocab (both text formats).",
    )

    merge = sub.add_parser(
        "merge", help="Combine several sessions into one multi-analyst trace file."
    )
    merge.add_argument("session_ids", nargs="+")
    merge.add_argument("--output", type=Path, required=True)
    merge.add_argument(
        "--workflow-family", default="",
        help="Force a shared family so different titles still cluster together.",
    )

    attach = sub.add_parser(
        "attach-transcript", help="Fold an agent transcript file into the timeline."
    )
    attach.add_argument("path", type=Path)
    attach.add_argument("--tool", default="manual")

    hooks = sub.add_parser("hooks", help="Install or remove shell hooks.")
    hooks.add_argument(
        "action", choices=["install", "uninstall", "status", "eval"]
    )
    hooks.add_argument(
        "--shell", action="append", default=[],
        choices=["bash", "zsh", "fish", "powershell"],
        help="Repeatable. Auto-detected when omitted.",
    )

    daemon = sub.add_parser("daemon", help="Run the recorder daemon.")
    daemon.add_argument("--port", type=int, default=None)
    daemon.add_argument(
        "--host",
        default=None,
        help="Listen address (default 127.0.0.1, or WFREC_BIND_HOST).",
    )
    daemon.add_argument(
        "--bind-all",
        action="store_true",
        help="Listen on all interfaces (0.0.0.0). Requires --allow-remote.",
    )
    daemon.add_argument(
        "--advertise-url",
        default=None,
        help="Base URL for browsers and wfrec gui (WFREC_ADVERTISE_URL).",
    )
    daemon.add_argument(
        "--allow-remote",
        action="store_true",
        help="Opt in to non-loopback bind (or set WFREC_ALLOW_REMOTE=1).",
    )
    daemon.add_argument("--gui", action="store_true", help="Open the UI on start.")
    daemon.add_argument(
        "--stop", action="store_true",
        help="Stop a running daemon (needed on macOS after granting Screen Recording).",
    )
    daemon.add_argument(
        "--no-indicator", action="store_true",
        help="Don't show the menu-bar/tray recording indicator.",
    )

    sub.add_parser("gui", help="Open the control window.")

    ssh = sub.add_parser("ssh", help="Open a recorded shell on a remote host.")
    ssh.add_argument("host")
    ssh.add_argument("ssh_args", nargs=argparse.REMAINDER)

    pull = sub.add_parser("pull", help="Pull remote spool and job output back.")
    pull.add_argument("host")
    pull.add_argument("--job", action="append", default=[], help="SLURM job id.")

    seal = sub.add_parser(
        "seal",
        help="De-identify a stopped session in place. Required before export.",
        description=(
            "Detect identifiers across the whole session, rewrite them "
            "destructively to per-run surrogates, and write seal.json. "
            "Irreversible by design: no reverse map is kept and the key is "
            "discarded. See docs/deid-evaluation.md for measured recall and, "
            "more importantly, for what the numbers do not prove."
        ),
    )
    seal.add_argument("session_id", nargs="?")
    seal.add_argument(
        "--engine",
        default="regex",
        choices=list(ENGINE_CHOICES),
        help="Model tier. surrogate_guard and regex_rules always run regardless.",
    )
    seal.add_argument("--profile", default="balanced", choices=list(PROFILES))
    seal.add_argument(
        "--reseal",
        action="store_true",
        help="Re-run over an already-sealed session, minting generation-2 "
        "surrogates only for what the first pass missed.",
    )
    seal.add_argument(
        "--force",
        action="store_true",
        help="Stop the session first, then seal. Without this an active session "
        "is refused rather than raced.",
    )
    seal.add_argument("--status", action="store_true", help="Report seal state and exit.")
    seal.add_argument(
        "--dry-run",
        action="store_true",
        help="Detect and report, stage nothing. The session is untouched.",
    )
    seal.add_argument(
        "--pseudonymize-analyst",
        action="store_true",
        help="Also pseudonymize analyst/host. Off by default: they identify the "
        "workforce, not the PHI subject, and WorkflowClusterer groups on analyst.",
    )
    seal.add_argument(
        "--deny-term",
        action="append",
        default=[],
        help="Repeatable. Adds to the default deny vocabulary.",
    )

    deid = sub.add_parser(
        "deid", help="De-identification model weights and provider status."
    )
    deid_verbs = deid.add_subparsers(dest="deid_command", required=True)
    fetch = deid_verbs.add_parser(
        "fetch", help="Download and verify the pinned model weights."
    )
    fetch.add_argument("--bundle", type=Path, default=None, help="Write an air-gap bundle instead.")
    load_weights = deid_verbs.add_parser("load", help="Install an air-gap bundle.")
    load_weights.add_argument("bundle", type=Path)
    deid_verbs.add_parser("verify", help="Re-verify every weight file's sha256.")
    deid_verbs.add_parser(
        "providers", help="List LLM providers with their resolved egress class."
    )

    return parser


# ------------------------------------------------------------------ dispatch
def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    as_json = args.json

    try:
        return _dispatch(args, as_json)
    except NoActiveSession as exc:
        error(exc)
        return 2
    except SessionNotFound as exc:
        error(exc)
        return 3
    except DaemonUnavailable as exc:
        error(exc)
        return 4
    except (ValueError, RuntimeError) as exc:
        error(exc)
        return 1


def _dispatch(args: argparse.Namespace, as_json: bool) -> int:
    command = args.command

    if command == "daemon":
        from .daemon import run, stop

        if args.stop:
            result = stop()
            if as_json:
                _emit(result, True)
            elif result["stopped"]:
                summary("Stopped daemon", [("PID", result["pid"])])
            else:
                warning(f"No daemon stopped: {result['reason']}")
            return 0 if result["stopped"] else 1
        return run(
            port=args.port,
            host=args.host,
            bind_all=args.bind_all,
            advertise_url=args.advertise_url,
            allow_remote=args.allow_remote,
            open_gui=args.gui,
            show_indicator=not args.no_indicator,
        )

    if command == "gui":
        from .daemon import launch_gui

        client = Client.discover(require=True)
        launch_gui(client.url, client.token)
        return 0

    if command == "doctor":
        from .doctor import diagnose, render

        report = diagnose()
        if as_json:
            _emit(report, True)
        else:
            console.print(render(report))
        return 0

    if command == "hooks":
        return _hooks(args, as_json)

    if command == "sessions":
        return _sessions(as_json)

    if command == "events":
        return _events(args, as_json)

    if command == "merge":
        from .merge import merge_sessions

        result = merge_sessions(
            args.session_ids,
            args.output,
            workflow_family=args.workflow_family or None,
        )
        if as_json:
            _emit(result, True)
        else:
            summary(
                "Merged sessions",
                [
                    ("Traces", result["traces"]),
                    ("Output", result["path"]),
                    ("Analysts", ", ".join(result["analysts"]) or "None"),
                ],
            )
            families = data_table("Workflow family", "Analysts", "Coverage")
            for family, people in result["workflow_families"].items():
                coverage = (
                    Text("Shared", style="green")
                    if len(people) > 1
                    else Text("Single analyst", style="dim")
                )
                families.add_row(
                    plain_text(family),
                    plain_text(", ".join(people) or "None"),
                    coverage,
                )
            console.print(Text("\nWorkflow families", style="bold"), families)
            for entry in result["skipped"]:
                warning(f"Skipped {entry['session']}: {entry['error']}")
        return 0

    if command == "ssh":
        return _ssh(args, as_json)

    if command == "pull":
        return _pull(args, as_json)

    if command == "attach-transcript":
        from .collectors.agents import attach_transcript

        session = SessionStore().resolve(None)
        events = attach_transcript(session, args.path, tool=args.tool)
        result = {"session": session.session_id, "turns": len(events)}
        if as_json:
            _emit(result, True)
        else:
            summary(
                "Attached transcript",
                [
                    ("Turns", len(events)),
                    ("Source", args.path),
                    ("Session", session.session_id),
                ],
            )
        return 0

    if command == "export":
        return _export(args, as_json)

    # `seal` and `deid` are routed here, **before** the daemon-preferring block
    # below, exactly like `export`. The seal and any weight fetch run in the
    # foreground CLI and never in the daemon: both can take minutes, both may
    # need an interactive tty (a progress bar, a typed confirmation), and a
    # daemon has neither.
    if command == "seal":
        return _seal(args, as_json)

    if command == "deid":
        return _deid(args, as_json)

    # ---- everything below prefers the daemon, falling back to direct mode ---
    client = Client.discover()
    if client is None and command == "start" and not getattr(args, "no_daemon", False):
        # Auto-start the daemon so `wfrec start` actually records. Without a
        # daemon nothing supervises the collectors, so a bare `start` would
        # create a session and capture nothing but shell commands.
        client = _spawn_daemon()
    if client is not None:
        return _via_daemon(client, args, as_json)
    return _direct(args, as_json)


def _spawn_daemon(timeout: float = 15.0) -> Client | None:
    """Launch a detached daemon and wait for it to answer /health."""

    import subprocess
    import time

    creation: dict[str, Any] = {}
    if sys.platform == "win32":  # pragma: no cover - Windows
        creation["creationflags"] = 0x00000008 | 0x00000200  # DETACHED | NEW_GROUP
    else:
        creation["start_new_session"] = True

    log_path = paths.home() / "daemon.log"
    try:
        paths.ensure_home()
        handle = log_path.open("a", encoding="utf-8")
        from .bind import daemon_spawn_argv

        subprocess.Popen(
            daemon_spawn_argv(),
            stdout=handle,
            stderr=handle,
            stdin=subprocess.DEVNULL,
            **creation,
        )
    except OSError as exc:
        warning(f"Could not start daemon ({exc}); continuing without one.")
        return None

    deadline = time.time() + timeout
    while time.time() < deadline:
        client = Client.discover()
        if client is not None:
            return client
        time.sleep(0.25)
    warning(
        f"Daemon did not start within {timeout:.0f}s; see {log_path}. "
        "Continuing without background capture."
    )
    return None


def _via_daemon(client: Client, args: argparse.Namespace, as_json: bool) -> int:
    command = args.command
    if command == "start":
        result = client.post(
            "/sessions/start",
            {
                "title": args.title,
                "analyst": args.analyst or paths.default_analyst(),
                "workflow_family": args.workflow_family,
                "tags": args.tag,
                "watch": [str(p) for p in args.watch],
                "sources": {name: False for name in args.without},
            },
        )
    elif command == "pause":
        result = client.post(
            "/sessions/pause",
            {"session_id": args.session_id, "reason": args.reason, "expect": args.expect},
        )
    elif command == "resume":
        result = client.post("/sessions/resume", {"session_id": args.session_id})
    elif command == "stop":
        result = client.post("/sessions/stop", {"session_id": args.session_id})
    elif command == "source":
        result = client.post(f"/sources/{args.name}", {"enabled": args.state})
    elif command == "shell-output":
        result = client.post("/shell-output", {"enabled": args.state})
    elif command == "note":
        result = client.post(
            "/notes", {"text": _note_text(args), "label": args.label, "pasted": True}
        )
    elif command == "mark":
        result = client.post("/markers", {"label": args.label, "detail": args.detail})
    elif command == "watch":
        result = client.post("/watch", {"root": str(args.root.expanduser().resolve())})
    elif command == "status":
        result = client.get("/status")
    else:  # pragma: no cover - argparse restricts this
        raise ValueError(f"Unhandled command: {command}")

    _report(command, result, as_json, daemon=True)
    return 0


def _direct(args: argparse.Namespace, as_json: bool) -> int:
    """Run without a daemon: state changes apply, background capture does not."""

    recorder = Recorder(supervise=False)
    command = args.command

    if command == "start":
        result = recorder.start_session(
            title=args.title,
            analyst=args.analyst or paths.default_analyst(),
            workflow_family=args.workflow_family,
            tags=args.tag,
            watch=args.watch,
            sources={name: False for name in args.without},
        )
    elif command == "pause":
        result = recorder.pause_session(
            args.session_id, reason=args.reason, expect=args.expect
        )
    elif command == "resume":
        result = recorder.resume_session(args.session_id)
    elif command == "stop":
        result = recorder.stop_session(args.session_id)
    elif command == "source":
        result = recorder.set_source(args.name, args.state)
    elif command == "shell-output":
        result = recorder.set_shell_output(args.state)
    elif command == "note":
        result = recorder.add_note(_note_text(args), label=args.label, pasted=True)
    elif command == "mark":
        result = recorder.add_marker(args.label, args.detail)
    elif command == "watch":
        result = recorder.add_watch_root(args.root)
    elif command == "status":
        result = recorder.status()
    else:  # pragma: no cover
        raise ValueError(f"Unhandled command: {command}")

    recorder.shutdown()
    _report(command, result, as_json, daemon=False)
    return 0


def _report(command: str, result: dict, as_json: bool, *, daemon: bool) -> None:
    if as_json:
        _emit(result, True)
        return

    session = (result or {}).get("session") or {}
    if command == "status":
        _print_status(result)
        return

    if command == "start":
        rows: list[tuple[str, object]] = [
            ("Session", session.get("id")),
            ("Title", session.get("title") or "Untitled"),
            ("Folder", session.get("root")),
        ]
        if result.get("preempted"):
            rows.append(("Paused", result["preempted"]))
        flags = ", ".join(n for n, on in (result.get("sources") or {}).items() if on)
        rows.append(("Capturing", flags or "Nothing"))
        summary("Recording session", rows)
        if not daemon:
            warning(
                "No daemon is running, so background collectors are inactive. "
                "Start one with `wfrec daemon` or `wfrec daemon --gui`."
            )
        return

    if command == "stop":
        summary(
            "Stopped session",
            [
                ("Session", result.get("stopped")),
                ("Folder", result.get("root")),
                ("Export", "wfrec export --format autocab"),
            ],
        )
        return

    if command in {"pause", "resume"}:
        summary(
            "Updated session",
            [
                ("Session", session.get("id")),
                ("Status", session.get("status")),
            ],
        )
        return

    if command == "source":
        flags = ", ".join(n for n, on in (result.get("sources") or {}).items() if on)
        summary("Updated capture sources", [("Capturing", flags or "Nothing")])
        collectors = result.get("collectors") or {}
        for name, info in collectors.items():
            if info.get("available") is False:
                warning(
                    f"{name}: {info.get('reason')} "
                    f"{info.get('detail', '')[:100]}".rstrip()
                )
        return

    if command == "shell-output":
        summary(
            "Shell output capture",
            [
                ("Status", Text("Off", style="yellow")),
                ("Reason", result.get("shell_output_reason", "")),
            ],
        )
        return

    if command == "note":
        redactions = result.get("redactions") or []
        suffix = f" (redacted: {', '.join(redactions)})" if redactions else ""
        success(f"Added note to timeline{suffix}")
        return

    if command == "mark":
        success(f"Added marker to timeline at event {result.get('seq')}.")
        return

    if command == "watch":
        roots = session.get("watch_roots") or []
        summary(
            "Updated watched roots",
            [("Root", roots[-1] if roots else "None")],
        )
        return

    console.print_json(json.dumps(result, indent=2, default=str))


def _print_status(result: dict) -> None:
    session = result.get("session") or {}
    if not result.get("active_session"):
        rows: list[tuple[str, object]] = [
            ("Session", Text("None", style="dim")),
        ]
        if result.get("paused_sessions"):
            rows.append(("Paused", ", ".join(result["paused_sessions"])))
        rows.append(("Start with", "wfrec start --title '...'"))
        summary("Recorder status", rows)
        return

    rows = [
        ("Session", session.get("id")),
        ("Status", session.get("status")),
        ("Title", session.get("title") or "Untitled"),
        ("Analyst", session.get("analyst")),
        ("Events", session.get("events")),
        ("Folder", session.get("root")),
    ]
    if result.get("pause_reason"):
        rows.append(("Pause reason", result["pause_reason"]))
    summary("Recorder status", rows)

    sources = data_table("State", "Source", "Backend", "Details")
    collectors = result.get("collectors") or {}
    for name in SOURCES:
        on = (result.get("sources") or {}).get(name)
        info = collectors.get(name) or {}
        state = Text("ON", style="bold green") if on else Text("OFF", style="dim")
        backend = info.get("backend") or ""
        detail = ""
        if on and info.get("available") is False:
            detail = str(info.get("reason") or "Unavailable")
            state = Text("--", style="yellow")
        sources.add_row(
            state,
            plain_text(name),
            plain_text(backend),
            plain_text(detail),
        )
    if result.get("shell_output_available") is False:
        sources.add_row(
            Text("--", style="yellow"),
            Text("shell-output"),
            Text(""),
            plain_text(result.get("shell_output_reason", "")),
        )
    console.print(Text("\nSources", style="bold"), sources)


def _note_text(args: argparse.Namespace) -> str:
    if args.text:
        return args.text
    if sys.stdin.isatty():
        raise ValueError("No text given. Pass it as an argument or pipe it on stdin.")
    return sys.stdin.read()


def _hooks(args: argparse.Namespace, as_json: bool) -> int:
    from . import hookinstall

    shells = args.shell or None
    if args.action == "install":
        result = hookinstall.install(shells)
        if as_json:
            _emit(result, True)
        else:
            table = data_table("Shell", "Result", "RC file", "Backup")
            for entry in result:
                table.add_row(
                    plain_text(entry["shell"]),
                    plain_text(entry["status"]),
                    plain_text(entry.get("rc_file") or ""),
                    plain_text(entry.get("backup") or ""),
                )
            console.print(Text("Shell hooks", style="bold cyan"), table)
            console.print(
                Text(
                    "\nOpen a new terminal, or run this in an existing one:",
                    style="bold",
                )
            )
            console.print(
                plain_text(
                    hookinstall.eval_line(shells[0] if shells else "bash"),
                    "cyan",
                )
            )
        return 0

    if args.action == "uninstall":
        result = hookinstall.uninstall(shells)
        if as_json:
            _emit(result, True)
        elif result:
            table = data_table("Shell", "Removed from")
            for entry in result:
                table.add_row(
                    plain_text(entry["shell"]),
                    plain_text(entry["rc_file"]),
                )
            console.print(Text("Removed shell hooks", style="bold cyan"), table)
        else:
            success("No wfrec hooks were installed.")
        return 0

    if args.action == "eval":
        sys.stdout.write(hookinstall.eval_line(shells[0] if shells else "bash") + "\n")
        return 0

    report = hookinstall.status()
    if as_json:
        _emit(report, True)
    else:
        table = status_table("Shell", "Configuration")
        for entry in report:
            installed = bool(entry["installed"])
            locations = list(entry["rc_files"])
            missing = list(entry.get("missing_rc_files") or [])
            if missing:
                locations.append(f"Missing: {', '.join(missing)}")
            table.add_row(
                status_text(installed),
                plain_text(entry["shell"]),
                plain_text(", ".join(locations) or "Not installed"),
            )
        console.print(Text("Shell hooks", style="bold cyan"), table)
    return 0


def _sessions(as_json: bool) -> int:
    sessions = SessionStore().list_sessions()
    if as_json:
        _emit(
            [
                {
                    "id": s.session_id,
                    "title": s.manifest.title,
                    "analyst": s.manifest.analyst,
                    "status": s.manifest.status,
                    "created_at": s.manifest.created_at,
                }
                for s in sessions
            ],
            True,
        )
        return 0
    if not sessions:
        summary(
            "Sessions",
            [
                ("Status", Text("None", style="dim")),
                ("Folder", paths.sessions_dir()),
            ],
        )
        return 0
    table = data_table("Session", "Status", "Analyst", "Title")
    for s in sessions:
        status = (
            Text(s.manifest.status, style="green")
            if s.manifest.status == "active"
            else plain_text(s.manifest.status)
        )
        table.add_row(
            plain_text(s.session_id),
            status,
            plain_text(s.manifest.analyst),
            plain_text(s.manifest.title or "Untitled"),
        )
    console.print(Text("Sessions", style="bold cyan"), table)
    return 0


def _events(args: argparse.Namespace, as_json: bool) -> int:
    session = SessionStore().resolve(args.session_id)
    events = session.writer.read()
    if args.source:
        events = [e for e in events if e.source == args.source]
    if args.type:
        events = [e for e in events if e.type.startswith(args.type)]
    if args.role:
        role = args.role.casefold()
        events = [
            event
            for event in events
            if str(event.payload.get("role") or "").casefold() == role
        ]
    if args.tool:
        tool = args.tool.casefold()
        events = [
            event
            for event in events
            if str(event.payload.get("tool") or "").casefold() == tool
        ]
    events = events[-args.limit :]

    if as_json:
        _emit([e.to_dict() for e in events], True)
        return 0
    table = data_table("Seq", "Time", "Type", "Summary")
    table.columns[0].justify = "right"
    for event in events:
        event_summary = _summarize(event)
        table.add_row(
            plain_text(event.seq),
            plain_text(event.ts[11:23]),
            plain_text(_event_type_label(event)),
            plain_text(event_summary),
        )
    console.print(Text("Timeline events", style="bold cyan"), table)
    return 0


def _event_type_label(event: Event) -> str:
    """Add useful agent context without changing the canonical event type."""

    if event.type != "agent.message":
        return event.type
    tool = str(event.payload.get("tool") or "unknown")
    role = str(event.payload.get("role") or "unknown")
    return f"{event.type} [{tool}/{role}]"


def _summarize(event: Event) -> str:
    payload = event.payload
    for key in ("command", "path", "window_title", "label", "reason"):
        if payload.get(key):
            return str(payload[key])[:80]
    if payload.get("text"):
        return str(payload["text"])[:80].replace("\n", " ")
    if payload.get("ocr_text"):
        return str(payload["ocr_text"])[:80].replace("\n", " ")
    if event.type == "git.snapshot":
        return f"{payload.get('branch')} {payload.get('changed_count')} changed"
    return ""


def _seal(args: argparse.Namespace, as_json: bool) -> int:
    from autocab.deid.engines.base import EngineUnavailable
    from autocab.deid.engines.registry import load as load_engine
    from autocab.deid.policy import Policy, RenderMode

    from .seal import SealError, seal_session, seal_status

    store = SessionStore()
    if args.status:
        try:
            session = store.resolve(args.session_id)
        except Exception as exc:
            error(str(exc))
            return 1
        status = seal_status(session.root)
        if as_json:
            _emit(status, True)
        else:
            rows: list[tuple[str, object]] = [
                ("Session", session.session_id),
                ("Sealed", "yes" if status["sealed"] else "no"),
            ]
            if status.get("journal"):
                rows.append(("Seal in progress", status["journal"]))
            record = status.get("seal") or {}
            for key in ("assurance", "generation", "engine", "findings", "distinct_values", "sealed_at"):
                if key in record:
                    rows.append((key.replace("_", " ").title(), record[key]))
            summary(f"Seal status for {session.session_id}", rows)
        return 0

    try:
        session = store.resolve(args.session_id)
    except Exception as exc:
        error(str(exc))
        return 1

    detectors = []
    names = [name for name in args.engine.split("+") if name and name != "regex"]
    for name in names:
        try:
            detectors.append(load_engine(name))
        except EngineUnavailable as exc:
            # Fail closed. `Redacted.available`'s degraded mode is right for
            # capture and wrong for a seal: the whole point of a seal is the
            # guarantee, so a missing tier refuses rather than quietly
            # producing a weaker artifact that still says `sealed`.
            error(f"{exc}")
            warning("Refusing to write a sealed artifact with a missing engine tier.")
            return 1

    policy = Policy(
        profile=args.profile,
        render=RenderMode.PSEUDONYMIZE,
        pseudonymize_analyst=args.pseudonymize_analyst,
    )
    deny_terms = ("patient", "diagnosis", "pathology", *args.deny_term)
    stop_session = None
    if args.force:
        client = Client.discover()
        if client is not None:
            stop_session = lambda: client.post(
                "/sessions/stop", {"session_id": session.session_id}
            )
        else:
            recorder = Recorder(supervise=False)
            stop_session = lambda: recorder.stop_session(session.session_id)

    try:
        result = seal_session(
            session,
            detectors=detectors,
            deny_terms=deny_terms,
            policy=policy,
            engine_label=args.engine,
            reseal=args.reseal,
            force=args.force,
            dry_run=args.dry_run,
            stop_session=stop_session,
        )
    except SealError as exc:
        error(str(exc))
        return 1

    if as_json:
        _emit({"session": session.session_id, "dry_run": result.dry_run, **result.record}, True)
        return 0

    record = result.record
    summary(
        f"{'Would seal' if result.dry_run else 'Sealed'} {session.session_id}",
        [
            ("Assurance", record["assurance"]),
            ("Generation", record["generation"]),
            ("Engine", record["engine"]),
            ("Findings", record["findings"]),
            ("Distinct values", record["distinct_values"]),
            ("Masked at capture", record["masked_at_capture"]),
            ("Targets", len(result.targets) if result.dry_run else len(record["targets"])),
            ("Pseudonym key", record["pseudonym_key"]),
            ("Reverse map", record["reverse_map"]),
            ("Duration", f"{record['duration_seconds']}s"),
        ],
    )
    if record["counts_by_label"]:
        summary("Findings by label", sorted(record["counts_by_label"].items()))
    if result.dry_run:
        warning("Dry run: nothing was written and the session is unchanged.")
    return 0


def _deid(args: argparse.Namespace, as_json: bool) -> int:
    from autocab.deid.engines.registry import available_engines

    verb = args.deid_command
    if verb == "providers":
        from autocab.deid.egress import classify

        rows: list[tuple[str, object]] = []
        payload: dict[str, Any] = {}
        for name, info in sorted(available_engines().items()):
            payload[name] = info.to_dict()
        if as_json:
            _emit({"engines": payload}, True)
            return 0
        for name, info in sorted(available_engines().items()):
            rows.append((name, "available" if info.available else f"unavailable: {info.reason}"))
        summary("De-identification engines", rows)
        warning(
            "LLM providers are not yet implemented in this build (build spec "
            "step 10). `classify` is available now for egress checks."
        )
        assert callable(classify)
        return 0

    error(
        f"`wfrec deid {verb}` needs the packaged model tier, which is build spec "
        "step 8 and is not implemented in this build. The regex tier works with "
        "no weights: `wfrec seal --engine regex`."
    )
    return 1


def _export(args: argparse.Namespace, as_json: bool) -> int:
    from .exporters import export_session

    session = SessionStore().resolve(args.session_id)
    result = export_session(session, formats=args.format or ["autocab", "trace"])
    if as_json:
        _emit(result, True)
        return 0
    details = result.get("details", {})
    rows: list[tuple[str, object]] = [("Session", result["session"])]
    if "terminal_log" in details:
        rows.append(("Shell commands", details["terminal_log"]["commands"]))
    if "screen_capture" in details:
        rows.append(("Capture events", details["screen_capture"]["events"]))
    if "trace" in details:
        rows.append(("Workflow steps", details["trace"]["steps"]))
    summary("Exported session", rows)

    files = data_table("Written file")
    for path in result["written"]:
        files.add_row(plain_text(path))
    console.print(Text("\nFiles", style="bold"), files)
    console.print(Text("\nRun with AutoCAB", style="bold"))
    console.print(
        plain_text(
            f"autocab demo --input-mode session --session-dir {session.root}",
            "cyan",
        )
    )
    return 0


def _ssh(args: argparse.Namespace, as_json: bool) -> int:
    from . import remote

    session = SessionStore().resolve(None)
    boot = remote.bootstrap(args.host)
    if not boot["ok"]:
        error(
            f"Could not bootstrap {args.host}: "
            f"{boot.get('error') or boot.get('stderr')}"
        )
        return 1
    if args.host not in session.manifest.remote_hosts:
        session.manifest.remote_hosts.append(args.host)
        session.save()
    summary(
        "Recording remote shell",
        [
            ("Host", args.host),
            ("Session", session.session_id),
            ("Next", "wfrec pull <host>"),
        ],
    )
    extra = [a for a in (args.ssh_args or []) if a != "--"]
    return remote.interactive_shell(args.host, session.session_id, extra)


def _pull(args: argparse.Namespace, as_json: bool) -> int:
    from . import remote
    from .collectors.shell import ShellCollector

    session = SessionStore().resolve(None)
    pulled = remote.pull_spool(args.host, session)
    events = remote.collect_slurm(args.host, session, args.job)
    if events:
        session.writer.extend(events)

    # Ingest the pulled spool through the same collector the local path uses,
    # so remote and local commands are normalized identically.
    collector = ShellCollector(
        session,
        extra_spools={Path(pulled["dir"]): f"remote:{args.host}"},
        local_backend=session.manifest.shell_backend,
    )
    collector._run_once()

    result = {
        "host": args.host,
        "files": pulled["pulled"],
        "job_events": len(events),
        "session": session.session_id,
    }
    if as_json:
        _emit(result, True)
    else:
        summary(
            "Pulled remote activity",
            [
                ("Host", args.host),
                ("Spool files", len(pulled["pulled"])),
                ("Scheduler events", len(events)),
                ("Session", session.session_id),
            ],
        )
        if pulled["pulled"]:
            files = data_table("Spool file")
            for name in pulled["pulled"]:
                files.add_row(plain_text(name))
            console.print(Text("\nFiles", style="bold"), files)
    return 0
