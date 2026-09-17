"""What to *do* with a detected span: actions, render modes, profiles.

Detection answers "is this an identifier". This module answers "and therefore
what". The two are separate because the same detection feeds two paths with
genuinely different obligations -- capture-time masking must stay byte-compatible
with what AutoCAB already consumes, while the seal rewrites destructively.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

from .labels import GENERALIZED_LABELS, DeidLabel


class Action(str, Enum):
    """What happens to the matched region."""

    KEEP = "keep"
    """Left verbatim. Used for workforce identity and allowlisted science."""

    MASK = "mask"
    """Replaced with ``[REDACTED_<LABEL>]``. Not reversible, not linkable."""

    PSEUDONYMIZE = "pseudonymize"
    """Replaced with ``LABEL_a1b2c3``. Not reversible, but *linkable within the
    run*, which is what keeps a workflow trace analyzable after sealing."""

    GENERALIZE = "generalize"
    """Replaced with a coarser value that is still true: ``[DATE:2012]``,
    ``AGE_90_PLUS``. Only for dates and ages."""


class RenderMode(str, Enum):
    """Which replacement vocabulary a pass uses.

    The compatibility contract:

    ============================================  =================  =====================
    Path                                          Mode               Output
    ============================================  =================  =====================
    ``SensitiveDataRedactor`` / cluster redaction  ``mask``           ``[REDACTED_<LABEL>]``
    ``wfrec`` inline capture                      ``mask``           ``[REDACTED_<LABEL>]``
    the seal pass                                 ``pseudonymize``   ``NAME_a1b2c3``
    ============================================  =================  =====================

    **Only the seal pseudonymizes.** Pseudonyms need a key that is stable across
    processes and restarts, which an in-memory key cannot give (the daemon and
    ``--no-daemon`` are different processes) and which a key file beside the data
    would supply by recreating the exact re-identification key that discarding
    the key exists to prevent. So capture masks, and the seal -- one process,
    one ephemeral key, start to finish -- pseudonymizes.
    """

    MASK = "mask"
    PSEUDONYMIZE = "pseudonymize"


#: Profiles, in increasing strictness. ``balanced`` is the default.
PROFILES: tuple[str, ...] = ("regex-only", "balanced", "strict", "limited-dataset")


@dataclass(frozen=True, slots=True)
class Policy:
    """Resolved de-identification policy for one pass."""

    profile: str = "balanced"
    render: RenderMode = RenderMode.MASK
    pseudonymize_analyst: bool = False
    """``analyst`` and ``host`` are KEEP by default: they identify the
    *workforce*, not the PHI subject, and ``WorkflowClusterer`` groups on
    ``trace.analyst`` (``framework/components.py``) -- pseudonymizing it breaks
    the clustering the recorder exists to feed. This flag exists because some
    sites do treat staff identity as in scope. It gets second-guessed in review,
    which is why the reasoning lives here rather than in a commit message."""

    box_precision: str = "line"
    """``line`` blacks out the whole OCR line box; ``proportional`` narrows it by
    character ratio. Per-glyph interpolation inside a line box is guesswork and
    over-redacting a line costs nothing."""

    overrides: frozenset[str] = field(default_factory=frozenset)
    """Active ``--allow-*`` escape hatches. **Ignored under ``strict``**, where
    passing one is an error rather than a no-op -- a silently dropped override
    would make ``strict`` look satisfied when it was asked to be lenient."""

    def __post_init__(self) -> None:
        if self.profile not in PROFILES:
            raise ValueError(
                f"unknown profile {self.profile!r}; expected one of {', '.join(PROFILES)}"
            )
        if self.box_precision not in ("line", "proportional"):
            raise ValueError(f"unknown box precision {self.box_precision!r}")
        if self.profile == "strict" and self.overrides:
            raise ValueError(
                "profile 'strict' forbids --allow-* overrides; "
                f"got {', '.join(sorted(self.overrides))}"
            )

    @property
    def strict(self) -> bool:
        return self.profile == "strict"

    def allows(self, override: str) -> bool:
        """Whether ``override`` is in force. Always ``False`` under ``strict``."""

        return not self.strict and override in self.overrides

    def action_for(self, label: str) -> Action:
        """Map a label to its action under this policy."""

        if label in GENERALIZED_LABELS:
            return Action.GENERALIZE
        if label == DeidLabel.DENY.value:
            # A deny term is **always** masked, never pseudonymized, even during
            # a seal. A surrogate exists to preserve linkage -- "this is the same
            # value as that" -- and a site-denied vocabulary word has no linkage
            # worth preserving: knowing that `pathology` recurs is not an
            # analytic. Pseudonymizing it produced `ID_9f8a7ed5c621: NAME_e481...`
            # where `[REDACTED_TERM]: NAME_e481...` says the same thing and
            # reads, and it inflated `distinct_values` with vocabulary.
            return Action.MASK
        if self.render is RenderMode.PSEUDONYMIZE:
            return Action.PSEUDONYMIZE
        return Action.MASK


#: The default capture-time policy: mask, balanced, no overrides.
CAPTURE_POLICY = Policy(profile="balanced", render=RenderMode.MASK)

#: The default seal policy.
SEAL_POLICY = Policy(profile="balanced", render=RenderMode.PSEUDONYMIZE)


def assurance_for(profile: str, *, model_tier: bool, degraded: bool) -> str:
    """The ``assurance`` string stamped into ``seal.json``.

    Fail closed means *refuse to produce a sealed artifact*: a degraded run is
    ``partial``, **never** ``verified``. Keeping that mapping in one function
    stops a future caller inventing a third, friendlier word for degraded.
    """

    if degraded:
        return "partial"
    if profile == "regex-only" or not model_tier:
        return "regex-only"
    return "verified"


__all__ = [
    "Action",
    "CAPTURE_POLICY",
    "DeidLabel",
    "PROFILES",
    "Policy",
    "RenderMode",
    "SEAL_POLICY",
    "assurance_for",
]
