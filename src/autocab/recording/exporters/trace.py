"""Export straight to ``WorkflowTrace`` JSON, the pipeline's native input.

This is the highest-fidelity of the three exports because it skips the two
intermediate text formats, but it is still a projection: ``WorkflowStep`` has
only ``timestamp``/``tool``/``action``/``detail``, so everything else is folded
into ``detail``.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from pathlib import Path

from ..events import (
    AGENT_MESSAGE,
    CONTEXT_NOTE,
    FILE_DIFF,
    GIT_SNAPSHOT,
    JOB_SUBMITTED,
    MARKER_USER,
    SCREEN_OCR,
    SESSION_PAUSED,
    SESSION_RESUMED,
    SESSION_WAITING,
    SHELL_COMMAND,
    Event,
)
from ..session import Session

ACTION_HINTS = {
    "python": "run",
    "python3": "run",
    "bash": "run",
    "sh": "run",
    "snakemake": "workflow",
    "nextflow": "workflow",
    "Rscript": "run",
    "jupyter": "notebook",
    "grep": "inspect",
    "cat": "inspect",
    "less": "inspect",
    "head": "inspect",
    "tail": "inspect",
    "ls": "inspect",
    "find": "inspect",
    "samtools": "analyze",
    "bcftools": "analyze",
    "bedtools": "analyze",
    "awk": "transform",
    "sed": "transform",
    "cp": "bundle",
    "mv": "bundle",
    "tar": "bundle",
    "zip": "bundle",
    "sbatch": "submit",
    "srun": "submit",
    "bwa": "align",
    "minimap2": "align",
    "star": "align",
    "salmon": "quantify",
    "fastqc": "qc",
    "multiqc": "qc",
    "git": "version",
}

#: Tag vocabulary, aligned with `autocab.terminal_logs.COMMAND_TAGS` so
#: recorder-derived traces cluster alongside hand-written ones.
TAG_HINTS = {
    "qc": ("fastqc", "multiqc", "qc", "metrics", "samtools stats"),
    "variant": ("bcftools", "gatk", "vcf", "variant", "deepvariant"),
    "benchmark": ("truvari", "hap.py", "happy", "giab", "benchmark"),
    "figure": ("plot", "figure", "matplotlib", "seaborn", ".png", ".pdf"),
    "report": ("report", "markdown", "html", "quarto", "rmarkdown"),
    "alignment": ("bwa", "minimap2", "star", "bowtie", "hisat"),
    "rna": ("salmon", "kallisto", "rsem", "featurecounts", "deseq"),
    "workflow": ("snakemake", "nextflow", "cromwell", "wdl"),
    "hpc": ("sbatch", "srun", "squeue", "sacct", "slurm"),
}


def _slug(text: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return slug or "workflow"


def _action_for(command: str) -> str:
    tokens = re.findall(r"[A-Za-z0-9._/-]+", command)
    if not tokens:
        return "run"
    executable = Path(tokens[0]).name.lower()
    return ACTION_HINTS.get(executable, "run")


def infer_tags(text: str, existing: list[str]) -> list[str]:
    tags: list[str] = []
    seen: set[str] = set()
    for tag in existing:
        normalized = _slug(tag)
        if normalized and normalized not in seen:
            seen.add(normalized)
            tags.append(normalized)
    lowered = text.lower()
    for tag, hints in TAG_HINTS.items():
        if tag in seen:
            continue
        if any(hint in lowered for hint in hints):
            seen.add(tag)
            tags.append(tag)
    for match in re.finditer(r"\bhg\d{3,4}\b", lowered):
        token = match.group(0)
        if token not in seen:
            seen.add(token)
            tags.append(token)
    if "giab" in lowered and "giab" not in seen:
        tags.append("giab")
    return tags[:8]


_TraceProjection = tuple[dict[str, str], str | None]
_EventProjector = Callable[[Event], _TraceProjection | None]


def _projection(
    event: Event,
    *,
    tool: str,
    action: str,
    detail: str,
    corpus_text: str | None = None,
) -> _TraceProjection:
    """Keep the shared trace-step shape separate from event-specific parsing."""

    return (
        {
            "timestamp": event.ts,
            "tool": tool,
            "action": action,
            "detail": detail,
        },
        corpus_text,
    )


def _project_shell_command(event: Event) -> _TraceProjection | None:
    payload = event.payload
    command = str(payload.get("command") or "")
    if not command:
        return None

    detail_parts = [f"Executed command: {command}"]
    if payload.get("cwd"):
        detail_parts.append(f"cwd={payload['cwd']}")
    if payload.get("exit_code") not in (None, ""):
        detail_parts.append(f"exit={payload['exit_code']}")
    if payload.get("duration_ms") not in (None, ""):
        detail_parts.append(f"duration={payload['duration_ms']}ms")
    if payload.get("origin") or event.origin != "local":
        detail_parts.append(f"host={event.host}")
    return _projection(
        event,
        tool="terminal",
        action=_action_for(command),
        detail=" ".join(detail_parts),
        corpus_text=command,
    )


def _project_screen_ocr(event: Event) -> _TraceProjection:
    text = event.payload.get("ocr_text", "")
    return _projection(
        event,
        tool="screen",
        action="observe",
        detail=f"Screen text: {text[:1200]}",
        corpus_text=str(text or ""),
    )


def _project_context_note(event: Event) -> _TraceProjection:
    payload = event.payload
    text = payload.get("text", "")
    return _projection(
        event,
        tool="notes",
        action="annotate",
        detail=f"Analyst note ({payload.get('label') or 'context'}): {text[:1200]}",
        corpus_text=str(text or ""),
    )


def _project_agent_message(event: Event) -> _TraceProjection:
    payload = event.payload
    text = str(payload.get("text") or "")
    return _projection(
        event,
        tool=str(payload.get("tool") or "agent"),
        action="converse",
        detail=f"{payload.get('role')}: {text[:1200]}",
        corpus_text=text,
    )


def _project_file_diff(event: Event) -> _TraceProjection:
    payload = event.payload
    path = str(payload.get("path") or "")
    return _projection(
        event,
        tool="editor",
        action="edit",
        detail=(
            f"Edited {payload.get('path')} (+{payload.get('added')}/-{payload.get('deleted')})"
        ),
        corpus_text=path,
    )


def _project_git_snapshot(event: Event) -> _TraceProjection | None:
    payload = event.payload
    if not payload.get("changed_count"):
        return None
    return _projection(
        event,
        tool="git",
        action="version",
        detail=(
            f"Git snapshot ({payload.get('trigger')}) on branch "
            f"{payload.get('branch')}: {payload.get('changed_count')} changed paths"
        ),
    )


def _project_job_submission(event: Event) -> _TraceProjection:
    payload = event.payload
    detail = (
        f"Submitted {payload.get('scheduler')} job {payload.get('job_id')} "
        f"{payload.get('jobname', '')} workdir={payload.get('workdir', '')}"
    ).strip()
    return _projection(
        event,
        tool="scheduler",
        action="submit",
        detail=detail,
        corpus_text=detail,
    )


def _project_session_wait(event: Event) -> _TraceProjection:
    payload = event.payload
    if event.type == SESSION_PAUSED:
        detail = (
            f"Paused: {payload.get('reason') or 'no reason given'} "
            f"(expected {payload.get('expect') or 'unknown'})"
        )
    elif event.type == SESSION_RESUMED:
        detail = f"Resumed after {payload.get('gap_ms') or 0}ms"
    else:
        detail = f"Still waiting ({payload.get('reason')}) after {payload.get('elapsed_ms', 0)}ms"
    return _projection(event, tool="session", action="wait", detail=detail)


def _project_marker(event: Event) -> _TraceProjection:
    payload = event.payload
    detail = f"Marker {payload.get('label')}: {payload.get('detail', '')}".strip()
    return _projection(event, tool="notes", action="mark", detail=detail)


_EVENT_PROJECTORS: dict[str, _EventProjector] = {
    SHELL_COMMAND: _project_shell_command,
    SCREEN_OCR: _project_screen_ocr,
    CONTEXT_NOTE: _project_context_note,
    AGENT_MESSAGE: _project_agent_message,
    FILE_DIFF: _project_file_diff,
    GIT_SNAPSHOT: _project_git_snapshot,
    JOB_SUBMITTED: _project_job_submission,
    SESSION_PAUSED: _project_session_wait,
    SESSION_RESUMED: _project_session_wait,
    SESSION_WAITING: _project_session_wait,
    MARKER_USER: _project_marker,
}


def build_trace(session: Session) -> tuple[dict[str, object], int]:
    manifest = session.manifest
    steps: list[dict[str, str]] = []
    corpus: list[str] = []

    for event in session.writer.read_sorted():
        projector = _EVENT_PROJECTORS.get(event.type)
        if projector is None:
            # Unsupported events and the seal record are timeline metadata,
            # not workflow steps.
            continue
        projection = projector(event)
        if projection is None:
            continue
        step, corpus_text = projection
        steps.append(step)
        if corpus_text is not None:
            corpus.append(corpus_text)

    title = manifest.title or f"Session {session.session_id}"
    joined = " ".join(corpus)
    trace = {
        "trace_id": f"wfrec-{_slug(manifest.analyst)}-{session.session_id}",
        "analyst": manifest.analyst,
        "title": title,
        "summary": (
            f"wfrec-recorded workflow with {len(steps)} steps on {manifest.host} "
            f"({manifest.platform.get('system', 'unknown')}); "
            f"{round(manifest.active_seconds)}s active, "
            f"{round(manifest.paused_seconds)}s waiting."
        ),
        "tags": infer_tags(joined, manifest.tags),
        "source": f"wfrec:{session.session_id}",
        "workflow_family": manifest.workflow_family or _slug(title).replace("-", " "),
        "steps": steps,
    }
    return trace, len(steps)


def write_trace(
    session: Session,
    destination: Path | None = None,
) -> tuple[Path, int]:
    """Write ``exports/workflow-trace.json`` as a one-element array, matching
    ``autocab.demo_data.load_workflow_traces``."""

    trace, steps = build_trace(session)
    target = destination or (session.root / "exports" / "workflow-trace.json")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps([trace], indent=2) + "\n", encoding="utf-8")
    return target, steps
