![AutoCAB](images/autocab-logo.png)

[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE.md)
[![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue.svg)](pyproject.toml)
[![Tests](https://github.com/stjude-biohackathon/KIDS26-Team6/actions/workflows/tests.yml/badge.svg)](https://github.com/stjude-biohackathon/KIDS26-Team6/actions/workflows/tests.yml)
[![Platforms](https://img.shields.io/badge/platform-macOS%20%7C%20Linux%20%7C%20Windows-lightgrey.svg)](src/wfrec/README.md)

Recurring fixes, workarounds, and analysis steps often survive only in one
person's memory or terminal history. The next analyst must rediscover them.
AutoCAB records real work and turns recurring workflows into shared skills
that people review, edit, and approve before sharing.

![wfrec activity dashboard screenshot](images/clipboard-2893987244.png)

## Features

- AutoCAB normalizes and redacts workflow evidence, then compares it with
  existing skills before proposing a draft. A person must review, edit, and
  approve every draft before it becomes a shared skill.
- `wfrec` records on macOS, Linux, Windows, and HPC login nodes. Its five
  capture sources can be enabled independently: screen, shell, context notes,
  agent transcripts, and file changes.
- Regex rules redact detected sensitive text during capture. Optional
  model-based, Presidio, or LLM-gated checks can add another pass. `wfrec seal`
  applies redaction to an existing session before it is shared.
- The live dashboard shows session composition, activity over time, and event
  counts for windows and agents.
- A menu-bar or tray indicator and a floating badge remain visible while
  recording is active.
- SSH-based capture supports HPC work. Teams can open the dashboard remotely
  and merge sessions from multiple analysts.
- `skill-forge` turns incomplete activity records into validated skill
  packages linked to their source evidence. A person approves each package.

## Project background

Agent skills package the instructions, scripts, examples, and validation steps
needed for AI agents to repeat scientific workflows. CAB already publishes
these skills. Creating one requires maintainers to recognize a repeated
workflow, document it, test it, and contribute it to the shared library. Until
that happens, teams may repeat the same data preparation, quality control,
analysis, reporting, or troubleshooting work.

AutoCAB records workflow evidence and prepares a skill proposal for review.
People edit and approve each proposal before it becomes a shared skill.

The BioHackathon prototype uses public or synthetic data. Recorded sessions
involve volunteers who consented to capture while working with public data.
`wfrec` provides the current recording layer. Input adapters can extend
collection to ActivityWatch, Screenpipe, and future activity sources.

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

## Install and setup

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

#### Install without uv (pip and venv)

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

#### Install and run on HPC

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

### Set up activity tracking with Codex or Claude

The maintained skill in `skills/setup-activity-tracking/` helps Codex or Claude
verify and install DevSQL and Atuin. It configures Atuin for shell history.
Atuin hooks that attribute commands to agents remain optional because `wfrec`
reads Codex messages through DevSQL and reads Claude Code transcripts directly.
The agent shows its planned changes and waits for approval before changing the
system.

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

### Run the demo pipeline

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

### Collect workflow evidence

`wfrec` records live work and produces session artifacts for AutoCAB's input
adapters. Recorded sessions and supplied demo inputs follow the same path to a
reviewed skill proposal.

After completing the setup above, check which backends are available:

```bash
wfrec doctor                  # show the backend selected for each source and why
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

#### Capture sources and session boundaries

Source changes and pauses take effect in open terminals at the next prompt.
Only one session can be active. Starting or resuming another session pauses the
current one and records the handover in both timelines.

Five sources can be toggled independently: `screen` (frames, OCR text, window
titles), `context` (text deliberately added through the paste box), `shell`
(commands, exit codes, and durations), `agents` (Codex, Claude Code, Copilot
Chat, and Cursor transcripts), and `files` (git-verified changes in declared
roots). Local shell collection stores command metadata; bounded Slurm output
enters through remote job-log collection.

#### Session data and exports

Each session stores an append-only `events.jsonl` timeline plus frames, diffs,
notes, and job logs. `wfrec export` creates a complete `events.json` document
plus three **lossy** AutoCAB adapter formats. The timeline remains the source
of truth. Paused and archived sessions export through a temporary, local,
pattern-checked snapshot.

#### Remote and team workflows

`wfrec ssh <host>` and `wfrec pull <host>` add remote commands, SLURM metadata,
and bounded `slurm-*.out` slices through a POSIX hook on the HPC login node.
For multiple analysts, `wfrec merge` or a session folder passed to
`--session-dir` combines sessions and separates repeated workflows from
one-off work.

#### Help and detailed documentation

Run `wfrec --help`, or use the `recorder` skill in
`.claude/skills/recorder/` from Claude Code or Copilot.

The [full recorder guide](src/wfrec/README.md) covers installation, platform
notes such as the macOS Screen Recording restart and Wayland limits,
multi-analyst and HPC workflows, and troubleshooting.
See [`docs/mgatta42/plan.md`](docs/mgatta42/plan.md) for the design rationale
and the per-OS backend matrix.

## Development and testing

Run the test suite from the repository-local environment:

```bash
.venv/bin/python -m pytest
```

## Repository layout

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
