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
    "arXiv:2311.08526 (2023). https://arxiv.org/abs/2311.08526"
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


def render_markdown(cards: Sequence[Mapping[str, object]]) -> str:
    if not cards:
        raise ValueError("at least one scorecard is required")
    engines = [str(card["headline"]["engine"]) for card in cards]  # type: ignore[index]
    fingerprint = str(cards[0]["headline"]["corpus_fingerprint"])  # type: ignore[index]
    header = "| Metric | " + " | ".join(display_name(engine) for engine in engines) + " |"
    rule = "| --- | " + " | ".join("---:" for _ in engines) + " |"

    metrics = (
        ("Safe records", "safe_record_rate", 4),
        ("Leaks per 1,000 records", "leaks_per_1000_records", 2),
        ("Full-coverage recall, gated", "recall_strict_gated", 4),
        ("Full-coverage recall, all", "recall_strict_overall", 4),
        ("Over-redaction rate", "over_redaction_rate", 4),
    )
    out = [
        "# De-identification evaluation",
        "",
        "<!-- Generated from every data/deid-eval/scorecard.*.json snapshot. Do not hand-edit. -->",
        "",
        f"Corpus `{fingerprint[:16]}` · {cards[0]['headline']['records']} records · "  # type: ignore[index]
        f"{cards[0]['headline']['gold_spans']} gold spans",  # type: ignore[index]
        "",
        "![Accuracy comparison for the available de-identification engines](deid-accuracy.svg)",
        "",
        "## Headline comparison",
        "",
        header,
        rule,
    ]
    for label, key, digits in metrics:
        values = [_format(card["headline"][key], digits) for card in cards]  # type: ignore[index]
        out.append(f"| {label} | " + " | ".join(values) + " |")

    out += [
        "",
        "Full-coverage recall counts a gold span only when every character is rewritten. "
        "Partial overlap remains a leak. The gated result uses the easy and medium tiers.",
        "`Regex + GLiNER` is additive: the regex rules always run, then GLiNER adds contextual "
        "findings.",
        "`GLiNER only` is a diagnostic evaluation mode. It is not available for production "
        "capture or sealing.",
        "`Regex + GLiNER2 PII` is a supported local sealing choice. The model-only mode is "
        "diagnostic and does not bypass the production regex floor.",
        "The tested GLiNER2 PII configuration requests only `person` at a 0.97 threshold. "
        "A local prompt and threshold sweep found that broader PII prompts increased false "
        "positives without improving the additive redaction result.",
        "",
        "## Recall by label",
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
        "## Recall by channel",
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
        "## Remaining full-coverage misses",
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
        "## Runtime benchmark",
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
        "## What this does not prove",
        "",
        "- The corpus is synthetic. It does not establish performance on external clinical corpora.",
        "- A text detector does not remove faces or identifiers embedded only in images.",
        "- These measurements are not a HIPAA Safe Harbor determination.",
        "- The scorecards measure the detector, not storage, key handling, or operator behavior.",
        "- Thresholds are regression controls, not claims that missed identifiers are acceptable.",
        "",
        "## Reference",
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
        ("Safe records", "safe_record_rate"),
        ("Gated full coverage", "recall_strict_gated"),
        ("Overall full coverage", "recall_strict_overall"),
    )
    width = 960
    group_height = max(92, len(cards) * 27 + 28)
    height = 160 + len(metrics) * group_height
    left, chart_width = 250, 620
    out = _svg_start(
        "De-identification accuracy comparison",
        "Grouped bars compare safe-record rate and full-coverage recall from zero to one.",
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
    report_path = docs / "deid-evaluation.md"
    plot_path = docs / "deid-accuracy.svg"
    report_path.write_text(render_markdown(cards), encoding="utf-8")
    plot_path.write_text(render_accuracy_svg(cards), encoding="utf-8")
    return report_path, plot_path
