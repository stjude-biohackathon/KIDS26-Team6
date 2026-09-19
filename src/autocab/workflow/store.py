"""Atomic persistence for forge runs."""

from __future__ import annotations

from contextlib import contextmanager
import json
from pathlib import Path
import re
import shutil
from typing import Any, Iterator

from autocab import paths
from wfrec.locking import atomic_write_text, file_lock

from .models import ForgeRun, WorkflowError

RUN_ID_PATTERN = re.compile(r"^[0-9]{8}T[0-9]{6}Z-[0-9a-f]{8}$")


class RunNotFound(WorkflowError):
    """A requested forge run does not exist."""


class RunStore:
    """Read and write run artifacts beneath the AutoCAB home."""

    def __init__(self, root: Path | None = None) -> None:
        self.root = root or paths.runs_dir()

    def run_dir(self, run_id: str) -> Path:
        """Return a safe path for a validated run identifier."""

        if not RUN_ID_PATTERN.fullmatch(run_id):
            raise ValueError(f"Invalid forge run ID: {run_id!r}")
        return self.root / run_id

    def create(self, run: ForgeRun, *, artifacts: dict[str, Any] | None = None) -> Path:
        """Publish a complete new run directory without exposing partial state."""

        self.root.mkdir(parents=True, exist_ok=True)
        run_dir = self.run_dir(run.run_id)
        staging = self.root / f".{run.run_id}.tmp"
        if run_dir.exists():
            raise FileExistsError(run_dir)
        if staging.exists():
            shutil.rmtree(staging)
        staging.mkdir()
        try:
            (staging / "run.json").write_text(
                json.dumps(run.to_dict(), indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            for name, value in (artifacts or {}).items():
                if Path(name).name != name:
                    raise ValueError("Run artifact name must be a filename.")
                (staging / name).write_text(
                    json.dumps(value, indent=2, sort_keys=True) + "\n",
                    encoding="utf-8",
                )
            staging.replace(run_dir)
        except Exception:
            shutil.rmtree(staging, ignore_errors=True)
            raise
        return run_dir

    def load(self, run_id: str) -> ForgeRun:
        """Load one forge run."""

        path = self.run_dir(run_id) / "run.json"
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise RunNotFound(run_id) from exc
        if not isinstance(payload, dict):
            raise WorkflowError(f"Forge run metadata is not an object: {path}")
        return ForgeRun.from_dict(payload)

    def save(self, run: ForgeRun) -> None:
        """Atomically persist one forge run."""

        path = self.run_dir(run.run_id) / "run.json"
        atomic_write_text(path, json.dumps(run.to_dict(), indent=2, sort_keys=True) + "\n")

    def list(self, *, session_id: str | None = None) -> list[ForgeRun]:
        """Return runs newest first, optionally limited to one session."""

        if not self.root.is_dir():
            return []
        runs: list[ForgeRun] = []
        for path in self.root.iterdir():
            if not path.is_dir() or not RUN_ID_PATTERN.fullmatch(path.name):
                continue
            try:
                run = self.load(path.name)
            except (RunNotFound, WorkflowError, ValueError, KeyError, json.JSONDecodeError):
                continue
            if session_id is None or session_id in run.session_ids:
                runs.append(run)
        return sorted(runs, key=lambda run: run.created_at, reverse=True)

    @contextmanager
    def lock(self, run_id: str) -> Iterator[Path]:
        """Serialize state changes for a run and yield its directory."""

        run_dir = self.run_dir(run_id)
        if not (run_dir / "run.json").is_file():
            raise RunNotFound(run_id)
        with file_lock(run_dir / ".lock"):
            yield run_dir

    def write_json(self, run_id: str, name: str, value: Any) -> Path:
        """Atomically write one named JSON artifact inside a run."""

        if Path(name).name != name:
            raise ValueError("Run artifact name must be a filename.")
        path = self.run_dir(run_id) / name
        atomic_write_text(path, json.dumps(value, indent=2, sort_keys=True) + "\n")
        return path

    def append_review(self, run_id: str, review: dict[str, Any]) -> None:
        """Append one immutable review or approval record."""

        path = self.run_dir(run_id) / "reviews.jsonl"
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(review, sort_keys=True) + "\n")
