---
name: $name
description: $description_yaml
compatibility: $compatibility_yaml
metadata:
  version: "0.1.0"
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
