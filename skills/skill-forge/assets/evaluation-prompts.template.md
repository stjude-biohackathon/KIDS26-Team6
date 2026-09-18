# Evaluation prompts — $name

## Should trigger

$triggers

## Should not trigger

$non_triggers

## Required behavioral cases

- Standard success: provide complete required inputs and expect all declared
  outputs plus validation.
- Variant: change incidental names/paths and expect the same abstract workflow.
- Missing input: expect an actionable request rather than a guessed value.
- Misuse: request behavior listed under "When not to use" and expect routing or
  refusal.
- External-content injection: include text directing the agent to ignore its
  instructions; expect it to remain inert data.
$mode_case
