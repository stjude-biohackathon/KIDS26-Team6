"""Local inference with the PII-tuned GLiNER2 checkpoint."""

from __future__ import annotations

import importlib.util
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from typing import Any, Mapping, Sequence

from ..models import GLINER2_PII_SPEC, ModelWeightsError, resolve_weights, verify_weights
from ..spans import DetectorInfo, Span
from .base import EngineUnavailable

DEFAULT_THRESHOLD = 0.97
DEFAULT_BATCH_SIZE = 4

# Only labels with a clear equivalent in wfrec's existing taxonomy are used.
# State and country are intentionally absent because HIPAA Safe Harbor permits
# geographic areas at that granularity.
SUPPORTED_MODEL_LABELS: dict[str, str] = {
    "person": "NAME",
    "date_of_birth": "DOB_LABELLED",
    "email": "EMAIL",
    "phone_number": "PHONE",
    "address": "LOCATION",
    "street_address": "LOCATION",
    "city": "LOCATION",
    "postal_code": "LOCATION",
    "government_id": "SUBJECT_ID",
    "national_id_number": "SUBJECT_ID",
    "passport_number": "SUBJECT_ID",
    "drivers_license_number": "LICENSE",
    "license_number": "LICENSE",
    "tax_id": "SUBJECT_ID",
    "tax_number": "SUBJECT_ID",
    "bank_account": "ACCOUNT",
    "account_number": "ACCOUNT",
    "routing_number": "ACCOUNT",
    "iban": "ACCOUNT",
    "payment_card": "ACCOUNT",
    "card_number": "ACCOUNT",
    "card_expiry": "ACCOUNT",
    "card_cvv": "ACCOUNT",
    "ip_address": "IP",
    "account_id": "SUBJECT_ID",
    "sensitive_account_id": "SUBJECT_ID",
    "sensitive_date": "DATE_BARE",
    "document_date": "DATE_BARE",
    "expiration_date": "DATE_BARE",
    "transaction_date": "DATE_BARE",
}
DEFAULT_MODEL_LABELS = {"person": "NAME"}


class Gliner2Pii:
    """Detect supported PII labels without weakening the regex floor."""

    name = "gliner2_pii"
    kind = "model"
    version = GLINER2_PII_SPEC.revision[:12]

    @classmethod
    def probe(cls) -> DetectorInfo:
        problems: list[str] = []
        if importlib.util.find_spec("gliner2") is None:
            problems.append("install .[deid-gliner2]")
        status = verify_weights(model="gliner2-pii")
        problems.extend(status.problems)
        return cls._info(status.path, problems)

    @classmethod
    def _info(cls, weights_path: Path, problems: Sequence[str] = ()) -> DetectorInfo:
        weights = next(
            item for item in GLINER2_PII_SPEC.files if item.local_name == "model.safetensors"
        )
        return DetectorInfo(
            name=cls.name,
            kind=cls.kind,
            available=not problems,
            version=cls.version,
            reason="; ".join(problems),
            detail={
                "model": GLINER2_PII_SPEC.name,
                "repository": GLINER2_PII_SPEC.repository,
                "revision": GLINER2_PII_SPEC.revision,
                "license": GLINER2_PII_SPEC.license,
                "weights_sha256": weights.sha256,
                "variant": GLINER2_PII_SPEC.variant,
                "weights_path": str(weights_path),
            },
        )

    def __init__(
        self,
        *,
        weights_dir: Path | None = None,
        threshold: float = DEFAULT_THRESHOLD,
        batch_size: int = DEFAULT_BATCH_SIZE,
        model_labels: Mapping[str, str] | None = None,
        extractor: Any | None = None,
    ) -> None:
        if not 0 < threshold < 1:
            raise ValueError("GLiNER2 PII threshold must be between 0 and 1")
        if batch_size < 1:
            raise ValueError("GLiNER2 PII batch size must be positive")

        try:
            self.weights_dir = resolve_weights(weights_dir, model="gliner2-pii")
        except ModelWeightsError as exc:
            raise EngineUnavailable("gliner2-pii", str(exc)) from exc

        if extractor is None:
            try:
                from gliner2 import GLiNER2
            except ImportError as exc:
                raise EngineUnavailable(
                    "gliner2-pii",
                    "install the optional dependency with `uv sync --extra deid-gliner2`",
                ) from exc
            try:
                # GLiNER2 currently prints a configuration banner. Keep library
                # output from corrupting `autocab ... --json` on stdout.
                with redirect_stdout(StringIO()):
                    extractor = GLiNER2.from_pretrained(str(self.weights_dir), map_location="cpu")
            except Exception as exc:
                raise EngineUnavailable("gliner2-pii", f"model load failed: {exc}") from exc

        self.extractor = extractor
        self.threshold = threshold
        self.batch_size = batch_size
        self.model_labels = dict(model_labels or DEFAULT_MODEL_LABELS)
        unknown = self.model_labels.keys() - SUPPORTED_MODEL_LABELS.keys()
        if unknown:
            raise ValueError(f"unsupported GLiNER2 PII labels: {', '.join(sorted(unknown))}")

    def info(self) -> DetectorInfo:
        status = verify_weights(self.weights_dir, model="gliner2-pii")
        return self._info(status.path, status.problems)

    def detect(self, texts: Sequence[str]) -> list[list[Span]]:
        """Return GLiNER2's exact character offsets in the shared taxonomy."""

        if not texts:
            return []
        try:
            predictions = self.extractor.batch_extract_entities(
                list(texts),
                list(self.model_labels),
                batch_size=self.batch_size,
                threshold=self.threshold,
                include_confidence=True,
                include_spans=True,
                overlap_policy="flat",
            )
        except Exception as exc:
            raise EngineUnavailable("gliner2-pii", f"inference failed: {exc}") from exc

        results: list[list[Span]] = []
        for prediction in predictions:
            entities = prediction.get("entities", {})
            spans = [
                Span(
                    int(entity["start"]),
                    int(entity["end"]),
                    self.model_labels[model_label],
                    self.name,
                    float(entity["confidence"]),
                )
                for model_label, values in entities.items()
                if model_label in self.model_labels
                for entity in values
            ]
            results.append(spans)
        return results
