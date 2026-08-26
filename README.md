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
src/aggregation/                Workflow aggregation and matching helpers
src/redaction/                  Privacy redaction helpers
src/triage_console/             Minimal review queue support
src/pr_generator/               PR-ready draft export
tests/                          Unit and pipeline tests
skills/generated-drafts/        Generated output folders from demo runs
```

## Key Files

- `data/sample_workflow_traces.json`: synthetic workflow traces
- `data/sample_screen_capture.json`: pre-exported screen activity timeline example
- `data/sample_terminal_session.log`: sample terminal workflow log
- `data/sample_skill_catalog.json`: curated sample skill catalog
- `docs/proposal/AutoCAB-challenge.docx`: project challenge brief
- `docs/team/registration.md`: registration notes
- `docs/team/team_member_info.md`: team member details

## Quick Start

Run the default demo pipeline:

```bash
PYTHONPATH=src python3 -m autocab demo
```

Run with explicit input modes:

```bash
PYTHONPATH=src python3 -m autocab demo --input-mode trace --trace-file data/sample_workflow_traces.json
PYTHONPATH=src python3 -m autocab demo --input-mode screen-capture --capture-file data/sample_screen_capture.json
PYTHONPATH=src python3 -m autocab demo --input-mode terminal-log --log-file data/sample_terminal_session.log
```

Convert a terminal log into normalized trace JSON:

```bash
PYTHONPATH=src python3 -m autocab ingest-terminal-log data/sample_terminal_session.log --output /tmp/generated_terminal_trace.json
```

Generated proposals are written to `skills/generated-drafts/` by default.

## Current Status

- Working local-first proof of concept migrated into this repository
- Team and proposal documentation kept in the new project structure
- Human review remains explicit in the workflow
- Automated test coverage is available under `tests/`
