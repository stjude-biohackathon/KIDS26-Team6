# Integrated AutoCAB workflow

AutoCAB uses one durable workflow across the command line and dashboard. Its
recorder captures evidence. Skill Forge turns a sealed session into a
conservative draft. A person reviews and approves that draft before AutoCAB can
package it.

```text
recording -> sealed session -> blocked draft -> review-ready -> approved -> packaged
 AutoCAB       redaction        Skill Forge        human         human      validator
```

There is no automatic approval. A successful draft is not proof that the
recorded procedure is correct or reproducible.

## First setup

Run the guided setup in a terminal:

```bash
autocab init
```

The setup can save the analyst name, select a redaction engine, download and
verify explicitly selected model weights, install shell hooks, copy legacy
sessions, and run readiness checks. Model downloads, profile edits, and legacy
migration each require a direct flag or an interactive confirmation.

For scripts and managed workstations, use explicit non-interactive options:

```bash
autocab init --no-interactive \
  --analyst "Analyst name" \
  --redaction gliner2-pii \
  --fetch-model \
  --install-hooks \
  --migrate-legacy \
  --check
```

GLiNER uses the base installation. GLiNER2 PII also needs the optional runtime:

```bash
uv pip install -e '.[deid-gliner2]'
```

Both model choices run locally during sealing. Pattern matching always runs
first. The model weights are pinned and verified before installation.

## Record and seal

```bash
autocab record start --title "Workflow title" --watch .
autocab record note "Why this step changed" --label decision
autocab record pause --reason "Waiting for a job" --expect 2h
autocab record resume
autocab record finish --seal
```

`record finish --seal` uses the engine selected by `autocab init`. Use
`--engine` to override it for one session. Model setup failure stops before the
saved engine is changed.

For a session that is already archived, apply the configured engine separately:

```bash
autocab record redact <session-id>
```

Advanced capture controls remain available through `wfrec`, including source
toggles, remote SSH capture, Slurm collection, transcript attachment, and
multi-session merging.

## Forge, review, approve, and package

Create a draft from a sealed session:

```bash
autocab forge --session <session-id>
```

The command writes a run under `~/.autocab/runs/<run-id>/`. The initial draft
is blocked. It records observed commands, evidence links, unverified
dependencies, and questions about inputs and outputs. AutoCAB does not invent
missing procedure details.

Edit a copy of `skill-spec.json`, then submit it for review:

```bash
autocab review <run-id> \
  --reviewer "Analyst name" \
  --notes "Verified inputs, outputs, and dependency versions" \
  --spec reviewed-skill-spec.json
```

The run stays blocked while any blocking question remains. Dependency-bearing
skills also require a human-observed clean-environment smoke test. AutoCAB
records that result but never executes commands reconstructed from the session:

```bash
autocab verify-runtime <run-id> \
  --reviewer "Analyst name" \
  --result passed \
  --environment "fresh conda prefix" \
  --platform linux-64 \
  --smoke-test "command used for the smoke test" \
  --notes "Expected output reproduced" \
  --evidence-ref verification/run.log
```

A valid `compose` or `novel` decision with no blocking questions and, for an
executable skill, successful runtime verification moves the run to
`needs_review`.

Approval and packaging are separate commands:

```bash
autocab approve <run-id> --reviewer "Maintainer name"
autocab package <run-id>
```

Packaging renders the skill into the run directory and applies strict package
validation. It does not publish, push, or open a pull request.

If an approved run needs correction before packaging, invalidate its current
approval and return it to blocked review:

```bash
autocab reopen <run-id> \
  --reviewer "Maintainer name" \
  --notes "Correct the runtime verification evidence"
```

The earlier approval remains in the append-only review log. A reopened run must
be reviewed and approved again.

## Stored data

| Path | Contents |
| --- | --- |
| `~/.autocab/config.toml` | User setup and redaction defaults |
| `~/.autocab/state.json` | Current recorder state and analyst preference |
| `~/.autocab/sessions/<session-id>/` | Raw session evidence and seal records |
| `~/.autocab/runs/<run-id>/` | Forge state, evidence, reviews, and package |
| `~/.autocab/models/` | Explicitly downloaded local model weights |
| `~/.autocab/hooks/` | Materialized shell capture hooks |

Existing `~/.wfrec/sessions/` remain visible for compatibility. Run
`autocab migrate --legacy` to copy them into AutoCAB storage. Migration
verifies copied files and does not delete the original sessions.

Each forge run contains:

```text
run.json
evidence.json
skill-spec.json
reviews.jsonl
validation.json        # after packaging
package/               # after packaging
```

`evidence.json` contains the sealed event snapshot used to build the draft.
`reviews.jsonl` is an append-only record of review and approval decisions.

## Dashboard and CLI state

The dashboard reads the same session and run files as the CLI. Its Skill
workflow section shows the selected session's latest forge run and the same
blocked, review, approval, and package states. The dashboard cannot bypass a
CLI validation or approval rule.

Use `autocab status` to see the active recording and recent forge runs. Use
`wfrec doctor` or `autocab init --check` to inspect capture readiness.

## Failure boundaries

- An unsealed session cannot be forged.
- Missing model weights prevent model-based sealing instead of falling back
  silently to a weaker engine.
- A malformed or credential-bearing `config.toml` is rejected.
- Blocking questions prevent approval.
- Approval does not package automatically.
- Executable drafts cannot be approved until runtime verification succeeds.
- Package validation failure leaves the approved run available to reopen for
  review without erasing its earlier approval record.
- Setup never deletes legacy recordings or stores provider credentials.
