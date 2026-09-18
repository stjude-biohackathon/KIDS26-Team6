# Modes and Codebase Configuration

## Contents

- Mode model
- CBD creation
- STD creation
- Updating an existing skill
- Naming
- CBD config schema
- Agent-specific config paths
- Resolution precedence
- Freshness checks
- Safe updates
- CBD and STD migration

## Mode model

Keep the requested operation separate from runtime packaging:

```text
operation = create | update
packaging = cbd | std
```

This avoids treating "update existing skill" as a third packaging type. An
existing CBD or STD skill can be updated without changing how it obtains code.

## CBD creation

Codebase-dependent (CBD) is the default requested packaging when custom code may
be involved.

Use CBD when:

- the workflow depends on a private or team-maintained repository;
- custom scripts form an integrated pipeline or have a large dependency closure;
- one shared implementation must remain the source of truth;
- redistribution rights are unclear;
- copying code would make fixes diverge across skill packages.

A CBD skill:

- ends in `-cbd`;
- calls code from one or more configured roots;
- records required relative sentinel paths;
- declares public tools separately;
- may contain small skill-specific helpers under `scripts/`;
- does not copy integrated source code from the reference codebase;
- validates roots before execution;
- never pulls, edits, or upgrades the codebase automatically.

Changes to the original codebase are outside normal CBD generation. If
instrumentation is required, propose a separate, explicit change. Logger or
manifest additions must preserve defaults and outputs unless the user approves
otherwise.

## STD creation

Use standalone (STD) when:

- all behavior is instructions plus public installable tools/pipelines; or
- every required custom helper is small, independently usable, available,
  redistributable, and copied with its complete runtime closure.

An STD skill:

- ends in `-std`;
- has no runtime dependency on the source codebase;
- declares public tools as installable dependencies instead of vendoring them;
- stores approved custom code and required static assets inside the skill;
- removes machine-specific absolute paths;
- includes a reproducible environment contract when external dependencies exist;
- tests the bundled copy, not the original source path.

If CBD was requested but analysis finds only public tools or self-contained
shell operations, select STD and explain the override. If any custom runtime
reference remains external, the proposal is not yet standalone.

## Updating an existing skill

Before editing:

1. inspect the current skill, scripts, references, tests, and changelog;
2. translate new evidence into actions, I/O roles, branches, validations, and
   failure modes;
3. compare those requirements against current coverage;
4. identify a material delta.

Material deltas include a new supported input, optional branch, failure
recovery, dependency, output, validation rule, or corrected behavior. New
examples of already-supported behavior are normally evaluation additions, not a
workflow rewrite.

Return `no-update` when there is no material delta. Do not churn prose merely to
make a change.

## Naming

Agent Skill identifiers must use lowercase letters, numbers, and hyphens.

- New CBD: `<descriptive-name>-cbd`
- New STD: `<descriptive-name>-std`
- Display title/metadata: may say `CBD` or `STD`

Do not silently rename an existing skill during update. A packaging migration
normally requires a suffix change and therefore an explicit migration plan.

## CBD config schema

Use JSON arrays, not comma-separated path strings:

```json
{
  "schemaVersion": "1.0",
  "skills": {
    "example-workflow-cbd": {
      "roots": [
        "/local/path/to/reference-code",
        "/local/path/to/shared-configs"
      ],
      "sentinels": [
        "scripts/run_workflow.py",
        "config/defaults.yaml"
      ],
      "gitRemotes": {
        "/local/path/to/reference-code": "ssh://example/repository.git"
      },
      "lastValidatedUtc": "2026-09-17T14:00:00Z"
    }
  }
}
```

Sentinels are relative paths that must exist under at least one configured root.
They establish that the path is the expected codebase, not merely an existing
directory. An expected Git remote is optional and must not trigger a fetch.

The config is machine-local because roots often contain absolute paths. Warn if
it is tracked by source control. Do not edit `.gitignore` without permission.

## Agent-specific config paths

The products below officially document skill directories, not AutoCAB config
files. AutoCAB therefore uses the adjacent `autoCAB/` location as its own
convention:

- Cursor native: `.cursor/autoCAB/codebase-dependent-skill.config`
- Claude Code native: `.claude/autoCAB/codebase-dependent-skill.config`
- GitHub Copilot native: `.github/autoCAB/codebase-dependent-skill.config`
- Codex and cross-agent standard:
  `.agents/autoCAB/codebase-dependent-skill.config`
- Codex compatibility path requested by early AutoCAB designs:
  `.codex/autoCAB/codebase-dependent-skill.config`
- Generic/unknown agent:
  `.agents/autoCAB/codebase-dependent-skill.config`

Official directory evidence, checked 2026-09-17:

- Cursor discovers `.cursor/skills/` and `.agents/skills/`:
  <https://cursor.com/docs/skills>
- Claude Code project skills use `.claude/skills/`:
  <https://docs.anthropic.com/en/docs/claude-code/skills>
- GitHub Copilot accepts `.github/skills/`, `.claude/skills/`, and
  `.agents/skills/`:
  <https://docs.github.com/en/copilot/how-tos/copilot-cli/customize-copilot/add-skills>
- Codex repository skills use `.agents/skills/`:
  <https://developers.openai.com/codex/skills>
- The Agent Skills implementation guide recommends `.agents/skills/` for
  interoperability:
  <https://agentskills.io/client-implementation/adding-skills-support>

The path registry is deliberately extendable. Do not invent a product-specific
directory for an unknown agent; use the portable `.agents/` path or an explicit
override.

## Resolution precedence

`scripts/manage_cbd_config.py` resolves a config in this order:

1. explicit `--config`;
2. `AUTOCAB_CBD_CONFIG`;
3. exactly one existing recognized project config;
4. explicit `--agent` or `AUTOCAB_AGENT` mapping;
5. portable `.agents/autoCAB/codebase-dependent-skill.config`.

If multiple recognized configs already exist, fail and ask which is canonical.
Do not merge machine-local roots automatically.

Supported agent values are `cursor`, `claude`, `copilot`, `codex`, and
`generic`. `codex` writes the current official `.agents/` path while still
recognizing an existing `.codex/` compatibility file.

## Freshness checks

Before a CBD skill runs:

1. resolve the project root and config path;
2. find the skill entry;
3. verify each configured root exists, is a readable directory, and is not a
   broken symlink;
4. verify every sentinel exists under at least one root;
5. when configured, compare the local Git `remote.origin.url` without fetching;
6. optionally record the current local commit and dirty state in run provenance;
7. update `lastValidatedUtc` only after validation succeeds.

A timestamp alone never proves that code is current. Do not equate "up to date"
with "latest remote commit" unless the user asks for a network check. Never pull
automatically.

## Safe updates

When roots are missing or stale:

- show the failing roots and sentinels;
- ask for replacement roots or an explicit config path;
- validate replacements before writing;
- update atomically with restrictive permissions where supported;
- preserve unrelated skill entries;
- record the config path, not its machine-specific content, in shareable
  provenance.

The config helper performs no interactive prompting. The calling agent must
obtain user confirmation before invoking `set`.

## CBD and STD migration

### CBD to STD

Advantages:

- no dependency on a separately located repository;
- simpler transfer and first-run behavior.

Costs and risks:

- copied code can drift from the maintained source;
- larger package and duplicate maintenance;
- redistribution/license review;
- complete transitive closure and new tests are required.

### STD to CBD

Advantages:

- one maintained source of truth;
- easier team-wide updates for large integrated systems;
- smaller skill package.

Costs and risks:

- every user needs codebase access and local config;
- path/access failures become runtime concerns;
- source updates can change behavior unless versions/commits are recorded.

When a new log suggests migration, present these trade-offs plus concrete
dependency evidence and ask the user. Do not change packaging or suffix before
approval.
