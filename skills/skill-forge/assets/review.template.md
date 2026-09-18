# Skill Forge Review: $name

## Decision

- Operation: `$operation`
- Requested packaging: `$requested_packaging`
- Analyzed packaging: `$packaging`
- Proposed skill version: `$skill_version`
- Coverage decision: `$decision`
- Installable proposal rendered: `$proposal_rendered`

## Inferred purpose

$purpose

## Evidence summary

$evidence_summary

## Coverage

$coverage_summary

## Proposed missing commands requiring approval

$proposed_steps

## Manual and blocked steps

$manual_blocked_steps

## Dependencies and code closure

$dependency_summary

## Runtime environment and reproducibility

$runtime_environment_review

## Unresolved questions

$unresolved_questions

## Update and migration assessment

$update_summary

## Review checklist

- [ ] The inferred goal and workflow boundary are correct.
- [ ] Reconstructed commands preserve the source meaning.
- [ ] Proposed commands were approved or removed.
- [ ] Missing custom scripts/configs/assets were supplied or remain blocking.
- [ ] CBD/STD packaging and any migration were approved.
- [ ] Custom-code redistribution was approved before STD copying.
- [ ] Manual checkpoints are explicit.
- [ ] Machine-readable environment setup succeeds from a clean runtime.
- [ ] Runtime recorder smoke test captures the request, commands, parameters,
      skills/versions, outputs, failures/retries, and JSON/Markdown summaries.
- [ ] The generated skill requires a final findings summary in interactive chat.
- [ ] Package tests and smallest safe smoke case passed.
- [ ] Installation destination was approved.
