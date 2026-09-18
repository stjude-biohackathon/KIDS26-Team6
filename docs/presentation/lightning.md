---
# The lightning cut: the first three slides of slides.md, timed for 2-3 minutes.
# Shares this folder's style.css, components/ and global-bottom.vue, so the two
# decks cannot drift apart visually — only the words differ.
theme: default
title: AutoCAB — lightning
titleTemplate: '%s — BioHackathon 2026, Team 6'
info: |
  ## AutoCAB — lightning talk
  Repeated bioinformatics work becomes a governed agent skill, de-identified on
  the way.

  Three slides, two to three minutes. The full deck is slides.md.
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
  <span class="chip on">record</span>
  <span class="chip on">de-identify</span>
  <span class="chip on">draft</span>
  <span class="chip on">a human approves</span>
</div>

<div class="mt-5 text-xs opacity-55" style="font-family: var(--mono)">
macOS · Windows · Linux · HPC
</div>

<!--
~25 seconds. One sentence and move on: a bioinformatician does their job, and at
the end of it there is a de-identified, reviewable agent skill anyone on the
team can run. Do not list the tooling here — slide 3 shows it.
-->

---
layout: default
---

<div class="kicker">The problem</div>

# A skill only exists if somebody stops to write it

<div class="claim mt-7">
Writing one needs the person who is busiest doing the science. So workflows and their documentation 
are re-interpreted and performed again each time the work is done. 
</div>

<div class="mt-14">

<FlowChain numbered highlight="De-identify" :steps="[
  { label: 'Record', note: 'the real session' },
  { label: 'De-identify', note: 'identifiers become surrogates' },
  { label: 'Compare to Skill Index', note: 'ensure skills stay atomic' },
  { label: 'Draft', note: 'a skill, with its failure modes' },
  { label: 'Review', note: 'you approve or edit' },
]" />

</div>

<div class="mt-14 text-lg text-center" style="color: var(--text-primary)">
The work already leaves the evidence behind. AutoCAB turns it into the skill.
</div>

<div class="mt-4 text-sm text-center opacity-65">
Runs on your machine. Ends in a proposed skill that automates your work entirely.
</div>

<!--
~50 seconds. The line to land: the bottleneck is not that people cannot write
skills, it is that skill-writing competes with the science for the same
person's attention. Then walk the chain in one breath and stop.
-->

---
layout: default
---

<div class="kicker">How it works</div>

# Three moves, one path

<div class="mt-5">
<SystemMap />
</div>

<div class="grid grid-cols-3 gap-6 mt-7 text-sm">
<div class="panel">

**Record.** Shell, files, transcripts, screen and notes — on macOS, Windows,
Linux or HPC. Switch any source off mid-session.

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
~60 seconds, then stop talking. Left to right: the work, the de-identification
pass, the draft. Two things to say out loud: it runs on your machine by default,
and it opens a pull request rather than deploying anything.

If there is time for one more sentence: the same path works for HPC operations,
data delivery QC and onboarding — nothing in it is specific to genomics.
Everything else lives in slides.md.
-->
