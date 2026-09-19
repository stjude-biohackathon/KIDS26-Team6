"""Engine lookup by CLI name.

``surrogate_guard`` and ``regex_rules`` are **not** in this table on purpose.
They always run, first and second, and no ``--engine`` value substitutes for
them; putting them here would make them look selectable. ``--engine llm`` swaps
the *model* tier only.

An engine whose module is absent reports ``unavailable: <reason>`` rather than
raising at import time, because ``wfrec doctor`` has to be able to describe a
machine where the optional tiers are not installed.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from ..spans import Detector, DetectorInfo
from .base import EngineUnavailable

#: ``--engine`` choices. ``gliner+llm`` is a composition, resolved by the seal.
ENGINE_CHOICES: tuple[str, ...] = ("regex", "gliner", "gliner+llm", "llm", "torch", "presidio")


@dataclass(frozen=True, slots=True)
class EngineEntry:
    name: str
    module: str
    factory: str
    extra: str
    """The install extra that provides it, or ``""`` when it is a base
    dependency. Layer 1 of the LLM gate *is* this field being unsatisfiable."""

    description: str


ENTRIES: dict[str, EngineEntry] = {
    "gliner": EngineEntry(
        "gliner",
        "autocab.deid.engines.gliner_onnx",
        "GlinerOnnx",
        "",
        "Zero-shot ONNX NER over the labels.py descriptions. The default model tier.",
    ),
    "torch": EngineEntry(
        "torch",
        "autocab.deid.engines.transformers_ner",
        "TransformersNer",
        "deid-torch",
        "obi/deid_roberta_i2b2. Highest clinical recall, heaviest install.",
    ),
    "presidio": EngineEntry(
        "presidio",
        "autocab.deid.engines.presidio_engine",
        "PresidioEngine",
        "deid-presidio",
        "Presidio analyzer. Its validators are already adopted; the framework is opt-in.",
    ),
    "llm": EngineEntry(
        "llm",
        "autocab.deid.engines.llm_findings",
        "LlmFindings",
        "provider-specific",
        "Additive LLM tier. OFF by default, egress-classified, five-layer gate.",
    ),
}


def available_engines() -> dict[str, DetectorInfo]:
    """Probe every optional engine. Never raises.

    Feeds the ``deid`` row in ``wfrec doctor`` and the engine list in
    ``seal.json``.
    """

    out: dict[str, DetectorInfo] = {}
    for name in ENTRIES:
        try:
            engine = load(name)
        except EngineUnavailable as exc:
            out[name] = DetectorInfo(name=name, kind="model", available=False, reason=exc.reason)
        else:
            out[name] = engine.info()
    return out


def load(name: str, **kwargs: object) -> Detector:
    """Construct the engine called ``name``.

    Raises :class:`EngineUnavailable` -- with the install command in the reason
    -- when the module or its dependencies are absent. Callers on the seal path
    turn that into a refusal to write a sealed artifact.
    """

    entry = ENTRIES.get(name)
    if entry is None:
        raise EngineUnavailable(name, f"unknown engine; expected one of {', '.join(ENTRIES)}")
    try:
        module = __import__(entry.module, fromlist=[entry.factory])
    except ImportError as exc:
        hint = (
            f"pip install -e '.[{entry.extra}]'"
            if entry.extra and entry.extra != "provider-specific"
            else "see docs/deid-evaluation.md"
        )
        raise EngineUnavailable(entry.name, f"{exc}; {hint}") from exc
    factory: Callable[..., Detector] = getattr(module, entry.factory)
    return factory(**kwargs)
