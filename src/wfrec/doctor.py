"""`wfrec doctor` -- report the resolved backend per source, and why.

With three operating systems, two display servers and a hybrid team, the most
expensive class of bug is "works on my machine". This prints, for the current
host, exactly which backend each source resolved to and the reason for any
degradation, so a teammate can paste one output block instead of describing
symptoms.
"""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path
from typing import Any

from rich import box
from rich.console import Group
from rich.table import Table
from rich.text import Text

from . import SOURCES, __version__, paths
from .collectors.shell import (
    ShellBackendSelection,
    resolve_shell_backend,
)
from .devsql import DevSQLClient
from .events import platform_summary
from .state import (
    SHELL_BACKEND_DEVSQL,
    RecorderState,
    read_api,
    read_sentinel,
)


def _probe_import(module: str) -> dict[str, Any]:
    try:
        imported = __import__(module)
        return {"ok": True, "version": getattr(imported, "__version__", "")}
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"[:160]}


def diagnose() -> dict[str, Any]:
    """Collect a full environment report."""

    from .collectors.window import active_window

    state = RecorderState.load()
    report: dict[str, Any] = {
        "wfrec_version": __version__,
        "python": sys.version.split()[0],
        "platform": platform_summary(),
        "paths": {
            "home": str(paths.home()),
            "runtime_dir": str(paths.runtime_dir()),
            "sentinel": str(paths.active_path()),
            "sentinel_present": paths.active_path().exists(),
        },
        "state": {
            "active_session": state.active_session,
            "paused_sessions": state.paused,
            "sources": state.sources,
            "shell_output": state.shell_output,
            "shell_backend": state.shell_backend,
        },
        "sentinel": None,
        "daemon": None,
        "sources": {},
        "hooks": [],
        "binaries": {},
        "warnings": [],
    }

    sentinel = read_sentinel()
    if sentinel:
        report["sentinel"] = {
            "session": sentinel[0],
            "flags": sentinel[1],
            "spool": str(sentinel[2]),
        }
        # The sentinel is what shells actually obey, so a disagreement between
        # it and the rich state is a real bug, not cosmetic.
        if sentinel[0] != state.active_session:
            report["warnings"].append(
                "Sentinel session does not match state.json; shells may be "
                "writing to the wrong session. Run `wfrec status` to resync."
            )

    api = read_api()
    if api:
        report["daemon"] = {"url": api[0], "token_present": bool(api[1])}

    # ------------------------------------------------------------- libraries
    libs = {
        "mss": _probe_import("mss"),
        "PIL": _probe_import("PIL"),
        "watchdog": _probe_import("watchdog"),
        "fastapi": _probe_import("fastapi"),
        "rapidocr_onnxruntime": _probe_import("rapidocr_onnxruntime"),
        "imageio_ffmpeg": _probe_import("imageio_ffmpeg"),
    }
    if sys.platform == "linux":
        libs["Xlib"] = _probe_import("Xlib")
    if sys.platform == "darwin":  # pragma: no cover - macOS only
        libs["Quartz"] = _probe_import("Quartz")
        libs["ocrmac"] = _probe_import("ocrmac")
    report["libraries"] = libs

    if not libs["rapidocr_onnxruntime"]["ok"] and "libGL" in str(
        libs["rapidocr_onnxruntime"].get("error", "")
    ):
        report["warnings"].append(
            "OCR failed to import with a libGL error: rapidocr-onnxruntime pulled "
            "in `opencv-python`, which needs system OpenGL. Install "
            "`opencv-python-headless` instead."
        )

    # -------------------------------------------------------------- binaries
    for name in ("git", "ssh", "rsync", "xdotool", "grim", "sacct", "sbatch"):
        report["binaries"][name] = shutil.which(name) or ""
    if not report["binaries"]["git"]:
        report["warnings"].append(
            "git not found: file-change diffs and git snapshots will be skipped."
        )

    # ------------------------------------------------------- source backends
    window = active_window()
    display = report["platform"]["display_server"]

    report["sources"]["screen"] = _screen_status(display, libs, window)
    persisted_backend = state.shell_backend if state.active_session else None
    shell_selection = resolve_shell_backend(persisted_backend)
    report["sources"]["shell"] = _shell_status(shell_selection)
    report["sources"]["context"] = {
        "backend": "api",
        "available": True,
        "detail": "Paste box and `wfrec note`. No background clipboard access.",
    }
    report["sources"]["files"] = _files_status(libs)
    report["sources"]["agents"] = _agents_status(shell_selection.client)

    # ----------------------------------------------------------------- hooks
    try:
        from .hookinstall import status as hook_status

        report["hooks"] = hook_status()
        hooks_available = any(
            entry.get("installed", False) for entry in report["hooks"]
        )
        report["sources"]["shell"]["fallback"] = (
            "hook-spool available"
            if hooks_available
            else "hook-spool not installed"
        )
        if (
            shell_selection.name != SHELL_BACKEND_DEVSQL
            and not hooks_available
        ):
            report["sources"]["shell"].update(
                available=False,
                reason="no-shell-hooks",
            )
            report["warnings"].append(
                "No shell hooks installed: shell commands will not be captured. "
                "Run `wfrec hooks install`."
            )
    except Exception as exc:  # pragma: no cover - defensive
        report["hooks"] = [{"error": str(exc)}]

    if display == "wayland":
        report["warnings"].append(
            "Wayland session detected: unattended screen capture and window "
            "titles are unavailable to ordinary clients. Use an Xorg session "
            "for a screen-recording demo."
        )
    if sys.platform == "win32":  # pragma: no cover - Windows
        report["warnings"].append(
            "On Windows only PowerShell can be hooked; cmd.exe provides no "
            "preexec/precmd mechanism."
        )

    return report


def _screen_status(display: str, libs: dict, window) -> dict[str, Any]:
    if display == "wayland":
        return {
            "backend": "portal-manual",
            "available": False,
            "reason": "wayland-no-unattended-capture",
            "detail": "mss has no Wayland backend and the xdg portal prompts per request.",
            "window_backend": window.backend,
        }
    if not libs["mss"]["ok"]:
        return {
            "backend": "",
            "available": False,
            "reason": "mss-missing",
            "detail": libs["mss"].get("error", ""),
        }
    ocr = "rapidocr" if libs["rapidocr_onnxruntime"]["ok"] else ""
    if sys.platform == "darwin" and libs.get("ocrmac", {}).get("ok"):  # pragma: no cover
        ocr = "ocrmac"
    return {
        "backend": "mss",
        "available": True,
        "ocr_backend": ocr or "none",
        "video": "imageio-ffmpeg" if libs["imageio_ffmpeg"]["ok"] else "none",
        "window_backend": window.backend,
        **({"window_reason": window.reason} if window.reason else {}),
    }


def _files_status(libs: dict) -> dict[str, Any]:
    from .collectors.files import network_filesystem

    detail = ""
    cwd_fs = network_filesystem(Path.cwd())
    if cwd_fs:
        detail = (
            f"Current directory is on {cwd_fs}, where inotify delivers no events; "
            "this root would be polled instead."
        )
    return {
        "backend": "watchdog" if libs["watchdog"]["ok"] else "polling",
        "available": True,
        "detail": detail or "watchdog triggers, git verifies.",
    }


def _shell_status(selection: ShellBackendSelection) -> dict[str, Any]:
    """Describe the selected shell backend without reading command rows."""

    available = (
        selection.name != SHELL_BACKEND_DEVSQL
        or selection.client is not None
    )
    return {
        "backend": selection.name,
        "available": available,
        "provider": selection.provider,
        "version": selection.devsql_version,
        "reason": "" if available else "devsql-unavailable",
        "detail": selection.detail,
    }


def _agents_status(
    devsql_client: DevSQLClient | None = None,
) -> dict[str, Any]:
    """Probe agent schemas and paths without reading transcript rows."""

    from .collectors.agents import (
        ClaudeCodeAdapter,
        CopilotChatAdapter,
        CursorAdapter,
        DevSQLCodexAdapter,
    )

    detected: dict[str, bool] = {DevSQLCodexAdapter.name: False}
    try:
        client = devsql_client or DevSQLClient.discover()
        detected[DevSQLCodexAdapter.name] = DevSQLCodexAdapter(
            client,
            since="1970-01-01T00:00:00Z",
        ).available()
    except Exception:
        pass

    for adapter in (ClaudeCodeAdapter(), CopilotChatAdapter(), CursorAdapter()):
        try:
            detected[adapter.name] = adapter.available()
        except Exception:
            detected[adapter.name] = False
    return {
        "backend": ",".join(name for name, ok in detected.items() if ok) or "none",
        "available": any(detected.values()),
        "detected": detected,
        "unavailable": [name for name, ok in detected.items() if not ok],
        "detail": "Manual fallback: `wfrec attach-transcript <file> --tool <name>`.",
    }


def render(report: dict[str, Any]) -> Group:
    """Build a compact terminal report without changing diagnostic data."""

    platform_info = report["platform"]
    heading = Text.assemble(
        (f"wfrec {report['wfrec_version']}", "bold cyan"),
        (f"  Python {report['python']}", "dim"),
    )

    system = Table.grid(padding=(0, 2))
    system.add_column(style="bold", no_wrap=True)
    system.add_column(overflow="fold")
    system.add_row(
        "Host",
        _plain_text(
            f"{platform_info['system']} {platform_info['release']} "
            f"{platform_info['machine']}"
        ),
    )
    system.add_row("Display", _plain_text(platform_info["display_server"]))
    system.add_row("Home", _plain_text(report["paths"]["home"]))
    system.add_row("Runtime", _plain_text(report["paths"]["runtime_dir"]))
    daemon = report.get("daemon")
    system.add_row(
        "Daemon",
        _plain_text(daemon["url"], "cyan")
        if daemon
        else Text("Not running", style="yellow"),
    )

    state = report["state"]
    active_session = state["active_session"]
    system.add_row(
        "Session",
        _plain_text(active_session)
        if active_session
        else Text("None", style="dim"),
    )
    if report.get("sentinel"):
        system.add_row(
            "Sentinel",
            _plain_text(report["sentinel"]["flags"]),
        )

    sources = _status_table("Source", "Backend", "Details")
    for name in SOURCES:
        info = report["sources"].get(name, {})
        backend = info.get("backend") or "none"
        sources.add_row(
            _status_text(bool(info.get("available"))),
            _plain_text(name),
            _plain_text(backend),
            _plain_text(_source_details(info)),
        )

    hooks = _status_table("Shell", "Configuration")
    for entry in report["hooks"]:
        if "error" in entry:
            hooks.add_row(
                _status_text(False),
                Text("Error", style="yellow"),
                _plain_text(entry["error"]),
            )
            continue
        installed = bool(entry["installed"])
        location = ", ".join(entry["rc_files"])
        hooks.add_row(
            _status_text(installed),
            _plain_text(entry["shell"]),
            _plain_text(location)
            if location
            else Text("Not installed", style="dim"),
        )

    sections: list[Any] = [
        heading,
        Text(""),
        Text("System", style="bold"),
        system,
        Text(""),
        Text("Sources", style="bold"),
        sources,
        Text(""),
        Text("Shell hooks", style="bold"),
        hooks,
    ]
    if report["warnings"]:
        warnings = Table.grid(padding=(0, 1))
        warnings.add_column(no_wrap=True)
        warnings.add_column(overflow="fold")
        for warning in report["warnings"]:
            warnings.add_row(
                Text("!", style="bold yellow"),
                _plain_text(warning),
            )
        sections.extend(
            [Text(""), Text("Warnings", style="bold yellow"), warnings]
        )
    return Group(*sections)


def _status_table(*columns: str) -> Table:
    """Return the shared compact table used by doctor sections."""

    table = Table(
        box=box.SIMPLE_HEAVY,
        expand=False,
        header_style="bold dim",
        pad_edge=False,
        show_edge=False,
    )
    table.add_column("Status", no_wrap=True)
    for column in columns:
        table.add_column(column, overflow="fold")
    return table


def _status_text(available: bool) -> Text:
    """Represent availability with both text and color."""

    if available:
        return Text("OK", style="bold green")
    return Text("--", style="yellow")


def _plain_text(value: object, style: str = "") -> Text:
    """Render dynamic values literally rather than as Rich markup."""

    return Text(str(value), style=style)


def _source_details(info: dict[str, Any]) -> str:
    """Summarize optional source attributes in one readable cell."""

    details: list[str] = []
    if info.get("provider"):
        details.append(f"provider: {info['provider']}")
    if info.get("version"):
        details.append(f"version: {info['version']}")
    if info.get("ocr_backend"):
        details.append(f"OCR: {info['ocr_backend']}")
    if info.get("window_backend"):
        details.append(f"window: {info['window_backend']}")
    if info.get("fallback"):
        details.append(f"fallback: {info['fallback']}")
    if info.get("unavailable"):
        details.append(f"unavailable: {', '.join(info['unavailable'])}")
    if info.get("reason"):
        details.append(f"reason: {info['reason']}")
    return ", ".join(details)
