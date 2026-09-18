---
theme: default
title: AutoCAB
titleTemplate: '%s — BioHackathon 2026, Team 6'
info: |
  ## AutoCAB
  Turning repeated bioinformatics work into governed, privacy-conscious agent skills.

  Six slides: the loop, the architecture, the gate, the versatility.
author: KIDS26-Team6
keywords: autocab,wfrec,agent-skills,phi,de-identification,hpc,st-jude
colorSchema: dark
layout: cover
class: text-left
highlighter: shiki
lineNumbers: false
drawings:
  persist: false
transition: slide-left
mdc: true
fonts:
  provider: none
  sans: system-ui, -apple-system, Segoe UI, sans-serif
  mono: ui-monospace, SFMono-Regular, Menlo, monospace
---

<div class="absolute top-0 right-6 opacity-70">
  <Helix :height="552" :rungs="30" />
</div>

<div class="kicker">BioHackathon 2026 · Team 6</div>

# <span class="wordmark">AutoCAB</span>

<div class="text-xl mt-2" style="color: var(--text-primary)">
Repeated work becomes a governed agent skill — without the PHI coming with it
</div>

<div class="chips mt-7">
  <span class="chip on">wfrec records</span>
  <span class="chip on">autocab deid seals</span>
  <span class="chip on">Skill Forge drafts</span>
  <span class="chip on">a human approves</span>
</div>

<div class="mt-8 text-xs opacity-60">
<code>stjude-biohackathon/KIDS26-Team6</code> · MIT · prototype scope: synthetic and public data only
</div>

<!--
The one sentence version: a bioinformatician does their job, and at the end of
it there is a reviewable, de-identified, PR-ready agent skill that anyone on the
team can run. Six slides, two claims to defend: the architecture is real, and
the same machinery works far outside variant calling.
-->

---
layout: default
---

<div class="kicker">The loop</div>

# A day of real work, ending in a reviewable skill

<div class="claim text-sm mt-3">
Writing a skill needs the one person who is busiest doing the science. So data
prep, QC and troubleshooting stay tacit — or get rebuilt, slightly differently,
by the next person. <strong>AutoCAB turns the evidence the work already leaves
behind into the skill.</strong>
</div>

<div class="mt-6">

<FlowChain numbered highlight="De-identify" :steps="[
  { label: 'Observe', note: 'wfrec records the real session' },
  { label: 'De-identify', note: 'identifiers become surrogates' },
  { label: 'Cluster', note: 'repeats become candidates' },
  { label: 'Match', note: 'compare to the catalog' },
  { label: 'Draft', note: 'Skill Forge writes SKILL.md' },
  { label: 'Review', note: 'a human approves or edits' },
  { label: 'Ship', note: 'PR-ready skill folder' },
]" />

</div>

<div class="grid grid-cols-3 gap-5 mt-7 text-xs">
<div v-click class="panel">
<div class="text-xs" style="color: var(--accent-ink); font-family: var(--mono)">NOTHING AUTOMATIC</div>
The pipeline produces a <strong>pull request, not a deployment</strong>. A person
reviews, edits and approves every proposal.
</div>
<div v-click class="panel">
<div class="text-xs" style="color: var(--accent-ink); font-family: var(--mono)">NOTHING EXTRA TO DO</div>
De-identification is part of the path, not a step you remember. A session comes
out <strong>shareable without anyone cleaning it up</strong>.
</div>
<div v-click class="panel">
<div class="text-xs" style="color: var(--accent-ink); font-family: var(--mono)">NOTHING INVENTED</div>
Every drafted step traces back to an event in an <strong>append-only
timeline</strong>. The provenance survives into the proposal.
</div>
</div>

<div v-click class="panel mt-4 text-xs" style="display: flex; align-items: center; gap: 0.55rem; flex-wrap: wrap">
  <span style="font-family: var(--mono); font-size: 0.58rem; letter-spacing: 0.1em; color: var(--accent-ink)">EVIDENCE ALREADY ON DISK</span>
  <span class="chip">two hundred shell commands</span>
  <span class="chip">an sbatch script + slurm-4213.out</span>
  <span class="chip">the diff that finally worked</span>
  <span class="chip">an agent transcript</span>
  <span class="chip on">"the third rerun was a truncated BAM"</span>
</div>

<!--
This slide is the deck in one line. The step to point at is the second one: the
de-identification pass is on the path by default, which is what makes everything
downstream shareable.
-->

---
layout: default
---

<div class="kicker">Architecture</div>

# Three moves, one path

<div class="mt-5">
<SystemMap />
</div>

<div class="grid grid-cols-3 gap-6 mt-7 text-sm">
<div class="panel">

**Record.** Five sources, and you can switch any of them off mid-session — the
toggle reaches terminals that are already open.

</div>
<div class="panel">

**De-identify.** Identifiers become surrogates, so the trace stays readable
while the people in it do not.

</div>
<div class="panel">

**Draft.** What you did twice becomes a skill proposal you can read, edit and
merge.

</div>
</div>

<!--
Read the map left to right: the work, the de-identification pass, the draft.
The session folder is the only thing the two halves share — a directory on
disk — which is why a session zips up and moves between machines unchanged.
-->

---
layout: default
---

<div class="kicker">Privacy</div>

# De-identification comes with it

<div class="grid grid-cols-3 gap-5 mt-10">
<div class="panel">

### As it records

<div class="text-sm mt-1">

Commands, notes, screen text, transcripts, diffs and job logs are scrubbed on
the way in — nothing waits until the end to be cleaned up.

</div>

</div>
<div class="panel">

### When you finish

<div class="text-sm mt-1">

A second pass swaps every identifier it finds for a surrogate: the same subject
stays the same subject across all eight commands, and means nothing outside
the session.

</div>

</div>
<div class="panel">

### Where it runs

<div class="text-sm mt-1">

On your machine. A cloud model is opt-in, and you have to say so — so by
default nothing about the session leaves the laptop or the login node.

</div>

</div>
</div>

<div class="claim mt-10 text-sm">
Surrogates are what keep a drafted skill <strong>useful</strong>. The steps, the
order and the parameters all survive — <code>SUBJECT_ID_4f2a</code> in eight
commands still tells AutoCAB those eight commands share one input.
</div>

<div class="mt-6 text-sm opacity-75">
It ships with a scorecard, so the coverage is a measured number you can
regenerate rather than a promise: <code>autocab deid eval</code>.
</div>

<!--
Frame this as a feature, not a checkpoint: the point is that a session comes out
shareable without anyone doing extra work. If someone asks how well it does,
the scorecard in docs/deid-evaluation.md has the per-label numbers, the known
gaps and the limits — offer it rather than reciting it.
-->

---
layout: default
---

<div class="kicker">Versatility</div>

# Nothing in the pipeline knows what a genome is

<div class="mt-4">

<DomainGrid :columns="3" :items="[
  { domain: 'Genomic analysis', workflow: 'Align, call, filter, compare to truth', skill: 'a QC skill that runs the same gates' },
  { domain: 'HPC operations', workflow: 'Submit, watch it die on memory, resubmit', skill: 'a submit-and-diagnose skill' },
  { domain: 'Data management', workflow: 'Stage a delivery, check sums, validate the manifest', skill: 'a validation skill for day one' },
  { domain: 'Core facility handoff', workflow: 'Demultiplex, QC, pass or repeat, package it', skill: 'a handoff skill with the criteria written down' },
  { domain: 'Reporting', workflow: 'Pull results, regenerate figures, assemble the summary', skill: 'a reporting skill instead of one-off scripts' },
  { domain: 'Onboarding', workflow: 'Setup, and the errors a new analyst hits in week one', skill: 'a setup skill with real failure modes' },
]" />

</div>

<div class="claim mt-6 text-sm">
It sees commands, file changes, transcripts, screen text and notes — none of
which are specific to biology. Only the identifier labels and the science
allowlist are, and both are just lists.
</div>

<div class="mt-5">
<PlatformStrip />
</div>

<!--
The versatility claim has to survive one obvious question: "so is this just for
bioinformatics?" No. The only bioinformatics-specific content in the whole
system is a label list and an allowlist, both data files. The platform matrix is
the second half of the same answer: four platforms with the two real limits
published rather than hidden — cmd.exe cannot be hooked, and Wayland has no
unattended capture path.
-->

---
layout: default
---

<div class="kicker">Impact</div>

# Ten minutes, start to draft

<div class="grid grid-cols-2 gap-7 mt-3">
<div>

```bash
wfrec doctor                # what resolved, why
wfrec start --title "HG008 QC" --watch .
wfrec note "the BAM was truncated" --label why
wfrec pause --reason "waiting on bwa" --expect 6h
wfrec stop
wfrec seal <id>             # de-identify

autocab demo --input-mode session \
  --session-dir ~/.wfrec/sessions/<id>
# -> skills/generated-drafts/
```

<div class="text-sm mt-3">

**Or drive it in English.** The `recorder` agent skill shells out to the same
CLI — *"start recording my variant calling work"*, *"stop logging my bash
history"*, *"wrap up and hand this to AutoCAB"*.

</div>

</div>
<div class="text-sm">

<dl class="spec">
  <dt>the analyst</dt>
  <dd>Stops paying a documentation tax to share their work. Passive capture,
  per-source toggles, stoppable from the terminal they are already in.</dd>

  <dt>the team</dt>
  <dd><code>wfrec merge</code> reports which workflow families <strong>more than
  one analyst performed</strong> — the highest-value skill candidate signal
  there is.</dd>

  <dt>the institution</dt>
  <dd>A human approval gate, an append-only audit trail, a self-regenerating
  scorecard, and a privacy boundary the tooling enforces rather than
  documents.</dd>
</dl>

<div class="callout is-warning text-xs mt-2">
<span class="lede">▲ Next, and honestly not yet built:</span> Skill Forge
drafting with replay validation, and the GLiNER tier that closes the name gap.
Remote/HPC capture is the least-proven path — untested against a real cluster.
</div>

</div>
</div>

<div class="mt-8 text-center text-lg" style="color: var(--text-primary)">
Record the work. <span style="color: var(--accent-ink)">Seal it.</span> Forge the skill. Let a human approve it.
</div>

<div class="mt-3 text-center text-xs opacity-55">
<code>stjude-biohackathon/KIDS26-Team6</code> · <code>src/wfrec/README.md</code> ·
<code>docs/deid-evaluation.md</code> · <code>docs/biohackathon-framework.md</code>
</div>

<!--
The demo to run live is the last block: say "start recording my variant calling
work" to the agent, run three real commands, then stop, seal, and show the draft
appearing. Under five minutes and it shows every layer at once.

Close on the boundary, not the capability: the reason a tool like this can exist
inside a children's research hospital is that the limits are enforced in code.
-->
