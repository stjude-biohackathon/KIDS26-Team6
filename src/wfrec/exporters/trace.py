"""Export straight to ``WorkflowTrace`` JSON, the pipeline's native input.

This is the highest-fidelity of the three exports because it skips the two
intermediate text formats, but it is still a projection: ``WorkflowStep`` has
only ``timestamp``/``tool``/``action``/``detail``, so everything else is folded
into ``detail``.
"""

from __future__ import annotations

import json
import re
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
)

ACTION_HINTS = {
    "python": "run", "python3": "run", "bash": "run", "sh": "run",
    "snakemake": "workflow", "nextflow": "workflow", "Rscript": "run",
    "jupyter": "notebook", "grep": "inspect", "cat": "inspect",
    "less": "inspect", "head": "inspect", "tail": "inspect", "ls": "inspect",
    "find": "inspect", "samtools": "analyze", "bcftools": "analyze",
    "bedtools": "analyze", "awk": "transform", "sed": "transform",
    "cp": "bundle", "mv": "bundle", "tar": "bundle", "zip": "bundle",
    "sbatch": "submit", "srun": "submit", "bwa": "align", "minimap2": "align",
    "star": "align", "salmon": "quantify", "fastqc": "qc", "multiqc": "qc",
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


def build_trace(session) -> tuple[dict, int]:
    manifest = session.manifest
    steps: list[dict[str, str]] = []
    corpus: list[str] = []

    for event in session.writer.read_sorted():
        payload = event.payload
        detail = ""
        tool = event.source
        action = "observe"

        if event.type == SHELL_COMMAND:
            command = str(payload.get("command") or "")
            if not command:
                continue
            tool, action = "terminal", _action_for(command)
            bits = [f"Executed command: {command}"]
            if payload.get("cwd"):
                bits.append(f"cwd={payload['cwd']}")
            if payload.get("exit_code") not in (None, ""):
                bits.append(f"exit={payload['exit_code']}")
            if payload.get("duration_ms") not in (None, ""):
                bits.append(f"duration={payload['duration_ms']}ms")
            if payload.get("origin") or event.origin != "local":
                bits.append(f"host={event.host}")
            detail = " ".join(bits)
            corpus.append(command)
        elif event.type == SCREEN_OCR:
            tool, action = "screen", "observe"
            detail = f"Screen text: {payload.get('ocr_text', '')[:1200]}"
            corpus.append(str(payload.get("ocr_text") or ""))
        elif event.type == CONTEXT_NOTE:
            tool, action = "notes", "annotate"
            detail = f"Analyst note ({payload.get('label') or 'context'}): {payload.get('text', '')[:1200]}"
            corpus.append(str(payload.get("text") or ""))
        elif event.type == AGENT_MESSAGE:
            tool, action = str(payload.get("tool") or "agent"), "converse"
            detail = f"{payload.get('role')}: {str(payload.get('text') or '')[:1200]}"
            corpus.append(str(payload.get("text") or ""))
        elif event.type == FILE_DIFF:
            tool, action = "editor", "edit"
            detail = (
                f"Edited {payload.get('path')} (+{payload.get('added')}/"
                f"-{payload.get('deleted')})"
            )
            corpus.append(str(payload.get("path") or ""))
        elif event.type == GIT_SNAPSHOT:
            if not payload.get("changed_count"):
                continue
            tool, action = "git", "version"
            detail = (
                f"Git snapshot ({payload.get('trigger')}) on branch "
                f"{payload.get('branch')}: {payload.get('changed_count')} changed paths"
            )
        elif event.type == JOB_SUBMITTED:
            tool, action = "scheduler", "submit"
            detail = (
                f"Submitted {payload.get('scheduler')} job {payload.get('job_id')} "
                f"{payload.get('jobname', '')} workdir={payload.get('workdir', '')}"
            ).strip()
            corpus.append(detail)
        elif event.type in (SESSION_PAUSED, SESSION_RESUMED, SESSION_WAITING):
            # Waits are workflow signal: "the analyst blocked six hours on an
            # alignment" is exactly the kind of step a skill should encode.
            tool, action = "session", "wait"
            if event.type == SESSION_PAUSED:
                detail = f"Paused: {payload.get('reason') or 'no reason given'} (expected {payload.get('expect') or 'unknown'})"
            elif event.type == SESSION_RESUMED:
                detail = f"Resumed after {payload.get('gap_ms') or 0}ms"
            else:
                detail = f"Still waiting ({payload.get('reason')}) after {payload.get('elapsed_ms', 0)}ms"
        elif event.type == MARKER_USER:
            tool, action = "notes", "mark"
            detail = f"Marker {payload.get('label')}: {payload.get('detail', '')}".strip()
        else:
            continue

        steps.append(
            {
                "timestamp": event.ts,
                "tool": tool,
                "action": action,
                "detail": detail,
            }
        )

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


def write_trace(session, destination: Path | None = None) -> tuple[Path, int]:
    """Write ``exports/trace.json`` as a one-element array, matching
    ``autocab.demo_data.load_workflow_traces``."""

    trace, steps = build_trace(session)
    target = destination or (session.root / "exports" / "trace.json")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps([trace], indent=2) + "\n", encoding="utf-8")
    return target, steps
