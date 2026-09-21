"""AutoCAB recording daemon: control API plus collector supervision."""

from __future__ import annotations

import contextlib
import secrets
import signal
import sys
import threading
import time
from typing import Any

from rich.text import Text

from autocab.output import ACCENT_STYLE, panel_summary, plain_text, warning

from . import paths
from .bind import resolve_daemon_bind, daemon_summary_rows
from .recorder import Recorder
from .state import StateTransaction, clear_api, mark_boot, publish_api, read_api

DEFAULT_PORT = 8787
_GUI_READY_TIMEOUT = 30.0


def free_port(preferred: int = DEFAULT_PORT, listen_host: str = "127.0.0.1") -> int:
    """Return ``preferred`` if bindable, otherwise an OS-assigned port."""

    from .bind import free_port as _free_port

    return _free_port(preferred, listen_host)


def daemon_alive(timeout: float = 1.0) -> str | None:
    """Return the daemon's base URL if one is responding, else ``None``."""

    api = read_api()
    if not api:
        return None
    url, _token = api
    try:
        import httpx

        response = httpx.get(f"{url}/health", timeout=timeout)
        if response.status_code == 200:
            return url
    except Exception:
        return None
    return None


def stop() -> dict[str, object]:
    """Signal a running daemon to exit.

    Needed in practice on macOS: the Screen Recording grant is cached per
    process, so after granting it the daemon has to be restarted before screen
    capture will work. Hunting for the PID in `ps` is a poor substitute.
    """

    import os
    import signal

    try:
        pid = int(paths.pid_path().read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return {"stopped": False, "reason": "no daemon pid file found"}

    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        clear_api()
        with contextlib.suppress(FileNotFoundError):
            paths.pid_path().unlink()
        return {
            "stopped": False,
            "pid": pid,
            "reason": "process not running; cleaned up stale files",
        }
    except PermissionError:
        return {"stopped": False, "pid": pid, "reason": "not permitted to signal that process"}

    return {"stopped": True, "pid": pid}


def _spawn_module(module: str, label: str):
    """Launch ``python -m <module>`` as its own detached process.

    A separate process, not a thread: each recording-indicator surface needs
    to own its platform's native GUI event loop (Cocoa/Win32 for the tray
    icon, Tk for the floating badge) for its whole life, which would fight
    the daemon's asyncio server -- and each other -- for the main thread if
    run in-process. Each discovers the daemon itself via the published api
    file, so nothing needs passing to it.
    """

    import subprocess

    creation: dict[str, Any] = {}
    if sys.platform == "win32":  # pragma: no cover - Windows
        creation["creationflags"] = 0x00000008 | 0x00000200  # DETACHED | NEW_GROUP
    else:
        creation["start_new_session"] = True

    try:
        return subprocess.Popen(
            [sys.executable, "-m", module],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
            **creation,
        )
    except OSError as exc:
        warning(f"Could not start the {label} ({exc}); continuing without it.")
        return None


def _spawn_indicator_processes() -> list:
    """Launch every recording-indicator surface supported on this platform."""

    if sys.platform not in ("darwin", "win32"):
        return []  # no supported tray backend without extra system packages
    procs = [
        _spawn_module("autocab.recording.indicator", "menu-bar/tray indicator"),
        _spawn_module("autocab.recording.overlay", "floating recording badge"),
    ]
    return [proc for proc in procs if proc is not None]


def _stop_indicator_processes(procs: list) -> None:
    for proc in procs:
        if proc is None or proc.poll() is not None:
            continue
        try:
            proc.terminate()
            proc.wait(timeout=3)
        except Exception:
            with contextlib.suppress(Exception):
                proc.kill()


def run(
    port: int | None = None,
    *,
    host: str | None = None,
    bind_all: bool = False,
    advertise_url: str | None = None,
    allow_remote: bool = False,
    open_gui: bool = False,
    show_indicator: bool = True,
) -> int:
    """Run the daemon in the foreground until interrupted."""

    import uvicorn

    from .api import create_app

    paths.ensure_home()

    try:
        bind = resolve_daemon_bind(
            port,
            host=host,
            bind_all=bind_all,
            advertise_url=advertise_url,
            allow_remote_flag=allow_remote,
            preferred_port=DEFAULT_PORT,
        )
    except ValueError as exc:
        warning(str(exc))
        return 1

    # A previous daemon that died without cleaning up leaves a stale sentinel
    # and api file. Recording the boot id makes that detectable, and adopting
    # the active session means a restart resumes capture rather than orphaning
    # whatever the analyst was recording.
    mark_boot()
    from .locking import atomic_write_text

    atomic_write_text(paths.pid_path(), f"{__import__('os').getpid()}\n")
    recorder = Recorder()
    active = recorder.store.active()
    if active is not None:
        recorder.attach(active)
        recorder._start_collectors()

    token = secrets.token_urlsafe(32)
    publish_api(bind.public_url, token)
    with StateTransaction() as state:
        state.api_url = bind.public_url
        state.api_token = token

    app = create_app(recorder, token)
    config = uvicorn.Config(
        app,
        host=bind.listen_host,
        port=bind.port,
        log_level="warning",
        access_log=False,
    )
    server = uvicorn.Server(config)

    def shutdown(_signum: int, _frame: Any) -> None:
        server.should_exit = True

    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(ValueError, OSError):  # pragma: no cover - non-main thread
            signal.signal(sig, shutdown)

    rows = daemon_summary_rows(bind)
    if bind.listen_host not in ("127.0.0.1", "localhost"):
        rows.append(
            (
                "Security",
                "Non-loopback bind: only trusted networks; token is in the UI page",
            )
        )
    if bind.listen_host == "0.0.0.0" and bind.public_url.startswith("http://127."):
        warning(
            "Could not guess a cluster IP for --advertise-url; set "
            "WFREC_ADVERTISE_URL or --advertise-url so your browser can connect."
        )

    panel_rows, panel_hint = _dashboard_panel_content(rows)
    panel_summary("AutoCAB dashboard", panel_rows, hint=panel_hint)

    if open_gui:
        threading.Thread(
            target=_launch_gui_when_ready,
            args=(bind.public_url, token),
            daemon=True,
        ).start()

    indicators = _spawn_indicator_processes() if show_indicator else []

    try:
        server.run()
    finally:
        _stop_indicator_processes(indicators)
        recorder.shutdown()
        clear_api()
        with contextlib.suppress(FileNotFoundError):
            paths.pid_path().unlink()
    return 0


def _dashboard_panel_content(
    rows: list[tuple[str, str]],
) -> tuple[list[tuple[str, object]], str | None]:
    """Condense daemon connection details into the human-facing startup panel."""

    values = dict(rows)
    browser_url = values["Open in browser"]
    local_url = values["Local"]
    local_only = "Remote access" in values
    bind_scope = "local only" if local_only else "network"
    bind_value = Text.assemble(
        plain_text(values["Bind"]),
        (f" · {bind_scope}", "dim"),
    )
    panel_rows: list[tuple[str, object]] = [
        ("Status", Text("Running", style="bold green")),
        ("Browser", plain_text(browser_url, style=ACCENT_STYLE)),
        ("Bind", bind_value),
        ("Host", values["Host"]),
    ]
    if local_url != browser_url:
        panel_rows.append(("Local", plain_text(local_url, style=ACCENT_STYLE)))
    for label in ("Listen (all interfaces)", "Private", "Public", "Security"):
        if label in values:
            panel_rows.append((label, values[label]))
    panel_rows.append(("Sessions", plain_text(values["Sessions"], style=ACCENT_STYLE)))
    remote_hint = values.get("Remote access")
    if remote_hint:
        remote_hint = f"Network access: {remote_hint[0].lower()}{remote_hint[1:]}"
    return panel_rows, remote_hint


def _wait_for_server(base_url: str, timeout: float = _GUI_READY_TIMEOUT) -> bool:
    """Poll ``/health`` until the daemon accepts connections."""

    import httpx

    deadline = time.time() + timeout
    health = f"{base_url.rstrip('/')}/health"
    while time.time() < deadline:
        try:
            response = httpx.get(health, timeout=1.0)
            if response.status_code == 200:
                return True
        except Exception:
            pass
        time.sleep(0.15)
    return False


def _prefer_manual_browser_open() -> bool:
    """True when ``webbrowser`` would invoke a TUI client or there is no display."""

    import os

    browser = (os.environ.get("BROWSER") or "").lower()
    if any(name in browser for name in ("lynx", "links", "w3m", "curl", "elinks")):
        return True
    return sys.platform == "linux" and not os.environ.get("DISPLAY")


def _launch_gui_when_ready(url: str, token: str) -> None:  # pragma: no cover - UI
    if not _wait_for_server(url):
        warning(
            f"Daemon did not respond within {_GUI_READY_TIMEOUT:.0f}s; "
            f"open manually in a graphical browser: {url.rstrip('/')}/"
        )
        return
    launch_gui(url, token)


def _launch_gui(url: str, token: str) -> None:  # pragma: no cover - UI
    launch_gui(url, token)


def launch_gui(url: str, token: str) -> None:  # pragma: no cover - UI
    """Open the control window.

    ``pywebview`` gives a real OS window via the platform's native webview, but
    its Linux backend needs PyGObject, which is sdist-only and requires a
    compiler plus GTK headers -- an unacceptable install blocker for a hybrid
    team. So pywebview is attempted first and the system browser is the
    fallback, which keeps the UI available everywhere.

    On headless HPC nodes ``BROWSER`` is often ``lynx``; that cannot run the
    control UI, so we print the URL for a browser on the user's laptop instead.
    """

    page = f"{url.rstrip('/')}/"
    target = f"{url}/?token={token}"

    if _prefer_manual_browser_open():
        warning(
            "No graphical browser on this host (often lynx on HPC). "
            f"Open this URL from your laptop browser: {page} "
            "(use Public/Private from the summary if bound with --bind-all, "
            "or SSH port forwarding to 127.0.0.1)."
        )
        return

    try:
        import webview

        webview.create_window("AutoCAB", target, width=980, height=760)
        webview.start()
        return
    except Exception:
        pass
    try:
        import webbrowser

        webbrowser.open(target)
    except Exception:
        warning(f"Open this URL in a browser: {page}")
