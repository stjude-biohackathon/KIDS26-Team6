"""The experimental GLiNER2 adapter stays local and preserves exact offsets."""

from __future__ import annotations

from pathlib import Path

import pytest

from autocab.deid.engines.base import EngineUnavailable
from autocab.deid.engines.gliner2_pii import Gliner2Pii


class _Extractor:
    def batch_extract_entities(self, texts, labels, **kwargs):
        assert texts == ["Patient Jane Smith lives in Memphis."]
        assert labels == ["person", "city"]
        assert kwargs["include_spans"] is True
        return [
            {
                "entities": {
                    "person": [{"text": "Jane Smith", "start": 8, "end": 18, "confidence": 0.91}],
                    "city": [{"text": "Memphis", "start": 28, "end": 35, "confidence": 0.86}],
                }
            }
        ]


def test_missing_gliner2_weights_name_the_explicit_setup_command(wfrec_home: Path) -> None:
    with pytest.raises(EngineUnavailable, match="fetch --model gliner2-pii"):
        Gliner2Pii(extractor=_Extractor())


def test_gliner2_maps_supported_labels_to_shared_taxonomy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    weights = tmp_path / "weights"
    weights.mkdir()
    monkeypatch.setattr(
        "autocab.deid.engines.gliner2_pii.resolve_weights",
        lambda *_args, **_kwargs: weights,
    )
    engine = Gliner2Pii(extractor=_Extractor(), model_labels={"person": "NAME", "city": "LOCATION"})

    spans = engine.detect(["Patient Jane Smith lives in Memphis."])[0]

    assert [(span.start, span.end, span.label) for span in spans] == [
        (8, 18, "NAME"),
        (28, 35, "LOCATION"),
    ]
    assert all(span.detector == "gliner2_pii" for span in spans)
