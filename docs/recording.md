# AutoCAB workflow recorder

Records a bioinformatician's working session into a folder containing an
append-only, deliberately verbose JSONL timeline plus sidecar artifacts, so
AutoCAB can draft a `SKILL.md` from real work instead of a fixture file.

The recorder is AutoCAB's observation front end. Use `autocab record` for the
common lifecycle. The `wfrec` compatibility command provides the advanced
capture, hook, remote, and diagnostic controls documented below.

Design rationale, the per-OS backend matrix and the known platform limits live
in [`docs/mgatta42/plan.md`](mgatta42/plan.md). This file is how to
use it.

---

## Install

Install [uv](https://docs.astral.sh/uv/getting-started/installation/), then
create the project environment:

```bash
uv venv
source .venv/bin/activate
uv pip install -e '.[macos,gui]'   # Linux: '.[linux,gui]'   Windows: '.[gui]'
```

**Without uv** (typical on HPC — use Python **3.10+**, not the cluster default
`python3` if it is 3.6/3.8):

```bash
python3.11 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip setuptools wheel
pip install -e '.[linux,dev]'    # HPC login node; quote in zsh
```

**Quote the extras.** In zsh, `uv pip install -e .[macos,gui]` fails with
`zsh: no matches found` because brackets are glob characters.

Extras are optional but worth having:

| Extra | Gives you |
|---|---|
| `macos` | Window titles via Quartz, plus Apple Vision OCR (faster than the bundled RapidOCR, and needs no permission prompt) |
| `linux` | X11 window titles (`python-xlib`) and the Wayland desktop-portal screenshot path (`jeepney`) |
| `gui` | A native app window via `pywebview`. Without it the UI opens in your browser, which works everywhere — this extra is optional *because* `pywebview`'s Linux backend needs PyGObject, which is sdist-only and wants a compiler |

The first install pulls a few hundred megabytes (ONNX OCR models and a bundled
ffmpeg binary). Do it before an event, not on event wifi.

## Check the machine first

```bash
wfrec doctor
```

Run this before anything else, on every machine. It reports which backend each
source resolved to and **why** anything degraded, which is the difference
between diagnosing a problem and describing symptoms over Slack. With three
operating systems and two display servers, "works on my machine" is the most
expensive bug class here.

## Install the fallback shell hook when needed

DevSQL with Atuin is the primary local shell backend. Run `wfrec doctor` first.
Install a hook only when the reported shell backend is `hook-spool`:

```bash
wfrec hooks install                # detects zsh/bash/fish, or PowerShell on Windows
wfrec hooks status
wfrec hooks uninstall
```

This appends one guarded, marker-delimited `source` line to your rc file and
takes a timestamped backup first. The hook body itself lives in
`~/.autocab/hooks/`, so your rc file gains two reviewable lines rather than a few
hundred of someone else's code.

On Windows, one installation updates the current-user all-hosts profiles for
both PowerShell 7 (`Documents/PowerShell/profile.ps1`) and Windows PowerShell
5.1 (`Documents/WindowsPowerShell/profile.ps1`). This keeps the hook available
when teammates use different PowerShell generations on the same machine.

Installing only affects **new** terminals. To start capturing in the terminal
you are already sitting in:

```bash
eval "$(wfrec hooks eval --shell zsh)"
```

In PowerShell, run:

```powershell
Invoke-Expression (wfrec hooks eval --shell powershell)
```

Fallback hooks are installed once and left alone. Every later start, stop,
pause, and source toggle reaches already-open shells at their next prompt.

## Record

```bash
wfrec start --title "HG008 variant QC" --watch .
```

`--watch` matters: without a declared project root the `files` source reports
`no-watch-roots` and captures nothing. `--title` becomes the workflow family
AutoCAB clusters on, and `--analyst` (defaulting to your OS username) is what
makes multi-analyst aggregation work.

`start` auto-spawns the background daemon; its log is `~/.autocab/daemon.log`.
Pass `--no-daemon` to skip background collectors. Installed fallback hooks can
still spool commands for later ingestion.

Then, at any point mid-session:

```bash
wfrec source screen on            # or off -- any source, any time
wfrec source shell off            # "stop logging my bash history"
wfrec note "reran because the BAM was truncated" --label why
wfrec mark "QC finished"
wfrec watch ../other-project
wfrec pause --reason "waiting on bwa" --expect 6h
wfrec resume
wfrec status
wfrec stop
```

Toggles and pause take effect in terminals that are **already open** — no
restarting and no re-sourcing.

**Always pass `--reason` to `pause`.** It is what lets a downstream skill
distinguish *"the analyst waited six hours on an alignment job"* from
*"nothing happened"*. A pause without a reason throws that signal away.

## The five sources

| Source | Captures | Notes |
|---|---|---|
| `shell` | Commands, cwd, exit codes, durations | DevSQL with Atuin is primary; installed hooks provide the fallback. Stdout and stderr are not captured |
| `files` | Git-verified changes and diffs in declared roots | `watchdog` triggers, `git` verifies. Genomics binaries are metadata-only, never opened |
| `agents` | Codex, Claude Code, Copilot Chat and Cursor activity | Codex uses DevSQL messages and completed tool executions; Claude Code uses its local JSONL transcripts; `attach-transcript` is the manual fallback |
| `screen` | Frames, OCR text, active-window titles, optional video | OCR is the part a downstream LLM can read; video is for humans |
| `context` | A paste box and `wfrec note` | Everything captured is deliberate. There is no background clipboard watching, by design |

Exactly one session is active at a time; any number may be paused. Starting or
resuming another auto-pauses the current one and records the handover on both
timelines — so either timeline alone explains what happened.

## Look at what you captured

```bash
wfrec sessions
wfrec events --limit 40
wfrec events --source shell
wfrec events --type git. --json
```

A session folder is self-contained and movable — zip one and hand it to a
teammate:

```text
~/.autocab/sessions/<id>/
├── manifest.json     # title, analyst, host, lifecycle log, toggle history
├── events.jsonl      # the verbose timeline (source of truth)
├── screen/           # frames, ocr text, video
├── shell/            # DevSQL cursor and pulled-back remote spools
├── agents/           # transcript captures
├── files/            # change events and diffs
├── context/          # pasted notes
├── jobs/             # slurm output slices and accounting
└── exports/          # lossy projections for AutoCAB
```

`events.jsonl` is append-ordered by `seq`, which is **not** the same as
chronological: a shell hook stamps a command the instant it finishes, but the
daemon ingests it up to a second later. Sort by `(ts, seq)` if you need
chronology — `EventWriter.read_sorted()` does, and so does every exporter.

## Hand off to AutoCAB

```bash
wfrec export
autocab demo --input-mode session --session-dir ~/.autocab/sessions/<id>
```

`wfrec export` prints the exact `autocab` command with the id filled in. Drafts
land in `skills/generated-drafts/`.

The portable event document is lossless. The other three files target existing
AutoCAB adapters:

| Format | Consumed by |
|---|---|
| `events.json` | Complete session timeline, metadata, and seal provenance |
| `terminal.log` | `autocab.terminal_logs.convert_terminal_log` |
| `screen-events.json` | `autocab.input_sources.load_screen_capture_input` |
| `workflow-trace.json` | `autocab.demo_data.load_workflow_traces` |

The three AutoCAB adapter formats are **deliberately lossy**.
`autocab.models.WorkflowStep` has exactly four fields (`timestamp`, `tool`,
`action`, `detail`), so exit codes, durations, diffs and OCR collapse into
`detail` or are dropped. The session folder stays the source of truth; do not
widen `WorkflowStep` to fit the recorder.

You can also skip `export` entirely — `--input-mode session` reads the session
folder directly and rebuilds from the live timeline rather than trusting a
possibly-stale export.

## The GUI

```bash
wfrec daemon --gui        # start the daemon and open the window
wfrec gui                 # attach to a daemon that is already running
```

### Remote browser on HPC (cluster IP)

By default the control API listens on **loopback only**. To open the web UI from
your laptop while the daemon runs on a cluster node (similar to a Dash app on a
node IP), bind all interfaces and publish a URL your browser can reach:

```bash
export WFREC_ALLOW_REMOTE=1
export WFREC_BIND_HOST=0.0.0.0
export WFREC_ADVERTISE_URL="http://YOUR_NODE_IP:8787"

wfrec daemon              # or: wfrec start … (auto-spawn reads those env vars)
```

Or explicitly:

```bash
wfrec daemon --bind-all --allow-remote \
  --advertise-url "http://YOUR_NODE_IP:8787" --port 8787
```

Do **not** rely on ``--gui`` on a headless login node: ``BROWSER`` is often
``lynx``, which cannot run the control UI. Use ``wfrec daemon`` without
``--gui`` and open the **Public** / **Private** URL from the summary in a
browser on your laptop (or use SSH ``-L`` to loopback).

Then open the **Public** or **Private** URL from the daemon summary (or
**`WFREC_ADVERTISE_URL`**) on your machine. The startup table lists
``hostname -I`` addresses: field 1 = site/public, field 2 = internal/private.
Ensure the site firewall allows that port on the node.

**Security:** the UI page embeds the API token. Non-loopback bind means anyone
who can reach the port can control the recorder. Prefer **`ssh -L 8787:127.0.0.1:8787 login-node`**
when that is enough. Only use `--bind-all` on trusted networks.

#### HPC: background daemon and recording

Keep the daemon running after you disconnect, then drive capture from the CLI or
the web UI:

```bash
source /path/to/KIDS26-Team6/.venv/bin/activate

export WFREC_ALLOW_REMOTE=1
export WFREC_BIND_HOST=0.0.0.0
# optional if auto-detect is wrong:
# export WFREC_ADVERTISE_URL="http://10.x.x.x:8787"

nohup wfrec daemon >> ~/.autocab/daemon.log 2>&1 &
# tail -f ~/.autocab/daemon.log   # Host / Public / Private URLs appear at startup

wfrec doctor
wfrec hooks install             # if shell backend is hook-spool
eval "$(wfrec hooks eval)"      # in each shell that should record commands

wfrec start --title "HG008 QC" --watch .
# … work …
wfrec stop

wfrec daemon --stop             # when done for the day
```

`wfrec start` can auto-spawn a daemon if none is running; on HPC it is clearer
to start **`nohup wfrec daemon &`** first so background collectors stay up and
the startup summary (with **Local**, **Private**, **Public** URLs) is in
`~/.autocab/daemon.log`.

Per-source toggle switches, a paste box, session controls and a live event
tail. It is a client of the same HTTP control API the CLI and the agent skill
use, so a click and a command do the same thing.

## Multiple analysts

Challenge extension (b). Each session records its own `analyst`, and
`autocab.framework.components.WorkflowClusterer` already groups by workflow
family while collecting the distinct analysts per cluster.

```bash
wfrec merge <id1> <id2> <id3> --output merged.json --workflow-family "hg008 qc"
autocab demo --input-mode session --session-dir ~/.autocab/sessions  # a folder of sessions
```

`merge` reports which families more than one analyst performed — that is the
high-value skill-candidate signal. `--workflow-family` forces a shared family
so sessions people titled differently still cluster together.

## Remote / HPC

```bash
wfrec ssh hpc-login                    # bootstrap a POSIX hook, open a recorded shell
wfrec pull hpc-login --job 4213        # fold remote commands and job metadata in
```

This wraps *your* `ssh` and `~/.ssh/config`, so ProxyJump, agent forwarding and
Duo/2FA behave exactly as you already expect. `sbatch` is wrapped on the remote
so a job is captured at **submit** time, which is the only moment `scontrol`
will tell you the real `StdOut`/`StdErr`/`WorkDir` paths rather than guessing
`slurm-%j.out`. Job output is pulled as a bounded head-and-tail slice, never
whole — a long alignment's log can be gigabytes.

Writing to `~/.bashrc` on shared infrastructure is a change your colleagues may
also live with. `wfrec ssh` only ever touches the host you explicitly name, and
the block it adds is plain text and marker-delimited.

This path is the least-proven part of the tool: it could not be tested against
a real cluster. Try it early.

## Platform reality

| Source | macOS | Windows | Linux X11 | Linux Wayland |
|---|---|---|---|---|
| shell | DevSQL + Atuin; hook fallback | **PowerShell hook fallback**; `cmd.exe` cannot be hooked | DevSQL + Atuin; hook fallback | same |
| screen frames | `mss` (needs Screen Recording) | `mss` | `mss` | **unavailable unattended** |
| window titles | Quartz (needs Screen Recording) | ctypes GDI | `python-xlib` | **unavailable** |
| OCR | Apple Vision or RapidOCR | RapidOCR | RapidOCR | RapidOCR |
| files / agents / context / remote | yes | yes | yes | yes |

**macOS Screen Recording is cached per process.** Granting it while the daemon
is running does nothing; you must restart the daemon:

```bash
wfrec source screen on       # triggers the prompt
# grant it in System Settings > Privacy & Security > Screen Recording
wfrec daemon --stop
wfrec daemon
```

Without the grant, `kCGWindowName` returns empty **with no prompt at all**, so
titles silently come back blank. `wfrec` falls back to the app name and records
the reason rather than looking broken.

**Wayland cannot do unattended screen capture.** `mss` has no Wayland backend,
the bundled ffmpeg has neither `kmsgrab` nor pipewire, and the desktop portal
prompts for consent on every request. Everything else works normally; plan a
screen-capture demo on an Xorg session.

## Privacy posture

The prototype targets **non-PHI inputs only** — synthetic or public datasets,
matching the challenge brief's hard boundary.

**Two layers, and they do different jobs.**

*Inline, at capture.* Shell commands, notes, OCR text, agent transcripts, file
paths, git diffs, remote command output and SLURM accounting fields all go
through `autocab.deid` on the way in, and each event records which rules fired
in its `redactions` array. This is masking: `[REDACTED_MRN]`, not reversible,
not linkable.

*The post-capture check.* `autocab record redact <id>` walks every payload of every
event plus `context/`, `screen/ocr/`, `files/diffs/`, `shell/remote/` and
`jobs/`, and rewrites what it finds to per-run surrogates — `MRN_a7f3c14b9e02`
— so a value stays linkable *within* the session and unrecoverable outside it.
The key is `secrets.token_bytes(32)`, never written to disk, and zeroed when the
process ends. **No reverse map is produced, ever**; `deid/audit.jsonl` records
labels, offsets and a `value_id` integer, never the matched text and never a
digest of it (a six-digit MRN brute-forces against a bare SHA-256 in
milliseconds). This pass is deterministic and local. It uses patterns by
default. To add the local GLiNER model, install its pinned files once and
select it explicitly:

```bash
autocab deid fetch
autocab deid verify
autocab record redact <id> --engine gliner
```

The model lives under `~/.autocab/models/` or `$AUTOCAB_HOME/models/`. Package
installation, the dashboard, and ordinary regex sealing never download model
data. Use `autocab deid fetch --bundle gliner-model.zip` followed by
`autocab deid load gliner-model.zip` for an offline computer.

A PII-tuned GLiNER2 model is also available as a local sealing option:

```bash
uv sync --extra deid-gliner2
autocab deid fetch --model gliner2-pii
autocab deid eval --engine gliner2-pii-only
autocab deid eval --engine gliner2-pii
autocab deid benchmark --engine gliner2-pii
autocab record redact <id> --engine gliner2-pii
```

Its pinned files live under the same `~/.autocab/models/` root. The production
seal keeps the pattern-matching floor and adds GLiNER2 findings.

The model architecture is from Zaratiana U, Tomeh N, Holat P, and Charnois T,
“GLiNER: Generalist Model for Named Entity Recognition using Bidirectional
Transformer,” arXiv:2311.08526 (2023),
<https://arxiv.org/abs/2311.08526>.

The PII model is from Zaratiana U, Lewis A, and Hurn-Maloney G,
“GLiNER2-PII: A Multilingual Model for Personally Identifiable Information
Extraction,” arXiv:2605.09973 (2026), <https://arxiv.org/abs/2605.09973>.

**A successful pattern check is mandatory before anything consumes a session.**
For a paused or archived unsealed session, `wfrec export` copies the manifest
and timeline under the event lock, checks and seals that temporary snapshot,
exports only from the checked copy, and deletes it. The original remains
unchanged, so a paused session can resume. Already-sealed archives export
directly. Active sessions remain blocked because collectors may still be
writing. The AutoCAB session adapter continues to require a sealed session
folder when it consumes the folder directly.

### What is actually measured, and what is not

`docs/deid-evaluation.md` carries the numbers, regenerated by
`autocab deid eval`. As of the committed scorecard, the stdlib regex tier scores
**1.00 strict recall** on email, IP, URL, SSN, MRN, labelled DOB, subject id,
accession, account, device, health-plan id and licence, **0.89 overall** across
the gated tiers, with **0.4% over-redaction** and zero must-survive violations
across 316 synthetic records.

It scores **0.45 on names**, and that number is in `thresholds.json` as a
tested floor rather than a footnote. The optional GLiNER tier raises gated name
recall to **0.95** on the same synthetic corpus. Its complete results are in
`data/deid-eval/scorecard.gliner.json`. Read
**"What this does not prove"** in `docs/deid-evaluation.md` before quoting any
of this — in particular, none of it is a HIPAA Safe Harbor determination, and
HIPAA identifier 17 (full-face photographs) is uncovered by construction.

Screen-capture application denylisting, encryption at rest and a panic-pause
remain future work in the design plan.

### Local by default, and the three cases that matter

**The default is local, with no egress.** The regex tier and GLiNER inference
are local by construction. Only the explicit `autocab deid fetch` setup command
uses the network; sealing never does. An LLM tier pointed at a loopback
address (`ollama`, or any OpenAI-compatible server on `127.0.0.1`) is also
local: `autocab.deid.egress` classifies it `none` and applies no egress gate. A
**cloud** provider is a different thing entirely: it is egress, it deletes the
local-only claim for that session, and it requires all five gate layers
including a named-approver acknowledgement with a BAA and a zero-data-retention
confirmation. The gate refuses by default and the LLM tier is off unless
explicitly armed.

One trap worth stating because it is easy to miss: if `HTTP_PROXY`,
`HTTPS_PROXY` or `ALL_PROXY` is set and `NO_PROXY` does not exempt the endpoint,
the text goes to the **proxy** — so `--llm-base-url http://localhost:11434`
under `ALL_PROXY` is classified from the *proxy's* address, not loopback's, and
is fully gated. Everything else here is local-only with no outbound network
requests; the control API binds to `127.0.0.1` behind a token unless you opt
in to a non-loopback bind with `--allow-remote` / `WFREC_ALLOW_REMOTE=1`.

## Troubleshooting

| Symptom | Cause and fix |
|---|---|
| No shell commands captured | Run `wfrec doctor`. For `devsql`, verify Atuin setup. For `hook-spool`, run `eval "$(wfrec hooks eval)"` |
| `NotSealed` on direct ingestion | The session folder has not been pattern checked. Export a paused or archived session, or run `wfrec seal <id>` before passing the folder directly to AutoCAB |
| `SessionSealed` on an append | A sealed session is immutable. Use `wfrec seal --reseal <id>` if it genuinely has to change |
| `wfrec seal` says "not idle" | Stop the session first, or pass `--force` to stop and then seal. It refuses rather than racing the collectors |
| `ImportError: libGL.so.1` | `opencv-python` shadowed the headless build. `uv pip install --force-reinstall opencv-python-headless` |
| `files` source records nothing | No watch root. `wfrec watch <dir>` |
| `agents` says `no-agent-tools-found` | No transcripts found. `wfrec attach-transcript <file> --tool <name>` |
| Screen capture disabled on Linux | Wayland. See the platform table above |
| File events missing on HPC scratch | inotify delivers nothing on NFS/Lustre. Those roots are polled instead; `wfrec doctor` reports it |
| Session seems dead | `cat ~/.autocab/daemon.log`; `wfrec daemon --stop && wfrec daemon` |
| Anything else | `wfrec doctor` first |

## Driving it from an agent

The `recorder` skill in [`.claude/skills/recorder/`](../../.claude/skills/recorder/)
(with a Copilot copy in `.github/skills/recorder/`) maps natural language onto
these commands, so you can say "start screen recording" or "pause, I'm waiting
on the alignment job" instead of typing them. The skill shells out to this CLI
rather than reimplementing anything.

## Development

```bash
pytest                       # no PYTHONPATH needed after `uv pip install -e .`
pytest tests/test_recording_spool.py -v
```

Tests redirect `WFREC_HOME` and `WFREC_RUN` into `tmp_path` via the fixtures in
`tests/conftest.py`. That is not cosmetic: without it, running the suite on a
machine where somebody is actually recording would clobber their sessions.

Module map:

| Path | Role |
|---|---|
| `paths.py` | Durable root vs. runtime root, and why the sentinel is never in `$HOME` |
| `state.py` | Rich JSON state plus the TAB-separated sentinel the hooks parse |
| `spool.py` | The RS/US shell-to-daemon wire format |
| `events.py` | Event model, sequencing, and the `seq`-vs-`ts` distinction |
| `session.py` | Lifecycle: create, pause, resume, stop, preempt |
| `recorder.py` | Owns the active session and its collectors |
| `collectors/` | One backend per source, each degrading with a named reason |
| `hooks/` | bash, zsh, fish, PowerShell, and POSIX `sh` for remote |
| `exporters/` | The three lossy projections into AutoCAB's formats |
| `api.py` / `daemon.py` / `cli.py` / `ui/` | The four faces of one control API |
| `doctor.py` | Per-machine backend report |
| `remote.py` / `merge.py` | SSH+SLURM capture, and multi-analyst aggregation |
