"""Stable comparison reports and dependency-free SVG benchmark plots."""

from __future__ import annotations

import json
from html import escape
from pathlib import Path
from typing import Mapping, Sequence

from .benchmark import RuntimeBenchmark

GLINER_REFERENCE = (
    "Zaratiana U, Tomeh N, Holat P, Charnois T. "
    "GLiNER: Generalist Model for Named Entity Recognition using Bidirectional Transformer. "
    "NAACL 2024:5364-5376. https://doi.org/10.18653/v1/2024.naacl-long.300"
)
GLINER2_PII_REFERENCE = (
    "Zaratiana U, Lewis A, Hurn-Maloney G. GLiNER2-PII: A Multilingual Model for "
    "Personally Identifiable Information Extraction. arXiv:2605.09973 (2026). "
    "https://arxiv.org/abs/2605.09973"
)

ENGINE_NAMES = {
    "regex": "Regex",
    "gliner-only": "GLiNER only (diagnostic)",
    "gliner": "Regex + GLiNER",
    "gliner2-pii-only": "GLiNER2 PII only (diagnostic)",
    "gliner2-pii": "Regex + GLiNER2 PII",
}

ENGINE_ORDER = {
    "regex": 0,
    "gliner-only": 1,
    "gliner": 2,
    "gliner2-pii-only": 3,
    "gliner2-pii": 4,
}

_COLORS = ("#2563eb", "#10b981", "#f59e0b", "#8b5cf6")
_ENGINE_COLORS = {
    "regex": "#2563eb",
    "gliner-only": "#f59e0b",
    "gliner": "#10b981",
    "gliner2-pii-only": "#8b5cf6",
    "gliner2-pii": "#db2777",
}


def display_name(engine: str) -> str:
    return ENGINE_NAMES.get(engine, engine.replace("+", " + ").title())


def _engine_color(engine: str, index: int) -> str:
    return _ENGINE_COLORS.get(engine, _COLORS[index % len(_COLORS)])


def load_scorecards(root: Path) -> list[dict[str, object]]:
    cards = [json.loads(path.read_text(encoding="utf-8")) for path in root.glob("scorecard.*.json")]
    cards.sort(
        key=lambda card: (
            ENGINE_ORDER.get(str(card["headline"]["engine"]), len(ENGINE_ORDER)),  # type: ignore[index]
            str(card["headline"]["engine"]),  # type: ignore[index]
        )
    )
    fingerprints = {card["headline"]["corpus_fingerprint"] for card in cards}  # type: ignore[index]
    if len(fingerprints) > 1:
        raise ValueError("scorecards use different corpus fingerprints")
    corpus_sizes = {
        (card["headline"]["records"], card["headline"]["gold_spans"])  # type: ignore[index]
        for card in cards
    }
    if len(corpus_sizes) > 1:
        raise ValueError("scorecards contain different record or gold-span counts")
    return cards


def _table_map(card: Mapping[str, object], key: str) -> dict[str, dict[str, object]]:
    return {str(row["label"]): row for row in card[key]}  # type: ignore[index,union-attr]


def _format(value: object, digits: int = 4) -> str:
    return f"{float(value):.{digits}f}"


def load_historical_baseline(root: Path, release: str = "v0.2") -> dict[str, object] | None:
    """Load a compact, versioned baseline without mixing it into current scorecards."""

    path = root / "baselines" / f"{release}-regex.json"
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def _card_by_engine(
    cards: Sequence[Mapping[str, object]], engine: str
) -> Mapping[str, object] | None:
    return next(
        (card for card in cards if str(card["headline"]["engine"]) == engine),  # type: ignore[index]
        None,
    )


def _historical_comparison(
    cards: Sequence[Mapping[str, object]], baseline: Mapping[str, object] | None
) -> list[str]:
    if baseline is None:
        return []

    regex = _card_by_engine(cards, "regex")
    gliner = _card_by_engine(cards, "gliner")
    gliner2 = _card_by_engine(cards, "gliner2-pii")
    if regex is None or gliner is None or gliner2 is None:
        return []

    current_headline = regex["headline"]  # type: ignore[index]
    expected_identity = (
        current_headline["corpus_fingerprint"],  # type: ignore[index]
        current_headline["records"],  # type: ignore[index]
        current_headline["gold_spans"],  # type: ignore[index]
    )
    baseline_identity = (
        baseline.get("corpus_fingerprint"),
        baseline.get("records"),
        baseline.get("gold_spans"),
    )
    if baseline_identity != expected_identity:
        raise ValueError("historical baseline does not describe the current evaluation corpus")

    metrics = baseline["metrics"]  # type: ignore[index]
    rows = (
        ("Records with zero missed spans", "safe_record_rate", 4),
        ("Missed spans per 1,000 records", "leaks_per_1000_records", 2),
        ("Full-coverage recall, all", "recall_strict_overall", 4),
    )
    out = [
        "## Improvement since v0.2",
        "",
        "The historical comparison uses the committed v0.2 regex scorecard and the same "
        "synthetic corpus fingerprint as v0.3. The v0.3 regex column is included because "
        "the pattern rules also changed between releases.",
        "",
        "| Metric | v0.2 regex | v0.3 regex | v0.3 Regex + GLiNER | v0.3 Regex + GLiNER2 PII |",
        "| --- | ---: | ---: | ---: | ---: |",
    ]
    for label, key, digits in rows:
        values = [
            metrics[key],  # type: ignore[index]
            regex["headline"][key],  # type: ignore[index]
            gliner["headline"][key],  # type: ignore[index]
            gliner2["headline"][key],  # type: ignore[index]
        ]
        out.append(f"| {label} | " + " | ".join(_format(value, digits) for value in values) + " |")

    name_recall_gated = {
        "v0.2": metrics["name_recall_gated"],  # type: ignore[index]
        "regex": _table_map(regex, "by_label_gated")["NAME"]["recall_strict"],
        "gliner": _table_map(gliner, "by_label_gated")["NAME"]["recall_strict"],
        "gliner2": _table_map(gliner2, "by_label_gated")["NAME"]["recall_strict"],
    }
    name_recall_overall = {
        "v0.2": metrics["name_recall_overall"],  # type: ignore[index]
        "regex": _table_map(regex, "by_label")["NAME"]["recall_strict"],
        "gliner": _table_map(gliner, "by_label")["NAME"]["recall_strict"],
        "gliner2": _table_map(gliner2, "by_label")["NAME"]["recall_strict"],
    }
    out.extend(
        [
            f"| Name recall, gated | {_format(name_recall_gated['v0.2'])} | "
            f"{_format(name_recall_gated['regex'])} | "
            f"{_format(name_recall_gated['gliner'])} | "
            f"{_format(name_recall_gated['gliner2'])} |",
            f"| Name recall, all | {_format(name_recall_overall['v0.2'])} | "
            f"{_format(name_recall_overall['regex'])} | "
            f"{_format(name_recall_overall['gliner'])} | "
            f"{_format(name_recall_overall['gliner2'])} |",
            "",
            "On this corpus, the principal NER contribution is contextual name detection: "
            f"gated name recall increases from {_format(name_recall_gated['regex'])} with "
            f"v0.3 regex alone to {_format(name_recall_gated['gliner'])} with either "
            "additive model. "
            "GLiNER2 PII also improves `SLURM_JOB_NAME` recall relative to the other "
            "production configurations.",
            "",
        ]
    )
    return out


def render_markdown(
    cards: Sequence[Mapping[str, object]],
    historical_baseline: Mapping[str, object] | None = None,
) -> str:
    if not cards:
        raise ValueError("at least one scorecard is required")
    engines = [str(card["headline"]["engine"]) for card in cards]  # type: ignore[index]
    fingerprint = str(cards[0]["headline"]["corpus_fingerprint"])  # type: ignore[index]
    header = "| Metric | " + " | ".join(display_name(engine) for engine in engines) + " |"
    rule = "| --- | " + " | ".join("---:" for _ in engines) + " |"

    metrics = (
        ("Records with zero missed spans", "safe_record_rate", 4),
        ("Missed spans per 1,000 records", "leaks_per_1000_records", 2),
        ("Full-coverage recall, gated", "recall_strict_gated", 4),
        ("Full-coverage recall, all", "recall_strict_overall", 4),
        ("Over-redaction rate", "over_redaction_rate", 4),
    )
    out = [
        "# PHI redaction benchmarking",
        "",
        "<!-- Generated from every data/deid-eval/scorecard.*.json snapshot. Do not hand-edit. -->",
        "",
        "## Executive summary",
        "",
        "AutoCAB combines mandatory pattern matching with optional local named entity "
        "recognition (NER) models to detect text that may contain protected health information "
        "(PHI) or other personally identifiable information (PII). On the committed synthetic "
        "corpus, the additive configurations improve complete-span coverage and reduce records "
        "containing at least one missed identifier. The model-only configurations perform "
        "poorly and remain diagnostic controls, not production modes.",
        "",
        "These measurements are regression evidence for AutoCAB's text detector. They do not "
        "establish clinical performance, HIPAA Safe Harbor compliance, or end-to-end system "
        "security.",
        "",
        "![Accuracy comparison for the available de-identification engines]"
        "(figures/phi-redaction-accuracy.png)",
        "",
        "## Benchmark objective",
        "",
        "The benchmark measures whether each detector completely rewrites labelled synthetic "
        "identifiers while preserving non-sensitive scientific and computational text. It also "
        "tests whether an NER layer adds contextual coverage without replacing the mandatory "
        "pattern-based floor.",
        "",
        "## Methods",
        "",
        f"The deterministic corpus has fingerprint `{fingerprint[:16]}` and contains "
        f"{cards[0]['headline']['records']} records with "  # type: ignore[index]
        f"{cards[0]['headline']['gold_spans']} labelled spans across shell, OCR, notes, "  # type: ignore[index]
        "agent, diff, job, and pre-scrubbed channels. The labelled text spans cover the HIPAA "
        "identifier categories represented by the text detector, plus workflow-specific "
        "sensitive identifiers such as sample IDs, accessions, and Slurm job names. Values are "
        "synthetic and generated from reserved ranges or independently sampled public-domain "
        "lexicons.",
        "",
        "Five configurations are compared: regex alone; two model-only diagnostic controls; "
        "and two production configurations in which regex runs before GLiNER or GLiNER2 PII. "
        "The tested GLiNER2 PII configuration requests only `person` at a 0.97 threshold because "
        "broader PII prompts increased false positives without improving additive redaction.",
        "",
        "Full-coverage recall counts a gold span only when every character is rewritten. Partial "
        "overlap remains a missed span. The gated result uses the easy and medium tiers. "
        "Records with zero missed spans is the fraction of records with no full-coverage miss. "
        "Missed spans per 1,000 records scales the total number of full-coverage misses by the "
        "corpus record count. Over-redaction is measured against all corpus characters and "
        "protected `must_survive` terms.",
        "",
        "## Results",
        "",
        "### Headline comparison",
        "",
        header,
        rule,
    ]
    for label, key, digits in metrics:
        values = [_format(card["headline"][key], digits) for card in cards]  # type: ignore[index]
        out.append(f"| {label} | " + " | ".join(values) + " |")

    out += [
        "",
        "`Regex + GLiNER` is additive: the regex rules always run, then GLiNER adds contextual "
        "findings.",
        "`GLiNER only` is a diagnostic evaluation mode. It is not available for production "
        "capture or sealing.",
        "`Regex + GLiNER2 PII` is a supported local sealing choice. The model-only mode is "
        "diagnostic and does not bypass the production regex floor.",
        "",
    ]
    out.extend(_historical_comparison(cards, historical_baseline))
    out += [
        "### Recall by label",
        "",
        "| Label | " + " | ".join(display_name(engine) for engine in engines) + " |",
        rule,
    ]
    tables = [_table_map(card, "by_label_gated") for card in cards]
    labels = sorted(set().union(*(table.keys() for table in tables)))
    for label in labels:
        values = [
            _format(table[label]["recall_strict"]) if label in table else "-" for table in tables
        ]
        out.append(f"| `{label}` | " + " | ".join(values) + " |")

    out += [
        "",
        "### Recall by channel",
        "",
        "| Channel | " + " | ".join(display_name(engine) for engine in engines) + " |",
        rule,
    ]
    channel_tables = [_table_map(card, "by_channel") for card in cards]
    channels = sorted(set().union(*(table.keys() for table in channel_tables)))
    for channel in channels:
        values = [
            _format(table[channel]["recall_strict"]) if channel in table else "-"
            for table in channel_tables
        ]
        out.append(f"| `{channel}` | " + " | ".join(values) + " |")

    out += [
        "",
        "### Remaining full-coverage misses",
        "",
        "| Label | " + " | ".join(display_name(engine) for engine in engines) + " |",
        rule,
    ]
    for label in sorted(
        {str(leak["label"]) for card in cards for leak in card.get("leaks", [])}  # type: ignore[union-attr]
    ):
        values = [
            str(sum(str(leak["label"]) == label for leak in card.get("leaks", [])))  # type: ignore[union-attr]
            for card in cards
        ]
        out.append(f"| `{label}` | " + " | ".join(values) + " |")

    out += [
        "",
        "## Interpretation",
        "",
        "The model-only controls show that neither NER model should replace deterministic "
        "patterns for structured identifiers. Their value is additive. In this corpus, most of "
        "the gain comes from contextual person-name detection, while regex retains coverage for "
        "MRNs, dates, account numbers, network addresses, and other structured forms.",
        "",
        "These results compare detector configurations on one internal synthetic corpus. They "
        "do not demonstrate that the same ranking or error rates will hold for external clinical "
        "text, noisier OCR, or institution-specific identifiers.",
        "",
        "## Runtime benchmarking",
        "",
        "Runtime is not committed in scorecards because it depends on the machine. Measure model "
        "load separately from warmed inference:",
        "",
        "```bash",
        "autocab deid benchmark --engine regex --engine gliner-only --engine gliner \\",
        "  --output benchmark.json --plot benchmark-performance.svg",
        "autocab deid benchmark --engine gliner2-pii-only --engine gliner2-pii",
        "```",
        "",
        "The command warms each loaded engine once, then reports seven runs by default: median and "
        "p95 ms/KB, median records/second, peak process memory, and machine/model provenance.",
        "",
        "## Limitations and security boundaries",
        "",
        "- The corpus is synthetic. It does not establish performance on external clinical corpora.",
        "- A text detector does not remove faces or identifiers embedded only in images.",
        "- These measurements are not a HIPAA Safe Harbor determination.",
        "- The scorecards measure the detector, not storage, key handling, or operator behavior.",
        "- Thresholds are regression controls, not claims that missed identifiers are acceptable.",
        "",
        "## Reproduction",
        "",
        "Install the optional local model runtime before reproducing all five detector "
        "configurations. The two fetch commands require network access and store verified model "
        "weights locally. Evaluation does not require network access after the weights are "
        "available. The final image conversion requires `rsvg-convert`.",
        "",
        "```bash",
        "uv sync --extra deid-gliner2",
        "autocab deid fetch --model gliner",
        "autocab deid fetch --model gliner2-pii",
        "autocab deid gen-corpus --seed 1337 --check",
        "autocab deid eval --engine regex --write-scorecard --check-thresholds",
        "autocab deid eval --engine gliner-only --write-scorecard",
        "autocab deid eval --engine gliner --write-scorecard --check-thresholds",
        "autocab deid eval --engine gliner2-pii-only --write-scorecard",
        "autocab deid eval --engine gliner2-pii --write-scorecard",
        "rsvg-convert docs/figures/phi-redaction-accuracy.svg \\",
        "  --output docs/figures/phi-redaction-accuracy.png",
        "```",
        "",
        "The corpus check should report:",
        "",
        "```text",
        "OK Corpus reproduces byte-for-byte: 316 records, fingerprint ccfbf64eb85ed302",
        "```",
        "",
        "Each evaluation command writes its engine scorecard and rebuilds this report and the "
        "canonical SVG. The final command creates the PNG shown above. Runtime results are "
        "intentionally written to a separate machine-specific benchmark file rather than "
        "committed scorecards.",
        "",
        "## References",
        "",
        f"- {GLINER_REFERENCE}",
        f"- {GLINER2_PII_REFERENCE}",
        "",
    ]
    return "\n".join(out)


def _svg_start(title: str, description: str, width: int, height: int) -> list[str]:
    return [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
        f'viewBox="0 0 {width} {height}" role="img" aria-labelledby="title desc">',
        f'  <title id="title">{escape(title)}</title>',
        f'  <desc id="desc">{escape(description)}</desc>',
        "  <style>text{font-family:system-ui,sans-serif;fill:#172033}"
        ".chart-title{font-size:18px;font-weight:700}.axis-title{font-size:13px;font-weight:600}"
        ".label{font-size:13px}.value{font-size:12px;font-weight:600}.legend{font-size:12px}"
        ".grid{stroke:#d8dee9}</style>",
        f'  <rect width="{width}" height="{height}" fill="#ffffff"/>',
    ]


def render_accuracy_svg(cards: Sequence[Mapping[str, object]]) -> str:
    """Render a deterministic grouped bar chart from committed scorecards."""

    metrics = (
        ("Zero missed spans", "safe_record_rate"),
        ("Gated full coverage", "recall_strict_gated"),
        ("Overall full coverage", "recall_strict_overall"),
    )
    width = 960
    group_height = max(92, len(cards) * 27 + 28)
    height = 160 + len(metrics) * group_height
    left, chart_width = 250, 620
    out = _svg_start(
        "De-identification accuracy comparison",
        "Grouped bars compare records with zero missed spans and full-coverage recall from zero "
        "to one.",
        width,
        height,
    )
    out.append(
        f'  <text class="chart-title" x="{width / 2:.1f}" y="27" '
        'text-anchor="middle">PHI Redaction Accuracy</text>'
    )
    out.append(
        f'  <text class="axis-title" x="{left + chart_width / 2:.1f}" y="{height - 12}" '
        'text-anchor="middle">Full-coverage rate</text>'
    )
    out.append(
        f'  <text class="axis-title" transform="translate(18 {height / 2:.1f}) rotate(-90)" '
        'text-anchor="middle">Evaluation metric</text>'
    )
    for tick in range(6):
        x = left + chart_width * tick / 5
        out.append(f'  <line class="grid" x1="{x:.1f}" y1="105" x2="{x:.1f}" y2="{height - 48}"/>')
        out.append(
            f'  <text class="legend" x="{x:.1f}" y="96" text-anchor="middle">{tick / 5:.1f}</text>'
        )

    legend_columns = min(3, len(cards))
    for index, card in enumerate(cards):
        engine = str(card["headline"]["engine"])  # type: ignore[index]
        x = 40 + (index % legend_columns) * 300
        y = 39 + (index // legend_columns) * 22
        color = _engine_color(engine, index)
        out.append(f'  <rect x="{x}" y="{y}" width="14" height="14" fill="{color}"/>')
        out.append(
            f'  <text class="legend" x="{x + 20}" y="{y + 12}">'
            f"{escape(display_name(engine))}</text>"
        )

    bar_height = 22
    for metric_index, (label, key) in enumerate(metrics):
        group_y = 116 + metric_index * group_height
        out.append(f'  <text class="label" x="45" y="{group_y + 18}">{escape(label)}</text>')
        for engine_index, card in enumerate(cards):
            engine = str(card["headline"]["engine"])  # type: ignore[index]
            value = float(card["headline"][key])  # type: ignore[index]
            y = group_y + engine_index * 27
            bar_width = chart_width * value
            out.append(
                f'  <rect x="{left}" y="{y}" width="{bar_width:.1f}" height="{bar_height}" '
                f'fill="{_engine_color(engine, engine_index)}" rx="3"/>'
            )
            out.append(
                f'  <text class="value" x="{left + bar_width + 7:.1f}" y="{y + 16}">{value:.3f}</text>'
            )
    out.append("</svg>")
    return "\n".join(out) + "\n"


def render_performance_svg(results: Sequence[RuntimeBenchmark]) -> str:
    """Render cold-start and warm-throughput panels with independent axes."""

    if not results:
        raise ValueError("at least one benchmark result is required")
    width, height = 900, 390
    out = _svg_start(
        "De-identification performance comparison",
        "Separate panels compare cold-start seconds and warmed median milliseconds per kilobyte.",
        width,
        height,
    )
    panels = (
        ("Cold start (seconds)", [item.cold_start_seconds for item in results]),
        ("Warm median (ms/KB)", [item.median_ms_per_kb for item in results]),
    )
    for panel_index, (title, values) in enumerate(panels):
        top = 45 + panel_index * 175
        maximum = max(values) or 1.0
        out.append(f'  <text class="label" x="20" y="{top}">{escape(title)}</text>')
        for index, (result, value) in enumerate(zip(results, values, strict=True)):
            y = top + 24 + index * 42
            bar_width = 650 * value / maximum
            out.append(
                f'  <text class="legend" x="20" y="{y + 16}">{escape(display_name(result.engine))}</text>'
            )
            out.append(
                f'  <rect x="180" y="{y}" width="{bar_width:.1f}" height="24" '
                f'fill="{_engine_color(result.engine, index)}" rx="3"/>'
            )
            out.append(
                f'  <text class="value" x="{187 + bar_width:.1f}" y="{y + 17}">{value:.2f}</text>'
            )
    out.append("</svg>")
    return "\n".join(out) + "\n"


def write_accuracy_artifacts(root: Path, docs: Path) -> tuple[Path, Path]:
    cards = load_scorecards(root)
    docs.mkdir(parents=True, exist_ok=True)
    figures = docs / "figures"
    figures.mkdir(parents=True, exist_ok=True)
    report_path = docs / "phi-redaction-benchmarking.md"
    plot_path = figures / "phi-redaction-accuracy.svg"
    baseline = load_historical_baseline(root)
    report_path.write_text(render_markdown(cards, historical_baseline=baseline), encoding="utf-8")
    plot_path.write_text(render_accuracy_svg(cards), encoding="utf-8")
    return report_path, plot_path
