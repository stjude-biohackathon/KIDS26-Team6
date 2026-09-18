---
name: $name
description: $description_yaml
compatibility: $compatibility_yaml
metadata:
  version: "$skill_version"
  status: draft
  packaging: CBD
  generated_by: skill-forge
  last_reviewed: "$review_date"
allowed-tools: shell python
---

# $title

## Purpose

$purpose

## When to use

$when_to_use

## When not to use

$when_not_to_use

## Required inputs

$required_inputs

## Optional inputs

$optional_inputs

## Codebase access (critical)

This is a codebase-dependent (CBD) skill. Before invoking workflow code:

1. resolve the project-local AutoCAB config with
   `python scripts/cbd_config.py resolve --projectRoot <PROJECT_ROOT> --agent <AGENT>`;
2. validate this skill's entry with
   `python scripts/cbd_config.py validate --projectRoot <PROJECT_ROOT> --agent <AGENT> --skill $name --touch`;
3. if the config, roots, or sentinels are missing/stale, show the validation
   report and ask the user for replacement roots;
4. after confirmation, update with `cbd_config.py set`; validate again;
5. never pull, edit, or upgrade the reference codebase implicitly.

$codebase_requirements

The portable default config is
`.agents/autoCAB/codebase-dependent-skill.config`. Native Cursor, Claude Code,
GitHub Copilot, and compatibility paths are resolved by the helper. An explicit
`--config` or `AUTOCAB_CBD_CONFIG` takes precedence.

## Dependencies

$dependencies

## Runtime environment (critical)

$runtime_environment

The `compatibility` field advertises requirements but does not install them.
Use the machine-readable environment above or the sentinel-validated
codebase-owned specification; never rely on an environment nickname. Record
resolved versions and the codebase revision in every run manifest.

## Reproducibility and run reporting (critical)

Before the first setup, download, analysis, or validation command:

1. save the request used for execution to a local request file;
2. initialize a run with `scripts/record_run.py init`;
3. execute every shell command through `scripts/record_run.py exec`, including
   environment setup, composed-skill commands, failed attempts, and retries;
4. capture a resolved environment snapshot and direct tool versions;
5. use `record-action` for non-shell tools and manual steps;
6. finalize from a structured summary and run `record_run.py validate`;
7. read `run_summary.md` and display its key findings, outputs, parameters,
   warnings, skills/tools used, versions, and run directory in chat.

Each run must contain `agent_request.txt`, `agent_workflow.md`, `commands.sh`,
`logs/commands.log`, `logs/commands.jsonl`, `logs/parameters.jsonl`, `run_manifest.json`,
`run_manifest.md`, `run_summary.json`, and `run_summary.md`. The manifest must include every
effective parameter, input/output, skill invoked with version/path/hash, tool
version, resolved environment snapshot, codebase revision, command working
directory, exit status, failure/retry, assumption, and manual action.

Command shapes below are examples, not execution records. Only commands captured
by the recorder during this run establish what was actually done. Redact
credentials or protected data with explicit placeholders and record the
redaction; never write secrets into logs.

Follow the exact recorder commands and summary schema in
[references/runtime-reproducibility.md](references/runtime-reproducibility.md).

## Workflow

$workflow

## Outputs

$outputs

## Evidence and assumptions

Read [references/forge-provenance.md](references/forge-provenance.md) before
changing commands, defaults, or codebase sentinels. Proposed steps remain
subject to the approval recorded there.

## Quality checks

$quality_checks

## Failure and escalation

$failure_modes

- Missing or stale codebase root: stop and request a corrected location.
- Missing custom code: do not substitute an inferred implementation.
- Codebase change appears necessary: propose it separately and obtain explicit
  approval before editing.
