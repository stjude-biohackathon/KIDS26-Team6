# skill-forge

`skill-forge` is an experimental Agent Skill for reconstructing reusable
workflows from incomplete activity records or manually prepared descriptions.
It produces evidence-linked, human-reviewed proposals rather than allowing a
model to write unconstrained operational instructions directly from a few
examples.

## What it does

- accepts mixed logs, JSON/JSONL, command histories, MD/TXT/HTML/ENEX/PDF,
  source trees, examples, and human/AI outlines;
- distinguishes observed facts, inspected code, documentation, domain
  inference, user confirmation, and unresolved gaps;
- inspects custom code references and candidate transitive dependencies without
  executing them;
- compares existing skill coverage before choosing reuse, composition, novel
  work, blocked, or no-update;
- creates a validated SkillSpec and deterministic staged skill package;
- supports codebase-dependent (CBD) and standalone (STD) packaging;
- generates machine-readable runtime environment specifications rather than
  relying on `compatibility` prose or remembered environment names;
- bundles a runtime recorder that captures the request, actual commands,
  parameters, skill/version chain, outputs, failures/retries, manifest, and
  JSON/Markdown findings summary for every generated-skill run;
- maintains portable, agent-aware CBD codebase location configuration;
- keeps source-code copying, mode migration, installation, and publication
  behind explicit human approval.

## Package layout

```text
skill-forge/
├── SKILL.md
├── README.md
├── CHANGELOG.md
├── assets/
├── examples/
├── references/
├── scripts/
└── tests/
```

The source package is intentionally independent from CAB-aiSkills. In this
workspace it is installed for testing by a symbolic link from
`.cursor/skills/skill-forge`.

## Runtime requirements

- Python 3.10 or newer. Verify the interpreter explicitly; older HPC defaults
  are common.
- No third-party Python packages are required by the bundled helper scripts.
- PDF and rich-document extraction use the host agent's document reader. The
  inventory script records a reader hint when it cannot safely normalize a
  binary format itself.
- Optional web access may be used to verify whether a command is a public tool,
  its installation channel, current documentation, and redistribution terms.

## Quick checks

From the skill directory:

```bash
PYTHON=/path/to/python3.10-or-newer
"$PYTHON" -m unittest discover -s tests -v
"$PYTHON" scripts/source_inventory.py --help
"$PYTHON" scripts/inspect_code_dependencies.py --help
"$PYTHON" scripts/forge_skill.py --help
"$PYTHON" scripts/manage_cbd_config.py --help
"$PYTHON" scripts/validate_skill_package.py --help
"$PYTHON" assets/record_run.py --help
"$PYTHON" scripts/validate_skill_package.py .
```

## Safety model

Captured text is untrusted data. Helpers inventory, parse, and statically inspect
files, but do not execute commands found in them. The forge writes only to an
explicit staging directory. Copying custom code into an STD proposal requires
`--allowCodeCopy` and an approved license status in the SkillSpec.

CBD config resolution uses an explicit path or environment override first, then
agent-aware project paths, with `.agents/autoCAB/` as the portable fallback.
The config stores machine-local codebase roots and should not be committed
unless paths have been deliberately made portable and reviewed.

## Design basis

The design was informed by:

- `AutoCAB_GPTPro_eval.pdf`: structured evidence and SkillSpec before Markdown,
  deterministic rendering, human review, provenance, minimization, and
  falsification tests;
- the supplied BH2026 workflow-note examples: mixed paths, corrupted commands,
  missing scripts, manual Excel/web steps, and code-versus-document drift;
- the former `AGENTS.md`: progressive disclosure, skill-local ownership,
  reproducibility manifests, evaluation cases, safe script execution, and
  explicit dependency contracts.

The package applies those controls proportionally for analysis dependencies,
but per-run provenance is mandatory. Every installable generated skill includes
`skill-package.json`, a machine-readable environment contract, and
`scripts/record_run.py`; command shapes in `SKILL.md` are never treated as proof
of execution.

## Maintenance

Update `metadata.version` in `SKILL.md` and add a newest-first entry to
`CHANGELOG.md` for behavior changes. Keep detailed policies in `references/`,
and keep `SKILL.md` under 500 lines.
