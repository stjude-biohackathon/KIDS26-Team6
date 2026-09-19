"""Safe, repeatable initialization of an AutoCAB user workspace."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path

from autocab import paths
from wfrec.locking import atomic_write_text, file_lock
from wfrec.state import RecorderState, resolved_default_analyst

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
    legacy_sessions: int
    next_commands: tuple[str, ...]

    def to_dict(self) -> dict[str, object]:
        """Return a JSON-safe representation for the CLI and tests."""

        payload = asdict(self)
        payload["home"] = str(self.home)
        payload["config"] = str(self.config)
        payload["next_commands"] = list(self.next_commands)
        return payload


def _legacy_session_count() -> int:
    legacy = paths.legacy_home()
    sessions = legacy / "sessions" if legacy is not None else None
    if sessions is None or not sessions.is_dir():
        return 0
    return sum((child / "manifest.json").is_file() for child in sessions.iterdir())


def initialize(*, analyst: str = "") -> InitResult:
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

    state = RecorderState.load()
    requested_analyst = analyst.strip() or resolved_default_analyst(state)
    if analyst.strip() or not state.default_analyst.strip():
        state.default_analyst = requested_analyst
        state.save()
    resolved_analyst = resolved_default_analyst(state)

    legacy_sessions = _legacy_session_count()
    next_commands = []
    if legacy_sessions:
        next_commands.append("autocab migrate --from-wfrec")
    next_commands.extend(('autocab record start --title "My workflow" --watch .', "autocab status"))
    return InitResult(
        home=root,
        config=config,
        config_created=config_created,
        analyst=resolved_analyst,
        legacy_sessions=legacy_sessions,
        next_commands=tuple(next_commands),
    )
