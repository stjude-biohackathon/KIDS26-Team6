# AutoCAB × ActivityWatch

Custom session-recording tooling built on top of ActivityWatch, for AutoCAB's activity-capture pipeline stage. Everything here lives on the `explore-activitywatch` branch. For the full story of *why* each piece exists and what was learned building it, see [KNOWLEDGE_BASE.md](KNOWLEDGE_BASE.md) — this file just covers *what's here* and *how to run it*.

## What's in this folder

| Path | What it is |
|---|---|
| `KNOWLEDGE_BASE.md` | Findings, rationale, running log — read this for the "why" |
| `aw-server.toml.reference` | Copy of the real AW server config needed for the custom dashboard visualization + CORS — see below to apply it |
| `scripts/screenshot_ocr_watcher.py` | Standalone watcher: periodic screenshot + OCR, posted into AW as a custom bucket, task-labeled |
| `visualization/index.html` | Custom AW dashboard panel showing that OCR bucket's events |
| `aw-webui/` | Fork of AW's real dashboard (Vue app) — this is where Start/Stop/session/merge/skill-drafting features are being built directly into the UI |
| `backend/` | Our own small API service the dashboard calls into to actually start/stop capture and manage sessions *(added as of the session controller phase — see KNOWLEDGE_BASE.md)* |

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

**4. The session backend** *(to be filled in as this phase lands — see KNOWLEDGE_BASE.md for current status)*.

**5. The screenshot+OCR watcher, standalone** (if not driven through the dashboard yet):
```bash
cd activitywatch/scripts
nohup python3 screenshot_ocr_watcher.py --label "your-task-name" > /tmp/aw_ocr.log 2>&1 &
echo $! > /tmp/aw_ocr.pid
# ... later:
kill "$(cat /tmp/aw_ocr.pid)" && rm /tmp/aw_ocr.pid
```

## Status

This is being filled in as we go — check [KNOWLEDGE_BASE.md](KNOWLEDGE_BASE.md)'s Running Log for what's actually been built and verified so far vs. what's still planned.
