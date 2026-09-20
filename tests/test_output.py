"""Regression tests for shared terminal presentation."""

from io import StringIO
from pathlib import Path

from rich.console import Console
from rich.logging import RichHandler
from rich.text import Text

from autocab import output
from autocab.forge.renderer import closeLogging, configureLogging
from autocab.output import (
    command_list,
    completed,
    emit_json,
    error,
    panel_summary,
    success,
    summary,
)


def test_human_output_keeps_dynamic_text_literal_and_plain_when_redirected(capfd) -> None:
    dynamic_value = "[bold]/tmp/session[/bold]"

    summary("Session", [("Path", dynamic_value)])
    success(dynamic_value)
    error(dynamic_value)

    captured = capfd.readouterr()
    assert dynamic_value in captured.out
    assert dynamic_value in captured.err
    assert "\x1b[" not in captured.out + captured.err


def test_json_output_contains_only_parseable_json(capfd) -> None:
    emit_json({"session": "example", "path": Path("/tmp/session")})

    captured = capfd.readouterr()
    assert captured.err == ""
    assert captured.out == '{\n  "session": "example",\n  "path": "/tmp/session"\n}\n'


def test_panel_summary_uses_a_compact_terminal_panel(monkeypatch) -> None:
    terminal_output = StringIO()
    monkeypatch.setattr(
        output,
        "console",
        Console(
            file=terminal_output,
            force_terminal=True,
            color_system="truecolor",
            no_color=False,
            width=100,
        ),
    )

    panel_summary(
        "AutoCAB dashboard",
        [("Status", "Running"), ("Browser", "http://127.0.0.1:8787")],
        hint="Network access: use --bind-all --allow-remote",
    )
    summary("AutoCAB status", [("Recording", "none")])
    command_list("Next", ["autocab status"])

    rendered = terminal_output.getvalue()
    plain_rendered = Text.from_ansi(rendered).plain
    assert "╭─ AutoCAB dashboard" in plain_rendered
    assert "\x1b[1;2;36m AutoCAB dashboard " in rendered
    assert "\x1b[1;36mAutoCAB status" in rendered
    assert "\x1b[1;36mNext" in rendered
    assert "Status" in plain_rendered
    assert "Browser" in plain_rendered
    assert "Network access: use --bind-all --allow-remote" in plain_rendered


def test_forge_console_is_rich_and_keeps_debug_paths_in_the_log(tmp_path, capfd) -> None:
    """Forge progress stays concise while the file log retains diagnostics."""

    logger = configureLogging(tmp_path)
    uses_rich = any(isinstance(handler, RichHandler) for handler in logger.handlers)
    try:
        logger.debug("Rendered staged proposal: /private/tmp/render/proposal/example")
        logger.debug("Building skill package example-std")
        completed("Skill package built · example-std", stderr=True)
    finally:
        closeLogging(logger)

    captured = capfd.readouterr()
    assert uses_rich is True
    assert "Building skill package example-std" not in captured.err
    assert "DONE Skill package built · example-std" in captured.err
    assert "/private/tmp/render" not in captured.err
    log = (tmp_path / "logs" / "skill-forge.log").read_text(encoding="utf-8")
    assert "Building skill package example-std" in log
    assert "/private/tmp/render/proposal/example" in log
