# wfrec de-identification — build spec

*Implementation handoff. Distilled from [`pland2.md`](pland2.md), which holds the design argument
for every choice here under the same headings. **When something below looks wrong, over-built, or
expensive, read the argument there before changing it** — most of these were the second or third
answer, not the first.*

Line references verified 2026-09-17. `grep` to confirm before editing.

**Start here:** read §0 (invariants) and §3 (build order), then implement step 1. Steps 1–4 are
self-contained — no new dependencies, no CI changes, no model — and they are what turns "we scrub
things" into a measured number. Sections §4–§7 are reference specs the build order points into;
read each one when you reach its step, not before.

---

## 0. Invariants

Violating any of these is a bug, not a tradeoff.

1. **One detector.** The regex pattern table lives only in `engines/regex_rules.py`;
   `SensitiveDataRedactor` is reimplemented on top of it with the same return type.
2. **One taxonomy.** `labels.py` feeds the regex tier, the zero-shot prompt, the LLM schema enum,
   and the corpus validator.
3. **Offsets are half-open NFC code-point offsets.** An offset outside `[0, len(text)]` **aborts the
   whole seal** — a wrong-region replacement looks like success.
4. **Never ask a model for offsets.** LLMs return verbatim substring + 1-based occurrence; offsets
   are computed locally; unverifiable findings are **dropped**.
5. **`surrogate_guard` and `regex_rules` always run.** No flag substitutes them; `--engine llm`
   swaps the *model* tier only.
6. **Fail closed = refuse to produce a sealed artifact.** A degraded run is `sealed=partial`,
   **never** `sealed=verified`.
7. **No reverse map, ever.** No `pseudonyms.json`; no `sha256(surface)` in the audit (a 6-digit MRN
   brute-forces in ms — use `value_id`); no matched text, bodies, credentials or prompt echoes.
8. **Egress class comes from the resolved address, not the provider name** — and when a proxy is in
   play, classify the **proxy**.
9. **Credential presence is never a gate layer.** No keys in config, no dotenv, no `.env` autoload.
10. **The seal and any weight fetch run in the foreground CLI, never the daemon.**
11. **These existing assertions must pass unmodified:** `test_pipeline.py:28,67,68`,
    `test_redaction.py:9-11`, `test_wfrec_session.py:198`, `test_wfrec_collectors.py:92,149`,
    `test_wfrec_agents.py:397`, `test_wfrec_cli_api.py:142`.
12. **CI downloads nothing** — enforced by an autouse fixture, not by convention.

---

## 1. Layout and CLI

New subpackage `src/autocab/deid/`. No third console script: verbs split along the existing
`autocab` (pipeline/measurement) vs `wfrec` (session) seam.

```
src/autocab/deid/
  labels.py      THE taxonomy — HIPAA 1-18 + bio shapes
  spans.py       Span dataclass, resolve()
  policy.py      label -> Action, profiles, render mode
  allowlist.py   KEEP set (bioinformatics false positives)
  pseudonym.py   Pseudonymizer (HMAC, ephemeral key)
  egress.py      endpoint -> EgressClass. stdlib only, injected resolver, no network
  llm_gate.py    the five-layer gate, pure logic, no SDK
  models.py      ModelSpec: repo, pinned commit sha, per-file sha256, license assertion
  compat.py      RedactionReport / wfrec Redacted adapters + label -> legacy finding-name map
  engines/       base, regex_rules, surrogate_guard, gliner_onnx,
                 transformers_ner, presidio_engine, llm_findings, registry
  providers/     base, anthropic_msgs, openai_compat, ollama_native, google_genai, registry
  eval/          corpus, scorer, report, generate

src/wfrec/seal.py    orchestration + I/O (detection stays in autocab.deid)
src/wfrec/frames.py  box redaction
src/wfrec/video.py   ffmpeg stitch, shared with ScreenCollector
```

**`engines/` vs `providers/` is load-bearing.** *Every* safety-relevant decision — chunking, prompt,
schema, verification ladder, post-check, audit — lives in `engines/llm_findings.py`. A provider's
whole job is *text in, findings out*; it cannot weaken verification because it never runs it. That
is what makes four transports tolerable: a neglected provider degrades to fewer verified findings,
never a weaker control.

```
autocab deid eval --engine regex|gliner|torch|presidio|llm [--llm-provider ...]
                  [--difficulty ...] [--write-scorecard] [--report-latency]
autocab deid gen-corpus --seed 1337 --check
wfrec seal [id] [--engine gliner|gliner+llm|llm|torch] [--reseal] [--force] [--status] [--dry-run]
                [--llm-provider anthropic|openai|ollama|google] [--llm-base-url ...] [--llm-model ...]
wfrec deid fetch [--bundle out.tar.zst] | load <bundle> | verify
wfrec deid providers
```

---

## 2. Contracts

```python
@dataclass(frozen=True, slots=True)
class Span:
    start: int; end: int        # half-open, NFC code-point offsets
    label: str; detector: str
    score: float = 1.0
    protect: bool = False       # nothing may overlap this region

class Detector(Protocol):
    name: str
    def info(self) -> DetectorInfo: ...
    def detect(self, texts: Sequence[str]) -> list[list[Span]]: ...
```

`detect` is **batch-shaped by contract** so no call site can invoke a model once per string across
30k events. `find_spans` implements it as a trivial loop.

### `spans.resolve()`

1. Clip to bounds, drop empty.
2. **Drop** (not clip) any candidate intersecting a `protect` span.
3. Drop allowlisted surfaces (match the *normalized* surface).
4. Sort by `(start, -end, LABEL_RANK, DETECTOR_RANK, label, detector)`.
5. Sweep, absorbing overlaps into the **outer hull** — never splitting. Splitting nested spans emits
   `NAME_ab12 MRN_cd34` from one string: noisier *and* leaks structure.
6. Replace **right-to-left** so earlier offsets stay valid.

```
LABEL_RANK    = SSN < MRN < SUBJECT_ID < ACCESSION < PHONE < EMAIL < DEVICE
                    < DATE < NAME < AGE < LOCATION < ORG < DENY
DETECTOR_RANK = regex < model < llm      # regex wins ties: its label is structurally certain
```

### Render modes — the compatibility contract

| Path | Mode | Output |
|---|---|---|
| `SensitiveDataRedactor` / AutoCAB cluster redaction | `mask` | `[REDACTED_<LABEL>]` — unchanged |
| `wfrec` inline capture | `mask` | `[REDACTED_<LABEL>]` — unchanged |
| The seal pass | `pseudonymize` | `NAME_a1b2c3` |

**Only the seal pseudonymizes** — pseudonyms need a key stable across processes and restarts, which
an in-memory key can't give (daemon + `--no-daemon`) and a key file beside the data recreates the
re-identification key that discarding it exists to prevent. Spans masked at capture arrive as
`[REDACTED_MRN]`, get `protect`ed, and are audited as masked-at-capture.

Legacy finding names `email`, `sj_id`, `mrn`, `dob` must survive verbatim as `labels.py` grows →
`compat.py` needs an explicit map, **not** `label.lower()`.

### Engines

| Layer | Dependency | Default | Role |
|---|---|---|---|
| `surrogate_guard` | stdlib | **always first** | `protect` spans over sealed text → idempotency |
| `regex_rules` | stdlib | **always on** | the floor; the only layer inline at capture |
| `gliner_onnx` | **base** | **always on**, weights auto-resolved | the default model tier |
| `transformers_ner` | `[deid-torch]` | opt-in | `obi/deid_roberta_i2b2`, MIT, highest clinical recall |
| `presidio_engine` | `[deid-presidio]` | opt-in | validators adopted, framework declined |
| `llm_findings` | provider extra | **OFF** | additive only, egress-classified, 4 providers |

An unavailable engine reports `unavailable: <reason>` and the seal **fails closed**.
`Redacted.available`'s degraded mode is right for *capture*, wrong for a *seal*.

### Pseudonyms

`surrogate = f"{PREFIX[label]}_{hmac_sha256(key, label + b'\0' + norm)[:6].hex()}"` → 12 hex chars
(48 bits). `label` is in the HMAC input so a value that is both a plausible MRN and a plausible
account number doesn't collapse across labels.

| Class | Normalization |
|---|---|
| `MRN, SUBJECT_ID, SSN, PHONE, ACCESSION, DEVICE` | strip captured label prefix, drop non-alphanumerics, uppercase |
| `EMAIL` | `casefold()` the whole address (no Gmail-style dot-stripping) |
| `NAME` | NFKC, casefold, collapse whitespace, strip titles (`Dr.`, `MD`, `PhD`); keep middle initials |
| other | NFKC + casefold + whitespace collapse |

- **Dates generalize, never pseudonymize**: `2012-06-01` → `[DATE:2012]`; age `> 89` →
  `AGE_90_PLUS` (`<= 89` kept). Pseudonymizing dates destroys the timeline the recorder exists to
  capture, and Safe Harbor only asks for elements finer than year.
- **Key lifecycle**: `secrets.token_bytes(32)` in a `bytearray`, overwritten in place in a `finally`;
  `__repr__` → `"Pseudonymizer(key=<discarded>)"`; excluded from serialization. Docstring says this
  is best-effort — the real guarantee is that it never leaves the process.
- **Idempotency is by grammar, not by key.** `SurrogateGuard` emits `protect=True` spans for
  `\b(?:NAME|MRN|SUBJ|SSN|PHONE|EMAIL|ADDR|ORG|DEV|ACC|ID)_[0-9a-f]{6,}\b`,
  `\[REDACTED_[A-Z_]+\]`, `\[DATE:\d{4}\]`. Sealed text is therefore a fixpoint.
- **`analyst` and `host` are KEEP** — workforce, not the PHI subject; pseudonymizing `analyst`
  breaks `WorkflowClusterer`'s grouping. Override: `--pseudonymize-analyst`. **Document inline** —
  this gets second-guessed in review.

---

## 3. Build order

Each step ships. **Step 4 needs zero new dependencies and zero CI changes — ship at least that far.**

| # | Step | Deliverables | Gate |
|---|---|---|---|
| 1 | Spans + refactor | `labels.py`, `spans.py`, `policy.py`, `allowlist.py`, `engines/regex_rules.py::find_spans()`; `SensitiveDataRedactor` (`components.py:98`) reimplemented on top inside `try/except ImportError` falling back to current literals | `test_redaction.py` + `test_pipeline.py` pass **unmodified** |
| 2 | Corpus **before** detector | `eval/generate.py`, `data/deid-eval/`, `test_deid_corpus.py` | `gen-corpus --check` reproduces byte-for-byte |
| 3 | Scorer | `eval/scorer.py`, `test_deid_scorer.py` | clipped-span case asserts strict miss + partial hit |
| 4 | **The measured claim** | `thresholds.json`, `test_deid_regex_recall.py`, `autocab deid eval`, `docs/phi-redaction-benchmarking.md` | per-label floors + over-redaction ceiling + scorecard snapshot |
| 5 | **Security boundary first** | `egress.py`, `llm_gate.py`, `test_deid_egress.py`, `test_deid_gate.py` (§5) | all 2⁵ layer combos per class; exactly one allows |
| 6 | Seal, with a fake engine | `src/wfrec/seal.py` (§4); `DEID_SEALED`+`SessionSealed` in `events.py`; gates in `exporters/__init__.py:30` and `session_bundle.py:41`; `seal`/`deid` subparsers in `cli.py` routed **before** the daemon-preferring block (like `export`); `POST /sessions/seal`; detached spawn from `recorder.py:205` | `wfrec pull` on a sealed session raises |
| 7 | Close the inline gaps | `files.py:{381-405,451-459,491}`, `remote.py:{131,198}` — written **verbatim** today, so the seal is their *only* control | extend `test_wfrec_collectors.py`, `test_wfrec_remote.py` |
| 8 | Package GLiNER (§6) | `engines/gliner_onnx.py`, `models.py` (int8 **and** fp32 + license assertions), `pyproject.toml` move, `[deid-local]` deleted, `uv.lock` regenerated **same commit**, autouse offline fixture, `wfrec deid fetch\|load\|verify`, `test_deid_weights.py`, `doctor.py` row, `deid-nightly.yml`, model-tier thresholds. Then `transformers_ner.py` | `pip install -e .` alone yields a working model tier |
| 9 | Frames and video (§7) | sidecar in `screen.py:309`, ffmpeg → `src/wfrec/video.py`, `src/wfrec/frames.py` | `test_deid_frames.py`, `test_deid_audit.py` |
| 10 | LLM tier (§5) | `engines/llm_findings.py` + `providers/{base,registry}.py` + `wfrec deid providers` + `test_deid_llm.py`. Providers in order: **`anthropic_msgs`** → **`openai_compat`** (unlocks Codex *and* every local server in one transport) → **`ollama_native`** → **`google_genai`**. Stopping after two satisfies most of the ask | mocked transport × all four |
| 11 | Docs honesty pass | see below | — |

Step 8 is deliberately **after** 2–4: packaging the detector does not change the rule that the
corpus and the scorer come first.

### Step 1 detail
- Fix `components.py:128`: `re.sub(token, ...)` is **unescaped and unbounded** — it turns
  `outpatients_cohort.tsv` into `out[REDACTED_TERM]s_cohort.tsv`, and a term containing `(` raises
  at redaction time. Use `re.escape` + word boundaries.
- **Adopt Presidio's checksum validators** (MIT): SSN/NPI/card checksums, phone/URL/IP patterns.
  Cheapest precision win available; the current tier has no validation at all.
- `allowlist.py` default KEEP set: GIAB sample names, reference builds, tool names, HGNC symbols,
  HGVS `c.`/`p.`, `chrN:pos`, accessions (`SRR12345678`), barcodes (`ATCACG`, `SI-GA-A1`), container
  tags with date-like versions. Extensible via `~/.wfrec/deid-allowlist.txt`. `SJ-1234` is
  deliberately **not** allowlisted. Note `PipelineConfig.benchmark_dataset = "GIAB HG008"`
  (`framework/config.py:25`) — without the allowlist a clinical model redacts the repo's own
  benchmark name.

### Steps 2–4 detail (the eval harness)

```
data/deid-eval/
  README.md  thresholds.json  scorecard.regex.json
  lexicons/{surnames,given-names,cities,gene-symbols}.txt
  corpus/{shell,ocr,notes,agents,diffs,jobs,prescrubbed,negatives}.jsonl
```

Record: `{id, channel, difficulty, text, spans:[{start,end,label,text,hipaa}], must_survive:[...],
provenance:"generated:v1:seed=1337:tmpl=...", license:"CC0-1.0"}`.

- **Everything must be `.jsonl`.** `.gitignore` already ignores `*.vcf`, `*.fastq`, `*.bam`, `*.log`
  — a fixture named `slurm-4213.log` is silently dropped from the commit. Embed those shapes as
  strings inside JSONL.
- `span.text` is a redundant copy of `text[start:end]`; **CI asserts equality for every span in
  every record.** Cheapest defence against offset rot, and it will fire.
- **Tier 1 safety — structural reservation**, enforced by test: `@example.com`/`.org` (RFC 2606),
  `+1-555-01xx` (NANP fictitious), `000-`/`666-`/`9xx-` SSNs, `192.0.2.0/24`, `198.51.100.0/24`,
  `203.0.113.0/24` (RFC 5737), a documented test-only `44xxxxx` MRN band.
- **Tier 2 — independent-draw provenance** for `NAME`, `LOCATION`, `DATE`, `AGE` (no reserved space
  exists): each field an independent seeded draw from a committed public-domain lexicon, so no
  *combination* maps to a real person. Generator deterministic and network-free.
- **Difficulty stratification is not optional.** Gate on `easy`+`medium` only, or the benchmark is a
  tautology — the regex tier scores 1.00 on shapes the generator built from the same regexes.
  `hard` (`MRN l23456` with an ell, `M.R.N. 123456`, `MRN:123456`) is reported, gated far lower.
- **Gold is minimal-span**: `/data/proj/SJALL018/smith_jane_R1.fastq.gz` → two spans
  (`SJALL018`→`SUBJECT_ID`, `smith_jane`→`NAME`), not one over the path. Whole-path redaction still
  earns full recall credit but is charged as over-redaction.
- **`negatives.jsonl` must be adversarial** or precision is meaningless: HGNC symbols that read as
  names, `c.1521_1523delCTC`, `p.Phe508del`, `chr7:117559590`, `GRCh38`/`hg19`,
  `biocontainers/gatk:4.5.0.0--2024-01-15`, `SRR12345678`, `samtools sort -@ 8`, `GIAB HG008`.

**Metrics** are all defined on the **original** text. `G` = a gold span's char indices; `P` = the
union of *all* predicted spans' indices for that record, **regardless of predicted label**.

- **`recall_strict`** — counts iff `G ⊆ P`. **This is the gate.**
- **`recall_partial`** — counts iff `|G ∩ P| > 0`. Diagnostic, **never gated**. High partial + low
  strict means the detector clips (`[NAME] Smith`), which is a leak, not a partial success.
- **Label is ignored for safety**; scored separately as `label_accuracy` over strictly-recalled spans
  (the label picks the pseudonym class — a mislabel costs utility, not safety).
- Utility: `over_redaction_rate`, `false_positive_spans_per_negative_record`,
  `must_survive_violations`. Precision gets a **ceiling, not a floor** — that ceiling is what stops a
  `.*` patch gaming recall to 1.00.
- Headline: `safe_record_rate` (records with zero strict misses) and `leaks_per_1000_records`.
- **No single F1** — it lets precision buy back recall, the one trade this control must never
  silently make. Decision 3 is destructive rewrite: a miss cannot be undone.

Scorer unit tests: exact, clipped, superset, overlaps, adjacency, empty, non-BMP unicode, CRLF.

**Floors** live in `thresholds.json` keyed on a `corpus_fingerprint`, **not** in test source:

```
EMAIL/IP/URL/SSN/MRN/DOB_LABELLED/SUBJECT_ID 1.00   PHONE 0.95   SAMPLE_ID 0.60
SLURM_JOB_NAME 0.50   DATE_BARE 0.30   NAME 0.10   LOCATION 0.05   AGE_OVER_89 0.00
overall 0.55   safe_record_rate 0.40   max_over_redaction_rate 0.05   must_survive_violations 0
```

The `NAME 0.10` / `LOCATION 0.05` / `AGE_OVER_89 0.00` rows look absurd and are the most valuable
lines in the file: they encode "the regex tier does not detect names" as a tested, blame-able fact,
and they are the baseline the model tier must beat. Model tier: `NAME 0.95`, overall `0.95`,
combined `0.97`. **These are regression gates, not safety claims** — the docs must say so.

Plus a **committed scorecard snapshot** (`scorecard.regex.json`): floors catch catastrophic
regressions, not a slide from 0.98 to 0.91. CI asserts computed == snapshot; any pattern change fails
with a diff naming the label that moved and the author reruns `--write-scorecard`, putting the delta
in the PR. **No nondeterministic fields** in the snapshot — no timestamp, hostname, or wall-clock
latency, or it churns and gets ignored.

`docs/phi-redaction-benchmarking.md` is generated and committed, headed "do not hand-edit": headline numbers
first; per-label table (`recall_strict`, `recall_partial`, the gap, `label_accuracy`, floor, margin)
split by difficulty; a HIPAA 1–18 rollup (the language a compliance reviewer reads); a **per-channel**
table (`notes` and `jobs` are the high-risk channels and a corpus-wide average hides them); the
utility table; measured CPU latency per KB per engine; and a **full leak table** of every strict miss
with `record_id`, label and gold text — safe to print because the corpus is synthetic.

Plus a mandatory **"What this does not prove"**: the corpus is synthetic and its easy tier shares
shapes with the regex generator; real OCR garble is worse; HIPAA identifier 17 is uncovered; Safe
Harbor needs all 18 removed *and* no actual knowledge of residual identifiability, which a recall
number does not establish; and this measures the detector, not the rewrite, the key handling, or the
operator.

Expect the harness to earn its keep immediately: the current substring deny-term rule will fail
`max_over_redaction_rate` on `negatives.jsonl` as written.

### Step 11 detail
- Replace the "that is scrubbing, **not a PHI control**" paragraph at `src/wfrec/README.md:263`
  with the measured claim; extend the `redactions` note at `:269`.
- The local-only sentence needs **three cases in the same paragraph, not a footnote**: regex and
  GLiNER are local by construction; a loopback LLM is local; a cloud provider is egress and deletes
  the sentence. **Name the default — local, no egress — in that same paragraph.**
- Update `.claude/skills/recorder/SKILL.md:104` **and its `.github/skills/` twin** (byte-identical
  today — keep them so); fix `src/wfrec/ui/index.html:138`.

---

## 4. Seal mechanics — `src/wfrec/seal.py`

**Coverage:** `events.jsonl` payloads, `context/*.md`, `screen/ocr/*.txt`, **`files/diffs/*.patch`**,
**`shell/remote/*`**, **`jobs/*.out`**, `screen/frames/*.webp`.

**Invert the schema rule.** A fixed field map means any new event type or payload key silently
bypasses the seal: leak-by-default. Instead **recursively walk every payload and scrub every `str`
value**, with an explicit `SKIP_KEYS` set of structural keys (`seq`, `ts`, `type`, `source`,
`backend`, `exit_code`, `duration_ms`, `chars`, `width`, `height`, `sha`, `pid`, `prompt_seq`,
`job_id`, …). Adding an event type can then only over-scrub, never under-scrub.

Paths (`path`, `cwd`, `root`, `workdir`) scrub **component-wise**, keeping separators:
`/data/proj/SJALL018/smith_jane_R1.fastq.gz` → `/data/proj/SUBJ_9c2f1a/NAME_4d81b2_R1.fastq.gz`.

**Preconditions.** Refuse unless the session is genuinely idle, checked three ways because any one
can be stale: `RecorderState.load().active_session`, `read_sentinel()`, `manifest.status`.
`--force` stops first, then seals. **Refuse, don't race** — the alternative is holding
`.events.lock` across minutes of inference, stalling every collector thread.

**The locking property everything rests on.** `EventWriter.append` (`events.py:201`) opens
`events.jsonl` with `"a"` **inside** `file_lock(.events.lock)`, and the lock file is a **separate
inode**. So `os.replace(tmp, events.jsonl)` while holding the lock is safe: a blocked appender
re-opens by path after the lock releases and lands on the new file; no appender holds an fd across
the swap. **`test_deid_seal_concurrency.py` must assert this directly** — a future refactor to a
long-lived append fd would break the seal silently.

| Phase | Lock | Duration | Work |
|---|---|---|---|
| 1 snapshot | `.events.lock` | ms | read timeline, record `snapshot_bytes = st_size`, `sealed_through_seq` |
| 2 scrub | `.seal/lock` only | s–min | detect, pseudonymize, stage every replacement |
| 3 commit | `.events.lock` | ms | verify `st_size == snapshot_bytes`, then `os.replace` each staged file |

A phase-3 size mismatch means a straggler appended after the snapshot: read the tail beyond
`snapshot_bytes`, scrub it synchronously, append to the staged file, retry — bounded at **3 retries
or 200 tail events**, then abort with nothing committed.

**Crash safety.** `<session>/.seal/{lock, journal.json, audit.jsonl, staged/<relpath>}`

| Journal state | Recovery |
|---|---|
| `planning`, `staging` | delete `.seal/`; session **unchanged** |
| `committing` | **roll forward**: replay `os.replace` for every file still under `staged/` |
| `committed` | write `seal.json`, delete `staged/`, done |

The commit loop is idempotent because **the disappearance of a staged file *is* its per-file commit
record**. Hence **every file must be staged before any file is replaced.**

Cross-file atomicity is unachievable on POSIX; this gives the *observable* equivalent.
`recover(session_dir)` runs at the top of `wfrec seal`, `export_session`,
`session_bundle._trace_from_session_dir` and `EventWriter.__init__`, and while a journal sits in
`committing` those readers refuse with `SealInProgress`. A mixed state can exist on disk for
milliseconds but is never *consumed*.

**Post-seal immutability.** `EventWriter.__init__` stats `<session>/seal.json` once and sets
`self._sealed`; `append`/`extend` then raise `SessionSealed` (with a bypass the seal itself uses).
One stat per `Session` construction. **This is what closes the `wfrec pull` /
`attach-transcript` / `merge` hole** — a stopped session can otherwise still be appended to after
scrubbing.

**Marker and reseal.** `seal.json` present → `wfrec seal` is a no-op returning the existing record;
`--reseal` required. A reseal with a better engine mints generation-2 surrogates **only for PHI the
first pass missed**, recorded as `generation: 2` with a per-generation engine list.

### Hook points

| Caller | Behaviour |
|---|---|
| `wfrec seal [id]` | Primary + recovery entry point. **Foreground CLI, never the daemon** |
| `wfrec stop` (`recorder.py:205`) | Prints the command; in `balanced` spawns a detached `python -m wfrec seal <id>` reusing `_spawn_daemon` (`cli.py:332`). Blocking the interactive path by default is how a safety feature gets switched off; safe because the export gates block consumption. `strict` seals synchronously with a progress bar |
| `export_session` (`exporters/__init__.py:30`) | **Hard gate** — `NotSealed` unless `seal.json` exists |
| `session_bundle._trace_from_session_dir` (`session_bundle.py:41`) | **Second hard gate** — it rebuilds from `events.jsonl` live, so gating `exports/` alone is bypassable. Guard the timeline, not the projection |
| `POST /sessions/seal` | Mirrors `/export` for the GUI and recorder skill |

### Batching
`Item(target_id, field_path, text)` → **dedupe by exact string** (consecutive OCR frames share most
text; expect a few thousand unique strings from tens of thousands of events) → regex stage → model
stage → **cache the rewritten string**, not the spans (pseudonymization is a pure function of
`(label, normalized_surface)` within a run).

Transformer path: **explicit tokenizer + model** with `offset_mapping`,
`return_overflowing_tokens=True`, `stride=128`, `max_length=512` — which gives the sliding window
over long OCR dumps for free, plus `overflow_to_sample_mapping` for window→string. **Not**
`pipeline(aggregation_strategy=...)`: its char offsets are unreliable across subword merges on
byte-level BPE, and exact offsets drive both text replacement and image blackboxing.

### Audit
`<session>/deid/audit.jsonl`, one record per finding:

```json
{"target":"events.jsonl","seq":1841,"field":"payload.ocr_text","label":"MRN",
 "engine":"gliner_onnx","score":0.99,"start":412,"end":421,
 "action":"pseudonymize","surrogate":"MRN_a7f3c1","value_id":17}
```

`value_id` is a monotonic integer per distinct `(label, normalized_surface)` — it delivers every
analytic the audit exists for (distinct-value counts, repeat rates, recurrence) with zero
recoverable content, and dies with the run. **Omit `len` for `NAME`**; a 4-character surname is a
meaningful hint.

`<session>/seal.json` is the marker and integrity chain: profile, assurance, generation, engine list
with `patterns_sha256` and each model's pinned revision + `weights_sha256`, `sealed_through_seq`,
`counts_by_label`, `distinct_values`, `collisions`, per-target `before_sha256`/`after_sha256`, frame
and video stats, `pseudonym_key: "discarded"`, `reverse_map: "not-written"`, duration.

The seal appends one `deid.sealed` event as the **last line of the staged `events.jsonl`, before
hashing**, so the timeline self-documents and `after_sha256` covers it. Add
`DEID_SEALED = "deid.sealed"` to `events.py`; skip it in `exporters/trace.py`.

**Limited data set** (separate, explicit, never the default): `--profile limited-dataset
--map-out <path outside ~/.wfrec> --map-recipient <pubkey>` encrypts the map to a public key **the
sealing machine does not hold the private half of**, refuses a path under `paths.home()`, and stamps
`profile: "limited-dataset"` so downstream consumers see it is not Safe Harbor.

---

## 5. LLM tier — one engine, four providers

**Call it `llm`, never `api`, never "remote"** — `wfrec remote` / `src/wfrec/remote.py` already mean
*SSH to an HPC host*. So `engines/llm_findings.py`, `providers/*`, `wfrec seal --llm`, events
`session.deid.llm.*`, config `[deid.llm]`, `llm_gate.py`.

**One engine**, because "designate a local LLM" and "supply a key for Claude or Codex" are the same
shape: an LLM returning verbatim substrings, verified locally, contributing **additively** on top of
the regex + GLiNER floor. The only real difference is how far the text travels — a property of the
resolved endpoint, not the vendor's name.

**The local tier must run first — mandatory, non-configurable.** The gate's refusal predicates
include "local tier has not completed for this session". Without it: the disclosure is larger; a
failed call can leave a session with *no* de-identification and a `sealed` flag (fail-closed
composition); and the "measured recall in CI" claim evaporates, since CI can enforce no floor for
someone else's model. Pre-inserted pseudonyms confusing the detector is a real but small risk —
mitigate with distinctive placeholders, a system-prompt note that they're already redacted, and
`corpus/prescrubbed.jsonl` as a gold tier that *measures* it.

```python
class LlmProvider(Protocol):
    name: str
    def endpoint(self) -> str                    # what egress.py classifies
    def default_model(self) -> str | None
    def list_models(self) -> list[str]           # for `deid providers`; may be empty
    def extract(self, system: str, chunk: str, schema: type[BaseModel]) -> ProviderResult
```

`ProviderResult`: `findings, model_served, request_id, usage, stop_reason, schema_mode, latency_ms`.

| Provider | Extra | Structured output | Covers |
|---|---|---|---|
| `anthropic` | `[deid-anthropic]` | `client.messages.parse(..., output_format=Findings)` | Claude |
| `openai` | `[deid-openai]` | `response_format={"type":"json_schema","json_schema":{...,"strict":true}}` | Codex/OpenAI, **vLLM, LM Studio, llama.cpp, Azure, internal gateways** — anything with a `base_url` |
| `ollama` | **none** | `POST /api/chat` with `format=<schema>`, `stream=false`, `keep_alive` held across chunks so the model loads once; `/api/tags` discovery, `/api/ps` load state | Ollama, natively |
| `google` | `[deid-google]` | `response_mime_type="application/json"` + `response_schema` | Gemini API, Vertex |

- Ollama needs **no extra**: plain HTTP over the `httpx>=0.26` already in base deps
  (`pyproject.toml:19`). The lowest-friction LLM path is also the zero-egress, zero-new-dependency
  one — the right incentive.
- **Strict JSON-schema support is not uniform across things claiming OpenAI compatibility.**
  `openai_compat.py` **probes and degrades**, recording the rung as `schema_mode` in the audit:
  `strict_schema` → `json_object` → `prompt_json` (local `json.loads` + pydantic validate, one
  retry, then drop the chunk and stamp `sealed=partial`). Degrading the *parse* is safe precisely
  because it cannot degrade the *verification*.
- **Model IDs: resolve, don't transcribe.** `providers/registry.py` resolves defaults from the
  provider's own model-list endpoint where one exists (`/api/tags`, `/v1/models`), otherwise carries
  a constant marked *verify against the provider's docs at implementation time*.
  **Local providers have no default — `--llm-model` is required**; which 8B model someone pulled is
  unknowable and guessing wrong burns a 30-second load to produce garbage.
- **Anthropic default: `claude-sonnet-5`** — bounded extraction, not reasoning; ~15k–150k input
  tokens post-stage-1 → ~$0.03–$0.30/session. `claude-opus-5` behind `--llm-model` for max recall.
  **Do not use `claude-fable-5`**: it requires 30-day retention and is unavailable under zero data
  retention — and an org whose gate here is a ZDR attestation structurally cannot use it.
  **Re-read the `claude-api` skill at implementation time**; don't trust IDs transcribed here, and
  don't append date suffixes.

### Request shape and verification ladder

```python
class Finding(BaseModel):
    record_id: str; text: str; label: DeidLabel
    occurrence: int = 1                              # 1-based
    confidence: Literal["high","medium","low"]
```

Structured outputs — **not** free JSON in prose and **not** tool use (forcing a function call to
smuggle out data with no function to run is the pattern structured outputs supersede).
`thinking={"type":"adaptive"}`, `output_config={"effort":"medium"}`. Chunk on **record boundaries,
never mid-record**, ~200 records / 40k chars per request, so a retry is cheap and occurrence
localization happens inside a short string where it is essentially never ambiguous.

Per finding, client-side:

1. Exact match at the claimed occurrence → **accept** (offsets exact by construction).
2. Exact match, fewer occurrences than claimed → accept the last, record `occurrence_adjusted`.
3. No exact match → normalized match (NFKC, collapse whitespace, casefold); accept a **unique** hit
   as `match_mode="normalized"`.
4. Otherwise **drop** and increment `findings_unverified`.

Then an **independent post-check**: re-run the local regex tier over the rewritten text. Any hit is
a hard error — it means the rewrite did not apply. Cheap, and it catches whole classes of bug that
offset verification cannot.

**Prompt caching: default OFF** (`[deid.llm] prompt_cache = false`). The system prompt is
byte-identical every request so caching is free money — but a cache is server-side retention of a
prefix, and an org whose gate is a ZDR attestation deserves to opt in rather than discover it.
Meaningless for a loopback model. **Refusal fallbacks: off** — one named model per configured
policy; record `model_served` regardless.

### Egress classification — `egress.py`

Structural, from the **resolved address**, because the provider label carries no information about
where the bytes go.

| Class | Condition | Gate |
|---|---|---|
| `none` | unix socket, or **every** resolved address is loopback (`127.0.0.0/8`, `::1`) | **No egress gate.** Consent event still written |
| `internal` | **every** resolved address is private/site-local/link-local/CGNAT (`10/8`, `172.16/12`, `192.168/16`, `fc00::/7`, `169.254/16`, `100.64/10`) | Ack with `internal_endpoints_approved`; BAA and ZDR clauses **not** required. Per-invocation flag required |
| `external` | any resolved address globally routable, **or** resolution fails, **or** the set is mixed | **All five layers**, incl. BAA, ZDR, verbose flag |

1. **Escalate on ambiguity.** Mixed A records, DNS failure, or any non-loopback in the set → the
   most restrictive class present. Classifying off record zero is the natural implementation and it
   is unsafe.
2. **Pin the resolution.** Resolve once in the gate, classify, hand the resolved address to the HTTP
   client — or re-verify immediately before the request and abort on change, else a short TTL
   downgrades the class between check and call. Honest limit: this closes the accidental case, not
   an attacker who controls both DNS and the box.
3. **Classify the proxy, not the endpoint.** If `HTTP_PROXY`/`HTTPS_PROXY`/`ALL_PROXY` is set and
   the endpoint is not matched by `NO_PROXY`, the text goes to the **proxy**. Without this rule
   `--llm-base-url http://localhost:11434` under `ALL_PROXY` classifies as `none` while egressing
   every byte. **The easiest bypass in the design to miss, and the most important line here.**
4. **`::ffff:127.0.0.1` is loopback; `0.0.0.0` and `[::]` are not.** Both directions are routinely
   gotten wrong; both are in the test matrix.
5. **Plaintext to non-loopback is refused** unless `--allow-plaintext-llm`, which stamps
   `sealed=partial`. Loopback over `http` is fine and must never be nagged about.
6. **`deny_egress_classes[]`** in the ack, alongside `deny_hosts[]`, so an org can write "loopback
   only, ever" once and have it hold for every provider and every future flag.

`egress.py` is pure stdlib, no SDK, no network — **the resolver is injected** so the whole matrix is
testable offline.

### The gate

`llm_gate.evaluate() -> GateDecision`, deliberately **non-short-circuiting** so the user sees every
failed layer at once. Scoping: `external` → all five; `internal` → 1, 3, 4, 5 plus an ack carrying
`internal_endpoints_approved`; `none` → 1 and 5 only (a consent event is still written for a
loopback call — "which model saw this session" is an audit question independent of whether anything
left the machine).

| # | Layer | Where |
|---|---|---|
| 1 | Install opt-in — the `deid-anthropic`/`deid-openai`/`deid-google` extra | `pyproject.toml`. With the SDK absent the most reliable gate is an `ImportError`. **Ollama is the deliberate exception**: gating an install on a transport that reaches only loopback buys nothing; its containment is layer 2's `allowed_endpoints[]` plus the classifier |
| 2 | Org-policy acknowledgement, **per provider** | `~/.wfrec/policy/deid-llm-<provider>.json` — its own file, not `state.json`, not the config. Requires `provider`, `org`, `baa_confirmed`, `zero_data_retention_confirmed`, `approver_name`, `approver_role`, `approved_at`, `policy_reference`, `expires_at`, `allowed_endpoints[]`, `deny_hosts[]`, `deny_egress_classes[]`. An attestation **naming a human**, and it **expires** |
| 3 | Config flag `[deid.llm] enabled` | `~/.wfrec/config.toml` — standing user intent, in a *different file* from the org attestation. One person editing one file is never sufficient |
| 4 | Per-invocation flags | `wfrec seal --llm --i-am-sending-text-offbox`. The second is deliberately verbose and **kept out of the short `--help`** so it can't become muscle memory. A persistent config flag must never by itself cause egress. Not required for class `none` |
| 5 | Consent event, **pre-flight** | `session.deid.llm.consent` in `events.jsonl` **before the first byte leaves**: provider, resolved endpoint, **egress class**, sha256 of the ack file that authorized *this* egress, `approver_name`, `policy_reference`, resolved model, exact record/char counts about to be sent. Writing it first means a crash mid-call still leaves evidence egress was *attempted*; `append` already `fsync`s |

**One ack per provider.** A BAA is a contract with a named counterparty: an attestation naming
Anthropic must not authorize egress to OpenAI or Google. `test_deid_gate.py` asserts the isolation,
including ack-for-A + invocation-names-B → abort.

Two guards that are not layers: an interactive confirmation requiring the session id typed back
(bypassable only by `--yes` **and** `WFREC_DEID_LLM_NONINTERACTIVE=1` **together**, so a script
can't inherit the bypass from a stray flag); and refusal predicates — expired ack, host in
`deny_hosts`, class in `deny_egress_classes`, endpoint outside `allowed_endpoints`, provider
mismatch, `max_chars_per_session` exceeded, local tier incomplete.

**Credential presence is deliberately not a layer, for any provider.** An unset `ANTHROPIC_API_KEY`
does not mean there are no credentials — the SDK also resolves `ANTHROPIC_AUTH_TOKEN`, an
`ant auth login` profile under `~/.config/anthropic/`, and WIF env vars. Gating on key availability
would let ambient credentials silently arm network egress.

### Key handling

- **Let each SDK resolve its own credential**: zero-arg `anthropic.Anthropic()`, `openai.OpenAI()`,
  `google.genai.Client()`, so we never hold a secret in a variable, never pass one through a
  signature, and never have one in a frame that could land in a traceback. **Ollama needs no
  credential at all.** On auth error, print provider-specific guidance.
- OS keyring only as an optional convenience in the extra, degrading with a named reason (`keyring`
  on Linux pulls SecretStorage/dbus and fails on a login node with no session bus).
- **Never a config file.** The schema has no key field, and the loader **hard-rejects** — refuses to
  run the LLM path, not a warning — any config with a key *named* `api_key`/`token`/`secret`, or any
  string matching `sk-ant-[A-Za-z0-9_-]{20,}`, `sk-(proj-)?[A-Za-z0-9_-]{20,}`,
  `AIza[A-Za-z0-9_-]{30,}`. The key-*name* rejection is the real catch-all; the shapes are belt and
  braces for a key pasted under an innocuous name.
- **No dotenv dependency and no `.env` autoload from cwd** — that is exactly how a key ends up
  inside a session folder when someone records while sitting in a repo, and session folders are
  designed to be zipped and handed to teammates.
- Tested, not stated: a `_scrub_secrets()` guard on the audit writer, plus a CI test
  **parameterized over all four credential shapes** that plants a fake key in the environment, runs
  a fully mocked seal, then walks the entire session tree *and* `~/.wfrec/daemon.log` asserting the
  string appears nowhere.

### LLM audit
Per chunk (`session.deid.llm.chunk` + `<session>/deid/seal-report.json`): `provider`,
`model_requested`, `model_served`, **`egress_class`**, **`schema_mode`**, endpoint **host only**,
`request_id`, chunk index, `records_in_chunk`, `chars_sent`, `chunk_digest`, token usage,
`latency_ms`, `findings_count`/`_verified`/`_unverified`, `stop_reason`, failure class, running
`chars_sent_total`; plus one aggregate `session.deid.llm.completed`. **Never** the request or
response body, any finding's text, the credential, or any prompt echo.

`chunk_digest` is an **HMAC** with the ephemeral session key, not a bare hash — a plain SHA-256 of
`MRN 4419902` is brute-forceable in ms, so a plain digest would itself be a disclosure. Per chunk,
never per span.

---

## 6. Packaging and weights

```toml
[project]
dependencies = [
  # ... existing control plane / capture / OCR entries unchanged ...

  # --- de-identification: the default model tier, not an opt-in ---
  # In base deps rather than an extra, because a PHI control that requires a
  # second install flag is a PHI control that is off on most machines. The
  # marginal cost is two small wheels: onnxruntime already arrives transitively
  # via rapidocr-onnxruntime and numpy via opencv. Listed explicitly anyway --
  # depending on another package's transitive dep for a core code path is how a
  # future rapidocr bump silently breaks you (same reasoning as the
  # opencv-python-headless comment above).
  "onnxruntime>=1.17",
  "tokenizers>=0.20",
  "huggingface-hub>=0.24",
  "numpy>=1.24",
]

[project.optional-dependencies]
deid-torch     = ["torch>=2.2", "transformers>=4.40"]
deid-presidio  = ["presidio-analyzer>=2.2", "spacy>=3.7"]   # + the en_core_web_lg problem, documented
deid-anthropic = ["anthropic>=1.0"]      # confirm the major with the claude-api skill
deid-openai    = ["openai>=1.40"]        # also covers Codex, vLLM, LM Studio, Azure, gateways
deid-google    = ["google-genai>=1.0"]   # Gemini API + Vertex
# Ollama native deliberately has NO extra: plain HTTP over the httpx that
# pyproject.toml:19 already depends on.
dev            = ["pytest>=8.0"]         # UNCHANGED -> CI adds no LLM SDK
```

- `[deid-local]` is **deleted**, not kept as an alias.
- `dev` untouched → `.github/workflows/tests.yml` needs no edit.
- Nothing heavy in base deps: no torch, no LLM SDK, no spaCy. The regex tier must still work with
  stdlib only — that is what lets `SensitiveDataRedactor` delegate to `find_spans()` without making
  any of this a hard requirement.
- Don't pin a CUDA index; document `--index-url .../whl/cpu` for login nodes.
- **`uv.lock` is committed** — regenerate in the same commit. This touches base deps, so the lock
  diff will be larger than usual and should be reviewed, not waved through.
- **Presidio: validators yes, framework no.** Declined because `en_core_web_lg` is not on PyPI,
  installs from a GitHub release URL via `spacy download` (breaking air-gapped and proxied
  installs), and spaCy's `thinc`/`numpy` pins fight other dependencies — the `libGL.so.1` class of
  fragility `pyproject.toml` already documents.

**Model: `gliner_small-v2.1`, ONNX, int8.** Zero-shot is the decisive property — labels are authored
in *our* `labels.py` as descriptions (`"medical record number"`, `"SLURM job name"`, `"patient name
embedded in a file path"`, `"sequencing sample identifier"`). Every fixed-label clinical model
collapses MRN/account/license/device into one `ID` bucket and has no concept of a SLURM job name.
~0.1–0.3 s/KB CPU, ~0.2–0.6 GB RAM.

> **License trap:** `gliner_small-v2.1` and `gliner_medium-v2.1` are Apache-2.0, but **`gliner_base`
> and `gliner_multi` are CC-BY-NC-4.0.** `models.py` pins checkpoint *and* revision and asserts the
> license string in a test. Re-verify on the model card at implementation time.

**Weight resolution happens on first use of the model tier, never at install.** Wheels can't run
post-install hooks. `resolve_weights()` honors `HF_HUB_CACHE` → `HF_HOME` →
`paths.home()/"models"/"hf"` (so a shared HPC cache keeps working). Pin by **commit sha, not tag**,
with a per-file sha256 table.

1. Present and every sha256 matches → load.
2. Absent, **foreground CLI + interactive tty + network reachable** → fetch with a `rich` progress
   bar, verify each hash, then set `HF_HUB_OFFLINE=1` for the rest of the seal.
3. Absent and (non-interactive **or** unreachable **or** `HF_HUB_OFFLINE=1`) → **abort**, naming
   `wfrec deid fetch`. Never silently, never from the daemon.

Air-gap: `wfrec deid fetch --bundle out.tar.zst` on a connected box, `wfrec deid load out.tar.zst`
on the isolated one — verifying every hash *before* placing files and refusing a bundle whose
manifest revision differs from the pinned one.

**int8's cost is measured, not assumed**: both variants registered, nightly scores both, the delta is
a row in `docs/phi-redaction-benchmarking.md`. If it's material on `notes` or `jobs`, fp32 becomes the default.

**CI must never download a weight**: `tests/conftest.py` sets `HF_HUB_OFFLINE=1` and points
`HF_HUB_CACHE` at a `tmp_path_factory` dir, session-scoped and autouse. An accidental fetch then
fails in milliseconds instead of pulling 120 MB, and no test can pollute a developer's real cache.

`wfrec doctor` gains a `deid` row via `_probe_import` (`doctor.py:43`): engine, weights path + hash
status, resolved profile, configured providers with their **resolved egress class**, gate status.

**Nightly** `deid-nightly.yml`: `schedule` + `workflow_dispatch`, 45-min timeout,
`pip install -e ".[dev]"`, `actions/cache` keyed on the pinned revision, then `-m model` plus
`autocab deid eval --engine gliner --json` for both int8 and fp32, uploaded as an artifact.

---

## 7. Frames and video

**Capture-time (`collectors/screen.py::_run_ocr`).**
1. **Redact and join line-by-line** rather than joining then redacting, so each OCR line occupies an
   exactly known `[start, end)` range in the *stored* `ocr_text`. All four inline patterns are
   `\b`-anchored and single-line, so nothing observable changes — but this makes offset→rectangle
   mapping exact instead of approximate.
2. Write `screen/ocr/<name>.boxes.json`: `{v, frame, backend, coords, ocr_image:{w,h},
   frame_image:{w,h}, lines:[{start, end, box, score}]}`. **No text in this file** — that would be an
   unredacted second copy. Offsets and geometry only.

Two coordinate traps, both verified in the installed library:
- RapidOCR entries are `[polygon, text, confidence]`, and `_run_ocr` re-resizes to
  `OCR_MAX_WIDTH = 1600` (`screen.py:301-303`) **before** inference — boxes are in the *downscaled*
  space. Record both `ocr_image` and `frame_image`, scale by `frame_w / ocr_w`.
- `ocrmac` returns `(text, confidence, bbox)` with bbox **normalized to [0,1] in Vision's
  bottom-left-origin space**. Tag `coords: "normalized-bottomleft"` and convert. **Verify the tuple
  order on a real macOS box before shipping** — backwards silently blacks out the wrong rectangles.

**Seal-time (`src/wfrec/frames.py`).** Per `screen.ocr` event: resolve spans on `ocr_text` → select
every box intersecting a span → scale → inflate 2 px → `ImageDraw.rectangle(fill=(0,0,0))` →
re-encode WebP at the same `quality`/`method` → stage. Default `--box-precision line`
(`proportional` narrows by character ratio); per-glyph interpolation inside a line box is guesswork
and over-redacting a line is free.

| Case | `balanced` | `strict` |
|---|---|---|
| `retain_images=False` — frames already deleted after OCR (`screen.py:268-276`) | **Satisfied**, the pixels never persisted. Audit `frames_absent` | same |
| frame present, no boxes sidecar (pre-change session) | **Fail closed: delete the frame.** `--allow-unboxed-frames` keeps it but sets `sealed=partial`, which the export gate rejects | delete all frames + video |
| frame present with boxes, but its `screen.ocr` event is missing | delete (no evidence it was ever screened) | delete |

**Video.** Refactor the ffmpeg call out of `ScreenCollector._build_video` (`screen.py:349`) into
`src/wfrec/video.py::stitch(...)` so the collector and the seal share one implementation. The seal
can't use `self._frames` (gone with the process) — it reconstructs the list from `screen.frame`
events, which carry `ref` and `ts`. If **any** frame was modified or deleted, delete `session.mp4`
and `frames.txt` first, then re-stitch from survivors. **Never leave the pre-seal mp4 in place**; it
is a full-fidelity copy of everything the seal just removed.

**Two limits to state in the docs, not footnote.** Box redaction can only be as good as OCR recall.
And **HIPAA identifier 17 (full-face photographs) is entirely uncovered** — a text de-identifier
does nothing to a face in a video call window; the control is deletion or source gating.
`retain_images=False` is strictly stronger and already implemented — **recommend flipping it to the
default**, treating box redaction as the path for sessions that already have frames on disk.

---

## 8. Fail-closed matrix

| Failure | `regex-only` | `balanced` (default) | `strict` |
|---|---|---|---|
| engine libs not installed | proceed, `assurance: "regex-only"` | **abort**; can't normally happen now they're base deps | abort |
| weights absent, foreground CLI + tty + network | n/a | fetch w/ progress bar, verify every sha256, then `HF_HUB_OFFLINE=1` | same |
| weights absent, non-interactive / no network / `HF_HUB_OFFLINE=1` | n/a | **abort**, naming `wfrec deid fetch` | abort |
| weights sha256 mismatch / license assertion fails | n/a | abort, delete the cached blob | abort |
| OOM during inference | n/a | retry `batch //= 2` → 1, then `max_length=256`, then abort | same |
| offset out of bounds | n/a | **abort the whole seal** | abort |
| LLM unreachable / auth fails | n/a | degrade to local tier, record `llm_degraded`, seal `partial` | abort |
| LLM returns unverifiable `high`-confidence findings | n/a | chunk `needs_review`, seal `partial` | abort |
| LLM JSON unparseable after one retry | n/a | drop that chunk's findings, seal `partial` | abort |
| `schema_mode` degraded below `strict_schema` | n/a | proceed, record the mode | abort |
| proxy env set, endpoint not in `NO_PROXY` | n/a | reclassify to the **proxy's** class; abort if it exceeds the ack | abort |
| plaintext `http://` to non-loopback | n/a | refuse unless `--allow-plaintext-llm` → `partial` | abort |
| DNS resolution fails | n/a | classify `external`; abort unless the ack permits external | abort |
| ack for provider A, invocation names provider B | n/a | **abort** — a BAA names a counterparty | abort |
| frame without boxes sidecar | delete the frame | delete the frame | delete all frames + video |
| sidecar not valid UTF-8 | replace with a removal notice | same | same |
| session still active | refuse (`--force` stops first) | refuse | refuse, no `--force` |
| disk full mid-staging | recovery deletes `.seal/`, session unchanged | same | same |
| any `--allow-*` override | honored | honored | **ignored**; passing it is an error |

The export gates give "fail closed" teeth: an unsealed session cannot be exported or ingested, so a
failed seal blocks the leak instead of permitting it.

---

## 9. Tests

| File | CI? | Asserts |
|---|---|---|
| `test_deid_corpus.py` | yes | schema, `span.text == text[start:end]`, reserved-range conformance, no `@stjude.org`/real hostname/repo-sample strings, per-label coverage floors |
| `test_deid_scorer.py` | yes | exact, clipped (strict miss + partial hit), superset, overlaps, adjacency, empty, non-BMP offsets, CRLF |
| `test_deid_regex_recall.py` | yes | **the gate** — per-label `recall_strict` ≥ `thresholds.json`, over-redaction ≤ ceiling, `must_survive` clean, snapshot matches |
| `test_deid_egress.py` | yes | loopback v4/v6, unix socket, `::ffff:127.0.0.1`, `0.0.0.0`, `[::]`, RFC1918, CGNAT, link-local, public, **mixed A records**, DNS failure, **proxy-env override**, hostname resolving to loopback. Injected resolver, no network |
| `test_deid_gate.py` | yes | all 2⁵ layer combos per egress class; exactly one allows. Plus **per-provider ack isolation**, expiry, `deny_egress_classes` |
| `test_deid_llm.py` | yes | mocked transport × **all four providers** (mirror `test_wfrec_remote.py`'s mocked-SSH idiom): verification ladder, `schema_mode` degradation, fail-closed on unverifiable, no key and no text anywhere under the session dir — parameterized over all four credential shapes |
| `test_deid_weights.py` | yes | constructing `gliner_onnx` with no weights **raises rather than downloads**; `gliner_small`/`medium` Apache-2.0 accepted, **`base`/`multi` CC-BY-NC-4.0 rejected** |
| `test_deid_seal{,_atomicity,_concurrency,_idempotent}.py`, `test_deid_payload_coverage.py` | yes | 3-phase commit; failure injected at each journal state (not by killing a process); the separate-inode lock property; fixpoint reseal; every event type's every string field |
| `test_deid_frames.py` | yes | synthetic WebP with a known box — pixels inside black, outside **byte-identical**; missing sidecar → deleted; `retain_images=False` → no-op |
| `test_deid_audit.py` | yes | every planted PHI value against every byte of `seal.json`, `deid/audit.jsonl` and every staged file → zero occurrences; no `pseudonyms.json` anywhere |
| `test_deid_model_recall.py` | **no** | `@pytest.mark.model`, int8 **and** fp32 |
| `test_deid_live_llm.py` | **no** | `@pytest.mark.live`, per provider |

New `conftest.py` fixtures: `unsealed_session` (plants a **distinct sentinel PHI string in every
string field of one event of every type** in `events.py`), `sealed_session`, `fake_engine` (all seal
mechanics at zero model cost), `fixed_key`, the autouse `HF_HUB_OFFLINE` guard, and a `deid` cache
reset mirroring the `redaction._SHARED = None` pattern at `conftest.py:45`.

**Skip via a `pytest_collection_modifyitems` hook, not `addopts`.** CI runs bare `python -m pytest`
so the workflow needs no edit, and a developer running the model test file directly sees an explicit
skip *reason* ("set `AUTOCAB_DEID_MODEL_TESTS=1`") rather than a silent zero-collected pass — which
is how people conclude a safety test is green when it never ran.

**Local-LLM scorecards are deliberately not a CI job** (which model a box has pulled is unknowable,
load time dominates, results aren't reproducible): `autocab deid eval --engine llm --llm-provider
ollama --llm-model <model>` is a documented manual command contributed by a human PR. **No bot
commits to `main`** — a safety number that updates itself is a safety number nobody reads.

---

## 10. Smoke verification

```bash
# --- 1. GLiNER is packaged: one install, no extra, no flag ---
uv pip install -e '.[dev]'
python -c "import onnxruntime, tokenizers, huggingface_hub"
pytest                                        # incl. egress + weights guards
grep -rn "HF_HUB_OFFLINE" tests/conftest.py   # the guard that keeps CI weightless
autocab deid gen-corpus --seed 1337 --check
autocab deid eval --engine regex --write-scorecard

wfrec deid fetch && wfrec deid verify && wfrec doctor
AUTOCAB_DEID_MODEL_TESTS=1 pytest -m model
autocab deid eval --engine gliner --write-scorecard --report-latency   # int8 vs fp32 delta

wfrec start --title "deid smoke" --watch .
wfrec note "MRN 4419902, Jane Smith, DOB 2012-06-01, /data/proj/smith_jane_R1.fastq.gz"
wfrec stop && wfrec seal --status <id>
grep -rc "4419902\|Jane Smith\|smith_jane" ~/.wfrec/sessions/<id>/   # expect 0 EVERYWHERE
jq . ~/.wfrec/sessions/<id>/seal.json
wfrec seal <id>            # no-op
wfrec seal <id> --reseal   # gen-2, existing surrogates intact
wfrec pull <id>            # must raise SessionSealed
autocab demo --input-mode session --session-dir ~/.wfrec/sessions/<id>

# --- 2. designate a local LLM: no egress, no extra, no ack ---
wfrec deid providers
wfrec seal <id> --reseal --engine gliner+llm --llm-provider ollama \
    --llm-base-url http://localhost:11434 --llm-model <model>
jq '.engines, .egress_class' ~/.wfrec/sessions/<id>/seal.json         # expect "none"

# the bypass that MUST fail -- the single most important check in this file
ALL_PROXY=http://proxy.example:3128 wfrec seal <id> --reseal --engine llm \
    --llm-provider ollama --llm-base-url http://localhost:11434 --llm-model <model>
# expect: refusal naming the PROXY's egress class, not loopback's

# a non-loopback base_url must NOT inherit loopback's exemption
wfrec seal <id> --reseal --engine llm --llm-provider openai \
    --llm-base-url http://10.0.0.7:8000/v1 --llm-model <model>
# expect: refusal -- class `internal`, needs an ack with internal_endpoints_approved

# --- 3. a cloud provider, fully gated ---
uv pip install -e '.[dev,deid-openai]'
wfrec seal <id> --reseal --engine gliner+llm --llm-provider openai --llm-model <model>
# expect: EVERY unsatisfied gate layer listed at once (non-short-circuiting)

wfrec seal <id> --reseal --llm-provider anthropic --llm-model claude-sonnet-5 ...
# with only deid-llm-openai.json present -> expect abort: an ack names a counterparty

grep -rc "4419902\|Jane Smith\|sk-ant-\|sk-proj-\|AIza" \
    ~/.wfrec/sessions/<id>/ ~/.wfrec/daemon.log   # expect 0 EVERYWHERE
```

**Easiest to get wrong, in order:** the proxy reclassification; mixed A records (and DNS failure
treated as anything but `external`); per-provider ack isolation; `HF_HUB_OFFLINE=1` in
`conftest.py`; the recursive `grep -rc` sweep; crash recovery from each journal state; and
`test_deid_scorer.py`'s clipped-span case — the difference between measuring leaks and hiding them
behind partial credit.

---

## 11. Out of scope

- **Bioinformatics paths, filenames and sample IDs** — scoped out by decision, though in this domain
  it is probably the dominant leak vector. Step 7 stops these surfaces being written raw, and the
  corpus *labels* them with explicit low floors (`SAMPLE_ID 0.60`, `SLURM_JOB_NAME 0.50`) so the gap
  is measured rather than assumed. GLiNER's zero-shot labels are the cheap way to close it later — a
  `labels.py` change, not a retrain.
- **HIPAA identifier 17 (photographs)** — uncovered by construction; the control is
  `retain_images=False` or source gating, not redaction.
- Screen-capture app denylisting with auto-pause, encryption at rest, GUI panic-pause — already
  future work in `plan.md` §9.
- Re-identification support — excluded by design, not schedule.
- IRB documentation and a security review. Nothing here replaces them. What this delivers is the
  framework plus a measured recall number: a real control with known limits, not a safety guarantee.
  The step 11 docs must say exactly that.
