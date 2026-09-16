"""Active window title, per platform.

Deliberately hand-rolled rather than using ``pywinctl`` or ``pygetwindow``:
``pywinctl`` drags in the entire ``pyobjc`` meta-package on macOS (hundreds of
megabytes of framework bindings for one string), and ``pygetwindow`` is
sdist-only, unmaintained, and has no Linux support.

The window title is the highest value-per-byte signal the screen source
produces, and it maps directly onto the ``window_title`` field that
``autocab.input_sources.load_screen_capture_input`` already reads.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from dataclasses import dataclass


@dataclass(slots=True)
class WindowInfo:
    """The focused window, as far as the platform will tell us."""

    title: str | None
    app: str | None = None
    backend: str = ""
    reason: str = ""

    def to_dict(self) -> dict[str, str | None]:
        payload: dict[str, str | None] = {"window_title": self.title, "app": self.app}
        if self.reason:
            payload["reason"] = self.reason
        return payload


def _linux_wayland() -> bool:
    return bool(os.environ.get("WAYLAND_DISPLAY")) and not os.environ.get("DISPLAY")


def _windows() -> WindowInfo:  # pragma: no cover - Windows only
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.windll.user32
    handle = user32.GetForegroundWindow()
    if not handle:
        return WindowInfo(None, backend="win32", reason="no-foreground-window")
    length = user32.GetWindowTextLengthW(handle)
    buffer = ctypes.create_unicode_buffer(length + 1)
    user32.GetWindowTextW(handle, buffer, length + 1)
    pid = wintypes.DWORD()
    user32.GetWindowThreadProcessId(handle, ctypes.byref(pid))
    app = None
    try:
        import psutil

        app = psutil.Process(pid.value).name()
    except Exception:
        app = None
    return WindowInfo(buffer.value or None, app=app, backend="win32")


def _macos() -> WindowInfo:  # pragma: no cover - macOS only
    try:
        from Quartz import (
            CGWindowListCopyWindowInfo,
            kCGNullWindowID,
            kCGWindowListOptionOnScreenOnly,
        )
    except ImportError:
        return _macos_app_only("quartz-unavailable")

    try:
        windows = CGWindowListCopyWindowInfo(
            kCGWindowListOptionOnScreenOnly, kCGNullWindowID
        )
    except Exception as exc:
        return _macos_app_only(f"quartz-failed:{type(exc).__name__}")

    for entry in windows or []:
        if entry.get("kCGWindowLayer", 1) != 0:
            continue
        title = entry.get("kCGWindowName")
        owner = entry.get("kCGWindowOwnerName")
        if title:
            return WindowInfo(str(title), app=str(owner) if owner else None, backend="quartz")
        if owner:
            # kCGWindowName is withheld without the Screen Recording grant, and
            # macOS shows no prompt for it -- so titles silently come back
            # empty. Report the app name and say why rather than look broken.
            return WindowInfo(
                None,
                app=str(owner),
                backend="quartz",
                reason="no-screen-recording-permission",
            )
    return _macos_app_only("no-onscreen-window")


def _macos_app_only(reason: str) -> WindowInfo:  # pragma: no cover - macOS only
    """Frontmost app name via NSWorkspace, which needs no permission at all."""

    try:
        from AppKit import NSWorkspace

        app = NSWorkspace.sharedWorkspace().frontmostApplication()
        name = app.localizedName() if app else None
        return WindowInfo(None, app=str(name) if name else None, backend="nsworkspace", reason=reason)
    except Exception:
        return WindowInfo(None, backend="none", reason=reason)


def _linux_x11() -> WindowInfo:
    try:
        from Xlib import display as xdisplay
        from Xlib import X
    except ImportError:
        return _linux_x11_cli()

    try:
        disp = xdisplay.Display()
        root = disp.screen().root
        prop = root.get_full_property(
            disp.intern_atom("_NET_ACTIVE_WINDOW"), X.AnyPropertyType
        )
        if not prop or not prop.value:
            return WindowInfo(None, backend="xlib", reason="no-active-window")
        window = disp.create_resource_object("window", prop.value[0])
        for atom_name in ("_NET_WM_NAME", "WM_NAME"):
            value = window.get_full_property(
                disp.intern_atom(atom_name), X.AnyPropertyType
            )
            if value and value.value:
                raw = value.value
                title = raw.decode("utf-8", "replace") if isinstance(raw, bytes) else str(raw)
                return WindowInfo(title, backend="xlib")
        return WindowInfo(None, backend="xlib", reason="no-title-property")
    except Exception as exc:
        return _linux_x11_cli(f"xlib-failed:{type(exc).__name__}")


def _linux_x11_cli(reason: str = "xlib-unavailable") -> WindowInfo:
    """Fallback to xdotool when python-xlib is missing or errors."""

    if shutil.which("xdotool"):
        try:
            out = subprocess.run(
                ["xdotool", "getactivewindow", "getwindowname"],
                capture_output=True,
                text=True,
                timeout=2,
            )
            if out.returncode == 0 and out.stdout.strip():
                return WindowInfo(out.stdout.strip(), backend="xdotool")
        except (OSError, subprocess.TimeoutExpired):
            pass
    return WindowInfo(None, backend="none", reason=reason)


def active_window() -> WindowInfo:
    """Best-effort focused-window info for the current platform."""

    if sys.platform == "win32":  # pragma: no cover
        try:
            return _windows()
        except Exception as exc:
            return WindowInfo(None, reason=f"win32-failed:{type(exc).__name__}")
    if sys.platform == "darwin":  # pragma: no cover
        return _macos()
    if _linux_wayland():
        # No Wayland protocol exposes the focused window's title to an ordinary
        # client. GNOME requires a shell extension; KWin requires scripting
        # over D-Bus. Returning null with a reason beats returning a stale X11
        # value, which is what the X11 path would do here.
        return WindowInfo(None, backend="none", reason="wayland-unsupported")
    if os.environ.get("DISPLAY"):
        return _linux_x11()
    return WindowInfo(None, backend="none", reason="no-display")
