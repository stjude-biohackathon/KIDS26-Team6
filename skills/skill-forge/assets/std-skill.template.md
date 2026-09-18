---
name: $name
description: $description_yaml
compatibility: $compatibility_yaml
metadata:
  version: "0.1.0"
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
