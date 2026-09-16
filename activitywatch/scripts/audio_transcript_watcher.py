"""Prototype ActivityWatch watcher: mic audio + local transcription, posted as a new AW bucket.

Same shape as screenshot_ocr_watcher.py, three off-the-shelf pieces glued together:
  1. `ffmpeg` (avfoundation) records a rolling chunk from the microphone.
  2. `whisper-cli` (whisper.cpp, fully local — no cloud API) transcribes it.
  3. `aw_client` posts the transcript text as an event into a new AW bucket.

Runs in short chunks (near-real-time, not true streaming) so a task's spoken
narration ends up alongside its OCR/window-title data as context for skill
drafting later.

Usage:
    python3 audio_transcript_watcher.py --label "explaining-the-fix"
    python3 audio_transcript_watcher.py --interval 10 --max-iterations 2   # quick test
"""

from __future__ import annotations

import argparse
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from aw_client import ActivityWatchClient
from aw_core.models import Event

BUCKET_NAME = "aw-watcher-audiotranscript"
DEFAULT_MODEL = Path(__file__).resolve().parent.parent / "models" / "ggml-base.en.bin"


def record_audio_chunk(path: Path, duration: int, device: str) -> None:
    subprocess.run(
        [
            "ffmpeg", "-y",
            "-f", "avfoundation",
            "-i", f":{device}",
            "-ar", "16000", "-ac", "1",
            "-t", str(duration),
            str(path),
        ],
        check=True,
        capture_output=True,
    )


def transcribe(path: Path, model: Path) -> str:
    result = subprocess.run(
        ["whisper-cli", "-m", str(model), "-f", str(path), "-nt", "-np"],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--interval", type=int, default=15, help="Seconds per audio chunk.")
    parser.add_argument("--max-iterations", type=int, default=0, help="Stop after N chunks (0 = run forever).")
    parser.add_argument("--label", default="", help="Task name to tag every event with.")
    parser.add_argument("--device", default="1", help="ffmpeg avfoundation audio device index (see `ffmpeg -f avfoundation -list_devices true -i \"\"`).")
    parser.add_argument("--model", type=Path, default=DEFAULT_MODEL, help="Path to a whisper.cpp ggml model file.")
    args = parser.parse_args()

    if not args.model.exists():
        raise SystemExit(f"Model not found at {args.model} — download one to activitywatch/models/ first.")

    client = ActivityWatchClient("aw-watcher-audiotranscript", testing=False)
    bucket_id = f"{BUCKET_NAME}_{client.client_hostname}"
    client.create_bucket(bucket_id, event_type="audio.transcript")
    print(f"Posting to AW bucket: {bucket_id}", flush=True)

    iteration = 0
    with client:
        while True:
            iteration += 1
            with tempfile.TemporaryDirectory() as tmpdir:
                chunk_path = Path(tmpdir) / "chunk.wav"
                record_audio_chunk(chunk_path, args.interval, args.device)
                text = transcribe(chunk_path, args.model)

            preview = (text[:120] + "...") if len(text) > 120 else text
            print(f"[{iteration}] transcribed {len(text)} chars. Preview: {preview!r}", flush=True)

            if text:
                event = Event(timestamp=datetime.now(timezone.utc), data={"text": text, "label": args.label})
                client.heartbeat(bucket_id, event, pulsetime=args.interval + 1)

            if args.max_iterations and iteration >= args.max_iterations:
                break


if __name__ == "__main__":
    main()
