#!/usr/bin/env python3
"""Verify local AutoCAB activity-tracking dependencies without exposing activity."""

from __future__ import annotations

import copy
import json
import logging
import os
import platform
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import ProxyHandler, build_opener


logger = logging.getLogger("AUTOCAB")

ACTIVITYWATCH_BUCKETS_URL = "http://127.0.0.1:5600/api/0/buckets/"
# A first DevSQL query may need to build its local Codex history index.
COMMAND_TIMEOUT_SECONDS = 45
HTTP_TIMEOUT_SECONDS = 3
LOG_FORMAT = (
    "%(color)s[%(levelname)s | %(name)s] "
    "[%(asctime)s | %(module)s - line %(lineno)d]:%(end_color)s %(message)s"
)
LOG_DATE_FORMAT = "%b-%d-%Y at %I:%M:%S %p"
LOG_COLORS = {
    logging.DEBUG: "\033[94m",
    logging.INFO: "\033[32m",
    logging.WARNING: "\033[33m",
    logging.ERROR: "\033[31m",
    logging.CRITICAL: "\033[91m",
}
ANSI_RESET = "\033[0m"


class OrthoStyleFormatter(logging.Formatter):
    """Reproduce the OrthoEvolution console layout using standard logging."""

    def __init__(self, *, use_color: bool) -> None:
        super().__init__(fmt=LOG_FORMAT, datefmt=LOG_DATE_FORMAT)
        self._use_color = use_color

    def format(self, record: logging.LogRecord) -> str:
        """Add level-specific terminal colors without mutating the log record."""

        formatted_record = copy.copy(record)
        formatted_record.color = (
            LOG_COLORS.get(record.levelno, "") if self._use_color else ""
        )
        formatted_record.end_color = ANSI_RESET if self._use_color else ""
        return super().format(formatted_record)


def configure_logging() -> None:
    """Configure one non-root logger with terminal-aware color output."""

    use_color = sys.stderr.isatty() and "NO_COLOR" not in os.environ
    handler = logging.StreamHandler()
    handler.setFormatter(OrthoStyleFormatter(use_color=use_color))

    logger.handlers.clear()
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    logger.propagate = False


@dataclass(frozen=True, slots=True)
class CheckResult:
    """One status line and whether failure should affect the exit code."""

    marker: str
    message: str
    passed: bool
    required: bool = False
    log_level: int = logging.INFO


def find_binary(name: str, fallback_path: Path) -> Path | None:
    """Resolve a binary even before a newly edited shell profile is reloaded."""

    discovered_path = shutil.which(name)
    if discovered_path:
        return Path(discovered_path)
    if fallback_path.is_file() and os.access(fallback_path, os.X_OK):
        return fallback_path
    return None


def binary_is_usable(binary_path: Path | None) -> bool:
    """Confirm that an installed executable can start without printing its output."""

    if binary_path is None:
        return False

    try:
        result = subprocess.run(
            [str(binary_path), "--version"],
            check=False,
            capture_output=True,
            text=True,
            timeout=COMMAND_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0


def file_contains(path: Path, text: str) -> bool:
    """Search configuration in memory without displaying potentially sensitive data."""

    try:
        return path.is_file() and text in path.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return False


def atuin_shell_integration_detected(home_directory: Path) -> bool:
    """Detect supported Atuin initialization statements in common shell profiles."""

    zsh_directory = Path(os.environ.get("ZDOTDIR", home_directory))
    candidates = (
        (home_directory / ".bashrc", "atuin init bash"),
        (zsh_directory / ".zshrc", "atuin init zsh"),
        (home_directory / ".config/fish/config.fish", "atuin init fish"),
    )
    return any(file_contains(path, marker) for path, marker in candidates)


def directory_contains_files(directory: Path, pattern: str) -> bool:
    """Check for history artifacts without opening or reporting their contents."""

    if not directory.is_dir():
        return False
    try:
        next(directory.rglob(pattern))
    except (OSError, StopIteration):
        return False
    return True


def claude_history_detected(home_directory: Path) -> bool:
    """Detect Claude Code history using its documented local locations."""

    claude_directory = home_directory / ".claude"
    return (claude_directory / "history.jsonl").is_file() or directory_contains_files(
        claude_directory / "projects", "*.jsonl"
    )


def codex_history_detected(home_directory: Path) -> bool:
    """Detect Codex history while respecting a custom CODEX_HOME."""

    codex_directory = Path(os.environ.get("CODEX_HOME", home_directory / ".codex"))
    return (
        (codex_directory / "history.jsonl").is_file()
        or directory_contains_files(codex_directory / "sessions", "rollout-*.jsonl*")
        or directory_contains_files(
            codex_directory / "archived_sessions", "rollout-*.jsonl*"
        )
    )


def devsql_query_succeeds(devsql_path: Path | None) -> bool:
    """Run a bounded read-only query and validate JSON without printing activity."""

    if devsql_path is None:
        return False

    # Source metadata is sufficient to prove the table is readable. Avoid
    # returning command text because commands can contain secrets or identifiers.
    query = "SELECT source FROM command_events LIMIT 1"
    try:
        result = subprocess.run(
            [str(devsql_path), "--format", "json", query],
            check=False,
            capture_output=True,
            text=True,
            timeout=COMMAND_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False

    if result.returncode != 0:
        return False

    try:
        json.loads(result.stdout)
    except json.JSONDecodeError:
        return False
    return True


def fetch_activitywatch_buckets() -> dict[str, Any] | None:
    """Read local bucket metadata without retrieving activity events."""

    try:
        # Ignore proxy environment variables so local activity metadata cannot be
        # sent to an external proxy by a workstation configuration.
        local_opener = build_opener(ProxyHandler({}))
        with local_opener.open(
            ACTIVITYWATCH_BUCKETS_URL,
            timeout=HTTP_TIMEOUT_SECONDS,
        ) as response:
            payload = json.load(response)
    except (HTTPError, URLError, TimeoutError, OSError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def vscode_bucket_detected(buckets: dict[str, Any] | None) -> bool:
    """Recognize official VS Code watcher bucket identifiers and metadata."""

    if buckets is None:
        return False

    for bucket_id, metadata in buckets.items():
        if not isinstance(metadata, dict):
            continue
        searchable_values = (
            str(bucket_id),
            str(metadata.get("client", "")),
            str(metadata.get("type", "")),
        )
        if any("aw-watcher-vscode" in value for value in searchable_values):
            return True
    return False


def atuin_agent_hooks_detected(home_directory: Path) -> bool:
    """Detect optional hooks without parsing or displaying unrelated settings."""

    codex_directory = Path(os.environ.get("CODEX_HOME", home_directory / ".codex"))
    return file_contains(
        home_directory / ".claude/settings.json", "atuin hook claude-code"
    ) or file_contains(codex_directory / "hooks.json", "atuin hook codex")


def status_result(
    condition: bool,
    success_message: str,
    missing_message: str,
    *,
    required: bool = False,
) -> CheckResult:
    """Build a consistent success or missing status line."""

    return CheckResult(
        marker="✓" if condition else ("✗" if required else "○"),
        message=success_message if condition else missing_message,
        passed=condition,
        required=required,
        log_level=(
            logging.INFO
            if condition
            else (logging.ERROR if required else logging.WARNING)
        ),
    )


def collect_results() -> list[CheckResult]:
    """Run all local checks in display order."""

    home_directory = Path.home()
    devsql_path = find_binary("devsql", home_directory / ".cargo/bin/devsql")
    atuin_path = find_binary("atuin", home_directory / ".atuin/bin/atuin")
    activitywatch_buckets = fetch_activitywatch_buckets()
    agent_hooks_present = atuin_agent_hooks_detected(home_directory)

    results = [
        status_result(
            platform.system() == "Linux",
            "Linux detected",
            "Linux not detected",
            required=True,
        ),
        status_result(
            binary_is_usable(devsql_path),
            "DevSQL installed",
            "DevSQL missing or unusable",
            required=True,
        ),
        status_result(
            binary_is_usable(atuin_path),
            "Atuin installed",
            "Atuin missing or unusable",
            required=True,
        ),
        status_result(
            atuin_shell_integration_detected(home_directory),
            "Atuin shell integration detected",
            "Atuin shell integration not detected",
            required=True,
        ),
        status_result(
            claude_history_detected(home_directory),
            "Claude Code history detected",
            "Claude Code history not detected",
        ),
        status_result(
            codex_history_detected(home_directory),
            "Codex history detected",
            "Codex history not detected",
        ),
        status_result(
            devsql_query_succeeds(devsql_path),
            "DevSQL command_events query succeeded",
            "DevSQL command_events query failed",
            required=True,
        ),
        status_result(
            activitywatch_buckets is not None,
            "ActivityWatch detected",
            "ActivityWatch not reachable on the local API",
        ),
        status_result(
            vscode_bucket_detected(activitywatch_buckets),
            "VS Code ActivityWatch bucket detected",
            "VS Code ActivityWatch bucket not detected",
        ),
    ]

    if agent_hooks_present:
        results.append(
            CheckResult(
                marker="○",
                message="Atuin agent hooks detected; duplicate records are possible",
                passed=True,
                log_level=logging.WARNING,
            )
        )
    else:
        results.append(
            CheckResult(
                marker="○",
                message="Atuin agent hooks disabled",
                passed=True,
            )
        )
    return results


def main() -> int:
    """Print the verification summary and return failure for required checks."""

    configure_logging()
    logger.info("Starting AutoCAB activity-tracking verification.")
    results = collect_results()

    for result in results:
        logger.log(result.log_level, "%s %s", result.marker, result.message)

    required_failures = sum(
        result.required and not result.passed for result in results
    )
    optional_unavailable = sum(
        not result.required and not result.passed for result in results
    )
    passed_checks = sum(result.passed for result in results)
    summary_message = (
        f"Verification complete: {passed_checks} passed, "
        f"{required_failures} failed, "
        f"{optional_unavailable} optional items unavailable."
    )
    if required_failures:
        logger.error(summary_message)
    else:
        logger.info(summary_message)

    return int(required_failures > 0)


if __name__ == "__main__":
    raise SystemExit(main())
