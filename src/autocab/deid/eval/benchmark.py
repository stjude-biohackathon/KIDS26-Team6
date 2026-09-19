"""Reproducible accuracy and CPU-runtime benchmarks for de-identification engines.

The committed scorecards remain deterministic safety artifacts. Runtime results
are separate because wall-clock time and process memory depend on the machine.
"""

from __future__ import annotations

import importlib.metadata
import json
import math
import platform
import statistics
import threading
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Sequence, TypeVar

import psutil

from ..models import GLINER2_PII_SPEC, MODEL_NAME, MODEL_REVISION
from ..spans import Detector
from .corpus import Corpus
from .report import build_detectors, predict
from .scorer import Prediction

DEFAULT_REPEATS = 7
_Result = TypeVar("_Result")


@dataclass(frozen=True, slots=True)
class ExactSpanMetrics:
    """Conventional typed exact-match metrics, reported only as diagnostics."""

    true_positives: int
    predicted: int
    gold: int

    @property
    def precision(self) -> float:
        return self.true_positives / self.predicted if self.predicted else 0.0

    @property
    def recall(self) -> float:
        return self.true_positives / self.gold if self.gold else 0.0

    def to_dict(self) -> dict[str, int | float]:
        return {
            "true_positives": self.true_positives,
            "predicted": self.predicted,
            "gold": self.gold,
            "precision": round(self.precision, 4),
            "recall": round(self.recall, 4),
        }


@dataclass(frozen=True, slots=True)
class RuntimeBenchmark:
    engine: str
    corpus_fingerprint: str
    records: int
    kilobytes: float
    repeats: int
    cold_start_seconds: float
    warmup_seconds: float
    warm_seconds: tuple[float, ...]
    median_ms_per_kb: float
    p95_ms_per_kb: float
    median_records_per_second: float
    peak_rss_mb: float
    exact_span: ExactSpanMetrics
    provenance: dict[str, str | float]

    def to_dict(self) -> dict[str, object]:
        payload = asdict(self)
        payload["cold_start_seconds"] = round(self.cold_start_seconds, 4)
        payload["warmup_seconds"] = round(self.warmup_seconds, 4)
        payload["warm_seconds"] = [round(value, 4) for value in self.warm_seconds]
        payload["kilobytes"] = round(self.kilobytes, 2)
        payload["median_ms_per_kb"] = round(self.median_ms_per_kb, 2)
        payload["p95_ms_per_kb"] = round(self.p95_ms_per_kb, 2)
        payload["median_records_per_second"] = round(self.median_records_per_second, 2)
        payload["peak_rss_mb"] = round(self.peak_rss_mb, 2)
        payload["exact_span"] = self.exact_span.to_dict()
        return payload


class _PeakMemory:
    """Sample process RSS while native ONNX code runs outside Python's allocator."""

    def __init__(self) -> None:
        self._process = psutil.Process()
        self._stop = threading.Event()
        self.peak_bytes = self._process.memory_info().rss
        self._thread = threading.Thread(target=self._sample, daemon=True)

    def _sample(self) -> None:
        while not self._stop.wait(0.01):
            self.peak_bytes = max(self.peak_bytes, self._process.memory_info().rss)

    def __enter__(self) -> _PeakMemory:
        self._thread.start()
        return self

    def __exit__(self, *_args: object) -> None:
        self.peak_bytes = max(self.peak_bytes, self._process.memory_info().rss)
        self._stop.set()
        self._thread.join()


def _timed(action: Callable[[], _Result]) -> tuple[_Result, float]:
    started = time.perf_counter()
    result = action()
    return result, time.perf_counter() - started


def _percentile(values: Sequence[float], percentile: float) -> float:
    """Return a linearly interpolated percentile without adding NumPy here."""

    if not values:
        raise ValueError("cannot calculate a percentile from no values")
    ordered = sorted(values)
    position = (len(ordered) - 1) * percentile
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)


def exact_span_metrics(corpus: Corpus, predictions: Sequence[Prediction]) -> ExactSpanMetrics:
    """Score typed, exact boundaries without changing the safety-oriented scorecard."""

    gold = {
        (index, span.start, span.end, span.label)
        for index, record in enumerate(corpus)
        for span in record.spans
    }
    predicted = {
        (index, span.start, span.end, span.label)
        for index, (_rendered, spans) in enumerate(predictions)
        for span in spans
        if not span.protect
    }
    return ExactSpanMetrics(len(gold & predicted), len(predicted), len(gold))


def _provenance(corpus: Corpus, engine: str) -> dict[str, str | float]:
    details: dict[str, str | float] = {
        "measured_at": datetime.now(timezone.utc).isoformat(),
        "corpus_fingerprint": corpus.fingerprint,
        "platform": platform.platform(),
        "machine": platform.machine(),
        "processor": platform.processor() or "not reported",
        "python": platform.python_version(),
        "psutil": importlib.metadata.version("psutil"),
    }
    if engine == "gliner-only" or "gliner" in engine.split("+"):
        from ..engines.gliner_onnx import DEFAULT_THRESHOLD

        details.update(
            {
                "model": MODEL_NAME,
                "model_revision": MODEL_REVISION,
                "threshold": DEFAULT_THRESHOLD,
                "onnxruntime": importlib.metadata.version("onnxruntime"),
            }
        )
    if engine == "gliner2-pii-only" or "gliner2-pii" in engine.split("+"):
        from ..engines.gliner2_pii import DEFAULT_THRESHOLD as GLINER2_THRESHOLD

        details.update(
            {
                "model": GLINER2_PII_SPEC.name,
                "model_revision": GLINER2_PII_SPEC.revision,
                "threshold": GLINER2_THRESHOLD,
                "gliner2": importlib.metadata.version("gliner2"),
                "torch": importlib.metadata.version("torch"),
            }
        )
    return details


def run_engine(corpus: Corpus, engine: str, *, repeats: int = DEFAULT_REPEATS) -> RuntimeBenchmark:
    """Measure model construction once, warm up once, then time repeated inference."""

    if repeats < 1:
        raise ValueError("repeats must be at least 1")

    detectors: list[Detector] = []
    cold_predictions: list[Prediction] = []
    with _PeakMemory() as memory:

        def cold_run() -> list[Prediction]:
            nonlocal detectors
            detectors = build_detectors(engine)
            predictions, _latency = predict(corpus, engine=engine, detectors=detectors)
            return predictions

        cold_predictions, cold_seconds = _timed(cold_run)
        (_warm_predictions, warmup_seconds) = _timed(
            lambda: predict(corpus, engine=engine, detectors=detectors)[0]
        )
        warm_seconds = tuple(
            _timed(lambda: predict(corpus, engine=engine, detectors=detectors)[0])[1]
            for _ in range(repeats)
        )

    kilobytes = sum(len(record.text.encode("utf-8")) for record in corpus) / 1024.0
    median_seconds = statistics.median(warm_seconds)
    return RuntimeBenchmark(
        engine=engine,
        corpus_fingerprint=corpus.fingerprint,
        records=len(corpus),
        kilobytes=kilobytes,
        repeats=repeats,
        cold_start_seconds=cold_seconds,
        warmup_seconds=warmup_seconds,
        warm_seconds=warm_seconds,
        median_ms_per_kb=1000 * median_seconds / kilobytes if kilobytes else 0.0,
        p95_ms_per_kb=1000 * _percentile(warm_seconds, 0.95) / kilobytes if kilobytes else 0.0,
        median_records_per_second=len(corpus) / median_seconds if median_seconds else 0.0,
        peak_rss_mb=memory.peak_bytes / (1024 * 1024),
        exact_span=exact_span_metrics(corpus, cold_predictions),
        provenance=_provenance(corpus, engine),
    )


def write_json(results: Sequence[RuntimeBenchmark], path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps([result.to_dict() for result in results], indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return path
