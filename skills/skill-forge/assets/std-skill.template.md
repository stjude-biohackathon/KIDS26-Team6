---
name: $name
description: $description_yaml
compatibility: $compatibility_yaml
metadata:
  version: "$skill_version"
  status: draft
  packaging: STD
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

## Standalone contract (critical)

This standalone (STD) skill must run without the source codebase from which it
was derived. Public programs remain declared dependencies; approved custom
helpers, local modules, configs, templates, and runtime assets must live inside
this package.

Do not add a machine-specific codebase path or AutoCAB CBD config dependency.
If a required custom file is absent, stop and repair the package rather than
falling back to the original source tree.

## Dependencies

$dependencies

## Runtime environment (critical)

$runtime_environment

The `compatibility` field advertises requirements but does not install them.
Use the machine-readable environment above, not an environment name remembered
from another machine. Record resolved versions in every run manifest.

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
version, resolved environment snapshot, command working directory, exit status,
failure/retry, assumption, and manual action.

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
changing commands, defaults, or bundled code. Proposed steps remain subject to
the approval recorded there.

## Quality checks

$quality_checks

- Resolve bundled scripts and assets within this skill directory.
- Confirm no private absolute paths or unresolved external custom-code
  references remain.

## Failure and escalation

$failure_modes

- Missing bundled helper or asset: stop; do not read it from the source
  codebase.
- Unknown redistribution rights: do not add the file; request a decision or use
  CBD packaging.
