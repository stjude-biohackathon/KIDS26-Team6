# Runtime Reproducibility Contract

This contract applies to every execution, including partial and failed runs.
The forge-time records in `references/forge-provenance.md` do not replace it.

## 1. Capture the request

Write the request used to choose inputs, parameters, branches, and outputs to a
temporary text file. Preserve non-sensitive requests verbatim. For sensitive
requests, create a faithful redacted copy, describe each redaction, and retain
the original SHA-256 when it can be computed safely.

Never include credentials, tokens, PHI, or protected values in logs.

## 2. Initialize before any runtime command

```bash
SKILL_DIR=/absolute/path/to/$name
OUTPUT_ROOT=/absolute/path/to/run-output
REQUEST_FILE=/path/to/request-used-for-this-run.txt

RUN_DIR="$("$PYTHON" "$SKILL_DIR/scripts/record_run.py" init \
  --outputRoot "$OUTPUT_ROOT" \
  --skillDir "$SKILL_DIR" \
  --requestFile "$REQUEST_FILE" \
  --requestCapture verbatim)"
```

For a redacted request, use `--requestCapture sanitized`,
`--originalRequestSha256 <SHA256>`, and one `--redaction <DESCRIPTION>` per
redaction.

## 3. Execute and record commands

Use one unique step ID for every attempt:

```bash
"$PYTHON" "$SKILL_DIR/scripts/record_run.py" exec \
  --runDir "$RUN_DIR" \
  --stepId step-001 \
  --category workflow \
  --description "Describe what this exact command does" \
  --cwd "$WORK_DIR" \
  -- program --flag value
```

For a pipeline, pass Bash explicitly so the recorded argv and pipe-failure
behavior are unambiguous:

```bash
"$PYTHON" "$SKILL_DIR/scripts/record_run.py" exec \
  --runDir "$RUN_DIR" \
  --stepId step-002 \
  --category workflow \
  --description "Map and sort reads" \
  --cwd "$WORK_DIR" \
  -- bash -o pipefail -c 'aligner ... | sorter ...'
```

The recorder stores the exact display command, cwd, timestamps, exit code,
retry link, stdout, and stderr. Use `--retryOf <FAILED_STEP_ID>` for a corrected
retry. Failed attempts remain in the JSONL and plain-text logs.

Classify commands as `setup`, `workflow`, `validation`, `environment`, or
`provenance`. Declare material paths with repeatable `--consumes` and
`--produces` options. A successful command-driven skill run requires at least
one successful `workflow` command.

Attach effective parameters as `--parameter NAME=JSON` on the command that uses
them. Record defaults, branch choices, or non-command parameters explicitly:

```bash
"$PYTHON" "$SKILL_DIR/scripts/record_run.py" record-parameter \
  --runDir "$RUN_DIR" \
  --name genome_build \
  --valueJson '"hg38"' \
  --source user-request \
  --description "Reference build selected for every downstream step"
```

The finalized parameter object must exactly match these recorded values.

If an actual argument contains a protected value, provide a safe replay form:

```bash
... exec --displayCommand 'program --token "${API_TOKEN}" ...' --redacted \
  -- program --token "$ACTUAL_TOKEN" ...
```

The record then stores only the redacted display command and marks the execution
identity as withheld. The raw protected argv and its unsalted hash are not
written.

## 4. Record non-shell actions and composed skills

```bash
"$PYTHON" "$SKILL_DIR/scripts/record_run.py" record-action \
  --runDir "$RUN_DIR" \
  --stepId manual-review \
  --kind manual \
  --description "Review the quality-control report" \
  --details "State the decision and resulting artifact" \
  --status success

"$PYTHON" "$SKILL_DIR/scripts/record_run.py" record-skill \
  --runDir "$RUN_DIR" \
  --name another-skill \
  --version 1.2.3 \
  --path /resolved/path/to/another-skill \
  --role "Generated the reference input"
```

Record resolved runtime versions rather than only requested constraints:

```bash
"$PYTHON" "$SKILL_DIR/scripts/record_run.py" exec \
  --runDir "$RUN_DIR" \
  --stepId samtools-version \
  --category provenance \
  --description "Capture the resolved samtools version" \
  --cwd "$RUN_DIR" \
  --produces "$RUN_DIR/samtools.version.txt" \
  -- bash -o pipefail -c \
  'samtools --version > "$1"' _ "$RUN_DIR/samtools.version.txt"

"$PYTHON" "$SKILL_DIR/scripts/record_run.py" record-version \
  --runDir "$RUN_DIR" \
  --name samtools \
  --version 1.20 \
  --kind cli \
  --path /resolved/env/bin/samtools \
  --versionCommand 'samtools --version' \
  --sourceStep samtools-version \
  --evidenceFile "$RUN_DIR/samtools.version.txt"
```

Repeat this for every required public tool/pipeline, integrated codebase, and
reference-data dependency. Recording only Python does not satisfy other
declared dependencies.

For any runtime manager other than `none`, also create a complete resolved
environment snapshot through the recorder. For Conda/Mamba, for example:

```bash
"$PYTHON" "$SKILL_DIR/scripts/record_run.py" exec \
  --runDir "$RUN_DIR" \
  --stepId environment-snapshot \
  --category environment \
  --description "Capture the resolved Conda environment" \
  --cwd "$RUN_DIR" \
  --produces "$RUN_DIR/environment-resolved.txt" \
  -- bash -o pipefail -c \
  "conda list --explicit > '$RUN_DIR/environment-resolved.txt'"
```

Use `python -m pip freeze --all` for venv, the immutable inspected image digest
for containers, or the resolved codebase spec plus revision for CBD. Name this
file in the summary's `environmentSnapshot` record. Requested constraints alone
are not a resolved environment record.

Add interpretation, branch decisions, and any agent-only reasoning needed to
understand the workflow to `agent_workflow.md`.

## 5. Finalize and validate

Copy `examples/run-summary.template.json` outside the skill package, replace all
example values with facts from this run, and preserve every required field.
Then:

```bash
"$PYTHON" "$SKILL_DIR/scripts/record_run.py" finalize \
  --runDir "$RUN_DIR" \
  --summaryFile "$RUN_DIR/summary-input.json"

"$PYTHON" "$SKILL_DIR/scripts/record_run.py" validate \
  --runDir "$RUN_DIR"
```

Do not report full success if validation fails or a runtime command bypassed
the recorder.

## 6. Interactive handoff

Read `run_summary.md` and display a concise version in chat. Include:

- what was done and the ordered multi-command path;
- findings/statistics;
- effective parameters and important defaults;
- inputs and deliverables;
- skills/tools and resolved versions;
- failures, retries, warnings, assumptions, manual actions, and limitations;
- the run directory, `commands.sh`, `run_manifest.json`, and
  `run_manifest.md`.
