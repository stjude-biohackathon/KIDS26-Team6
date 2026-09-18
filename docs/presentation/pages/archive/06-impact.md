---
layout: section
---

# Impact & versatility

The same machinery, far outside variant calling

---
layout: default
---

<div class="kicker">Versatility</div>

# Nothing in the pipeline knows what a genome is

<div class="mt-4">

<DomainGrid :columns="3" :items="[
  { domain: 'Genomic analysis', workflow: 'Variant QC on a public benchmark — align, call, filter, compare to truth, read the metrics that matter', skill: 'a QC skill that runs the same gate set and produces the same tables' },
  { domain: 'HPC operations', workflow: 'Size a job, sbatch it, watch it fail on memory, resubmit with the flag that worked', skill: 'a submit-and-diagnose skill carrying the real resource shape' },
  { domain: 'Data management', workflow: 'Stage a delivery, verify checksums, validate the manifest, reconcile sample counts', skill: 'a pre-analysis validation skill run before anyone touches the data' },
  { domain: 'Core facility handoff', workflow: 'Demultiplex, run-level QC, decide pass/repeat, package the handoff', skill: 'a handoff skill encoding the accept/repeat criteria explicitly' },
  { domain: 'Reporting & figures', workflow: 'Pull results, regenerate the figure set, assemble the summary document', skill: 'a reproducible reporting skill instead of a folder of one-off scripts' },
  { domain: 'Onboarding & troubleshooting', workflow: 'The environment setup and the twelve errors a new analyst hits in week one', skill: 'a setup skill whose failure modes were observed, not imagined' },
]" />

</div>

<div class="grid grid-cols-2 gap-8 mt-7 text-sm">
<div>

**Why the domain does not matter to the code**

The pipeline's inputs are commands, file changes, transcripts, screen text and
notes. Those are domain-agnostic. Only the **labels** and the
**allowlist** are domain-specific — and they are data, not logic.

</div>
<div>

**What makes it St. Jude-shaped**

`SAMPLE_ID`, `ACCESSION`, `SUBJECT_ID`, `SLURM_JOB_NAME` are first-class
identifier labels, not afterthoughts. HPC is a target platform, not a porting
exercise. And the hard boundary — non-PHI inputs only, with sealing enforced
before anything downstream — was designed in from the brief.

</div>
</div>

<!--
The versatility claim has to survive one obvious question: "so is this just for
bioinformatics?" No. The only bioinformatics-specific content in the whole
system is a label list and an allowlist, both of which are data files.
-->

---
layout: default
---

<div class="kicker">Impact</div>

# Who gets what

<div class="grid grid-cols-3 gap-6 mt-5">
<div>

### The analyst

<div class="text-sm mt-2">

Stops paying a documentation tax to share their work. Recording is passive,
toggleable per source, and stoppable from the terminal they are already in.

The knowledge that used to live in a shell history becomes something a colleague
can actually run.

</div>

</div>
<div>

### The team

<div class="text-sm mt-2">

`wfrec merge` reports which workflow families **more than one analyst
performed** — turning "I think we all do this" into a ranked list of
skill candidates.

A recorded session is also the best onboarding artifact that exists: the real
sequence, including the parts that failed.

</div>

</div>
<div>

### The institution

<div class="text-sm mt-2">

A governed path with a human approval gate, an append-only audit trail, a
scorecard that regenerates itself, and a stated privacy boundary the tooling
enforces rather than documents.

Local by default. Cloud egress requires five independent layers to agree.

</div>

</div>
</div>

<div class="mt-9">
<StatTiles :columns="5" :tiles="[
  { label: 'Capture sources, independently toggleable', value: '5' },
  { label: 'Platforms supported, limits published', value: '4' },
  { label: 'Shells hooked, incl. POSIX sh for HPC', value: '5' },
  { label: 'Identifier labels with measured coverage', value: '20' },
  { label: 'Tests in the suite', value: '438' },
]" />
</div>

<div class="mt-4 text-xs opacity-65">
Roughly 19,600 lines of Python across <code>src/</code>. CI installs
<code>.[dev]</code> and runs a bare <code>pytest</code> — no LLM SDK and no
model weight ever reaches CI.
</div>

<!--
The five-number row is the credibility row. The one to linger on is 438 tests:
most of them cover the failure paths — sealing refusals, egress classification,
platform degradation — because those are the paths that matter and the ones
nobody exercises by hand.
-->

---
layout: default
---

<div class="kicker">Status</div>

# Built, and next

<div class="grid grid-cols-2 gap-8 mt-4">
<div>

### Working today

<div class="text-sm mt-2">

- `wfrec` across macOS, Windows, Linux X11, and HPC login nodes
- Five capture sources with per-source toggles that reach live shells
- Shell hooks for bash, zsh, fish, PowerShell 5.1/7, POSIX `sh`
- SSH + SLURM capture at submit time, with bounded log slices
- One control API behind a CLI, a web/native UI, and the `recorder` agent skill
- Inline masking at capture, plus the mandatory fail-closed `wfrec seal`
- The regex detection tier, the egress classifier, and the five-layer gate
- A self-regenerating de-identification scorecard with published floors
- The AutoCAB pipeline end to end, to a PR-ready skill folder

</div>

</div>
<div>

### Next

<div class="text-sm mt-2">

- **Skill Forge** — replaces `SkillProposalBuilder`; drafts from sealed sessions with replay validation and per-step provenance
- **The GLiNER model tier** — closes the name gap the scorecard already measures a floor for, landing in *base* dependencies rather than an extra
- Screen-capture application denylisting
- Encryption at rest, and a panic-pause
- Embeddings or retrieval in place of keyword catalog matching
- A persisted review queue with `needs-edits` and `rejected` states

</div>

<div class="mt-5 p-3 rounded text-sm" style="border:1px solid rgba(250,178,25,.55)">
<strong style="color:#fab219">▲ Least-proven path:</strong> the remote/HPC
capture could not be tested against a real cluster during the build. It is the
first thing to exercise on site.
</div>

</div>
</div>

<!--
Being specific about what is not built is how the built parts stay believable.
The GLiNER gap is already a number in a committed file with a floor attached, so
progress on it will be visible rather than asserted.
-->

---
layout: default
---

<div class="kicker">Try it</div>

# Ten minutes, start to draft

<div class="grid grid-cols-2 gap-8 mt-4">
<div>

**Install, then check the machine**

```bash
uv venv && source .venv/bin/activate
uv pip install -e '.[linux,dev]'   # macos / gui / linux
wfrec doctor                       # always first
wfrec hooks install                # only if doctor says hook-spool
eval "$(wfrec hooks eval)"
```

**Record real work**

```bash
wfrec start --title "HG008 variant QC" --watch .
wfrec source screen on
wfrec note "reran because the BAM was truncated" --label why
wfrec pause --reason "waiting on bwa" --expect 6h
wfrec resume
wfrec stop
```

</div>
<div>

**De-identify, then draft**

```bash
wfrec seal <id>                    # mandatory
wfrec seal --status <id>
autocab demo --input-mode session \
  --session-dir ~/.wfrec/sessions/<id>
# → skills/generated-drafts/
```

**Or drive it in English**

```text
/recorder start recording my variant calling work
/recorder stop logging my bash history
/recorder pause, I'm waiting on the alignment job
/recorder wrap up and hand this to AutoCAB
```

<div class="text-sm mt-3 opacity-80">

The `recorder` skill shells out to the same CLI — a click, a command and a
sentence all reach one control API.

</div>

</div>
</div>

<div class="mt-6 text-sm">

Regenerate the privacy evidence yourself: `autocab deid eval --write-scorecard`
rewrites `docs/deid-evaluation.md`, leak table and all.

</div>

<!--
The demo to run live is the last block: say "start recording my variant calling
work" to the agent, run three real commands, then stop, seal, and show the draft
appearing. It is under five minutes and it shows every layer at once.
-->

---
layout: center
class: text-center
---

# AutoCAB

<div class="text-lg opacity-80 mt-2">
Record the work. Seal it. Forge the skill. Let a human approve it.
</div>

<div class="mt-10 text-sm opacity-70 leading-relaxed">

`stjude-biohackathon/KIDS26-Team6` · MIT licensed

`src/wfrec/README.md` — recording, platforms, HPC, troubleshooting<br/>
`docs/deid-evaluation.md` — the scorecard, the leak table, and the limits<br/>
`docs/biohackathon-framework.md` — pipeline contracts and extension points<br/>
`docs/mgatta42/plan.md` — design rationale and the per-OS backend matrix

</div>

<div class="mt-10 text-xs opacity-50">
Prototype scope: public and synthetic data, volunteer-consented sessions, non-PHI inputs only.
</div>

<!--
Close on the boundary, not on the capability. The reason a tool like this can
exist inside a children's research hospital is that the limits are enforced in
code and stated on the last slide.
-->
