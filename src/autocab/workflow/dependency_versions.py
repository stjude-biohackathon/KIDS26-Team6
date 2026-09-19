"""Detect installed dependency versions without installing or importing tools."""

from __future__ import annotations

from dataclasses import dataclass
from importlib import metadata
import re
import shutil
import subprocess
import sys


VERSION_PATTERN = re.compile(r"(?<![0-9A-Za-z])v?(\d+(?:\.\d+)+(?:[-+._][0-9A-Za-z.-]+)?)")
VERSION_ARGUMENTS = {
    "bcftools": ("--version",),
    "docker": ("--version",),
    "git": ("--version",),
    "java": ("-version",),
    "nextflow": ("-version",),
    "node": ("--version",),
    "npm": ("--version",),
    "pip": ("--version",),
    "pip3": ("--version",),
    "python": ("--version",),
    "python3": ("--version",),
    "quarto": ("--version",),
    "r": ("--version",),
    "ruff": ("--version",),
    "samtools": ("--version",),
    "snakemake": ("--version",),
    "uv": ("--version",),
}


@dataclass(frozen=True)
class DetectedVersion:
    """Describe a detected version and the local evidence used to find it."""

    value: str
    source: str


def _version_from_text(value: str) -> str | None:
    """Extract the first dotted version from standard version output."""

    match = VERSION_PATTERN.search(value)
    return match.group(1) if match else None


def _python_distribution_version(executable: str) -> DetectedVersion | None:
    """Use installed Python metadata before starting another process."""

    entry_points = metadata.entry_points(group="console_scripts")
    for entry_point in entry_points:
        if entry_point.name != executable or entry_point.dist is None:
            continue
        distribution_name = entry_point.dist.metadata.get("Name", executable)
        return DetectedVersion(
            entry_point.dist.version,
            f"installed Python distribution {distribution_name}",
        )
    try:
        return DetectedVersion(
            metadata.version(executable),
            f"installed Python distribution {executable}",
        )
    except metadata.PackageNotFoundError:
        return None


def _known_tool_version(executable: str) -> DetectedVersion | None:
    """Run a bounded version-only probe for a known command-line tool."""

    arguments = VERSION_ARGUMENTS.get(executable.lower())
    executable_path = shutil.which(executable)
    if arguments is None or executable_path is None:
        return None
    try:
        result = subprocess.run(
            [executable_path, *arguments],
            capture_output=True,
            check=False,
            text=True,
            timeout=3,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    version = _version_from_text(f"{result.stdout}\n{result.stderr}")
    if version is None:
        return None
    return DetectedVersion(version, f"{executable} {' '.join(arguments)}")


def detect_dependency_version(executable: str) -> DetectedVersion | None:
    """Return a best-effort local version with no imports or network access."""

    if executable.lower() in {"python", "python3"}:
        version = ".".join(str(value) for value in sys.version_info[:3])
        return DetectedVersion(version, "running Python interpreter")
    return _python_distribution_version(executable) or _known_tool_version(executable)
