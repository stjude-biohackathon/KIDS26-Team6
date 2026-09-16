"""Prototype ActivityWatch watcher: real screen video recording, posted as a new AW bucket.

Same shape as the other two watchers, one difference: actual video can't be
posted as an AW event (events are small JSON, not binary blobs), so each
chunk is saved to disk under activitywatch/backend/video_output/ and the AW
event just references its path + duration. Records in rolling chunks (like
the audio watcher) rather than one unbounded file for the whole session.

Usage:
    python3 video_watcher.py --label "reproducing-the-bug"
    python3 video_watcher.py --interval 10 --max-iterations 2   # quick test
"""

from __future__ import annotations

import argparse
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from aw_client import ActivityWatchClient
from aw_core.models import Event

BUCKET_NAME = "aw-watcher-video"
OUTPUT_DIR = Path(__file__).resolve().parent.parent / "backend" / "video_output"


def record_video_chunk(path: Path, duration: int, device: str, fps: int) -> None:
    subprocess.run(
        [
            "ffmpeg", "-y",
            "-f", "avfoundation",
            "-i", f"{device}:none",  # video only, no audio track (audio is its own capture mode)
            "-t", str(duration),
            "-r", str(fps),
            "-pix_fmt", "yuv420p",
            str(path),
        ],
        check=True,
        capture_output=True,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--interval", type=int, default=30, help="Seconds per video chunk.")
    parser.add_argument("--max-iterations", type=int, default=0, help="Stop after N chunks (0 = run forever).")
    parser.add_argument("--label", default="", help="Task name to tag every event with.")
    parser.add_argument("--device", default="4", help="ffmpeg avfoundation screen-capture device index (see `ffmpeg -f avfoundation -list_devices true -i \"\"`).")
    parser.add_argument("--fps", type=int, default=10, help="Frames per second (kept low to keep file size down — this is for context, not smooth playback).")
    args = parser.parse_args()

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    client = ActivityWatchClient("aw-watcher-video", testing=False)
    bucket_id = f"{BUCKET_NAME}_{client.client_hostname}"
    client.create_bucket(bucket_id, event_type="video.chunk")
    print(f"Posting to AW bucket: {bucket_id}", flush=True)
    print(f"Video chunks saved under: {OUTPUT_DIR}", flush=True)

    label_slug = (args.label or "session").replace(" ", "-")
    iteration = 0
    with client:
        while True:
            iteration += 1
            timestamp = datetime.now(timezone.utc)
            chunk_path = OUTPUT_DIR / f"{label_slug}_{timestamp.strftime('%Y%m%dT%H%M%S')}.mp4"
            record_video_chunk(chunk_path, args.interval, args.device, args.fps)

            size_kb = chunk_path.stat().st_size / 1024 if chunk_path.exists() else 0
            print(f"[{iteration}] recorded {size_kb:.0f}KB -> {chunk_path.name}", flush=True)

            event = Event(
                timestamp=timestamp,
                duration=args.interval,
                data={"label": args.label, "video_path": str(chunk_path), "duration_seconds": args.interval},
            )
            client.heartbeat(bucket_id, event, pulsetime=args.interval + 1)

            if args.max_iterations and iteration >= args.max_iterations:
                break


if __name__ == "__main__":
    main()
