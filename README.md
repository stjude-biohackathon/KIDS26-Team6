![AutoCAB](images/autocab-logo.png)

[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE.md)
[![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue.svg)](pyproject.toml)
[![Tests](https://github.com/stjude-biohackathon/KIDS26-Team6/actions/workflows/tests.yml/badge.svg)](https://github.com/stjude-biohackathon/KIDS26-Team6/actions/workflows/tests.yml)
[![Platforms](https://img.shields.io/badge/platform-macOS%20%7C%20Linux%20%7C%20Windows-lightgrey.svg)](src/wfrec/README.md)

Every recurring fix, workaround, and analysis step that lives only in one
person's head or one messy terminal history is knowledge the next analyst has
to rediscover from scratch. AutoCAB watches how you actually work, and turns
that observation into a shared, reviewed skill — instead of leaving it tacit
or duplicated across the team.

![wfrec activity dashboard screenshot](images/clipboard-2893987244.png)

## Features

- **Governed pipeline**: workflow evidence is normalized, redacted, and
  compared against existing skills before a draft is proposed — nothing
  becomes a shared skill without a human reviewing, editing, and approving it.
- **Cross-platform recording** with `wfrec` (macOS, Linux, Windows, and HPC
  login nodes): five independently toggleable capture sources — screen, shell,
  context notes, agent transcripts, and file changes.
- **Privacy by construction**: an always-on regex redaction tier runs inline
  during capture, with optional heavier tiers (model-based, Presidio, or
  LLM-gated) layered on top, plus `wfrec seal` to retroactively scrub an
  already-recorded session before sharing it.
- **Live activity dashboard**: session composition, an activity timeline, and
  event/window/agent breakdowns, right in the recorder's own GUI.
- **Always know you're being recorded**: a menu-bar/tray indicator and a
  floating on-screen badge, so recording is never silent or easy to miss.
- **Remote and multi-analyst workflows**: SSH-based capture on HPC clusters,
  a remotely reachable dashboard, and session merging across analysts.
- **skill-forge**: an evidence-linked toolchain for turning incomplete
  activity records into a validated, human-approved skill package.

## Project Background

Agent skills package instructions, scripts, examples, and validation steps so AI
agents can perform scientific workflows consistently. CAB already publishes
these skills. Creating one currently requires maintainers to recognize a
repeated workflow, document it, test it, and contribute it to the shared
library. As a result, recurring work in data preparation, quality control,
analysis, reporting, and troubleshooting can remain tacit or duplicated.

AutoCAB addresses this gap through a governed observation-to-review workflow.
People review, edit, and approve every proposal before it becomes a shared
skill.

The BioHackathon prototype uses public or synthetic data and
volunteer-consented sessions involving public data only. `wfrec` provides the
current recording layer. Input adapters can extend collection to ActivityWatch,
Screenpipe, and future activity sources.

```text
Workflow evidence
  -> normalize and cluster
  -> redact and compare with existing skills
  -> draft SKILL.md
  -> human review
  -> PR-ready skill folder
```

See the [challenge description](docs/proposal/AutoCAB-challenge-description.docx)
and [framework documentation](docs/biohackathon-framework.md) for the project
rationale and architecture.

## Install & Setup

### Install AutoCAB

Install [uv](https://docs.astral.sh/uv/getting-started/installation/), then
create a local environment and install the project with its test dependencies:

```bash
uv venv
source .venv/bin/activate
uv pip install -e '.[dev]'
```

AutoCAB requires Python 3.10 or newer. Its Python packages are installed from
`pyproject.toml`. Live shell and Codex capture use DevSQL and Atuin, which are
installed separately from the Python environment. Use the activity-tracking
skill below to manage their setup and removal.

#### Install without uv (pip / venv)

On HPC and shared Linux hosts, default `python3` is often older than 3.10. Create
the environment with **Python 3.10+** explicitly:

```bash
cd KIDS26-Team6
python3.11 -m venv .venv          # not plain python3 if that is 3.6/3.8
source .venv/bin/activate
python -m pip install --upgrade pip setuptools wheel
pip install -e '.[linux,dev]'   # quote extras in zsh: '.[linux,dev]'
python --version                # must be >= 3.10
```

If OCR fails with `libGL.so.1`, run `pip install --force-reinstall opencv-python-headless`
and `wfrec doctor` again.

#### Install and run on HPC (daemon + browser UI)

On a cluster **login or interactive node**, run the recorder daemon in the
background and open the web UI from your **laptop browser** (do not use
`wfrec daemon --gui` on headless nodes; `BROWSER` is often `lynx`).

1. **Install** (once per clone / venv) as above with `'.[linux,dev]'`.

2. **Optional shell hooks** (if `wfrec doctor` reports `hook-spool`):

   ```bash
   wfrec hooks install
   eval "$(wfrec hooks eval)"    # or add to ~/.bashrc for new shells
   ```

3. **Export remote-bind settings** (for UI via node IP; skip if you only use
   SSH port forwarding to `127.0.0.1`):

   ```bash
   export WFREC_ALLOW_REMOTE=1
   export WFREC_BIND_HOST=0.0.0.0
   export WFREC_ADVERTISE_URL="http://YOUR_NODE_IP:8787"   # optional; see daemon summary
   ```

   Replace `YOUR_NODE_IP` with the **Public** or **Private** URL from the daemon
   startup table (`hostname -I`: first field = site/public, second = internal/private).

4. **Start the daemon in the background** (same shell session must keep these
   exports if you set them):

   ```bash
   nohup wfrec daemon >> ~/.wfrec/daemon.log 2>&1 &
   ```

   Or in `tmux`/`screen` without `nohup`: `wfrec daemon` in the foreground.

5. **Open the UI** on your laptop: use **Public** / **Private** from the log or
   run `wfrec daemon` once in the foreground to print the summary. Alternative:
   `ssh -L 8787:127.0.0.1:8787 you@login-node` and open `http://127.0.0.1:8787/`
   (loopback bind only; no `WFREC_*` remote exports needed).

6. **Record a session** (in terminals where hooks are active):

   ```bash
   wfrec start --title "My workflow" --watch /path/to/project
   wfrec status
   wfrec stop
   wfrec seal
   wfrec export
   ```

7. **Stop the background daemon** when finished:

   ```bash
   wfrec daemon --stop
   ```

See [`src/wfrec/README.md`](src/wfrec/README.md) for capture sources, security
notes, and troubleshooting.

### Set Up PHI Redaction

Pattern-based PHI redaction works by default and requires no setup.

For additional name and location detection, download and verify the local
GLiNER model:

```bash
wfrec deid fetch
wfrec deid verify
```

The model is stored in `~/.wfrec/models/` or `$WFREC_HOME/models/`. AutoCAB
does not download it during installation.

Select GLiNER when sealing a session:

```bash
wfrec seal <session-id> --engine gliner
```

Pattern matching still runs before GLiNER. See
[`docs/deid-evaluation.md`](docs/deid-evaluation.md) for benchmark results and
limitations.

### Set Up Activity Tracking with Codex or Claude

The maintained skill in `skills/setup-activity-tracking/` helps Codex or Claude
verify and install DevSQL and Atuin. It configures Atuin for shell history.
Agent-attributed Atuin hooks remain opt-in because `wfrec` reads Codex messages
through DevSQL and Claude Code transcripts directly. The agent shows the
planned changes and waits for approval before changing the system.

#### Codex

Ask Codex to install the skill from this repository:

```text
Use $skill-installer to install:
https://github.com/stjude-biohackathon/KIDS26-Team6/tree/main/skills/setup-activity-tracking
```

On the next turn, invoke the installed skill:

```text
$setup-activity-tracking verify and set up activity tracking
```

#### Claude Code

Ask Claude Code to copy `skills/setup-activity-tracking/` to
`~/.claude/skills/setup-activity-tracking/` while preserving an existing
installation. Then invoke the installed skill:

```text
/setup-activity-tracking verify and set up activity tracking
```

## Usage

### Run the Demo Pipeline

Run the default demo pipeline:

```bash
uv run autocab demo
```

Run with explicit input modes:

```bash
uv run autocab demo --input-mode trace --trace-file data/sample_workflow_traces.json
uv run autocab demo --input-mode screen-capture --capture-file data/sample_screen_capture.json
uv run autocab demo --input-mode terminal-log --log-file path/to/terminal-session.txt
uv run autocab demo --input-mode session --session-dir ~/.wfrec/sessions/<id>
```

Convert a terminal log into normalized trace JSON:

```bash
uv run autocab ingest-terminal-log path/to/terminal-session.txt --output /tmp/generated_terminal_trace.json
```

Generated proposals are written to `skills/generated-drafts/` by default.

### Collect Workflow Evidence

`wfrec` records live work and produces the session artifacts that AutoCAB's
input adapters read. Recorded sessions follow the same path to a reviewed skill
proposal as the supplied demo inputs.

After completing the setup above, check which backends are available:

```bash
wfrec doctor                  # which backend each source resolved to, and why
```

DevSQL with Atuin is the primary local shell backend. If `wfrec doctor` selects
`hook-spool`, install the fallback hook with `wfrec hooks install` and confirm
it with `wfrec hooks status`.

Then record:

```bash
wfrec start --title "HG008 variant QC" --watch .
wfrec status
wfrec source screen on                       # toggle any source, any time
wfrec note "reran because the BAM was truncated" --label why
wfrec pause --reason "waiting on bwa" --expect 6h
wfrec resume
wfrec stop
wfrec export
```

#### Capture Sources and Boundaries

Toggles and pause apply to already-open terminals at the next prompt. One
session is active at a time; starting or resuming another pauses the current
session and records the handover in both timelines.

Five sources can be toggled independently: `screen` (frames, OCR text, window
titles), `context` (text deliberately added through the paste box), `shell`
(commands, exit codes, and durations), `agents` (Codex, Claude Code, Copilot
Chat, and Cursor transcripts), and `files` (git-verified changes in declared
roots). Local shell collection stores command metadata; bounded Slurm output
enters through remote job-log collection.

#### Session Data and Exports

Each session stores an append-only `events.jsonl` timeline plus frames, diffs,
notes, and job logs. `wfrec export` creates a complete `events.json` document
plus three **lossy** AutoCAB adapter formats. The timeline remains the source
of truth. Paused and archived sessions export through a temporary, local,
pattern-checked snapshot.

#### Remote and Team Workflows

`wfrec ssh <host>` and `wfrec pull <host>` add remote commands, SLURM metadata,
and bounded `slurm-*.out` slices through a POSIX hook on the HPC login node.
For multiple analysts, `wfrec merge` or a session folder passed to
`--session-dir` combines sessions and separates repeated workflows from
one-off work.

#### Help and Detailed Documentation

Run `wfrec --help`, or use the `recorder` skill in
`.claude/skills/recorder/` from Claude Code or Copilot.

**Full usage guide: [`src/wfrec/README.md`](src/wfrec/README.md)**: install,
per-platform notes (including the macOS Screen Recording restart and the
Wayland limits), multi-analyst and HPC workflows, and troubleshooting.
See [`docs/mgatta42/plan.md`](docs/mgatta42/plan.md) for the design rationale
and the per-OS backend matrix.

## Development and Testing

Run the test suite from the repository-local environment:

```bash
.venv/bin/python -m pytest
```

## Repository Layout

```text
src/autocab/       CLI, orchestration, pipeline framework, and de-identification engine
src/wfrec/         Cross-platform workflow recorder (the observation front end)
skills/            Agent skills, including skill-forge and setup-activity-tracking
docs/              Project documentation, proposal, and team information
data/              Sample inputs used by the prototype
tests/             Unit and pipeline tests
```

See [`src/wfrec/README.md`](src/wfrec/README.md) for the recorder's own layout
(collectors, exporters, hooks) in detail.

## Team

See [`docs/team/team_member_info.md`](docs/team/team_member_info.md) for the
project leads and team members.

## License

This project is licensed under the [MIT License](LICENSE.md).
