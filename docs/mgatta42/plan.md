# `wfrec` — Workflow Recorder Design Plan

**Status:** design draft · **Owner:** Michael Gattas (`mgatta42`) · **Lane:** data/trace (see [`docs/biohackathon-framework.md`](../biohackathon-framework.md))

A cross-platform native recorder that captures a bioinformatician's real working session into a verbose, machine-readable log, plus the `recorder.md` agent skill that drives it in natural language.

---

## 1. Why this exists

AutoCAB's pipeline (`src/autocab/framework/pipeline.py`) runs *ingest → normalize → cluster → redact → match → propose → review → export*. Every stage after ingest works. **Nothing in this repo actually captures a session.** Both existing input adapters are passive consumers of artifacts somebody else produced:

- `load_screen_capture_input()` in `src/autocab/input_sources.py` reads a pre-exported screenpipe-style JSON. Its own docstring concedes: *"This demo expects pre-exported local events rather than controlling a recorder directly."*
- `load_terminal_log_input()` reads `data/sample_terminal_session.log` — **which is not in the repo.** `.gitignore` contains `*.log` (lines 10 and 85), so the fixture was silently dropped in commit `045d19d`. This breaks `tests/test_terminal_logs.py` plus three tests across `test_pipeline.py` / `test_framework.py`, and the commands documented at `README.md:40,59,65`.

The challenge brief makes real observation load-bearing:

> "in the room, a CAB analyst watches AutoCAB process an observed workflow, opens the triage console, picks a proposal, edits it, approves it, and AutoCAB opens a correctly formatted draft pull request… **The full loop, observation to reviewed skill, runs live.**"

Today the observation end of that loop is a missing fixture file. `wfrec` makes it genuine.

It also **displaces the screenpipe dependency** the proposal names. Screenpipe is a heavy Rust daemon, weakest on Windows — a non-starter for a team split across Windows, macOS, and Linux. A pure-Python recorder that every teammate can `pip install` is worth more than a richer one only half the team can run.

### Goal

A per-session folder containing an append-only, **verbose** JSONL event timeline plus sidecar artifacts. Five independently toggleable capture sources. Pause/resume across multiple concurrent sessions. Lossy projections exported into the formats AutoCAB's existing adapters already consume.

The log is deliberately over-complete and never pre-summarized: **downstream skills do the interpreting.** The recorder's only job is to miss nothing.

---

## 2. Decisions

| Decision | Choice | Rationale |
|---|---|---|
| Where work runs | Hybrid: local workstation **and** remote HPC over SSH/SLURM | Real bioinformatics work is submitted to a cluster; a local-only recorder would miss the actual analysis |
| Control surfaces | CLI + GUI + agent skill, all thin clients of one HTTP API | Different moments call for different surfaces; one API means no duplicated logic |
| Screen capture | Screenshots + OCR + optional video | An LLM cannot watch an mp4. OCR text is the consumable artifact; video is for humans |
| GUI stack | pywebview + FastAPI | Pure Python, native OS webview, no Rust/Node toolchain to inflict on the team |
| Dependencies | Declared directly in `pyproject.toml` | The repo currently has zero deps; a real recorder cannot |
| Shell depth | Commands always; full output capture **opt-in** per terminal | Output is high-value signal but multiplies volume and records anything echoed to the terminal |
| Clipboard | Paste box only, no background watching | Automatic clipboard monitoring silently captures password-manager contents |
| File scope | Declared roots, git-aware, size-capped | Genomics files reach 100 GB; a naive watcher fills the disk |
| Multi-analyst | Capture `analyst` identity + `wfrec merge` | Directly serves scored challenge extension (b) |
| Privacy | Non-PHI/synthetic data for the prototype | Matches the brief's hard boundary; see §9 |

---

## 3. Architecture

```
wfrecd — long-lived local daemon (Python)
  ├─ FastAPI control API on 127.0.0.1:<port>   ← single source of truth for all commands
  ├─ SessionStore    → ~/.wfrec/sessions/<session-id>/
  ├─ EventWriter     → append-only events.jsonl, monotonic `seq`, fsync on flush
  ├─ StateFile       → ~/.wfrec/state.json (atomic replace; read by out-of-process hooks)
  └─ Collectors, each independently start/stop-able:
       ScreenCollector · ContextCollector · ShellCollector
       AgentCollector  · FileCollector    · RemoteCollector

Control surfaces (all POST to the same API):
  1. `wfrec` CLI                      — stdlib argparse; the scriptable, demo-safe path
  2. pywebview window                 — renders the FastAPI-served UI in the OS native webview
  3. .claude/skills/recorder/SKILL.md — the "recorder.md" AI skill
```

**Why a daemon plus a state file rather than in-process capture:** shell hooks, SSH sessions, and the GUI all live in different processes from whatever started the recording. One small state file is the only coordination primitive every one of them can read cheaply, without IPC.

### 3.1 Critical design choice: hooks write to disk, never to the daemon

Shell hooks **append to the session's `shell/local.jsonl` themselves** and never call the API. The daemon tails that file into the unified timeline.

This matters for three reasons:

1. **A dead or restarting daemon can never hang an interactive shell.** A hook that blocks on HTTP to a wedged daemon makes every terminal on the machine unusable — an unacceptable failure mode for a tool handed to teammates mid-hackathon.
2. **Toggling a source off is instant for already-open shells**, with no re-`source` of rc files. The hook reads `~/.wfrec/state.json` on each prompt (sub-millisecond, page-cached) and no-ops when `active_session` is `null` or `sources.shell` is `false`. This is what makes "stop bash history" work in the terminal the analyst is already sitting in.
3. Install rc hooks **once**; every later start/stop/pause/toggle is pure state-file mutation.

The hook emits JSON with shell `printf`, **never by invoking Python** — a ~200 ms interpreter start on every prompt would make every terminal feel broken.

---

## 4. Session folder layout

```
~/.wfrec/sessions/2026-09-16T14-03-22_a3f9c1/
├── manifest.json            # id, title, analyst, host, os, lifecycle log, toggle history
├── events.jsonl             # ← THE verbose unified timeline (append-only)
├── screen/
│   ├── frames/000123_1726500000.webp
│   ├── ocr/000123.txt
│   └── video/segment-001.mp4
├── shell/
│   ├── local.jsonl          # written directly by shell hooks
│   ├── output/*.cast        # opt-in full-output capture
│   └── remote/<host>.jsonl  # pulled back from HPC
├── agents/{claude-code,copilot,cursor}/…
├── files/
│   ├── changes.jsonl
│   └── diffs/000045.patch
├── context/notes.jsonl      # pasted context blocks
├── jobs/                    # slurm-*.out, sacct JSON
└── exports/                 # lossy projections for AutoCAB, generated on demand
    ├── autocab-terminal.log
    ├── autocab-screen-capture.json
    └── trace.json
```

Sessions are self-contained and movable — a teammate zips one and hands it to the matching lane.

---

## 5. Unified event schema (`events.jsonl`)

One JSON object per line:

```json
{
  "seq": 1412,
  "ts": "2026-09-16T14:22:03.418Z",
  "session": "2026-09-16T14-03-22_a3f9c1",
  "host": "lab-mbp.local",
  "analyst": "mgatta42",
  "source": "shell",
  "type": "command.completed",
  "payload": {
    "command": "samtools view -c HG008.bam",
    "cwd": "/data/hg008",
    "exit_code": 0,
    "duration_ms": 8421,
    "shell": "zsh",
    "pid": 4411
  },
  "redactions": ["mrn"]
}
```

`seq` is a per-session monotonic counter — the tiebreaker for events sharing a timestamp, and the stable anchor a downstream skill cites when it explains its reasoning. `ts` is UTC ISO-8601 with milliseconds.

**Event types**

| Group | Types |
|---|---|
| Lifecycle | `session.{created,started,paused,resumed,stopped,preempted,waiting}` |
| Toggles | `source.{enabled,disabled}` |
| Shell | `shell.command.{start,completed}` |
| Screen | `screen.{frame,ocr,window,recording.started,recording.stopped}` |
| Context | `context.note` |
| Agents | `agent.message`, `agent.adapter.failed` |
| Files | `file.changed`, `file.diff`, `git.snapshot` |
| Jobs | `job.{submitted,completed}` |
| Manual | `marker.user` |

---

## 6. Session lifecycle and pause semantics

**Exactly one active session; any number paused.** `start` or `resume` of session B while A is active auto-pauses A and writes `session.preempted` to A's timeline, naming B.

This delivers "a session can be resumed or started while another is paused" without ever having to decide which session owns an ambiguous shell event — a question with no correct answer if two sessions could be live at once.

- `wfrec pause --reason "waiting on alignment" --expect 6h` stops every collector and writes `session.paused`.
- **Heartbeat during pause.** `session.waiting` ticks let a downstream skill distinguish *"the analyst waited six hours on a BWA job"* from *"nothing happened."* The wait is itself workflow signal — a silent gap throws it away, and "pause while I wait on a job" is one of the primary use cases.
- `resume` writes `session.resumed` with `gap_ms`, plus **gap reconciliation**: a `git.snapshot` diff of each tracked root taken at pause and again at resume. File changes made during the gap are still recovered in aggregate. Cheap, and it closes the obvious hole in "pause captures nothing."

State file contract — written write-temp-then-`os.replace`, so a hook never reads a torn file:

```json
{
  "active_session": "2026-09-16T14-03-22_a3f9c1",
  "sources": {"screen": false, "shell": true, "files": true, "agents": true, "context": true},
  "api": "http://127.0.0.1:8787",
  "token": "…"
}
```

---

## 7. Capture backends

Every backend follows the same contract: **probe at startup, degrade to a named fallback, and emit `source.disabled` with a reason rather than crashing.** A teammate on an unsupported configuration must still get a usable session.

### 7.1 Screen

- **Frames** — `mss` (pure-Python ctypes over CoreGraphics / GDI / XGetImage). Change-detect on a downscaled grayscale thumbnail and persist only frames that differ past a threshold; this cuts disk 10–50× while the analyst is reading rather than acting. Encode WebP via `Pillow` (~5–10× smaller than PNG).
- **Video, derived rather than captured** — instead of three per-OS ffmpeg capture pipelines (`avfoundation` / `gdigrab` / `x11grab`), stitch the **already-captured frames** through `imageio-ffmpeg` with `-f image2pipe` into hourly mp4 segments. One code path on all platforms, no second permission surface, and it works on Wayland where `x11grab` does not. `imageio-ffmpeg` bundles its own ffmpeg binary, which matters — a system ffmpeg cannot be assumed.
- **Active-window title** — high value for near-zero cost, and it maps straight onto the `window_title` field `load_screen_capture_input()` already reads. No cross-platform library is reliable enough, so use a small per-OS `active_window()`: `ctypes` `GetForegroundWindow`/`GetWindowText` on Windows (no dependency), Quartz `CGWindowListCopyWindowInfo` via `pyobjc-framework-Quartz` on macOS, `xdotool`/`xprop` on X11, `None` on Wayland.
- **OCR** — `rapidocr-onnxruntime`, which is effectively the only option. `pytesseract` requires a system tesseract install (disqualifying for a hybrid team) and `easyocr` pulls ~2 GB of torch. RapidOCR is pip-installable with bundled ONNX models, CPU-only, offline, on all three platforms. Run it in a worker pool **off** the capture path, only on frames that pass change detection, and only over the active-window region when it is known.

### 7.2 Pasted context

A GUI textarea plus `wfrec note "…"`. Everything captured is deliberate, so there is no background clipboard access to justify to security review. The `context.note` payload carries the text, the active window at paste time, and an optional user-supplied label.

### 7.3 Shell

| Shell | Mechanism | Command / cwd / exit / duration |
|---|---|---|
| bash | vendor `bash-preexec.sh` (MIT) | `$BASH_COMMAND`, `$PWD`, `$?`, `EPOCHREALTIME` |
| zsh | native `preexec`/`precmd` arrays | `$1`, `$PWD`, `$?`, `EPOCHREALTIME` |
| fish | `--on-event fish_preexec` / `fish_postexec` | event arg, `$PWD`, `$status`, `$CMD_DURATION` |
| PowerShell | override the `prompt` function | PSReadLine `HistoryInfo` supplies `CommandLine`, `StartExecutionTime`, `EndExecutionTime`, `ExecutionStatus` — duration for free |
| cmd.exe | **no mechanism exists** | unsupported; Windows is PowerShell-only |

**Vendor `bash-preexec.sh` rather than hand-rolling `trap DEBUG`.** The DEBUG trap fires once per simple command in a pipeline, so a naive hook records `samtools view x | head` three separate times. `bash-preexec` already solves that and the other known pitfalls; reimplementing it is a guaranteed afternoon lost to subtle double-recording bugs.

**Opt-in output capture** uses two dependency-free backends: stdlib `pty` on POSIX, writing an asciinema-v2 `.cast` (the format is simple enough to emit directly, so the POSIX-only `asciinema` package is unnecessary), and `Start-Transcript` on Windows PowerShell.

`wfrec hooks install` appends one guarded, marker-delimited source line to `~/.bashrc` / `~/.zshrc` / `~/.config/fish/config.fish` / `$PROFILE`. Idempotent, with a matching `wfrec hooks uninstall`.

### 7.4 Agent / chat history

Failure-isolated adapters — each wrapped so a format change emits `agent.adapter.failed` instead of killing the collector. Files are selected by session time window on mtime, then filtered per-entry by timestamp.

- **Claude Code** — `~/.claude/projects/<url-encoded-cwd>/<session-uuid>.jsonl`. Newline-delimited, one object per turn with `type`, `message.content`, `timestamp`, `cwd`, `sessionId`. By far the best-structured of the three; this is the one to demo.
- **GitHub Copilot Chat (VS Code)** — `<AppData>/Code/User/workspaceStorage/<hash>/chatSessions/*.json` on current VS Code; older versions bury it in `state.vscdb` SQLite under `ItemTable`. Best-effort and version-dependent. Worth prioritizing because [`docs/ai-guidance.md`](../ai-guidance.md) shows the team's assumed agent host is Copilot in VS Code.
- **Cursor** — `<AppData>/Cursor/User/workspaceStorage/<hash>/state.vscdb` (`ItemTable`) and `globalStorage/state.vscdb` (`cursorDiskKV`, `composerData:<uuid>` blobs). The most fragile, and it cloud-syncs. **Open SQLite read-only via `file:…?mode=ro&immutable=1`** so the recorder can never lock the editor's database out from under the analyst.
- **Universal fallback** — `wfrec attach-transcript <file> --tool <name>` plus a GUI drop zone. Always works regardless of format drift, and it is what guarantees the demo survives a Cursor update.

### 7.5 File changes

- `watchdog` (FSEvents / `ReadDirectoryChangesW` / inotify) on roots declared via `wfrec watch ./project`.
- **Ignore aggressively.** Respect `.gitignore` via batched `git check-ignore --stdin`, plus a hard extension denylist — `.bam .cram .sam .fastq .fq .fastq.gz .vcf.gz .bcf .bw .bigwig .h5 .hdf5 .zarr .npy .parquet` — and `.git/` internals, `__pycache__`, `node_modules`, venv/conda directories. A watcher that hashes or diffs a 100 GB BAM fills the disk and stalls the session.
- **Diff only when safe:** text-like (no NUL byte in the first 8 KB) **and** under 256 KB **and** the diff itself under 64 KB. Otherwise metadata-only (`path`, `size`, `mtime`).
- **Debounce** per path over roughly one second — editors write-temp-then-rename, so a single save emits several raw events.
- **Git via `subprocess`**, not `pygit2` (needs libgit2 wheels) or `GitPython` (an extra dependency that still shells out). `git` is present on any bioinformatician's machine and is the most robust option available.
- **HPC caveat:** inotify does **not** fire on NFS or Lustre. Any declared root on a network filesystem silently falls back to periodic `git status` polling, with an event recording that it did so.

### 7.6 Remote / HPC

- `wfrec ssh <host>` wraps the user's own `ssh` and `~/.ssh/config`, and never opens connections beyond the one interactive session plus one pull-back — so ControlMaster and Duo/2FA prompts behave exactly as the analyst expects.
- Bootstrap once: copy `hook.sh` to the remote `~/.wfrec/`, then launch `ssh -t host "export WFREC_SESSION=…; source ~/.wfrec/hook.sh; exec \$SHELL -i"`. Sourcing explicitly rather than relying on `~/.bashrc` avoids the standard non-interactive early-return that would silently skip the hook.
- The remote hook writes remote-side JSONL; on pause/stop, `rsync --append` (or `scp` as fallback) pulls it into `shell/remote/<host>.jsonl`.
- **SLURM:** on seeing `sbatch` or `srun`, capture the job id; on pause/stop run `sacct -j <ids> --json` (falling back to `--parsable2` on older SLURM) and copy `slurm-<jobid>.out` into `jobs/`. Emit `job.submitted` / `job.completed`. This is what turns "pause while waiting on a job" into real data instead of a gap.

### 7.7 Platform matrix

| Source | Windows | macOS | Linux X11 | Linux Wayland |
|---|---|---|---|---|
| Frames | `mss` | `mss` (TCC prompt) | `mss` | ⚠ `grim`/portal subprocess |
| Video | frames → `imageio-ffmpeg` | same | same | same ✅ |
| Window title | `ctypes` GDI | `pyobjc` Quartz | `xdotool` | ❌ unavailable |
| OCR | `rapidocr-onnxruntime` | same | same | same |
| Shell | PowerShell only | bash/zsh/fish | bash/zsh/fish | bash/zsh/fish |
| Output capture | `Start-Transcript` | stdlib `pty` | stdlib `pty` | stdlib `pty` |
| Files | `watchdog` | `watchdog` | `watchdog` (⚠ inotify limits) | same |
| Agents | AppData paths | `~/Library/…` | `~/.config/…` | same |
| Remote | OpenSSH (Win10+) | ✅ | ✅ | ✅ |

New `pyproject.toml` dependencies: `fastapi`, `uvicorn`, `pywebview`, `mss`, `Pillow`, `rapidocr-onnxruntime`, `imageio-ffmpeg`, `watchdog`, `httpx`; plus `pyobjc-framework-Quartz` under a `sys_platform == "darwin"` marker.

---

## 8. AutoCAB integration

The verbose timeline is the source of truth. **The AutoCAB export is an explicitly lossy projection.** `WorkflowStep` in `src/autocab/models.py` has only four fields (`timestamp`, `tool`, `action`, `detail`), so exit codes, durations, diffs, and OCR all collapse into `detail` or are dropped entirely.

Keeping the two representations separate is deliberate: **do not widen `WorkflowStep` to fit the recorder.** The pipeline's model is tuned for clustering and matching; the recorder's is tuned for completeness. Conflating them would degrade both.

### Three exporters, each targeting code that already exists

1. **`exports/autocab-terminal.log`** → `convert_terminal_log()` in `src/autocab/terminal_logs.py`. The parser is strict: `METADATA_PATTERN` accepts only `# key: value`, `TIMESTAMP_PATTERN` accepts only `YYYY-MM-DDTHH:MM:SS <command>` (no timezone, no sub-second), and **any other line raises `ValueError`**. The exporter must second-truncate its timestamps and emit exactly that shape, using only the recognized metadata keys: `analyst`, `title`, `workflow_family`, `trace_id`, `summary`, `tags`, `source`.
2. **`exports/autocab-screen-capture.json`** → `load_screen_capture_input()`, which reads `{"sessions":[{…,"events":[{timestamp,tool,action,window_title,ocr_text,notes}]}]}`. OCR text and window titles map directly onto `ocr_text` and `window_title`. This is the payoff for choosing OCR over video-only.
3. **New `src/autocab/session_bundle.py`** → `load_session_input(path) -> InputBundle`, the native full-fidelity path.

### Wiring — three small edits

- `src/autocab/framework/bootstrap.py:27` — add `"session": SessionBundleInputAdapter()` to the `input_adapters` dict.
- `src/autocab/cli.py:40` — add `"session"` to the `--input-mode` choices and a `--session-dir` argument.
- `src/autocab/orchestrator.py:11` — thread a `session_path` parameter through `run_pipeline` into `pipeline.run`.

This follows the extension point `docs/biohackathon-framework.md` already documents: *"Add a new input mode by implementing an `InputAdapter`."*

### Reuse the redaction layer; do not rebuild it

`SensitiveDataRedactor` in `src/autocab/framework/components.py` already covers email, `SJ\d{4,8}`, MRN, and DOB, and `PipelineConfig.redact_deny_terms` in `framework/config.py` holds `("patient","diagnosis","pathology")`. Route recorder text — shell, context, OCR — through it at capture time, and record which patterns fired in each event's `redactions` array.

### Multi-analyst — challenge extension (b)

`manifest.json` records an `analyst` identity that flows into `WorkflowTrace.analyst`, which `WorkflowClusterer` **already groups on**. `wfrec merge <folders…>` combines teammates' session folders into a single export, so workflows repeated across several analysts separate cleanly from one-off idiosyncratic ones.

### Repo fixes this unblocks *(applied)*

- The recorder regenerates the missing `data/sample_terminal_session.log`, repairing four broken tests. Requires a `!data/sample_terminal_session.log` negation in `.gitignore`.
- `docs/ai-guidance.md` links to a nonexistent `AGENTS.md`.
- `README.md` and `docs/biohackathon-framework.md` both reference `AutoCAB-challenge.docx`; the real filenames are `AutoCAB-challenge-description.docx` and `AutoCAB-challenge-submission-questions.docx`.

---

## 9. Privacy posture

The prototype targets **non-PHI inputs only**, matching the brief's hard boundary: *"synthetic or volunteer-consented screen recordings made while analyzing public datasets."*

That said, extension (c) — *"harden the privacy and redaction layer for a pediatric hospital environment"* — is a **scored deliverable**, and wiring the existing `SensitiveDataRedactor` into the recorder costs only a few hours. It stays in scope (Phase 3).

Genuinely deferred, and documented here as the path to real use:

- Screen-capture application denylisting with auto-pause (Epic, REDCap, password managers)
- Encryption at rest for session folders
- A prominent panic-pause in the GUI
- Security review / IRB documentation before the tool is ever pointed at real clinical work

Everything is local-only with no network egress. That story needs to be ready, because a daemon that screenshots, watches the filesystem, and hooks shells looks exactly like an infostealer to hospital endpoint security.

---

## 10. The `recorder.md` skill

Lives at `.claude/skills/recorder/SKILL.md`, with a Copilot-compatible copy since `docs/ai-guidance.md` points the team at VS Code Copilot agent-skills.

Note the frontmatter convention is **not** what `src/pr_generator/__init__.py::_skill_markdown` emits — that template uses `name`/`slug`/`proposal_type` for *generated proposals*. An agent-host skill needs `name` + `description`.

The skill body is deliberately thin: a verb table mapping natural language onto `wfrec` CLI calls, because the CLI is already the stable contract. The skill should not reimplement recorder logic.

| Analyst says | Skill runs |
|---|---|
| "start recording my variant calling work" | `wfrec start --title "…" --analyst …` |
| "start / stop screen recording" | `wfrec source screen {on,off}` |
| "stop logging my bash history" | `wfrec source shell off` |
| "pause, I'm waiting on the alignment job" | `wfrec pause --reason "…" --expect 6h` |
| "note that I reran this because the BAM was truncated" | `wfrec note "…"` |
| "what are you recording right now?" | `wfrec status --json` |
| "wrap up and hand this to AutoCAB" | `wfrec stop && wfrec export --format autocab` |

---

## 11. Phases

| # | Window | Deliverable |
|---|---|---|
| 0 | 0–4h | `src/wfrec/` skeleton: state file, SessionStore, EventWriter, manifest, CLI, FastAPI control API. Runs on all three platforms, captures nothing. **Validates the session/pause/preempt model before taking on any OS-specific risk.** |
| 1 | 4–12h | Shell hooks (bash/zsh/PowerShell) + file watcher + paste box + pywebview GUI |
| 2 | 12–24h | Screen frames + active-window titles + OCR; derived video last |
| 3 | 24–36h | Agent transcript adapters + the three exporters + `session_bundle.py` wired into bootstrap + redaction wiring |
| 4 | 36–48h | SSH wrapper + SLURM job metadata pull-back |
| 5 | 48h+ | `recorder.md` skill, `wfrec merge`, regenerated sample fixture, demo script, docs |

Ordering is by **risk-adjusted demo value**. The event model and shell capture carry the demo, so they come first. Screen video is the most likely thing to break on an unfamiliar OS, so it is last within its phase — if it slips, nothing else slips with it.

---

## 12. Verification

- **`pytest`** — the existing 16 tests stay green, *including the four currently broken* by the missing fixture. Tests must run from the repo root with `PYTHONPATH=src`, because they import `autocab` / `aggregation` / `redaction` as top-level modules and `test_terminal_logs.py` uses a relative fixture path.
- **New unit tests** — event-schema round-trip; the pause/resume/preempt state machine; and strict-format conformance of the terminal-log exporter, asserted by **feeding its output back through `parse_terminal_session()`**. Round-tripping proves the format rather than eyeballing it.
- **Manual cross-platform smoke**, one teammate per OS:
  `wfrec start` → run three commands in an **already-open** shell → `wfrec source screen on` → paste context → `wfrec pause --reason x` → run a command that must **not** appear → `wfrec resume` → `wfrec stop` → inspect `events.jsonl`.
- **Second-session check** — `wfrec start` a new session mid-flight; confirm `session.preempted` lands in the first session's timeline and that subsequent shell events attribute to the second.
- **End-to-end** — `wfrec export --format autocab`, then `autocab demo --input-mode session --session-dir <folder>` produces a draft `SKILL.md` under `skills/generated-drafts/`.

---

## 13. Open risks

| Risk | Impact | Mitigation |
|---|---|---|
| **Wayland** — `mss` has no Wayland backend; window titles need a GNOME extension | Degraded screen capture for a Linux teammate | `grim`/portal subprocess fallback; video derived from frames works regardless |
| **Endpoint security** — screenshots + filesystem watching + shell hooks reads as an infostealer | A teammate's machine flags the daemon | Local-only, no network egress; have the explanation ready before the event |
| **Agent transcript formats** for Cursor and Copilot are undocumented and version-dependent | Adapters silently break | Failure-isolated adapters; manual `attach-transcript` fallback always works |
| **Install weight** — `rapidocr-onnxruntime` + `imageio-ffmpeg` is a few hundred MB | Hackathon wifi | Pre-download wheels before the event |
| **`cmd.exe` cannot be hooked** | Windows users on cmd get no shell capture | Document PowerShell-only up front rather than discovering it during the demo |
| **inotify does not fire on NFS/Lustre** | Silent loss of file events on HPC-mounted roots | Detect network filesystems and fall back to `git status` polling, recording that it happened |
