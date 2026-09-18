---
layout: section
---

# Skill Forge

The next feature: sessions become skills

---
layout: default
---

<div class="kicker">Skill Forge · what it is</div>

# The half of the loop that is still manual

<div class="grid grid-cols-2 gap-8 mt-4">
<div>

**Today, the pipeline gets to a draft and stops.**

The default `SkillProposalBuilder` classifies coverage against the catalog and
drafts proposal content from clustered traces. It is deliberately simple — a
placeholder with a clean seam, which is exactly what the framework's extension
notes say to replace.

<div class="mt-4 text-sm opacity-85">

> *"Replace `SkillProposalBuilder` with an LLM-backed drafting service."*
> — `docs/biohackathon-framework.md`, extension points

</div>

<div class="mt-5 claim text-sm">
<strong>Skill Forge</strong> is that replacement: a skill that reads a sealed
<code>wfrec</code> session and writes a working agent skill — procedure,
scripts, validation steps and failure modes — with every line traceable to an
event in the timeline.
</div>

</div>
<div>

**What makes it tractable here and not in general**

<div class="text-sm mt-2">

A general "turn this transcript into a skill" prompt has to guess. Skill Forge
does not, because the timeline it reads is structured:

- exit codes say which attempt **worked**
- durations say what needs a job instead of a prompt
- `pause --reason` says **why** there is a six-hour hole
- git-verified diffs say which parameter actually changed
- `--label why` notes say what the analyst was thinking
- multi-analyst clusters say which steps are **essential** versus personal habit

</div>

<div class="mt-4 p-3 rounded text-sm" style="border:1px solid rgba(42,120,214,.5)">
It drafts from evidence with provenance, not from a summary of a screenshot.
</div>

</div>
</div>

<!--
Frame it as replacing a named component rather than bolting on a feature. The
framework was written with this seam in it, so Skill Forge is a ProposalBuilder
implementation — the pipeline around it does not change.
-->

---
layout: default
---

<div class="kicker">Skill Forge · how it works</div>

# Six steps, each one refusable

<div class="mt-5">

<FlowChain :steps="[
  { label: '1 · Read', note: 'sealed timeline, sorted by (ts, seq)' },
  { label: '2 · Segment', note: 'markers, pauses and git checkpoints delimit episodes' },
  { label: '3 · Lift', note: 'separate the invariant procedure from its parameters' },
  { label: '4 · Synthesize', note: 'SKILL.md, scripts, validation, failure modes' },
  { label: '5 · Replay', note: 'dry-run against the session it came from' },
  { label: '6 · Review', note: 'queued with per-step provenance' },
]" />

</div>

<div class="grid grid-cols-3 gap-6 mt-9 text-sm">
<div>

**Lifting is the interesting part**

A surrogate that recurs across a session marks a **slot**, not a literal.
`SUBJECT_ID_4f2a` appearing in eight commands is the signal that those eight
commands take one shared parameter — which is a structural fact the seal
*created*, not one it destroyed.

</div>
<div>

**Replay is the precision control**

A drafted step that cannot be reproduced from the timeline is dropped or flagged
— never guessed. The session is both the training evidence and the test fixture,
so a hallucinated flag has somewhere to fail before a human sees it.

</div>
<div>

**Review is not a rubber stamp**

The proposal arrives with per-step event provenance, a diff against the matched
catalog skill, and its redaction report attached. A reviewer can **check**
rather than trust, then edit, approve, or reject.

</div>
</div>

<div class="mt-7 text-sm opacity-80">

The output is the existing artifact: `SKILL.md` + `metadata.json` in a PR-ready
folder under `skills/generated-drafts/`. Skill Forge changes what gets written
there, not who decides to merge it.

</div>

<!--
If asked "why not just prompt a model over the transcript" — step 5. Replay
against the originating session is what turns a plausible draft into a checked
one, and it is only possible because the timeline is append-only and complete.
-->

---
layout: default
---

<div class="kicker">Skill Forge · safely</div>

# Safety is inherited, not re-implemented

<div class="grid grid-cols-2 gap-8 mt-4">
<div>

**Four properties Skill Forge gets for free**

<div class="text-sm mt-2">

1. **It cannot read an unsealed session.** It consumes the session adapter,
   which already refuses one. The PHI gate is upstream of the drafting, so
   there is no path where a draft is built from raw text.
2. **It cannot emit a raw identifier.** Every byte it reads has already been
   rewritten to a surrogate. The worst case is a draft containing
   `MRN_a7f3c14b9e02`, which is unrecoverable outside that session.
3. **It cannot ship anything by itself.** The `Exporter` writes a folder. A
   human opens the pull request.
4. **It cannot quietly duplicate the library.** Catalog matching classifies a
   proposal as extending an existing skill or introducing a new one, before a
   reviewer reads a word of it.

</div>

</div>
<div>

**And one property redaction has to protect**

<div class="text-sm mt-2">

A skill whose reference-genome path got redacted is a skill nobody can run. So
the utility metrics are not cosmetic — they are what keeps the generated skill
**executable**:

</div>

<div class="mt-3">
<StatTiles :columns="2" :tiles="[
  { label: 'Must-survive violations — science terms the redactor may never touch', value: '0 / 380', status: 'good', statusLabel: 'held' },
  { label: 'False-positive spans per negative record', value: '0.0', status: 'good', statusLabel: 'ceiling 0.25' },
]" />
</div>

<div class="mt-4 text-sm">

That is why precision carries a **ceiling** rather than a floor, and why
`over_redaction_rate` sits at 0.43% against a 5% ceiling. Over-redaction is not
a safe failure here — it is the failure that produces a useless skill, and it is
measured on every run.

</div>

</div>
</div>

<!--
This is the slide that connects the two halves of the project for a
non-engineering audience: the de-identification work is not a tax on the skill
generation, it is the precondition that makes generated skills shareable at all
— and its utility metrics are what keep them runnable.
-->
