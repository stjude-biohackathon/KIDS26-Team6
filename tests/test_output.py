"""Regression tests for shared terminal presentation."""

from pathlib import Path

from rich.logging import RichHandler

from autocab.forge.renderer import closeLogging, configureLogging
from autocab.output import emit_json, error, success, summary


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


def test_forge_console_is_rich_and_keeps_debug_paths_in_the_log(tmp_path, capfd) -> None:
    """Forge progress stays concise while the file log retains diagnostics."""

    logger = configureLogging(tmp_path)
    uses_rich = any(isinstance(handler, RichHandler) for handler in logger.handlers)
    try:
        logger.debug("Rendered staged proposal: /private/tmp/render/proposal/example")
        logger.info("Building skill package example-std")
        success("Finished skill package example-std", stderr=True)
    finally:
        closeLogging(logger)

    captured = capfd.readouterr()
    assert uses_rich is True
    assert "Building skill package example-std" in captured.err
    assert "OK Finished skill package example-std" in captured.err
    assert "/private/tmp/render" not in captured.err
    log = (tmp_path / "logs" / "skill-forge.log").read_text(encoding="utf-8")
    assert "/private/tmp/render/proposal/example" in log
