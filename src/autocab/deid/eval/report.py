"""Run an engine over the corpus, compare to the floors, render the doc.

Three artifacts, deliberately different in kind:

``thresholds.json``
    per-label **floors**, keyed on a ``corpus_fingerprint``. Floors live in data,
    not in test source, so changing one is a reviewable diff in a file whose
    whole purpose is to be argued about.
``scorecard.<engine>.json``
    a committed **snapshot**. Floors catch a catastrophic regression; they do not
    catch a slide from 0.98 to 0.91. CI asserts computed == snapshot, so any
    pattern change fails with a diff naming the label that moved, and the author
    reruns ``--write-scorecard`` and puts the delta in the PR.
``docs/deid-evaluation.md``
    generated and committed, headed "do not hand-edit".

Latency is measured but **never** enters the snapshot: a wall-clock number in a
committed file churns on every run, and a file that always has a diff is a file
nobody reads.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

from .. import detect, mask_token, normalize_text, render
from ..allowlist import Allowlist
from ..labels import HIPAA_BY_LABEL, hipaa_rollup
from ..engines.base import EngineUnavailable
from ..engines.registry import load as load_engine
from ..spans import Detector
from .corpus import Corpus, load as load_corpus
from .generate import GATED_DIFFICULTIES
from .scorer import Prediction, Scorecard, score

DEFAULT_DENY_TERMS = ("patient", "diagnosis", "pathology")

THRESHOLDS_FILENAME = "thresholds.json"


@dataclass(frozen=True, slots=True)
class Latency:
    """Reported, never committed. See the module docstring."""

    engine: str
    seconds: float
    kilobytes: float

    @property
    def ms_per_kb(self) -> float:
        return 1000.0 * self.seconds / self.kilobytes if self.kilobytes else 0.0

    def to_dict(self) -> dict[str, object]:
        return {
            "engine": self.engine,
            "seconds": round(self.seconds, 4),
            "kilobytes": round(self.kilobytes, 2),
            "ms_per_kb": round(self.ms_per_kb, 2),
        }


def build_detectors(engine: str) -> list[Detector]:
    """Model tiers for ``--engine``. ``regex`` adds nothing.

    ``surrogate_guard`` and ``regex_rules`` are never in this list: they always
    run, inside :func:`autocab.deid.detect`.
    """

    if engine == "regex":
        return []
    names = [name for name in engine.split("+") if name and name != "regex"]
    return [load_engine(name) for name in names]


def predict(
    corpus: Corpus,
    *,
    engine: str = "regex",
    deny_terms: Sequence[str] = DEFAULT_DENY_TERMS,
    allowlist: Allowlist | None = None,
) -> tuple[list[Prediction], Latency]:
    """Detect and mask every record.

    Masks rather than pseudonymizes: the scorer only needs to know *which
    characters were rewritten*, and running the eval through the seal's
    pseudonymizer would make the scorecard depend on an ephemeral key.
    """

    detectors = build_detectors(engine)
    texts = [normalize_text(record.text) for record in corpus]
    allowlist = allowlist or Allowlist.default()

    started = time.perf_counter()
    all_spans = detect(
        texts, detectors=detectors, deny_terms=deny_terms, allowlist=allowlist
    )
    elapsed = time.perf_counter() - started

    predictions: list[Prediction] = []
    for text, spans in zip(texts, all_spans):
        rendered = render(text, spans, lambda span, _surface: mask_token(span.label))
        predictions.append((rendered, spans))

    kilobytes = sum(len(text.encode("utf-8")) for text in texts) / 1024.0
    return predictions, Latency(engine=engine, seconds=elapsed, kilobytes=kilobytes)


def run(
    *,
    engine: str = "regex",
    root: Path | None = None,
    deny_terms: Sequence[str] = DEFAULT_DENY_TERMS,
) -> tuple[Scorecard, Latency]:
    corpus = load_corpus(root)
    predictions, latency = predict(corpus, engine=engine, deny_terms=deny_terms)
    card = score(corpus, predictions, engine=engine)
    card.leaks.extend(())  # keep the list mutable-typed for report consumers
    return card, latency


# --------------------------------------------------------------------------
# Thresholds
# --------------------------------------------------------------------------
def load_thresholds(root: Path | None = None) -> dict[str, object]:
    root = root or _corpus_root()
    return json.loads((root / THRESHOLDS_FILENAME).read_text(encoding="utf-8"))


def _corpus_root() -> Path:
    from .corpus import default_root

    return default_root()


def check_thresholds(
    card: Scorecard, thresholds: dict[str, object], *, engine: str | None = None
) -> list[str]:
    """Compare a scorecard to its floors. Returns failures, most severe first.

    Non-short-circuiting: every violated floor is reported in one run. A gate
    that stops at the first failure turns one fix-and-rerun cycle into five.
    """

    engine = engine or card.engine
    failures: list[str] = []

    expected_fingerprint = thresholds.get("corpus_fingerprint")
    if expected_fingerprint and expected_fingerprint != card.corpus_fingerprint:
        failures.append(
            "corpus_fingerprint mismatch: thresholds were set against "
            f"{expected_fingerprint[:12]}... but the corpus is "
            f"{card.corpus_fingerprint[:12]}.... Regenerate the corpus or "
            "re-derive the floors; never compare across corpora."
        )

    engines = thresholds.get("engines", {})
    spec = engines.get(engine) if isinstance(engines, dict) else None
    if not isinstance(spec, dict):
        failures.append(f"no thresholds defined for engine {engine!r}")
        return failures

    per_label = spec.get("per_label_recall_strict", {})
    for label, floor in sorted(per_label.items()):
        observed = card.by_label_gated.get(label)
        if observed is None:
            failures.append(
                f"{label}: no gated gold spans in the corpus, so its floor of "
                f"{floor} is unenforced -- add fixtures or drop the floor"
            )
            continue
        if observed.recall_strict + 1e-9 < float(floor):
            failures.append(
                f"{label}: recall_strict {observed.recall_strict:.4f} < floor {floor} "
                f"({observed.gold - observed.strict}/{observed.gold} gated spans missed)"
            )

    overall_floor = spec.get("overall_recall_strict")
    if overall_floor is not None and card.recall_strict_gated + 1e-9 < float(overall_floor):
        failures.append(
            f"overall: recall_strict_gated {card.recall_strict_gated:.4f} < floor {overall_floor}"
        )

    safe_floor = spec.get("safe_record_rate")
    if safe_floor is not None and card.safe_record_rate + 1e-9 < float(safe_floor):
        failures.append(
            f"safe_record_rate {card.safe_record_rate:.4f} < floor {safe_floor}"
        )

    ceiling = spec.get("max_over_redaction_rate")
    if ceiling is not None and card.over_redaction_rate > float(ceiling) + 1e-9:
        failures.append(
            f"over_redaction_rate {card.over_redaction_rate:.4f} > ceiling {ceiling}. "
            "This is the ceiling that stops a `.*` patch buying recall."
        )

    fp_ceiling = spec.get("max_false_positive_spans_per_negative_record")
    if (
        fp_ceiling is not None
        and card.false_positive_spans_per_negative_record > float(fp_ceiling) + 1e-9
    ):
        failures.append(
            "false_positive_spans_per_negative_record "
            f"{card.false_positive_spans_per_negative_record:.4f} > ceiling {fp_ceiling}"
        )

    allowed_violations = spec.get("must_survive_violations", 0)
    if len(card.must_survive_violations) > int(allowed_violations):
        names = ", ".join(
            sorted({item["text"] for item in card.must_survive_violations})[:8]
        )
        failures.append(
            f"must_survive_violations {len(card.must_survive_violations)} > "
            f"{allowed_violations}: {names}"
        )

    return failures


# --------------------------------------------------------------------------
# Snapshot
# --------------------------------------------------------------------------
def scorecard_path(engine: str, root: Path | None = None) -> Path:
    root = root or _corpus_root()
    return root / f"scorecard.{engine.replace('+', '-')}.json"


def write_scorecard(card: Scorecard, root: Path | None = None) -> Path:
    path = scorecard_path(card.engine, root)
    path.write_text(
        json.dumps(card.to_dict(), indent=2, sort_keys=True, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    return path


def compare_scorecard(card: Scorecard, root: Path | None = None) -> list[str]:
    """Diff a fresh scorecard against the committed snapshot.

    Reports the *labels that moved*, not a blob diff -- the point of the snapshot
    is that a failure names what changed so the delta can go in the PR
    description.
    """

    path = scorecard_path(card.engine, root)
    if not path.exists():
        return [f"{path} is missing; run `autocab deid eval --write-scorecard`"]
    committed = json.loads(path.read_text(encoding="utf-8"))
    fresh = card.to_dict()

    problems: list[str] = []
    old_head = committed.get("headline", {})
    new_head = fresh["headline"]
    for key in sorted(set(old_head) | set(new_head)):  # type: ignore[arg-type]
        if old_head.get(key) != new_head.get(key):  # type: ignore[union-attr]
            problems.append(
                f"headline.{key}: {old_head.get(key)!r} -> {new_head.get(key)!r}"
            )

    def label_map(payload: dict[str, object]) -> dict[str, dict[str, object]]:
        rows = payload.get("by_label", [])
        return {str(row["label"]): row for row in rows}  # type: ignore[index,union-attr]

    old_labels, new_labels = label_map(committed), label_map(fresh)
    for label in sorted(set(old_labels) | set(new_labels)):
        old_row, new_row = old_labels.get(label), new_labels.get(label)
        if old_row == new_row:
            continue
        if old_row is None:
            problems.append(f"{label}: new label in the scorecard")
        elif new_row is None:
            problems.append(f"{label}: disappeared from the scorecard")
        else:
            for metric in ("recall_strict", "recall_partial", "label_accuracy", "gold"):
                if old_row.get(metric) != new_row.get(metric):
                    problems.append(
                        f"{label}.{metric}: {old_row.get(metric)} -> {new_row.get(metric)}"
                    )
    if problems:
        problems.append(
            "If these changes are intended, rerun "
            "`autocab deid eval --engine "
            f"{card.engine} --write-scorecard` and describe the delta in the PR."
        )
    return problems


# --------------------------------------------------------------------------
# Markdown
# --------------------------------------------------------------------------
_DO_NOT_EDIT = (
    "<!-- Generated by `autocab deid eval --write-scorecard`. Do not hand-edit: "
    "the next run overwrites it. -->"
)

WHAT_THIS_DOES_NOT_PROVE = """\
## What this does not prove

Read this section before quoting any number above.

- **The corpus is synthetic, and its `easy` tier shares shapes with the
  generator that built it.** Several `easy` fixtures were written from the same
  understanding of the same identifier shapes as the regex table, so a high
  `easy` recall is partly a measurement of internal consistency. That is why the
  gate is computed over `easy`+`medium` and why the `hard` tier is reported
  separately and gated far lower.
- **Real OCR garble is worse than the `hard` tier.** The generator substitutes
  at most two digit lookalikes per value. A real screenshot at low DPI drops
  characters, merges columns, and reflows lines.
- **HIPAA identifier 17 (full-face photographs) is entirely uncovered.** A text
  de-identifier does nothing to a face in a video-call window. The control there
  is deletion or source gating -- `retain_images=False` -- not redaction.
- **This is not a Safe Harbor determination.** Safe Harbor requires all 18
  identifier classes removed *and* no actual knowledge that the residual
  information could identify an individual. A recall number establishes neither
  of those, and nothing here substitutes for IRB review or a security review.
- **This measures the detector, not the system.** It says nothing about the
  rewrite applying correctly, the key handling, the crash-recovery path, the
  frame redaction, or the operator who ran it.
- **The floors are regression gates, not safety claims.** `NAME 0.10` does not
  mean a 10% name recall is acceptable; it means "the regex tier does not detect
  names" is now a tested fact and the baseline the model tier must beat.
"""


def render_markdown(
    card: Scorecard,
    thresholds: dict[str, object],
    *,
    latency: Sequence[Latency] = (),
) -> str:
    spec = thresholds.get("engines", {}).get(card.engine, {})  # type: ignore[union-attr]
    floors = spec.get("per_label_recall_strict", {}) if isinstance(spec, dict) else {}
    head = card.headline()

    out: list[str] = [
        "# De-identification evaluation",
        "",
        _DO_NOT_EDIT,
        "",
        f"Engine: **`{card.engine}`** · corpus `{card.corpus_fingerprint[:16]}` · "
        f"{card.records} records · {card.gold_spans} gold spans",
        "",
        "## Headline",
        "",
        "| Metric | Value |",
        "| --- | --- |",
        f"| `safe_record_rate` (records with zero strict misses) | **{head['safe_record_rate']}** |",
        f"| `leaks_per_1000_records` | **{head['leaks_per_1000_records']}** |",
        f"| `recall_strict` (gated: easy+medium) | {head['recall_strict_gated']} |",
        f"| `recall_strict` (all difficulties) | {head['recall_strict_overall']} |",
        f"| `over_redaction_rate` (share of all corpus characters) | {head['over_redaction_rate']} |",
        f"| `over_redaction_share_of_predicted` | {head['over_redaction_share_of_predicted']} |",
        f"| `false_positive_spans_per_negative_record` | {head['false_positive_spans_per_negative_record']} |",
        f"| `must_survive_violations` | {head['must_survive_violations']} / {head['must_survive_total']} |",
        "",
        "`recall_strict` counts a gold span only when **every** character of it was",
        "rewritten. `recall_partial` appears in the per-label table below and is never",
        "gated: a large gap between the two means the detector clips, which is a leak",
        "rather than a partial success. There is deliberately no F1 -- see",
        "`src/autocab/deid/eval/scorer.py`.",
        "",
        "## Per label",
        "",
        "| Label | HIPAA | Gold | `recall_strict` | `recall_partial` | Clip gap | `label_accuracy` | Floor | Margin |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- |",
    ]

    for label in sorted(card.by_label):
        row = card.by_label[label]
        gated = card.by_label_gated.get(label)
        floor = floors.get(label)
        margin = (
            f"{gated.recall_strict - float(floor):+.3f}"
            if floor is not None and gated is not None
            else "-"
        )
        hipaa = HIPAA_BY_LABEL.get(label)
        out.append(
            f"| `{label}` | {hipaa if hipaa is not None else '-'} | {row.gold} | "
            f"{row.recall_strict:.3f} | {row.recall_partial:.3f} | {row.clip_gap:+.3f} | "
            f"{row.label_accuracy:.3f} | {floor if floor is not None else '-'} | {margin} |"
        )

    out += ["", "### By difficulty", "", "| Difficulty | Gold | `recall_strict` | `recall_partial` | Gated |", "| --- | --- | --- | --- | --- |"]
    for key in sorted(card.by_difficulty):
        row = card.by_difficulty[key]
        out.append(
            f"| `{key}` | {row.gold} | {row.recall_strict:.3f} | {row.recall_partial:.3f} | "
            f"{'yes' if key in GATED_DIFFICULTIES else 'no (reported only)'} |"
        )

    out += [
        "",
        "### By channel",
        "",
        "`notes` and `jobs` are the high-risk channels; a corpus-wide average hides them.",
        "",
        "| Channel | Gold | `recall_strict` | `recall_partial` |",
        "| --- | --- | --- | --- |",
    ]
    for key in sorted(card.by_channel):
        row = card.by_channel[key]
        out.append(f"| `{key}` | {row.gold} | {row.recall_strict:.3f} | {row.recall_partial:.3f} |")

    out += [
        "",
        "### HIPAA Safe Harbor rollup",
        "",
        "The language a compliance reviewer reads. An identifier with no labels is",
        "**not covered by this detector at all**.",
        "",
        "| HIPAA | Labels | Gold | `recall_strict` |",
        "| --- | --- | --- | --- |",
    ]
    rollup = hipaa_rollup()
    for identifier in sorted((key for key in rollup if isinstance(key, int))):
        labels = rollup[identifier]
        key = f"hipaa-{identifier:02d}"
        row = card.by_hipaa.get(key)
        gold = row.gold if row else 0
        recall = f"{row.recall_strict:.3f}" if row and row.gold else "no fixtures"
        out.append(
            f"| {identifier} | {', '.join(f'`{label}`' for label in labels)} | {gold} | {recall} |"
        )
    missing = sorted(set(range(1, 19)) - {key for key in rollup if isinstance(key, int)})
    if missing:
        out.append(
            f"| {', '.join(str(item) for item in missing)} | *none* | 0 | "
            "**not covered** |"
        )

    if latency:
        out += [
            "",
            "### Measured CPU latency",
            "",
            "Wall clock on the machine that generated this file. **Not** part of the",
            "committed scorecard snapshot, which must stay deterministic.",
            "",
            "| Engine | ms/KB | KB | seconds |",
            "| --- | --- | --- | --- |",
        ]
        for item in latency:
            out.append(
                f"| `{item.engine}` | {item.ms_per_kb:.2f} | {item.kilobytes:.1f} | {item.seconds:.3f} |"
            )

    out += [
        "",
        "### Utility",
        "",
        "| Metric | Value | Ceiling |",
        "| --- | --- | --- |",
        f"| `over_redaction_rate` | {head['over_redaction_rate']} | "
        f"{spec.get('max_over_redaction_rate', '-') if isinstance(spec, dict) else '-'} |",
        f"| `false_positive_spans_per_negative_record` | "
        f"{head['false_positive_spans_per_negative_record']} | "
        f"{spec.get('max_false_positive_spans_per_negative_record', '-') if isinstance(spec, dict) else '-'} |",
        f"| `must_survive_violations` | {head['must_survive_violations']} | "
        f"{spec.get('must_survive_violations', 0) if isinstance(spec, dict) else 0} |",
        "",
        "Precision has a **ceiling, not a floor**. A floor would let a detector trade",
        "recall for precision; the ceiling only stops it trading the other way.",
        "",
        "## Leak table",
        "",
        f"Every strict miss, all {len(card.leaks)} of them. Safe to print because every",
        "value is drawn from a reserved range or an independent lexicon draw -- see",
        "`data/deid-eval/README.md`.",
        "",
        "| Record | Channel | Difficulty | Label | Gold text | Partial hit |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for leak in sorted(card.leaks, key=lambda item: (item.channel, item.record_id, item.start)):
        text = leak.text.replace("|", "\\|").replace("\n", " ")
        out.append(
            f"| `{leak.record_id}` | {leak.channel} | {leak.difficulty} | `{leak.label}` | "
            f"`{text}` | {'yes' if leak.partial else 'no'} |"
        )
    if not card.leaks:
        out.append("| *none* | | | | | |")

    out += ["", WHAT_THIS_DOES_NOT_PROVE]
    return "\n".join(out) + "\n"


def unavailable_message(exc: EngineUnavailable) -> str:
    return f"{exc.engine} unavailable: {exc.reason}"
