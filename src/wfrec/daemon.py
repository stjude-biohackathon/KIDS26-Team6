"""The wfrec daemon: control API plus collector supervision."""

from __future__ import annotations

import secrets
import signal
import socket
import threading
from typing import Any

from . import paths
from .recorder import Recorder
from .state import StateTransaction, clear_api, mark_boot, publish_api, read_api

DEFAULT_PORT = 8787


def free_port(preferred: int = DEFAULT_PORT) -> int:
    """Return ``preferred`` if bindable, otherwise an OS-assigned port."""

    for candidate in (preferred, 0):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            try:
                sock.bind(("127.0.0.1", candidate))
                return sock.getsockname()[1]
            except OSError:
                continue
    return preferred


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
        try:
            paths.pid_path().unlink()
        except FileNotFoundError:
            pass
        return {"stopped": False, "pid": pid, "reason": "process not running; cleaned up stale files"}
    except PermissionError:
        return {"stopped": False, "pid": pid, "reason": "not permitted to signal that process"}

    return {"stopped": True, "pid": pid}


def run(port: int | None = None, *, open_gui: bool = False) -> int:
    """Run the daemon in the foreground until interrupted."""

    import uvicorn

    from .api import create_app

    paths.ensure_home()

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
    chosen = port or free_port()
    url = f"http://127.0.0.1:{chosen}"
    publish_api(url, token)
    with StateTransaction() as state:
        state.api_url = url
        state.api_token = token

    app = create_app(recorder, token)
    config = uvicorn.Config(
        app, host="127.0.0.1", port=chosen, log_level="warning", access_log=False
    )
    server = uvicorn.Server(config)

    def shutdown(_signum: int, _frame: Any) -> None:
        server.should_exit = True

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, shutdown)
        except (ValueError, OSError):  # pragma: no cover - non-main thread
            pass

    print(f"wfrec daemon listening on {url}")
    print(f"  session folder root: {paths.sessions_dir()}")
    print(f"  open the UI with:    wfrec gui")

    if open_gui:
        threading.Thread(target=_launch_gui, args=(url, token), daemon=True).start()

    try:
        server.run()
    finally:
        recorder.shutdown()
        clear_api()
        try:
            paths.pid_path().unlink()
        except FileNotFoundError:
            pass
    return 0


def _launch_gui(url: str, token: str) -> None:  # pragma: no cover - UI
    launch_gui(url, token)


def launch_gui(url: str, token: str) -> None:  # pragma: no cover - UI
    """Open the control window.

    ``pywebview`` gives a real OS window via the platform's native webview, but
    its Linux backend needs PyGObject, which is sdist-only and requires a
    compiler plus GTK headers -- an unacceptable install blocker for a hybrid
    team. So pywebview is attempted first and the system browser is the
    fallback, which keeps the UI available everywhere.
    """

    target = f"{url}/?token={token}"
    try:
        import webview

        webview.create_window("wfrec", target, width=980, height=760)
        webview.start()
        return
    except Exception:
        pass
    try:
        import webbrowser

        webbrowser.open(target)
    except Exception:
        print(f"Open this URL in a browser: {target}")
