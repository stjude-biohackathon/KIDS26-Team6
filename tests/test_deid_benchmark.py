"""Benchmark diagnostics stay separate from the safety-gated scorecards."""

from __future__ import annotations

import json
import xml.etree.ElementTree as element_tree
from pathlib import Path

import pytest

from autocab.deid.eval import benchmark, comparison
from autocab.deid.eval import report
from autocab.deid.eval.corpus import Corpus, GoldSpan, Record
from autocab.deid.engines.registry import ENGINE_CHOICES
from autocab.deid.spans import Span
from autocab.cli import build_parser


def _corpus() -> Corpus:
    return Corpus(
        records=(
            Record(
                id="record-1",
                channel="notes",
                difficulty="easy",
                text="Jane Smith",
                spans=(GoldSpan(0, 10, "NAME", "Jane Smith", 1),),
                must_survive=(),
                provenance="test",
                license="CC0-1.0",
            ),
        ),
        fingerprint="fixture-fingerprint",
    )


def _card(engine: str, value: float) -> dict[str, object]:
    return {
        "headline": {
            "engine": engine,
            "corpus_fingerprint": "fixture-fingerprint",
            "records": 10,
            "gold_spans": 12,
            "safe_record_rate": value,
            "leaks_per_1000_records": 1000 * (1 - value),
            "recall_strict_gated": value,
            "recall_strict_overall": value,
            "over_redaction_rate": 0.01,
        },
        "by_label_gated": [{"label": "NAME", "recall_strict": value}],
        "by_channel": [{"label": "notes", "recall_strict": value}],
    }


def test_exact_span_metrics_are_typed_and_secondary() -> None:
    exact = Span(0, 10, "NAME", "test")
    wrong_label = Span(0, 10, "LOCATION", "test")

    metrics = benchmark.exact_span_metrics(_corpus(), [("masked", [exact, wrong_label])])

    assert metrics.true_positives == 1
    assert metrics.precision == 0.5
    assert metrics.recall == 1.0


def test_percentile_interpolates_small_samples() -> None:
    assert benchmark._percentile([1.0, 2.0, 3.0], 0.95) == pytest.approx(2.9)


def test_accuracy_svg_is_deterministic_and_accessible() -> None:
    cards = [
        _card("regex", 0.7),
        _card("gliner-only", 0.8),
        _card("gliner", 0.9),
    ]

    first = comparison.render_accuracy_svg(cards)
    second = comparison.render_accuracy_svg(cards)

    assert first == second
    element_tree.fromstring(first)
    assert 'role="img"' in first
    assert "<title" in first
    assert "<desc" in first
    assert "Regex + GLiNER" in first
    assert "GLiNER only (diagnostic)" in first
    assert "PHI Redaction Accuracy" in first
    assert "Full-coverage rate" in first
    assert "Evaluation metric" in first


def test_comparison_report_names_additive_engine_and_cites_gliner() -> None:
    rendered = comparison.render_markdown(
        [_card("regex", 0.7), _card("gliner-only", 0.8), _card("gliner", 0.9)]
    )

    assert "Regex + GLiNER" in rendered
    assert "GLiNER only (diagnostic)" in rendered
    assert "arXiv:2311.08526" in rendered
    assert "seven runs" in rendered
    assert "Full-coverage recall" in rendered


def test_scorecards_must_share_a_corpus(tmp_path: Path) -> None:
    regex = _card("regex", 0.7)
    gliner = _card("gliner", 0.9)
    gliner["headline"]["corpus_fingerprint"] = "different"  # type: ignore[index]
    (tmp_path / "scorecard.regex.json").write_text(json.dumps(regex), encoding="utf-8")
    (tmp_path / "scorecard.gliner.json").write_text(json.dumps(gliner), encoding="utf-8")

    with pytest.raises(ValueError, match="different corpus fingerprints"):
        comparison.load_scorecards(tmp_path)


def test_scorecards_must_use_the_full_same_corpus(tmp_path: Path) -> None:
    regex = _card("regex", 0.7)
    gliner = _card("gliner", 0.9)
    gliner["headline"]["records"] = 2  # type: ignore[index]
    (tmp_path / "scorecard.regex.json").write_text(json.dumps(regex), encoding="utf-8")
    (tmp_path / "scorecard.gliner.json").write_text(json.dumps(gliner), encoding="utf-8")

    with pytest.raises(ValueError, match="different record or gold-span counts"):
        comparison.load_scorecards(tmp_path)


def test_runtime_benchmark_separates_cold_and_warm_measurements() -> None:
    result = benchmark.run_engine(_corpus(), "regex", repeats=2)

    assert result.repeats == 2
    assert len(result.warm_seconds) == 2
    assert result.cold_start_seconds >= 0
    assert result.warmup_seconds >= 0
    assert result.peak_rss_mb > 0
    assert result.exact_span.gold == 1


class _NameDetector:
    name = "test-name-model"

    def detect(self, texts: list[str]) -> list[list[Span]]:
        return [
            [Span(0, 10, "NAME", "model:test")] if text.startswith("Jane Smith") else []
            for text in texts
        ]


def test_gliner_only_evaluation_bypasses_the_regex_floor() -> None:
    record = Record(
        id="record-1",
        channel="notes",
        difficulty="easy",
        text="Jane Smith MRN 4419902",
        spans=(
            GoldSpan(0, 10, "NAME", "Jane Smith", 1),
            GoldSpan(11, 22, "MRN", "MRN 4419902", 8),
        ),
        must_survive=(),
        provenance="test",
        license="CC0-1.0",
    )
    corpus = Corpus((record,), "fixture-fingerprint")
    detectors = [_NameDetector()]

    model_only, _latency = report.predict(corpus, engine="gliner-only", detectors=detectors)
    additive, _latency = report.predict(corpus, engine="gliner", detectors=detectors)

    assert {span.label for span in model_only[0][1]} == {"NAME"}
    assert {span.label for span in additive[0][1]} == {"NAME", "MRN"}


def test_gliner_only_loads_gliner_but_is_not_a_production_choice(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    detector = _NameDetector()
    loaded: list[str] = []

    def fake_load(name: str) -> _NameDetector:
        loaded.append(name)
        return detector

    monkeypatch.setattr(report, "load_engine", fake_load)

    assert report.build_detectors("gliner-only") == [detector]
    assert loaded == ["gliner"]
    assert "gliner-only" not in ENGINE_CHOICES


def test_gliner2_supports_production_and_model_only_evaluation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    detector = _NameDetector()
    loaded: list[str] = []

    def fake_load(name: str) -> _NameDetector:
        loaded.append(name)
        return detector

    monkeypatch.setattr(report, "load_engine", fake_load)

    assert report.build_detectors("gliner2-pii-only") == [detector]
    assert report.build_detectors("gliner2-pii") == [detector]
    assert loaded == ["gliner2-pii", "gliner2-pii"]
    assert "gliner2-pii" in ENGINE_CHOICES


def test_benchmark_cli_accepts_three_engines_and_output_paths(tmp_path: Path) -> None:
    args = build_parser().parse_args(
        [
            "deid",
            "benchmark",
            "--engine",
            "regex",
            "--engine",
            "gliner-only",
            "--engine",
            "gliner",
            "--output",
            str(tmp_path / "benchmark.json"),
            "--plot",
            str(tmp_path / "benchmark.svg"),
        ]
    )

    assert args.engines == ["regex", "gliner-only", "gliner"]
    assert args.repeats == 7
