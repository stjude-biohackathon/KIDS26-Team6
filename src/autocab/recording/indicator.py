"""Show recording state in the macOS menu bar or Windows system tray.

The indicator runs in a separate process because native tray frameworks own
the main event loop. It polls the control API, which keeps its state aligned
with the CLI and dashboard.
"""

from __future__ import annotations

import contextlib
import time

from . import paths
from .client import Client

POLL_SECONDS = 2.0
UNREACHABLE_LIMIT = 3  # consecutive missed polls before giving up and exiting

RECORDING_COLOR = (255, 59, 48, 255)  # Apple systemRed -- actively capturing
PAUSED_COLOR = (255, 149, 0, 255)  # Apple systemOrange -- session paused
VISIBLE_STATES = ("active", "paused")

_ICON_SUPERSAMPLE = 4  # render this many times larger, then downsample for crisp anti-aliasing


def _load_bold_font(size: int):
    """A real bold system font renders far more crisply at icon sizes than
    Pillow's built-in default font. Tries the common system locations on
    each platform and falls back gracefully if none are found."""

    from PIL import ImageFont

    for path in (
        "/System/Library/Fonts/Supplemental/Arial Bold.ttf",  # macOS
        "/System/Library/Fonts/Helvetica.ttc",  # macOS fallback
        "C:\\Windows\\Fonts\\arialbd.ttf",  # Windows
        "C:\\Windows\\Fonts\\segoeuib.ttf",  # Windows fallback
    ):
        try:
            return ImageFont.truetype(path, size)
        except Exception:
            continue
    try:
        return ImageFont.load_default(size=size)
    except TypeError:  # Pillow < 10.1: load_default() took no size argument
        return ImageFont.load_default()


def _make_icon(color: tuple[int, int, int, int]):
    """A colored disc with an 'AC' (AutoCAB) monogram and a soft highlight --
    a plain dot reads as some generic alert, not as wfrec, and is easy to
    miss or misidentify among other menu-bar icons."""

    from PIL import Image, ImageDraw, ImageFilter

    final_size = 64
    size = final_size * _ICON_SUPERSAMPLE
    margin = 5 * _ICON_SUPERSAMPLE
    image = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    draw.ellipse((margin, margin, size - margin, size - margin), fill=color)

    highlight = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    ImageDraw.Draw(highlight).ellipse(
        (size * 0.20, size * 0.14, size * 0.62, size * 0.46), fill=(255, 255, 255, 130)
    )
    highlight = highlight.filter(ImageFilter.GaussianBlur(size * 0.06))
    image.alpha_composite(highlight)

    text = "AC"
    font = _load_bold_font(24 * _ICON_SUPERSAMPLE)
    bounds = draw.textbbox((0, 0), text, font=font)
    text_w, text_h = bounds[2] - bounds[0], bounds[3] - bounds[1]
    draw.text(
        ((size - text_w) / 2 - bounds[0], (size - text_h) / 2 - bounds[1]),
        text,
        fill=(255, 255, 255, 255),
        font=font,
    )

    rim = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    ImageDraw.Draw(rim).ellipse(
        (margin, margin, size - margin, size - margin),
        outline=(0, 0, 0, 60),
        width=2 * _ICON_SUPERSAMPLE,
    )
    image.alpha_composite(rim)
    return image.resize((final_size, final_size), Image.LANCZOS)


def _status_snapshot() -> tuple[str, str]:
    """Return ``(status, title)``; ``status`` is one of active/paused/idle/unreachable."""

    client = Client.discover()
    if client is None:
        return "unreachable", ""
    try:
        state = client.get("/status")
    except Exception:
        return "unreachable", ""
    session = state.get("session")
    if not session:
        return "idle", ""
    return session.get("status", "idle"), (session.get("title") or "").strip()


def _act(path: str, payload: dict | None = None) -> None:
    client = Client.discover()
    if client is None:
        return
    # The tray menu is a best-effort shortcut, not the source of truth.
    with contextlib.suppress(Exception):
        client.post(path, payload or {})


def _open_dashboard(_icon=None, _item=None) -> None:
    import webbrowser

    client = Client.discover()
    if client is not None:
        webbrowser.open(f"{client.url}/?token={client.token}")


def _overlay_alive() -> bool:
    """Whether a floating-badge process is actually running right now.

    Not the same question as "did the user hide it": the badge can also exit
    on its own (it gives up after losing contact with the daemon for a few
    polls), so relying only on the hidden-preference file would leave no way
    back in that case. Liveness is checked directly instead.

    A bare ``os.kill(pid, 0)`` isn't enough: once the badge process exits,
    its old PID can be reused by a completely unrelated process within
    seconds, and that check can't tell the difference -- it would keep
    reporting "alive" for a process that isn't ours at all. Checking the
    live process's own command line confirms it's actually still wfrec's
    overlay before believing the PID file.
    """

    try:
        pid = int(paths.overlay_pid_path().read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return False

    import psutil

    try:
        cmdline = " ".join(psutil.Process(pid).cmdline())
    except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
        return False
    return "autocab.recording.overlay" in cmdline


def _toggle_overlay(_icon=None, _item=None) -> None:
    """Show or hide the floating badge, based on whether it is actually running.

    Neither direction blocks waiting on the other process: a menu-item
    callback blocking for up to a couple of seconds risks freezing the tray
    icon's own event loop on some backends, and it isn't needed here --
    hiding kills the known PID directly instead of writing a preference and
    waiting for that process to notice on its own next poll, so both
    directions take effect immediately.

    Showing clears the persistent hidden-preference (so the new process
    doesn't immediately exit again) and relaunches it, since a gone process
    -- for any reason -- has to be started fresh.
    """

    if not _overlay_alive():
        with contextlib.suppress(FileNotFoundError):
            paths.overlay_hidden_path().unlink()
        import subprocess
        import sys

        creation: dict = {}
        if sys.platform == "win32":  # pragma: no cover - Windows
            creation["creationflags"] = 0x00000008 | 0x00000200
        else:
            creation["start_new_session"] = True
        with contextlib.suppress(OSError):
            subprocess.Popen(
                [sys.executable, "-m", "autocab.recording.overlay"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                stdin=subprocess.DEVNULL,
                **creation,
            )
    else:
        try:
            pid = int(paths.overlay_pid_path().read_text(encoding="utf-8").strip())
            import psutil

            psutil.Process(pid).terminate()
        except Exception:
            pass  # already gone, or the pid file was stale -- either way, nothing to kill

        from .locking import atomic_write_text

        paths.ensure_home()
        atomic_write_text(paths.overlay_hidden_path(), "1\n")


def _build_menu():
    import pystray

    def label(_item):
        status, title = _status_snapshot()
        if status in VISIBLE_STATES:
            return f"wfrec — {status}: {title or 'Untitled session'}"
        return "wfrec — no active session"

    def overlay_label(_item):
        return "Show floating badge" if not _overlay_alive() else "Hide floating badge"

    return pystray.Menu(
        pystray.MenuItem(label, None, enabled=False),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Pause", lambda *_: _act("/sessions/pause", {"reason": ""})),
        pystray.MenuItem("Resume", lambda *_: _act("/sessions/resume")),
        pystray.MenuItem("Stop session", lambda *_: _act("/sessions/stop")),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Open dashboard", _open_dashboard),
        pystray.MenuItem(overlay_label, _toggle_overlay),
    )


def _watch(icon) -> None:
    last_status = None
    misses = 0
    while True:
        status, title = _status_snapshot()
        if status == "unreachable":
            misses += 1
            if misses >= UNREACHABLE_LIMIT:
                icon.stop()
                return
        else:
            misses = 0
        if status != last_status:
            icon.visible = status in VISIBLE_STATES
            if icon.visible:
                color = RECORDING_COLOR if status == "active" else PAUSED_COLOR
                icon.icon = _make_icon(color)
                icon.title = f"wfrec — {status}" + (f": {title}" if title else "")
            last_status = status
        time.sleep(POLL_SECONDS)


def run() -> int:
    """Run the tray/menu-bar indicator until the process is stopped."""

    import pystray

    icon = pystray.Icon(
        "wfrec", icon=_make_icon(RECORDING_COLOR), title="wfrec", menu=_build_menu()
    )
    icon.visible = False
    icon.run(setup=_watch)
    return 0


if __name__ == "__main__":  # pragma: no cover - manual/process entry point
    import sys

    sys.exit(run())
