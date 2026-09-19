"""Safe, repeatable initialization of an AutoCAB user workspace."""

from __future__ import annotations

import importlib.util
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from autocab import paths
from autocab.deid.config import load as load_deid_config
from autocab.deid.models import ModelWeightsError, WeightStatus, fetch_weights, verify_weights
from autocab.migration import migrate_wfrec_sessions
from autocab.recording.locking import atomic_write_text, file_lock
from autocab.recording.state import RecorderState, resolved_default_analyst

REDACTION_ENGINES = ("regex", "gliner", "gliner2-pii")
_TABLE_PATTERN = re.compile(r"^\s*\[([^]]+)]\s*(?:#.*)?$")
_ENGINE_PATTERN = re.compile(r"^\s*engine\s*=")

DEFAULT_CONFIG = """# AutoCAB user configuration.
# PHI redaction always applies the regex tier. Optional model tiers are set up
# separately so initialization never downloads model weights without consent.
config_version = 1

[deid]
engine = "regex"
profile = "balanced"
"""


@dataclass(frozen=True, slots=True)
class InitResult:
    """Summary of what initialization found and created."""

    home: Path
    config: Path
    config_created: bool
    analyst: str
    redaction_engine: str
    legacy_sessions: int
    next_commands: tuple[str, ...]

    def to_dict(self) -> dict[str, object]:
        """Return a JSON-safe representation for the CLI and tests."""

        payload = asdict(self)
        payload["home"] = str(self.home)
        payload["config"] = str(self.config)
        payload["next_commands"] = list(self.next_commands)
        return payload


def legacy_session_count() -> int:
    """Count discoverable legacy sessions without reading their event data."""

    legacy = paths.legacy_home()
    sessions = legacy / "sessions" if legacy is not None else None
    if sessions is None or not sessions.is_dir():
        return 0
    return sum((child / "manifest.json").is_file() for child in sessions.iterdir())


def _set_redaction_engine(config: Path, engine: str) -> None:
    """Update the one managed TOML key while preserving all other settings."""

    if engine not in REDACTION_ENGINES:
        raise ValueError(f"unknown redaction engine {engine!r}")
    # Reject malformed or credential-bearing configuration before rewriting it.
    load_deid_config(config)
    lines = config.read_text(encoding="utf-8").splitlines()
    section_start = next(
        (
            index
            for index, line in enumerate(lines)
            if (match := _TABLE_PATTERN.match(line)) and match.group(1).strip() == "deid"
        ),
        None,
    )
    setting = f'engine = "{engine}"'
    if section_start is None:
        if lines and lines[-1]:
            lines.append("")
        lines.extend(("[deid]", setting))
    else:
        section_end = next(
            (
                index
                for index in range(section_start + 1, len(lines))
                if _TABLE_PATTERN.match(lines[index])
            ),
            len(lines),
        )
        engine_line = next(
            (
                index
                for index in range(section_start + 1, section_end)
                if _ENGINE_PATTERN.match(lines[index])
            ),
            None,
        )
        if engine_line is None:
            lines.insert(section_start + 1, setting)
        else:
            lines[engine_line] = setting
    atomic_write_text(config, "\n".join(lines) + "\n")
    load_deid_config(config)


def initialize(*, analyst: str = "", redaction_engine: str | None = None) -> InitResult:
    """Create local storage and record the analyst default without side effects.

    Model downloads, shell-profile edits, and legacy-data migration remain
    separate because each has a different consent and failure boundary.
    """

    root = paths.ensure_home()
    config = paths.config_path()
    config_created = False
    with file_lock(root / "config.lock"):
        if not config.exists():
            atomic_write_text(config, DEFAULT_CONFIG)
            config_created = True
        if redaction_engine is not None:
            _set_redaction_engine(config, redaction_engine)

    state = RecorderState.load()
    requested_analyst = analyst.strip() or resolved_default_analyst(state)
    if analyst.strip() or not state.default_analyst.strip():
        state.default_analyst = requested_analyst
        state.save()
    resolved_analyst = resolved_default_analyst(state)

    configured_engine = load_deid_config(config).engine
    if configured_engine not in REDACTION_ENGINES:
        raise ValueError(
            f"unsupported configured redaction engine {configured_engine!r}; "
            f"expected one of {', '.join(REDACTION_ENGINES)}"
        )
    legacy_sessions = legacy_session_count()
    next_commands = []
    if legacy_sessions:
        next_commands.append("autocab migrate --from-wfrec")
    next_commands.extend(('autocab record start --title "My workflow" --watch .', "autocab status"))
    return InitResult(
        home=root,
        config=config,
        config_created=config_created,
        analyst=resolved_analyst,
        redaction_engine=configured_engine,
        legacy_sessions=legacy_sessions,
        next_commands=tuple(next_commands),
    )


def prepare_model(engine: str, *, fetch: bool) -> WeightStatus | None:
    """Verify a selected local model, downloading only when explicitly requested."""

    if engine == "regex":
        if fetch:
            raise ValueError("--fetch-model requires a model-based --redaction choice")
        return None
    if engine not in REDACTION_ENGINES:
        raise ValueError(f"unsupported session redaction engine: {engine}")
    if engine == "gliner2-pii" and importlib.util.find_spec("gliner2") is None:
        raise ModelWeightsError(
            "GLiNER2 requires the optional runtime. Install it with "
            "`uv sync --extra deid-gliner2`, then rerun init."
        )

    status = verify_weights(model=engine)
    if status.valid:
        return status
    if not fetch:
        raise ModelWeightsError(
            f"{status.spec.name} is not installed. Add --fetch-model or choose --redaction regex."
        )
    downloaded = fetch_weights(model=engine)
    if not isinstance(downloaded, WeightStatus):  # pragma: no cover - no bundle was requested
        raise ModelWeightsError(f"the {status.spec.name} download did not install a model")
    return downloaded


def install_shell_hooks() -> list[dict[str, str]]:
    """Install auto-detected shell hooks after the caller obtains consent."""

    from autocab.recording.hookinstall import install

    return install()


def migrate_legacy_sessions() -> dict[str, Any]:
    """Copy legacy sessions after the caller obtains consent."""

    source = paths.legacy_home() or Path.home() / ".wfrec"
    return migrate_wfrec_sessions(source_root=source).to_dict()


def readiness_summary() -> dict[str, Any]:
    """Return a compact, read-only summary of recorder readiness."""

    from autocab.recording.doctor import diagnose

    report = diagnose()
    sources = {
        name: bool(payload.get("available"))
        for name, payload in (report.get("sources") or {}).items()
        if isinstance(payload, dict)
    }
    engine = load_deid_config().engine
    model_ready = True
    if engine != "regex":
        model_ready = verify_weights(model=engine).valid
        if engine == "gliner2-pii":
            model_ready = model_ready and importlib.util.find_spec("gliner2") is not None
    return {
        "available_sources": sum(sources.values()),
        "total_sources": len(sources),
        "sources": sources,
        "redaction_engine": engine,
        "redaction_ready": model_ready,
        "warnings": list(report.get("warnings") or []),
    }
