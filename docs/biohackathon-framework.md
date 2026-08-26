# AutoCAB BioHackathon Framework

This repository now includes a lightweight framework for the challenge described in `docs/proposal/AutoCAB-challenge.docx`: turn repeated public-data bioinformatics workflows into governed, reviewable AI skill drafts.

## Core flow

1. Ingest workflow evidence from a safe input adapter.
2. Normalize events into `WorkflowTrace` records.
3. Cluster repeated traces into candidate workflow families.
4. Redact sensitive text before any proposal generation.
5. Match clusters against a curated skill reference set.
6. Generate a draft `SKILL.md` proposal with validation notes.
7. Route the proposal through a human review service.
8. Export approved proposals as PR-ready folders.

## Package layout

```text
src/autocab/framework/
  bootstrap.py      Default pipeline factory for hackathon demos
  config.py         Tunable thresholds, deny terms, benchmark target
  contracts.py      Pluggable component interfaces
  components.py     Default adapters and services
  pipeline.py       End-to-end orchestration with execution context
```

## Default components

- `TraceInputAdapter`: loads synthetic or public workflow traces from JSON.
- `ScreenCaptureInputAdapter`: loads pre-exported local capture timelines.
- `WorkflowClusterer`: groups traces by workflow family.
- `SensitiveDataRedactor`: masks identifiers and deny-listed terms.
- `SkillCatalog`: loads the 3-5 skill reference set for matching.
- `SkillProposalBuilder`: classifies coverage and drafts proposal content.
- `InMemoryReviewService`: keeps a simple human review queue.
- `ProposalExporter`: writes `SKILL.md` and `metadata.json`.

## Team split for a 72-hour hackathon

- Data/trace lane: improve input adapters and synthetic/public workflow traces.
- Privacy/governance lane: expand redaction rules and audit logs.
- Matching lane: replace keyword overlap with embeddings or retrieval.
- Authoring lane: improve `SKILL.md` generation and examples.
- Review/demo lane: add a Streamlit or FastAPI review UI on top of `ReviewService`.

## Extension points

- Add a new input mode by implementing an `InputAdapter`.
- Swap the `Clusterer` for embeddings or sequence-aware grouping.
- Replace `SkillProposalBuilder` with an LLM-backed drafting service.
- Replace `InMemoryReviewService` with a persisted queue or API.
- Replace `ProposalExporter` with GitHub PR automation.

## Suggested next steps

1. Add `streamlit_app.py` that calls `build_default_pipeline()`.
2. Add a `catalog/` folder with real public `SKILL.md` examples.
3. Add a retrieval abstraction for embeddings versus lexical matching.
4. Add review states such as `needs-edits` and `rejected`.
5. Add validation runners for HG008 or another public benchmark workflow.
