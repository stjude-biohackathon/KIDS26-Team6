---
layout: section
---

# The gap

Why recurring expertise stays tacit

---
layout: default
---

<div class="kicker">The problem</div>

# A skill exists only if somebody stops to write it

<div class="grid grid-cols-2 gap-8 mt-4">
<div>

**Making an agent skill today is five manual acts:**

<v-clicks>

1. Someone **notices** a workflow has repeated
2. Someone **documents** it from memory
3. Someone **tests** it against real data
4. Someone **scrubs** whatever should not be shared
5. Someone **contributes** it to the shared library

</v-clicks>

<div v-click class="mt-5 claim">
Every step needs the one person who is busiest doing the work. So data prep, QC,
analysis, reporting and troubleshooting stay tacit — or get rebuilt, slightly
differently, by the next person.
</div>

</div>
<div>

**What the work actually leaves behind**

<div class="text-sm opacity-80 mt-2">

- Two hundred shell commands in a history file
- A `sbatch` script and a `slurm-4213.out` nobody reads twice
- An agent transcript where the real reasoning happened
- A git diff that shows which parameter finally worked
- The knowledge that the third rerun was because the BAM was truncated

</div>

<div v-click class="mt-6 p-4 rounded border border-gray-400 border-opacity-40 text-sm">
All of that is <strong>evidence of a workflow</strong>. None of it is a skill.
AutoCAB is the path from the first to the second — with review, and without
carrying PHI along.
</div>

</div>
</div>

<!--
The framing that matters for a hospital: the bottleneck is not "we cannot write
skills", it is that skill-writing competes with the science for the same
person's attention, and that anything derived from real sessions is a privacy
question before it is an engineering question.
-->

---
layout: default
---

<div class="kicker">The thesis</div>

# One governed path, end to end

<div class="mt-8">

<FlowChain :steps="[
  { label: 'Observe', note: 'wfrec records the real session' },
  { label: 'Seal', note: 'de-identify, or nothing proceeds', gate: true },
  { label: 'Normalize', note: 'events become workflow traces' },
  { label: 'Cluster', note: 'repeats become candidates' },
  { label: 'Match', note: 'compare to the existing catalog' },
  { label: 'Draft', note: 'Skill Forge writes SKILL.md' },
  { label: 'Review', note: 'a human approves or edits' },
  { label: 'Ship', note: 'PR-ready skill folder' },
]" />

</div>

<div class="grid grid-cols-3 gap-6 mt-10 text-sm">
<div v-click>

**Nothing is automatic about approval**

People review, edit, and approve every proposal before it becomes a shared
skill. The pipeline produces a pull request, not a deployment.

</div>
<div v-click>

**Nothing proceeds unsealed**

`wfrec export` and the AutoCAB session adapter both refuse an unsealed session.
A failed de-identification pass blocks the leak instead of permitting it.

</div>
<div v-click>

**Nothing is invented**

Every drafted step traces back to an event in an append-only timeline. The
provenance survives into the proposal.

</div>
</div>

<!--
This slide is the whole deck in one line. If they remember one thing, it should
be the red gate in the middle: sealing is not a stage you can skip, it is a
precondition two separate consumers enforce independently.
-->
