"""Show an always-on-top recording badge outside the menu bar.

macOS uses a Cocoa window for transparent rendering. Windows uses Tk and its
transparent-color setting. The badge runs in a separate process so each GUI
event loop owns its main thread.
"""

from __future__ import annotations

import sys

from . import paths
from .client import Client

POLL_SECONDS = 2.0
UNREACHABLE_LIMIT = 3  # consecutive missed polls before giving up and exiting
DRAG_THRESHOLD = 3  # pixels of movement below which a mouse-up counts as a click

BADGE_SIZE = 44
MARGIN = 28  # from the screen edge, for the default top-left placement
TRANSPARENT_KEY = "#123456"  # windows only: an arbitrary color unlikely to appear in the badge

BADGE_COLORS = {
    "active": (255, 59, 48, 255),  # Apple systemRed
    "paused": (255, 149, 0, 255),  # Apple systemOrange
}
LABELS = {"active": "wfrec — recording", "paused": "wfrec — paused"}
_BADGE_SUPERSAMPLE = 4  # render this many times larger, then downsample for crisp anti-aliasing

PULSE_PERIOD_SECONDS = 1.6  # one full breathe-in/breathe-out cycle
PULSE_MIN_ALPHA = 0.55
PULSE_TICK_SECONDS = 0.05  # ~20fps -- smooth without needing a real animation API


def _dismiss_persistently() -> None:
    """Record that the user asked to stop seeing the badge.

    Checked both at startup (so a daemon restart doesn't resurrect a badge
    the user dismissed) and periodically while running (so toggling it off
    from the menu-bar icon's menu takes effect within one poll, without
    needing to kill this process from outside).
    """

    from .locking import atomic_write_text

    paths.ensure_home()
    atomic_write_text(paths.overlay_hidden_path(), "1\n")


def _dismissed() -> bool:
    return paths.overlay_hidden_path().exists()


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


def _open_dashboard() -> None:
    import webbrowser

    client = Client.discover()
    if client is not None:
        webbrowser.open(f"{client.url}/?token={client.token}")


def _tooltip_text(status: str, title: str) -> str:
    label = LABELS.get(status, "wfrec")
    detail = f": {title}" if title else ""
    return f"{label}{detail}  ·  click to open, right-click to hide"


def _make_badge_image(color: tuple[int, int, int, int]):
    """A small glossy sphere: a flat base circle, a blurred highlight near the
    top-left to fake a light source, and a thin dark rim for depth -- reads
    as a polished status light rather than a flat dot."""

    from PIL import Image, ImageDraw, ImageFilter

    final_size = BADGE_SIZE
    size = final_size * _BADGE_SUPERSAMPLE
    margin = 3 * _BADGE_SUPERSAMPLE
    image = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    ImageDraw.Draw(image).ellipse((margin, margin, size - margin, size - margin), fill=color)

    highlight = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    ImageDraw.Draw(highlight).ellipse(
        (size * 0.20, size * 0.14, size * 0.62, size * 0.48), fill=(255, 255, 255, 150)
    )
    highlight = highlight.filter(ImageFilter.GaussianBlur(size * 0.06))
    image.alpha_composite(highlight)

    rim = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    ImageDraw.Draw(rim).ellipse(
        (margin, margin, size - margin, size - margin),
        outline=(0, 0, 0, 70),
        width=2 * _BADGE_SUPERSAMPLE,
    )
    image.alpha_composite(rim)
    return image.resize((final_size, final_size), Image.LANCZOS)


# --------------------------------------------------------------------- macOS
def _run_macos() -> int:
    if _dismissed():
        return 0

    import objc
    from Cocoa import (
        NSApplication,
        NSApplicationActivationPolicyAccessory,
        NSBackingStoreBuffered,
        NSColor,
        NSCompositingOperationSourceOver,
        NSFloatingWindowLevel,
        NSMakeRect,
        NSObject,
        NSScreen,
        NSTimer,
        NSView,
        NSWindow,
        NSWindowCollectionBehaviorCanJoinAllSpaces,
        NSWindowCollectionBehaviorFullScreenAuxiliary,
        NSWindowCollectionBehaviorStationary,
        NSWindowStyleMaskBorderless,
    )

    size = BADGE_SIZE
    images = {}

    def image_for(status: str):
        if status not in images:
            import io

            buffer = io.BytesIO()
            _make_badge_image(BADGE_COLORS[status]).save(buffer, format="PNG")
            from Cocoa import NSData, NSImage

            data = NSData.dataWithBytes_length_(buffer.getvalue(), len(buffer.getvalue()))
            images[status] = NSImage.alloc().initWithData_(data)
        return images[status]

    class BadgeView(NSView):
        def initWithFrame_(self, frame):
            self = objc.super(BadgeView, self).initWithFrame_(frame)
            if self is None:
                return None
            self._image = None
            self._drag_origin = None
            self._window_origin = None
            self._moved = 0.0
            return self

        def setStatusImage_(self, image):
            self._image = image
            self.setNeedsDisplay_(True)

        def drawRect_(self, _rect):
            if self._image is not None:
                self._image.drawInRect_fromRect_operation_fraction_(
                    self.bounds(),
                    NSMakeRect(0, 0, size, size),
                    NSCompositingOperationSourceOver,
                    1.0,
                )

        def mouseDown_(self, event):
            self._drag_origin = event.locationInWindow()
            self._window_origin = self.window().frame().origin
            self._moved = 0.0

        def mouseDragged_(self, event):
            if self._drag_origin is None:
                return
            location = event.locationInWindow()
            dx = location.x - self._drag_origin.x
            dy = location.y - self._drag_origin.y
            self._moved = max(self._moved, (dx * dx + dy * dy) ** 0.5)
            self.window().setFrameOrigin_((self._window_origin.x + dx, self._window_origin.y + dy))

        def mouseUp_(self, _event):
            if self._moved < DRAG_THRESHOLD:
                _open_dashboard()
            self._drag_origin = None

        def rightMouseUp_(self, _event):
            _dismiss_persistently()
            NSApplication.sharedApplication().terminate_(None)

        def acceptsFirstResponder(self):
            return True

    class Poller(NSObject):
        def initWithCallback_(self, callback):
            self = objc.super(Poller, self).init()
            if self is None:
                return None
            self._callback = callback
            return self

        def tick_(self, _timer):
            self._callback()

    app = NSApplication.sharedApplication()
    app.setActivationPolicy_(NSApplicationActivationPolicyAccessory)  # no Dock icon

    screen = NSScreen.mainScreen().frame()
    x = screen.origin.x + MARGIN
    y = screen.origin.y + screen.size.height - size - MARGIN  # top-left corner
    window = NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
        NSMakeRect(x, y, size, size),
        NSWindowStyleMaskBorderless,
        NSBackingStoreBuffered,
        False,
    )
    window.setOpaque_(False)
    window.setBackgroundColor_(NSColor.clearColor())
    window.setLevel_(NSFloatingWindowLevel)
    window.setHasShadow_(True)
    # Without this, NSFloatingWindowLevel only keeps the badge above other
    # windows on the Space it was created on -- switching Spaces or opening a
    # full-screen app would otherwise make it vanish instead of following.
    window.setCollectionBehavior_(
        NSWindowCollectionBehaviorCanJoinAllSpaces
        | NSWindowCollectionBehaviorStationary
        | NSWindowCollectionBehaviorFullScreenAuxiliary
    )

    view = BadgeView.alloc().initWithFrame_(NSMakeRect(0, 0, size, size))
    window.setContentView_(view)

    state: dict = {"status": None, "title": "", "misses": 0}

    def poll() -> None:
        if _dismissed():
            app.terminate_(None)
            return
        status, title = _status_snapshot()
        if status == "unreachable":
            state["misses"] += 1
            if state["misses"] >= UNREACHABLE_LIMIT:
                app.terminate_(None)
                return
        else:
            state["misses"] = 0
        if status != state["status"] or title != state["title"]:
            state["status"], state["title"] = status, title
            if status in BADGE_COLORS:
                view.setStatusImage_(image_for(status))
                view.setToolTip_(_tooltip_text(status, title))
                window.orderFront_(None)
            else:
                window.orderOut_(None)

    import math
    import time

    pulse_start = time.monotonic()

    def pulse() -> None:
        # Only the actively-recording state pulses -- paused stays a steady
        # solid color, so the animation itself signals "live" vs "on hold"
        # rather than just being decorative.
        if state["status"] != "active":
            window.setAlphaValue_(1.0)
            return
        elapsed = time.monotonic() - pulse_start
        phase = (elapsed % PULSE_PERIOD_SECONDS) / PULSE_PERIOD_SECONDS
        wave = 0.5 - 0.5 * math.cos(phase * 2 * math.pi)  # 0 -> 1 -> 0, smooth
        window.setAlphaValue_(PULSE_MIN_ALPHA + (1 - PULSE_MIN_ALPHA) * wave)

    poller = Poller.alloc().initWithCallback_(poll)
    NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
        POLL_SECONDS, poller, "tick:", None, True
    )
    pulser = Poller.alloc().initWithCallback_(pulse)
    NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
        PULSE_TICK_SECONDS, pulser, "tick:", None, True
    )
    app.run()
    return 0


# ------------------------------------------------------------------- Windows
def _run_windows() -> int:  # pragma: no cover - Windows
    if _dismissed():
        return 0

    import math
    import time
    import tkinter as tk

    root = tk.Tk()
    root.overrideredirect(True)
    root.attributes("-topmost", True)
    root.configure(bg=TRANSPARENT_KEY)
    root.wm_attributes("-transparentcolor", TRANSPARENT_KEY)
    root.withdraw()
    root.geometry(f"{BADGE_SIZE}x{BADGE_SIZE}+{MARGIN}+{MARGIN}")  # top-left corner

    label = tk.Label(root, bg=TRANSPARENT_KEY, bd=0, highlightthickness=0, cursor="hand2")
    label.pack()

    photos: dict = {}

    def photo_for(status: str):
        if status not in photos:
            from PIL import ImageTk

            photos[status] = ImageTk.PhotoImage(
                _make_badge_image(BADGE_COLORS[status]), master=root
            )
        return photos[status]

    tooltip_window: dict = {"win": None}

    def show_tooltip(event) -> None:
        hide_tooltip()
        win = tk.Toplevel(root)
        win.overrideredirect(True)
        win.attributes("-topmost", True)
        tk.Label(
            win,
            text=_tooltip_text(current["status"] or "", current["title"]),
            bg="#1a1d24",
            fg="#e7e9ee",
            font=("Helvetica", 11),
            padx=8,
            pady=4,
            bd=0,
        ).pack()
        win.geometry(f"+{event.x_root + 14}+{event.y_root + 12}")
        tooltip_window["win"] = win

    def hide_tooltip(_event=None) -> None:
        if tooltip_window["win"] is not None:
            tooltip_window["win"].destroy()
            tooltip_window["win"] = None

    drag: dict = {"x": 0, "y": 0, "moved": 0.0}

    def on_press(event) -> None:
        drag["x"], drag["y"], drag["moved"] = event.x, event.y, 0.0

    def on_motion(event) -> None:
        dx, dy = event.x - drag["x"], event.y - drag["y"]
        drag["moved"] = max(drag["moved"], (dx * dx + dy * dy) ** 0.5)
        root.geometry(f"+{root.winfo_x() + dx}+{root.winfo_y() + dy}")

    def on_release(_event) -> None:
        if drag["moved"] < DRAG_THRESHOLD:
            _open_dashboard()

    label.bind("<Enter>", show_tooltip)
    label.bind("<Leave>", hide_tooltip)
    label.bind("<ButtonPress-1>", on_press)
    label.bind("<B1-Motion>", on_motion)
    label.bind("<ButtonRelease-1>", on_release)

    def on_dismiss(_event) -> None:
        _dismiss_persistently()
        root.destroy()

    label.bind("<Button-3>", on_dismiss)

    current: dict = {"status": None, "title": ""}
    seen: dict = {"misses": 0}
    pulse_start = time.monotonic()

    def pulse() -> None:
        if current["status"] != "active":
            root.attributes("-alpha", 1.0)
        else:
            elapsed = time.monotonic() - pulse_start
            phase = (elapsed % PULSE_PERIOD_SECONDS) / PULSE_PERIOD_SECONDS
            wave = 0.5 - 0.5 * math.cos(phase * 2 * math.pi)
            root.attributes("-alpha", PULSE_MIN_ALPHA + (1 - PULSE_MIN_ALPHA) * wave)
        root.after(int(PULSE_TICK_SECONDS * 1000), pulse)

    def poll() -> None:
        if _dismissed():
            root.destroy()
            return
        status, title = _status_snapshot()
        if status == "unreachable":
            seen["misses"] += 1
            if seen["misses"] >= UNREACHABLE_LIMIT:
                root.destroy()
                return
        else:
            seen["misses"] = 0
        if status != current["status"] or title != current["title"]:
            current["status"], current["title"] = status, title
            if status in BADGE_COLORS:
                label.configure(image=photo_for(status))
                root.deiconify()
            else:
                hide_tooltip()
                root.withdraw()
        root.after(int(POLL_SECONDS * 1000), poll)

    root.after(0, poll)
    root.after(0, pulse)
    root.mainloop()
    return 0


def run() -> int:
    """Run the floating badge until the process is stopped."""

    import os

    from .locking import atomic_write_text

    paths.ensure_home()
    atomic_write_text(paths.overlay_pid_path(), f"{os.getpid()}\n")

    if sys.platform == "darwin":
        return _run_macos()
    return _run_windows()


if __name__ == "__main__":  # pragma: no cover - manual/process entry point
    sys.exit(run())
