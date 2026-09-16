# AutoCAB

AutoCAB is a BioHackathon 2026 project for turning repeated CAB bioinformatics workflows into governed, privacy-conscious AI agent skill proposals.

## Project Focus

- Detect repeated workflow patterns from safe demo inputs
- Redact obvious sensitive content before proposal generation
- Match workflow summaries against a curated skill catalog
- Draft reviewable `SKILL.md` proposals
- Export approved proposals as PR-ready folders

## Set Up Activity Tracking with Codex or Claude

The maintained skill in `skills/setup-activity-tracking/` helps Codex or Claude
verify and install DevSQL and Atuin. The agent shows the planned changes and
waits for approval before installing anything. The skill configures Atuin for
shell history and leaves ActivityWatch unchanged. DevSQL reads Claude Code and
Codex histories directly, so no agent hooks are required.

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
`~/.claude/skills/setup-activity-tracking/` without overwriting an existing
installation. Then invoke the installed skill:

```text
/setup-activity-tracking verify and set up activity tracking
```

## Quick Start

Install [uv](https://docs.astral.sh/uv/getting-started/installation/), then
create a local environment and install the project with its test dependencies:

```bash
uv venv
source .venv/bin/activate
uv pip install -e '.[dev]'
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

`wfrec` records live work and produces the session artifacts that AutoCAB's
input adapters read. This connects observation to a reviewed skill proposal
without relying on demo data.

After completing the setup above, check wfrec and install the shell hook once:

```bash
wfrec doctor                  # which backend each source resolved to, and why
wfrec hooks install           # one guarded block appended to your shell rc file
wfrec hooks status
```

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

### Capture controls and sources

Toggles and pause apply to already-open terminals at the next prompt, with no
restart. Only one session can be active; starting or resuming another pauses
the current session and records the handover in both timelines.

Five sources can be toggled independently: `screen` (frames, OCR text, window
titles), `context` (pasted text with no background clipboard access), `shell`
(commands, exit codes, durations, optional output), `agents` (Claude Code,
Copilot Chat, Cursor transcripts), and `files` (git-verified changes in
declared roots).

### Session data and exports

Each session stores an append-only `events.jsonl` timeline plus frames, diffs,
notes, and job logs. `wfrec export` creates three **lossy** adapter formats;
the timeline remains the source of truth.

### Remote and team workflows

`wfrec ssh <host>` and `wfrec pull <host>` add remote commands, SLURM metadata,
and bounded `slurm-*.out` slices through a POSIX hook on the HPC login node.
For multiple analysts, `wfrec merge` or a session folder passed to
`--session-dir` combines sessions and separates repeated workflows from
one-off work.

### Help and detailed documentation

Run `wfrec --help`, or use the `recorder` skill in
`.claude/skills/recorder/` from Claude Code or Copilot.

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

## License

This project is licensed under the [MIT License](LICENSE.md).
