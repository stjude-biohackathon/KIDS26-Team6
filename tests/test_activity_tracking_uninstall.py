import hashlib
import os
import subprocess
from pathlib import Path

import pytest


REPOSITORY_ROOT = Path(__file__).parents[1]
UNINSTALLER = REPOSITORY_ROOT / "skills/setup-activity-tracking/scripts/uninstall_dependencies.sh"
INSTALLER = REPOSITORY_ROOT / "skills/setup-activity-tracking/scripts/install_dependencies.sh"
ARTIFACT_SUFFIXES = {
    "devsql": ".cargo/bin/devsql",
    "devsql_receipt": ".config/devsql/devsql-receipt.json",
    "atuin": ".atuin/bin/atuin",
    "atuin_env": ".atuin/bin/env",
    "atuin_env_fish": ".atuin/bin/env.fish",
    "atuin_receipt": ".config/atuin/atuin-receipt.json",
}
CONFIRMATION_PHRASE = "REMOVE AUTOCAB ACTIVITY TRACKING"


def artifact_paths(home_directory: Path) -> dict[str, Path]:
    """Return the fixed paths the installer is allowed to own."""

    return {key: home_directory / suffix for key, suffix in ARTIFACT_SUFFIXES.items()}


def create_artifacts(home_directory: Path) -> dict[str, Path]:
    """Create representative files written by the upstream installers."""

    paths = artifact_paths(home_directory)
    for path in paths.values():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"test artifact: {path.name}\n", encoding="utf-8")
    return paths


def sha256_digest(path: Path) -> str:
    """Return the digest stored in the ownership receipt."""

    return hashlib.sha256(path.read_bytes()).hexdigest()


def write_receipt(
    home_directory: Path,
    *,
    owned_artifacts: set[str],
    shell_configuration: Path | None = None,
    path_line_added: bool = False,
    init_line_added: bool = False,
) -> Path:
    """Write the fixed, non-executable receipt consumed by the Bash script."""

    lines = ["schema|2"]
    for key, path in artifact_paths(home_directory).items():
        if key in owned_artifacts:
            lines.append(f"artifact|{path}|{sha256_digest(path)}")

    if shell_configuration:
        lines.append(
            "|".join(
                (
                    "shell",
                    "bash",
                    str(shell_configuration),
                    str(int(path_line_added)),
                    str(int(init_line_added)),
                )
            )
        )
    receipt_path = home_directory / ".local/state/autocab/activity-tracking-install.receipt"
    receipt_path.parent.mkdir(parents=True)
    receipt_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    receipt_path.chmod(0o600)
    return receipt_path


def run_uninstaller(home_directory: Path, *arguments: str) -> subprocess.CompletedProcess:
    """Run the public Bash interface in an isolated fake home directory."""

    environment = os.environ.copy()
    environment.update(
        {
            "HOME": str(home_directory),
            "XDG_CONFIG_HOME": str(home_directory / ".config"),
            "XDG_STATE_HOME": str(home_directory / ".local/state"),
            "ZDOTDIR": str(home_directory),
            "NO_COLOR": "1",
        }
    )
    return subprocess.run(
        ["/bin/bash", str(UNINSTALLER), *arguments],
        check=False,
        capture_output=True,
        text=True,
        env=environment,
    )


def write_executable(path: Path, content: str) -> None:
    """Write one executable used to isolate installer behavior in tests."""

    path.write_text(content, encoding="utf-8")
    path.chmod(0o755)


@pytest.mark.parametrize("platform_name", ["Linux", "Darwin"])
def test_installer_records_artifacts_for_bash_uninstall(
    tmp_path: Path,
    platform_name: str,
) -> None:
    tools_directory = tmp_path / "test-tools"
    tools_directory.mkdir()
    devsql_installer = tools_directory / "fake-devsql-installer.sh"
    atuin_installer = tools_directory / "fake-atuin-installer.sh"
    write_executable(
        devsql_installer,
        """#!/bin/bash
set -euo pipefail
mkdir -p "$HOME/.cargo/bin" "$XDG_CONFIG_HOME/devsql"
printf '#!/bin/bash\nexit 0\n' > "$HOME/.cargo/bin/devsql"
chmod 0755 "$HOME/.cargo/bin/devsql"
printf '{}\n' > "$XDG_CONFIG_HOME/devsql/devsql-receipt.json"
""",
    )
    write_executable(
        atuin_installer,
        """#!/bin/bash
set -euo pipefail
mkdir -p "$HOME/.atuin/bin" "$XDG_CONFIG_HOME/atuin"
printf '#!/bin/bash\nexit 0\n' > "$HOME/.atuin/bin/atuin"
chmod 0755 "$HOME/.atuin/bin/atuin"
printf '{}\n' > "$XDG_CONFIG_HOME/atuin/atuin-receipt.json"
""",
    )
    write_executable(
        tools_directory / "uname",
        f"#!/bin/bash\nprintf '{platform_name}\\n'\n",
    )
    write_executable(tools_directory / "xz", "#!/bin/bash\nexit 0\n")
    write_executable(
        tools_directory / "curl",
        """#!/bin/bash
set -euo pipefail
destination=""
while [[ $# -gt 0 ]]; do
    if [[ "$1" == "--output" ]]; then
        destination="$2"
        shift 2
    else
        shift
    fi
done
case "$destination" in
    *devsql*) cp "$FAKE_DEVSQL_INSTALLER" "$destination" ;;
    *atuin*) cp "$FAKE_ATUIN_INSTALLER" "$destination" ;;
    *) exit 1 ;;
esac
""",
    )
    environment = os.environ.copy()
    environment.update(
        {
            "HOME": str(tmp_path),
            "XDG_CONFIG_HOME": str(tmp_path / ".config"),
            "XDG_STATE_HOME": str(tmp_path / ".local/state"),
            "ZDOTDIR": str(tmp_path),
            "SHELL": "/bin/bash",
            "NO_COLOR": "1",
            "PATH": f"{tools_directory}:/usr/bin:/bin:/usr/sbin:/sbin",
            "FAKE_DEVSQL_INSTALLER": str(devsql_installer),
            "FAKE_ATUIN_INSTALLER": str(atuin_installer),
        }
    )

    install_result = subprocess.run(
        ["/bin/bash", str(INSTALLER)],
        check=False,
        capture_output=True,
        text=True,
        env=environment,
    )

    assert install_result.returncode == 0, install_result.stderr
    receipt_path = tmp_path / ".local/state/autocab/activity-tracking-install.receipt"
    receipt = receipt_path.read_text(encoding="utf-8")
    assert f"artifact|{tmp_path / '.cargo/bin/devsql'}|" in receipt
    assert f"artifact|{tmp_path / '.atuin/bin/atuin'}|" in receipt
    assert f"shell|bash|{tmp_path / '.bashrc'}|1|1" in receipt

    uninstall_result = run_uninstaller(
        tmp_path,
        "apply",
        CONFIRMATION_PHRASE,
    )

    assert uninstall_result.returncode == 0, uninstall_result.stderr
    assert not receipt_path.exists()
    assert not (tmp_path / ".cargo/bin/devsql").exists()
    assert not (tmp_path / ".atuin/bin/atuin").exists()


def test_receipt_does_not_claim_preexisting_tools(tmp_path: Path) -> None:
    paths = create_artifacts(tmp_path)
    write_receipt(tmp_path, owned_artifacts=set())

    result = run_uninstaller(tmp_path)

    assert result.returncode == 0
    assert paths["devsql"].exists()
    assert paths["atuin"].exists()


def test_changed_artifact_requires_manual_review(tmp_path: Path) -> None:
    paths = create_artifacts(tmp_path)
    write_receipt(tmp_path, owned_artifacts={"devsql"})
    paths["devsql"].write_text("updated outside AutoCAB\n", encoding="utf-8")

    result = run_uninstaller(tmp_path)

    assert result.returncode == 1
    assert "MANUAL_REVIEW" in result.stderr
    assert paths["devsql"].exists()


def test_apply_removes_owned_files_and_preserves_data(tmp_path: Path) -> None:
    paths = create_artifacts(tmp_path)
    shell_configuration = tmp_path / ".bashrc"
    path_line = 'export PATH="$HOME/.cargo/bin:$HOME/.atuin/bin:$PATH"'
    init_line = 'eval "$(atuin init bash --disable-ai)"'
    shell_configuration.write_text(
        f"# user configuration\n{path_line}\n{init_line}\n",
        encoding="utf-8",
    )
    history_path = tmp_path / ".local/share/atuin/history.db"
    history_path.parent.mkdir(parents=True)
    history_path.write_text("preserve me\n", encoding="utf-8")
    receipt_path = write_receipt(
        tmp_path,
        owned_artifacts=set(ARTIFACT_SUFFIXES),
        shell_configuration=shell_configuration,
        path_line_added=True,
        init_line_added=True,
    )

    result = run_uninstaller(
        tmp_path,
        "apply",
        CONFIRMATION_PHRASE,
    )

    assert result.returncode == 0, result.stderr
    assert all(not path.exists() for path in paths.values())
    assert history_path.exists()
    assert shell_configuration.read_text(encoding="utf-8") == "# user configuration\n"
    assert list(tmp_path.glob(".bashrc.autocab-backup-*"))
    assert not receipt_path.exists()


def test_manual_review_prevents_all_removal(tmp_path: Path) -> None:
    paths = create_artifacts(tmp_path)
    write_receipt(tmp_path, owned_artifacts={"devsql", "atuin"})
    paths["devsql"].write_text("updated outside AutoCAB\n", encoding="utf-8")

    result = run_uninstaller(
        tmp_path,
        "apply",
        CONFIRMATION_PHRASE,
    )

    assert result.returncode == 1
    assert paths["devsql"].exists()
    assert paths["atuin"].exists()
