# Package wfrec with a local PHI de-identification model

*Design plan, drafted 2026-09-17. Companion to [`plan.md`](plan.md); this covers challenge
extension (c), "harden the privacy and redaction layer for a pediatric hospital
environment", recorded in `plan.md` §9 as a scored deliverable.*

---

## Context

`plan.md` §9 records extension **(c)** as a *scored* deliverable. Today that layer is four
regexes.

`src/autocab/framework/components.py:98` `SensitiveDataRedactor` catches `email`,
`SJ[-_ ]?\d{4,8}`, `MRN[: ]?\d{6,10}`, `DOB[: ]?\d{4}-\d{2}-\d{2}`, plus a substring deny
list. Measured against it during planning:

```
in:  "sample 4871293 for Jane Smith, 555-867-5309, 1234 Oak St, Memphis TN 38105"
out: "sample 4871293 for Jane Smith, 555-867-5309, 1234 Oak St, Memphis TN 38105"
     findings: []          # name, phone, street, ZIP, bare MRN — all untouched
```

Four more defects, all confirmed against the code:

- **Deny terms are unescaped and unbounded.** `re.sub(token, ...)` at `components.py:128`
  turns `outpatients_cohort.tsv` into `out[REDACTED_TERM]s_cohort.tsv`; a term containing `(`
  raises at redaction time. Any tool `--help` text containing "patient" is mangled.
- **Three whole surfaces have no inline redaction at all** — `files/diffs/*.patch`
  (`files.py:491`), `shell/remote/*` (`remote.py:131`), and `jobs/*.out` (`remote.py:198`)
  are written verbatim. For these the seal pass is not a recall booster, it is the **only**
  control.
- **A stopped session can still be appended to.** `wfrec pull`, `attach-transcript` and
  `merge` all write to the timeline after `stop`, so anything scrubbing at stop time can be
  followed by fresh unscrubbed text.
- **The existing API destroys offsets.** `redact_text` returns a rewritten string plus
  `sorted(set(rule_names))` — no spans, no counts. Pseudonymization, image blackboxing and
  recall scoring all require character offsets, so a span-returning API is a hard
  prerequisite for everything else here.

So the honest status quo is what `src/wfrec/README.md:263` already says: *"scrubbing, **not a
PHI control**."*

### Decisions taken (do not re-open)

| # | Decision |
|---|---|
| 1 | **Two-stage.** Fast regex stays inline at capture. A model **seal** pass runs over a whole session at stop / before export. |
| 2 | **Heavy local model is acceptable** (500 MB–1 GB, torch/transformers) as an optional extra — superseded for GLiNER by decision 7, which makes it a *base* dependency. |
| 3 | **Destructive in-place rewrite** on seal, so the scrubbed form is the only form on disk. |
| 4 | **Consistent pseudonyms** (`NAME_a1b2c3`) via HMAC, key discarded — not `[REDACTED_X]`. |
| 5 | **Blackbox PHI regions** in `screen/frames/*.webp` and re-stitch the video. |
| 6 | Bar is an **auditable de-identification control**: fail-closed, measured recall in CI, audit trail. |
| 7 | **GLiNER is packaged with the application** — base dependency, default engine, weights resolved on first use. Not vendored in git. |
| 8 | **Four LLM providers day one**: Anthropic, OpenAI-compatible (Codex, vLLM, LM Studio, Azure, gateways), Ollama native, Google Gemini/Vertex. All **OFF** by default. |
| 9 | **The egress gate keys on the resolved endpoint address**, not the provider label. Loopback is not a disclosure; a hostname pointing at a shared GPU node is. |

Decision 7 is what separates "the default engine" from "the engine you get". `pland2.md`
previously had GLiNER behind a `[deid-local]` extra and on only *"when weights present"*, which
means a plain `pip install -e .` produced the regex floor and nothing else. Weights are not
committed: there is no `git lfs` here, `.git` is 2.3 MB, and the largest tracked file is 296 KB,
so a 60–150 MB blob would be paid by every clone and every CI checkout, forever, and could not
be removed later without a history rewrite.

Decision 9 is the load-bearing security change. `http://localhost:11434` and
`http://gpu-node-04.hospital.internal:11434` are both "Ollama", but only one of them is a
disclosure. Gating on the provider *name* would either require a BAA attestation to talk to
loopback — which gets the feature switched off — or let a `base_url` pointed at a shared
machine inherit loopback's exemption, which is a silent PHI disclosure.

Decision 3 is load-bearing: `src/autocab/session_bundle.py:41` `_trace_from_session_dir`
**rebuilds traces live from `events.jsonl`** and only falls back to a written
`exports/trace.json` if `wfrec` is unimportable. Scrubbing only `exports/` would be silently
bypassed by AutoCAB's own preferred ingest path.

---

## Architecture

New subpackage **`src/autocab/deid/`** — not a new top-level package and **not a third
console script**. The repo has exactly two entry points with clean ownership (`autocab` =
pipeline and governance, `wfrec` = recorder); measurement is a pipeline concern and sealing is
a session concern, so the verbs split along the existing seam:

```
autocab deid eval --engine regex|gliner|torch|presidio|llm [--llm-provider ...]
                  [--difficulty ...] [--write-scorecard] [--report-latency]
autocab deid gen-corpus --seed 1337 --check
wfrec seal [id] [--engine gliner|gliner+llm|llm|torch] [--reseal] [--force] [--status] [--dry-run]
                [--llm-provider anthropic|openai|ollama|google] [--llm-base-url ...] [--llm-model ...]
wfrec deid fetch | load | verify     # weights: download, air-gap import, hash check
wfrec deid providers                 # provider, endpoint, egress class, ack status, key present
```

This also keeps the existing guarded `wfrec → autocab` import direction exactly as it is
today (`wfrec/redaction.py:38`), rather than introducing a third package both must reach.

```
src/autocab/deid/
  labels.py        THE taxonomy — HIPAA 1-18 + bio shapes. One source of truth.
  spans.py         Span dataclass, merge / normalize / containment
  policy.py        label -> Action, profiles, render mode
  allowlist.py     KEEP set (bioinformatics false positives)
  pseudonym.py     Pseudonymizer (HMAC, ephemeral key)
  llm_gate.py      the five-layer egress gate, pure logic, no SDK  (was api_gate.py)
  egress.py        endpoint -> EgressClass: none | internal | external. stdlib only, no SDK.
  models.py        ModelSpec: repo, pinned revision, per-file sha256, license assertion
  compat.py        RedactionReport / wfrec Redacted adapters
  engines/         base, regex_rules, surrogate_guard, gliner_onnx,
                   transformers_ner, presidio_engine, llm_findings, registry
  providers/       base, anthropic_msgs, openai_compat, ollama_native,
                   google_genai, registry
  eval/            corpus, scorer, report, generate
```

`labels.py` is shared by the regex tier, the zero-shot label prompt, the LLM response schema
enum, and the corpus validator. One source of truth or those four drift apart.

`engines/` and `providers/` are a deliberate split, not layering for its own sake. **Every
safety-relevant decision lives in `engines/llm_findings.py`** — chunking, the prompt, the
response schema, the verification ladder, the independent post-check, the audit. A provider's
entire job is *text in, findings out*; it cannot weaken verification because it never runs it.
That is what makes shipping four transports tolerable: a neglected provider degrades to *fewer
verified findings*, never to a weaker control.

### Why a new package, and how the "one redactor" rule survives

The repo has a hard-won rule (`src/wfrec/redaction.py:5`): *"two redactors in one repo is how
a term ends up scrubbed on one path and leaked on another."* So **invert the dependency
rather than fork it**:

1. Add `engines/regex_rules.py::find_spans(text) -> list[Span]` holding the pattern table.
2. Reimplement `SensitiveDataRedactor.redact_text` **on top of** `find_spans`, preserving its
   exact return type, inside a `try/except ImportError` that falls back to the current
   literals so `autocab` stays installable without the subpackage.

One pattern set, one detector, spans available to everything that needs them.

### Two render policies over one detector — the exact compatibility contract

Decision 4 cannot apply everywhere, because existing tests pin the output format. Checked
against the suite:

- `tests/test_pipeline.py:28,67,68` and `tests/test_redaction.py:9-11` assert the literal
  `[REDACTED_EMAIL]` / `[REDACTED_MRN]` / `[REDACTED_DOB]`. Both exercise the **autocab** path
  (`framework/pipeline.py:97`, fed from `data/sample_*.json`).
- `tests/test_wfrec_session.py:198`, `test_wfrec_collectors.py:92,149`,
  `test_wfrec_agents.py:397`, `test_wfrec_cli_api.py:142` assert only on **finding labels** —
  `{"email","sj_id","mrn"}` — never on replacement text.

| Path | Mode | Output |
|---|---|---|
| `SensitiveDataRedactor` / AutoCAB cluster redaction | `mask` | `[REDACTED_<LABEL>]` — unchanged |
| `wfrec` inline capture | `mask` | `[REDACTED_<LABEL>]` — unchanged |
| The seal pass | `pseudonymize` | `NAME_a1b2c3` |

All ten assertions pass **unmodified** — that is the compatibility gate. One hard constraint:
the legacy finding names `email`, `sj_id`, `mrn`, `dob` must survive as-is even as `labels.py`
grows, so `compat.py` needs an explicit label → finding-name map rather than lowercasing.

**Only the seal pseudonymizes, and that is deliberate.** Pseudonyms require a key that is
stable across hours and across processes (the daemon plus `wfrec --no-daemon` direct mode,
across restarts), which an in-memory-only key cannot provide; the alternative — a key file
next to the data — recreates the re-identification key that discarding it exists to prevent.
The seal sees the whole session in one process, so it produces fully consistent surrogates
with a key that never touches disk. Spans already masked at capture arrive as
`[REDACTED_MRN]`, are protected by `SurrogateGuard`, and are recorded in the audit as
masked-at-capture rather than pseudonymized.

### Core types and overlap resolution

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

`detect` is **batch-shaped by contract**, so transformer batching is a detector-internal
detail and no call site can invoke a model once per string across 30k events.
`find_spans` implements it as a trivial loop.

`spans.resolve()` — union for recall, hull for safety, total order for determinism:
clip and drop empty → **drop** (not clip) any candidate intersecting a `protect` span → drop
allowlisted surfaces → sort by `(start, -end, LABEL_RANK, DETECTOR_RANK, label, detector)`
and sweep, absorbing overlaps into the **outer hull**. `LABEL_RANK` puts specific identifiers
first (`SSN < MRN < SUBJECT_ID < ACCESSION < PHONE < EMAIL < DEVICE < DATE < NAME < AGE <
LOCATION < ORG < DENY`); `DETECTOR_RANK` is `regex < model < api` so regex wins ties, its
label being structurally certain. Hull rather than split because splitting nested spans emits
`NAME_ab12 MRN_cd34` from one string, which is noisier *and* leaks structure. Replacement
applies right-to-left so earlier offsets stay valid.

### Layered engines

| Layer | Dependency | Default | Role |
|---|---|---|---|
| `surrogate_guard` | stdlib | **always first** | `protect` spans over already-sealed text — this is what makes sealing idempotent |
| `regex_rules` | stdlib | **always on** | The floor; the only layer that runs inline at capture |
| `gliner_onnx` | **base** | **always on**, weights auto-resolved | **The default model tier.** Names, addresses, contextual dates, and the bio shapes |
| `transformers_ner` | `[deid-torch]` | opt-in | Highest published clinical recall; the fallback |
| `presidio_engine` | `[deid-presidio]` | opt-in | Validators adopted; framework declined |
| `llm_findings` | provider extra | **OFF** | Opt-in, additive only, egress-classified. Any of four providers, local or cloud |

**`surrogate_guard` and `regex_rules` are not substitutable by any flag.** `--engine llm` swaps
out the *model* tier, never the floor: the CI-measured regex baseline is the thing that makes a
failed, unreachable or lying LLM survivable rather than catastrophic.

An unavailable engine reports `unavailable: <reason>` and the **seal fails closed** — it does
not silently fall back to regex-only. `Redacted.available`'s degraded mode is right for
*capture* (record the fact, keep capturing) and wrong for a *seal*: a seal that quietly
downgrades is a seal you cannot audit.

---

## The seal pass — `src/wfrec/seal.py`

Orchestration and I/O in `wfrec`; detection in `autocab.deid`.

### Coverage

Every text-bearing surface, with the three unredacted ones called out because for them this is
the only control: `events.jsonl` payloads, `context/*.md`, `screen/ocr/*.txt`,
**`files/diffs/*.patch`**, **`shell/remote/*`**, **`jobs/*.out`**, and `screen/frames/*.webp`.

**Schema drift — invert the rule.** A fixed field map (`context.note→payload.text`, …) means
any new event type or payload key silently bypasses the seal: leak-by-default. Instead
**recursively walk every payload and scrub every `str` value**, with an explicit `SKIP_KEYS`
set of structural keys (`seq`, `ts`, `type`, `source`, `backend`, `exit_code`, `duration_ms`,
`chars`, `width`, `height`, `sha`, `pid`, `prompt_seq`, `job_id`, …). Adding an event type can
then only over-scrub, never under-scrub.

Paths (`path`, `cwd`, `root`, `workdir`) are scrubbed **component-wise**, keeping separators
so pipeline shape stays legible: `/data/proj/SJALL018/smith_jane_R1.fastq.gz` →
`/data/proj/SUBJ_9c2f1a/NAME_4d81b2_R1.fastq.gz`.

**`analyst` and `host` are KEEP.** They identify workforce members, not the subject of the
PHI; pseudonymizing `analyst` would break `WorkflowClusterer`'s per-analyst grouping, and a
discarded key makes cross-session analyst identity impossible anyway. Overridable with
`--pseudonymize-analyst`. Document the reasoning inline — this gets second-guessed in review.

### Preconditions

Refuse unless the session is genuinely idle, checked three ways because any one can be stale:
`RecorderState.load().active_session`, `read_sentinel()`, and `manifest.status`.
`wfrec seal --force` stops the session first, then seals. **Refuse, don't race** — the
alternative is holding `.events.lock` across minutes of inference, stalling every collector
thread in the daemon.

### The locking property the whole design rests on

`EventWriter.append` (`events.py:201`) opens `events.jsonl` with `"a"` **inside**
`file_lock(.events.lock)`, and the lock file is a **separate inode** from the data file. So
`os.replace(tmp, events.jsonl)` while holding the lock is safe: a blocked appender re-opens by
path once the lock releases and lands on the new file. No appender holds an fd across the
swap. This is load-bearing and invisible, so `tests/test_deid_seal_concurrency.py` must assert
it directly — a future refactor to a long-lived append fd would break the seal silently.

### Three phases; only the third takes `.events.lock`

| Phase | Lock | Duration | Work |
|---|---|---|---|
| 1 snapshot | `.events.lock` | ms | read timeline, record `snapshot_bytes = st_size`, `sealed_through_seq` |
| 2 scrub | `.seal/lock` only | seconds–minutes | detect, pseudonymize, stage every replacement |
| 3 commit | `.events.lock` | ms | verify `st_size == snapshot_bytes`, then `os.replace` each staged file |

A phase-3 size mismatch means a straggler appended after the snapshot: read the tail beyond
`snapshot_bytes`, scrub it synchronously, append to the staged file, retry — bounded at 3
retries or 200 tail events, then abort with nothing committed.

### Crash safety — staging plus resumable forward commit

`<session>/.seal/{lock, journal.json, audit.jsonl, staged/<relpath>}`

| Journal state | Recovery |
|---|---|
| `planning`, `staging` | delete `.seal/`; session **unchanged** |
| `committing` | **roll forward**: replay `os.replace` for every file still under `staged/` |
| `committed` | write `seal.json`, delete `staged/`, done |

The commit loop is naturally idempotent because **the disappearance of a staged file *is* its
per-file commit record** — `os.replace` moves it out of `staged/`, so a replayed loop skips
it. Hence every file must be staged before any file is replaced.

Cross-file atomicity is unachievable on POSIX (`os.replace` is per-file). What this gives is
the *observable* equivalent: `recover(session_dir)` runs at the top of `wfrec seal`,
`export_session`, `session_bundle._trace_from_session_dir` and `EventWriter.__init__`, and
while a journal sits in `committing` those readers refuse with `SealInProgress`. A mixed state
can exist on disk for milliseconds but is never *consumed*.

### Post-seal immutability

`EventWriter.__init__` stats `<session>/seal.json` once and sets `self._sealed`;
`append`/`extend` then raise `SessionSealed` (with a bypass the seal itself uses). One stat
per `Session` construction, zero cost on the hot path. This is what closes the
`wfrec pull` / `attach-transcript` / `merge` hole.

### Idempotency — by grammar, not by key

- **Reserved namespace.** `SurrogateGuard` runs first and emits `protect=True` spans matching
  `\b(?:NAME|MRN|SUBJ|SSN|PHONE|EMAIL|ADDR|ORG|DEV|ACC|ID)_[0-9a-f]{6,}\b`, plus
  `\[REDACTED_[A-Z_]+\]` and `\[DATE:\d{4}\]`. `resolve()` drops every candidate intersecting
  a protect span, so **sealed text is a fixpoint** — `NAME_a1b2c3` can never become
  `NAME_9f04de`. This is the mechanism that actually matters; the key being discarded is
  irrelevant to it.
- **Marker.** `seal.json` present → `wfrec seal` is a no-op returning the existing record;
  `--reseal` required. A reseal with a better engine mints generation-2 surrogates only for
  PHI the first pass **missed**, recorded as `generation: 2` with a per-generation engine
  list. Correct semantics: the surrogate namespace is per-generation-of-discovery.

### Hook points

| Caller | Behaviour |
|---|---|
| `wfrec seal [id]` | Primary and recovery entry point. Runs in the **foreground CLI process, never the daemon** — the daemon is long-lived, inherits whoever's environment started it, and writes `daemon.log`; three reasons to keep credentials and PHI-bearing inference out of it. |
| `wfrec stop` (`recorder.py:205`) | Prints the seal command and, in `balanced`, spawns a detached `python -m wfrec seal <id>` reusing the `_spawn_daemon` pattern (`cli.py:332`). Model load is seconds and inference minutes; blocking the interactive path by default is how a safety feature gets switched off. Safe because the gates below block consumption. `strict` seals synchronously with a `rich` progress bar. |
| `export_session` (`exporters/__init__.py:30`) | **Hard gate** — `NotSealed` unless `seal.json` exists. |
| `session_bundle._trace_from_session_dir` (`session_bundle.py:41`) | **Second hard gate** — this rebuilds from `events.jsonl` live, so gating `exports/` alone is bypassable. Guard the timeline, not the projection. |
| `POST /sessions/seal` | Mirrors `/export` so the GUI and recorder skill can drive it. |

### Batching

Collect into `Item(target_id, field_path, text)` → **dedupe by exact string** (consecutive OCR
frames share most text, window titles repeat constantly; expect a few thousand unique strings
from tens of thousands of events) → regex stage → model stage → **cache the rewritten
string**, not the spans, since pseudonymization is a pure function of
`(label, normalized_surface)` within a run.

For the transformer path use the **explicit tokenizer + model** with `offset_mapping`,
`return_overflowing_tokens=True`, `stride=128`, `max_length=512` — which gives the sliding
window over long OCR dumps for free, with `overflow_to_sample_mapping` for the window→string
mapping. **Not** `pipeline(aggregation_strategy=...)`: its char offsets are unreliable across
subword merges on byte-level BPE, and exact offsets are non-negotiable when they drive both
text replacement and image blackboxing. An offset outside `[0, len(text)]` **aborts the whole
seal** — a wrong-region replacement looks like success, which is worse than no seal.

---

## Pseudonymization — `src/autocab/deid/pseudonym.py`

`surrogate = f"{PREFIX[label]}_{hmac_sha256(key, label + b'\0' + norm)[:6].hex()}"` — 12 hex
chars (48 bits), negligible collision probability at any session size.

| Class | Normalization |
|---|---|
| `MRN, SUBJECT_ID, SSN, PHONE, ACCESSION, DEVICE` | strip captured label prefix, drop non-alphanumerics, uppercase — `MRN: 123-456`, `mrn 123456`, `MRN123456` collide to one surrogate |
| `EMAIL` | `casefold()` the whole address (no local-part dot-stripping; that is Gmail-specific) |
| `NAME` | NFKC, casefold, collapse whitespace, strip titles (`Dr.`, `MD`, `PhD`). Middle initials kept — different people. |
| other | NFKC + casefold + whitespace collapse |

Including `label` in the HMAC input stops a value that is both a plausible MRN and a plausible
account number from collapsing across labels. Honest limitation: `"Smith"` and `"John Smith"`
get different surrogates; heuristic coreference would cause worse errors than it prevents.

**Dates are generalized, not pseudonymized.** Safe Harbor removes date elements finer than
year: `2012-06-01` → `[DATE:2012]`, ages `> 89` → `AGE_90_PLUS` (`<= 89` kept).
Pseudonymizing dates would destroy the timeline ordering the recorder exists to capture, and
is not what the standard asks for.

**Key lifecycle.** `secrets.token_bytes(32)` at seal start, in a `bytearray`, overwritten in
place and cleared in a `finally`. `__repr__` returns `"Pseudonymizer(key=<discarded>)"`; the
field is excluded from serialization. Say plainly in the docstring that this is best-effort —
Python cannot guarantee no copy survives GC; the real guarantee is that the key never leaves
the process.

**No `pseudonyms.json`.** A reverse map beside the de-identified data is exactly the "code or
other means of re-identification" that 45 CFR 164.514(b)(2)(ii) forbids retaining under Safe
Harbor, and co-locating it in `<session>/` makes the directory re-identifiable from one `ls`.
No permission bit fixes that. If the org genuinely needs a limited data set, that is a
separate explicit flag (`--profile limited-dataset --map-out <path outside ~/.wfrec>
--map-recipient <pubkey>`) that encrypts the map to a public key **the sealing machine does not
hold the private half of**, refuses a path under `paths.home()`, and stamps
`profile: "limited-dataset"` so downstream consumers see the session is not Safe Harbor.

---

## Precision — the thing most likely to get this switched off

A clinical de-ID model will confidently label `HG008`, `NA12878`, `samtools`,
`chr7:117559590`, `ENSG00000001626` and `GRCh38` as identifiers, names or locations. Note
`PipelineConfig.benchmark_dataset = "GIAB HG008"` (`framework/config.py:25`) — the model would
redact the repo's own benchmark name. Left alone this shreds sessions into unreadability and
the feature gets disabled, which is a worse privacy outcome than a slightly leakier one.

`allowlist.py` ships a default KEEP set (GIAB sample names, reference builds, common tool
names, HGNC symbols, HGVS `c.`/`p.` notation, `chrN:pos`, accessions like `SRR12345678`,
barcodes like `ATCACG`/`SI-GA-A1`, container tags with date-like versions), applied after
`resolve()` on the normalized surface and extensible via `~/.wfrec/deid-allowlist.txt`.
`SJ-1234` is deliberately **not** allowlisted — it is a real institutional identifier.

**Adopt Presidio's checksum validators, not its framework.** Presidio is MIT, so
reimplementing its SSN / NPI / card checksums and its phone / URL / IP patterns is both legal
and probably the highest-value hour in this plan — checksum validation is the cheapest
precision win available and our regex tier has none. What we decline is the
`en_core_web_lg` install shape: it is not on PyPI, it installs from a GitHub release URL via
`spacy download` (breaking air-gapped and proxied installs), and spaCy's `thinc`/`numpy` pins
fight other dependencies — precisely the `libGL.so.1` class of fragility `pyproject.toml`
already has a paragraph about. Available behind `[deid-presidio]` if the team wants it; let
the harness settle it.

---

## Fail-closed semantics

| Failure | `regex-only` | `balanced` (default) | `strict` |
|---|---|---|---|
| engine libs not installed | proceed, `assurance: "regex-only"` | **abort**; cannot normally happen, since decision 7 puts them in base deps | abort |
| **weights absent**, foreground CLI + interactive tty + network | n/a | fetch with a `rich` progress bar, verify every sha256, then `HF_HUB_OFFLINE=1` | same |
| **weights absent**, non-interactive / no network / `HF_HUB_OFFLINE=1` already set | n/a | **abort**, naming `wfrec deid fetch`. Never fetch silently, never from the daemon | abort |
| weights sha256 mismatch / license assertion fails | n/a | abort, delete the cached blob | abort |
| OOM during inference | n/a | retry `batch //= 2` → 1, then `max_length=256`, then abort | same |
| offset out of bounds | n/a | **abort the whole seal** | abort |
| LLM endpoint unreachable / auth fails | n/a | degrade to local tier, record `llm_degraded`, seal `partial` | abort |
| LLM returns unverifiable `high`-confidence findings | n/a | chunk `needs_review`, seal downgraded to `partial` | abort |
| LLM JSON unparseable after one retry | n/a | drop that chunk's findings, seal `partial` | abort |
| `schema_mode` degraded below `strict_schema` | n/a | proceed, record the mode in the audit | abort |
| **proxy env set** and endpoint not matched by `NO_PROXY` | n/a | reclassify to the **proxy's** egress class; abort if that exceeds the ack | abort |
| plaintext `http://` to a non-loopback endpoint | n/a | refuse unless `--allow-plaintext-llm`, which stamps `partial` | abort |
| DNS resolution fails for the endpoint | n/a | classify `external`; abort unless the ack permits external | abort |
| ack exists for provider A, invocation names provider B | n/a | **abort** — a BAA names a counterparty | abort |
| frame without boxes sidecar | delete the frame | delete the frame | delete all frames + video |
| sidecar not valid UTF-8 | replace with a removal notice | same | same |
| session still active | refuse (`--force` stops first) | refuse | refuse, no `--force` |
| disk full mid-staging | recovery deletes `.seal/`, session unchanged | same | same |
| any `--allow-*` override | honored | honored | **ignored**; passing it is an error |

"Fail closed" means *refuse to produce a sealed artifact*, not "silently degrade". The export
gates give that teeth: an unsealed session cannot be exported or ingested, so a failed seal
blocks the leak instead of permitting it. Crucially, a seal that degrades is stamped
`sealed=partial`, **never** `sealed=verified` — silently stamping a garbled run as sealed is
the worst outcome available in this design space.

### Audit trail — no PHI, and no brute-forceable hash

`<session>/deid/audit.jsonl`, one record per finding:

```json
{"target":"events.jsonl","seq":1841,"field":"payload.ocr_text","label":"MRN",
 "engine":"gliner_onnx","score":0.99,"start":412,"end":421,
 "action":"pseudonymize","surrogate":"MRN_a7f3c1","value_id":17}
```

**Do not write `sha256(surface)`.** A 6-digit MRN spans 10⁶ candidates — a plain hash is
brute-forced in milliseconds, making the audit file a re-identification oracle, and a keyed
hash only moves the problem to key handling. Instead `value_id`: a monotonic integer per
distinct `(label, normalized_surface)`. It delivers every analytic the audit exists for —
distinct-value counts, repeat rates, which value recurs where — with zero recoverable
content, and it dies with the run. Also omit `len` for `NAME`; a 4-character surname is a
meaningful hint.

`<session>/seal.json` is the marker and integrity chain: profile, assurance, generation,
engine list with `patterns_sha256` and each model's pinned revision + `weights_sha256`,
`sealed_through_seq`, `counts_by_label`, `distinct_values`, `collisions`, per-target
`before_sha256`/`after_sha256`, frame and video stats, `pseudonym_key: "discarded"`,
`reverse_map: "not-written"`, duration. Whole-file hashes of high-entropy files are not
brute-forceable and are what prove what was replaced.

The seal appends one `deid.sealed` event as the **last line of the staged `events.jsonl`,
before hashing**, so the timeline self-documents and `after_sha256` covers it. Add
`DEID_SEALED = "deid.sealed"` to `events.py` and skip it in `exporters/trace.py`.

---

## Frames and video

### Capture-time change (`collectors/screen.py::_run_ocr`)

1. **Redact and join line-by-line** rather than joining then redacting, so each OCR line
   occupies an exactly known `[start, end)` range in the *stored* `ocr_text`. All four inline
   patterns are `\b`-anchored and single-line, so nothing observable changes — but this is
   what makes offset → rectangle mapping exact instead of approximate.
2. Write `screen/ocr/<name>.boxes.json`: `{v, frame, backend, coords, ocr_image:{w,h},
   frame_image:{w,h}, lines:[{start, end, box, score}]}`. **No text in this file** — that
   would be an unredacted second copy. Offsets and geometry only.

Two coordinate traps, both verified in the installed library:

- RapidOCR entries are `[polygon, text, confidence]`, and `_run_ocr` re-resizes to
  `OCR_MAX_WIDTH = 1600` (`screen.py:301-303`) **before** inference — so boxes are in the
  *downscaled* space. Record both `ocr_image` and `frame_image` and scale by
  `frame_w / ocr_w`. This version also supports `return_word_box=True` for word-level
  precision later.
- `ocrmac` returns `(text, confidence, bbox)` with bbox **normalized to [0,1] in Vision's
  bottom-left-origin space**. Tag `coords: "normalized-bottomleft"` and convert. Verify the
  tuple order on a real macOS box before shipping — getting it backwards silently blacks out
  the wrong rectangles.

### Seal-time redaction (`src/wfrec/frames.py`)

Per `screen.ocr` event: resolve spans on `ocr_text` → select every box intersecting a span →
scale → inflate 2 px → `ImageDraw.rectangle(fill=(0,0,0))` → re-encode WebP at the same
`quality`/`method` → stage. Default `--box-precision line`; `proportional` narrows by
character ratio. Default to `line` — per-glyph interpolation inside a line box is guesswork
and over-redacting a line is free.

| Case | `balanced` | `strict` |
|---|---|---|
| `retain_images=False` — frames already deleted after OCR (`screen.py:268-276`) | **Satisfied**; the pixels never persisted. Audit `frames_absent`. | same |
| frame present, no boxes sidecar (pre-change session) | **Fail closed: delete the frame.** `--allow-unboxed-frames` keeps it but sets `sealed=partial`, which the export gate rejects. | delete all frames + video |
| frame present with boxes, but its `screen.ocr` event is missing | delete (no evidence it was ever screened) | delete |

**Video.** Refactor the ffmpeg call out of `ScreenCollector._build_video` (`screen.py:349`)
into `src/wfrec/video.py::stitch(...)` so the collector and the seal share one
implementation. The seal cannot use `self._frames` (gone with the process) — it reconstructs
the list from `screen.frame` events, which carry `ref` and `ts`. If **any** frame was modified
or deleted, delete `session.mp4` and `frames.txt` first, then re-stitch from survivors. Never
leave the pre-seal mp4 in place; it is a full-fidelity copy of everything the seal just
removed.

**Two limits that must be stated out loud in the docs, not footnoted.** Box redaction can only
be as good as OCR recall, so a frame whose text OCR missed keeps its pixels. And **HIPAA
identifier 17 (full-face photographs) is entirely uncovered** — frames are images, and a text
de-identifier does nothing to a face in a video call window. The control for that is deletion
or source gating, not redaction. `retain_images=False` is strictly stronger than box
redaction and is already implemented — **recommend flipping it to the default**, treating box
redaction as the path for sessions that already have frames on disk.

---

## Engine selection

**Primary, default, and packaged with the application: GLiNER small, ONNX —
`gliner_small-v2.1`, int8-quantized.** Per decision 7 its dependencies sit in base
`[project].dependencies`, not an extra, so `pip install -e .` yields a working model tier with
no second install flag and no opt-in switch. A PHI control that requires an extra is a PHI
control that is off on most machines.

| Why | Detail |
|---|---|
| Install cost | `tokenizers` + `huggingface-hub` on top of the `onnxruntime 1.30.0` **already installed transitively via `rapidocr-onnxruntime`** (and `numpy` via opencv) — so promoting this to a *base* dependency costs two small wheels, which is precisely why it can be the packaged default. No torch (~2.5 GB, and no 3.14 wheels, which conflicts with the `requires-python` rationale at `pyproject.toml:7-12`), no spaCy pins, no compiler, no system libs. |
| HPC login nodes | No GPU, no listening server, no system daemon, bounded RAM, `nice`-able. This is what disqualifies Ollama as the **default** — not as an option. |
| Air-gapped | One `.onnx` plus `tokenizer.json`; ships on a USB stick, `HF_HUB_OFFLINE=1` respected. |
| Label mapping | **Zero-shot is the decisive advantage.** Labels are authored in *our* `labels.py` as descriptions — `"medical record number"`, `"SLURM job name"`, `"patient name embedded in a file path"`, `"sequencing sample identifier"`. Every fixed-label clinical model collapses MRN / account / license / device into a single `ID` bucket (fine for safety, poor for pseudonym classing) and has no concept of a SLURM job name. |
| Cost | ~0.1–0.3 s/KB CPU, ~0.2–0.6 GB. |

**License trap to handle explicitly:** `gliner_small-v2.1` and `gliner_medium-v2.1` are
Apache-2.0, but **`gliner_base` and `gliner_multi` are CC-BY-NC-4.0**. A non-commercial
checkpoint inside a hospital tool is a real problem, so `models.py` pins the checkpoint *and*
its revision and asserts the license string in a test.

**Fallback: `obi/deid_roberta_i2b2` behind `[deid-torch]`.** Highest published clinical recall
(~0.97–0.99 token recall on i2b2 2014), MIT, and — not a small thing for an auditable control
— the model a reviewer or IRB recognizes by name. You authorized a heavy install, so this path
is fully supported; it is an extra rather than the default because the inference engine for
GLiNER is already on disk. `StanfordAIMI/stanford-deidentifier-base` (~440 MB, BERT-base) is
the cheaper variant of the same idea.

### Ollama and other local LLMs — designatable, not default

**A correction to an earlier draft of this plan, stated rather than quietly reversed.** The
previous version gave three reasons to disqualify Ollama. Two hold and are retained: ~10–60 s/KB
is one to two orders of magnitude slower than GLiNER, and a *system* install plus a background
server violates the repo's no-system-deps ethos and is unusable on an HPC login node. The third
— *"it hallucinates spans, reintroducing the offset problem the API path is designed around"* —
**does not survive this plan's own design.** The verification ladder below (verbatim substring
plus occurrence index, matched and verified **locally**, unverifiable findings **dropped**)
exists precisely to make an untrusted span source safe, and it is provider-agnostic. A local
LLM inherits that protection for free. So "not the default" was right; "excluded entirely" was
not.

A local LLM is therefore a **first-class designatable engine**, reached through the same
`llm_findings` engine as any cloud provider:

```bash
# Ollama, loopback: additive on top of GLiNER, and no egress gate because there is no egress
wfrec seal <id> --engine gliner+llm --llm-provider ollama \
    --llm-base-url http://localhost:11434 --llm-model <model>

# any OpenAI-compatible local server — vLLM, LM Studio, llama.cpp, an internal gateway
wfrec seal <id> --engine gliner+llm --llm-provider openai \
    --llm-base-url http://localhost:8000/v1 --llm-model <model>

# score whatever you actually have pulled, against the same corpus and the same scorer
autocab deid eval --engine llm --llm-provider ollama --llm-model <model> --report-latency
```

Three rules make this safe rather than merely possible:

- **Additive only.** `--engine gliner+llm` stacks; `--engine llm` substitutes for the *model*
  tier. Neither can remove `regex_rules` or `surrogate_guard`. An unmeasurable engine can
  therefore only raise recall or cost precision — never remove the CI-measured floor.
- **No default model.** `--llm-model` is required for a local provider. Which 8B model someone
  has pulled is unknowable, and guessing wrong burns a 30-second model load to produce garbage.
  `ollama_native.py` reads `/api/tags` so `wfrec deid providers` can list the real options.
- **Loopback is classified, not assumed.** `--llm-base-url` is user-supplied, so pointing it at
  a shared GPU node is egress. See the egress classifier below — this is the whole reason
  decision 9 exists.

**The harness decides, not this document.** Every engine is scored on the same corpus by the
same scorer and `docs/phi-redaction-benchmarking.md` turns "which model" into a number. If GLiNER
underperforms on `notes` and `jobs`, the fallback ships as the default and the plan was still
right, because the plan's actual deliverable is the measurement. Re-verify every license on
the model card at implementation time.

### Weights — packaged means "no second install step", not "committed to git"

Respect the ecosystem standard so a shared HPC cache keeps working: `resolve_weights()` honors
`HF_HUB_CACHE`, then `HF_HOME`, and only then defaults to `paths.home()/"models"/"hf"`. Pin by
**commit sha, not tag**, with a per-file sha256 table; every hash is verified (mismatch aborts
*and* deletes the blob) and `HF_HUB_OFFLINE=1` is then set for the rest of the seal, so a seal
never makes a network call once the weights are in place.

**Resolution happens on first use of the model tier, never at install time.** Wheels cannot run
post-install hooks, and a `pip install` that quietly pulled 120 MB would be hostile anyway:

1. Present and every sha256 matches → load.
2. Absent, **foreground CLI, interactive tty, network reachable** → fetch with a `rich`
   progress bar, verify, proceed.
3. Absent and (non-interactive **or** unreachable **or** `HF_HUB_OFFLINE=1`) → **abort**,
   naming `wfrec deid fetch`. Never fetch silently, and **never from the daemon** — a
   long-lived background process pulling 120 MB is exactly the surprise the "foreground CLI,
   never the daemon" rule exists to prevent.

`wfrec deid fetch --bundle out.tar.zst` produces an air-gap transfer artifact, and its
counterpart **`wfrec deid load out.tar.zst`** imports one on the isolated box, verifying every
hash *before* placing files and refusing a bundle whose manifest revision differs from the
pinned one.

**int8 is the default, and its cost is measured rather than assumed.** The quantized export is
~60–150 MB against fp32's several hundred. Quantization's effect on NER recall is usually small
but it is real, so `models.py` registers **both** variants and the nightly harness scores both —
the int8 delta becomes a row in `docs/phi-redaction-benchmarking.md`, not an act of faith. If the delta is
material on the `notes` or `jobs` channels, fp32 becomes the default. Same rule as above: the
harness decides.

**The honest tension in decision 7.** Making GLiNER the packaged default trades a one-time
network fetch against an air-gap posture the rest of this plan takes seriously. It is mitigated
(`deid fetch --bundle` / `deid load`, hash pinning, `HF_HUB_OFFLINE` afterwards) but not
eliminated. The variant that removes it entirely is publishing a weights wheel — the exact
pattern `pyproject.toml:31` already relies on, where `rapidocr-onnxruntime` is chosen *because*
"ONNX models ship inside the wheel: fully offline". That remains available later without
re-architecting anything, because `resolve_weights()` is the single seam.

**CI must still never download a weight**, and that needs a guard rather than a convention.
`tests/conftest.py` sets `HF_HUB_OFFLINE=1` and points `HF_HUB_CACHE` at a `tmp_path_factory`
directory, session-scoped and autouse. An accidental fetch then fails loudly in milliseconds
instead of pulling 120 MB into the runner, and no test can read or pollute a developer's real
cache. `.github/workflows/tests.yml` still needs no edit.

`wfrec doctor` gains a `deid` row via the existing `_probe_import` helper (`doctor.py:43`)
reporting engine, weights path and hash status, resolved profile, configured providers with
their **resolved egress class**, and gate status — the repo's established answer to "works on
my machine", and the right place for "is the control armed, and could this box send text
off-site right now?"

---

## Validation — what makes "auditable" true

### Corpus — `data/deid-eval/`

```
README.md  thresholds.json  scorecard.regex.json
lexicons/{surnames,given-names,cities,gene-symbols}.txt
corpus/{shell,ocr,notes,agents,diffs,jobs,prescrubbed,negatives}.jsonl
```

**Everything must be `.jsonl`.** `.gitignore` already ignores `*.vcf`, `*.fastq`, `*.bam` and
`*.log`, so a fixture named `sample.vcf` or `slurm-4213.log` would be silently dropped from
the commit. Embed those shapes as *strings inside JSONL*, never as files with those extensions.

Record schema: `{id, channel, difficulty, text, spans:[{start,end,label,text,hipaa}],
must_survive:[...], provenance:"generated:v1:seed=1337:tmpl=...", license:"CC0-1.0"}`.
Offsets are half-open NFC code-point offsets. `span.text` is a **deliberately redundant copy**
of `text[start:end]`, and CI asserts equality for every span in every record — the cheapest
possible defence against offset rot when someone hand-edits a record, and it will fire.

**Two tiers of synthetic safety, both tested.** Intent is not a guarantee:

- **Tier 1 — structural reservation.** Draw only from never-issued ranges, enforced by test:
  `@example.com`/`@example.org` (RFC 2606), `+1-555-01xx` (NANP fictitious block),
  `000-/666-/9xx-` SSNs (never issued), `192.0.2.0/24`, `198.51.100.0/24`, `203.0.113.0/24`
  (RFC 5737), and a documented test-only `44xxxxx` MRN band.
- **Tier 2 — independent-draw provenance** for `NAME`, `LOCATION`, `DATE`, `AGE`, which have
  no reserved space: each field is an independent seeded draw from a committed public-domain
  lexicon, so no *combination* corresponds to a real person. The generator is deterministic
  and network-free; `autocab deid gen-corpus --check` regenerates in memory and diffs against
  the committed corpus.

The README must state the honest limit: Tier 2 gives non-attribution by construction, not the
structural impossibility Tier 1 gives — which is why Tier 1 is used wherever it exists.

**Difficulty stratification is not optional.** Gating on `easy`+`medium` only. Without it the
benchmark is a tautology: the regex tier scores 1.00 on shapes the generator built from the
same regexes. The `hard` tier (OCR garble like `MRN l23456` with an ell for a one, `M.R.N.
123456`, unspaced `MRN:123456`) is reported but gated far lower.

**Gold labelling: minimal spans.** `/data/proj/SJALL018/smith_jane_R1.fastq.gz` gets two
spans (`SJALL018` → `SUBJECT_ID`, `smith_jane` → `NAME`), not one over the whole path —
because redacting the whole path destroys the pipeline shape AutoCAB exists to extract. A
whole-path redaction still earns full recall credit; it just gets charged as over-redaction, so
the tradeoff is visible instead of averaged away.

**`negatives.jsonl` must be adversarial** or precision is meaningless: HGNC symbols that read
as names, HGVS (`c.1521_1523delCTC`, `p.Phe508del`), `chr7:117559590`, `GRCh38`/`hg19`,
`biocontainers/gatk:4.5.0.0--2024-01-15`, `SRR12345678`, `samtools sort -@ 8`, and the repo's
own `GIAB HG008`.

### Metrics

Everything is defined on the **original** text; engines detect spans and rewriting is separate,
which keeps offsets stable and the scorer honest. Let `G` be a gold span's character indices
and `P` the union of *all* predicted spans' indices for that record, **regardless of predicted
label**.

- **`recall_strict`** — gold span counts iff `G ⊆ P`. Full containment. **This is the gate.**
- **`recall_partial`** — counts iff `|G ∩ P| > 0`. Reported for diagnosis, **never gated**.

The gap between them is the most actionable diagnostic in the report: high partial with low
strict means the detector sees the identifier and *clips* it — `[NAME] Smith` — which is a
leak, not a partial success. Any metric awarding fractional credit for a clipped name
misreports a disclosure as a 0.7, which is why token-level F1 from the i2b2 literature is the
wrong headline number here.

**Label is ignored for safety** — what matters is that the characters were removed, not whether
`smith_jane` was called `NAME` or `OTHER_ID`. Label correctness is scored separately as
`label_accuracy` over strictly-recalled spans, because the label picks the pseudonym class: a
mislabel degrades consistency and utility, not safety.

**Utility metrics** (character-level, because span-level precision is ill-defined against
minimal-span gold): `over_redaction_rate`, `false_positive_spans_per_negative_record`, and
`must_survive_violations`. Precision gets a **ceiling, not a floor** — and the ceiling exists
precisely so a `.*` patch cannot game recall to 1.00.

**Two headline numbers for a compliance reader**, because de-identification is judged per
released document, not per token: `safe_record_rate` (fraction of records with zero strict
misses) and `leaks_per_1000_records`. A session is safe only if *every* record in it is clean.

**Why recall, not F1.** The costs are asymmetric and irreversible in one direction. A missed
identifier in a sealed session is an unauthorized disclosure that cannot be undone — and
decision 3 mandates destructive rewrite, so there is no original to go back to. An
over-redaction costs a slightly worse `SKILL.md` draft that a human is already reviewing
(`review_status: pending` is in the pipeline today). Reporting a single F1 lets precision buy
back recall, which is the one trade this control must never silently make.

### CI, within the existing 10-minute budget

CI has no model weights and must download nothing.

| File | CI? | Asserts |
|---|---|---|
| `tests/test_deid_corpus.py` | yes | schema, `span.text == text[start:end]`, reserved-range conformance, no `@stjude.org` / real hostname / repo-sample strings, per-label coverage floors |
| `tests/test_deid_scorer.py` | yes | unit: exact, clipped (strict miss + partial hit), superset (strict hit + precision charge), overlaps, adjacency, empty, non-BMP unicode offsets, CRLF |
| `tests/test_deid_regex_recall.py` | yes | **the gate** — per-label `recall_strict` ≥ `thresholds.json`, over-redaction ≤ ceiling, `must_survive` clean, scorecard snapshot matches |
| `tests/test_deid_egress.py` | yes | **the classifier**: loopback v4/v6, unix socket, `::ffff:127.0.0.1`, `0.0.0.0`, `[::]`, RFC1918, CGNAT, link-local, globally routable, **mixed A records**, DNS failure, **proxy-env override**, hostname resolving to loopback. Resolver injected, so no network |
| `tests/test_deid_gate.py` | yes | all 2⁵ gate combinations per egress class; exactly one allows. Plus **per-provider ack isolation** (an Anthropic ack must not authorize OpenAI), ack expiry, `deny_egress_classes` |
| `tests/test_deid_llm.py` | yes | mocked transport × **all four providers** (mirroring `tests/test_wfrec_remote.py`'s mocked-SSH idiom): the verification ladder, `schema_mode` degradation, fail-closed on unverifiable, and no key and no text anywhere under the session dir — parameterized over all four credential shapes |
| `tests/test_deid_weights.py` | yes | `HF_HUB_OFFLINE=1` is set by conftest; constructing `gliner_onnx` with no weights **raises rather than downloads**; registry license assertions — `gliner_small`/`medium` Apache-2.0 accepted, **`base`/`multi` CC-BY-NC-4.0 rejected** |
| `tests/test_deid_model_recall.py` | **no** | `@pytest.mark.model`, int8 **and** fp32 |
| `tests/test_deid_live_llm.py` | **no** | `@pytest.mark.live`, per provider |

**Skip via a `pytest_collection_modifyitems` hook in `tests/conftest.py`, not `addopts`.** CI
runs bare `python -m pytest` so `.github/workflows/tests.yml` needs no edit, and a developer
running the model test file directly sees an explicit skip *reason* ("set
`AUTOCAB_DEID_MODEL_TESTS=1`") rather than a silent zero-collected pass — which is how people
conclude a safety test is green when it never ran.

**Nightly**: new `.github/workflows/deid-nightly.yml` — `schedule` + `workflow_dispatch`,
45-minute timeout, `pip install -e ".[dev]"` (GLiNER now needs no extra), `actions/cache` keyed
on the pinned model revision so only the first run pays the download, then `-m model` plus
`autocab deid eval --engine gliner --json` for **both int8 and fp32**, uploaded as an artifact.
Nightly rather than per-PR because the model tier's numbers move only when the revision or
corpus moves.

**Local-LLM scorecards are deliberately not a CI job.** Which model a given box has pulled is
unknowable, load time dominates, and results are not reproducible across machines — so
`autocab deid eval --engine llm --llm-provider ollama --llm-model <model>` is a documented
manual command whose output is contributed by a human PR. The "no bot commits to `main`" rule
applies unchanged: a safety number that updates itself is a safety number nobody reads.

### Regression gating — two mechanisms, deliberately different

**(a) Hard floors in `data/deid-eval/thresholds.json`**, not in test source, keyed on a
`corpus_fingerprint`. Starting values for the regex tier:

```
EMAIL/IP/URL/SSN/MRN/DOB_LABELLED/SUBJECT_ID 1.00   PHONE 0.95   SAMPLE_ID 0.60
SLURM_JOB_NAME 0.50   DATE_BARE 0.30   NAME 0.10   LOCATION 0.05   AGE_OVER_89 0.00
overall 0.55   safe_record_rate 0.40   max_over_redaction_rate 0.05   must_survive_violations 0
```

The 1.00 floors are only honest *because* gating is restricted to `easy`+`medium` on
deterministic shapes. The `NAME 0.10` / `LOCATION 0.05` / `AGE_OVER_89 0.00` rows look absurd
and are the most valuable lines in the file: they encode "the regex tier does not detect
names" as a tested, visible, blame-able fact rather than an unstated hope, and they are the
baseline the model tier must beat. Any future PR claiming "improved redaction" has to move
those numbers *in the diff*, where a reviewer sees it. The over-redaction ceiling is the
counterweight that makes the recall floors mean anything — without it `re.compile(r"\S+")`
passes every floor. Model tier: `NAME 0.95`, overall `0.95`, combined `0.97`, set at the low
end of published clinical performance because our text is shell commands and OCR garble, not
narrative. These are **regression gates, not safety claims**, and the docs must say so.

**(b) A committed scorecard snapshot** — `data/deid-eval/scorecard.regex.json`. Floors catch
catastrophic regressions but not a quiet slide from 0.98 to 0.91, so CI asserts freshly
computed regex-tier numbers equal the snapshot; any pattern change fails with a diff naming
the label that moved, and the author must run `--write-scorecard`, putting the delta in the PR
as a reviewable line. The snapshot carries **no** nondeterministic fields — no timestamp, no
hostname, no wall-clock latency — or it churns on every run and gets ignored.

Expect the harness to earn its keep on day one: the current substring deny-term rule will fail
`max_over_redaction_rate` on `negatives.jsonl` as written.

### Reporting

`docs/phi-redaction-benchmarking.md`, generated and committed, headed "do not hand-edit":
`safe_record_rate` and `leaks_per_1000_records` at the top; per-label table
(`recall_strict`, `recall_partial`, the gap, `label_accuracy`, floor, margin) split by
difficulty; a HIPAA 1–18 rollup, which is the language a compliance reviewer reads in; a
**per-channel** table, because `notes` and `jobs` are the high-risk channels and a corpus-wide
average hides them; the utility table; measured CPU latency per KB per engine; and a **full
leak table** listing every strict miss with `record_id`, label and gold span text — safe to
print verbatim because the corpus is synthetic, and the artifact you actually work from.

Plus a mandatory **"What this does not prove"** section: the corpus is synthetic and its easy
tier shares shapes with the regex generator; real OCR garble is worse; HIPAA identifier 17 is
entirely uncovered because frames are images; Safe Harbor requires all 18 removed *and* no
actual knowledge of residual identifiability, which a recall number does not establish; and
this measures the detector, not the rewrite, the key handling, or the operator.

Model-tier results go to the nightly artifact plus a human-authored PR. **No bot commits to
`main`** — a safety number that updates itself is a safety number nobody reads.

---

## The LLM tier — one engine, four providers

**Call it `llm`, never `api`, and never "remote".** "Remote" is taken: `wfrec remote` and
`src/wfrec/remote.py` already mean *SSH to an HPC host*, and reusing it guarantees permanent
confusion in code, flags and docs. "API" is now simply inaccurate — a loopback Ollama is not an
API path. So: `engines/llm_findings.py`, `providers/*`, `wfrec seal --llm`, events
`session.deid.llm.*`, config `[deid.llm]`, and `llm_gate.py` for what was `api_gate.py`. None
of this is implemented yet, so the rename costs nothing.

**One engine, because "designate a local LLM" and "supply a key for Claude or Codex" are the
same shape.** Both are an LLM returning findings as verbatim substrings, verified locally,
contributing additively on top of the regex + GLiNER floor. The only thing that genuinely
differs is *how far the text travels* — and that is a property of the resolved endpoint, not of
the vendor's name. Collapsing them into one engine with four thin transports means the
verification ladder, the chunking, the schema and the audit are written once, and the security
boundary is one classifier (`egress.py`) rather than a per-provider judgement call that drifts.

**The local tier must run first — mandatory and non-configurable.** The LLM is a *third*
stage, and the gate's refusal predicates include "local tier has not completed for this
session." Four reasons: the disclosure is strictly smaller (everything local already caught is
pseudonymized before it goes anywhere); **fail-closed composition** — if the call fails,
refuses, or returns garbage, the session is still de-identified to the CI-measured local
standard, whereas an LLM-only path has a failure mode where a session ends up with *no*
de-identification and a `sealed` flag on it; the "measured recall in CI" claim evaporates
without it, since there is no floor CI can enforce for a call to someone else's model; and
pre-scrubbed text is shorter and cheaper, with the LLM acting as a recall booster on the
*residue* — unusual name forms, free notes, OCR garble, and the three surfaces with no inline
redaction at all. The counter-argument (pre-inserted pseudonyms confusing the detector) is real
but small: mitigate with distinctive placeholders, a system-prompt instruction that they are
already redacted, and `corpus/prescrubbed.jsonl` as a gold tier that *measures* whether it
actually confuses it.

### The provider seam

```python
class LlmProvider(Protocol):
    name: str
    def endpoint(self) -> str                    # what egress.py classifies
    def default_model(self) -> str | None
    def list_models(self) -> list[str]           # for `deid providers`; may be empty
    def extract(self, system: str, chunk: str, schema: type[BaseModel]) -> ProviderResult
```

`ProviderResult` carries `findings, model_served, request_id, usage, stop_reason, schema_mode,
latency_ms`. Providers are this thin on purpose: chunking, the prompt, the schema, the
verification ladder, the post-check and the audit all live in `engines/llm_findings.py`, so
there is exactly **one** implementation of everything that matters for safety.

| Provider | Extra | Structured-output mechanism | Covers |
|---|---|---|---|
| `anthropic` | `[deid-anthropic]` | `client.messages.parse(..., output_format=Findings)` | Claude |
| `openai` | `[deid-openai]` | `response_format={"type":"json_schema", "json_schema":{..., "strict": true}}` | Codex/OpenAI, **vLLM, LM Studio, llama.cpp, Azure, internal gateways** — anything with a `base_url` |
| `ollama` | **none** | `POST /api/chat` with `format=<schema>`, `stream=false`, `keep_alive` held across chunks so the model loads once. `/api/tags` for discovery, `/api/ps` for load state | Ollama, natively |
| `google` | `[deid-google]` | `response_mime_type="application/json"` + `response_schema` | Gemini API and Vertex |

Ollama native needs **no extra at all**: it is plain HTTP and `httpx>=0.26` is already a base
dependency (`pyproject.toml:19`). One transport — OpenAI-compatible — satisfies both the Codex
half of the ask and every local inference server in one implementation.

**Strict JSON-schema support is not uniform across things that claim OpenAI compatibility.**
vLLM has guided decoding, Ollama's `/v1` shim varies by version, internal gateways vary by
whim. So `openai_compat.py` **probes and degrades**, recording the rung it landed on as
`schema_mode` in the audit: `strict_schema` → `json_object` → `prompt_json` (local
`json.loads` + pydantic validate, one retry on parse failure, then drop the chunk and stamp
`sealed=partial`). Degrading the *parse* is safe precisely because it cannot degrade the
*verification* — an unparseable or invented finding is dropped by the ladder either way.

**Model IDs: resolve, don't transcribe.** Anthropic's default is pinned below. For the other
three, `providers/registry.py` resolves the default from the provider's own model-list endpoint
where one exists (`/api/tags`, `/v1/models`), and otherwise carries a constant explicitly
marked *verify against the provider's docs at implementation time* — the same discipline
applied to Anthropic IDs, extended to vendors whose naming there is less reason to trust from
memory. Local providers have no default at all; `--llm-model` is required.

**Anthropic's default model: `claude-sonnet-5`** ($2/$10 per MTok, 1M context) — span extraction
against a fixed taxonomy with a strict schema is a bounded extraction task, not a reasoning
task, and a session's post-stage-1 residual is ~15k–150k input tokens, so ~$0.03–$0.30 per
session. `claude-opus-5` ($5/$25) behind `--llm-model` for maximum recall.
**Do not use `claude-fable-5`**: per the `claude-api` skill it requires 30-day data retention
and is **unavailable under zero data retention** — and an org whose gate for this feature is a
ZDR attestation structurally cannot use it. That is decisive, and the kind of thing otherwise
discovered in production. Re-read the `claude-api` skill at implementation time rather than
trusting any ID transcribed into this document, and do not append date suffixes.

**Never ask the model for character offsets.** Models miscount them, and a de-ID control that
trusts a miscounted offset redacts the wrong characters — worse than a miss, because it looks
like it worked. Ask for a **verbatim substring plus a 1-based occurrence index** and compute
offsets locally:

```python
class Finding(BaseModel):
    record_id: str; text: str; label: DeidLabel
    occurrence: int = 1
    confidence: Literal["high","medium","low"]
```

Submitted via **structured outputs** (`client.messages.parse(..., output_format=Findings)`),
not free JSON in prose and not tool use — forcing a function call to smuggle out data with no
function to run is the pattern structured outputs supersede. `thinking={"type":"adaptive"}`,
`output_config={"effort":"medium"}`. Chunk on **record boundaries, never mid-record**, ~200
records / 40k chars per request, so a retry is cheap and occurrence localization happens
inside a short string where it is essentially never ambiguous.

**Verification ladder**, client-side per finding: exact match at the claimed occurrence →
accept (offsets exact by construction); exact match but fewer occurrences than claimed →
accept the last, record `occurrence_adjusted`; no exact match → try a normalized match
(NFKC, collapse whitespace, casefold) and accept a unique hit as `match_mode="normalized"`;
otherwise **drop** and increment `findings_unverified`. Then an **independent post-check**:
re-run the local regex tier over the rewritten text; any hit is a hard error, because it means
the rewrite did not apply. Cheap, and it catches whole classes of bug offset verification
cannot.

**Prompt caching: default OFF.** The stable system prompt plus the taxonomy is byte-identical
every request, so caching would be free money — but a cache is by definition server-side
retention of a prefix, and an org whose gate is a ZDR attestation deserves to opt into that
explicitly rather than discover it. Expose as `[deid.llm] prompt_cache = false`, and it applies
only to providers that offer a cache — it is meaningless for a loopback model. **Refusal
fallbacks: off** — a safety control should have exactly one named model per configured policy;
record `model_served` (`response.model`) regardless.

### Egress classification — `src/autocab/deid/egress.py`

Decision 9. Classification is **structural**, from the resolved address, because the provider
label carries no information about where the bytes go:

| Class | Condition | Gate |
|---|---|---|
| `none` | unix socket, or **every** resolved address is loopback (`127.0.0.0/8`, `::1`) | **No egress gate.** Consent event still written. |
| `internal` | **every** resolved address is private / site-local / link-local / CGNAT (`10/8`, `172.16/12`, `192.168/16`, `fc00::/7`, `169.254/16`, `100.64/10`) | Ack required with `internal_endpoints_approved`; BAA and ZDR clauses **not** required. Per-invocation flag required. |
| `external` | any resolved address is globally routable, **or** resolution fails, **or** the set is mixed | **All five layers below**, including BAA, ZDR and the verbose flag. |

Six rules, each closing one specific hole:

1. **Escalate on ambiguity.** Mixed A records, DNS failure, or any non-loopback in the set →
   the most restrictive class present. Classifying off record zero is the natural
   implementation and it is unsafe.
2. **Pin the resolution.** Resolve once in the gate, classify, then hand the resolved address
   to the HTTP client — or re-verify immediately before the request and abort on change.
   Otherwise a short TTL downgrades the class between check and call. Honest limit: this closes
   the accidental case, not an attacker who controls both DNS and the box.
3. **Classify the proxy, not the endpoint.** If `HTTP_PROXY` / `HTTPS_PROXY` / `ALL_PROXY` is
   set and the endpoint is not matched by `NO_PROXY`, the text goes to the **proxy**. Without
   this rule, `--llm-base-url http://localhost:11434` under `ALL_PROXY` classifies as `none`
   while egressing every byte. This is the easiest bypass in the design to miss, and the
   single most important line in this section.
4. **`::ffff:127.0.0.1` is loopback; `0.0.0.0` and `[::]` are not.** Both directions are
   routinely gotten wrong, and both are in the test matrix.
5. **Plaintext to non-loopback is refused** unless `--allow-plaintext-llm`, which stamps
   `sealed=partial`. PHI-adjacent text over unencrypted HTTP on a hospital LAN is a finding in
   its own right. Loopback over `http` is fine and must never be nagged about.
6. **`deny_egress_classes[]`** in the ack, alongside the existing `deny_hosts[]`, so an org can
   write "loopback only, ever" once and have it hold for every provider and every future flag.

`egress.py` is pure stdlib with no SDK and no network — the resolver is injected so the whole
matrix is testable offline. That is why it lands early in the implementation order: the security
boundary gets locked before any transport exists to be tempted into shortcutting it.

### The gate — five layers for external egress, all required, each in a different place

Evaluated by `llm_gate.evaluate() -> GateDecision`, deliberately **non-short-circuiting** so
the user sees every failed layer at once instead of fixing them one error message at a time.
The layers below apply in full to `external`; `internal` requires layers 1, 3, 4, 5 plus an ack
carrying `internal_endpoints_approved`; `none` requires layers 1 and 5 only — a consent event
is still written for a loopback call, because "which model saw this session" is an audit
question independent of whether anything left the machine.

| # | Layer | Where | Why separate |
|---|---|---|---|
| 1 | Install opt-in — the `deid-anthropic` / `deid-openai` / `deid-google` extra | `pyproject.toml` | With the SDK absent the first and most reliable gate is an `ImportError`. Nobody enables a cloud provider by accident on a default install. **Ollama is the deliberate exception**: it needs no extra, because gating an install on a transport that reaches only loopback would buy nothing — its containment comes from layer 2's `allowed_endpoints[]` and the egress classifier instead. |
| 2 | Org-policy acknowledgement, **per provider** | `~/.wfrec/policy/deid-llm-<provider>.json` — its own file per provider, not `state.json`, not the config | Requires `provider`, `org`, `baa_confirmed`, `zero_data_retention_confirmed`, `approver_name`, `approver_role`, `approved_at`, `policy_reference`, `expires_at`, `allowed_endpoints[]`, `deny_hosts[]`, `deny_egress_classes[]`. An **attestation naming a human**, so it cannot be written by accident, and it **expires**, so a stale ack cannot outlive the policy that justified it. |
| 3 | Config flag `[deid.llm] enabled` | `~/.wfrec/config.toml` | User-level standing intent, in a *different file* from the org attestation — one person editing one file is never sufficient. |
| 4 | Per-invocation flags | `wfrec seal --llm --i-am-sending-text-offbox` | Two flags, the second deliberately verbose and kept out of the short `--help` so it cannot become muscle memory. A persistent config flag must never by itself cause egress. Not required for egress class `none`. |
| 5 | Consent event, written **pre-flight** | `session.deid.llm.consent` in `events.jsonl` before the first byte leaves | Carries the provider, resolved endpoint, **egress class**, the sha256 of the ack file that authorized *this* egress, `approver_name`, `policy_reference`, resolved model, and the exact record/char counts about to be sent. Writing it first means a crash mid-call still leaves evidence egress was *attempted*, and `EventWriter.append` already `fsync`s. |

**One ack per provider, and that is not bureaucracy.** A BAA is a contract with a *named
counterparty*, so a single global "LLM egress is approved" flag would misrepresent the legal
position the gate exists to encode: an attestation naming Anthropic must not authorize egress to
OpenAI or Google. `tests/test_deid_gate.py` asserts the isolation directly, including the case
where an ack for provider A exists and the invocation names provider B — which aborts.

Plus two guards that are not layers: an interactive confirmation requiring the session id typed
back (bypassable only by `--yes` **and** `WFREC_DEID_LLM_NONINTERACTIVE=1` together, so a
script cannot inherit the bypass from a stray flag); and refusal predicates — expired ack, host
matching `deny_hosts` (so an org can allow the feature generally and still block the machine
inside the clinical VLAN), egress class in `deny_egress_classes`, endpoint outside
`allowed_endpoints`, provider not matching the ack's `provider`, `max_chars_per_session`
exceeded, or local tier incomplete.

**The presence of a credential is deliberately not one of the layers, for any provider.** Per
the `claude-api` skill, an unset `ANTHROPIC_API_KEY` does *not* mean there are no credentials —
the SDK also resolves `ANTHROPIC_AUTH_TOKEN`, an `ant auth login` profile under
`~/.config/anthropic/`, and
WIF env vars. Gating on "is a key available" would let ambient credentials silently arm
network egress.

### Key handling

**Preference order, and the first option is "don't touch it": let each SDK resolve its own.**
Construct zero-arg `anthropic.Anthropic()`, `openai.OpenAI()`, `google.genai.Client()` so we
never hold a secret in a variable of our own, never pass one through a signature, and never
have one in a frame that could land in a traceback. Each SDK reads its own environment
(`ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `GOOGLE_API_KEY` or Vertex ADC); **Ollama needs no
credential at all**, which is part of why the loopback path is the low-friction one. On an
authentication error, print provider-specific guidance rather than a generic failure.

OS keyring only as an optional convenience in the extra, degrading with a named reason —
`keyring` on Linux pulls SecretStorage/dbus and fails on a login node with no session bus,
which is the `libGL` class of failure again. **Never a config file**: the config schema has no
key field, and the loader **hard-rejects** — refuses to run the LLM path, not a warning — any
config containing a key *named* `api_key`/`token`/`secret`, or any string matching a known
credential shape: `sk-ant-[A-Za-z0-9_-]{20,}` (Anthropic), `sk-(proj-)?[A-Za-z0-9_-]{20,}`
(OpenAI), `AIza[A-Za-z0-9_-]{30,}` (Google). The key-*name* rejection is the real catch-all,
since it does not depend on knowing every vendor's prefix — the shape patterns are belt and
braces for the case where someone pastes a key as a value under an innocuous name.
**No dotenv dependency and no auto-loading `.env` from cwd** —
auto-loading from the working directory is exactly how a key ends up inside a session folder
when someone records while sitting in a repo, and session folders are designed to be zipped
and handed to teammates.

Tested, not just stated: a `_scrub_secrets()` guard on the audit writer, plus a CI test
**parameterized over all four credential shapes** that plants a fake key in the environment,
runs a fully mocked seal, then walks the entire session tree *and* `~/.wfrec/daemon.log`
asserting the string appears nowhere.

### Audit and residual risk

Per chunk (`session.deid.llm.chunk` + `<session>/deid/seal-report.json`): `provider`,
`model_requested`, `model_served`, **`egress_class`**, **`schema_mode`**, endpoint **host only**,
`request_id` (`response._request_id` where the provider supplies one — what vendor support
needs), chunk index, `records_in_chunk`, `chars_sent`, `chunk_digest`, token usage,
`latency_ms`, `findings_count` / `_verified` / `_unverified`, `stop_reason`, failure class, and
running `chars_sent_total`. Plus one aggregate `session.deid.llm.completed`. **Never** the
request or response body, any finding's text, the credential, or any prompt echo.

`chunk_digest` is an **HMAC** with the ephemeral session key, not a bare hash — a plain
SHA-256 of a short string like `MRN 4419902` is brute-forceable in milliseconds, so a plain
digest would itself be a disclosure. Per chunk, never per span.

**Stated plainly:** sending text off-box **is** the disclosure event, and no engineering
changes that. Today the project can honestly write "everything is local-only with no outbound
network requests" (`src/wfrec/README.md`). That sentence now has three fates depending on
configuration, and the README must state all three together: it stays **true** for the default
(regex + GLiNER, local by construction) and for a loopback LLM at egress class `none`; it needs
qualifying at class `internal`; and choosing a cloud provider **deletes it**. The prerequisite is an executed BAA
**and** a confirmed zero-data-retention configuration, confirmed by the organization — not by
this tool and not by the analyst. Crucially the confirmation is **echoed verbatim into the
session's own timeline** at consent time, not merely consulted from a machine-level config:
the evidence must live with the data it covers, because an admin can edit `~/.wfrec/policy/`
next week and the session folder is what gets handed to a teammate or attached to an incident
review. And the honest limit: even with every layer satisfied, a BAA is a contractual control,
not a technical one. The technical control is that the text leaving is already pseudonymized to
the measured local standard.

---

## Packaging

Decision 7 moves the default engine into base dependencies. Everything else stays optional.

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
# Ollama native deliberately has NO extra: it is plain HTTP over the httpx that
# pyproject.toml:19 already depends on. The lowest-friction LLM path is also the
# one with zero egress and zero new dependencies, which is the right incentive.
dev            = ["pytest>=8.0"]         # UNCHANGED -> CI adds no LLM SDK
```

`[deid-local]` is **deleted**, not kept as an alias — a no-op extra that people keep typing is
worse than an error telling them it is now built in.

`dev` is untouched, so `.github/workflows/tests.yml` still needs no edit. CI now installs
`onnxruntime` and `tokenizers` explicitly (onnxruntime was already there transitively, so the
real delta is small), and it still **never downloads a weight** — enforced by the autouse
`HF_HUB_OFFLINE=1` fixture rather than by hoping no test constructs the engine.

Nothing heavy enters base dependencies: no torch, no LLM SDK, no spaCy. The regex tier must
still work with stdlib only, which is what lets `SensitiveDataRedactor` delegate to
`find_spans()` without making any of this a hard requirement. Do not pin a CUDA index; document
`--index-url .../whl/cpu` for login nodes. **`uv.lock` is committed**, so any dependency change
must regenerate the lock in the same commit — and this change touches base dependencies, so
the lock diff will be larger than usual and should be reviewed, not waved through.

---

## Implementation order

Sequenced so the "auditable control" claim becomes true as early as possible — **step 4 is
reachable with zero new dependencies and zero CI changes; ship at least that far.**

1. **`labels.py`, `spans.py`, `policy.py`, `allowlist.py`, and the `find_spans()` refactor**
   of `SensitiveDataRedactor` (`components.py:98`), with the `re.escape` + word-boundary fix at
   `:128` and Presidio's checksum validators adopted. Everything else depends on spans
   existing. **Gate: `tests/test_redaction.py` and `tests/test_pipeline.py` pass unmodified.**
2. **`eval/generate.py` + `data/deid-eval/` + `tests/test_deid_corpus.py`.** Corpus *before*
   detector — otherwise you tune the detector against your intuition and the corpus ratifies
   it.
3. **`eval/scorer.py` + `tests/test_deid_scorer.py`.**
4. **`thresholds.json` + `tests/test_deid_regex_recall.py` + `autocab deid eval` +
   `docs/phi-redaction-benchmarking.md`.** This is where the claim becomes measured rather than asserted.
5. **`egress.py` + `llm_gate.py` + `tests/test_deid_egress.py` + `tests/test_deid_gate.py`** —
   pure logic, no SDK, no network, no weights, injected resolver. The cheapest high-value step
   in the plan: it locks the security boundary *before* any transport exists to be tempted into
   shortcutting it, and it locks the gate's shape before anyone collapses five layers into one
   boolean. The proxy-override and mixed-A-record rules belong here, not in a provider.
6. **The seal, with a fake engine.** `src/wfrec/seal.py` (journal, staging, 3-phase commit,
   recovery, recursive payload walk, sidecar rewrite covering `files/diffs/`, `shell/remote/`
   and `jobs/`); `DEID_SEALED` + `SessionSealed` in `events.py`; recovery calls and
   `NotSealed`/`SealInProgress` gates in `exporters/__init__.py:30` and
   `session_bundle.py:41`; `seal`/`deid` subparsers in `cli.py` routed before the
   daemon-preferring block (like `export`); `POST /sessions/seal`; detached spawn from
   `recorder.py:205`. Tests: `test_deid_seal.py`, `test_deid_seal_atomicity.py` (inject
   failure at each journal state rather than killing a process),
   `test_deid_seal_concurrency.py` (assert the separate-inode lock property),
   `test_deid_idempotent.py`, `test_deid_payload_coverage.py`. New `conftest.py` fixtures:
   `unsealed_session` (plants a distinct sentinel PHI string in every string field of one event
   of every type in `events.py`), `sealed_session`, `fake_engine` (tests all seal mechanics at
   zero model cost), `fixed_key`, and a `deid` cache reset mirroring the
   `redaction._SHARED = None` pattern at `conftest.py:45`.
7. **Close the three inline gaps** — `files.py:{381-405,451-459,491}` and
   `remote.py:{131,198}`. Extend `tests/test_wfrec_collectors.py` and
   `tests/test_wfrec_remote.py`.
8. **Package GLiNER as the default (decision 7).** `engines/gliner_onnx.py` + `models.py`
   with int8 **and** fp32 registered and license assertions; the `pyproject.toml` move of
   `onnxruntime`/`tokenizers`/`huggingface-hub`/`numpy` into base `dependencies` with
   `[deid-local]` deleted and `uv.lock` regenerated in the same commit; the autouse
   `HF_HUB_OFFLINE=1` + tmp `HF_HUB_CACHE` fixture in `tests/conftest.py`;
   `wfrec deid fetch|load|verify`; `tests/test_deid_weights.py`; the `doctor.py` section;
   `deid-nightly.yml`; model-tier thresholds. Then `transformers_ner.py` behind
   `[deid-torch]`. Deliberately *after* steps 2–4: packaging the detector does not change the
   rule that the corpus and the scorer come first.
9. **Frames and video.** Line-by-line redact-then-join and the boxes sidecar in
   `screen.py:309`; ffmpeg extracted to `src/wfrec/video.py`; `src/wfrec/frames.py`. Tests:
   `test_deid_frames.py` (synthetic WebP with a known box — pixels inside black, outside
   byte-identical; missing sidecar → deleted; `retain_images=False` → no-op) and
   `test_deid_audit.py` (iterate every planted PHI value against every byte of `seal.json`,
   `deid/audit.jsonl` and every staged file, asserting zero occurrences, and that no
   `pseudonyms.json` exists anywhere).
10. **The LLM tier.** `engines/llm_findings.py` (chunking, prompt, schema, verification
    ladder, post-check, audit — all the safety logic, once) + `providers/{base,registry}.py` +
    `wfrec deid providers` + `tests/test_deid_llm.py` (mocked). Providers land in dependency
    order, each usable before the next exists: **`anthropic_msgs.py`** (design already
    complete) → **`openai_compat.py`** (unlocks Codex *and* every local inference server in one
    transport, with the `schema_mode` probe-and-degrade ladder) → **`ollama_native.py`**
    (`/api/chat` + `/api/tags` + `/api/ps`, zero new dependencies) → **`google_genai.py`**.
    Stopping after the first two already satisfies most of the ask.
11. **Docs honesty pass.** Replace the "that is scrubbing, **not a PHI control**" paragraph at
    `src/wfrec/README.md:263` with a measured claim. The local-only sentence now needs **three
    cases in the same paragraph, not a footnote**: regex and GLiNER are local by construction;
    a loopback LLM is local; a cloud provider is egress and deletes the sentence outright. Say
    which is the default — **local, no egress** — in that same paragraph. Then extend its
    `redactions` note at `:269`; update `.claude/skills/recorder/SKILL.md:104` and its
    `.github/skills/` twin (byte-identical today — keep them so); fix
    `src/wfrec/ui/index.html:138`, which claims "Sensitive values are redacted before storage"
    (true of the inline pass, but it should say what the seal adds).

---

## Verification

```bash
# --- 1. GLiNER is packaged: one install, no extra, no flag ---
uv pip install -e '.[dev]'
python -c "import onnxruntime, tokenizers, huggingface_hub"   # base deps, no extra needed
pytest                                        # full suite incl. egress + weights guards
grep -rn "HF_HUB_OFFLINE" tests/conftest.py   # the guard that keeps CI weightless
autocab deid gen-corpus --seed 1337 --check   # corpus is reproducible
autocab deid eval --engine regex --write-scorecard   # per-label recall + precision corpus

wfrec deid fetch && wfrec deid verify && wfrec doctor   # armed; revision + hashes + egress class
AUTOCAB_DEID_MODEL_TESTS=1 pytest -m model
autocab deid eval --engine gliner --write-scorecard --report-latency   # int8 vs fp32 delta

wfrec start --title "deid smoke" --watch .
wfrec note "MRN 4419902, Jane Smith, DOB 2012-06-01, /data/proj/smith_jane_R1.fastq.gz"
wfrec stop && wfrec seal --status <id>
grep -rc "4419902\|Jane Smith\|smith_jane" ~/.wfrec/sessions/<id>/   # expect 0 EVERYWHERE
jq . ~/.wfrec/sessions/<id>/seal.json                                # receipt + integrity chain
wfrec seal <id>                                                      # no-op
wfrec seal <id> --reseal                                             # gen-2, existing surrogates intact
wfrec pull <id>                                                      # must raise SessionSealed
autocab demo --input-mode session --session-dir ~/.wfrec/sessions/<id>

# --- 2. designate a local LLM: no egress, no extra, no ack ---
wfrec deid providers          # ollama reachable? which models pulled? egress class?
wfrec seal <id> --reseal --engine gliner+llm --llm-provider ollama \
    --llm-base-url http://localhost:11434 --llm-model <model>
jq '.engines, .egress_class' ~/.wfrec/sessions/<id>/seal.json         # expect "none"

# the bypass that MUST fail, and the single most important check in this file
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

The checks that matter most and are easiest to get wrong:

- **The proxy reclassification.** A silent full-egress bypass that reads as loopback in every
  log. If only one thing from part 3 gets tested, it is this.
- **Mixed A records.** Classifying off record zero is the natural implementation and it is
  unsafe; so is treating a DNS failure as anything other than `external`.
- **Per-provider ack isolation.** One global flag would misrepresent a contract with a named
  counterparty — the legal position the gate exists to encode.
- **`HF_HUB_OFFLINE=1` in `conftest.py`.** Without it CI quietly starts downloading 120 MB per
  run and nobody notices until the bill or the 10-minute timeout.
- **The recursive `grep -rc` sweep.** A de-identification control whose own audit file leaks
  the identifiers is worse than none — now widened to credential shapes and `daemon.log`.
- **Crash recovery from each journal state**, and `test_deid_scorer.py`'s clipped-span case,
  which is the difference between measuring leaks and hiding them behind partial credit.

---

## Deliberately deferred — and the one I'd argue about

- **Bioinformatics paths, filenames and sample IDs.** Scoped out by decision, and worth
  flagging once that in this domain it is probably the dominant leak vector. Two things make
  the deferral much safer than it sounds: step 7 means these surfaces stop being written raw,
  and the eval corpus *labels* them with explicit low floors (`SAMPLE_ID 0.60`,
  `SLURM_JOB_NAME 0.50`), so the gap is measured and visible rather than assumed. GLiNER's
  zero-shot labels are also the cheap way to close it whenever desired — the label
  descriptions are authored in our own `labels.py`, so it is a config change, not a retrain,
  and after decision 7 that engine is present on every install rather than behind an extra.
- **HIPAA identifier 17 (photographs)** — uncovered by construction; frames are images. The
  control is `retain_images=False` or source gating, not redaction.
- Screen-capture application denylisting with auto-pause, encryption at rest, GUI panic-pause
  — already future work in `plan.md` §9.
- Re-identification support — excluded by design, not schedule.
- IRB documentation and a security review, which `plan.md` §9 already names as prerequisites
  before this is pointed at real clinical work. Nothing here replaces them. What this delivers
  is the framework plus a measured recall number: a real control with known limits, not a
  safety guarantee. The docs in step 11 should say exactly that.
