"""Direct ONNX decoding preserves source offsets and bounded input size."""

from __future__ import annotations

import os
from pathlib import Path

import numpy as np
import pytest

from autocab.deid.engines.gliner_onnx import GlinerOnnx, TextWindow
from autocab.deid.eval import corpus as corpus_mod
from autocab.deid.eval import report
from autocab.deid.eval.scorer import score
from autocab.deid.labels import ZERO_SHOT_LABELS


def _bare_engine() -> GlinerOnnx:
    engine = object.__new__(GlinerOnnx)
    engine.max_width = 12
    engine.window_words = 16
    engine.threshold = 0.35
    engine.batch_size = 2
    return engine


def test_long_text_is_split_into_overlapping_windows() -> None:
    engine = _bare_engine()
    text = " ".join(f"word{index}" for index in range(30))

    windows = engine._windows(0, text)

    assert [len(window.tokens) for window in windows] == [16, 16, 16, 15]
    assert windows[0].tokens[-11:] == windows[1].tokens[:11]
    assert windows[-1].offsets[-1][1] == len(text)


def test_decoder_maps_word_predictions_to_half_open_character_offsets() -> None:
    engine = _bare_engine()
    text = "Patient Jane Smith arrived"
    window = engine._windows(0, text)[0]
    logits = np.full(
        (len(window.tokens), engine.max_width, len(ZERO_SHOT_LABELS)),
        -20.0,
        dtype=np.float32,
    )
    name_index = ZERO_SHOT_LABELS.index("person full name")
    logits[1, 1, name_index] = 8.0

    spans = engine._decode(logits, window)

    assert len(spans) == 1
    assert spans[0].label == "NAME"
    assert text[spans[0].start : spans[0].end] == "Jane Smith"


def test_overlapping_window_predictions_are_deduplicated() -> None:
    engine = _bare_engine()
    window = TextWindow(0, ("Jane", "Smith"), ((0, 4), (5, 10)))
    logits = np.full((2, 12, len(ZERO_SHOT_LABELS)), -20.0, dtype=np.float32)
    logits[0, 1, ZERO_SHOT_LABELS.index("person full name")] = 5.0

    first = engine._decode(logits, window)[0]
    duplicate = type(first)(**{**first.to_dict(), "score": 1.0})
    deduplicated = engine._deduplicate([first, duplicate])

    assert len(deduplicated) == 1
    assert deduplicated[0].score == 1.0


@pytest.mark.model
def test_pinned_model_detects_a_name_and_location() -> None:
    model_dir = os.environ.get("AUTOCAB_DEID_MODEL_DIR")
    assert model_dir, "set AUTOCAB_DEID_MODEL_DIR to the verified revision directory"
    text = "Patient Jane Smith lives at 500 Pine Street."

    spans = GlinerOnnx(weights_dir=Path(model_dir)).detect([text])[0]

    labels = {span.label for span in spans}
    assert {"NAME", "LOCATION"} <= labels


@pytest.mark.model
def test_pinned_model_matches_the_committed_scorecard(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    model_dir = os.environ.get("AUTOCAB_DEID_MODEL_DIR")
    assert model_dir, "set AUTOCAB_DEID_MODEL_DIR to the verified revision directory"
    model_home = Path(model_dir).parents[2]
    monkeypatch.setenv("WFREC_HOME", str(model_home))
    loaded = corpus_mod.load()

    predictions, _latency = report.predict(loaded, engine="gliner")
    card = score(loaded, predictions, engine="gliner")

    assert report.check_thresholds(card, report.load_thresholds()) == []
    assert report.compare_scorecard(card) == []
