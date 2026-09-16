# AutoCAB

AutoCAB is a BioHackathon 2026 project for turning repeated CAB bioinformatics workflows into governed, privacy-conscious AI agent skill proposals.

This repository now combines the new team/project documentation with the working prototype code migrated from the earlier development repo. The repository structure below matches the files that are actually present in this project today.

## Project Focus

- Detect repeated workflow patterns from safe demo inputs
- Redact obvious sensitive content before proposal generation
- Match workflow summaries against a curated skill catalog
- Draft reviewable `SKILL.md` proposals
- Export approved proposals as PR-ready folders

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

## Key Files

- `data/sample_workflow_traces.json`: synthetic workflow traces
- `data/sample_screen_capture.json`: pre-exported screen activity timeline example
- `data/sample_skill_catalog.json`: curated sample skill catalog
- `docs/proposal/AutoCAB-challenge-description.docx`: project challenge brief
- `docs/team/registration.md`: registration notes
- `docs/team/team_member_info.md`: team member details
- `docs/mgatta42/plan.md`: design plan for the `wfrec` workflow recorder
- `skills/setup-activity-tracking/SKILL.md`: maintained DevSQL and Atuin setup skill

## Set Up Activity Tracking with Codex or Claude

The maintained skill lives in `skills/setup-activity-tracking/`. Installing the
skill makes its workflow available to the agent. DevSQL and Atuin are installed
separately only after the agent displays the planned changes and receives
explicit approval. ActivityWatch and optional agent hooks are not installed.

### Codex

Ask Codex to install the skill from this repository:

```text
Use $skill-installer to install:
https://github.com/stjude-biohackathon/KIDS26-Team6/tree/main/skills/setup-activity-tracking
```

On the next turn, invoke the installed skill:

```text
$setup-activity-tracking verify and set up activity tracking
```

### Claude Code

Ask Claude Code to copy `skills/setup-activity-tracking/` to
`~/.claude/skills/setup-activity-tracking/`. It should stop rather than
overwrite an existing installation. Then invoke the installed skill:

```text
/setup-activity-tracking verify and set up activity tracking
```

## Quick Start

Install the project and test dependencies:

```bash
uv pip install --python .venv/bin/python -e '.[dev]'
```

Run the default demo pipeline:

```bash
PYTHONPATH=src python3 -m autocab demo
```

Run with explicit input modes:

```bash
PYTHONPATH=src python3 -m autocab demo --input-mode trace --trace-file data/sample_workflow_traces.json
PYTHONPATH=src python3 -m autocab demo --input-mode screen-capture --capture-file data/sample_screen_capture.json
PYTHONPATH=src python3 -m autocab demo --input-mode terminal-log --log-file path/to/terminal-session.txt
PYTHONPATH=src python3 -m autocab demo --input-mode session --session-dir ~/.wfrec/sessions/<id>
```

Convert a terminal log into normalized trace JSON:

```bash
PYTHONPATH=src python3 -m autocab ingest-terminal-log path/to/terminal-session.txt --output /tmp/generated_terminal_trace.json
```

Generated proposals are written to `skills/generated-drafts/` by default.

## Recording a live session (`wfrec`)

The pipeline's input adapters are passive: they read artifacts somebody else
produced. `wfrec` is the missing front end that produces them, so the full loop
from observation to reviewed skill can run on real work rather than a fixture.

```bash
pip install -e .              # adds the `wfrec` and `autocab` commands
wfrec hooks install           # one guarded block appended to your shell rc file
wfrec doctor                  # which backend each source resolved to, and why
```

Then record:

```bash
wfrec start --title "HG008 variant QC" --watch .
wfrec source screen on                       # toggle any source, any time
wfrec note "reran because the BAM was truncated" --label why
wfrec pause --reason "waiting on bwa" --expect 6h
wfrec resume
wfrec stop
wfrec export --format autocab --format trace
```

Toggles and pause take effect immediately in terminals that are **already
open** — the hooks read a small sentinel file on each prompt, so nothing needs
restarting. Exactly one session is active at a time; starting or resuming
another auto-pauses the current one and records the handover on both timelines.

Five independently toggleable sources: `screen` (frames, OCR text, window
titles), `context` (a paste box; no background clipboard access), `shell`
(commands, exit codes, durations, optional full output), `agents` (Claude Code,
Copilot Chat, Cursor transcripts), and `files` (git-verified changes in
declared roots).

A session folder holds `events.jsonl` — an append-only, deliberately verbose
timeline — plus sidecar frames, diffs, notes and job logs. `wfrec export`
writes three **lossy** projections of it into the formats the existing adapters
already read; the timeline stays the source of truth.

Remote work is supported via `wfrec ssh <host>` and `wfrec pull <host>`, which
bootstrap a POSIX hook onto an HPC login node and fold remote commands, SLURM
job metadata and bounded slices of `slurm-*.out` into the same timeline.

For multiple analysts, `wfrec merge` (or pointing `--session-dir` at a folder of
sessions) combines several people's sessions so repeated cross-analyst
workflows separate from one-off ones.

Run `wfrec --help`, or ask Claude Code / Copilot — the `recorder` skill in
`.claude/skills/recorder/` maps natural language onto these commands.

**Full usage guide: [`src/wfrec/README.md`](src/wfrec/README.md)** — install,
per-platform notes (including the macOS Screen Recording restart and the
Wayland limits), multi-analyst and HPC workflows, and troubleshooting.
See [`docs/mgatta42/plan.md`](docs/mgatta42/plan.md) for the design rationale
and the per-OS backend matrix.

## Current Status

- Working local-first proof of concept migrated into this repository
- Team and proposal documentation kept in the new project structure
- Human review remains explicit in the workflow
- Automated test coverage is available under `tests/`
