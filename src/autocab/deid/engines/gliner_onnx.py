"""Lightweight GLiNER inference using the pinned INT8 ONNX graph."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

import numpy as np
import onnxruntime as ort
from tokenizers import Encoding, Tokenizer

from ..labels import LABEL_BY_DESCRIPTION, ZERO_SHOT_LABELS
from ..models import (
    MODEL_FILES,
    MODEL_LICENSE,
    MODEL_NAME,
    MODEL_REPOSITORY,
    MODEL_REVISION,
    SOURCE_REPOSITORY,
    SOURCE_REVISION,
    ModelWeightsError,
    WeightStatus,
    resolve_weights,
    verify_weights,
)
from ..spans import DetectorInfo, Span
from .base import EngineUnavailable

WORD_PATTERN = re.compile(r"\w+(?:[-_]\w+)*|\S", re.UNICODE)
MODEL_FILENAME = "model-int8.onnx"
TOKENIZER_FILENAME = "tokenizer.json"
CONFIG_FILENAME = "gliner_config.json"
DEFAULT_THRESHOLD = 0.35
DEFAULT_BATCH_SIZE = 4
DEFAULT_WINDOW_WORDS = 256


@dataclass(frozen=True, slots=True)
class TextWindow:
    """One overlapping word window with offsets into its source string."""

    text_index: int
    tokens: tuple[str, ...]
    offsets: tuple[tuple[int, int], ...]


class GlinerOnnx:
    """Detect contextual identifiers without PyTorch or network access."""

    name = "gliner_onnx"
    kind = "model"
    version = MODEL_REVISION[:12]

    @classmethod
    def probe(cls) -> DetectorInfo:
        """Report installation state without loading the 183 MB graph."""

        return cls._status_info(verify_weights())

    @classmethod
    def _status_info(cls, status: WeightStatus) -> DetectorInfo:
        weights = next(item for item in MODEL_FILES if item.local_name == MODEL_FILENAME)
        return DetectorInfo(
            name=cls.name,
            kind=cls.kind,
            available=status.valid,
            version=cls.version,
            reason="; ".join(status.problems),
            detail={
                "model": MODEL_NAME,
                "repository": MODEL_REPOSITORY,
                "revision": MODEL_REVISION,
                "source_repository": SOURCE_REPOSITORY,
                "source_revision": SOURCE_REVISION,
                "license": MODEL_LICENSE,
                "weights_sha256": weights.sha256,
                "variant": "int8",
                "weights_path": str(status.path),
            },
        )

    def __init__(
        self,
        *,
        weights_dir: Path | None = None,
        threshold: float = DEFAULT_THRESHOLD,
        batch_size: int = DEFAULT_BATCH_SIZE,
        window_words: int = DEFAULT_WINDOW_WORDS,
    ) -> None:
        if not 0 < threshold < 1:
            raise ValueError("GLiNER threshold must be between 0 and 1")
        if batch_size < 1:
            raise ValueError("GLiNER batch size must be positive")

        try:
            self.weights_dir = resolve_weights(weights_dir)
        except ModelWeightsError as exc:
            raise EngineUnavailable("gliner", str(exc)) from exc

        config = json.loads((self.weights_dir / CONFIG_FILENAME).read_text(encoding="utf-8"))
        self.max_width = int(config["max_width"])
        if window_words < self.max_width:
            raise ValueError("GLiNER window must be at least as wide as its maximum entity span")
        self.threshold = threshold
        self.batch_size = batch_size
        self.window_words = window_words

        self.tokenizer = Tokenizer.from_file(str(self.weights_dir / TOKENIZER_FILENAME))
        self.tokenizer.enable_padding(pad_id=0, pad_token="[PAD]", direction="right")
        options = ort.SessionOptions()
        options.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
        self.session = ort.InferenceSession(
            str(self.weights_dir / MODEL_FILENAME),
            sess_options=options,
            providers=["CPUExecutionProvider"],
        )

    def info(self) -> DetectorInfo:
        return self._status_info(verify_weights(self.weights_dir))

    def detect(self, texts: Sequence[str]) -> list[list[Span]]:
        """Run batched inference and return half-open source-text offsets."""

        results: list[list[Span]] = [[] for _ in texts]
        windows = [
            window for index, text in enumerate(texts) for window in self._windows(index, text)
        ]
        batch_size = self.batch_size
        position = 0
        while position < len(windows):
            current = windows[position : position + batch_size]
            try:
                predictions = self._infer(current)
            except Exception as exc:
                if batch_size > 1 and self._is_memory_error(exc):
                    batch_size = max(1, batch_size // 2)
                    continue
                raise EngineUnavailable("gliner", f"ONNX inference failed: {exc}") from exc
            for window, spans in zip(current, predictions, strict=True):
                results[window.text_index].extend(spans)
            position += len(current)

        return [self._deduplicate(spans) for spans in results]

    def _windows(self, text_index: int, text: str) -> list[TextWindow]:
        words = [
            (match.group(), match.start(), match.end()) for match in WORD_PATTERN.finditer(text)
        ]
        if not words:
            return []
        overlap = self.max_width - 1
        step = self.window_words - overlap
        windows: list[TextWindow] = []
        for start in range(0, len(words), step):
            current = words[start : start + self.window_words]
            windows.append(
                TextWindow(
                    text_index=text_index,
                    tokens=tuple(word for word, _, _ in current),
                    offsets=tuple((begin, end) for _, begin, end in current),
                )
            )
            if start + self.window_words >= len(words):
                break
        return windows

    def _infer(self, windows: Sequence[TextWindow]) -> list[list[Span]]:
        prompt = tuple(
            item for description in ZERO_SHOT_LABELS for item in ("<<ENT>>", description)
        ) + ("<<SEP>>",)
        inputs = [list(prompt + window.tokens) for window in windows]
        encodings = self.tokenizer.encode_batch(
            inputs,
            add_special_tokens=True,
            is_pretokenized=True,
        )
        model_inputs = self._model_inputs(encodings, windows, len(prompt))
        logits = self.session.run([self.session.get_outputs()[0].name], model_inputs)[0]
        return [self._decode(logits[index], window) for index, window in enumerate(windows)]

    def _model_inputs(
        self,
        encodings: Sequence[Encoding],
        windows: Sequence[TextWindow],
        prompt_words: int,
    ) -> dict[str, np.ndarray]:
        input_ids = np.asarray([encoding.ids for encoding in encodings], dtype=np.int64)
        attention_mask = np.asarray(
            [encoding.attention_mask for encoding in encodings], dtype=np.int64
        )
        words_mask = np.asarray(
            [self._word_mask(encoding, prompt_words) for encoding in encodings],
            dtype=np.int64,
        )
        text_lengths = np.asarray([[len(window.tokens)] for window in windows], dtype=np.int64)

        span_rows = [self._span_indices(len(window.tokens)) for window in windows]
        max_spans = max(len(row) for row in span_rows)
        span_idx = np.zeros((len(windows), max_spans, 2), dtype=np.int64)
        span_mask = np.zeros((len(windows), max_spans), dtype=np.bool_)
        for index, row in enumerate(span_rows):
            span_idx[index, : len(row)] = row
            span_mask[index, : len(row)] = row[:, 1] < len(windows[index].tokens)

        return {
            "input_ids": input_ids,
            "attention_mask": attention_mask,
            "words_mask": words_mask,
            "text_lengths": text_lengths,
            "span_idx": span_idx,
            "span_mask": span_mask,
        }

    @staticmethod
    def _word_mask(encoding: Encoding, prompt_words: int) -> list[int]:
        mask: list[int] = []
        previous: int | None = None
        for word_id in encoding.word_ids:
            is_first = word_id is not None and word_id != previous
            if is_first and word_id >= prompt_words:
                mask.append(word_id - prompt_words + 1)
            else:
                mask.append(0)
            previous = word_id
        return mask

    def _span_indices(self, token_count: int) -> np.ndarray:
        starts = np.repeat(np.arange(token_count, dtype=np.int64), self.max_width)
        widths = np.tile(np.arange(self.max_width, dtype=np.int64), token_count)
        return np.column_stack((starts, starts + widths))

    def _decode(self, logits: np.ndarray, window: TextWindow) -> list[Span]:
        probabilities = 1.0 / (1.0 + np.exp(-np.clip(logits, -30, 30)))
        candidates: list[Span] = []
        if probabilities.ndim == 3:
            locations = np.argwhere(probabilities > self.threshold)
            scored = (
                (
                    int(start),
                    int(start + width),
                    int(label),
                    float(probabilities[start, width, label]),
                )
                for start, width, label in locations
            )
        elif probabilities.ndim == 2:
            span_indices = self._span_indices(len(window.tokens))
            locations = np.argwhere(probabilities > self.threshold)
            scored = (
                (
                    int(span_indices[span][0]),
                    int(span_indices[span][1]),
                    int(label),
                    float(probabilities[span, label]),
                )
                for span, label in locations
            )
        else:
            raise ValueError(f"unexpected GLiNER output shape: {probabilities.shape}")

        for start_word, end_word, label_index, score in scored:
            if end_word >= len(window.tokens) or label_index >= len(ZERO_SHOT_LABELS):
                continue
            description = ZERO_SHOT_LABELS[label_index]
            candidates.append(
                Span(
                    start=window.offsets[start_word][0],
                    end=window.offsets[end_word][1],
                    label=LABEL_BY_DESCRIPTION[description],
                    detector="model:gliner_onnx",
                    score=score,
                )
            )
        return self._greedy(candidates)

    @staticmethod
    def _greedy(candidates: Sequence[Span]) -> list[Span]:
        selected: list[Span] = []
        for candidate in sorted(candidates, key=lambda span: (-span.score, span.start, -span.end)):
            if not any(candidate.overlaps(existing) for existing in selected):
                selected.append(candidate)
        return sorted(selected, key=lambda span: (span.start, span.end))

    @classmethod
    def _deduplicate(cls, candidates: Sequence[Span]) -> list[Span]:
        best: dict[tuple[int, int, str], Span] = {}
        for candidate in candidates:
            key = (candidate.start, candidate.end, candidate.label)
            if key not in best or candidate.score > best[key].score:
                best[key] = candidate
        return cls._greedy(tuple(best.values()))

    @staticmethod
    def _is_memory_error(exc: Exception) -> bool:
        message = str(exc).casefold()
        return any(term in message for term in ("memory", "allocate", "bad_alloc"))
