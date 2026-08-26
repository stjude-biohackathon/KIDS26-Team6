# AutoCAB

This repository is adapted for a BioHackathon 2026 team project focused on turning repeated bioinformatics workflows into governed, privacy-conscious AI agent skill proposals.

## Project Profile

- **Project name:** AutoCAB: Turning Everyday CAB Workflows into Reusable, Vetted AI Agent Skills
- **Question, problem, or opportunity:** CAB analysts repeatedly perform high-value workflow patterns, but reusable skills are still mostly authored manually. We need a systematic, safe way to discover repeated workflows and convert them into reviewable skill proposals.
- **Data, inputs, or evidence:**
  - Synthetic/public workflow traces (`data/sample_workflow_traces.json`)
  - Pre-exported screen-capture timeline example (`data/sample_screen_capture.json`)
  - Terminal workflow logs (`data/sample_terminal_session.log`)
  - Curated skill catalog examples (`data/sample_skill_catalog.json`)
  - Public skill conventions from CAB-ai Skills and related open repositories
- **Expected output:**
  - Ranked repeated-workflow candidates
  - Redacted, matched skill proposals in `SKILL.md` format
  - Human-review queue entries for approve/edit/reject
  - PR-ready exported draft folders in `skills/generated-drafts/`
- **Tools and stack:**
  - Python 3.10+
  - Local CLI pipeline (`src/autocab`)
  - Modular framework (`src/autocab/framework`)
  - Aggregation/matching (`src/aggregation`)
  - Privacy redaction (`src/redaction`)
  - Review queue and proposal export (`src/triage_console`, `src/pr_generator`)
  - Test suite with pytest (`tests/`)
- **Team lead:** TBD (fill with name and GitHub handle)
- **Team members and roles:** TBD (add link to your team roster/roles document)
- **Communication:** TBD (Slack/Teams/email channel)

Naming the stack and responsibilities early helps split the work across input ingestion, privacy, matching, review workflow, and PR export lanes.

## Vision and Mission

- **Vision:** Build a self-improving, governed skill discovery loop where repeated analyst work is transformed into reusable, high-quality automation artifacts, while keeping human control and privacy safeguards explicit.
- **Mission:** During the BioHackathon, deliver a working end-to-end AutoCAB prototype that ingests workflow evidence, aggregates repeated patterns across analysts, redacts sensitive content, drafts skill proposals, and exports reviewable outputs for downstream pull request workflows.

## About

AutoCAB extends an existing proof of concept into a team-scale, governance-first architecture suitable for shared bioinformatics operations. The project addresses a practical bottleneck: the gap between repeated real-world analyst work and the current skill library.

The repository already includes a runnable local-first pipeline and an extensible framework. This allows the team to focus hackathon time on challenge-specific improvements:

- Better multi-analyst aggregation and deduplication
- Stronger PHI/sensitive-text redaction policies
- Better matching against CAB-style skill catalogs
- Cleaner human review and export workflows

This matters because it can reduce repeated manual effort, improve reproducibility, and provide a data-driven map of automation opportunities.

## Roadmap and Milestones

| When | Focus | Expected outcome |
| --- | --- | --- |
| Day 1 | Align challenge scope, finalize input mode(s), define privacy guardrails, and assign lanes | Shared execution plan, successful baseline demo run, and agreed acceptance criteria |
| Day 2 | Improve clustering/matching, harden redaction, and connect review/export flow | Working end-to-end prototype with measurable improvements and reviewable generated drafts |
| Day 3 | Stabilize, test, document limitations, and prepare presentation/demo script | Demo-ready handoff with reproducible run steps, known constraints, and next-step backlog |

The goal is not a perfect production system in 72 hours. The goal is a clear, safe, and useful prototype that can be reviewed, extended, and adopted incrementally.

## Quick Start

Run the demo pipeline:

```bash
PYTHONPATH=src python3 -m autocab demo
```

Run with explicit input modes:

```bash
PYTHONPATH=src python3 -m autocab demo --input-mode trace --trace-file data/sample_workflow_traces.json
PYTHONPATH=src python3 -m autocab demo --input-mode screen-capture --capture-file data/sample_screen_capture.json
PYTHONPATH=src python3 -m autocab demo --input-mode terminal-log --log-file data/sample_terminal_session.log
```

Convert a terminal log to reusable trace JSON:

```bash
PYTHONPATH=src python3 -m autocab ingest-terminal-log data/sample_terminal_session.log --output /tmp/generated_terminal_trace.json
```

## Current Status

- Runnable BioHackathon-focused proof of concept
- Local-first architecture and synthetic/public demo data
- Explicit human review step retained in the workflow
- Unit/pipeline tests included under `tests/`
