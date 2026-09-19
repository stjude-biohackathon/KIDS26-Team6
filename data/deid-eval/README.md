# `data/deid-eval` — the de-identification evaluation corpus

Generated, committed, and reproducible:

```bash
autocab deid gen-corpus --seed 1337 --check    # must report no differences
autocab deid eval --engine regex --write-scorecard
autocab deid eval --engine gliner --write-scorecard
# Diagnostic only; this is not a production sealing mode.
autocab deid eval --engine gliner-only --write-scorecard
# Experimental PII model. Install and fetch it explicitly first.
autocab deid eval --engine gliner2-pii-only --write-scorecard
autocab deid eval --engine gliner2-pii --write-scorecard
```

The corpus exists **before** the detector, on purpose. A corpus written after
the patterns measures the author's memory of the patterns.

## Layout

| Path | What it is |
| --- | --- |
| `corpus/{shell,ocr,notes,agents,diffs,jobs,prescrubbed,negatives}.jsonl` | the fixtures, one JSON object per line |
| `lexicons/{surnames,given-names,cities,gene-symbols}.txt` | public-domain draw pools |
| `thresholds.json` | recall floors and the precision ceiling, keyed on `corpus_fingerprint` |
| `scorecard.regex.json` | committed regex snapshot; CI asserts computed == this |
| `scorecard.gliner.json` | committed Regex + GLiNER snapshot from the same corpus |
| `scorecard.gliner2-pii-only.json` | experimental GLiNER2 PII-only snapshot |
| `scorecard.gliner2-pii.json` | experimental Regex + GLiNER2 PII snapshot |

## Comparing accuracy and runtime

Each `eval --write-scorecard` run rebuilds the comparison report and accessible
accuracy plot from every available scorecard. The last engine run cannot replace
the other engine's results.

Runtime is machine-specific, so it is measured separately and is not committed
to a scorecard:

```bash
autocab deid benchmark --engine regex --engine gliner-only --engine gliner \
  --output benchmark.json --plot benchmark-performance.svg

# Compare the optional PII-tuned model after fetching its pinned weights.
autocab deid benchmark --engine gliner2-pii-only --engine gliner2-pii
```

The benchmark reports model load plus first inference as cold start. It then
warms the loaded engine once and measures seven inference runs by default. The
JSON records median and p95 ms/KB, records per second, peak process memory,
corpus fingerprint, model revision, threshold, CPU, Python, ONNX Runtime, and
measurement time. Exact typed span metrics are secondary diagnostics. The CI
gate remains full character coverage because partial masking still leaks PHI.
The `gliner-only` engine bypasses the regex floor only inside this evaluation
harness. Production capture, export, and sealing continue to require regex.

Record shape:

```json
{"id":"notes-nt01-00","channel":"notes","difficulty":"easy",
 "text":"Subject SJ-4817 (Mary Brooks, DOB 1972-03-14) consented on 2019-08-02. ...",
 "spans":[{"start":8,"end":15,"label":"SUBJECT_ID","text":"SJ-4817","hipaa":18}, ...],
 "must_survive":["GRCh38"],
 "provenance":"generated:v1:seed=1337:tmpl=nt01","license":"CC0-1.0"}
```

`span.text` is a redundant copy of `text[start:end]`, and
`tests/test_deid_corpus.py` asserts they are equal for **every span in every
record**. It is the cheapest available defence against offset rot and it will
fire the first time somebody edits a fixture by hand.

### Everything is `.jsonl`, and that is not a style preference

The repository `.gitignore` already ignores `*.vcf`, `*.fastq`, `*.bam` and
`*.log`. A fixture file named `slurm-4213.log` would be silently dropped from
the commit, and CI would then score a corpus with a missing channel and report a
higher number than the detector deserves. Those shapes are embedded as strings
inside JSONL instead.

## Why none of this is real PHI

Two tiers, because the two halves of the taxonomy have different options.

### Tier 1 — structural reservation

Where a reserved space exists, the generator uses it and
`tests/test_deid_corpus.py` enforces it. A fixture in these classes physically
cannot collide with a live value:

| Label | Reserved space | Authority |
| --- | --- | --- |
| `EMAIL`, `URL` | `example.org`, `example.com`, `example.net` | RFC 2606 |
| `PHONE`, `FAX` | `555-555-01xx` | NANP fictitious range, in an unassignable area code |
| `SSN` | areas `000`, `666`, `9xx` | never issued by the SSA |
| `IP` (v4) | `192.0.2.0/24`, `198.51.100.0/24`, `203.0.113.0/24` | RFC 5737 |
| `IP` (v6) | `2001:db8::/32` | RFC 3849 |
| `ACCOUNT` | published card test numbers | the card networks |
| `MRN` | `44xxxxx` | **documented test-only band, defined here** |
| `LICENSE` (NPI) | checksum-**invalid** 10-digit numbers | reserved by arithmetic |

The NPI row is worth dwelling on. A checksum-*valid* NPI could belong to a real
clinician, so the generator produces only numbers that fail the Luhn check with
the `80840` issuer prefix. Recall on them is still 1.00 — which makes the corpus
a live demonstration of the rule in `engines/validators.py` that **validators
raise the score and never reject**. A mistyped SSN is still PHI.

### Tier 2 — independent-draw provenance

`NAME`, `LOCATION`, `DATE` and `AGE` have no reserved space: every surname is
somebody's. So **every field of every record is an independent seeded draw**
from a committed public-domain lexicon. The surname does not know the given
name, which does not know the city, which does not know the ZIP, which does not
know the date. A city is deliberately paired with an unrelated ZIP.

The property that matters is that no *combination* maps to a real person. A lone
surname in a synthetic shell command is not a disclosure; a surname next to a
matching DOB and a matching address would be.

`SUBJECT_ID`, `SAMPLE_ID` and `ACCESSION` are covered by the same reasoning —
there is no reserved band for an institutional subject code, so they are
independent draws carrying no linked attributes.

The lexicons are US Census surname frequencies, SSA given-name frequencies and
USGS/Census place names — all works of the US federal government and therefore
public domain. See the header comment in each file.

### What is also checked

`tests/test_deid_corpus.py` asserts the corpus contains no real institutional
domain (`stjude.org` and friends), and that `GIAB HG008` — this repository's own
`PipelineConfig.benchmark_dataset` — is never labelled as PHI. It appears only
in `must_survive`, which is the point: without the allowlist, a clinical name
detector redacts the benchmark the pipeline is measured against.

## Difficulty stratification is not optional

| Tier | What it is | Gated? |
| --- | --- | --- |
| `easy` | canonical shapes: `MRN 4419902`, `DOB 2012-06-01` | yes |
| `medium` | realistic variation: `MRN:4419902`, `M.R.N.`, `3/14/1972`, no separator | yes |
| `hard` | OCR garble (`MRN 44S427O`), collapsed whitespace, run-together tokens | **no — reported only** |

Gating `easy` alone would be a tautology: the regex table and the generator
share one understanding of the same identifier shapes, so a high `easy` score is
partly a measurement of internal consistency. Gating `hard` would fail the suite
for the wrong reason. So the floors are computed over `easy`+`medium`, and
`hard` is reported in `docs/deid-evaluation.md` with its own much lower floor.

## Gold is minimal-span, with one documented exception

`/data/proj/SJALL018/smith_jane_R1.fastq.gz` carries **two** gold spans —
`SJALL018` → `SUBJECT_ID` and `smith_jane` → `NAME` — not one span over the
whole path. A detector that redacts the entire path still earns full recall
credit (`G ⊆ P`) but is charged for the difference as over-redaction. That is
the right incentive: whole-path redaction is safe but destroys the directory
structure an analyst needs.

The exception is **label-anchored identifiers**. `MRN 4419902` is one gold span
including the `MRN` keyword, matching what the detector has always emitted and
what `[REDACTED_MRN]` / `MRN_a7f3c1` replace. Same for `DOB`, `SSN`, `sample:`
and `specimen`.

## `negatives.jsonl` is adversarial, and that is the whole point

Without adversarial negatives, precision is meaningless and a `.*` patch scores
perfect recall. These fixtures carry **zero** gold spans and every token in
`must_survive`: HGNC symbols that read as surnames (`MET`, `SET`, `MAX`,
`CLOCK`, `TIMELESS`), HGVS (`c.1521A>G`, `p.Phe508del`), coordinates
(`chr7:117559590`), reference builds, `SRR12345678`, `PRJNA######`, 10x index
sets, sequencing barcodes, and container tags whose build stamp looks exactly
like a clinical date (`biocontainers/gatk:4.5.0.0--2024-01-15`).

## Deliberately awkward fixtures

`notes-nt06` reads `"Specimen specimen YO64-40805 from ..."`. The duplication is
ugly and it stays: it is the fixture that caught a real bug. The
`accession_labelled` rule first matched `"Specimen specimen"`, which its
`require` predicate correctly rejected for containing no digits — and because
the scan resumed after the rejected match, the *actual* accession was never
seen. Measured `ACCESSION` recall sat at 0.500 and looked like a missing
pattern. `RegexRules._apply` now resumes at `match.start() + 1` on rejection,
and this record is what keeps it that way.

## Adding fixtures

1. Add or edit a `Template` in `src/autocab/deid/eval/generate.py`. Give it a
   new `tid`; per-record seeds are derived from `(seed, tid, index)` so adding a
   template does not reshuffle every record after it.
2. Use only `{field}` markers from `FIELDS`. A marker with no field is a hard
   error, not a literal.
3. `autocab deid gen-corpus --seed 1337` and commit the result.
4. `autocab deid eval --engine regex --write-scorecard` and commit that too. The
   scorecard diff names exactly which labels moved; put that in the PR.
5. `pytest tests/test_deid_corpus.py tests/test_deid_regex_recall.py`.

Never hand-edit a `corpus/*.jsonl` file. `--check` will catch it, but only after
you have wasted the time.
