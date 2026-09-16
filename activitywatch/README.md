# ActivityWatch

Working notes on ActivityWatch — what it is, what it can and can't do, how it was installed and verified on this machine, and how it fits into AutoCAB. Lives on the `explore-activitywatch` branch and grows as more gets tested.

## What it is

ActivityWatch is a free, open-source, **local-first automated time tracker** (activitywatch.net), MPL-2.0 licensed. It runs on Windows, macOS, Linux, and Android. All data is stored on the device it runs on and is never uploaded anywhere — there's no account, no cloud sync, no server component outside your own machine.

## Architecture

- **`aw-qt`** — the tray/menu-bar manager. Launching the app starts this, and it spawns everything else.
- **`aw-server`** — the local REST API and storage layer, served at `http://localhost:5600`. This is what the web UI and any API queries talk to.
- **Watchers** — modular processes, each logging one activity stream:
  - `aw-watcher-window` (on by default) — active app name + window title + timestamp/duration
  - `aw-watcher-afk` (on by default) — away-from-keyboard/idle detection
  - Browser extension (Chrome/Firefox) — tab titles + URLs. Separate install, not on by default.
  - VS Code / editor plugin — editor activity. Separate install, not on by default.
- **Web UI** — served by `aw-server` at `localhost:5600`, tabs: Activity, Timeline, Stopwatch, Raw Data, Settings.

## What it can do

- Passive capture of active app + window title + duration, continuously, with no manual start/stop needed.
- AFK/idle detection out of the box.
- Browser tab titles and URLs, once the browser extension is installed.
- Editor activity (e.g. which file is open in VS Code), once the editor plugin is installed.
- **Stopwatch** — a manual, explicit start/stop timer (optionally labeled) for marking a clean task boundary. Logged to its own separate bucket from the passive watchers.
- **REST API**:
  - `GET /api/0/buckets/` — list all data streams ("buckets")
  - `GET /api/0/buckets/<bucket-id>/events` — get events, with optional `start`/`end` (ISO 8601) to slice by time range and `limit` to cap results
  - `GET /api/0/export` — export *all* buckets as one JSON file
  - `GET /api/0/buckets/<bucket-id>/export` — export a single bucket
- **Export via web UI**: Raw Data tab → export one bucket, or "Export all buckets as JSON" for everything. Downloads through the browser like any file (default Downloads folder), then can be moved anywhere.
- **Pausing a single watcher** without quitting everything: tray icon → Modules → uncheck the one you want off. ActivityWatch's own docs describe this as "a low-tech solution to filter sensitive data."

## What it can't do

- No screen recording and no screenshots.
- No OCR of on-screen text/content.
- No keystroke logging or clipboard capture.
- No built-in redaction — whatever's in a window title gets logged as-is unless that watcher is paused.
- No automatic task-boundary detection. The default model is continuous passive logging; Stopwatch is the one manual exception.
- No cloud sync or multi-device merging by default — purely local, per-machine.

### vs. screenpipe

| | ActivityWatch | screenpipe |
|---|---|---|
| Captures | App name + window title + duration | Full screen recording + OCR of everything visible |
| Privacy risk | Low — structured metadata only | High — reads screen pixels, real PHI risk on a hospital workstation |
| Richness | Coarse (what app/window, not what's in it) | Rich (the actual text/numbers on screen) |
| Weight | Lightweight background daemon | Heavier, continuous recording |

**Manual start/stop comparison**: screenpipe also has a tray timer for manually starting/stopping recording, but it's scoped specifically to *meetings* (its transcription feature), plus a separate general pause/resume toggle for the main continuous capture — not a generic labeled task-timer like AW's Stopwatch. (From screenpipe's docs/GitHub, not hands-on testing — it isn't installed in this project.)

Beifang's framing from the #team6 Slack channel: agent/terminal logs can capture the main scripted pipeline, while something like screenpipe helps catch the "shadow activity" in between (the manual steps, Excel edits, browser lookups). ActivityWatch sits as the safer, cheaper middle ground for that shadow-activity layer — enough signal to reconstruct a rough workflow timeline without screenpipe's redaction burden.

## Installation (macOS)

```bash
brew install --cask activitywatch
```

This installs `ActivityWatch.app` to `/Applications`, wrapping the same official release available as a `.dmg` from activitywatch.net — functionally identical either way. Homebrew is the better choice here for reproducibility (a one-line setup instruction for teammates), easy upgrades (`brew upgrade --cask activitywatch`), and easy removal (`brew uninstall --cask activitywatch`).

Confirmed on this machine: `brew info --cask activitywatch` showed **0.13.2**, matching the version listed on activitywatch.net at install time.

**First-launch permission**: macOS will prompt for Accessibility or Screen Recording access under **System Settings → Privacy & Security**. This is required for `aw-watcher-window` to read the frontmost window's title — without it, window-title logging silently doesn't work.

## Managing it (start / stop / restart)

Launch:
```bash
open -a ActivityWatch
```

This starts four processes. Confirm they're running:
```bash
ps aux | grep -i "aw-qt\|aw-server\|aw-watcher"
```
Expect to see `aw-qt`, `aw-server`, `aw-watcher-window`, and `aw-watcher-afk`.

Stop everything:
```bash
killall aw-qt
```
This takes down its spawned children too. If anything lingers:
```bash
pkill -f aw-server; pkill -f aw-watcher-window; pkill -f aw-watcher-afk
```

Pause just one watcher (without a full quit): tray icon → **Modules** → uncheck.

**Menu bar icon gotcha (hit on this machine)**: on a notched MacBook Air, the tray icon can get silently pushed off-screen when the menu bar is crowded. If it's not visible, fall back to **Activity Monitor** (search "aw" to confirm/force-quit processes) or the terminal commands above rather than hunting for the icon.

**Verified behaviors**:
- Restarting does **not** lose history — every event is written continuously to a local database as it happens, not held in memory until a clean quit. `open -a ActivityWatch` after a kill brings back all prior data and resumes logging.
- Killing it makes the `localhost:5600` browser tab show `ERR_CONNECTION_REFUSED` — expected, since nothing's listening on that port anymore, not a sign anything is broken. Reload the tab after restarting.

## Working with the data

Web UI tabs at `localhost:5600`: **Activity** (daily summary — top apps, top window titles, timeline barchart), **Timeline**, **Stopwatch**, **Raw Data** (per-bucket browsing + export), **Settings**.

REST API, commands actually run and confirmed working:
```bash
# list buckets
curl -s localhost:5600/api/0/buckets/ | python3 -m json.tool

# sample events from one bucket
curl -s "localhost:5600/api/0/buckets/<bucket-id>/events?limit=5" | python3 -m json.tool

# time-range slice = "everything that happened during one task"
curl -s "localhost:5600/api/0/buckets/<bucket-id>/events?start=2026-09-16T11:30:00&end=2026-09-16T11:45:00"
```

Exporting to an arbitrary folder (both confirmed working):
```bash
# GUI: Raw Data tab -> "Export all buckets as JSON" (downloads to browser's Downloads folder, then move it)

# Direct, saves exactly where you want, no move step:
curl -s "http://localhost:5600/api/0/export" -o /any/path/you/want/aw_export.json
curl -s "http://localhost:5600/api/0/buckets/<bucket-id>/export" -o /any/path/you/want/aw_bucket.json
```

ActivityWatch's own docs advise aggregating/redacting data locally before sharing raw exports with anyone — directly relevant to AutoCAB's own redaction layer, see [Working with AutoCAB](#how-this-fits-into-autocab) below.

## How this fits into AutoCAB

Named directly by Wojciech in the `#team6` Slack channel (Sept 8) alongside screenpipe, as one of two candidate tools for the pipeline's very first step: capturing what an analyst actually did, upstream of clustering/redaction/matching. This is also the subject of Michael Gattas's open action item from the Sept 14 team meeting — researching cross-platform session-recording technologies and how to standardize the event stream format.

**Code-side integration point**: the pipeline already has three `InputAdapter`s implementing the same `Protocol` interface —`TraceInputAdapter`, `ScreenCaptureInputAdapter`, `TerminalLogInputAdapter` (see [contracts.py](../src/autocab/framework/contracts.py), [components.py](../src/autocab/framework/components.py), [input_sources.py](../src/autocab/input_sources.py)). A future `ActivityWatchInputAdapter` would be a natural fourth: pull events from the REST API for a given time window, map each window-title event into a `WorkflowStep`, group them into a `WorkflowTrace` — the same shape `ScreenCaptureInputAdapter` already uses for [data/sample_screen_capture.json](../data/sample_screen_capture.json) (see [data/README.md](../data/README.md) for the existing sample-input convention to follow).

## Extending it (custom watchers)

No fork of the core repo needed — every watcher (`aw-watcher-window`, `aw-watcher-afk`, `aw-watcher-web`, the VS Code plugin) is a separate standalone program that just talks to `aw-server`'s local REST API. `aw-server` doesn't care where events come from.

To write one: use the official `aw-client` Python library, create a named bucket, then send data via **heartbeats** (a heartbeat identical to the last one extends its duration; a different one starts a new event — this is the recommended pattern, it saves storage/disk IO):
```python
from aw_client import ActivityWatchClient

client = ActivityWatchClient("aw-watcher-screenocr", testing=False)
bucket_id = f"{client.client_name}_{client.client_hostname}"
client.create_bucket(bucket_id, event_type="ocr.text")

with client:
    client.heartbeat(bucket_id, {"text": text, "app": frontmost_app}, pulsetime=10)
```

A **screenshot + OCR watcher** (capture via macOS `screencapture`/`mss`, OCR via `pytesseract` or macOS Vision, then post the extracted text the same way) is a real, buildable extension — this is not native to AW today, by deliberate design.

**Two ways to ship a custom watcher**: (1) publish it as your own standalone companion repo/script depending on `aw-client` — this is how `aw-watcher-web` and the VS Code plugin already exist, as separate repos under the ActivityWatch org, not inside the core monorepo; no permission needed. (2) Actually fork the core repo — license-wise fine (MPL-2.0, weak copyleft, no obligation to publish private modifications), but unnecessary here since nothing in core needs changing.

**Not a formal plugin system**: no manifest, no registration, no marketplace, no sandboxing. There are three separate, independently-open entry points: (1) **data capture** — any process posting to the REST API, what we built; (2) **data analysis** — the web UI's Query Explorer, using AQL (ActivityWatch Query Language) to aggregate/filter any bucket including custom ones, no registration needed; (3) **custom visualizations** — a watcher can in theory supply its own dashboard panel for its bucket type, but the maintainers call this **experimental** — don't rely on it; a custom bucket just renders as plain data in Raw Data view by default.

**Caveat**: a screenshot+OCR watcher is functionally equivalent to screenpipe — same capability, same risk profile — which is exactly what picking ActivityWatch was meant to avoid by default (see the comparison table above). Fine to build and test against personal, non-sensitive activity, but should never run against a real St. Jude workstation or have its output shared/committed without passing through a redaction layer first (see [redaction/__init__.py](../src/redaction/__init__.py) for the existing one).

## Prototype: screenshot + OCR watcher

Built as `scripts/screenshot_ocr_watcher.py` (same folder). Three off-the-shelf pieces, no AW source touched: `screencapture` (macOS built-in) for the screenshot, `pytesseract` + `tesseract` (`brew install tesseract`, `pip install pytesseract pillow`) for OCR, `aw_client` (`pip install aw-client`) to post the extracted text as a heartbeat event into a new `aw-watcher-screenocr_<hostname>` bucket.

**Confirmed fully working end-to-end (2026-09-16)**. After granting Screen Recording permission to Terminal (System Settings → Privacy & Security → fully quit/reopen Terminal for it to take effect), hit one more bug: the installed `aw_client` version's `heartbeat()` requires an `aw_core.models.Event` object, not a plain dict — fixed by constructing `Event(timestamp=..., data={"text": text})` instead of passing a raw dict. After that fix, a full cycle (screenshot → OCR → post) ran clean and the event was verified sitting in the bucket via `GET .../events`.

**Task-scoped start/stop verified working**: backgrounded via `nohup ... &` + PID file, tagged events with `--label`, confirmed via the API that labeled events landed correctly, then killed cleanly via the PID. Hit and fixed one snag: Python fully buffers stdout when it isn't a real terminal (i.e. once redirected to a log file via `nohup`), so the log file stayed empty while the process was clearly alive and working — fixed by adding `flush=True` to the `print()` calls. Test buckets from this round of verification were deleted afterward (`DELETE /api/0/buckets/<id>?force=1`) rather than left sitting around, consistent with the redaction/cleanup point below.

**Real privacy lesson from the first live capture**: the OCR text pulled in genuinely unrelated content from screen — sidebar project names from an unrelated notes app (`TigerMind`, `CheXnet_FL`, `IPP_Project handoff rev`, etc.), alongside the actual Claude Code conversation. This is a live demonstration, not a hypothetical, of why raw screen-capture OCR output must never be shared/exported/committed without passing through redaction first — it captures everything visible, indiscriminately, with zero awareness of what's actually relevant. Test bucket should be deleted after experimentation (`DELETE /api/0/buckets/<id>?force=1`) rather than left sitting around.

## Custom dashboard visualization (built and verified working)

Our OCR bucket doesn't have to sit as raw JSON in Raw Data view — AW supports **custom visualizations**, a real (if under-documented) feature: a watcher can ship its own static HTML/JS page, and `aw-server` will serve it directly, discoverable from inside the dashboard's own view editor (Activity view → **Edit view** → **Add visualization** → cogwheel → **Custom visualization**). Confirmed via a real precedent already shipping in the ecosystem — `aw-watcher-input` ships one — so this isn't speculative.

**No build step needed** — despite `aw-watcher-input`'s example using a Pug+browserify pipeline, plain vanilla HTML/JS is explicitly supported ("as long as you have static content at the end"). Built as one self-contained file: [`visualization/index.html`](visualization/index.html) — plain `fetch()` calls against `aw-server`'s own REST API (same-origin, since `aw-server` serves the page itself, so no CORS issues), rendering a simple dark-themed table of timestamp/label/OCR-text.

**Wiring it up**:
1. In `aw-server.toml` (**not** `aw-qt.toml`) — at `~/Library/Application Support/activitywatch/aw-server/aw-server.toml`, **outside this repo**, AW's own local config — add under `[server.custom_static]`:
   ```toml
   [server.custom_static]
   aw-watcher-screenocr = "/absolute/path/to/activitywatch/visualization"
   ```
2. Restart AW (`killall aw-qt` then `open -a ActivityWatch`) for the config to take effect.

**Confirmed working end-to-end (2026-09-16)**: verified the static file is actually served (`curl -L localhost:5600/pages/aw-watcher-screenocr/` → HTTP 200, correct content, redirected from the `.../index.html` form). Loaded it in a real browser: empty-state message rendered correctly with no bucket present, no console errors; then ran the watcher for one real capture and reloaded — the table populated with the correct timestamp, label, and OCR text, fetched live from the real API. Test bucket deleted afterward per the cleanup habit below.

## Task-scoped recording (start with the task, stop with the task)

The script runs continuously once started, which isn't what we want by default — the goal is to capture *one task's slot*, not the whole day. So start it right when a task begins and stop it right when it ends, tagging it with `--label` so it's clear afterward which task each event belongs to.

**Start** (runs in the background, keeps going after this terminal command returns):
```bash
cd activitywatch/scripts
nohup python3 screenshot_ocr_watcher.py --label "your-task-name" > /tmp/aw_ocr.log 2>&1 &
echo $! > /tmp/aw_ocr.pid
```

**Stop** (run this the moment the task is done):
```bash
kill "$(cat /tmp/aw_ocr.pid)" && rm /tmp/aw_ocr.pid
```

Everything captured between those two commands is that task's slot. `/tmp/aw_ocr.log` has the running print output if you want to check it's still alive; `/tmp/aw_ocr.pid` just holds the process id so `stop` knows what to kill.

## Open questions / not yet tested

- Browser extension (tab titles + URLs) — not yet installed or tested.
- VS Code editor plugin — not yet installed or tested.
- Whether window-title granularity alone is rich enough signal for the clustering step, or whether it needs augmenting with terminal/shell history.
- Whether multi-day continuous background capture is practical for the actual 72-hour hackathon window.

## Running log

- **2026-09-16** — Installed via Homebrew (`brew install --cask activitywatch`, v0.13.2, matches activitywatch.net). Confirmed all 4 processes running via `ps aux`. Confirmed web UI at `localhost:5600` logging real events (`Claude`, `Terminal`, `Google Chrome` window titles). Confirmed `killall aw-qt` cleanly stops it (browser tab correctly shows `ERR_CONNECTION_REFUSED` afterward — expected). Confirmed restart (`open -a ActivityWatch`) preserves all prior history. Confirmed both export paths (`GET /api/0/export`, per-bucket export, and the Raw Data tab's "Export all buckets as JSON" button).
- **2026-09-16** — Clarified content depth, since this came up directly: even with the browser extension installed, ActivityWatch never captures page/window *content* — only app name, window title, URL (browser only, extension required), and duration. Confirmed via the `aw-watcher-web` source/docs: it watches "title, URL, audible and incognito state," nothing about page content, clicks, or typed text. Same ceiling applies to the Claude window — the watcher sees `{"app": "Claude", "title": "...", "duration": ...}`, never the actual prompts/responses/tool calls. This directly matters for AutoCAB's real need (actual workflow *steps*, not just "which app had focus"): AW alone only gives a coarse passive timeline skeleton. The content-rich sources are elsewhere — [terminal_logs.py](../src/autocab/terminal_logs.py) for real shell command history, and Claude Code's own local session transcripts (prompts/tool calls/responses) for what happens inside an AI-agent session, matching Wojciech's Slack point about "collecting the prompts we use to interact with LLMs." Realistic model: AW = passive skeleton timeline (what app, when, how long); terminal logs + Claude Code transcripts = the actual semantic content.
