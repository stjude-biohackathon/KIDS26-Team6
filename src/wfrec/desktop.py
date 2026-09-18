"""Small, dependency-free helpers for local desktop integration."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path


class DirectoryOpenError(RuntimeError):
    """Raised when the host cannot open a directory in its file manager."""


class DirectorySelectionError(RuntimeError):
    """Raised when the host cannot present a usable folder picker."""


EXPORT_DIRECTORY_PROMPT = "Choose a folder for exported events"


def choose_directory() -> Path | None:
    """Ask the daemon host to choose an existing export directory.

    The control UI may run in an ordinary browser, so the chooser belongs to
    the daemon host rather than to browser JavaScript. Returning ``None`` for a
    user cancellation keeps that normal action distinct from a desktop error.
    """

    if sys.platform == "darwin":
        return _run_directory_picker(
            [
                "/usr/bin/osascript",
                "-e",
                (
                    "POSIX path of (choose folder with prompt "
                    f'"{EXPORT_DIRECTORY_PROMPT}")'
                ),
            ],
            cancel_return_codes={1},
        )

    if sys.platform == "win32":  # pragma: no cover - exercised with a mock
        powershell = next(
            (
                executable
                for name in ("powershell.exe", "powershell", "pwsh.exe", "pwsh")
                if (executable := shutil.which(name)) is not None
            ),
            None,
        )
        if powershell is None:
            raise DirectorySelectionError("PowerShell is unavailable.")
        script = (
            "Add-Type -AssemblyName System.Windows.Forms; "
            "$dialog = New-Object System.Windows.Forms.FolderBrowserDialog; "
            f"$dialog.Description = '{EXPORT_DIRECTORY_PROMPT}'; "
            "$dialog.ShowNewFolderButton = $true; "
            "if ($dialog.ShowDialog() -eq "
            "[System.Windows.Forms.DialogResult]::OK) { "
            "[Console]::OutputEncoding = [System.Text.Encoding]::UTF8; "
            "Write-Output $dialog.SelectedPath; exit 0 }; exit 2"
        )
        return _run_directory_picker(
            [powershell, "-NoProfile", "-STA", "-Command", script],
            cancel_return_codes={2},
        )

    if sys.platform.startswith("linux"):
        if not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
            raise DirectorySelectionError(
                "No graphical desktop is available on this Linux host."
            )
        zenity = shutil.which("zenity")
        if zenity is not None:
            return _run_directory_picker(
                [
                    zenity,
                    "--file-selection",
                    "--directory",
                    f"--title={EXPORT_DIRECTORY_PROMPT}",
                ],
                cancel_return_codes={1},
            )
        kdialog = shutil.which("kdialog")
        if kdialog is not None:
            return _run_directory_picker(
                [kdialog, "--getexistingdirectory", ".", EXPORT_DIRECTORY_PROMPT],
                cancel_return_codes={1},
            )
        raise DirectorySelectionError(
            "A graphical folder picker requires zenity or kdialog on Linux."
        )

    raise DirectorySelectionError(
        f"Choosing export folders is unsupported on {sys.platform}."
    )


def open_directory(path: Path) -> None:
    """Open an existing directory with the host platform's file manager."""

    directory = path.expanduser().resolve()
    if not directory.is_dir():
        raise DirectoryOpenError(f"Session folder does not exist: {directory}")

    if sys.platform == "win32":  # pragma: no cover - exercised with a mock
        startfile = getattr(os, "startfile", None)
        if startfile is None:
            raise DirectoryOpenError("Windows Explorer is unavailable.")
        try:
            startfile(str(directory))
        except OSError as error:
            raise DirectoryOpenError(
                f"Could not open the session folder: {error}"
            ) from error
        return

    if sys.platform == "darwin":
        _launch_file_manager(["open", str(directory)])
        return

    if sys.platform.startswith("linux"):
        if not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
            raise DirectoryOpenError(
                "No graphical desktop is available on this Linux host."
            )
        opener = shutil.which("xdg-open")
        if opener is None:
            raise DirectoryOpenError("xdg-open is not installed on this Linux host.")
        _launch_file_manager([opener, str(directory)])
        return

    raise DirectoryOpenError(f"Opening folders is unsupported on {sys.platform}.")


def _run_directory_picker(
    command: list[str], *, cancel_return_codes: set[int]
) -> Path | None:
    """Run a platform picker and normalize its selected directory."""

    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError as error:
        raise DirectorySelectionError(
            f"Could not open the folder picker: {error}"
        ) from error

    if result.returncode in cancel_return_codes:
        return None
    if result.returncode != 0:
        detail = result.stderr.strip() or "the picker exited unexpectedly"
        raise DirectorySelectionError(f"Could not choose an export folder: {detail}")

    selected = result.stdout.strip()
    if not selected:
        raise DirectorySelectionError("The folder picker returned no directory.")
    directory = Path(selected).expanduser().resolve()
    if not directory.is_dir():
        raise DirectorySelectionError(
            f"The selected export folder does not exist: {directory}"
        )
    return directory


def _launch_file_manager(command: list[str]) -> None:
    """Start a file manager without tying its lifetime to the recorder daemon."""

    try:
        subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except OSError as error:
        raise DirectoryOpenError(
            f"Could not open the session folder: {error}"
        ) from error
