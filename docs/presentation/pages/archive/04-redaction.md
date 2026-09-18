---
layout: section
---

# PHI redaction

Local by default · measured, not asserted

---
layout: default
---

<div class="kicker">Redaction</div>

# Two layers, doing two different jobs

<div class="grid grid-cols-2 gap-8 mt-4">
<div>

### 1 · Inline, at capture

<div class="text-sm opacity-85 mt-2">

Shell commands, notes, OCR text, agent transcripts, file paths, git diffs,
remote command output and SLURM accounting fields **all pass through
`autocab.deid` on the way in**. Each event records which rules fired in its
`redactions` array.

</div>

```text
sbatch --job-name=[REDACTED_SLURM_JOB_NAME] …
# patient [REDACTED_MRN] rerun
```

<div class="text-sm mt-3">

This is **masking**: not reversible, not linkable. Byte-compatible with what
AutoCAB's redactor already consumed, so nothing downstream had to change.

</div>

</div>
<div>

### 2 · The seal, after the fact

<div class="text-sm opacity-85 mt-2">

`wfrec seal <id>` walks **every payload of every event**, plus `context/`,
`screen/ocr/`, `files/diffs/`, `shell/remote/` and `jobs/`, and rewrites what it
finds to per-run surrogates.

</div>

```text
MRN_a7f3c14b9e02 → same value, same surrogate,
                   within this session only
```

<div class="text-sm mt-3">

This is **pseudonymization**: a value stays linkable *within* the session — so
the trace is still analyzable — and unrecoverable outside it.

</div>

</div>
</div>

<div class="mt-7 claim text-sm">
Capture masks and only the seal pseudonymizes, because pseudonyms need a key
stable across processes. An in-memory key cannot span the daemon and
<code>--no-daemon</code>; a key file beside the data would recreate the exact
re-identification key that discarding the key exists to prevent. So: one
process, one ephemeral key, start to finish.
</div>

<!--
The two-layer split is the design decision I would defend hardest. Capture-time
masking has to be cheap and byte-stable; the seal can be expensive and
destructive because it runs once, in one process, after the session is idle.
Collapsing them into one pass would force the worse tradeoff on both.
-->

---
layout: default
---

<div class="kicker">Redaction · the gate that matters</div>

# Sealing is mandatory, and it fails closed

<div class="mt-6">

<FlowChain :steps="[
  { label: 'session stopped', note: 'collectors idle' },
  { label: 'wfrec seal <id>', note: 'every payload, every sidecar', gate: true },
  { label: 'sealed', note: 'refuses further appends' },
  { label: 'export / ingest', note: 'permitted' },
]" />

</div>

<div class="grid grid-cols-3 gap-6 mt-9 text-sm">
<div>

**Two independent consumers enforce it**

`wfrec export` refuses an unsealed session. The AutoCAB session adapter refuses
it too. `NotSealed` is an error you hit, not a warning you scroll past.

</div>
<div>

**No reverse map is produced, ever**

The key is `secrets.token_bytes(32)`, never written to disk, and zeroed when the
process ends. Re-identification is not gated — it is absent.

</div>
<div>

**The audit log cannot leak either**

`deid/audit.jsonl` records labels, offsets and a `value_id` integer. Never the
matched text, and **never a digest of it** — a six-digit MRN brute-forces
against a bare SHA-256 in milliseconds.

</div>
</div>

<div class="mt-8 text-sm opacity-80">

It refuses rather than racing: `wfrec seal` on a live session says *"not idle"*
instead of sealing underneath a running collector. Pass `--force` to stop and
then seal. A sealed session that genuinely has to change needs `--reseal`.

</div>

<!--
"Fail closed" is easy to claim and easy to get wrong. The check to point at is
that the refusal lives in the two consumers rather than in the seal itself — so
forgetting to seal cannot produce a quiet partial success.
-->

---
layout: two-cols
---

<div class="kicker">Redaction · detection</div>

# Tiers, and which ones you cannot turn off

<div class="text-sm mt-2">

**Always on, in this order, and not selectable:**

</div>

<div class="mt-2 text-sm">

1. `surrogate_guard` — never re-redact an existing surrogate
2. `regex_rules` — stdlib only, zero dependencies, the only tier that runs inline at capture

</div>

<div class="text-sm mt-4">

**`--engine` swaps the model tier only:**

</div>

| engine | what it is |
|---|---|
| `gliner` | zero-shot ONNX NER over `labels.py` descriptions — the intended default model tier |
| `gliner+llm` | composition, resolved by the seal |
| `llm` | a model tier, subject to the five-layer gate |
| `torch` | transformers-based |
| `presidio` | opt-in framework (its *validators* are already adopted) |

::right::

<div class="mt-14 text-sm">

**An absent engine reports `unavailable: <reason>`** instead of raising at
import — because `wfrec doctor` has to be able to describe a machine where the
optional tiers are not installed.

<div class="mt-5">

**The regex tier needs nothing installed.** That is what lets the default
redactor delegate to `find_spans()` without making any optional dependency a
hard requirement — and it is the reason inline masking is on for everybody, on
every platform, with no flag.

</div>

<div class="mt-5 p-3 rounded" style="border:1px solid rgba(250,178,25,.55)">
<strong style="color:#fab219">▲ The principle, stated in the code</strong><br/>
<span class="opacity-85">"A PHI control that requires a second install flag is
a PHI control that is off on most machines." The GLiNER tier is specified to
land in <em>base</em> dependencies, not an extra.</span>
</div>

</div>

<!--
Worth saying out loud: the model tier that closes the name gap is specified and
not yet built. The honest status is that regex ships, GLiNER is next, and the
scorecard already contains the floor it has to beat.
-->

---
layout: default
---

<div class="kicker">Redaction · policy</div>

# Detection asks "is this an identifier". Policy asks "and therefore what"

<div class="grid grid-cols-4 gap-4 mt-5 text-sm">
<div class="p-3 rounded" style="border:1px solid rgba(128,128,128,.35)">

**`KEEP`**

Left verbatim. Workforce identity and allowlisted science.

</div>
<div class="p-3 rounded" style="border:1px solid rgba(128,128,128,.35)">

**`MASK`**

`[REDACTED_<LABEL>]`. Not reversible, not linkable.

</div>
<div class="p-3 rounded" style="border:1px solid rgba(128,128,128,.35)">

**`PSEUDONYMIZE`**

`NAME_a1b2c3`. Not reversible, linkable within the run.

</div>
<div class="p-3 rounded" style="border:1px solid rgba(128,128,128,.35)">

**`GENERALIZE`**

`[DATE:2012]`, `AGE_90_PLUS`. Coarser, and still true.

</div>
</div>

<div class="grid grid-cols-2 gap-8 mt-8">
<div>

**Three profiles, increasing strictness**

`regex-only` · **`balanced`** (default) · `strict`

<div class="text-sm mt-3 opacity-85">

OCR box precision is a policy too: `line` blacks out the whole OCR line box,
`proportional` narrows it — a utility/safety dial, set in policy rather than
hardcoded in the collector.

</div>

</div>
<div>

**Why `analyst` and `host` are `KEEP` by default**

<div class="text-sm mt-2 opacity-85">

They identify the **workforce**, not the PHI subject — and
`WorkflowClusterer` groups on `trace.analyst`. Pseudonymizing it breaks the
clustering the recorder exists to feed.

Some sites *do* treat staff identity as in scope, so
`pseudonymize_analyst` exists as a flag — with the reasoning written where the
flag lives, because this is the decision that gets second-guessed in review.

</div>

</div>
</div>

<!--
The KEEP-the-analyst decision is the one a privacy officer will challenge first,
and the answer is a flag plus a documented tradeoff rather than a default anyone
has to argue about in a meeting.
-->

---
layout: default
---

<div class="kicker">Redaction · egress</div>

# Local by default — and the address decides, not the label

<div class="grid grid-cols-3 gap-5 mt-5 text-sm">
<div class="p-4 rounded" style="border:1px solid rgba(12,163,12,.5)">

<strong style="color:#0ca30c">● `none`</strong>

Unix socket, or **every** resolved address is loopback. No egress gate. Regex
and GLiNER are local by construction; Ollama or any OpenAI-compatible server on
`127.0.0.1` classifies here too.

</div>
<div class="p-4 rounded" style="border:1px solid rgba(250,178,25,.55)">

<strong style="color:#fab219">▲ `internal`</strong>

**Every** resolved address is private, site-local, link-local or CGNAT. Needs an
acknowledgement carrying `internal_endpoints_approved`. No BAA clause required —
nothing reached a third party.

</div>
<div class="p-4 rounded" style="border:1px solid rgba(208,59,59,.5)">

<strong style="color:#d03b3b">■ `external`</strong>

A cloud provider. This is egress: it **deletes the local-only claim for that
session** and requires all five gate layers, including a named-approver
acknowledgement with a BAA and zero-data-retention confirmation.

</div>
</div>

<div class="mt-8 grid grid-cols-2 gap-8">
<div>

**Classify the proxy, not the endpoint**

<div class="text-sm opacity-85 mt-1">

If `HTTP_PROXY` / `HTTPS_PROXY` / `ALL_PROXY` is set and `NO_PROXY` does not
exempt the endpoint, the text goes to the **proxy**.

</div>

```bash
# looks local. is not. fully gated.
ALL_PROXY=http://corp:3128 \
  autocab deid --llm-base-url http://localhost:11434
```

</div>
<div class="text-sm">

**Four more rules, each one a bug somebody would have shipped:**

- **Escalate on ambiguity** — mixed A records, DNS failure, or any non-loopback in the set yields the most restrictive class present
- **Pin the resolution** — `reverify()` re-resolves immediately before the request and aborts on change
- `::ffff:127.0.0.1` **is** loopback; `0.0.0.0` and `[::]` are **not**
- Plaintext to non-loopback is refused unless explicitly allowed; loopback over `http` is fine and is never nagged about

</div>
</div>

<!--
The resolver is injected — there is no network anywhere in egress.py — which is
why the whole matrix is testable offline, and why there IS a whole matrix.

Rule one is the single most important function in the module. Without it,
`--llm-base-url http://localhost:11434` under a corporate proxy classifies as
`none` while egressing every byte. That is the easiest bypass in the design to
miss.
-->

---
layout: default
---

<div class="kicker">Redaction · the gate</div>

# Five layers, and they are independent on purpose

<div class="mt-4">
<GateLadder />
</div>

<div class="grid grid-cols-2 gap-8 mt-5 text-sm">
<div>

**One ack per provider.** A BAA is a contract with a named counterparty. An
attestation naming Anthropic must not authorize egress to OpenAI or Google, so
the ack file is per-provider and a mismatch is a hard refusal.

</div>
<div>

**Credential presence is deliberately not a layer.** An unset `ANTHROPIC_API_KEY`
does not mean there are no credentials — SDKs also resolve auth tokens, login
profiles and workload identity. Gating on key availability would let ambient
credentials silently arm egress.

</div>
</div>

<!--
Each layer is owned by a different person, lives in a different file, or is
satisfied at a different time. That separation is the control; five checkboxes
in one config file would not be.
-->

---
layout: default
---

<div class="kicker">Redaction · evidence</div>

# Measured, not asserted

<div class="mt-6">
<StatTiles :columns="4" :tiles="[
  { label: 'Records with zero strict misses', value: '74.7%', note: 'safe_record_rate — the number a reviewer should read first' },
  { label: 'Strict recall, gated tiers (easy + medium)', value: '0.880', note: 'hard tier reported separately, gated far lower' },
  { label: 'Labels at 1.000 strict recall', value: '18 / 20', note: 'only NAME and SLURM_JOB_NAME fall short' },
  { label: 'Strict recall on names', value: '0.346', status: 'critical', statusLabel: 'known gap · tested floor 0.10' },
]" />
</div>

<div class="mt-5">
<StatTiles :columns="4" :tiles="[
  { label: 'Over-redaction, share of all corpus characters', value: '0.43%', status: 'good', statusLabel: 'ceiling 5%' },
  { label: 'Must-survive violations', value: '0 / 380', status: 'good', statusLabel: 'zero' },
  { label: 'Regex tier throughput, CPU, stdlib only', value: '1.67', note: 'ms per KB' },
  { label: 'Corpus size', value: '316', note: 'synthetic records · 572 gold spans' },
]" />
</div>

<div class="mt-6 text-xs opacity-65">
Regenerated by <code>autocab deid eval --write-scorecard</code> into
<code>docs/deid-evaluation.md</code> — not hand-edited; the next run overwrites it.
</div>

<!--
The number to lead with is safe_record_rate, not recall. 74.7% of records have
zero strict misses, which is a far less flattering framing than 0.88 recall and
a far more useful one. The scorecard was built to make the unflattering number
the prominent one.
-->

---
layout: two-cols
---

<div class="kicker">Redaction · evidence</div>

# The averages that would have hidden it

<MeterRow
  title="Strict recall by channel"
  unit="every character of the span rewritten"
  :items="[
    { label: 'shell', value: 1.0 },
    { label: 'diffs', value: 0.867 },
    { label: 'agents', value: 0.857 },
    { label: 'notes', value: 0.846, severity: 'warning', note: 'high-risk channel' },
    { label: 'ocr', value: 0.828 },
    { label: 'jobs', value: 0.792, severity: 'warning', note: 'high-risk channel' },
  ]" />

<div class="text-sm mt-5 opacity-85">

`notes` and `jobs` are the high-risk channels — free text a human typed, and
SLURM output nobody curated. A corpus-wide average hides exactly those, so the
scorecard breaks recall out by channel, by difficulty tier, and by HIPAA
identifier class.

</div>

::right::

<div class="mt-14 text-sm">

**`recall_strict` counts a gold span only when every character of it was
rewritten.**

A detector that clips is leaking, not partially succeeding — so the scorecard
reports `recall_partial` and the clip gap beside every label. A large gap means
the detector clips.

<div class="mt-4">

**There is deliberately no F1.**

Precision carries a **ceiling, not a floor**. A floor would let a detector trade
recall away for precision; the ceiling only stops it trading the other way.

</div>

<div class="mt-4">

**And every strict miss is printed.**

All 80 of them, with label, channel, difficulty and the gold text — safe to
publish because every value is drawn from a reserved range or an independent
lexicon draw.

</div>

</div>

<!--
Breaking the numbers out by channel is the methodological point. Had this been
reported as one corpus-wide average, jobs at 0.79 would have disappeared behind
shell at 1.00 — and jobs is the channel carrying SLURM output nobody curated.
-->

---
layout: default
---

<div class="kicker">Redaction · the honest slide</div>

# What this does not prove

<div class="grid grid-cols-2 gap-8 mt-4 text-sm">
<div>

<div class="p-3 rounded mb-3" style="border:1px solid rgba(208,59,59,.5)">
<strong style="color:#d03b3b">■ This is not a HIPAA Safe Harbor determination.</strong><br/>
<span class="opacity-85">Safe Harbor requires all 18 identifier classes removed
<em>and</em> no actual knowledge that residual information could identify
someone. A recall number establishes neither, and nothing here substitutes for
IRB or security review.</span>
</div>

<div class="p-3 rounded mb-3" style="border:1px solid rgba(208,59,59,.5)">
<strong style="color:#d03b3b">■ Identifier 17 is uncovered by construction.</strong><br/>
<span class="opacity-85">A text de-identifier does nothing to a face in a
video-call window. The control there is deletion or source gating —
<code>retain_images=False</code> — not redaction.</span>
</div>

<div class="p-3 rounded" style="border:1px solid rgba(250,178,25,.55)">
<strong style="color:#fab219">▲ The corpus is synthetic.</strong><br/>
<span class="opacity-85">Its <code>easy</code> tier shares identifier shapes
with the generator that built it, so high <code>easy</code> recall is partly a
measure of internal consistency. That is why the gate is computed over
<code>easy</code>+<code>medium</code> and <code>hard</code> is reported
separately.</span>
</div>

</div>
<div>

<div class="p-3 rounded mb-3" style="border:1px solid rgba(250,178,25,.55)">
<strong style="color:#fab219">▲ Real OCR garble is worse than the hard tier.</strong><br/>
<span class="opacity-85">The generator substitutes at most two digit-lookalikes
per value. A real screenshot at low DPI drops characters, merges columns and
reflows lines.</span>
</div>

<div class="p-3 rounded mb-3" style="border:1px solid rgba(250,178,25,.55)">
<strong style="color:#fab219">▲ This measures the detector, not the system.</strong><br/>
<span class="opacity-85">It says nothing about the rewrite applying correctly,
key handling, the crash-recovery path, frame redaction, or the operator who ran
it.</span>
</div>

<div class="p-3 rounded" style="border:1px solid rgba(128,128,128,.4)">
<strong>The floors are regression gates, not safety claims.</strong><br/>
<span class="opacity-85"><code>NAME 0.10</code> does not mean a 10% name recall
is acceptable. It means <em>"the regex tier does not detect names"</em> is now a
tested fact — and the baseline the model tier has to beat.</span>
</div>

<div class="mt-4 text-xs opacity-70">
The prototype targets <strong>non-PHI inputs only</strong> — synthetic or public
datasets, matching the challenge brief's hard boundary. Application
denylisting, encryption at rest and a panic-pause remain future work.
</div>

</div>
</div>

<!--
Leaving this slide in is the point. A privacy claim nobody can attack is a
privacy claim nobody has tested. Every limit here is written into the generated
scorecard, so it gets re-stated every time the numbers are regenerated rather
than living in a slide that ages out.
-->
