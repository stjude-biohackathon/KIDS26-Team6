# SkillSpec Contract

## Contents

- Why SkillSpec exists
- Top-level fields
- Evidence
- Steps
- Dependencies
- Runtime environment
- Codebase data
- Update assessment
- Validation invariants
- Rendering behavior
- Review lifecycle

## Why SkillSpec exists

SkillSpec is the evidence-linked intermediate representation between messy
source material and generated Markdown. It makes unsupported invention,
packaging decisions, unresolved questions, and review status machine-checkable.

Use [assets/skill-spec.schema.json](../assets/skill-spec.schema.json) as the
machine-readable schema and
[assets/skill-spec.template.json](../assets/skill-spec.template.json) as the
starting document.

## Top-level fields

Required fields:

- `schemaVersion`: currently `"1.1"`.
- `operation`: `create` or `update`.
- `requestedPackaging`: `cbd`, `std`, or `auto`. Treat omitted user preference
  as CBD at intake, then record the analyzed result in `packaging`.
- `packaging`: analyzed output, `cbd` or `std`.
- `decision`: `reuse`, `compose`, `novel`, `blocked`, or `no-update`.
- `name`: lowercase skill slug.
- `skillVersion`: semantic version written to `SKILL.md`, package metadata, and
  runtime manifests.
- `title`: human-readable title; may use uppercase CBD/STD.
- `description`: what the skill does and when to use it.
- `purpose`: evidence-bounded job to be done.
- `triggers` and `nonTriggers`: realistic routing language.
- `inputs` and `outputs`: semantic contracts.
- `steps`: ordered operational behavior.
- `dependencies`: public and custom runtime requirements.
- `runtimeEnvironment`: environment construction and locking contract.
- `evidence`: source-linked claims.
- `assumptions`: disclosed non-blocking assumptions.
- `unresolvedQuestions`: missing decisions or artifacts.
- `qualityChecks`: verifiable acceptance conditions.
- `failureModes`: trigger and response pairs.
- `existingSkillCoverage`: structural reuse/composition assessment.
- `codebase`: CBD roots/sentinels or an empty object for STD.
- `updateAssessment`: update delta or `null` for create.
- `licenseDecision`: summary of copying/redistribution status.

Inputs and outputs contain:

- `name`: stable semantic role;
- `description`;
- `required`: boolean;
- `evidenceIds`: supporting evidence.

## Evidence

Each evidence record contains:

```json
{
  "id": "ev-command-001",
  "source": "session-a.jsonl",
  "locator": "event 41",
  "basis": "observed",
  "confidence": "high",
  "summary": "samtools index completed with exit code 0"
}
```

Allowed bases are `observed`, `inspected_code`, `documented`,
`existing_skill`, `user_confirmed`, and `domain_inference`.

Use privacy-safe source identifiers in a shareable SkillSpec. Exact local paths
belong in the local source manifest, not the proposal.

## Steps

Each ordered step contains:

- `id`;
- `summary`;
- `status`: `supported`, `proposed`, `blocked`, `manual`, or `optional`;
- `basis`;
- `evidenceIds`;
- `existingSkill`: skill name when behavior is inherited/composed;
- `commandShape`: optional sanitized command with role placeholders;
- `dependencies`: dependency names used by the step;
- `rationale`;
- `approvalRequired`: boolean.

### Supported steps

A supported step needs at least one evidence ID or an `existingSkill`.
Documentation or inspected code may establish behavior even when the exact
command was not captured.

### Proposed steps

A proposed gap completion must:

- use `basis: domain_inference`;
- use `status: proposed`;
- set `approvalRequired: true`;
- include a non-empty `rationale`;
- use a command shape with semantic placeholders when a command is suggested;
- remain visibly pending in `REVIEW.md` and generated instructions.

After approval, add separate `user_confirmed` evidence and change status only
when the user has accepted the behavior.

### Blocked steps

Use `blocked` for missing custom logic or unresolved choices. Add a blocking
question with exact requested artifacts.

### Manual steps

Describe the human action and the artifact/confirmation produced. Do not
translate it into automation unless separately approved.

## Dependencies

Each dependency contains:

- `name`;
- `kind`: `public_tool`, `public_pipeline`, `custom_standalone`,
  `custom_integrated`, `missing`, `manual`, `existing_skill`, or
  `reference_data`;
- `required`;
- `evidenceIds`;
- `install`: public installation declaration, when relevant;
- `environmentPackage`: exact Conda/Pip/system package name used by
  `runtimeEnvironment` for a required `public_tool`, or exact
  `externalArtifacts` identity for a required `public_pipeline`;
- `versionConstraint`;
- `sourcePath`: local custom source, kept out of shareable prose;
- `bundlePath`: STD destination under the generated skill;
- `licenseStatus`: `approved`, `unknown`, `prohibited`, or `not_applicable`;
- `notes`.

`--allowCodeCopy` copies only regular files with `custom_standalone`,
`licenseStatus: approved`, and a safe relative `bundlePath`. It never copies a
directory, symlink, public tool, integrated component, or unknown-license file.
The complete closure must therefore be enumerated explicitly.

## Runtime environment

`runtimeEnvironment` contains:

```json
{
  "manager": "conda",
  "python": "3.11",
  "channels": ["conda-forge", "bioconda"],
  "condaDependencies": ["samtools=1.20", "bedtools=2.31.*"],
  "pipDependencies": [],
  "systemDependencies": [],
  "externalArtifacts": [],
  "containerImage": null,
  "codebaseEnvironmentFile": null,
  "lockStrategy": "direct-pins",
  "verified": true,
  "notes": ["Validated from a clean prefix on linux-64."]
}
```

Allowed managers are `none`, `conda`, `venv`, `container`, `system`, and
`codebase`.

- `conda` renders `environment.yml`; use it for mixed scientific binaries and
  Python/R packages.
- `venv` renders `requirements.txt` plus `python-requirement.txt`; use it only for
  pure-Python skills.
- `container` renders `container-image.txt`; use an immutable digest.
- `system` renders `system-requirements.txt`; use it only when packages cannot
  be expressed portably and document platform-specific setup.
- `externalArtifacts` renders `external-artifacts.txt` for versioned public
  pipelines, references, models, or data that are acquired separately from the
  package manager.
- `codebase` is valid for CBD only when a sentinel-validated environment or
  container specification exists in the reference codebase.
- `none` is valid only when no executable or non-stdlib runtime dependency
  exists.

`lockStrategy` is `none`, `direct-pins`, `container-digest`,
`codebase-owned`, or `unresolved`. A generated package may remain draft with
`unresolved`, but it must not be described as portable/stable. Never invent
versions to satisfy validation.

## Codebase data

For CBD, `codebase.roots` is an array of:

```json
{
  "path": "/local/reference/root",
  "sentinels": ["scripts/run.py", "config/defaults.yaml"],
  "gitRemote": "ssh://example/repository.git"
}
```

The local path is used to seed machine-local config and should be sanitized from
shareable artifacts. For STD, use an empty `roots` array.

## Update assessment

For `operation: update`, provide:

- `targetSkill`;
- `materialDelta`: boolean;
- `summary`;
- `migrationSuggested`: `none`, `cbd-to-std`, or `std-to-cbd`;
- `migrationApproved`: boolean.

`decision: no-update` requires `materialDelta: false`. A migration cannot be
rendered as final while `migrationApproved` is false.

## Validation invariants

The bundled validator enforces:

1. all evidence IDs resolve;
2. all dependency names used by steps resolve;
3. create names end in the analyzed `-cbd` or `-std` suffix;
4. a supported step has evidence or an existing skill;
5. a proposed step follows the approval contract above;
6. a blocked step has at least one blocking unresolved question;
7. `no-update` is an update with no material delta;
8. `reuse`, `blocked`, and `no-update` do not create an installable proposal;
9. CBD has codebase roots/sentinels unless blocked by a precise question;
10. STD has no `custom_integrated` runtime dependency;
11. STD custom files name safe bundle paths and require approved licenses before
    copying;
12. unapproved mode migration blocks final rendering;
13. executable dependencies have a compatible machine-readable runtime
    environment;
14. `none` is not used to hide public tools or custom runtime libraries;
15. a verified environment cannot use `lockStrategy: unresolved`;
16. the semantic `skillVersion` is consistent across instructions, package
    metadata, and runtime identity;
17. descriptions and names meet Agent Skills limits.

Validation cannot prove scientific correctness or complete dynamic dependency
discovery. Those remain human review and smoke-test obligations.

## Rendering behavior

`scripts/forge_skill.py`:

- validates before writing;
- writes reproducibility/review artifacts into an explicit run directory;
- renders `SKILL.md` from the CBD or STD template;
- creates a skill-local README, changelog, provenance reference, and evaluation
  prompts;
- writes `skill-package.json` and the applicable environment specification;
- bundles `scripts/record_run.py` into every installable proposal;
- adds the CBD config helper only for CBD;
- copies custom STD files only with `--allowCodeCopy`;
- creates no proposal directory for `reuse`, `blocked`, or `no-update`.

Rendering is deterministic for the same SkillSpec and skill-forge version except
for timestamps/run IDs in audit files.

## Review lifecycle

Suggested statuses:

1. `draft` — initial SkillSpec;
2. `blocked` — required evidence/artifacts missing;
3. `reviewable` — schema valid, proposed commands clearly marked;
4. `approved` — user accepted behavior and code-copy/migration decisions;
5. `validated` — package checks and smoke tests passed;
6. `installed` — explicitly promoted from staging.

The renderer does not claim the last three states. The calling agent records
them after the corresponding human decision and checks.
