"""Tiny custom entrypoint used only for static dependency discovery tests."""

from pathlib import Path
import subprocess

from helper_module import outputSuffix


def run(inputBed: Path, outputBed: Path) -> None:
    """Run a public interval command for the test fixture.

    Args:
        inputBed (Path): Input interval file.
        outputBed (Path): Output interval file.
    """
    if outputBed.suffix != outputSuffix():
        raise ValueError("Unexpected output suffix.")
    subprocess.run(["bedtools", "sort", "-i", str(inputBed)], check=True)
