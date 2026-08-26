"""Convert terminal workflow logs into normalized AutoCAB workflow traces."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

from .models import WorkflowStep, WorkflowTrace

METADATA_PATTERN = re.compile(r"^#\s*([A-Za-z0-9_-]+)\s*:\s*(.+?)\s*$")
TIMESTAMP_PATTERN = re.compile(
    r"^(?P<timestamp>\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})\s+(?P<command>.+?)\s*$"
)
NON_ALNUM_PATTERN = re.compile(r"[^a-z0-9]+")

COMMAND_TAGS: dict[str, list[str]] = {
    "qc": ["qc", "metrics", "summary"],
    "report": ["report", "markdown", "html", "bundle"],
    "variant": ["variant", "vcf", "bcf"],
    "benchmark": ["benchmark", "truvari", "hap.py", "happy", "giab"],
    "figure": ["figure", "plot", "png", "pdf"],
}

ACTION_HINTS: list[tuple[str, str]] = [
    ("python", "run"),
    ("python3", "run"),
    ("bash", "run"),
    ("sh", "run"),
    ("snakemake", "workflow"),
    ("nextflow", "workflow"),
    ("Rscript", "run"),
    ("jupyter", "notebook"),
    ("grep", "inspect"),
    ("cat", "inspect"),
    ("less", "inspect"),
    ("head", "inspect"),
    ("tail", "inspect"),
    ("ls", "inspect"),
    ("find", "inspect"),
    ("samtools", "analyze"),
    ("bcftools", "analyze"),
    ("bedtools", "analyze"),
    ("awk", "transform"),
    ("sed", "transform"),
    ("cp", "bundle"),
    ("mv", "bundle"),
    ("tar", "bundle"),
    ("zip", "bundle"),
]


@dataclass(slots=True)
class ParsedTerminalSession:
    """Raw terminal session before conversion into the canonical trace model."""

    metadata: dict[str, str]
    commands: list[tuple[str, str]]


def _slugify(text: str) -> str:
    slug = NON_ALNUM_PATTERN.sub("-", text.lower()).strip("-")
    return slug or "workflow"


def _normalize_tag(tag: str) -> str:
    return NON_ALNUM_PATTERN.sub("-", tag.lower()).strip("-")


def _tokenize_command(command: str) -> list[str]:
    return re.findall(r"[A-Za-z0-9._/-]+", command)


def parse_terminal_session(text: str) -> ParsedTerminalSession:
    """Parse a simple terminal workflow log into metadata and commands."""

    metadata: dict[str, str] = {}
    commands: list[tuple[str, str]] = []

    for line_number, raw_line in enumerate(text.splitlines(), start=1):
        line = raw_line.strip()
        if not line:
            continue

        metadata_match = METADATA_PATTERN.match(line)
        if metadata_match:
            key = metadata_match.group(1).strip().lower().replace("-", "_")
            metadata[key] = metadata_match.group(2).strip()
            continue

        command_match = TIMESTAMP_PATTERN.match(line)
        if not command_match:
            raise ValueError(
                "Unsupported terminal log line "
                f"{line_number}: expected '# key: value' or 'YYYY-MM-DDTHH:MM:SS <command>'."
            )

        commands.append((command_match.group("timestamp"), command_match.group("command")))

    if not commands:
        raise ValueError("Terminal log did not contain any timestamped commands.")

    return ParsedTerminalSession(metadata=metadata, commands=commands)


def infer_workflow_tags(commands: list[str], existing_tags: list[str] | None = None) -> list[str]:
    """Infer lightweight workflow tags from command content."""

    ordered_tags: list[str] = []
    seen: set[str] = set()

    def add(tag: str) -> None:
        normalized = _normalize_tag(tag)
        if normalized and normalized not in seen:
            seen.add(normalized)
            ordered_tags.append(normalized)

    for tag in existing_tags or []:
        add(tag)

    joined = " ".join(commands).lower()
    for tag, hints in COMMAND_TAGS.items():
        if any(hint.lower() in joined for hint in hints):
            add(tag)

    for command in commands:
        for token in _tokenize_command(command):
            lower_token = token.lower()
            if re.fullmatch(r"hg\d{3,4}", lower_token):
                add(lower_token)
            elif lower_token == "giab":
                add("giab")

    return ordered_tags[:8]


def command_to_step(timestamp: str, command: str) -> WorkflowStep:
    """Convert one shell command into a normalized workflow step."""

    tokens = _tokenize_command(command)
    executable = tokens[0] if tokens else "terminal"
    executable_lower = executable.lower()
    action = "run"
    for prefix, candidate_action in ACTION_HINTS:
        if executable_lower == prefix.lower():
            action = candidate_action
            break

    detail = f"Executed command: {command}"
    return WorkflowStep(
        timestamp=timestamp,
        tool="terminal",
        action=action,
        detail=detail,
    )


def summarize_commands(commands: list[str], tags: list[str]) -> str:
    """Generate a short summary from the command sequence."""

    tools: list[str] = []
    seen_tools: set[str] = set()
    for command in commands:
        token = _tokenize_command(command)
        if not token:
            continue
        executable = token[0]
        if executable not in seen_tools:
            seen_tools.add(executable)
            tools.append(executable)
        if len(tools) == 3:
            break

    summary_parts = [
        "Terminal-derived workflow trace built from timestamped commands.",
    ]
    if tags:
        summary_parts.append(f"Focus areas: {', '.join(tags[:4])}.")
    if tools:
        summary_parts.append(f"Observed tools: {', '.join(tools)}.")
    return " ".join(summary_parts)


def terminal_session_to_trace(
    session: ParsedTerminalSession,
    *,
    source_path: Path | None = None,
) -> WorkflowTrace:
    """Convert a parsed terminal session into a canonical workflow trace."""

    metadata = session.metadata
    commands = [command for _, command in session.commands]

    existing_tags = [
        part.strip()
        for part in metadata.get("tags", "").split(",")
        if part.strip()
    ]
    tags = infer_workflow_tags(commands, existing_tags)

    title = metadata.get("title") or metadata.get("workflow_family") or "Terminal Workflow Session"
    workflow_family = metadata.get("workflow_family") or _slugify(title).replace("-", " ")
    analyst = metadata.get("analyst", "unknown-analyst")
    trace_id = metadata.get("trace_id")
    if not trace_id:
        trace_id = f"trace-{_slugify(analyst)}-{_slugify(workflow_family)}"

    steps = [command_to_step(timestamp, command) for timestamp, command in session.commands]
    summary = metadata.get("summary") or summarize_commands(commands, tags)

    source = metadata.get("source", "terminal-log")
    if source_path is not None:
        source = f"terminal-log:{source_path.name}"

    return WorkflowTrace(
        trace_id=trace_id,
        analyst=analyst,
        title=title,
        summary=summary,
        tags=tags,
        source=source,
        workflow_family=workflow_family,
        steps=steps,
    )


def convert_terminal_log(path: Path) -> WorkflowTrace:
    """Read one terminal workflow log from disk and return a workflow trace."""

    session = parse_terminal_session(path.read_text(encoding="utf-8"))
    return terminal_session_to_trace(session, source_path=path)


def write_trace_json(trace: WorkflowTrace, output_path: Path) -> Path:
    """Write a single workflow trace as a JSON array for pipeline reuse."""

    output_path.write_text(
        json.dumps([trace.to_dict()], indent=2),
        encoding="utf-8",
    )
    return output_path
