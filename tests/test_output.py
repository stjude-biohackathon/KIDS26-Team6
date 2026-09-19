"""Regression tests for shared terminal presentation."""

from pathlib import Path

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
