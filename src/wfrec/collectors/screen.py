"""Screen collector: frames, change detection, OCR, window titles, video.

Two decisions shape this module.

**OCR is the point, not the video.** The recorder exists to feed a downstream
translation skill, and an LLM cannot watch an mp4. Text extracted from frames
is the consumable artifact; the video is for humans reviewing a session.

**Video is derived from frames, not captured separately.** Rather than three
per-platform ffmpeg capture pipelines (avfoundation / gdigrab / x11grab), the
already-captured frames are stitched with ``-f concat``. That is one code path
on all platforms, adds no second permission surface, and is the only approach
that produces video at all on Wayland -- where the bundled ffmpeg has neither
kmsgrab nor pipewire.
"""

from __future__ import annotations

import contextlib
import os
import subprocess
import sys
import time

from ..events import (
    Event,
    SCREEN_FRAME,
    SCREEN_OCR,
    SCREEN_RECORDING_STARTED,
    SCREEN_RECORDING_STOPPED,
    SCREEN_WINDOW,
)
from ..redaction import shared as shared_redactor
from .base import Collector, CollectorStatus, Degraded
from .window import active_window

FRAME_INTERVAL = 4.0
OCR_MIN_INTERVAL = 15.0
FRAME_MAX_WIDTH = 1600
OCR_MAX_WIDTH = 1600
WEBP_QUALITY = 80

SIGNATURE_SIZE = 128
SIGNATURE_TOLERANCE = 12
#: Fraction of signature pixels that must change for a frame to be kept.
#:
#: Measured against synthetic terminal screens: an identical frame scores
#: 0.000%, a blinking cursor 0.049%, one new line of terminal output 0.623%,
#: and switching from a terminal to an editor 1.025%. 0.2% therefore skips
#: idle redraws while catching every change an analyst would call meaningful.
CHANGE_THRESHOLD = 0.002


def frame_signature(image, size: int = SIGNATURE_SIZE) -> bytes:
    """Downscaled grayscale thumbnail used for change detection."""

    from PIL import Image as _Image

    return image.convert("L").resize((size, size), _Image.BILINEAR).tobytes()


def changed_fraction(
    previous: bytes, current: bytes, tolerance: int = SIGNATURE_TOLERANCE
) -> float:
    """Fraction of signature pixels differing by more than ``tolerance``.

    A difference hash (dHash) was the obvious choice here and is what most
    screenshot tools use, but it is measurably wrong for this workload: an 8x8
    dHash of a 1600px screen scored a Hamming distance of only 1 for a new line
    of terminal output and 2 for switching application -- both of which would
    have been discarded as "unchanged". Text-heavy developer screens need a
    spatial comparison, not a gradient-direction hash.
    """

    if not previous or len(previous) != len(current):
        return 1.0
    differing = sum(1 for a, b in zip(previous, current) if abs(a - b) > tolerance)
    return differing / len(current)


class ScreenCollector(Collector):
    """Captures frames, OCRs the ones that changed, and tracks window titles."""

    source = "screen"
    interval = FRAME_INTERVAL

    def __init__(self, session, *, retain_images: bool = True, video: bool = False) -> None:
        super().__init__(session)
        self._mss = None
        self._Image = None
        self._ocr = None
        self._ocr_backend = ""
        self._last_signature: bytes | None = None
        self._last_ocr = 0.0
        self._last_window: str | None = None
        self._frame_index = 0
        self._retain_images = retain_images
        self._video = video
        self._frames: list[tuple[str, float]] = []
        self._grab = None

    # ------------------------------------------------------------------ probe
    def probe(self) -> CollectorStatus:
        try:
            from PIL import Image
        except ImportError as exc:
            raise Degraded("pillow-missing", str(exc)) from exc
        self._Image = Image

        backend = self._resolve_grabber()
        self._resolve_ocr()

        return CollectorStatus(
            source=self.source,
            available=True,
            backend=backend,
            extra={
                "ocr_backend": self._ocr_backend or "none",
                "retain_images": self._retain_images,
                "video": self._video,
                "window_backend": active_window().backend,
            },
        )

    def _resolve_grabber(self) -> str:
        """Pick a screenshot mechanism, or explain why there is none."""

        wayland = bool(os.environ.get("WAYLAND_DISPLAY")) and not os.environ.get("DISPLAY")
        if wayland:
            # mss has no Wayland backend, and the xdg portal shows a consent
            # dialog per request for an unsandboxed caller -- so unattended
            # capture is not possible. Fail honestly rather than burn CPU.
            raise Degraded(
                "wayland-no-unattended-capture",
                "Wayland exposes no unattended screen capture to ordinary clients. "
                "Log into an Xorg session, or use `wfrec frame` to grab single "
                "frames via the desktop portal (one consent prompt each).",
                fallback="portal-manual",
            )
        if sys.platform not in {"win32", "darwin"} and not os.environ.get("DISPLAY"):
            raise Degraded("no-display", "No DISPLAY and not macOS/Windows.")

        try:
            import mss
        except ImportError as exc:
            raise Degraded("mss-missing", str(exc)) from exc

        try:
            self._mss = mss.mss()
            self._grab = self._mss.grab
            # Probe an actual grab: on macOS this is where a missing Screen
            # Recording grant surfaces, and it must fail at probe time rather
            # than once per frame for the rest of the session.
            self._mss.grab(self._mss.monitors[0])
        except Exception as exc:
            raise Degraded(
                "grab-failed",
                f"{type(exc).__name__}: {exc}. On macOS, grant Screen Recording "
                "in System Settings > Privacy & Security, then restart wfrec "
                "(the permission is cached per process).",
            ) from exc
        return "mss"

    def _resolve_ocr(self) -> None:
        """Pick an OCR backend: Apple Vision on macOS, else RapidOCR."""

        if sys.platform == "darwin":  # pragma: no cover - macOS only
            try:
                import ocrmac  # noqa: F401

                self._ocr_backend = "ocrmac"
                return
            except ImportError:
                pass
        try:
            from rapidocr_onnxruntime import RapidOCR

            # Cap the thread count: the default grabs every core and would make
            # the analyst's own samtools run visibly slower.
            self._ocr = RapidOCR(
                intra_op_num_threads=2, inter_op_num_threads=1
            )
            self._ocr_backend = "rapidocr"
        except Exception:
            self._ocr = None
            self._ocr_backend = ""

    # ------------------------------------------------------------- lifecycle
    def start(self) -> CollectorStatus:
        status = super().start()
        if status.available:
            self.session.record(
                SCREEN_RECORDING_STARTED,
                source=self.source,
                payload={
                    "backend": status.backend,
                    "ocr": self._ocr_backend or "none",
                    "retain_images": self._retain_images,
                },
            )
        return status

    def teardown(self) -> None:
        if self._mss is not None:
            with contextlib.suppress(Exception):  # pragma: no cover
                self._mss.close()
            self._mss = None
        if self._frames:
            self.session.record(
                SCREEN_RECORDING_STOPPED,
                source=self.source,
                payload={"frames": len(self._frames)},
            )
            if self._video:
                self._build_video()

    # ------------------------------------------------------------------ loop
    def _run_once(self) -> None:
        self._track_window()
        if self._grab is None or self._Image is None:
            return

        monitor = self._mss.monitors[0]
        raw = self._grab(monitor)
        image = self._Image.frombytes("RGB", raw.size, raw.bgra, "raw", "BGRX")

        signature = frame_signature(image)
        if self._last_signature is not None:
            delta = changed_fraction(self._last_signature, signature)
            if delta < CHANGE_THRESHOLD:
                # Visually unchanged: the analyst is reading, not acting. This
                # is where the 10-50x disk saving comes from.
                return
        self._last_signature = signature

        if image.width > FRAME_MAX_WIDTH:
            ratio = FRAME_MAX_WIDTH / image.width
            image = image.resize((FRAME_MAX_WIDTH, max(1, int(image.height * ratio))))

        self._frame_index += 1
        stamp = time.time()
        relative = f"screen/frames/{self._frame_index:06d}_{int(stamp)}.webp"
        target = self.session.root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        image.save(target, "WEBP", quality=WEBP_QUALITY, method=4)
        self._frames.append((relative, stamp))

        self.session.writer.append(
            Event(
                source=self.source,
                type=SCREEN_FRAME,
                payload={
                    "index": self._frame_index,
                    "width": image.width,
                    "height": image.height,
                    "bytes": target.stat().st_size,
                },
                ref=relative,
            )
        )

        if self._ocr_backend and (stamp - self._last_ocr) >= OCR_MIN_INTERVAL:
            self._last_ocr = stamp
            self._run_ocr(image, relative)

        if not self._retain_images:
            # Text-only retention: OCR then delete the pixels. This is the
            # right default anywhere PHI could be on screen -- it keeps the
            # signal the pipeline consumes and leaves no image on disk.
            with contextlib.suppress(OSError):
                target.unlink()

    def _track_window(self) -> None:
        info = active_window()
        title = info.title or info.app
        if title == self._last_window:
            return
        self._last_window = title
        redacted = shared_redactor().apply(title or "")
        self.session.writer.append(
            Event(
                source=self.source,
                type=SCREEN_WINDOW,
                payload={
                    "window_title": redacted.text or None,
                    "app": info.app,
                    "backend": info.backend,
                    **({"reason": info.reason} if info.reason else {}),
                },
                redactions=redacted.findings,
            )
        )

    def _run_ocr(self, image, frame_ref: str) -> None:
        """OCR one frame and record the redacted text."""

        text = ""
        try:
            if image.width > OCR_MAX_WIDTH:
                ratio = OCR_MAX_WIDTH / image.width
                image = image.resize((OCR_MAX_WIDTH, max(1, int(image.height * ratio))))
            if self._ocr_backend == "rapidocr" and self._ocr is not None:
                import numpy as np

                result, _ = self._ocr(np.array(image))
                text = "\n".join(line[1] for line in (result or []) if len(line) > 1)
            elif self._ocr_backend == "ocrmac":  # pragma: no cover - macOS only
                from ocrmac import ocrmac as _ocrmac

                buffer = self.session.root / frame_ref
                annotations = _ocrmac.OCR(str(buffer)).recognize()
                text = "\n".join(entry[0] for entry in annotations)
        except Exception as exc:
            self.session.record(
                "collector.error",
                source=self.source,
                payload={"stage": "ocr", "error": f"{type(exc).__name__}: {exc}"},
            )
            return

        if not text.strip():
            return

        redacted = shared_redactor().apply(text)
        relative = frame_ref.replace("screen/frames/", "screen/ocr/").replace(".webp", ".txt")
        target = self.session.root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(redacted.text, encoding="utf-8")
        self.session.writer.append(
            Event(
                source=self.source,
                type=SCREEN_OCR,
                payload={
                    "ocr_text": redacted.text,
                    "chars": len(redacted.text),
                    "frame": frame_ref,
                    "backend": self._ocr_backend,
                },
                redactions=redacted.findings,
                ref=relative,
            )
        )

    # ----------------------------------------------------------------- video
    def _build_video(self) -> None:
        """Stitch captured frames into an mp4 using the bundled ffmpeg."""

        try:
            import imageio_ffmpeg

            exe = imageio_ffmpeg.get_ffmpeg_exe()
        except Exception:
            return
        if len(self._frames) < 2:
            return

        listing = self.session.root / "screen" / "video" / "frames.txt"
        listing.parent.mkdir(parents=True, exist_ok=True)
        lines: list[str] = []
        for index, (relative, stamp) in enumerate(self._frames):
            absolute = (self.session.root / relative).resolve()
            if not absolute.exists():
                continue
            lines.append(f"file '{absolute}'")
            if index + 1 < len(self._frames):
                duration = max(0.04, self._frames[index + 1][1] - stamp)
                lines.append(f"duration {duration:.3f}")
        if len(lines) < 2:
            return
        lines.append(f"file '{(self.session.root / self._frames[-1][0]).resolve()}'")
        listing.write_text("\n".join(lines) + "\n", encoding="utf-8")

        output = self.session.root / "screen" / "video" / "session.mp4"
        cmd = [
            exe, "-hide_banner", "-loglevel", "error", "-y",
            "-f", "concat", "-safe", "0", "-i", str(listing),
            "-vsync", "vfr", "-c:v", "libx264", "-preset", "veryfast",
            "-crf", "28", "-pix_fmt", "yuv420p", "-movflags", "+faststart",
            str(output),
        ]
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
        except (OSError, subprocess.TimeoutExpired) as exc:
            self.session.record(
                "collector.error",
                source=self.source,
                payload={"stage": "video", "error": str(exc)},
            )
            return
        if proc.returncode == 0 and output.exists():
            self.session.record(
                SCREEN_RECORDING_STOPPED,
                source=self.source,
                payload={"frames": len(self._frames), "bytes": output.stat().st_size},
                ref="screen/video/session.mp4",
            )
