# Validation and Provenance

## Contents

- Forge run layout
- Generated-skill runtime layout
- Command and action recording
- Request capture
- Summaries and interactive handoff
- Environment reproducibility
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

## Generated-skill runtime layout

The forge audit above documents skill creation. It does not replace the audit
trail required every time the generated skill runs.

```text
<outputRoot>/<skill-name>-<YYYYMMDDTHHMMSSZ>/
├── agent_request.txt
├── agent_workflow.md
├── commands.sh
├── run_manifest.json
├── run_manifest.md
├── run_summary.json
├── run_summary.md
├── <primary outputs>
└── logs/
    ├── commands.log
    ├── commands.jsonl
    ├── parameters.jsonl
    ├── <step-id>.stdout.log
    └── <step-id>.stderr.log
```

Initialize the run directory before environment setup, downloads, data
preparation, or analysis. Runtime outputs must not be written into the skill
package.

### `run_manifest.json`

The canonical machine-readable record contains:

- skill name/version/package path and packaging mode;
- run ID and UTC start/end;
- request capture mode, request SHA-256, and redactions;
- ordered commands/actions with exact display command, working directory,
  start/end, exit status, and stdout/stderr log paths;
- all effective parameters, including defaults that affect output;
- semantic inputs and outputs, with paths and hashes when practical;
- every skill invoked, its version/path/hash or repository commit, its role, and
  invocation order;
- resolved interpreter, package, CLI, pipeline, container, and codebase
  versions;
- a resolved environment snapshot (`conda list --explicit`, `pip freeze --all`,
  immutable image digest, or codebase spec/revision) and its hash;
- manual steps, failed attempts, retries, warnings, assumptions, and status.

Every required public tool/pipeline, integrated codebase, and reference-data
dependency must have a corresponding resolved version record; recording Python
alone does not satisfy a samtools or nf-core dependency.

The environment snapshot must live inside the run directory and be declared as
the output of its successful recorded environment command. An arbitrary
external file or unrecorded export is insufficient.

`run_manifest.md` is the complete human-readable counterpart with the same run
identity, request hashes/redactions, environment, ordered commands/actions,
parameters, inputs/outputs, skills, versions, and limitations.

### `commands.sh`

This is the copy-pasteable human replay script. Record commands after shell
quoting, in execution order, with explicit `cd` context. Preserve failed
attempts in `commands.jsonl`/`commands.log`; normally include only intended
replay commands in `commands.sh`, annotating corrected retries.

Redact secrets as environment placeholders such as `${API_TOKEN}`. Never trade
credential safety for byte-for-byte command capture.

## Command and action recording

All shell commands that affect a run must go through
`scripts/record_run.py exec`, including:

- environment creation or activation helpers;
- downloads and reference preparation;
- analysis commands and pipelines;
- composed-skill commands;
- validation, conversion, and report generation;
- failed commands and corrected retries.

Classify each command as setup, workflow, validation, environment, or
provenance, and declare material consumed/produced paths. A successful
command-driven skill run needs at least one successful workflow command; an
arbitrary manual action cannot stand in for it.

Attach effective parameters to the command that uses them or record them with
`record-parameter`; the final parameter object must match those events exactly.
For command-driven runs, every summarized input/output path must be linked to a
command's `--consumes`/`--produces` record.

Use `record-action` for native tools, MCP calls, notebook/GUI actions, and
manual checkpoints that cannot be wrapped as shell commands. Record enough
information to reproduce or consciously repeat the action without implying it
was a CLI.

A command shape in `SKILL.md` is documentation. Only a command recorded during
the actual run is execution provenance.

Do not backfill a missing runtime record from command shapes. Mark the prior run
non-reproducible. Terminal events and tool logs may support an explicitly
labeled forensic reconstruction, but reconstructed commands must retain their
evidence basis and are not equivalent to recorder-captured execution.

## Request capture

`agent_request.txt` records the request used to choose parameters and outputs.
For non-sensitive work, preserve it verbatim. If it contains credentials, PHI,
or prohibited sensitive values:

1. write a faithful redacted request;
2. store its capture mode and redaction descriptions in the manifest;
3. record the SHA-256 of the original request when it can be computed safely;
4. store a verbatim private copy only after explicit user approval.

Never claim a redacted request is verbatim. Sanitized capture requires both an
original request SHA-256 and at least one explicit redaction description.

## Summaries and interactive handoff

At finalization write both:

- `run_summary.json`: status, what was done, findings, parameters, inputs,
  outputs, skills used, versions, warnings, assumptions, manual steps, and
  unresolved limitations;
- `run_summary.md`: the same information in concise human-readable form,
  including an ordered multi-command account.

After validating both files, the agent must display a concise summary in chat:

- what it did;
- key findings/statistics;
- important parameters and branches;
- deliverables and run directory;
- skills/tools and resolved versions;
- warnings, manual actions, failures/retries, and limitations;
- pointer to `run_manifest.json`, `run_manifest.md`, and `commands.sh`.

Do not report success from chat alone. The files are the durable record.

## Environment reproducibility

`compatibility` is standard Agent Skills metadata, but it is not an installer or
lockfile. Any generated skill with executable dependencies must include:

- `skill-package.json` describing the runtime manager and dependency contract;
- `environment.yml` for Conda/Mamba mixed binary/scientific environments, or
  `requirements.txt` plus a Python constraint for pure Python;
- `external-artifacts.txt` for versioned public pipelines, references, models,
  or data acquired outside the package manager;
- an immutable container digest when a container is the runtime;
- a named, sentinel-validated codebase environment spec when CBD deliberately
  delegates environment ownership;
- setup/reuse instructions and a clean-environment smoke check;
- resolved versions in each run manifest.

`requirements.txt` is not sufficient for tools such as HOMER, samtools,
bedtools, Bowtie2, MACS2, or UCSC binaries. Prefer `environment.yml`.

Direct version bounds are acceptable when justified. For stronger repeatability,
generate platform lock files (`conda-lock`) or use a container digest after the
base environment has been verified. Never invent a pin unsupported by evidence.

## Provenance responsibilities

The agent:

- writes the request used for execution to `agent_request.txt`, verbatim when
  non-sensitive and faithfully redacted with provenance when required;
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
- bundle the runtime recorder and machine-readable environment artifacts;
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
- `skill-package.json` and the environment specification agree;
- `scripts/record_run.py` exists and passes `--help`;
- `SKILL.md` initializes the run before its first command and finalizes before
  reporting success;
- a recorder smoke run produces request, commands, manifest, JSON/Markdown
  summaries, and logs;
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
- Public/runtime dependencies are installable from the bundled environment
  contract or a sentinel-validated codebase-owned specification.
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
- A clean machine can create the declared environment without the source
  codebase.

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
