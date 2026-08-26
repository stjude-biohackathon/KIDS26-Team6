"""Helpers for loading the built-in BioHackathon demo data."""

from __future__ import annotations

import json
from pathlib import Path

from .models import SkillReference, WorkflowTrace


def data_dir() -> Path:
    return Path(__file__).resolve().parents[2] / "data"


def load_skill_catalog(path: Path | None = None) -> list[SkillReference]:
    catalog_path = path or data_dir() / "sample_skill_catalog.json"
    payload = json.loads(catalog_path.read_text(encoding="utf-8"))
    return [SkillReference.from_dict(item) for item in payload]


def load_workflow_traces(path: Path | None = None) -> list[WorkflowTrace]:
    trace_path = path or data_dir() / "sample_workflow_traces.json"
    payload = json.loads(trace_path.read_text(encoding="utf-8"))
    return [WorkflowTrace.from_dict(item) for item in payload]
