"""Shared orchestration for applying configured PHI redaction to a session."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from autocab.deid.engines.registry import load as load_engine
from autocab.deid.policy import Policy, RenderMode

from .seal import SealResult, seal_session
from .session import Session


def apply_phi_redaction(
    session: Session,
    *,
    engine: str,
    profile: str = "balanced",
    reseal: bool = False,
    force: bool = False,
    dry_run: bool = False,
    stop_session: Callable[[], Any] | None = None,
) -> SealResult:
    """Seal one session with the requested engine and the mandatory regex floor."""

    engine_names = [name for name in engine.split("+") if name and name != "regex"]
    detectors = [load_engine(name) for name in engine_names]
    return seal_session(
        session,
        detectors=detectors,
        policy=Policy(profile=profile, render=RenderMode.PSEUDONYMIZE),
        engine_label=engine,
        reseal=reseal,
        force=force,
        dry_run=dry_run,
        stop_session=stop_session,
    )
