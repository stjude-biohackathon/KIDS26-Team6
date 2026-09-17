# AutoCAB

AutoCAB is a BioHackathon 2026 project for turning repeated CAB bioinformatics workflows into governed, privacy-conscious AI agent skill proposals.

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
   wfrec export --format autocab --format trace
   ```

7. **Stop the background daemon** when finished:

   ```bash
   wfrec daemon --stop
   ```

See [`src/wfrec/README.md`](src/wfrec/README.md) for capture sources, security
notes, and troubleshooting.

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
wfrec export --format autocab --format trace
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
notes, and job logs. `wfrec export` creates three **lossy** adapter formats;
the timeline remains the source of truth.

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
README.md                       Project overview and run instructions
pyproject.toml                  Python project configuration
docs/                           Supporting project documentation
docs/proposal/                  Challenge brief and proposal files
docs/team/                      Team registration and member information
docs/biohackathon-framework.md  Framework architecture notes
project-management/             Planning templates and meeting notes
data/                           Sample inputs used by the prototype
src/autocab/                    CLI, orchestration, models, and input adapters
src/autocab/framework/          Extensible pipeline framework
src/wfrec/                      Cross-platform workflow recorder (the observation front end)
src/wfrec/README.md             Recorder usage guide: install, record, export, troubleshoot
src/wfrec/hooks/                Shell hooks: bash, zsh, fish, PowerShell, POSIX sh (remote)
src/wfrec/collectors/           Per-source capture backends
src/wfrec/exporters/            Lossy projections into AutoCAB's input formats
.claude/skills/recorder/        `recorder` agent skill that drives wfrec in natural language
src/aggregation/                Workflow aggregation and matching helpers
src/redaction/                  Privacy redaction helpers
src/triage_console/             Minimal review queue support
src/pr_generator/               PR-ready draft export
tests/                          Unit and pipeline tests
skills/setup-activity-tracking/ Maintained local activity-tracking setup skill
skills/generated-drafts/        Generated output folders from demo runs
```

### Key Files

- `data/sample_workflow_traces.json`: synthetic workflow traces
- `data/sample_screen_capture.json`: pre-exported screen activity timeline example
- `data/sample_skill_catalog.json`: curated sample skill catalog
- `docs/proposal/AutoCAB-challenge-description.docx`: project challenge brief
- `docs/team/registration.md`: registration notes
- `docs/team/team_member_info.md`: team member details
- `docs/mgatta42/plan.md`: design plan for the `wfrec` workflow recorder
- `skills/setup-activity-tracking/SKILL.md`: maintained DevSQL and Atuin setup skill

## Team

See [`docs/team/team_member_info.md`](docs/team/team_member_info.md) for the
project leads and team members.

## License

This project is licensed under the [MIT License](LICENSE.md).
