# Changelog

## 2026-09-18 — 0.2.0

- Made machine-readable runtime environments mandatory for generated skills;
  `compatibility` prose alone is no longer accepted as dependency setup.
- Added generated `skill-package.json`, Conda/Python/container/system
  environment artifacts, and clean-environment validation gates.
- Added the bundled `record_run.py` runtime recorder for exact command, cwd,
  exit status, failed/retried command, request, parameter, input/output,
  composed-skill, resolved-version, and action provenance.
- Required JSON and human-readable manifests (`run_manifest.json` and
  `run_manifest.md`), JSON/Markdown summaries, `commands.sh`, command logs, and
  an interactive findings handoff after every generated-skill run.
- Bumped the SkillSpec schema to 1.1 with `runtimeEnvironment`.

## 2026-09-17 — 0.1.0

- Added the initial evidence-driven `skill-forge` workflow.
- Added independent create/update and CBD/STD mode contracts.
- Added bounded source inventory, static code-reference inspection,
  deterministic SkillSpec rendering, CBD config management, and package
  validation helpers.
- Added human approval gates for inferred missing commands, custom-code
  copying, mode migration, codebase changes, installation, and publication.
- Added sanitized evaluations derived from incomplete bioinformatics workflow
  records.
