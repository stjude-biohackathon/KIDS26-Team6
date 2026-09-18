"""State file and the hook-facing sentinel."""

from __future__ import annotations

from wfrec import paths
from wfrec.state import (
    RecorderState,
    SHELL_BACKEND_DEVSQL,
    SHELL_BACKEND_SPOOL,
    StateTransaction,
    read_sentinel,
    sentinel_enables,
)


def test_sentinel_absent_until_a_session_is_active(wfrec_home):
    assert read_sentinel() is None

    state = RecorderState.load()
    state.active_session = "2026-01-01T00-00-00_abcdef"
    state.save()

    sentinel = read_sentinel()
    assert sentinel is not None
    assert sentinel[0] == "2026-01-01T00-00-00_abcdef"


def test_stopping_removes_the_sentinel(wfrec_home):
    """Absence of the file is how every open shell learns to stop recording."""

    state = RecorderState.load()
    state.active_session = "s1"
    state.save()
    assert paths.active_path().exists()

    state.active_session = None
    state.save()
    assert not paths.active_path().exists()
    assert read_sentinel() is None


def test_flags_reflect_each_source(wfrec_home):
    state = RecorderState.load()
    state.active_session = "s1"
    state.sources = {
        "screen": True, "context": True, "shell": True, "agents": True, "files": True
    }
    state.save()
    flags = read_sentinel()[1]
    for source in ("screen", "context", "shell", "agents", "files"):
        assert sentinel_enables(flags, source), source

    state.sources["shell"] = False
    state.sources["screen"] = False
    state.save()
    flags = read_sentinel()[1]
    assert not sentinel_enables(flags, "shell")
    assert not sentinel_enables(flags, "screen")
    assert sentinel_enables(flags, "files")


def test_legacy_shell_output_does_not_change_hook_flags(wfrec_home):
    state = RecorderState.load()
    state.active_session = "s1"
    state.shell_output = True
    state.save()

    restored = RecorderState.load()
    assert restored.shell_output is True
    assert "O" not in read_sentinel()[1]


def test_devsql_backend_suppresses_only_the_shell_hook(wfrec_home):
    state = RecorderState.load()
    state.active_session = "s1"
    state.shell_backend = SHELL_BACKEND_DEVSQL
    state.save()

    restored = RecorderState.load()
    flags = read_sentinel()[1]
    assert restored.shell_backend == SHELL_BACKEND_DEVSQL
    assert restored.sources["shell"] is True
    assert not sentinel_enables(flags, "shell")
    assert sentinel_enables(flags, "context")


def test_unknown_shell_backend_defaults_to_hook_spool() -> None:
    state = RecorderState.from_dict({"shell_backend": "unknown"})

    assert state.shell_backend == SHELL_BACKEND_SPOOL


def test_sentinel_is_a_single_tab_separated_line(wfrec_home):
    """The hooks parse this with shell builtins, so the shape is a contract."""

    state = RecorderState.load()
    state.active_session = "s1"
    state.save()

    raw = paths.active_path().read_text(encoding="utf-8")
    assert raw.endswith("\n")
    assert len(raw.strip("\n").split("\t")) == 3
    assert "\n" not in raw.strip("\n")


def test_corrupt_state_file_yields_defaults_not_an_exception(wfrec_home):
    """A recorder must never refuse to start because its state file is broken."""

    paths.state_path().write_text("{not json", encoding="utf-8")
    state = RecorderState.load()
    assert state.active_session is None
    assert state.sources["shell"] is True


def test_transaction_persists_and_refreshes_sentinel(wfrec_home):
    with StateTransaction() as state:
        state.active_session = "s2"
        state.sources["screen"] = False
    assert read_sentinel()[0] == "s2"
    assert not sentinel_enables(read_sentinel()[1], "screen")


def test_runtime_dir_is_not_inside_home(wfrec_home, monkeypatch):
    """The sentinel must not live on a potentially-NFS $HOME.

    On an HPC workstation $HOME is frequently NFS, where a stat can block for
    seconds or hang on a stale mount -- which the shell hooks do on every
    prompt.
    """

    monkeypatch.delenv("WFREC_RUN", raising=False)
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(wfrec_home.parent / "xdg"))
    (wfrec_home.parent / "xdg").mkdir(exist_ok=True)
    assert paths.home() not in paths.runtime_dir().parents
