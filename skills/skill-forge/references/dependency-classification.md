# Dependency and Code Classification

## Contents

- Classification outcomes
- Public tools and pipelines
- Standalone custom helpers
- Integrated custom code
- Missing or opaque code
- Manual operations
- Recursive inspection
- Environment inference
- Packaging closure
- License and provenance
- Escalation report

## Classification outcomes

Classify every command, script, pipeline, config, and runtime asset into one of
these SkillSpec kinds:

- `public_tool`
- `public_pipeline`
- `custom_standalone`
- `custom_integrated`
- `missing`
- `manual`

Classification is evidence-based and may change after source inspection. A file
ending in `.py` is not automatically standalone, and a repository is not
automatically integrated.

## Public tools and pipelines

A tool is public when its identity, installation source, documentation, and
license can be verified independently of the user's private codebase.

Examples include `samtools`, `bedtools`, `bowtie2`, and a versioned public
nf-core pipeline. Treat these as dependencies:

- record the executable/package and installation channel;
- record a version or compatibility range when behavior depends on it;
- record external data/reference requirements;
- declare network, container, scheduler, or architecture constraints;
- do not copy the tool's implementation into the skill.

A command name alone is not proof. Resolve aliases, wrapper scripts, and
same-named local files before classifying.

Public pipelines still need versioned configuration, profiles, samplesheet
contracts, and output validation. A private fork of a public pipeline is custom
code unless the generated skill deliberately targets the public upstream.

## Standalone custom helpers

A custom helper is standalone only when all of the following hold:

- it has one focused purpose and a stable invocation;
- its source and required assets are available;
- local imports/sourced files can be bundled;
- subprocess/system calls are public dependencies or bundled helpers;
- configuration does not depend on hidden repository state;
- machine-specific paths can be parameterized without changing behavior;
- redistribution is approved for STD packaging;
- it can be tested independently with a small fixture.

A helper may have several files and still be standalone. "Standalone" describes
runtime independence, not file count.

## Integrated custom code

Classify as `custom_integrated` when code:

- imports many repository-internal modules;
- depends on shared configs, schemas, templates, or reference trees;
- assumes repository-relative layout or generated state;
- invokes private wrappers or services;
- is one stage of a larger custom pipeline with implicit handoffs;
- relies on deployment, credentials, or scheduler infrastructure not captured
  by the script;
- cannot be isolated without changing its public behavior.

CBD is normally appropriate. Do not recursively copy a whole private repository
into an STD skill merely to satisfy closure.

## Missing or opaque code

Classify as `missing` when:

- a referenced local path cannot be accessed;
- an attachment or generated script is named but absent;
- a wrapper resolves to an unknown executable;
- a private repository or submodule is unavailable;
- source is present but dynamically loads unknown code or configuration;
- a GUI/manual action hides essential transformation logic.

Do not replace missing custom logic with a plausible implementation. Record the
exact artifact requested, how it was referenced, and which workflow steps it
blocks.

## Manual operations

Use `manual` for intentional user actions such as:

- editing an Excel configuration;
- selecting data or columns in a GUI;
- reviewing a plot or quality report;
- resolving sample-name mappings;
- running an approved web service;
- granting access or selecting a cluster profile.

Document prerequisites, user decision, resulting artifact, and post-step
validation. Manual does not mean optional.

## Recursive inspection

Inspect only approved roots and use bounded traversal. Static analysis is a
discovery aid, not proof that all dynamic dependencies were found.

### Python

Inspect:

- `import` and `from ... import ...`;
- package-relative and repository-local modules;
- `subprocess` calls, `os.system`, shell strings, and executable lookup;
- dynamic imports, plugins, `sys.path` changes, and environment variables;
- `open`, `Path`, resource loaders, templates, and bundled data;
- packaging metadata and optional dependency groups.

Distinguish standard-library imports from third-party and local imports.
Imported libraries become dependency candidates; verify actual runtime use and
version constraints.

### Bash and shell

Inspect:

- `source` and dot-included files;
- commands at pipeline and conditional boundaries;
- `bash`, `python`, `Rscript`, `nextflow`, Java, and container entrypoints;
- generated scripts/configs and here-documents;
- `module`, Conda, environment activation, scheduler, and remote commands;
- redirections and output paths.

Aliases and shell functions may hide implementations; request their definitions
when material.

### R

Inspect:

- `library`, `require`, namespace `pkg::function`, and package installation;
- `source` and local data/resource loading;
- `system`, `system2`, shell pipes, and workflow packages;
- renv/packrat metadata and Bioconductor version assumptions.

### Nextflow and workflow engines

Inspect:

- `include`/module paths, subworkflows, plugins, and custom functions;
- `nextflow.config`, profiles, params files, and secrets indirection;
- process `script`, `shell`, `exec`, `container`, and `conda` declarations;
- referenced templates, bin scripts, schemas, and assets;
- workflow/pipeline version and private fork identity;
- executor, scheduler, storage, resume, and publish behavior.

For Snakemake or other engines, apply the same principles to rules, includes,
profiles, wrappers, environments, containers, and resource files.

## Environment inference

Commands such as `mamba create`, `module load`, `source activate`, container
tags, and workflow profiles are dependency evidence. They may be stale or
site-specific.

Build an environment contract from:

- verified public packages and binaries;
- imports and subprocess candidates from inspected code;
- script/package metadata;
- version-sensitive behavior;
- reference data and network requirements;
- architecture, OS, container, scheduler, and filesystem constraints.

Do not preserve an environment nickname as the only setup instruction. Do not
pin versions solely because one old log used them; document why the pin matters.

## Packaging closure

### CBD closure

The package must contain:

- instructions and skill-specific helpers;
- a config resolver;
- root and sentinel requirements;
- public dependency declarations;
- expected codebase version/remote information when known;
- validation and failure behavior.

The referenced custom code remains external.

### STD closure

The package must contain:

- every approved custom source file required at runtime;
- local modules, sourced scripts, templates, schemas, and static assets;
- declared public dependencies;
- portable configuration/defaults;
- licenses/notices required for copied code;
- tests that run against bundled paths.

Reject final STD validation when an absolute private path, unresolved custom
reference, or `custom_integrated` dependency remains.

## License and provenance

Before copying code:

1. identify owner and source repository/path;
2. inspect license and redistribution terms;
3. obtain user confirmation when ownership or permission is unclear;
4. preserve required notices;
5. record source commit/hash and modifications;
6. avoid attributing generic agent-written glue to unrelated authors.

Unknown redistribution rights favor CBD, not automatic copying.

## Escalation report

For each unresolved dependency, report:

- reference as observed;
- likely role;
- attempted resolution roots;
- files/modules/configs that appear downstream;
- blocked workflow steps and outputs;
- exact artifact or access required;
- whether a public alternative exists and whether adopting it would change
  behavior;
- safe partial progress that can continue.

If a public alternative would change the established method, offer it as a
separate proposal for approval rather than silently substituting it.
