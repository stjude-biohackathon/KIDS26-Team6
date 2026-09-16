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

from . import SOURCES, __version__, paths
from .client import Client, DaemonUnavailable
from .recorder import NoActiveSession, Recorder
from .session import SessionNotFound, SessionStore


def _emit(payload: Any, as_json: bool) -> None:
    if as_json:
        print(json.dumps(payload, indent=2, default=str))


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
        "shell-output", help="Toggle opt-in full shell output capture."
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
    daemon.add_argument("--gui", action="store_true", help="Open the UI on start.")
    daemon.add_argument(
        "--stop", action="store_true",
        help="Stop a running daemon (needed on macOS after granting Screen Recording).",
    )

    sub.add_parser("gui", help="Open the control window.")

    ssh = sub.add_parser("ssh", help="Open a recorded shell on a remote host.")
    ssh.add_argument("host")
    ssh.add_argument("ssh_args", nargs=argparse.REMAINDER)

    pull = sub.add_parser("pull", help="Pull remote spool and job output back.")
    pull.add_argument("host")
    pull.add_argument("--job", action="append", default=[], help="SLURM job id.")

    return parser


# ------------------------------------------------------------------ dispatch
def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    as_json = args.json

    try:
        return _dispatch(args, as_json)
    except NoActiveSession as exc:
        print(f"wfrec: {exc}", file=sys.stderr)
        return 2
    except SessionNotFound as exc:
        print(f"wfrec: {exc}", file=sys.stderr)
        return 3
    except DaemonUnavailable as exc:
        print(f"wfrec: {exc}", file=sys.stderr)
        return 4
    except (ValueError, RuntimeError) as exc:
        print(f"wfrec: {exc}", file=sys.stderr)
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
                print(f"Stopped daemon (pid {result['pid']}).")
            else:
                print(f"No daemon stopped: {result['reason']}")
            return 0 if result["stopped"] else 1
        return run(port=args.port, open_gui=args.gui)

    if command == "gui":
        from .daemon import launch_gui

        client = Client.discover(require=True)
        launch_gui(client.url, client.token)
        return 0

    if command == "doctor":
        from rich.console import Console

        from .doctor import diagnose, render

        report = diagnose()
        if as_json:
            _emit(report, True)
        else:
            Console(highlight=False).print(render(report))
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
            print(f"Merged {result['traces']} traces -> {result['path']}")
            print(f"  analysts: {', '.join(result['analysts']) or '(none)'}")
            for family, people in result["workflow_families"].items():
                shared = " <- shared" if len(people) > 1 else ""
                print(f"  {family}: {len(people)} analyst(s){shared}")
            for entry in result["skipped"]:
                print(f"  skipped {entry['session']}: {entry['error']}")
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
            print(f"Attached {len(events)} turns from {args.path} to {session.session_id}")
        return 0

    if command == "export":
        return _export(args, as_json)

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
        subprocess.Popen(
            [sys.executable, "-m", "wfrec", "daemon"],
            stdout=handle,
            stderr=handle,
            stdin=subprocess.DEVNULL,
            **creation,
        )
    except OSError as exc:
        print(f"wfrec: could not start daemon ({exc}); continuing without one.", file=sys.stderr)
        return None

    deadline = time.time() + timeout
    while time.time() < deadline:
        client = Client.discover()
        if client is not None:
            return client
        time.sleep(0.25)
    print(
        f"wfrec: daemon did not come up within {timeout:.0f}s; see {log_path}. "
        "Continuing without background capture.",
        file=sys.stderr,
    )
    return None


def _via_daemon(client: Client, args: argparse.Namespace, as_json: bool) -> int:
    command = args.command
    if command == "start":
        result = client.post(
            "/sessions/start",
            {
                "title": args.title,
                "analyst": args.analyst or _default_analyst(),
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
            analyst=args.analyst or _default_analyst(),
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
        print(f"Recording session {session.get('id')}  ({session.get('title')})")
        print(f"  folder: {session.get('root')}")
        if result.get("preempted"):
            print(f"  auto-paused previously active session {result['preempted']}")
        flags = ", ".join(n for n, on in (result.get("sources") or {}).items() if on)
        print(f"  capturing: {flags or 'nothing'}")
        if not daemon:
            print(
                "  note: no daemon running, so shell/file/screen/agent capture is "
                "not active.\n        Start one with `wfrec daemon` (or `wfrec daemon --gui`)."
            )
        return

    if command == "stop":
        print(f"Stopped {result.get('stopped')}")
        print(f"  folder: {result.get('root')}")
        print("  export with: wfrec export --format autocab")
        return

    if command in {"pause", "resume"}:
        print(f"Session {session.get('id')} is now {session.get('status')}")
        return

    if command == "source":
        flags = ", ".join(n for n, on in (result.get("sources") or {}).items() if on)
        print(f"capturing: {flags or 'nothing'}")
        collectors = result.get("collectors") or {}
        for name, info in collectors.items():
            if info.get("available") is False:
                print(f"  ! {name}: {info.get('reason')} -- {info.get('detail', '')[:100]}")
        return

    if command == "note":
        redactions = result.get("redactions") or []
        suffix = f" (redacted: {', '.join(redactions)})" if redactions else ""
        print(f"Added note to timeline{suffix}")
        return

    print(json.dumps(result, indent=2, default=str))


def _print_status(result: dict) -> None:
    session = result.get("session") or {}
    if not result.get("active_session"):
        print("No active session.")
        if result.get("paused_sessions"):
            print(f"  paused: {', '.join(result['paused_sessions'])}")
        print("  start one with: wfrec start --title '...'")
        return

    print(f"session {session.get('id')}  [{session.get('status')}]")
    print(f"  title:   {session.get('title')}")
    print(f"  analyst: {session.get('analyst')}")
    print(f"  events:  {session.get('events')}")
    print(f"  folder:  {session.get('root')}")
    if result.get("pause_reason"):
        print(f"  paused because: {result['pause_reason']}")
    print("  sources:")
    collectors = result.get("collectors") or {}
    for name in SOURCES:
        on = (result.get("sources") or {}).get(name)
        info = collectors.get(name) or {}
        mark = "on " if on else "off"
        note = ""
        if on and info.get("available") is False:
            note = f"  ! {info.get('reason')}"
        elif on and info.get("backend"):
            note = f"  ({info['backend']})"
        print(f"    {mark} {name}{note}")
    if result.get("shell_output"):
        print("    on  shell-output (full terminal output capture)")


def _note_text(args: argparse.Namespace) -> str:
    if args.text:
        return args.text
    if sys.stdin.isatty():
        raise ValueError("No text given. Pass it as an argument or pipe it on stdin.")
    return sys.stdin.read()


def _default_analyst() -> str:
    """Default the analyst id to the OS user, since it drives clustering."""

    import getpass

    try:
        return getpass.getuser()
    except Exception:  # pragma: no cover
        return "unknown-analyst"


def _hooks(args: argparse.Namespace, as_json: bool) -> int:
    from . import hookinstall

    shells = args.shell or None
    if args.action == "install":
        result = hookinstall.install(shells)
        if as_json:
            _emit(result, True)
        else:
            for entry in result:
                print(f"{entry['shell']}: {entry['status']}")
                if entry.get("rc_file"):
                    print(f"  rc file: {entry['rc_file']}")
                if entry.get("backup"):
                    print(f"  backup:  {entry['backup']}")
            print(
                "\nOpen a new terminal, or run this in an existing one to start "
                "recording there immediately:"
            )
            print(f"  {hookinstall.eval_line(shells[0] if shells else 'bash')}")
        return 0

    if args.action == "uninstall":
        result = hookinstall.uninstall(shells)
        _emit(result, as_json) if as_json else [
            print(f"{e['shell']}: removed from {e['rc_file']}") for e in result
        ]
        if not as_json and not result:
            print("No wfrec hooks were installed.")
        return 0

    if args.action == "eval":
        print(hookinstall.eval_line(shells[0] if shells else "bash"))
        return 0

    report = hookinstall.status()
    if as_json:
        _emit(report, True)
    else:
        for entry in report:
            mark = "installed" if entry["installed"] else "not installed"
            print(f"{entry['shell']:<11} {mark}")
            for rc in entry["rc_files"]:
                print(f"  {rc}")
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
        print(f"No sessions yet under {paths.sessions_dir()}")
        return 0
    for s in sessions:
        print(f"{s.session_id}  {s.manifest.status:<8} {s.manifest.analyst:<14} {s.manifest.title}")
    return 0


def _events(args: argparse.Namespace, as_json: bool) -> int:
    session = SessionStore().resolve(args.session_id)
    events = session.writer.read()
    if args.source:
        events = [e for e in events if e.source == args.source]
    if args.type:
        events = [e for e in events if e.type.startswith(args.type)]
    events = events[-args.limit :]

    if as_json:
        _emit([e.to_dict() for e in events], True)
        return 0
    for event in events:
        summary = _summarize(event)
        print(f"{event.seq:>5}  {event.ts[11:23]}  {event.type:<26} {summary}")
    return 0


def _summarize(event) -> str:
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


def _export(args: argparse.Namespace, as_json: bool) -> int:
    from .exporters import export_session

    session = SessionStore().resolve(args.session_id)
    result = export_session(session, formats=args.format or ["autocab", "trace"])
    if as_json:
        _emit(result, True)
        return 0
    print(f"Exported session {result['session']}:")
    for path in result["written"]:
        print(f"  {path}")
    details = result.get("details", {})
    if "terminal_log" in details:
        print(f"  {details['terminal_log']['commands']} shell commands")
    if "screen_capture" in details:
        print(f"  {details['screen_capture']['events']} capture events")
    if "trace" in details:
        print(f"  {details['trace']['steps']} workflow steps")
    print("\nFeed it to AutoCAB with:")
    print(f"  autocab demo --input-mode session --session-dir {session.root}")
    return 0


def _ssh(args: argparse.Namespace, as_json: bool) -> int:
    from . import remote

    session = SessionStore().resolve(None)
    boot = remote.bootstrap(args.host)
    if not boot["ok"]:
        print(f"wfrec: could not bootstrap {args.host}: {boot.get('error') or boot.get('stderr')}", file=sys.stderr)
        return 1
    if args.host not in session.manifest.remote_hosts:
        session.manifest.remote_hosts.append(args.host)
        session.save()
    print(f"wfrec: recording remote shell on {args.host} into {session.session_id}")
    print("       run `wfrec pull <host>` afterwards to fold the commands in")
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
    collector = ShellCollector(session)
    collector.add_spool(Path(pulled["dir"]), f"remote:{args.host}")
    collector.safe_probe()
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
        print(f"Pulled {len(pulled['pulled'])} spool file(s) from {args.host}")
        for name in pulled["pulled"]:
            print(f"  {name}")
        if events:
            print(f"  {len(events)} scheduler event(s)")
    return 0
