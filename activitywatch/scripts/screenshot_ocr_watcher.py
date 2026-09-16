"""Prototype ActivityWatch watcher: screenshot + OCR, posted as a new AW bucket.

Three off-the-shelf pieces glued together, nothing custom:
  1. `screencapture` (built into macOS) takes the screenshot.
  2. `pytesseract` (wrapping the `tesseract` binary) extracts text from it.
  3. `aw_client` posts that text to the local ActivityWatch server as an event.

Meant to be started right when a task begins and stopped right when it ends (see
docs/activitywatch/README.md "Task-scoped recording" for the exact start/stop commands) —
not left running continuously. Each event is tagged with --label so it's clear afterward
which task slot it belongs to.

Usage:
    python3 screenshot_ocr_watcher.py --label "fixing-terminal-log-bug"   # runs until stopped (Ctrl+C)
    python3 screenshot_ocr_watcher.py --interval 10 --max-iterations 3    # quick test, no label needed
"""

from __future__ import annotations

import argparse
import subprocess
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

import pytesseract
from aw_client import ActivityWatchClient
from aw_core.models import Event
from PIL import Image

BUCKET_NAME = "aw-watcher-screenocr"


def take_screenshot(path: Path) -> None:
    # -x = no camera sound, silent capture
    subprocess.run(["screencapture", "-x", str(path)], check=True)


def ocr_text(path: Path) -> str:
    return pytesseract.image_to_string(Image.open(path)).strip()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--interval", type=int, default=15, help="Seconds between captures.")
    parser.add_argument("--max-iterations", type=int, default=0, help="Stop after N captures (0 = run forever).")
    parser.add_argument("--label", default="", help="Task name to tag every event with, e.g. 'fixing-terminal-log-bug'.")
    args = parser.parse_args()

    client = ActivityWatchClient("aw-watcher-screenocr", testing=False)
    bucket_id = f"{BUCKET_NAME}_{client.client_hostname}"
    client.create_bucket(bucket_id, event_type="ocr.text")
    print(f"Posting to AW bucket: {bucket_id}", flush=True)

    iteration = 0
    with client:
        while True:
            iteration += 1
            with tempfile.TemporaryDirectory() as tmpdir:
                shot_path = Path(tmpdir) / "shot.png"
                take_screenshot(shot_path)
                text = ocr_text(shot_path)

            preview = (text[:120] + "...") if len(text) > 120 else text
            print(f"[{iteration}] captured {len(text)} chars of text. Preview: {preview!r}", flush=True)

            event = Event(timestamp=datetime.now(timezone.utc), data={"text": text, "label": args.label})
            client.heartbeat(bucket_id, event, pulsetime=args.interval + 1)

            if args.max_iterations and iteration >= args.max_iterations:
                break
            time.sleep(args.interval)


if __name__ == "__main__":
    main()
