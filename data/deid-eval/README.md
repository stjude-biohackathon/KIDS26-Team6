# De-identification evaluation corpus

This directory contains the generated synthetic corpus, scorecards, thresholds,
and historical baseline used to evaluate AutoCAB's text de-identification
engines.

## Purpose

The corpus is committed before detector changes are evaluated. This prevents a
new pattern from being assessed only against examples written with that pattern
in mind.

The benchmark measures complete-span coverage and over-redaction across
scientific and computational text. See the generated
[PHI redaction benchmarking report](../../docs/phi-redaction-benchmarking.md)
for current results, model comparisons, and limitations.

## Install and setup

Run all commands from the repository root. Install the optional GLiNER2 runtime
to reproduce every detector configuration:

```bash
uv sync --extra deid-gliner2
```

Fetch and verify the local model weights:

```bash
autocab deid fetch --model gliner
autocab deid fetch --model gliner2-pii
```

The fetch commands require network access. Evaluation is local after the
verified weights are available.

## Reproduce the evaluation

### Verify the corpus

Confirm that the committed corpus matches deterministic generation from seed
`1337`:

```bash
autocab deid gen-corpus --seed 1337 --check
```

Expected output:

```text
OK Corpus reproduces byte-for-byte: 316 records, fingerprint ccfbf64eb85ed302
```

### Generate the supported scorecards

The supported sealing configurations retain the mandatory regex floor:

```bash
autocab deid eval --engine regex --write-scorecard --check-thresholds
autocab deid eval --engine gliner --write-scorecard --check-thresholds
autocab deid eval --engine gliner2-pii --write-scorecard
```

### Generate the diagnostic controls

The model-only configurations measure what each model contributes without the
regex floor. They are diagnostic controls and are not production sealing modes:

```bash
autocab deid eval --engine gliner-only --write-scorecard
autocab deid eval --engine gliner2-pii-only --write-scorecard
```

Each `eval --write-scorecard` run rebuilds the comparison report and accessible
SVG from every available scorecard. One engine run does not replace the other
engines' results.

The report displays a PNG for broad Markdown-renderer compatibility. Refresh it
after regenerating the scorecards and SVG:

```bash
rsvg-convert docs/figures/phi-redaction-accuracy.svg \
  --output docs/figures/phi-redaction-accuracy.png
```

## Repository layout

| Path | Purpose |
| --- | --- |
| `corpus/{shell,ocr,notes,agents,diffs,jobs,prescrubbed,negatives}.jsonl` | Evaluation fixtures, one JSON object per line |
| `lexicons/{surnames,given-names,cities,gene-symbols}.txt` | Public-domain draw pools |
| `thresholds.json` | Recall floors and the precision ceiling, keyed on `corpus_fingerprint` |
| `baselines/v0.2-regex.json` | Historical metrics used for the release comparison |
| `scorecard.regex.json` | Committed regex snapshot checked by CI |
| `scorecard.gliner-only.json` | Diagnostic GLiNER-only snapshot |
| `scorecard.gliner.json` | Regex + GLiNER snapshot from the same corpus |
| `scorecard.gliner2-pii-only.json` | Diagnostic GLiNER2 PII-only snapshot |
| `scorecard.gliner2-pii.json` | Supported optional Regex + GLiNER2 PII snapshot |

## Evaluation methodology

### Coverage and scoring

The CI gate requires full character coverage because partial masking can still
expose an identifier. Exact typed-span metrics are secondary diagnostics.
Production capture, export, and sealing always retain the regex floor.

### Record structure

Each JSONL record contains its input text, labelled spans, protected terms, and
generation provenance. For example:

```json
{
  "channel": "notes",
  "difficulty": "easy",
  "id": "notes-nt01-00",
  "license": "CC0-1.0",
  "must_survive": [],
  "provenance": "generated:v1:seed=1337:tmpl=nt01",
  "spans": [
    {
      "end": 15,
      "hipaa": 18,
      "label": "SUBJECT_ID",
      "start": 8,
      "text": "SJ-7040"
    },
    {
      "end": 30,
      "hipaa": 1,
      "label": "NAME",
      "start": 17,
      "text": "Samuel Torres"
    },
    {
      "end": 46,
      "hipaa": 3,
      "label": "DOB_LABELLED",
      "start": 32,
      "text": "DOB 1970-12-15"
    },
    {
      "end": 71,
      "hipaa": 3,
      "label": "DATE_BARE",
      "start": 61,
      "text": "1976-08-11"
    },
    {
      "end": 96,
      "hipaa": 4,
      "label": "PHONE",
      "start": 81,
      "text": "+1-555-555-0191"
    }
  ],
  "text": "Subject SJ-7040 (Samuel Torres, DOB 1970-12-15) consented on 1976-08-11. Contact +1-555-555-0191."
}
```

`span.text` duplicates `text[start:end]` intentionally.
[`tests/test_deid_corpus.py`](../../tests/test_deid_corpus.py) verifies this
relationship for every span in every record so manual edits cannot silently
invalidate offsets.

### Why the fixtures use JSONL

The repository [`.gitignore`](../../.gitignore) ignores formats such as
`*.vcf`, `*.fastq`, `*.bam`, and `*.log`. Storing a fixture as
`slurm-4213.log`, for example, could silently remove an evaluation channel from
the commit and inflate detector performance. The corpus therefore embeds these
formats as strings inside tracked JSONL records.

### Difficulty tiers

| Tier | Definition | Included in the gate? |
| --- | --- | --- |
| `easy` | Canonical forms such as `MRN 4419902` and `DOB 2012-06-01` | Yes |
| `medium` | Realistic variations such as `MRN:4419902`, `M.R.N.`, or `3/14/1972` | Yes |
| `hard` | OCR corruption, collapsed whitespace, and run-together tokens | No, reported only |

Gating only `easy` records would partly measure consistency between the regex
table and the generator. Gating `hard` records would fail the suite for
deliberately malformed inputs. Thresholds therefore use `easy` and `medium`;
the report presents `hard` separately with a lower floor.

### Gold-span boundaries

Gold annotations use the smallest meaningful span. The path
`/data/proj/SJALL018/smith_jane_R1.fastq.gz`, for example, contains two spans:
`SJALL018` as `SUBJECT_ID` and `smith_jane` as `NAME`. Redacting the whole path
earns recall credit but incurs an over-redaction penalty because it destroys
useful directory structure.

Label-anchored identifiers are the documented exception. `MRN 4419902` is one
gold span that includes the `MRN` keyword. The same rule applies to `DOB`,
`SSN`, `sample:`, and `specimen` labels.

### Adversarial negatives

`negatives.jsonl` prevents a broad pattern such as `.*` from appearing to have
perfect performance. Its records have no gold spans, and each protected term is
listed in `must_survive`.

The negative fixtures include HGNC symbols that resemble surnames, HGVS
variants, genomic coordinates, reference builds, public accessions, 10x index
sets, sequencing barcodes, and container tags whose build stamps resemble
clinical dates.

## Synthetic data and PHI safeguards

The generator uses two strategies to reduce the risk that synthetic fixtures
encode linked information about real people.

### Reserved identifier spaces

Where a reserved space exists, the generator uses it and
[`tests/test_deid_corpus.py`](../../tests/test_deid_corpus.py) enforces it:

| Label | Reserved space | Authority or rationale |
| --- | --- | --- |
| `EMAIL`, `URL` | `example.org`, `example.com`, `example.net` | RFC 2606 |
| `PHONE`, `FAX` | `555-555-01xx` | NANP fictitious range in an unassignable area code |
| `SSN` | Areas `000`, `666`, and `9xx` | Never issued by the SSA |
| `IP` (v4) | `192.0.2.0/24`, `198.51.100.0/24`, `203.0.113.0/24` | RFC 5737 |
| `IP` (v6) | `2001:db8::/32` | RFC 3849 |
| `ACCOUNT` | Published card test numbers | Card-network test data |
| `MRN` | `44xxxxx` | Test-only band defined for this corpus |
| `LICENSE` (NPI) | Checksum-invalid 10-digit numbers | Reserved by arithmetic |

A checksum-valid NPI could belong to a real clinician. The generator therefore
uses only numbers that fail the Luhn check with the `80840` issuer prefix. The
detector still redacts them because validators can raise confidence but never
reject an otherwise sensitive match. A mistyped identifier may still contain
protected health information (PHI).

### Independent-draw provenance

Names, locations, dates, and ages have no reserved space. Each field is an
independent seeded draw from a committed public-domain lexicon. The surname,
given name, city, ZIP code, and date are sampled independently so their
combination does not intentionally construct a real person's profile.

`SUBJECT_ID`, `SAMPLE_ID`, and `ACCESSION` follow the same principle because no
reserved institutional identifier bands exist. The source lexicons use US
Census surname frequencies, Social Security Administration given-name
frequencies, and USGS or Census place names. Their file headers document the
sources.

### Additional checks

[`tests/test_deid_corpus.py`](../../tests/test_deid_corpus.py) verifies that the
corpus excludes real institutional domains. It also ensures that `GIAB HG008`,
the repository's `PipelineConfig.benchmark_dataset`, is never labelled as PHI.
The term appears only in `must_survive` so a name detector cannot redact the
benchmark dataset identifier.

## Runtime benchmarking

Runtime is machine-specific and is not committed to the scorecards. Measure
model loading separately from warmed inference:

```bash
autocab deid benchmark --engine regex --engine gliner-only --engine gliner \
  --output benchmark.json --plot benchmark-performance.svg

autocab deid benchmark --engine gliner2-pii-only --engine gliner2-pii
```

The benchmark records model loading plus first inference as cold start. It then
warms each loaded engine once and measures seven inference runs by default. Its
JSON output includes median and p95 milliseconds per kilobyte, records per
second, peak process memory, corpus fingerprint, model revision, threshold,
CPU, Python, ONNX Runtime, and measurement time.

## Maintaining the corpus

### Preserve regression fixtures

`notes-nt06` contains the intentionally awkward phrase `Specimen specimen
YO64-40805`. It exposed a scan-resume bug in `accession_labelled`: the first
candidate lacked digits, and resuming after that rejected match skipped the
actual accession. `RegexRules._apply` now resumes at `match.start() + 1`. Keep
this fixture to protect that behavior.

### Add or update fixtures

1. Add or edit a `Template` in
   [`src/autocab/deid/eval/generate.py`](../../src/autocab/deid/eval/generate.py).
   Assign a new `tid`. Per-record seeds derive from `(seed, tid, index)`, so a
   new template does not reshuffle later records.
2. Use only `{field}` markers defined in `FIELDS`. An unknown marker is an
   error, not a literal.
3. Regenerate the corpus with `autocab deid gen-corpus --seed 1337` and inspect
   the diff.
4. Regenerate the regex scorecard with
   `autocab deid eval --engine regex --write-scorecard` and inspect which labels
   changed.
5. Run the focused tests:

   ```bash
   .venv/bin/python -m pytest \
     tests/test_deid_corpus.py tests/test_deid_regex_recall.py
   ```

Do not hand-edit `corpus/*.jsonl`. The deterministic corpus check will reject
manual changes, but only after the offsets or provenance have already been put
at risk.
