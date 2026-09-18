# Validation and Provenance

## Contents

- Forge run layout
- Provenance responsibilities
- Review report
- Generated package checks
- CBD checks
- STD checks
- Update checks
- Evaluation set
- Installation gate
- User-facing handoff

## Forge run layout

Use an explicit directory outside the `skill-forge` source package:

```text
<outputRoot>/skill-forge-<YYYYMMDDTHHMMSSZ>/
├── agent_request.txt
├── agent_workflow.md
├── skill-spec.json
├── REVIEW.md
├── provenance.json
├── run_metadata.json
├── evidence/
│   ├── source-manifest.json
│   ├── dependency-report.json
│   ├── coverage-report.json
│   └── evidence.json
├── proposal/
│   └── <generated-skill>/
│       ├── SKILL.md
│       ├── README.md
│       ├── CHANGELOG.md
│       ├── references/
│       ├── examples/
│       └── scripts/
└── logs/
    ├── skill-forge.log
    └── commands.log
```

`proposal/` is absent for `reuse`, `blocked`, and `no-update`. Additional
normalized evidence stays under `evidence/` and should be excluded from a
published skill unless intentionally sanitized and approved.

## Provenance responsibilities

The agent:

- writes a sanitized `agent_request.txt`;
- records source inspection, goal inference, repairs, coverage, and decisions in
  `agent_workflow.md`;
- prepares source/dependency/coverage manifests;
- obtains approval for proposed commands, code copying, migrations, and
  installation;
- runs validation and records results.

The helper scripts:

- hash and inventory explicit inputs;
- record bounded static-analysis findings;
- validate SkillSpec invariants;
- write deterministic proposal files and run metadata;
- avoid executing captured or inspected code.

The reviewer:

- confirms intent and workflow boundary;
- resolves blocking questions;
- distinguishes accepted inference from observed history;
- confirms redistribution rights;
- approves or rejects the proposal.

## Privacy-safe records

Shareable provenance should contain:

- source IDs/hashes rather than sensitive absolute paths;
- semantic roles instead of sample/patient identifiers;
- redacted command shapes;
- skill-forge version and schema version;
- coverage decision and packaging rationale;
- code source commit/hash when approved for disclosure;
- validation commands and outcomes.

Keep verbatim prompts, exact paths, terminal output, and private source in a
separate local-only area only when needed and approved. Never put credentials in
provenance.

## Review report

`REVIEW.md` must make these decisions visible:

- inferred goal and confidence;
- requested versus analyzed packaging;
- reuse/compose/novel/blocked/no-update decision;
- evidence count by basis/confidence;
- observed workflow versus generalized workflow;
- repaired or proposed commands;
- dependencies by classification;
- missing artifacts and blocking questions;
- manual checkpoints;
- code-copy and license status;
- update delta and possible migration;
- checks completed and pending.

Proposed commands must be grouped under an approval heading. Do not bury them
inside otherwise supported workflow prose.

## Generated package checks

At minimum:

```bash
python scripts/validate_skill_package.py <proposal/skill-dir> \
  --expectedPackaging cbd
```

or:

```bash
python scripts/validate_skill_package.py <proposal/skill-dir> \
  --expectedPackaging std
```

Then verify:

- exactly one root `SKILL.md`;
- directory name matches frontmatter `name`;
- lowercase/hyphen identifier and correct suffix;
- description states what and when;
- `SKILL.md` is under 500 lines;
- relative links resolve inside the package;
- no unresolved template tokens;
- scripts expose `--help` when they are CLIs;
- dependencies appear consistently in instructions and environment files;
- expected output and failure behavior are explicit;
- package contains no secrets or raw evidence.

## CBD checks

- `-cbd` suffix.
- `scripts/cbd_config.py` exists and is documented.
- Required sentinels identify the expected reference code.
- Config path resolution is agent-aware or explicitly overridden.
- Missing/stale roots fail before workflow execution.
- No integrated reference code was copied into the skill.
- Public tools are independently declared.
- Source version/remote is recorded when known.
- No instruction auto-pulls or edits the codebase.

Test config behavior in a temporary project root; never create a developer's
real machine-local config merely for a unit test.

## STD checks

- `-std` suffix.
- No CBD config helper or runtime config dependency.
- No private absolute paths.
- No symlinks escaping the package.
- No `custom_integrated` or `missing` runtime dependency.
- Every `custom_standalone` file and static asset exists at its bundle path.
- Copied code has approved redistribution status and provenance.
- Imports, source calls, subprocesses, configs, and assets resolve against the
  bundled package or declared public dependencies.
- Tests execute the bundled copy.

Run static inspection again on bundled scripts to catch references introduced
by path rewriting or an incomplete copy.

## Update checks

- Existing behavior was inventoried before drafting.
- The coverage report names the material delta.
- No-update leaves the target skill unchanged.
- Existing defaults and outputs remain stable unless a breaking change was
  explicitly approved.
- Version/changelog change matches impact.
- Packaging migration and suffix rename have explicit approval and migration
  notes.
- Regression tests retain prior supported scenarios.

## Evaluation set

For a non-trivial generated skill, include:

1. standard success;
2. variant/edge case;
3. missing or ambiguous input;
4. misuse/out-of-scope request;
5. adversarial external content when sources or data may contain instructions;
6. failure of an external tool or codebase sentinel;
7. manual checkpoint when the workflow contains one.

For `skill-forge` itself, also test public-only STD override, missing private
code, integrated CBD code, no-op update, and unapproved migration.

## Installation gate

Staging is not installation. Install only after:

- blockers are resolved;
- proposed commands are approved or removed;
- code-copy rights and closure are verified;
- package validation succeeds;
- script/unit tests and a smallest safe smoke run succeed, or limitations are
  clearly accepted by the user;
- the user approves the destination.

Scope installation to the generated skill directory and any explicitly
requested discovery link. Do not update unrelated skills or repository policy.

## User-facing handoff

Report:

- staging and final paths;
- decision and CBD/STD rationale;
- important assumptions and manual steps;
- proposed commands accepted/rejected;
- missing artifacts or remaining limitations;
- dependencies and tested versions;
- validation commands and outcomes;
- codebase config path for CBD;
- copied-code provenance for STD;
- whether any source code was changed (normally no).
