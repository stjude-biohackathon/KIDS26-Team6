"""Install and remove shell capture hooks.

Each shell profile receives a short source block for a hook stored under
``~/.autocab/hooks/``. A timestamped backup makes each profile edit reversible.
Open shells read session state at each prompt.
"""

from __future__ import annotations

import shutil
import sys
import time
from dataclasses import dataclass
from importlib import resources
from pathlib import Path

from . import paths

MARKER_BEGIN = "# >>> wfrec hook >>>"
MARKER_END = "# <<< wfrec hook <<<"
PS_MARKER_BEGIN = "# >>> wfrec hook >>>"
PS_MARKER_END = "# <<< wfrec hook <<<"


@dataclass(frozen=True, slots=True)
class ShellTarget:
    """One shell we know how to hook."""

    name: str
    hook_resource: str
    hook_filename: str
    rc_candidates: tuple[str, ...]
    source_line: str
    install_all_rc_files: bool = False

    def rc_paths(self) -> tuple[Path, ...]:
        """Return the rc files that must receive this shell's hook."""

        home = Path.home()
        candidates = tuple(home / candidate for candidate in self.rc_candidates)
        if self.install_all_rc_files:
            return candidates
        for path in candidates:
            if path.exists():
                return (path,)
        return (candidates[0],)


def _powershell_profiles() -> tuple[str, ...]:
    """Both PowerShell profile locations.

    Teammates will have PowerShell 5.1 and 7 installed side by side, and they
    use different profile paths, so hooking only one silently misses half the
    terminals on the machine.
    """

    return (
        "Documents/PowerShell/profile.ps1",
        "Documents/WindowsPowerShell/profile.ps1",
    )


TARGETS: dict[str, ShellTarget] = {
    "bash": ShellTarget(
        name="bash",
        hook_resource="wfrec.bash",
        hook_filename="wfrec.bash",
        rc_candidates=(".bashrc", ".bash_profile", ".profile"),
        source_line='[ -f "{hook}" ] && . "{hook}"',
    ),
    "zsh": ShellTarget(
        name="zsh",
        hook_resource="wfrec.zsh",
        hook_filename="wfrec.zsh",
        rc_candidates=(".zshrc",),
        source_line='[ -f "{hook}" ] && . "{hook}"',
    ),
    "fish": ShellTarget(
        name="fish",
        hook_resource="wfrec.fish",
        hook_filename="wfrec.fish",
        rc_candidates=(".config/fish/config.fish",),
        source_line='test -f "{hook}"; and source "{hook}"',
    ),
    "powershell": ShellTarget(
        name="powershell",
        hook_resource="wfrec.ps1",
        hook_filename="wfrec.ps1",
        rc_candidates=_powershell_profiles(),
        source_line='if (Test-Path "{hook}") {{ . "{hook}" }}',
        # PowerShell 7 and Windows PowerShell 5.1 load different all-hosts
        # profiles. Install both because Python cannot reliably identify which
        # parent shell launched AutoCAB recording.
        install_all_rc_files=True,
    ),
}


def detect_shells() -> list[str]:
    """Guess which shells to hook on this machine.

    Detection is a convenience only; ``--shell`` always overrides it.
    """

    if sys.platform == "win32":  # pragma: no cover - Windows
        return ["powershell"]

    found: list[str] = []
    home = Path.home()
    if (home / ".zshrc").exists() or shutil.which("zsh"):
        found.append("zsh")
    if (home / ".bashrc").exists() or shutil.which("bash"):
        found.append("bash")
    if (home / ".config/fish/config.fish").exists() or shutil.which("fish"):
        found.append("fish")
    return found or ["bash"]


def materialize_hook(target: ShellTarget) -> Path:
    """Write the hook body into ``~/.autocab/hooks``, substituting the runtime dir.

    The runtime directory is resolved once here, in Python, and baked into the
    hook. Re-deriving ``XDG_RUNTIME_DIR``/``TMPDIR``/``LOCALAPPDATA`` precedence
    in four different shell languages would be four chances to disagree.
    """

    hooks = paths.hooks_dir()
    hooks.mkdir(parents=True, exist_ok=True)
    body = (
        resources.files("autocab.recording.hooks")
        .joinpath(target.hook_resource)
        .read_text(encoding="utf-8")
    )
    body = body.replace("@WFREC_RUN@", str(paths.runtime_dir()))
    destination = hooks / target.hook_filename
    destination.write_text(body, encoding="utf-8")
    destination.chmod(0o600)
    return destination


def _block(target: ShellTarget, hook_path: Path) -> str:
    line = target.source_line.format(hook=hook_path)
    return f"{MARKER_BEGIN}\n{line}\n{MARKER_END}\n"


def _strip_block(text: str) -> str:
    """Remove any existing wfrec block so installs stay idempotent."""

    out: list[str] = []
    skipping = False
    for line in text.splitlines(keepends=True):
        stripped = line.strip()
        if stripped == MARKER_BEGIN:
            skipping = True
            continue
        if stripped == MARKER_END:
            skipping = False
            continue
        if not skipping:
            out.append(line)
    return "".join(out)


def install(shells: list[str] | None = None) -> list[dict[str, str]]:
    """Install hooks for the given shells (auto-detected when omitted)."""

    paths.ensure_home()
    results: list[dict[str, str]] = []
    for name in shells or detect_shells():
        target = TARGETS.get(name)
        if target is None:
            results.append({"shell": name, "status": "unknown-shell"})
            continue

        hook_path = materialize_hook(target)
        for rc in target.rc_paths():
            rc.parent.mkdir(parents=True, exist_ok=True)
            existing = rc.read_text(encoding="utf-8") if rc.exists() else ""

            if existing:
                backup = rc.with_name(f"{rc.name}.wfrec-backup-{int(time.time())}")
                backup.write_text(existing, encoding="utf-8")
            else:
                backup = None

            cleaned = _strip_block(existing)
            # Append rather than prepend: frameworks like oh-my-zsh and
            # powerlevel10k rewrite hook arrays as they load, so running last
            # is the only way to be sure our registration survives.
            if cleaned and not cleaned.endswith("\n"):
                cleaned += "\n"
            rc.write_text(cleaned + _block(target, hook_path), encoding="utf-8")
            results.append(
                {
                    "shell": name,
                    "status": "installed",
                    "rc_file": str(rc),
                    "hook": str(hook_path),
                    "backup": str(backup) if backup else "",
                }
            )
    return results


def uninstall(shells: list[str] | None = None) -> list[dict[str, str]]:
    """Remove the wfrec block from rc files, leaving everything else intact."""

    results: list[dict[str, str]] = []
    for name in shells or list(TARGETS):
        target = TARGETS.get(name)
        if target is None:
            continue
        for candidate in target.rc_candidates:
            rc = Path.home() / candidate
            if not rc.exists():
                continue
            text = rc.read_text(encoding="utf-8")
            if MARKER_BEGIN not in text:
                continue
            rc.write_text(_strip_block(text), encoding="utf-8")
            results.append({"shell": name, "status": "removed", "rc_file": str(rc)})
    return results


def status() -> list[dict[str, object]]:
    """Report, per shell, whether a hook is installed and current."""

    report: list[dict[str, object]] = []
    for name, target in TARGETS.items():
        hook_path = paths.hooks_dir() / target.hook_filename
        installed_in: list[str] = []
        missing_from: list[str] = []
        for candidate in target.rc_candidates:
            rc = Path.home() / candidate
            if rc.exists() and MARKER_BEGIN in rc.read_text(encoding="utf-8"):
                installed_in.append(str(rc))
            elif target.install_all_rc_files:
                missing_from.append(str(rc))
        profiles_complete = bool(installed_in) and not missing_from
        report.append(
            {
                "shell": name,
                "hook_present": hook_path.exists(),
                "rc_files": installed_in,
                "missing_rc_files": missing_from,
                "installed": profiles_complete and hook_path.exists(),
            }
        )
    return report


def eval_line(shell: str = "bash") -> str:
    """Return a source line to paste into a terminal that is *already open*.

    Installing into an rc file only affects new shells. This gives an analyst a
    way to start recording in the terminal they are already sitting in.
    """

    target = TARGETS.get(shell)
    if target is None:
        raise ValueError(f"Unknown shell: {shell}")
    hook_path = materialize_hook(target)
    return target.source_line.format(hook=hook_path)
