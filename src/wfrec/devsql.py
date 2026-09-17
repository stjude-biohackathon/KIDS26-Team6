"""Safe, read-only access to DevSQL's normalized local activity tables."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from collections.abc import Callable, Collection
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


DEFAULT_TIMEOUT_SECONDS = 45.0
SELECT_PATTERN = re.compile(r"^select\b", re.IGNORECASE)
CommandRunner = Callable[..., subprocess.CompletedProcess[str]]


class DevSQLError(RuntimeError):
    """Base class for sanitized DevSQL integration failures."""


class DevSQLUnavailableError(DevSQLError):
    """Raised when the DevSQL executable cannot be found or started."""


class DevSQLTimeoutError(DevSQLError):
    """Raised when DevSQL does not finish within the configured timeout."""


class DevSQLExecutionError(DevSQLError):
    """Raised when DevSQL exits unsuccessfully."""

    def __init__(self, return_code: int) -> None:
        self.return_code = return_code
        super().__init__(f"DevSQL exited with status {return_code}.")


class DevSQLQueryError(DevSQLError):
    """Raised when a query is empty or is not read-only."""


class DevSQLResponseError(DevSQLError):
    """Raised when DevSQL returns an unexpected JSON response."""


@dataclass(slots=True)
class DevSQLClient:
    """Run bounded DevSQL queries without exposing activity in errors."""

    executable: Path
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS
    runner: CommandRunner = field(
        default=subprocess.run,
        repr=False,
        compare=False,
    )

    def __post_init__(self) -> None:
        """Reject a timeout that cannot bound the child process."""

        if self.timeout_seconds <= 0:
            raise ValueError("DevSQL timeout must be greater than zero.")

    @classmethod
    def discover(
        cls,
        *,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        runner: CommandRunner = subprocess.run,
    ) -> DevSQLClient:
        """Find DevSQL on ``PATH`` and return a configured client."""

        executable = shutil.which("devsql")
        if executable is None:
            raise DevSQLUnavailableError("DevSQL executable is not available.")
        return cls(
            executable=Path(executable),
            timeout_seconds=timeout_seconds,
            runner=runner,
        )

    def version(self) -> str:
        """Return DevSQL's version string without querying activity data."""

        result = self._run(["--version"])
        version = result.stdout.strip()
        if not version:
            raise DevSQLResponseError("DevSQL returned an empty version response.")
        return version

    def query(
        self,
        sql: str,
        *,
        required_columns: Collection[str] = (),
    ) -> list[dict[str, Any]]:
        """Execute one SELECT query and validate its JSON rows."""

        normalized_sql = sql.strip()
        if not normalized_sql:
            raise DevSQLQueryError("DevSQL query cannot be empty.")
        if SELECT_PATTERN.match(normalized_sql) is None:
            raise DevSQLQueryError("DevSQL client accepts SELECT queries only.")

        result = self._run(["--format", "json", normalized_sql])
        try:
            payload = json.loads(result.stdout)
        except json.JSONDecodeError as error:
            raise DevSQLResponseError("DevSQL returned invalid JSON.") from error

        if not isinstance(payload, list):
            raise DevSQLResponseError("DevSQL JSON response must be an array.")

        rows: list[dict[str, Any]] = []
        expected_columns = set(required_columns)
        for row in payload:
            if not isinstance(row, dict):
                raise DevSQLResponseError(
                    "Every DevSQL JSON row must be an object."
                )
            missing_columns = expected_columns.difference(row)
            if missing_columns:
                missing = ", ".join(sorted(missing_columns))
                raise DevSQLResponseError(
                    f"DevSQL response is missing required columns: {missing}."
                )
            rows.append(row)
        return rows

    def _run(self, arguments: list[str]) -> subprocess.CompletedProcess[str]:
        """Run DevSQL while keeping its output out of raised errors."""

        try:
            result = self.runner(
                [str(self.executable), *arguments],
                check=True,
                capture_output=True,
                encoding="utf-8",
                timeout=self.timeout_seconds,
                errors="replace",
                shell=False,
                stdin=subprocess.DEVNULL,
            )
        except subprocess.TimeoutExpired as error:
            raise DevSQLTimeoutError(
                "DevSQL query exceeded the configured timeout."
            ) from error
        except subprocess.CalledProcessError as error:
            raise DevSQLExecutionError(error.returncode) from error
        except OSError as error:
            raise DevSQLUnavailableError(
                "DevSQL executable could not be started."
            ) from error

        # Injected test runners may not implement ``check=True`` themselves.
        if result.returncode != 0:
            raise DevSQLExecutionError(result.returncode)
        if not isinstance(result.stdout, str):
            raise DevSQLResponseError("DevSQL did not return text output.")
        return result
