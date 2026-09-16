# AutoCAB × ActivityWatch

Custom session-recording tooling built on top of ActivityWatch, for AutoCAB's activity-capture pipeline stage. Everything here lives on the `explore-activitywatch` branch. For the full story of *why* each piece exists and what was learned building it, see [KNOWLEDGE_BASE.md](KNOWLEDGE_BASE.md) — this file just covers *what's here* and *how to run it*.

## What's in this folder

| Path | What it is |
|---|---|
| `KNOWLEDGE_BASE.md` | Findings, rationale, running log — read this for the "why" |
| `aw-server.toml.reference` | Copy of the real AW server config needed for the custom dashboard visualization + CORS — see below to apply it |
| `scripts/screenshot_ocr_watcher.py` | Capture mode "ocr": periodic screenshot + OCR, posted into AW as a custom bucket, task-labeled |
| `scripts/audio_transcript_watcher.py` | Capture mode "audio": rolling mic recording, transcribed locally (whisper.cpp), posted into AW, task-labeled |
| `scripts/video_watcher.py` | Capture mode "video": real screen recording in rolling chunks, saved to disk, referenced from AW events |
| `models/` | Local Whisper model file(s) for audio transcription — gitignored, download separately (see below) |
| `backend/video_output/` | Recorded video chunks — gitignored, real video files, never committed |
| `visualization/index.html` | Custom AW dashboard panel showing the OCR bucket's events |
| `aw-webui/` | Fork of AW's real dashboard (Vue app) — Start/Stop/session/merge/skill-drafting features are being built directly into the UI, including the **Sessions** tab |
| `backend/session_controller.py` | Flask API (port 5677) the dashboard's Sessions tab calls into. Capture modes (ocr, audio, ...) are a registry here — adding a new one is one dict entry, see KNOWLEDGE_BASE.md |

## Running everything

**1. ActivityWatch itself** (the real app — required first, everything else talks to it):
```bash
open -a ActivityWatch
```
Confirm it's up: `curl -s localhost:5600/api/0/buckets/`

**2. Apply the reference config** (needed once, for the custom dashboard visualization + CORS so the forked dashboard can reach the real server):
```bash
cp activitywatch/aw-server.toml.reference "$HOME/Library/Application Support/activitywatch/aw-server/aw-server.toml"
killall aw-qt && open -a ActivityWatch   # restart to pick it up
```

**3. Our forked dashboard** (Vue app, first time needs `npm install`):
```bash
cd activitywatch/aw-webui
npm install      # first time only
npm run serve    # → http://localhost:27180
```

**4. For the audio capture mode** (one-time setup — skip if only using "ocr"):
```bash
brew install whisper-cpp
mkdir -p activitywatch/models
curl -L -o activitywatch/models/ggml-base.en.bin \
  https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-base.en.bin
```

**5. The session backend** (needed for the Sessions tab's Start/Stop/Resume — see KNOWLEDGE_BASE.md for design details):
```bash
cd activitywatch/backend
python3 session_controller.py   # → http://localhost:5677
```
Then open the dashboard (step 3) and go to the **Sessions** tab in the nav — pick which capture mode(s) to enable there.

**6. Any watcher standalone**, without going through the dashboard (same pattern for either script):
```bash
cd activitywatch/scripts
nohup python3 screenshot_ocr_watcher.py --label "your-task-name" > /tmp/aw_ocr.log 2>&1 &
echo $! > /tmp/aw_ocr.pid
# ... later:
kill "$(cat /tmp/aw_ocr.pid)" && rm /tmp/aw_ocr.pid
```

## Status

This is being filled in as we go — check [KNOWLEDGE_BASE.md](KNOWLEDGE_BASE.md)'s Running Log for what's actually been built and verified so far vs. what's still planned.
